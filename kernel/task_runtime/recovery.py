"""Recovery summaries and processing sub-state helpers for adaptive runtime control."""

from __future__ import annotations

from . import state as kernel_state


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_SUMMARY_FORMAT = "yt_transcript.recovery_summary/v1"
PROCESSING_STATE_FORMAT = "yt_transcript.processing_state/v1"


def _parse_int(value, default: int = 0) -> int:
    """Parse int-like values into ints."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def derive_processing_substate(result=None, *, runtime_status: dict | None = None) -> str:
    """Derive a processing sub-state from result/runtime status signals."""
    payload = result if isinstance(result, dict) else {}
    runtime_status = runtime_status if isinstance(runtime_status, dict) else {}
    if payload.get("replan_required", False):
        return "replan_pending"
    if payload.get("paused", False):
        return "chunk_warning"
    if payload.get("failed_count", 0) or payload.get("warning_count", 0):
        return "chunk_warning"
    if payload.get("processed_count", 0):
        total = _parse_int(payload.get("processed_count", 0), 0) + _parse_int(payload.get("skipped_count", 0), 0)
        if total and payload.get("success", False):
            return "processing_done"
        return "chunk_running"
    if runtime_status.get("pending_chunks", 0):
        return "chunk_queue_ready"
    if runtime_status.get("completed_chunks", 0) and runtime_status.get("pending_chunks", 0) == 0:
        return "processing_done"
    return "chunk_queue_ready"


def build_processing_state(work_dir: str = "", *, result=None) -> dict:
    """Build a processing-state summary for a work directory."""
    runtime_status = kernel_state.summarize_runtime_status(work_dir) if str(work_dir or "").strip() else {}
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "format": PROCESSING_STATE_FORMAT,
        "work_dir": str(work_dir or "").strip(),
        "substate": derive_processing_substate(result, runtime_status=runtime_status),
        "total_chunks": _parse_int(runtime_status.get("total_chunks"), 0),
        "completed_chunks": _parse_int(runtime_status.get("completed_chunks"), 0),
        "failed_chunks": _parse_int(runtime_status.get("failed_chunks"), 0),
        "pending_chunks": _parse_int(runtime_status.get("pending_chunks"), 0),
        "interrupted_chunks": _parse_int(runtime_status.get("interrupted_chunks"), 0),
        "superseded_chunks": _parse_int(runtime_status.get("superseded_chunks"), 0),
    }


def build_recovery_summary(work_dir: str = "", *, result=None) -> dict:
    """Build a resumability and recovery summary for a work directory."""
    runtime_status = kernel_state.summarize_runtime_status(work_dir) if str(work_dir or "").strip() else {}
    ownership = runtime_status.get("ownership", {}) if isinstance(runtime_status.get("ownership", {}), dict) else {}
    effective_status = str(runtime_status.get("effective_runtime_status", "")).strip()
    interrupted_chunks = _parse_int(runtime_status.get("interrupted_chunks"), 0)
    resumable = bool(
        effective_status in {"paused", "pause_requested"}
        or interrupted_chunks > 0
        or ownership.get("status") == "stale"
        or str((result or {}).get("aborted_reason", "")).strip()
    )
    recommended_action = "continue_stage"
    if interrupted_chunks > 0 or ownership.get("status") == "stale":
        recommended_action = "prepare_resume"
    elif effective_status in {"paused", "pause_requested"}:
        recommended_action = "resume_run"
    elif (result or {}).get("replan_required", False):
        recommended_action = "replan_remaining"
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "format": RECOVERY_SUMMARY_FORMAT,
        "work_dir": str(work_dir or "").strip(),
        "resumable": resumable,
        "recommended_action": recommended_action,
        "effective_runtime_status": effective_status,
        "ownership_status": str(ownership.get("status", "")).strip(),
        "interrupted_chunks": interrupted_chunks,
    }
