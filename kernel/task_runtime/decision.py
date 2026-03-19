"""Rule-first decision helpers for selecting actions from policy outputs."""

from __future__ import annotations

from . import contracts as kernel_contracts
from . import policy as kernel_policy


def _select_action(*, run_state: dict | None = None, result=None,
                   policy_evaluation: dict | None = None,
                   quality_report: dict | None = None) -> tuple[str, str, float]:
    """Select the next action from an allowed action set using rule-first heuristics."""
    run_state = run_state if isinstance(run_state, dict) else {}
    payload = result if isinstance(result, dict) else {}
    policy_evaluation = policy_evaluation if isinstance(policy_evaluation, dict) else {}
    quality_report = quality_report if isinstance(quality_report, dict) else {}
    allowed_actions = policy_evaluation.get("allowed_actions", []) if isinstance(policy_evaluation.get("allowed_actions", []), list) else []
    active_stage = str(run_state.get("active_stage", "")).strip()

    if quality_report.get("passed", False) and "accept_output" in allowed_actions:
        return "accept_output", "quality report passed; accept output", 0.95
    if payload.get("replan_required", False) and "replan_remaining" in allowed_actions:
        return "replan_remaining", "result explicitly requires replanning", 0.95
    if payload.get("paused", False) and "resume_run" in allowed_actions:
        return "resume_run", "runtime is paused and resumable", 0.9
    if payload.get("aborted", False) and "request_human_escalation" in allowed_actions:
        return "request_human_escalation", "run aborted; escalate for inspection", 0.85
    if policy_evaluation.get("budget_pressure_level") == "high" and "shrink_chunk_size" in allowed_actions:
        return "shrink_chunk_size", "high timeout pressure suggests shrinking chunk size", 0.8
    if active_stage == "verify" and not quality_report.get("passed", payload.get("passed", False)):
        if "repair_chunk" in allowed_actions:
            return "repair_chunk", "quality gate failed; repair is the lowest-cost next step", 0.8
        if "replan_remaining" in allowed_actions:
            return "replan_remaining", "quality gate failed and repair is unavailable", 0.75
    if active_stage == "processing" and payload.get("warning_count", 0) and "retry_action" in allowed_actions:
        return "retry_action", "warnings detected during processing; retry is allowed", 0.7
    if "continue_stage" in allowed_actions:
        return "continue_stage", "no blocking condition detected; continue nominal flow", 0.65
    if allowed_actions:
        return allowed_actions[0], "fallback to first allowed action", 0.5
    return "", "no allowed action available", 0.0


def build_decision_record_for_command(command: str, *, run_state: dict | None = None,
                                      result=None, policy_evaluation: dict | None = None,
                                      quality_report: dict | None = None) -> dict:
    """Build a rule-first decision record for a command result."""
    selected_action, reason, confidence = _select_action(
        run_state=run_state,
        result=result,
        policy_evaluation=policy_evaluation,
        quality_report=quality_report,
    )
    allowed_actions = policy_evaluation.get("allowed_actions", []) if isinstance(policy_evaluation, dict) else []
    observations_used = [
        f"command:{str(command or '').strip()}",
        f"stage:{str((run_state or {}).get('active_stage', '')).strip()}",
        f"status:{str((run_state or {}).get('effective_runtime_status', '')).strip()}",
    ]
    return kernel_contracts.build_decision_record(
        state_before=str((run_state or {}).get("lifecycle_state", "")).strip(),
        observations_used=observations_used,
        allowed_actions=allowed_actions,
        selected_action=selected_action,
        reason=reason,
        confidence=confidence,
        decider_type="rule",
        policy_checks={
            "profile": str((policy_evaluation or {}).get("profile", kernel_policy.DEFAULT_POLICY_PROFILE)).strip(),
            "budget_pressure_level": str((policy_evaluation or {}).get("budget_pressure_level", "normal")).strip(),
        },
    )
