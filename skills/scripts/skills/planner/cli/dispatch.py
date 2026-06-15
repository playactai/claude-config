"""Generic batch RPC dispatch via function introspection.

Functions with 'ctx' as first parameter are auto-discovered from modules.
No decorators needed - write a function, it becomes a method.
"""

import inspect
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from skills.lib.io import atomic_write_text


def discover_methods(module) -> dict[str, Callable[..., Any]]:
    """Find all public functions with 'ctx' as first parameter.

    Convention: function_name -> method-name (underscores to hyphens)
    """
    methods = {}
    for name, func in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("_"):
            continue
        if func.__module__ != module.__name__:
            continue
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        if params and params[0] == "ctx":
            method_name = name.replace("_", "-")
            methods[method_name] = func
    return methods


def extract_params(func) -> tuple[set[str], dict[str, Any]]:
    """Extract required/optional params from function signature.

    Skips first param (ctx). Returns (required_set, optional_dict).
    """
    sig = inspect.signature(func)
    required, optional = set(), {}
    for pname, param in list(sig.parameters.items())[1:]:
        if param.default is inspect.Parameter.empty:
            required.add(pname)
        else:
            optional[pname] = param.default
    return required, optional


def dispatch(methods: dict, method: str, params: dict, ctx) -> Any:
    """Dispatch single RPC call. Returns result or raises."""
    if method not in methods:
        raise ValueError(f"Unknown method: {method}. Available: {sorted(methods.keys())}")

    func = methods[method]
    required, optional = extract_params(func)

    missing = required - set(params.keys())
    if missing:
        raise ValueError(f"Missing required params: {sorted(missing)}")

    # Build kwargs: start with optional defaults, override with provided params
    kwargs = {k: params.get(k, v) for k, v in optional.items()}
    kwargs.update({k: params[k] for k in required if k in params})
    # Also include any extra optional params that were provided
    for k in params:
        if k not in kwargs:
            kwargs[k] = params[k]

    return func(ctx, **kwargs)


def _snapshot_state(ctx) -> str | None:
    """Capture the RPC target's state file so a failed batch can roll back.

    Returns the file's current text, or None when no state file exists yet (so
    a rollback after a create removes the file). Contexts that do not expose
    state_file() get no snapshot and therefore run without rollback.
    """
    state_file = getattr(ctx, "state_file", None)
    if state_file is None:
        return None
    path = state_file()
    return path.read_text(encoding="utf-8") if path.exists() else None


def _restore_state(ctx, snapshot: str | None) -> bool:
    """Revert the state file to a pre-batch snapshot (None => remove it).

    Restores through atomic_write_text so a reader never sees a torn rollback.
    Returns True when a revert occurred, False when the ctx exposes no state file
    (nothing to roll back) -- the caller stamps rolled_back with this so it never
    reports a rollback that did not happen.
    """
    state_file = getattr(ctx, "state_file", None)
    if state_file is None:
        return False
    path = state_file()
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_text(path, snapshot)
    return True


def batch(methods: dict, requests: list[dict], ctx) -> list[dict]:
    """Execute a batch of RPC requests, persisting state once at flush.

    Each request: {"method": str, "params": dict, "id": any}
    Each response: {"id": any, "result": any} or {"id": any, "error": {...}}

    Commands validate-per-save but cache writes in the context; flush_batch()
    writes once after the loop. A failure in any command or during flush
    restores the pre-batch snapshot. When ctx exposes batch_lock(), the lock
    is held across snapshot+loop+flush+restore.

    Scope: commands must use ctx.load_*/save_* (not direct I/O) for caching to
    take effect. Single-call paths never call begin_batch, so save_* writes
    immediately as before.

    Response length: when a COMMAND fails, len(responses) == len(requests) --
    the failing request gets an error entry and every later request a skipped
    entry, so responses stay positionally aligned with requests. A FLUSH failure
    (every command succeeded but the single write failed) is not attributable to
    one request: all command results are flagged rolled_back and ONE extra entry
    with id=None reports the flush error, so len(responses) == len(requests) + 1.

    Raises:
        ValueError: if two requests share the same non-null id.
    """
    seen_ids: set = set()
    duplicate_ids: list = []
    for req in requests:
        rid = req.get("id")
        if rid is None:
            continue
        if rid in seen_ids:
            if rid not in duplicate_ids:
                duplicate_ids.append(rid)
        else:
            seen_ids.add(rid)
    if duplicate_ids:
        raise ValueError(f"Duplicate request id(s) in batch: {duplicate_ids}")

    lock_factory = getattr(ctx, "batch_lock", None)
    # QRContext exposes batch_lock (qr_write_lock) because QR verify fans out
    # concurrent per-item writers across processes, so the batch must hold the
    # lock across snapshot+loop+flush+restore. PlanContext deliberately omits
    # batch_lock: plan design is single-author (no concurrent writers), so
    # nullcontext() is correct, not an oversight. The batch-cache hooks
    # (begin_batch / flush_batch / end_batch) are a separate concern from the
    # cross-process lock.
    with (lock_factory() if lock_factory else nullcontext()):
        snapshot = _snapshot_state(ctx)
        results = []
        try:
            # begin_batch loads the state once; keep it inside the try so a
            # missing/unreadable state file (FileNotFoundError) is rolled back and
            # reported as a structured error like any command failure, not raised
            # past the CLI entrypoint (which only maps ValueError).
            if hasattr(ctx, "begin_batch"):
                ctx.begin_batch()
            for req in requests:
                req_id = req.get("id")
                method = req.get("method", "")
                params = req.get("params", {})
                result = dispatch(methods, method, params, ctx)
                results.append({"id": req_id, "result": result})
            if hasattr(ctx, "flush_batch"):
                ctx.flush_batch()
        except Exception as e:
            reverted = _restore_state(ctx, snapshot)
            for r in results:
                if "result" in r:
                    r["rolled_back"] = reverted
            failing_index = len(results)
            if failing_index < len(requests):
                failed_req = requests[failing_index]
                results.append(
                    {
                        "id": failed_req.get("id"),
                        "error": {"code": -32000, "message": str(e), "rolled_back": reverted},
                    }
                )
                for skipped in requests[failing_index + 1:]:
                    results.append(
                        {
                            "id": skipped.get("id"),
                            "error": {
                                "code": -32000,
                                "message": "skipped: batch rolled back due to an earlier failure",
                                "rolled_back": reverted,
                                "skipped": True,
                            },
                        }
                    )
            else:
                # flush itself failed; all command results were already appended
                results.append(
                    {
                        "id": None,
                        "error": {
                            "code": -32000,
                            "message": f"batch flush failed: {e}",
                            "rolled_back": reverted,
                        },
                    }
                )
        finally:
            if hasattr(ctx, "end_batch"):
                ctx.end_batch()
    return results


def list_methods(methods: dict) -> dict[str, dict]:
    """Return method signatures for discoverability."""
    result = {}
    for name, func in methods.items():
        required, optional = extract_params(func)
        doc = func.__doc__.split("\n")[0] if func.__doc__ else ""
        result[name] = {
            "required": sorted(required),
            "optional": sorted(optional.keys()),
            "description": doc,
        }
    return result
