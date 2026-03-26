"""Rule-first policy helpers for allowed runtime actions."""

from __future__ import annotations


POLICY_SCHEMA_VERSION = 1
POLICY_EVALUATION_FORMAT = "yt_transcript.policy_evaluation/v1"
DEFAULT_POLICY_PROFILE = "default"
STANDARD_ACTIONS = (
    "continue_stage",
    "retry_action",
    "repair_chunk",
    "replan_remaining",
    "shrink_chunk_size",
    "switch_model_profile",
    "fallback_to_deepgram",
    "degrade_output_mode",
    "accept_output",
    "pause_run",
    "resume_run",
    "abort_run",
    "request_human_escalation",
)


def _single_line_text(value) -> str:
    """Normalize arbitrary input into a single-line string."""
    return " ".join(str(value or "").split())


def _parse_bool(value) -> bool:
    """Parse boolean-like values conservatively."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _string_list(values) -> list[str]:
    """Normalize a list-like input into a list of strings."""
    if isinstance(values, (list, tuple, set)):
        return [str(item).strip() for item in values if str(item).strip()]
    if str(values or "").strip():
        return [str(values).strip()]
    return []


def _timeout_signal(result: dict) -> bool:
    """Infer whether a result payload is associated with timeout pressure."""
    payload = result if isinstance(result, dict) else {}
    text = " ".join([
        str(payload.get("aborted_reason", "")).strip(),
        str(payload.get("replan_reason", "")).strip(),
        str(payload.get("error", "")).strip(),
    ]).lower()
    return "timeout" in text or "timed out" in text


def _wants_deepgram_fallback(quality_report: dict | None = None) -> bool:
    """Return whether the current quality report recommends a Deepgram reroute."""
    quality_payload = quality_report if isinstance(quality_report, dict) else {}
    if str(quality_payload.get("recommended_action", "")).strip() == "fallback_to_deepgram":
        return True
    checks = quality_payload.get("checks", {}) if isinstance(quality_payload.get("checks", {}), dict) else {}
    return _parse_bool(checks.get("reroute_recommended")) and str(checks.get("reroute_target", "")).strip() == "deepgram"


def evaluate_policy(*, run_state: dict | None = None, result=None,
                    context: dict | None = None, quality_report: dict | None = None,
                    profile: str = DEFAULT_POLICY_PROFILE) -> dict:
    """Evaluate allowed actions for the current state and command result."""
    run_state = run_state if isinstance(run_state, dict) else {}
    payload = result if isinstance(result, dict) else {}
    context = context or {}
    quality_report = quality_report if isinstance(quality_report, dict) else {}
    active_stage = str(run_state.get("active_stage", "")).strip() or str(context.get("active_stage", "")).strip()
    lifecycle_state = str(run_state.get("lifecycle_state", "")).strip() or active_stage or "created"
    effective_status = str(run_state.get("effective_runtime_status", "")).strip() or lifecycle_state
    allowed_actions = []
    blocked_actions = []
    reroute_to_deepgram = _wants_deepgram_fallback(quality_report)

    if active_stage == "processing":
        allowed_actions.extend(["continue_stage", "pause_run", "abort_run"])
        if payload.get("replan_required", False):
            allowed_actions.extend(["replan_remaining", "request_human_escalation"])
        if payload.get("paused", False) or effective_status in {"paused", "pause_requested"}:
            allowed_actions.extend(["resume_run", "abort_run"])
        if payload.get("aborted", False):
            allowed_actions.extend(["request_human_escalation", "abort_run"])
        if payload.get("failed_count", 0) or payload.get("warning_count", 0):
            allowed_actions.extend(["retry_action", "repair_chunk"])
        if _timeout_signal(payload):
            allowed_actions.extend(["shrink_chunk_size", "switch_model_profile"])
    elif active_stage == "verify":
        if quality_report.get("passed", payload.get("passed", False)):
            allowed_actions.append("accept_output")
        else:
            allowed_actions.extend(["repair_chunk", "replan_remaining", "degrade_output_mode", "request_human_escalation", "abort_run"])
    elif active_stage == "assemble":
        allowed_actions.extend(["continue_stage", "abort_run"])
    elif active_stage in {"planning", "normalize", "preflight", "source"}:
        if reroute_to_deepgram:
            allowed_actions.extend(["fallback_to_deepgram", "request_human_escalation", "abort_run"])
        else:
            allowed_actions.extend(["continue_stage", "request_human_escalation", "abort_run"])
    else:
        allowed_actions.extend(["continue_stage", "abort_run"])

    if context.get("allow_degrade") is False:
        blocked_actions.append("degrade_output_mode")
        allowed_actions = [action for action in allowed_actions if action != "degrade_output_mode"]

    if effective_status == "cancellation_requested":
        allowed_actions = [action for action in allowed_actions if action in {"abort_run", "request_human_escalation"}]

    budget_pressure_level = "normal"
    if _timeout_signal(payload):
        budget_pressure_level = "high"
    elif payload.get("warning_count", 0):
        budget_pressure_level = "medium"

    degrade_allowed = "degrade_output_mode" in allowed_actions
    escalation_required = bool(payload.get("aborted", False) and not payload.get("success", False))

    deduped_allowed = []
    for action in allowed_actions:
        if action not in deduped_allowed:
            deduped_allowed.append(action)

    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "format": POLICY_EVALUATION_FORMAT,
        "profile": str(profile or DEFAULT_POLICY_PROFILE).strip() or DEFAULT_POLICY_PROFILE,
        "active_stage": active_stage,
        "lifecycle_state": lifecycle_state,
        "effective_runtime_status": effective_status,
        "allowed_actions": deduped_allowed,
        "blocked_actions": _string_list(blocked_actions),
        "degrade_allowed": degrade_allowed,
        "escalation_required": escalation_required,
        "budget_pressure_level": budget_pressure_level,
        "reason": _single_line_text(payload.get("replan_reason", "") or payload.get("aborted_reason", "") or payload.get("error", "")),
    }
