"""Generic batch RPC dispatch via function introspection.

Functions with 'ctx' as first parameter are auto-discovered from modules.
No decorators needed - write a function, it becomes a method.
"""

import inspect
import json
import sys
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from skills.lib.io import atomic_write_text

from .plan_common import CREATE_REQUIRED, CSV_PARAM_NAMES


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


def _normalize_params(method: str, params: dict, valid: set) -> dict:
    """Normalize hyphenated param keys to underscores and unwrap/reject scalar lists.

    Single authority for key-shape (hyphen -> underscore; reject ambiguity) and
    value-shape (unwrap single-element scalar lists; reject multi-element; CSV
    params keep their list so parse_csv owns tokenization). *valid* (the
    required|optional key set) is passed in so dispatch owns the one computation
    used for both value-shaping here and the unknown-key check there.

    Canonicalizes param keys the same way method names are already normalized
    (hyphens -> underscores in discover_methods), so a batch caller writing
    ``decision-refs`` reaches the same ``decision_refs`` function param as the
    CLI's ``--decision-refs`` flag. Returns a new dict (never mutates *params*).
    """
    normalized: dict[str, object] = {}
    # Key-shape: hyphen -> underscore; reject if both forms of a stem are present.
    for k, v in params.items():
        stem = k.replace("-", "_")
        if stem in normalized:
            raise ValueError(
                f"Ambiguous params in {method}: both forms of {stem!r} present; drop one"
            )
        normalized[stem] = v
    # Value-shape: ONLY for known scalar params. Unknown keys fall through to
    # dispatch's unknown-key frame; CSV params keep their list so parse_csv owns
    # tokenization (preserving internal commas).
    for k, v in list(normalized.items()):
        if k not in valid or k in CSV_PARAM_NAMES:
            continue
        while isinstance(v, list) and len(v) == 1:   # recursive: fixes nested [[ "x" ]]
            v = v[0]
        if isinstance(v, list):                       # multi-element scalar -> clean reject
            raise ValueError(
                f"{k} must be a single value, got a list of {len(v)} values: {v!r}"
            )
        normalized[k] = v
    return normalized


def dispatch(methods: dict, method: str, params: dict, ctx) -> Any:
    """Dispatch single RPC call. Returns result or raises."""
    if method not in methods:
        raise ValueError(f"Unknown method: {method}. Available: {sorted(methods.keys())}")

    func = methods[method]
    required, optional = extract_params(func)
    valid = required | set(optional)

    # Normalize keys (hyphens->underscores, single-element lists unwrapped)
    # BEFORE key validation so the canonical forms are used for the
    # required/unknown checks. valid is computed once here and threaded into
    # _normalize_params and the unknown-key check below (one owner of "what is a
    # valid key").
    params = _normalize_params(method, params, valid)

    missing = required - set(params.keys())
    if missing:
        raise ValueError(f"Missing required params: {sorted(missing)}")

    # Reject unknown keys (the valid set is required + optional) so a typo
    # returns an actionable frame instead of a deep TypeError from
    # func(ctx, **kwargs). Hyphenated keys are already normalized above, so
    # only truly-unknown stems reach this check.
    unknown = set(params) - valid
    if unknown:
        raise ValueError(f"Unknown params: {sorted(unknown)}. Valid: {sorted(valid)}")

    # Every params key is now known and all required are present (checked above), so
    # the optional defaults simply underlay the provided params in one merge.
    kwargs = {**optional, **params}
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
    A SETUP failure (snapshot capture or begin_batch state load, before any
    command runs) writes nothing and is raised as ValueError; the CLI maps it
    to a clean error frame, exit 1.

    Raises:
        ValueError: if two requests share the same non-null id.
        ValueError: if snapshot capture or begin_batch state load fails before
            any command runs (batch-level error, nothing written).
    """
    seen_ids: set = set()
    duplicate_ids: list = []
    for req in requests:
        rid = req.get("id")
        if rid is None:
            continue
        # Guard the dedup scan against unhashable id types (list, dict) so direct
        # callers get the same clean ValueError that read_batch_requests provides
        # for the stdin path. str/int/float are the three JSON number/string types;
        # bool is rejected explicitly because isinstance(True, int) is True and
        # would cause a silent collision with integer id 1 in the dedup scan.
        if not isinstance(rid, (str, int, float)) or isinstance(rid, bool):
            raise ValueError(
                f"id must be a string, number, or null, got {type(rid).__name__} "
                f"in request method={req.get('method')!r}"
            )
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
        # Setup (snapshot + begin_batch) runs before any command and writes
        # nothing, so a failure here is a batch-level error, not a command
        # failure: convert it to ValueError so the CLI entrypoint's
        # `except ValueError: error_exit` emits a clean frame and exits 1, rather
        # than (a) escaping as a raw traceback on a non-ValueError read error or
        # (b) being misattributed to requests[0] with exit 0. Nothing was written,
        # so there is nothing to roll back.
        try:
            snapshot = _snapshot_state(ctx)
            if hasattr(ctx, "begin_batch"):
                ctx.begin_batch()
        except Exception as e:
            raise ValueError(f"batch could not start: {e}") from e
        results = []
        try:
            for req in requests:
                req_id = req.get("id")
                method = req.get("method", "")
                params = req.get("params") or {}  # null/absent both mean "no params"
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


def get_method_keys(methods: dict) -> dict[str, list[str]]:
    """Return {method_name: sorted_param_keys} for every discovered method.

    Used by the architect's _render_method_catalog (prompt lines). list_methods
    derives its own return shape (required/optional split) from the same
    extract_params primitive instead of this function. Both callers stay in
    sync because they share extract_params semantics.
    """
    result: dict[str, list[str]] = {}
    for name, func in methods.items():
        required, optional = extract_params(func)
        result[name] = sorted(required | set(optional))
    return result


def list_methods(methods: dict) -> dict[str, dict]:
    """Return method signatures for discoverability.

    For the dual create/update commands every field is optional in the signature
    (so one function serves both paths), so the signature-derived ``required`` set
    is empty and would tell an agent a create needs no fields. Surface
    ``create_required`` from CREATE_REQUIRED for those methods so this subcommand
    agrees with the architect catalog's CREATE/UPDATE prose rather than mislabeling
    e.g. set-intent's milestone as optional.
    """
    result = {}
    for name, func in methods.items():
        required, optional = extract_params(func)
        doc = func.__doc__.split("\n")[0] if func.__doc__ else ""
        entry: dict = {
            "required": sorted(required),
            "optional": sorted(optional.keys()),
            "description": doc,
        }
        if name in CREATE_REQUIRED:
            entry["create_required"] = sorted(CREATE_REQUIRED[name])
        result[name] = entry
    return result


def read_batch_requests(inline_arg: str | None, usage: str) -> list[dict]:
    """Read and shape-check a batch RPC request array from stdin.

    Shared by the plan and qr CLIs so the stdin contract -- no inline arg, a JSON
    array of objects, and a friendly JSON-decode frame -- can't drift between the two
    surfaces. `inline_arg` is the surface's positional/leftover token (None when
    absent); ANY provided value is rejected because an inline arg can't escape
    apostrophes in prose fields. `usage` is the surface-specific invocation echoed in
    that rejection. Raises ValueError on any misuse; the caller maps it to its own
    error_exit frame (error_exit is defined per-surface, so it is not called here).
    """
    if inline_arg is not None:
        raise ValueError(
            "batch reads JSON from stdin, not an inline argument. Write the batch "
            f"to a file and pipe it: {usage} batch < changes.json"
        )
    try:
        requests = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Invalid JSON in batch input: line {e.lineno} col {e.colno}: {e.msg}. "
            f"Write the batch to a file and pipe it via stdin: {usage} batch < changes.json"
        ) from e
    except UnicodeDecodeError as e:
        # json.load decodes stdin's bytes before parsing, so a non-UTF-8 pipe raises
        # here, ahead of any JSONDecodeError. Frame it the same friendly way -- it would
        # otherwise reach the caller's outer except only because UnicodeDecodeError
        # happens to subclass ValueError, emitting a raw codec message with no hint.
        raise ValueError(
            f"Batch input is not valid UTF-8 ({e.reason} at byte {e.start}). "
            f"Save the batch as UTF-8 and pipe it via stdin: {usage} batch < changes.json"
        ) from e
    if not isinstance(requests, list) or not all(isinstance(r, dict) for r in requests):
        raise ValueError("batch input must be a JSON array of {method, params} objects")
    # Shape-check each request so dispatch()/batch()/the role gate never hit a raw
    # TypeError or AttributeError on a malformed field -- the same LLM failure mode
    # (array-wrapping or mistyping a scalar) that _normalize_params already normalizes
    # for params. Reject here with the offending request's id/method:
    #   - params: a non-dict would raise AttributeError on .items() in _normalize_params,
    #   - method: a list/dict method makes the plan role gate's
    #     `method in restricted_methods` raise "unhashable type" (uncaught -> traceback);
    #     requiring a non-empty str also upgrades the otherwise bare "Unknown method: ."
    #     frame an empty/missing method would produce,
    #   - id: a list/dict (or bool) id is rejected up front so it never reaches batch()'s
    #     dedup scan. batch() now guards this directly too (covering direct callers); this
    #     stdin guard fires first and frames the rejection per-surface.
    for r in requests:
        params = r.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError(
                f"params must be a JSON object, got {type(params).__name__} "
                f"in request id={r.get('id')!r} method={r.get('method')!r}"
            )
        method = r.get("method")
        if not isinstance(method, str) or not method:
            raise ValueError(
                f"method must be a non-empty string, got {method!r} "
                f"in request id={r.get('id')!r}"
            )
        rid = r.get("id")
        # str/int/float are the three JSON number/string types; bool is rejected
        # explicitly because isinstance(True, int) is True and would cause a silent
        # collision with integer id 1 in batch()'s dedup scan. None means "no id".
        if rid is not None and (not isinstance(rid, (str, int, float)) or isinstance(rid, bool)):
            raise ValueError(
                f"id must be a string, number, or null, got {type(rid).__name__} "
                f"in request method={method!r}"
            )
    return requests
