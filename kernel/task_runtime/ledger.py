"""Budget-ledger helpers for runtime observations and action accounting."""

from __future__ import annotations


LEDGER_SCHEMA_VERSION = 1
BUDGET_LEDGER_FORMAT = "yt_transcript.budget_ledger/v1"


def _parse_int(value, default: int = 0) -> int:
    """Parse int-like values into ints."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def build_budget_ledger(*, warning_count: int = 0, processed_count: int = 0,
                        failed_count: int = 0, skipped_count: int = 0,
                        replan_count: int = 0, superseded_count: int = 0,
                        pause_count: int = 0, estimated_cost: float = 0.0,
                        latency_ms: int = 0) -> dict:
    """Build a normalized budget-ledger payload."""
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "format": BUDGET_LEDGER_FORMAT,
        "warning_count": max(0, _parse_int(warning_count, 0)),
        "processed_count": max(0, _parse_int(processed_count, 0)),
        "failed_count": max(0, _parse_int(failed_count, 0)),
        "skipped_count": max(0, _parse_int(skipped_count, 0)),
        "replan_count": max(0, _parse_int(replan_count, 0)),
        "superseded_count": max(0, _parse_int(superseded_count, 0)),
        "pause_count": max(0, _parse_int(pause_count, 0)),
        "estimated_cost": max(0.0, float(estimated_cost or 0.0)),
        "latency_ms": max(0, _parse_int(latency_ms, 0)),
    }


def derive_budget_ledger(result=None, *, context: dict | None = None) -> dict:
    """Derive a budget ledger from a result payload and command context."""
    payload = result if isinstance(result, dict) else {}
    context = context or {}
    pause = payload.get("pause", {}) if isinstance(payload.get("pause", {}), dict) else {}
    runtime = payload.get("runtime", {}) if isinstance(payload.get("runtime", {}), dict) else {}
    return build_budget_ledger(
        warning_count=payload.get("warning_count", len(payload.get("warnings", []) if isinstance(payload.get("warnings", []), list) else [])),
        processed_count=payload.get("processed_count", runtime.get("processed_count", 0)),
        failed_count=payload.get("failed_count", runtime.get("failed_count", 0)),
        skipped_count=payload.get("skipped_count", runtime.get("skipped_count", 0)),
        replan_count=payload.get("replan_count", runtime.get("resume_repair_count", 0)),
        superseded_count=payload.get("superseded_count", runtime.get("superseded_count", 0)),
        pause_count=(1 if pause.get("requested", False) else 0) + _parse_int(runtime.get("pause_count"), 0),
        estimated_cost=float(context.get("estimated_cost", 0.0) or 0.0),
        latency_ms=payload.get("latency_ms", 0),
    )
