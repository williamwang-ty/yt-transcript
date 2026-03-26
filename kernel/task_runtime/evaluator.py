"""Evaluator helpers for quality-gated action recommendations."""

from __future__ import annotations


EVALUATOR_SCHEMA_VERSION = 1
EVALUATOR_REPORT_FORMAT = "yt_transcript.evaluator_report/v1"


def _string_list(values) -> list[str]:
    """Normalize list-like values into non-empty strings."""
    if isinstance(values, (list, tuple, set)):
        return [str(item).strip() for item in values if str(item).strip()]
    if str(values or "").strip():
        return [str(values).strip()]
    return []


def build_evaluator_report(*, scope: str = "run", quality_gate_state: str = "unknown",
                           recommended_action: str = "", rationale: str = "",
                           warnings=None, hard_failures=None, metadata: dict | None = None) -> dict:
    """Build a normalized evaluator report."""
    return {
        "schema_version": EVALUATOR_SCHEMA_VERSION,
        "format": EVALUATOR_REPORT_FORMAT,
        "scope": str(scope or "run").strip() or "run",
        "quality_gate_state": str(quality_gate_state or "unknown").strip() or "unknown",
        "recommended_action": str(recommended_action or "").strip(),
        "rationale": " ".join(str(rationale or "").split()),
        "warnings": _string_list(warnings),
        "hard_failures": _string_list(hard_failures),
        "metadata": dict(metadata or {}),
    }


def derive_evaluator_report(command: str, *, quality_report: dict | None = None,
                            processing_state: dict | None = None, result=None) -> dict:
    """Derive a quality-gated evaluator report from command outputs."""
    payload = result if isinstance(result, dict) else {}
    quality_report = quality_report if isinstance(quality_report, dict) else {}
    processing_state = processing_state if isinstance(processing_state, dict) else {}
    normalized = str(command or "").strip()

    if quality_report:
        quality_action = str(quality_report.get("recommended_action", "")).strip()
        if quality_action == "fallback_to_deepgram":
            return build_evaluator_report(
                scope="run",
                quality_gate_state="warn",
                recommended_action="fallback_to_deepgram",
                rationale="quality report recommends rerouting source acquisition to Deepgram before continuing",
                warnings=quality_report.get("warnings", []),
                hard_failures=quality_report.get("hard_failures", []),
                metadata={"command": normalized},
            )
        if quality_report.get("passed", False):
            return build_evaluator_report(
                scope="run",
                quality_gate_state="pass",
                recommended_action="accept_output",
                rationale="quality report passed without hard failures",
                warnings=quality_report.get("warnings", []),
                hard_failures=quality_report.get("hard_failures", []),
                metadata={"command": normalized},
            )
        return build_evaluator_report(
            scope="run",
            quality_gate_state="fail",
            recommended_action="repair_chunk",
            rationale="quality report contains hard failures; repair is the lowest-cost recovery step",
            warnings=quality_report.get("warnings", []),
            hard_failures=quality_report.get("hard_failures", []),
            metadata={"command": normalized},
        )

    substate = str(processing_state.get("substate", "")).strip()
    if substate == "replan_pending":
        return build_evaluator_report(
            scope="processing",
            quality_gate_state="warn",
            recommended_action="replan_remaining",
            rationale="processing substate indicates replan is pending",
            warnings=payload.get("warnings", []),
            hard_failures=[],
            metadata={"command": normalized, "substate": substate},
        )

    if payload.get("warning_count", 0):
        return build_evaluator_report(
            scope="processing",
            quality_gate_state="warn",
            recommended_action="retry_action",
            rationale="warnings are present but no hard failure was detected",
            warnings=payload.get("warnings", []),
            hard_failures=[],
            metadata={"command": normalized, "substate": substate},
        )

    return build_evaluator_report(
        scope="run",
        quality_gate_state="pass" if payload.get("success", True) else "warn",
        recommended_action="continue_stage" if payload.get("success", True) else "request_human_escalation",
        rationale="no blocking quality signal detected",
        warnings=payload.get("warnings", []),
        hard_failures=[],
        metadata={"command": normalized, "substate": substate},
    )
