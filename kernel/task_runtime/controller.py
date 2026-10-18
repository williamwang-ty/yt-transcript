"""Generic runtime-controller helpers for owned mutations."""

from __future__ import annotations

from . import runtime as kernel_runtime


def build_delegated_runtime_ownership(runtime_ownership: dict | None = None) -> dict | None:
    """Mark an ownership record as delegated to a nested mutation call."""
    if isinstance(runtime_ownership, dict) and str(runtime_ownership.get("owner_id", "")).strip():
        delegated = dict(runtime_ownership)
        delegated["delegated"] = True
        return delegated
    return runtime_ownership


def resolve_runtime_mutation_ownership(work_dir: str, operation: str,
                                       runtime_ownership: dict | None = None) -> tuple[dict, bool]:
    """Resolve whether a mutation should reuse or acquire runtime ownership."""
    if isinstance(runtime_ownership, dict) and str(runtime_ownership.get("owner_id", "")).strip():
        return build_delegated_runtime_ownership(runtime_ownership) or {}, False
    return kernel_runtime.acquire_runtime_ownership(work_dir, operation), True


def finalize_mutation_result(result: dict, ownership: dict | None,
                             release_result: dict | None = None) -> dict:
    """Attach ownership release details to a mutation result payload."""
    if isinstance(result, dict) and isinstance(ownership, dict):
        result["ownership"] = kernel_runtime.finalize_runtime_ownership(ownership, release_result)
    return result


def runtime_ownership_error_parts(ownership: dict) -> tuple[str, str]:
    """Extract normalized error text from an ownership conflict payload."""
    error = str(ownership.get("error") or "Runtime ownership conflict").strip()
    message = str(ownership.get("message") or error).strip()
    return error, message


def run_owned_mutation(work_dir: str, operation: str, *, runtime_ownership: dict | None = None,
                       conflict_result_builder, mutation_fn):
    """Run a manifest mutation under exclusive runtime ownership."""
    ownership, owned_here = resolve_runtime_mutation_ownership(
        work_dir,
        operation,
        runtime_ownership=runtime_ownership,
    )
    if owned_here and not ownership.get("success", False):
        return conflict_result_builder(ownership)

    release_result = None
    try:
        result = mutation_fn(ownership)
    finally:
        if owned_here and ownership.get("success", False):
            release_result = kernel_runtime.release_runtime_ownership(work_dir, ownership.get("owner_id", ""))
    return finalize_mutation_result(result, ownership, release_result)


