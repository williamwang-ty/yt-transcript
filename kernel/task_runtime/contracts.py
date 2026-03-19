"""Runtime contracts for task specs, run state, actions, artifacts, and quality reports."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path


CONTRACT_SCHEMA_VERSION = 1
TASK_SPEC_FORMAT = "yt_transcript.task_spec/v1"
RUN_STATE_FORMAT = "yt_transcript.run_state/v1"
OBSERVATION_FORMAT = "yt_transcript.observation/v1"
DECISION_RECORD_FORMAT = "yt_transcript.decision_record/v1"
ACTION_REQUEST_FORMAT = "yt_transcript.action_request/v1"
ACTION_RESULT_FORMAT = "yt_transcript.action_result/v1"
ARTIFACT_REF_FORMAT = "yt_transcript.artifact_ref/v1"
QUALITY_REPORT_FORMAT = "yt_transcript.quality_report/v1"
CONTRACT_BUNDLE_FORMAT = "yt_transcript.contract_bundle/v1"


def _now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _single_line_text(value) -> str:
    """Normalize arbitrary input into a single-line string."""
    return " ".join(str(value or "").split())


def _parse_bool(value, default: bool = False) -> bool:
    """Parse common boolean-like values into bools."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_float(value, default: float = 0.0) -> float:
    """Parse float-like values into floats."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_int(value, default: int = 0) -> int:
    """Parse int-like values into ints."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _string_list(values) -> list[str]:
    """Normalize a list-like input into a list of non-empty strings."""
    if isinstance(values, (list, tuple, set)):
        return [str(item).strip() for item in values if str(item).strip()]
    if str(values or "").strip():
        return [str(values).strip()]
    return []


def _stable_id(prefix: str, seed: str = "") -> str:
    """Build a stable short identifier from a prefix and seed."""
    payload = f"{prefix}:{seed or _now_iso()}"
    return f"{prefix}_{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"


def build_task_spec(*, task_id: str = "", source_ref: str = "", output_mode: str = "markdown",
                    bilingual: bool = False, quality_profile: str = "balanced",
                    speed_priority: str = "balanced", cost_budget: float = 0.0,
                    latency_budget: float = 0.0, allowed_fallbacks=None,
                    human_escalation_policy: str = "on_blocking_failure",
                    metadata: dict | None = None, created_at: str = "") -> dict:
    """Build a normalized task-spec payload."""
    resolved_source_ref = str(source_ref or "").strip()
    resolved_task_id = str(task_id or _stable_id("task", resolved_source_ref)).strip()
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": TASK_SPEC_FORMAT,
        "task_id": resolved_task_id,
        "source_ref": resolved_source_ref,
        "output_mode": str(output_mode or "markdown").strip() or "markdown",
        "bilingual": bool(bilingual),
        "quality_profile": str(quality_profile or "balanced").strip() or "balanced",
        "speed_priority": str(speed_priority or "balanced").strip() or "balanced",
        "cost_budget": max(0.0, _parse_float(cost_budget, 0.0)),
        "latency_budget": max(0.0, _parse_float(latency_budget, 0.0)),
        "allowed_fallbacks": _string_list(allowed_fallbacks),
        "human_escalation_policy": (
            str(human_escalation_policy or "on_blocking_failure").strip() or "on_blocking_failure"
        ),
        "metadata": dict(metadata or {}),
        "created_at": str(created_at or _now_iso()).strip(),
    }


def build_run_state(*, run_id: str = "", task_id: str = "", lifecycle_state: str = "created",
                    active_stage: str = "", effective_runtime_status: str = "",
                    work_dir: str = "", policy_profile: str = "default",
                    ownership: dict | None = None, budget_ledger: dict | None = None,
                    metadata: dict | None = None, started_at: str = "",
                    updated_at: str = "") -> dict:
    """Build a normalized run-state payload."""
    resolved_run_id = str(run_id or _stable_id("run", f"{task_id}:{work_dir}:{active_stage}")).strip()
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": RUN_STATE_FORMAT,
        "run_id": resolved_run_id,
        "task_id": str(task_id or "").strip(),
        "lifecycle_state": str(lifecycle_state or "created").strip() or "created",
        "active_stage": str(active_stage or "").strip(),
        "effective_runtime_status": str(effective_runtime_status or "").strip(),
        "work_dir": str(work_dir or "").strip(),
        "policy_profile": str(policy_profile or "default").strip() or "default",
        "ownership": dict(ownership or {}),
        "budget_ledger": dict(budget_ledger or {}),
        "metadata": dict(metadata or {}),
        "started_at": str(started_at or _now_iso()).strip(),
        "updated_at": str(updated_at or _now_iso()).strip(),
    }


def build_observation(*, observation_id: str = "", observation_type: str = "",
                      source_command: str = "", state_ref: str = "",
                      severity: str = "info", data: dict | None = None,
                      observed_at: str = "") -> dict:
    """Build a normalized observation payload."""
    seed = f"{source_command}:{observation_type}:{state_ref}:{observed_at or _now_iso()}"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": OBSERVATION_FORMAT,
        "observation_id": str(observation_id or _stable_id("obs", seed)).strip(),
        "observation_type": str(observation_type or "command_result").strip() or "command_result",
        "source_command": str(source_command or "").strip(),
        "state_ref": str(state_ref or "").strip(),
        "severity": str(severity or "info").strip() or "info",
        "data": dict(data or {}),
        "observed_at": str(observed_at or _now_iso()).strip(),
    }


def build_decision_record(*, decision_id: str = "", state_before: str = "",
                          observations_used=None, allowed_actions=None,
                          selected_action: str = "", reason: str = "",
                          confidence: float = 0.0, decider_type: str = "rule",
                          policy_checks: dict | None = None, decided_at: str = "") -> dict:
    """Build a normalized decision-record payload."""
    seed = f"{state_before}:{selected_action}:{decided_at or _now_iso()}"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": DECISION_RECORD_FORMAT,
        "decision_id": str(decision_id or _stable_id("decision", seed)).strip(),
        "state_before": str(state_before or "").strip(),
        "observations_used": _string_list(observations_used),
        "allowed_actions": _string_list(allowed_actions),
        "selected_action": str(selected_action or "").strip(),
        "reason": _single_line_text(reason),
        "confidence": max(0.0, min(1.0, _parse_float(confidence, 0.0))),
        "decider_type": str(decider_type or "rule").strip() or "rule",
        "policy_checks": dict(policy_checks or {}),
        "decided_at": str(decided_at or _now_iso()).strip(),
    }


def build_action_request(*, action_id: str = "", action_type: str = "",
                         tool_name: str = "", inputs: dict | None = None,
                         requested_at: str = "") -> dict:
    """Build a normalized action-request payload."""
    seed = f"{action_type}:{tool_name}:{requested_at or _now_iso()}"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": ACTION_REQUEST_FORMAT,
        "action_id": str(action_id or _stable_id("action", seed)).strip(),
        "action_type": str(action_type or "").strip(),
        "tool_name": str(tool_name or "").strip(),
        "inputs": dict(inputs or {}),
        "requested_at": str(requested_at or _now_iso()).strip(),
    }


def build_action_result(*, action_id: str = "", action_type: str = "",
                        tool_name: str = "", success: bool = False,
                        warnings=None, artifacts_created=None, cost: dict | None = None,
                        failure_type: str = "", message: str = "",
                        completed_at: str = "") -> dict:
    """Build a normalized action-result payload."""
    seed = f"{action_type}:{tool_name}:{completed_at or _now_iso()}"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": ACTION_RESULT_FORMAT,
        "action_id": str(action_id or _stable_id("action", seed)).strip(),
        "action_type": str(action_type or "").strip(),
        "tool_name": str(tool_name or "").strip(),
        "success": bool(success),
        "warnings": _string_list(warnings),
        "artifacts_created": _string_list(artifacts_created),
        "cost": dict(cost or {}),
        "failure_type": str(failure_type or "").strip(),
        "message": _single_line_text(message),
        "completed_at": str(completed_at or _now_iso()).strip(),
    }


def build_artifact_ref(*, artifact_id: str = "", artifact_type: str = "",
                       path: str = "", source_action_id: str = "",
                       version: int = 1, parent_artifacts=None,
                       quality_status: str = "") -> dict:
    """Build a normalized artifact reference."""
    resolved_path = str(path or "").strip()
    seed = f"{artifact_type}:{resolved_path}:{version}"
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": ARTIFACT_REF_FORMAT,
        "artifact_id": str(artifact_id or _stable_id("artifact", seed)).strip(),
        "artifact_type": str(artifact_type or "unknown").strip() or "unknown",
        "path": resolved_path,
        "source_action_id": str(source_action_id or "").strip(),
        "version": max(1, _parse_int(version, 1)),
        "parent_artifacts": _string_list(parent_artifacts),
        "quality_status": str(quality_status or "").strip(),
    }


def build_quality_report(*, coverage_score: float = 0.0, missing_sections=None,
                         term_consistency_score: float = 0.0, translation_risk: str = "",
                         structure_integrity: str = "", recommended_action: str = "",
                         passed: bool = False, warnings=None, hard_failures=None,
                         checks: dict | None = None, generated_at: str = "") -> dict:
    """Build a normalized quality-report payload."""
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": QUALITY_REPORT_FORMAT,
        "coverage_score": max(0.0, min(1.0, _parse_float(coverage_score, 0.0))),
        "missing_sections": _string_list(missing_sections),
        "term_consistency_score": max(0.0, min(1.0, _parse_float(term_consistency_score, 0.0))),
        "translation_risk": str(translation_risk or "").strip(),
        "structure_integrity": str(structure_integrity or "").strip(),
        "recommended_action": str(recommended_action or "").strip(),
        "passed": bool(passed),
        "warnings": _string_list(warnings),
        "hard_failures": _string_list(hard_failures),
        "checks": dict(checks or {}),
        "generated_at": str(generated_at or _now_iso()).strip(),
    }


def _stage_for_command(command: str, result, context: dict | None = None) -> str:
    """Infer the business stage associated with a command."""
    del result
    context = context or {}
    normalized = str(command or "").strip()
    if normalized in {"pause-run", "cancel-run", "resume-run", "runtime-status", "prepare-resume", "replan-remaining", "process-chunks"}:
        return "processing"
    if normalized in {"validate-state", "plan-optimization", "chunk-text", "chunk-segments", "chunk-document", "get-chapters", "build-chapter-plan"}:
        return "planning"
    if normalized in {"normalize-document", "sync-state"}:
        return "normalize"
    if normalized in {"verify-quality"}:
        return "verify"
    if normalized in {"assemble-final", "merge-content"}:
        return "assemble"
    if normalized in {"load-config", "preflight"}:
        return "preflight"
    if normalized in {"download-subtitles", "download-audio", "download-metadata"}:
        return "source"
    return str(context.get("active_stage", "")).strip() or normalized.replace("-", "_")


def _lifecycle_state_for_command(command: str, result, context: dict | None = None) -> str:
    """Infer a lifecycle state from a command result."""
    context = context or {}
    if isinstance(result, dict):
        effective = str(result.get("effective_runtime_status", "")).strip()
        runtime = result.get("runtime", {}) if isinstance(result.get("runtime", {}), dict) else {}
        runtime_status = str(runtime.get("status", "")).strip()
        if effective in {"paused", "pause_requested"}:
            return "paused"
        if effective in {"cancellation_requested"}:
            return "failed_terminal"
        if runtime_status in {"resumable", "resume_pending"}:
            return "processing"
        if runtime_status in {"running", "pending"}:
            return "processing"
        if result.get("paused", False):
            return "paused"
        if result.get("aborted", False):
            return "failed_terminal"
        if result.get("dry_run", False):
            return "planned"
        if result.get("success", False):
            if str(command or "").strip() == "verify-quality":
                return "verifying"
            if str(command or "").strip() in {"assemble-final", "merge-content"}:
                return "completed"
            return _stage_for_command(command, result, context)
    return _stage_for_command(command, result, context)


def derive_task_spec(command: str, result=None, *, context: dict | None = None,
                     trace_id: str = "") -> dict:
    """Derive a task spec from a command invocation and result payload."""
    context = context or {}
    result = result if isinstance(result, dict) else {}
    source_ref = (
        str(context.get("video_url", "") or context.get("url", "") or result.get("work_dir", "")
            or context.get("work_dir", "") or context.get("state_path", "")
            or context.get("output_dir", "") or trace_id).strip()
    )
    allowed_fallbacks = context.get("allowed_fallbacks", [])
    bilingual = _parse_bool(context.get("bilingual", result.get("bilingual", False)), False)
    return build_task_spec(
        task_id=str(context.get("task_id", "") or result.get("task_id", "") or _stable_id("task", source_ref)).strip(),
        source_ref=source_ref,
        output_mode=str(context.get("output_mode", result.get("output_mode", "markdown"))).strip() or "markdown",
        bilingual=bilingual,
        quality_profile=str(context.get("quality_profile", "balanced")).strip() or "balanced",
        speed_priority=str(context.get("speed_priority", "balanced")).strip() or "balanced",
        cost_budget=_parse_float(context.get("cost_budget", 0.0), 0.0),
        latency_budget=_parse_float(context.get("latency_budget", 0.0), 0.0),
        allowed_fallbacks=allowed_fallbacks,
        human_escalation_policy=str(context.get("human_escalation_policy", "on_blocking_failure")).strip(),
        metadata={
            "command": str(command or "").strip(),
            "trace_id": str(trace_id or "").strip(),
        },
    )


def derive_run_state(command: str, result=None, *, context: dict | None = None,
                     trace_id: str = "", task_spec: dict | None = None) -> dict:
    """Derive a run-state contract from a command invocation and result payload."""
    context = context or {}
    result = result if isinstance(result, dict) else {}
    task_payload = task_spec if isinstance(task_spec, dict) else derive_task_spec(command, result, context=context, trace_id=trace_id)
    runtime = result.get("runtime", {}) if isinstance(result.get("runtime", {}), dict) else {}
    plan = result.get("plan", {}) if isinstance(result.get("plan", {}), dict) else {}
    work_dir = str(result.get("work_dir", "") or context.get("work_dir", "")).strip()
    run_id = (
        str(runtime.get("run_id", "") or result.get("run_id", "") or context.get("run_id", "")
            or plan.get("plan_id", "") or trace_id).strip()
    )
    lifecycle_state = _lifecycle_state_for_command(command, result, context)
    active_stage = _stage_for_command(command, result, context)
    effective_runtime_status = (
        str(result.get("effective_runtime_status", "") or runtime.get("status", "") or lifecycle_state).strip()
    )
    from . import ledger as kernel_ledger

    budget_ledger = kernel_ledger.derive_budget_ledger(result, context=context)

    return build_run_state(
        run_id=run_id,
        task_id=task_payload.get("task_id", ""),
        lifecycle_state=lifecycle_state,
        active_stage=active_stage,
        effective_runtime_status=effective_runtime_status,
        work_dir=work_dir,
        policy_profile=str(context.get("policy_profile", "default")).strip() or "default",
        ownership=result.get("ownership", {}) if isinstance(result.get("ownership", {}), dict) else {},
        budget_ledger=budget_ledger,
        metadata={
            "command": str(command or "").strip(),
            "trace_id": str(trace_id or "").strip(),
            "manifest_path": str(result.get("manifest_path", "")).strip(),
        },
        started_at=str(runtime.get("started_at", "") or result.get("started_at", "") or _now_iso()).strip(),
        updated_at=str(runtime.get("updated_at", "") or result.get("updated_at", "") or _now_iso()).strip(),
    )


def derive_artifacts(command: str, result=None, *, context: dict | None = None,
                     action_id: str = "") -> list[dict]:
    """Infer artifact references from common result/context path fields."""
    del command
    context = context or {}
    result = result if isinstance(result, dict) else {}
    artifacts = []
    seen_paths = set()
    candidates = [
        ("manifest", result.get("manifest_path", "")),
        ("work_dir", result.get("work_dir", "") or context.get("work_dir", "")),
        ("normalized_document", result.get("normalized_document_path", "") or context.get("normalized_document_path", "")),
        ("output_file", result.get("output_file", "") or context.get("output_file", "")),
        ("output_path", result.get("output_path", "") or context.get("output_path", "")),
        ("optimized_text", result.get("optimized_text", "") or context.get("optimized_text_path", "")),
        ("state", context.get("state_path", "") or context.get("state_ref", "")),
        ("telemetry", context.get("telemetry_path", "")),
    ]
    outputs = result.get("outputs", {}) if isinstance(result.get("outputs", {}), dict) else {}
    candidates.extend([
        ("work_dir", outputs.get("work_dir", "")),
        ("output_file", outputs.get("output_file", "")),
        ("optimized_text", outputs.get("optimized_text", "")),
    ])
    for artifact_type, value in candidates:
        path_text = str(value or "").strip()
        if not path_text or path_text in seen_paths:
            continue
        seen_paths.add(path_text)
        artifacts.append(build_artifact_ref(
            artifact_type=artifact_type,
            path=path_text,
            source_action_id=action_id,
        ))
    return artifacts


def derive_quality_report(command: str, result=None, *, context: dict | None = None) -> dict | None:
    """Infer a quality report from verify-quality and related command outputs."""
    del context
    result = result if isinstance(result, dict) else {}
    normalized = str(command or "").strip()
    if normalized == "verify-quality" or {"passed", "hard_failures", "checks"}.issubset(result.keys()):
        warnings = result.get("warnings", []) if isinstance(result.get("warnings", []), list) else []
        hard_failures = result.get("hard_failures", []) if isinstance(result.get("hard_failures", []), list) else []
        checks = result.get("checks", {}) if isinstance(result.get("checks", {}), dict) else {}
        coverage_score = 1.0 if result.get("passed", False) else 0.0
        missing_sections = result.get("missing_semantic_anchors", []) if isinstance(result.get("missing_semantic_anchors", []), list) else []
        recommended_action = "accept_output" if result.get("passed", False) else "repair_or_replan"
        structure_integrity = "passed" if checks.get("has_structure", result.get("passed", False)) else "failed"
        translation_risk = "low"
        if checks.get("bilingual_balanced") is False:
            translation_risk = "high"
        return build_quality_report(
            coverage_score=coverage_score,
            missing_sections=missing_sections,
            term_consistency_score=1.0,
            translation_risk=translation_risk,
            structure_integrity=structure_integrity,
            recommended_action=recommended_action,
            passed=result.get("passed", False),
            warnings=warnings,
            hard_failures=hard_failures,
            checks=checks,
        )
    return None


def derive_action_result(command: str, result=None, *, context: dict | None = None,
                         trace_id: str = "") -> dict:
    """Derive a standardized action result from a command result."""
    context = context or {}
    result = result if isinstance(result, dict) else {}
    quality_report = derive_quality_report(command, result, context=context)
    warnings = result.get("warnings", []) if isinstance(result.get("warnings", []), list) else []
    failure_type = ""
    if result.get("aborted", False):
        failure_type = "aborted"
    elif result.get("paused", False):
        failure_type = "paused"
    elif result.get("hard_failures"):
        failure_type = "quality_gate"
    elif not result.get("success", result.get("passed", True)):
        failure_type = str(result.get("error", "") or "operation_failed").strip()
    action_type = str(command or "").strip().replace("-", "_")
    action_id = str(context.get("action_id", "") or trace_id or _stable_id("action", action_type)).strip()
    artifacts = derive_artifacts(command, result, context=context, action_id=action_id)
    if quality_report:
        artifacts.append(build_artifact_ref(
            artifact_type="quality_report",
            path=f"inline:{quality_report['format']}",
            source_action_id=action_id,
        ))
    return build_action_result(
        action_id=action_id,
        action_type=action_type,
        tool_name=str(command or "").strip(),
        success=bool(result.get("success", result.get("passed", True))),
        warnings=warnings,
        artifacts_created=[artifact["artifact_id"] for artifact in artifacts],
        cost={
            "warning_count": len(warnings),
            "duration_hint_ms": _parse_int(result.get("latency_ms"), 0),
        },
        failure_type=failure_type,
        message=str(result.get("message", "") or result.get("error", "")).strip(),
    )


def build_command_contract_bundle(command: str, result=None, *, context: dict | None = None,
                                  trace_id: str = "") -> dict:
    """Build the runtime-contract bundle attached to command envelopes."""
    task_spec = derive_task_spec(command, result, context=context, trace_id=trace_id)
    action_result = derive_action_result(command, result, context=context, trace_id=trace_id)
    run_state = derive_run_state(command, result, context=context, trace_id=trace_id, task_spec=task_spec)
    artifacts = derive_artifacts(command, result, context=context, action_id=action_result["action_id"])
    quality_report = derive_quality_report(command, result, context=context)
    observation = build_observation(
        observation_type="command_result",
        source_command=str(command or "").strip(),
        state_ref=run_state.get("run_id", ""),
        data={
            "success": action_result.get("success", False),
            "active_stage": run_state.get("active_stage", ""),
            "lifecycle_state": run_state.get("lifecycle_state", ""),
            "artifact_count": len(artifacts),
        },
    )
    from . import artifacts as kernel_artifacts
    from . import decision as kernel_decision
    from . import policy as kernel_policy
    from . import recovery as kernel_recovery

    policy_evaluation = kernel_policy.evaluate_policy(
        run_state=run_state,
        result=result,
        context=context,
        quality_report=quality_report,
    )
    decision_record = kernel_decision.build_decision_record_for_command(
        command,
        run_state=run_state,
        result=result,
        policy_evaluation=policy_evaluation,
        quality_report=quality_report,
    )
    processing_state = None
    recovery_summary = None
    if str(run_state.get("active_stage", "")).strip() == "processing":
        processing_state = kernel_recovery.build_processing_state(str(run_state.get("work_dir", "")).strip(), result=result)
        recovery_summary = kernel_recovery.build_recovery_summary(str(run_state.get("work_dir", "")).strip(), result=result)
    artifact_graph = kernel_artifacts.build_artifact_graph(run_id=str(run_state.get("run_id", "")).strip(), artifacts=artifacts)
    bundle = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "format": CONTRACT_BUNDLE_FORMAT,
        "task_spec": task_spec,
        "run_state": run_state,
        "observation": observation,
        "policy": policy_evaluation,
        "decision_record": decision_record,
        "action_result": action_result,
        "artifacts": artifacts,
        "artifact_graph": artifact_graph,
    }
    if processing_state is not None:
        bundle["processing_state"] = processing_state
    if recovery_summary is not None:
        bundle["recovery_summary"] = recovery_summary
    if quality_report is not None:
        bundle["quality_report"] = quality_report
    return bundle


def summarize_contract_bundle(bundle: dict | None = None) -> dict:
    """Build a concise contract summary suitable for telemetry events."""
    payload = dict(bundle or {})
    task_spec = payload.get("task_spec", {}) if isinstance(payload.get("task_spec", {}), dict) else {}
    run_state = payload.get("run_state", {}) if isinstance(payload.get("run_state", {}), dict) else {}
    action_result = payload.get("action_result", {}) if isinstance(payload.get("action_result", {}), dict) else {}
    policy_evaluation = payload.get("policy", {}) if isinstance(payload.get("policy", {}), dict) else {}
    decision_record = payload.get("decision_record", {}) if isinstance(payload.get("decision_record", {}), dict) else {}
    processing_state = payload.get("processing_state", {}) if isinstance(payload.get("processing_state", {}), dict) else {}
    recovery_summary = payload.get("recovery_summary", {}) if isinstance(payload.get("recovery_summary", {}), dict) else {}
    quality_report = payload.get("quality_report", {}) if isinstance(payload.get("quality_report", {}), dict) else {}
    artifacts = payload.get("artifacts", []) if isinstance(payload.get("artifacts", []), list) else []
    return {
        "task_id": str(task_spec.get("task_id", "")).strip(),
        "run_id": str(run_state.get("run_id", "")).strip(),
        "lifecycle_state": str(run_state.get("lifecycle_state", "")).strip(),
        "active_stage": str(run_state.get("active_stage", "")).strip(),
        "action_type": str(action_result.get("action_type", "")).strip(),
        "selected_action": str(decision_record.get("selected_action", "")).strip(),
        "budget_pressure_level": str(policy_evaluation.get("budget_pressure_level", "")).strip(),
        "processing_substate": str(processing_state.get("substate", "")).strip(),
        "recovery_action": str(recovery_summary.get("recommended_action", "")).strip(),
        "artifact_count": len(artifacts),
        "quality_recommended_action": str(quality_report.get("recommended_action", "")).strip(),
    }
