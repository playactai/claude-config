"""Generic batch RPC dispatch via function introspection.

Functions with 'ctx' as first parameter are auto-discovered from modules.
No decorators needed - write a function, it becomes a method.
"""

import inspect
from collections.abc import Callable
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


def _restore_state(ctx, snapshot: str | None) -> None:
    """Revert the state file to a pre-batch snapshot (None => remove it).

    Restores through atomic_write_text so a reader never sees a torn rollback.
    """
    state_file = getattr(ctx, "state_file", None)
    if state_file is None:
        return
    path = state_file()
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_text(path, snapshot)


def batch(methods: dict, requests: list[dict], ctx) -> list[dict]:
    """Execute a batch of RPC requests as an all-or-nothing transaction.

    Each request: {"method": str, "params": dict, "id": any}
    Each response: {"id": any, "result": any} or {"id": any, "error": {...}}

    Commands persist incrementally (each save_plan/save_qr renames into place),
    so a mid-batch failure would otherwise leave earlier requests applied. To
    make the batch atomic the target state file is snapshotted up front and
    restored on the first failure, which also stops processing -- the response
    still lists every request's outcome, with the failing entry flagged
    rolled_back so the caller knows nothing was persisted.

    Scope: all-or-nothing covers exactly ctx.state_file() (plan.json or
    qr-{phase}.json); a command that mutates any other file is not rolled back.
    Assumes a single batch writer -- batch runs sequentially in one process and
    is not issued concurrently with the per-item, separately-locked update-item
    writers.

    Raises:
        ValueError: if two requests share the same non-null id (ambiguous
            results / accidental duplicate replays).
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

    snapshot = _snapshot_state(ctx)

    results = []
    for req in requests:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        try:
            result = dispatch(methods, method, params, ctx)
            results.append({"id": req_id, "result": result})
        except Exception as e:
            _restore_state(ctx, snapshot)
            results.append(
                {"id": req_id, "error": {"code": -32000, "message": str(e), "rolled_back": True}}
            )
            break
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
