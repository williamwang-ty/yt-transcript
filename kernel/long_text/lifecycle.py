"""Manifest lifecycle and resume-repair helpers for long-text runs."""

from __future__ import annotations

_LOCAL_NAMES = {
    '_bind_utils_globals',
    '_new_chunk_manifest_entry',
    '_resolve_chunk_output_filename',
    '_resolve_chunk_output_path',
    '_ensure_chunk_runtime_defaults',
    '_infer_resume_runtime_status',
    '_prepare_manifest_for_resume',
    '_format_resume_report',
    '_prepare_resume_impl',
    '_resume_run_impl'
}


def _bind_utils_globals() -> None:
    """Bind delegated helper names from `yt_transcript_utils` into module globals."""
    import yt_transcript_utils as utils

    for name, value in utils.__dict__.items():
        if name.startswith("__") or name in _LOCAL_NAMES:
            continue
        globals()[name] = value


def _new_chunk_manifest_entry(chunk_id: int, chunk_content: str, budget: dict,
                              config: dict | None = None, *, raw_path: str = "",
                              processed_path: str = "", plan_id: str = "",
                              continuity_prev_chunk_id: int | None = None,
                              chunk_contract: dict | None = None,
                              continuity_policy: dict | None = None) -> dict:
    """Create the persisted manifest record for a single planned chunk."""
    _bind_utils_globals()
    config = config or {}
    chunk_contract = chunk_contract if isinstance(chunk_contract, dict) else _build_manifest_chunk_contract()
    continuity_policy = continuity_policy if isinstance(continuity_policy, dict) else _build_manifest_continuity_policy(config)
    # The chunk entry is the durable checkpoint for one unit of work. It keeps
    # both execution bookkeeping and the continuity metadata needed for retries.
    return {
        "id": chunk_id,
        "chunk_id": chunk_id,
        "plan_id": plan_id,
        "raw_path": raw_path,
        "processed_path": processed_path,
        "source_kind": str(chunk_contract.get("source_kind", "text")).strip() or "text",
        "boundary_mode": str(chunk_contract.get("boundary_mode", "strict")).strip() or "strict",
        "output_scope": str(chunk_contract.get("output_scope", "current_chunk_only")).strip() or "current_chunk_only",
        "continuity_mode": str(continuity_policy.get("mode", "reference_only")).strip() or "reference_only",
        "char_count": len(chunk_content),
        "input_chars": len(chunk_content),
        "estimated_input_tokens": _estimate_tokens(chunk_content, "tokens", config),
        "input_tokens": _estimate_tokens(chunk_content, "tokens", config),
        "token_count_source": budget.get("token_count_source", ""),
        "tail_context_text": _extract_tail_sentences(
            chunk_content,
            max(0, _parse_int(continuity_policy.get("tail_sentences"), DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES)),
            config,
        ),
        "processed_tail_context_text": "",
        "processed_input_tail_context_text": "",
        "processed_input_section_title": "",
        "continuity_prev_chunk_id": continuity_prev_chunk_id,
        "continuity_context_chars": 0,
        "continuity_context_tokens": 0,
        "continuity_section_title": "",
        "glossary_terms": [],
        "glossary_term_count": 0,
        "glossary_context_tokens": 0,
        "semantic_anchors": [],
        "semantic_anchor_count": 0,
        "semantic_context_tokens": 0,
        "last_section_title": "",
        "output_chars": 0,
        "actual_output_tokens": 0,
        "planned_max_output_tokens": budget.get("planned_max_output_tokens", 0),
        "status": "pending",
        "attempts": 0,
        "attempt_logs": [],
        "recovery_attempts": 0,
        "recovery_logs": [],
        "last_error": "",
        "last_error_type": "",
        "error_type": "",
        "latency_ms": None,
        "request_url": "",
        "streaming_used": False,
        "autotune_target_tokens": 0,
        "autotune_next_target_tokens": 0,
        "autotune_event": "",
        "autotune_reason": "",
        "superseded_by_plan_id": "",
        "updated_at": "",
        "started_at": "",
        "completed_at": "",
        "control": _build_chunk_control_state(),
    }

def _resolve_chunk_output_filename(chunk_info: dict, prompt_name: str = "") -> str:
    """Resolve the output filename for a chunk under the current prompt."""
    _bind_utils_globals()
    chunk_id = _parse_int(chunk_info.get("id", chunk_info.get("chunk_id", 0)), 0)
    if prompt_name == "summarize":
        return f"summary_chunk_{chunk_id:03d}.txt"
    filename = str(chunk_info.get("processed_path", "")).strip()
    return filename or f"processed_{chunk_id:03d}.md"

def _resolve_chunk_output_path(work_path: Path, chunk_info: dict, prompt_name: str = "") -> Path:
    """Resolve the absolute output path for a chunk result file."""
    _bind_utils_globals()
    return work_path / _resolve_chunk_output_filename(chunk_info, prompt_name)

def _ensure_chunk_runtime_defaults(manifest: dict, runtime: dict, plan: dict,
                                   prompt_budget: dict, request_url: str,
                                   planned_max_output_tokens: int,
                                   autotune_state: dict) -> None:
    """Backfill chunk/runtime fields needed by resume and processing loops."""
    _bind_utils_globals()
    for chunk_info in manifest.get("chunks", []):
        chunk_info.setdefault("chunk_id", chunk_info.get("id", 0))
        chunk_info.setdefault("plan_id", runtime.get("active_plan_id", plan.get("plan_id", "")))
        chunk_info.setdefault("status", "pending")
        chunk_info.setdefault("attempts", 0)
        chunk_info.setdefault("attempt_logs", [])
        chunk_info.setdefault("recovery_attempts", 0)
        chunk_info.setdefault("recovery_logs", [])
        chunk_info.setdefault("last_error", "")
        chunk_info.setdefault("last_error_type", "")
        chunk_info.setdefault("error_type", chunk_info.get("last_error_type", ""))
        chunk_info.setdefault("latency_ms", None)
        chunk_info.setdefault("input_chars", chunk_info.get("char_count", 0))
        chunk_info.setdefault("estimated_input_tokens", 0)
        chunk_info.setdefault("input_tokens", chunk_info.get("estimated_input_tokens", 0))
        chunk_info.setdefault("token_count_source", prompt_budget["token_count_source"])
        chunk_info.setdefault("tail_context_text", "")
        chunk_info.setdefault("processed_tail_context_text", "")
        chunk_info.setdefault("processed_input_tail_context_text", "")
        chunk_info.setdefault("processed_input_section_title", "")
        chunk_info.setdefault("continuity_prev_chunk_id", None)
        chunk_info.setdefault("continuity_context_chars", 0)
        chunk_info.setdefault("continuity_context_tokens", 0)
        chunk_info.setdefault("continuity_section_title", "")
        chunk_info.setdefault("glossary_terms", [])
        chunk_info.setdefault("glossary_term_count", 0)
        chunk_info.setdefault("glossary_context_tokens", 0)
        chunk_info.setdefault("semantic_anchors", [])
        chunk_info.setdefault("semantic_anchor_count", 0)
        chunk_info.setdefault("semantic_context_tokens", 0)
        chunk_info.setdefault("last_section_title", "")
        chunk_info.setdefault("output_chars", 0)
        chunk_info.setdefault("actual_output_tokens", 0)
        chunk_info.setdefault("prompt_tokens", prompt_budget["prompt_tokens"])
        chunk_info.setdefault("planned_max_output_tokens", planned_max_output_tokens)
        chunk_info.setdefault("request_url", request_url)
        chunk_info.setdefault("streaming_used", False)
        chunk_info.setdefault("autotune_target_tokens", autotune_state["current_target_tokens"])
        chunk_info.setdefault("autotune_next_target_tokens", autotune_state["current_target_tokens"])
        chunk_info.setdefault("autotune_event", "")
        chunk_info.setdefault("autotune_reason", "")
        chunk_info.setdefault("superseded_by_plan_id", "")
        chunk_info.setdefault("updated_at", "")
        chunk_info.setdefault("started_at", "")
        chunk_info.setdefault("completed_at", "")
        _ensure_chunk_control_state(chunk_info)

def _infer_resume_runtime_status(runtime: dict, chunks: list[dict]) -> str:
    """Infer the safest runtime status to expose after loading a prior run."""
    _bind_utils_globals()
    active_chunks = [chunk for chunk in (chunks or []) if chunk.get("status") != SUPERSEDED_CHUNK_STATUS]
    active_statuses = {str(chunk.get("status", "pending")).strip() or "pending" for chunk in active_chunks}
    previous_status = str(runtime.get("status", "pending")).strip() or "pending"

    if runtime.get("replan_required", False):
        return "aborted"
    if active_chunks and active_statuses == {"done"}:
        return "completed"
    if INTERRUPTED_CHUNK_STATUS in active_statuses:
        return RESUMABLE_RUNTIME_STATUS
    if active_statuses and active_statuses.issubset({"done", "failed"}) and "failed" in active_statuses:
        return "completed_with_errors"
    if previous_status in {"running", PAUSED_RUNTIME_STATUS}:
        return RESUMABLE_RUNTIME_STATUS
    if "pending" in active_statuses:
        if previous_status in {RESUMABLE_RUNTIME_STATUS, PAUSED_RUNTIME_STATUS, "aborted", "completed", "completed_with_errors"}:
            return RESUMABLE_RUNTIME_STATUS
        return "pending"
    if "failed" in active_statuses:
        if previous_status in {RESUMABLE_RUNTIME_STATUS, PAUSED_RUNTIME_STATUS, "aborted"}:
            return RESUMABLE_RUNTIME_STATUS
        return "completed_with_errors"
    return previous_status

def _prepare_manifest_for_resume(manifest: dict, work_path: Path, prompt_name: str = "") -> dict:
    """Repair stale chunk/runtime state so an interrupted run can resume safely."""
    _bind_utils_globals()
    runtime = manifest.get("runtime", {}) if isinstance(manifest.get("runtime", {}), dict) else {}
    now = _now_iso()
    report = {
        "repaired": False,
        "repair_count": 0,
        "promoted_done_chunk_ids": [],
        "interrupted_chunk_ids": [],
        "demoted_missing_output_chunk_ids": [],
        "runtime_status_before": str(runtime.get("status", "pending")).strip() or "pending",
        "runtime_status_after": "",
        "interrupted_count": 0,
        "warnings": [],
    }

    def mark_promoted_done(chunk_info: dict, output_path: Path, reason: str) -> None:
        """Promote a chunk to done when its durable output already exists."""
        try:
            output_text = output_path.read_text(encoding="utf-8")
        except OSError:
            output_text = ""
        chunk_info["status"] = "done"
        chunk_info["output_chars"] = len(output_text)
        chunk_info["actual_output_tokens"] = max(
            _parse_int(chunk_info.get("actual_output_tokens"), 0),
            _estimate_tokens(output_text, "tokens") if output_text else 0,
        )
        chunk_info["last_error"] = ""
        chunk_info["last_error_type"] = ""
        chunk_info["error_type"] = ""
        chunk_info["completed_at"] = str(chunk_info.get("completed_at", "")).strip() or now
        chunk_info["updated_at"] = now
        report["promoted_done_chunk_ids"].append(_parse_int(chunk_info.get("id"), 0))
        report["warnings"].append(reason)

    def mark_interrupted(chunk_info: dict, reason: str, bucket: str) -> None:
        """Mark a chunk as interrupted when its recorded state is no longer trustworthy."""
        chunk_info["status"] = INTERRUPTED_CHUNK_STATUS
        chunk_info["last_error"] = reason
        chunk_info["last_error_type"] = "resume_interrupted"
        chunk_info["error_type"] = "resume_interrupted"
        chunk_info["completed_at"] = ""
        chunk_info["updated_at"] = now
        report[bucket].append(_parse_int(chunk_info.get("id"), 0))
        report["warnings"].append(reason)

    for chunk_info in manifest.get("chunks", []):
        if chunk_info.get("status") == SUPERSEDED_CHUNK_STATUS:
            continue
        output_path = _resolve_chunk_output_path(work_path, chunk_info, prompt_name)
        output_exists = output_path.exists()
        status = str(chunk_info.get("status", "pending")).strip() or "pending"

        if status == "running":
            # A stale `running` chunk means the previous process exited mid-run.
            # If the output file exists we can safely promote it to done; if not,
            # we downgrade it to interrupted for an explicit retry.
            if output_exists:
                mark_promoted_done(
                    chunk_info,
                    output_path,
                    f"Resume repair: promoted stale running chunk {_parse_int(chunk_info.get('id'), 0)} to done from {output_path.name}",
                )
            else:
                mark_interrupted(
                    chunk_info,
                    f"Resume repair: marked stale running chunk {_parse_int(chunk_info.get('id'), 0)} as interrupted because {output_path.name} is missing",
                    "interrupted_chunk_ids",
                )
        elif status == "done" and not output_exists:
            # A missing checkpoint file means the manifest claimed success before
            # the durable output was actually present on disk.
            mark_interrupted(
                chunk_info,
                f"Resume repair: demoted done chunk {_parse_int(chunk_info.get('id'), 0)} because checkpoint file {output_path.name} is missing",
                "demoted_missing_output_chunk_ids",
            )
        elif status == INTERRUPTED_CHUNK_STATUS and output_exists:
            # If a prior run wrote the output before failing to update manifest
            # state, prefer the durable file over the stale interrupted marker.
            mark_promoted_done(
                chunk_info,
                output_path,
                f"Resume repair: promoted interrupted chunk {_parse_int(chunk_info.get('id'), 0)} to done from {output_path.name}",
            )

    runtime_status_after = _infer_resume_runtime_status(runtime, manifest.get("chunks", []))
    runtime["last_resume_check_at"] = now
    runtime["interrupted_count"] = sum(
        1
        for chunk in manifest.get("chunks", [])
        if chunk.get("status") == INTERRUPTED_CHUNK_STATUS
    )
    if report["promoted_done_chunk_ids"] or report["interrupted_chunk_ids"] or report["demoted_missing_output_chunk_ids"]:
        runtime["last_resume_repair_at"] = now
        runtime["resume_repair_count"] = max(0, _parse_int(runtime.get("resume_repair_count"), 0)) +             len(report["promoted_done_chunk_ids"]) + len(report["interrupted_chunk_ids"]) + len(report["demoted_missing_output_chunk_ids"])
    runtime["status"] = runtime_status_after
    runtime["updated_at"] = now

    report["runtime_status_after"] = runtime_status_after
    report["interrupted_count"] = runtime["interrupted_count"]
    report["repair_count"] = len(report["promoted_done_chunk_ids"]) + len(report["interrupted_chunk_ids"]) + len(report["demoted_missing_output_chunk_ids"])
    report["repaired"] = report["repair_count"] > 0 or report["runtime_status_before"] != runtime_status_after
    return report

def _format_resume_report(report: dict) -> str:
    """Format a compact human-readable summary of resume repairs."""
    _bind_utils_globals()
    if not isinstance(report, dict) or not report.get("repaired", False):
        return ""
    parts = []
    if report.get("promoted_done_chunk_ids"):
        parts.append(f"promoted={len(report['promoted_done_chunk_ids'])}")
    if report.get("interrupted_chunk_ids"):
        parts.append(f"interrupted={len(report['interrupted_chunk_ids'])}")
    if report.get("demoted_missing_output_chunk_ids"):
        parts.append(f"demoted_missing_output={len(report['demoted_missing_output_chunk_ids'])}")
    parts.append(f"runtime={report.get('runtime_status_before', '')}->{report.get('runtime_status_after', '')}")
    return "Resume repair: " + ", ".join(parts)

def _prepare_resume_impl(work_dir: str, prompt_name: str = "", config_path: str = None,
                         input_key: str = "raw_path") -> dict:
    """Load, repair, and persist manifest state before a resume attempt."""
    _bind_utils_globals()
    del input_key
    work_path = Path(work_dir)
    manifest_path = work_path / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = _load_optional_config(config_path)
    resolved_prompt_name = str(prompt_name or manifest.get("plan", {}).get("prompt_name", manifest.get("prompt_name", ""))).strip()
    prompt_template = ""
    if resolved_prompt_name:
        prompt_template = _resolve_prompt_template_path(resolved_prompt_name).read_text(encoding="utf-8")
    prompt_budget = _calculate_chunk_budget(resolved_prompt_name, prompt_template, config)
    manifest = _ensure_manifest_structure(
        manifest,
        prompt_name=resolved_prompt_name,
        prompt_budget=prompt_budget,
        recommended_chunk_size=_recommended_chunk_size(resolved_prompt_name, config),
        source_file=str(manifest.get("source_file", "")).strip(),
        config=config,
    )
    plan = manifest["plan"]
    runtime = manifest["runtime"]
    planned_max_output_tokens = max(1, _parse_int(plan.get("planned_max_output_tokens"), prompt_budget["planned_max_output_tokens"]))
    autotune_state = _build_autotune_state(plan, config, manifest.get("autotune"))
    _ensure_chunk_runtime_defaults(
        manifest,
        runtime,
        plan,
        prompt_budget,
        request_url=str(runtime.get("last_request_url", "")).strip(),
        planned_max_output_tokens=planned_max_output_tokens,
        autotune_state=autotune_state,
    )
    resume = _prepare_manifest_for_resume(manifest, work_path, resolved_prompt_name)
    _refresh_manifest_token_source_summary(manifest)
    _sync_manifest_legacy_fields(manifest)
    _write_manifest(manifest_path, manifest)
    return {
        "success": True,
        "manifest_path": str(manifest_path),
        "prompt_name": resolved_prompt_name,
        "resume": resume,
        "runtime": manifest.get("runtime", {}),
    }

def _resume_run_impl(work_dir: str, reason: str = "") -> dict:
    """Clear pause state and mark the runtime as resumable again."""
    _bind_utils_globals()
    work_path = Path(work_dir)
    manifest_path = work_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = _ensure_manifest_structure(manifest)
    runtime = manifest.get("runtime", {})
    runtime_control = _ensure_runtime_control_state(runtime)

    pause_before = kernel_state.summarize_runtime_pause_request(work_dir)
    pause_after = kernel_state.clear_runtime_pause(work_dir)

    runtime_status_before = str(runtime.get("status", "pending")).strip() or "pending"
    runtime_status_after = _infer_resume_runtime_status(runtime, manifest.get("chunks", []))
    if runtime_status_before == PAUSED_RUNTIME_STATUS and runtime_status_after == PAUSED_RUNTIME_STATUS:
        runtime_status_after = RESUMABLE_RUNTIME_STATUS

    now = _now_iso()
    runtime["status"] = runtime_status_after
    runtime["last_resumed_at"] = now
    runtime["last_resume_reason"] = str(reason or "").strip() or str(pause_before.get("reason", "") or "").strip()
    runtime["updated_at"] = now
    runtime_control["pause_requested"] = False
    runtime_control["pause_reason"] = ""
    runtime_control["resumed_at"] = now
    if not runtime_control.get("paused_at"):
        runtime_control["paused_at"] = str(runtime.get("last_paused_at", "") or "")
    _sync_manifest_legacy_fields(manifest)
    _refresh_manifest_token_source_summary(manifest)
    _write_manifest(manifest_path, manifest)
    return {
        "success": True,
        "resumed": True,
        "pause": {
            "before": pause_before,
            "after": kernel_state.summarize_runtime_pause_request(work_dir),
        },
        "runtime": runtime,
        "runtime_status_before": runtime_status_before,
        "runtime_status_after": runtime_status_after,
        "message": "Runtime pause cleared; run may now be resumed.",
    }
