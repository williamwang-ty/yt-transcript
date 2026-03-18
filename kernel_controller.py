import kernel_runtime


def build_delegated_runtime_ownership(runtime_ownership: dict | None = None) -> dict | None:
    if isinstance(runtime_ownership, dict) and str(runtime_ownership.get("owner_id", "")).strip():
        delegated = dict(runtime_ownership)
        delegated["delegated"] = True
        return delegated
    return runtime_ownership


def resolve_runtime_mutation_ownership(work_dir: str, operation: str,
                                       runtime_ownership: dict | None = None) -> tuple[dict, bool]:
    if isinstance(runtime_ownership, dict) and str(runtime_ownership.get("owner_id", "")).strip():
        return build_delegated_runtime_ownership(runtime_ownership) or {}, False
    return kernel_runtime.acquire_runtime_ownership(work_dir, operation), True


def finalize_mutation_result(result: dict, ownership: dict | None,
                             release_result: dict | None = None) -> dict:
    if isinstance(result, dict) and isinstance(ownership, dict):
        result["ownership"] = kernel_runtime.finalize_runtime_ownership(ownership, release_result)
    return result


def runtime_ownership_error_parts(ownership: dict) -> tuple[str, str]:
    error = str(ownership.get("error") or "Runtime ownership conflict").strip()
    message = str(ownership.get("message") or error).strip()
    return error, message


def run_owned_mutation(work_dir: str, operation: str, *, runtime_ownership: dict | None = None,
                       conflict_result_builder, mutation_fn):
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


def run_auto_replan_loop(*, work_dir: str, prompt_name: str, extra_instruction: str = "",
                         config_path: str = None, input_key: str = "raw_path",
                         force: bool = False, max_replans: int = 3,
                         runtime_ownership: dict | None = None,
                         process_chunks_fn, replan_remaining_fn,
                         current_superseded_count_fn) -> dict:
    delegated_runtime_ownership = build_delegated_runtime_ownership(runtime_ownership)

    if input_key != "raw_path":
        result = process_chunks_fn(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=False,
            input_key=input_key,
            force=force,
            runtime_ownership=delegated_runtime_ownership,
        )
        result.setdefault(
            "message",
            "auto-replan is available only for raw_path plans; rerun manually if replanning is required.",
        )
        return result

    aggregate = {
        "processed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "superseded_count": 0,
        "warnings": [],
        "warning_count": 0,
        "output_files": [],
        "replan_count": 0,
        "request_url": "",
        "aborted": False,
        "aborted_reason": "",
        "paused": False,
        "pause_reason": "",
        "pause": {},
        "success": False,
        "control": {},
        "cancellation": {},
    }

    next_force = force
    last_result = {}
    for _ in range(max(0, max_replans) + 1):
        last_result = process_chunks_fn(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=False,
            input_key=input_key,
            force=next_force,
            runtime_ownership=delegated_runtime_ownership,
        )
        aggregate["processed_count"] += last_result.get("processed_count", 0)
        aggregate["failed_count"] += last_result.get("failed_count", 0)
        aggregate["skipped_count"] += last_result.get("skipped_count", 0)
        aggregate["warnings"].extend(last_result.get("warnings", []))
        aggregate["output_files"].extend(last_result.get("output_files", []))
        aggregate["request_url"] = last_result.get("request_url", aggregate["request_url"])
        aggregate["control"] = last_result.get("control", aggregate.get("control", {}))
        aggregate["cancellation"] = last_result.get("cancellation", aggregate.get("cancellation", {}))
        aggregate["pause"] = last_result.get("pause", aggregate.get("pause", {}))
        aggregate["superseded_count"] = current_superseded_count_fn()

        if not last_result.get("replan_required", False):
            aggregate.update({
                "success": last_result.get("success", False),
                "aborted": last_result.get("aborted", False),
                "aborted_reason": last_result.get("aborted_reason", ""),
                "paused": last_result.get("paused", False),
                "pause_reason": last_result.get("pause_reason", ""),
                "replan_required": False,
                "replan_reason": "",
                "plan": last_result.get("plan", {}),
            })
            if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
                aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
                aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
            aggregate["warning_count"] = len(aggregate["warnings"])
            aggregate["superseded_count"] = current_superseded_count_fn()
            return aggregate

        if aggregate["replan_count"] >= max(0, max_replans):
            aggregate.update({
                "success": False,
                "aborted": True,
                "aborted_reason": last_result.get("aborted_reason", "Reached max auto-replan limit"),
                "paused": last_result.get("paused", False),
                "pause_reason": last_result.get("pause_reason", ""),
                "replan_required": True,
                "replan_reason": last_result.get("replan_reason", ""),
                "plan": last_result.get("plan", {}),
            })
            if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
                aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
                aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
            aggregate["warning_count"] = len(aggregate["warnings"])
            aggregate["superseded_count"] = current_superseded_count_fn()
            return aggregate

        replan_result = replan_remaining_fn(
            work_dir,
            prompt_name=prompt_name,
            config_path=config_path,
            input_key=input_key,
            runtime_ownership=delegated_runtime_ownership,
        )
        aggregate["replan_count"] += 1
        aggregate["warnings"].extend(replan_result.get("warnings", []))
        aggregate["superseded_count"] = current_superseded_count_fn()
        if not replan_result.get("success", False):
            replan_error = replan_result.get("error") or replan_result.get("message") or "unknown error"
            aggregate.update({
                "success": False,
                "aborted": True,
                "aborted_reason": f"Auto-replan failed: {replan_error}",
                "paused": last_result.get("paused", False),
                "pause_reason": last_result.get("pause_reason", ""),
                "replan_required": True,
                "replan_reason": last_result.get("replan_reason", "") or replan_error,
                "plan": last_result.get("plan", {}),
            })
            if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
                aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
                aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
            aggregate["warning_count"] = len(aggregate["warnings"])
            return aggregate
        next_force = False

    aggregate.update({
        "success": False,
        "aborted": True,
        "aborted_reason": last_result.get("aborted_reason", "Reached max auto-replan limit"),
        "paused": last_result.get("paused", False),
        "pause_reason": last_result.get("pause_reason", ""),
        "replan_required": last_result.get("replan_required", False),
        "replan_reason": last_result.get("replan_reason", ""),
        "plan": last_result.get("plan", {}),
    })
    if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
        aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
        aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
    aggregate["warning_count"] = len(aggregate["warnings"])
    aggregate["superseded_count"] = current_superseded_count_fn()
    return aggregate
