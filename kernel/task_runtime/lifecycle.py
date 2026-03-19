"""Lifecycle shell helpers for bounded runtime-state transitions."""

from __future__ import annotations

import hashlib
import time

from . import contracts as kernel_contracts
from . import state as kernel_state


LIFECYCLE_SCHEMA_VERSION = 1
LIFECYCLE_TRANSITION_FORMAT = "yt_transcript.lifecycle_transition/v1"
TOP_LEVEL_LIFECYCLE_STATES = (
    "created",
    "preflighted",
    "sourcing",
    "normalized",
    "planned",
    "processing",
    "verifying",
    "assembling",
    "completed",
    "degraded",
    "paused",
    "failed_terminal",
)


def _now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _single_line_text(value) -> str:
    """Normalize arbitrary input into a single-line string."""
    return " ".join(str(value or "").split())


def _transition_id(command: str, work_dir: str = "", observed_at: str = "") -> str:
    """Build a short stable transition identifier."""
    payload = f"{command}:{work_dir}:{observed_at or _now_iso()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def top_level_states() -> list[str]:
    """Return the supported top-level lifecycle states."""
    return list(TOP_LEVEL_LIFECYCLE_STATES)


def observe_runtime_snapshot(work_dir: str = "") -> dict:
    """Observe the current persisted runtime status for a work directory."""
    observed_at = _now_iso()
    resolved_work_dir = str(work_dir or "").strip()
    if not resolved_work_dir:
        return {
            "available": False,
            "work_dir": "",
            "runtime_summary": {},
            "run_state": kernel_contracts.build_run_state(
                lifecycle_state="created",
                active_stage="",
                effective_runtime_status="created",
                work_dir="",
                started_at=observed_at,
                updated_at=observed_at,
            ),
            "observed_at": observed_at,
        }

    runtime_summary = kernel_state.summarize_runtime_status(resolved_work_dir)
    run_state = kernel_contracts.derive_run_state(
        "runtime-status",
        runtime_summary,
        context={"work_dir": resolved_work_dir},
        trace_id="",
    )
    return {
        "available": True,
        "work_dir": resolved_work_dir,
        "runtime_summary": runtime_summary,
        "run_state": run_state,
        "observed_at": observed_at,
    }


def _control_signal_for_command(command: str, result) -> str:
    """Infer a control-signal semantic from a command result."""
    payload = result if isinstance(result, dict) else {}
    normalized = str(command or "").strip()
    if normalized == "pause-run" and payload.get("requested", False):
        return "pause_requested"
    if normalized == "cancel-run" and payload.get("requested", False):
        return "cancellation_requested"
    if normalized == "resume-run" and payload.get("resumed", payload.get("success", False)):
        return "resume_completed"
    return ""


def build_lifecycle_transition(command: str, result, *, context: dict | None = None,
                               trace_id: str = "", before: dict | None = None,
                               after: dict | None = None) -> dict:
    """Build a lifecycle transition summary around a command result."""
    context = context or {}
    resolved_work_dir = str(context.get("work_dir", "")).strip()
    before_snapshot = before if isinstance(before, dict) else observe_runtime_snapshot(resolved_work_dir)
    after_snapshot = after if isinstance(after, dict) else observe_runtime_snapshot(resolved_work_dir)
    before_state = before_snapshot.get("run_state", {}) if isinstance(before_snapshot.get("run_state", {}), dict) else {}
    bundle = kernel_contracts.build_command_contract_bundle(
        command,
        result,
        context=context,
        trace_id=trace_id,
    )
    after_state = after_snapshot.get("run_state", {}) if isinstance(after_snapshot.get("run_state", {}), dict) else {}
    if not after_state:
        after_state = bundle.get("run_state", {}) if isinstance(bundle.get("run_state", {}), dict) else {}
    state_before = str(before_state.get("lifecycle_state", "created")).strip() or "created"
    state_after = str(after_state.get("lifecycle_state", state_before)).strip() or state_before
    transition_kind = "steady_state" if state_before == state_after else f"{state_before}->{state_after}"
    work_dir = str(after_state.get("work_dir", "") or before_snapshot.get("work_dir", "")).strip()
    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "format": LIFECYCLE_TRANSITION_FORMAT,
        "transition_id": _transition_id(str(command or "").strip(), work_dir, before_snapshot.get("observed_at", "")),
        "command": str(command or "").strip(),
        "work_dir": work_dir,
        "active_stage": str(after_state.get("active_stage", before_state.get("active_stage", ""))).strip(),
        "state_before": state_before,
        "state_after": state_after,
        "effective_status_before": str(before_state.get("effective_runtime_status", state_before)).strip(),
        "effective_status_after": str(after_state.get("effective_runtime_status", state_after)).strip(),
        "transition_kind": transition_kind,
        "control_signal": _control_signal_for_command(command, result),
        "success": bool(bundle.get("action_result", {}).get("success", False)),
        "trace_id": str(trace_id or "").strip(),
        "observed_at_before": str(before_snapshot.get("observed_at", "") or "").strip(),
        "observed_at_after": str(after_snapshot.get("observed_at", "") or _now_iso()).strip(),
    }


def summarize_lifecycle_transition(transition: dict | None = None) -> dict:
    """Build a concise lifecycle summary suitable for telemetry."""
    payload = dict(transition or {})
    return {
        "transition_id": str(payload.get("transition_id", "")).strip(),
        "active_stage": str(payload.get("active_stage", "")).strip(),
        "state_before": str(payload.get("state_before", "")).strip(),
        "state_after": str(payload.get("state_after", "")).strip(),
        "transition_kind": str(payload.get("transition_kind", "")).strip(),
        "control_signal": str(payload.get("control_signal", "")).strip(),
        "success": bool(payload.get("success", False)),
    }


def execute_lifecycle_command(command: str, action_fn, *, context: dict | None = None,
                              trace_id: str = ""):
    """Execute a command through the lifecycle shell and attach transition metadata."""
    context = context or {}
    resolved_work_dir = str(context.get("work_dir", "")).strip()
    before = observe_runtime_snapshot(resolved_work_dir)
    result = action_fn()
    if not isinstance(result, dict):
        return result
    after = observe_runtime_snapshot(resolved_work_dir)
    enriched = dict(result)
    from . import recovery as runtime_recovery

    if resolved_work_dir:
        enriched["processing_state"] = runtime_recovery.build_processing_state(resolved_work_dir, result=enriched)
        enriched["recovery"] = runtime_recovery.build_recovery_summary(resolved_work_dir, result=enriched)
    enriched["lifecycle"] = build_lifecycle_transition(
        command,
        enriched,
        context=context,
        trace_id=trace_id,
        before=before,
        after=after,
    )
    return enriched
