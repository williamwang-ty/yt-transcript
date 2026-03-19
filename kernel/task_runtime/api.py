"""Stable runtime-facing API helpers for outer-agent orchestration."""

from __future__ import annotations

import os
import time
from pathlib import Path

from ..long_text import execution as kernel_execution
from ..long_text import merge as kernel_merge
from . import artifacts as kernel_artifacts
from . import contracts as kernel_contracts
from . import lifecycle as kernel_lifecycle
from . import policy as kernel_policy
from . import recovery as kernel_recovery
from . import runtime as kernel_runtime
from . import state as kernel_state


RUNTIME_API_SCHEMA_VERSION = 1
RUNTIME_TASK_FORMAT = "yt_transcript.runtime_task/v1"
RUNTIME_API_SUMMARY_FORMAT = "yt_transcript.runtime_api_summary/v1"
RUNTIME_TASK_FILENAME = ".runtime_task.json"
RUNTIME_API_MODE_ENV = "YT_TRANSCRIPT_RUNTIME_API_MODE"
DEFAULT_RUNTIME_API_MODE = "runtime_api"
LEGACY_RUNTIME_API_MODE = "legacy_cli"
SUPPORTED_RUNTIME_API_MODES = (
    DEFAULT_RUNTIME_API_MODE,
    LEGACY_RUNTIME_API_MODE,
)
DEFAULT_RUNTIME_ADVANCE_ACTION = "auto"
DEFAULT_RUNTIME_PREFERRED_COMMANDS = [
    "create-run",
    "inspect-run",
    "advance-run",
    "apply-control",
    "resume-run",
    "finalize-run",
]
COMPATIBILITY_COMMANDS = [
    "runtime-status",
    "process-chunks",
    "prepare-resume",
    "replan-remaining",
    "pause-run",
    "cancel-run",
]


def _now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _string_list(values) -> list[str]:
    """Normalize list-like values into a compact string list."""
    if isinstance(values, (list, tuple, set)):
        return [str(item).strip() for item in values if str(item).strip()]
    if str(values or "").strip():
        return [str(values).strip()]
    return []


def runtime_task_path(work_dir: str) -> Path:
    """Return the persisted runtime-task metadata path for a work directory."""
    return Path(str(work_dir or "")).expanduser().resolve() / RUNTIME_TASK_FILENAME


def load_runtime_task(work_dir: str) -> tuple[Path, dict | None, str]:
    """Load the persisted runtime-task metadata for a work directory."""
    task_path = runtime_task_path(work_dir)
    payload, error = kernel_state.read_json_file(task_path)
    return task_path, payload, error


def resolve_runtime_api_mode(value: str = "") -> str:
    """Resolve the preferred external runtime API mode."""
    requested = str(value or os.environ.get(RUNTIME_API_MODE_ENV, "")).strip().lower()
    if requested in SUPPORTED_RUNTIME_API_MODES:
        return requested
    return DEFAULT_RUNTIME_API_MODE


def _runtime_api_summary(*, run_id: str = "", work_dir: str = "",
                         migration_mode: str = "", preferred_path: str = "runtime_api") -> dict:
    """Build a concise runtime-API summary payload."""
    return {
        "schema_version": RUNTIME_API_SCHEMA_VERSION,
        "format": RUNTIME_API_SUMMARY_FORMAT,
        "run_id": str(run_id or "").strip(),
        "work_dir": str(work_dir or "").strip(),
        "preferred_path": str(preferred_path or "runtime_api").strip() or "runtime_api",
        "migration_mode": resolve_runtime_api_mode(migration_mode),
        "preferred_commands": list(DEFAULT_RUNTIME_PREFERRED_COMMANDS),
        "compatibility_commands": list(COMPATIBILITY_COMMANDS),
    }


def _build_task_spec_payload(task_spec: dict | None = None, *, work_dir: str = "",
                             task_id: str = "", source_ref: str = "",
                             output_mode: str = "markdown", bilingual: bool = False,
                             quality_profile: str = "balanced",
                             speed_priority: str = "balanced",
                             cost_budget: float = 0.0,
                             latency_budget: float = 0.0,
                             allowed_fallbacks=None,
                             human_escalation_policy: str = "on_blocking_failure",
                             migration_mode: str = "") -> dict:
    """Normalize task-spec inputs into a stable task-spec contract."""
    payload = dict(task_spec or {})
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), dict) else {}
    resolved_work_dir = str(work_dir or metadata.get("work_dir", "")).strip()
    resolved_source_ref = str(source_ref or payload.get("source_ref", "") or resolved_work_dir).strip()
    resolved_metadata = dict(metadata)
    if resolved_work_dir:
        resolved_metadata["work_dir"] = resolved_work_dir
    resolved_metadata["preferred_commands"] = list(DEFAULT_RUNTIME_PREFERRED_COMMANDS)
    resolved_metadata["compatibility_commands"] = list(COMPATIBILITY_COMMANDS)
    resolved_metadata["migration_mode"] = resolve_runtime_api_mode(migration_mode or resolved_metadata.get("migration_mode", ""))
    return kernel_contracts.build_task_spec(
        task_id=str(task_id or payload.get("task_id", "")).strip(),
        source_ref=resolved_source_ref,
        output_mode=str(payload.get("output_mode", output_mode)).strip() or output_mode,
        bilingual=bool(payload.get("bilingual", bilingual)),
        quality_profile=str(payload.get("quality_profile", quality_profile)).strip() or quality_profile,
        speed_priority=str(payload.get("speed_priority", speed_priority)).strip() or speed_priority,
        cost_budget=payload.get("cost_budget", cost_budget),
        latency_budget=payload.get("latency_budget", latency_budget),
        allowed_fallbacks=payload.get("allowed_fallbacks", allowed_fallbacks),
        human_escalation_policy=(
            str(payload.get("human_escalation_policy", human_escalation_policy)).strip()
            or human_escalation_policy
        ),
        metadata=resolved_metadata,
        created_at=str(payload.get("created_at", "")).strip(),
    )


def _runtime_status_for(work_dir: str) -> dict:
    """Return the current runtime-status summary for a work directory."""
    resolved = str(work_dir or "").strip()
    if not resolved:
        return {
            "success": False,
            "work_dir": "",
            "manifest_present": False,
            "manifest_error": "missing_work_dir",
            "runtime": {},
            "effective_runtime_status": "created",
            "plan": {},
            "ownership": {},
            "cancellation": {},
            "pause": {},
            "total_chunks": 0,
            "status_counts": {},
            "completed_chunks": 0,
            "failed_chunks": 0,
            "pending_chunks": 0,
            "interrupted_chunks": 0,
            "superseded_chunks": 0,
        }
    return kernel_state.summarize_runtime_status(resolved)


def _build_run_state(*, work_dir: str, runtime_status: dict | None = None,
                     task_spec: dict | None = None, run_id: str = "",
                     policy_profile: str = "default") -> dict:
    """Build the effective run-state contract from persisted runtime state."""
    runtime_status = runtime_status if isinstance(runtime_status, dict) else _runtime_status_for(work_dir)
    snapshot = kernel_lifecycle.observe_runtime_snapshot(work_dir)
    observed = snapshot.get("run_state", {}) if isinstance(snapshot.get("run_state", {}), dict) else {}
    task_payload = task_spec if isinstance(task_spec, dict) else _build_task_spec_payload(work_dir=work_dir)
    metadata = observed.get("metadata", {}) if isinstance(observed.get("metadata", {}), dict) else {}
    metadata = dict(metadata)
    manifest_present = bool(runtime_status.get("manifest_present", False))
    if not manifest_present:
        observed = {}
        metadata = {
            **metadata,
            "command": "create-run",
            "trace_id": "",
            "manifest_path": str(kernel_state.manifest_path_for(work_dir)),
        }
    metadata.setdefault("command", "inspect-run")
    return kernel_contracts.build_run_state(
        run_id=str(run_id or observed.get("run_id", "") or runtime_status.get("plan", {}).get("plan_id", "")).strip(),
        task_id=str(task_payload.get("task_id", "")).strip(),
        lifecycle_state=str(observed.get("lifecycle_state", "") or ("created" if not manifest_present else "created")).strip() or "created",
        active_stage=str(observed.get("active_stage", "") or ("planning" if not manifest_present else "")).strip(),
        effective_runtime_status=(
            str(
                observed.get("effective_runtime_status", "")
                or ("created" if not manifest_present else runtime_status.get("effective_runtime_status", ""))
                or "created"
            ).strip()
            or "created"
        ),
        work_dir=str(work_dir or "").strip(),
        policy_profile=str(policy_profile or observed.get("policy_profile", "default")).strip() or "default",
        ownership=runtime_status.get("ownership", {}) if isinstance(runtime_status.get("ownership", {}), dict) else {},
        budget_ledger=observed.get("budget_ledger", {}) if isinstance(observed.get("budget_ledger", {}), dict) else {},
        metadata=metadata,
        started_at=str(observed.get("started_at", "") or _now_iso()).strip(),
        updated_at=str(observed.get("updated_at", "") or _now_iso()).strip(),
    )


def _collect_runtime_artifacts(*, work_dir: str, run_id: str = "", output_file: str = "") -> list[dict]:
    """Collect stable artifact references for runtime-facing inspection calls."""
    action_id = str(run_id or f"runtime_api:{work_dir}").strip()
    payload = {
        "manifest_path": str(kernel_state.manifest_path_for(work_dir)),
        "work_dir": str(work_dir or "").strip(),
        "output_file": str(output_file or "").strip(),
    }
    artifacts = kernel_contracts.derive_artifacts(
        "inspect-run",
        payload,
        context={"work_dir": str(work_dir or "").strip()},
        action_id=action_id,
    )
    task_path = runtime_task_path(work_dir)
    if task_path.exists():
        artifacts.append(kernel_contracts.build_artifact_ref(
            artifact_type="runtime_task",
            path=str(task_path),
            source_action_id=action_id,
        ))
    telemetry_path = Path(str(work_dir or "")).expanduser().resolve() / "telemetry.jsonl"
    if telemetry_path.exists():
        artifacts.append(kernel_contracts.build_artifact_ref(
            artifact_type="telemetry",
            path=str(telemetry_path),
            source_action_id=action_id,
        ))
    unique = []
    seen_paths = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path_text = str(artifact.get("path", "")).strip()
        if path_text and path_text in seen_paths:
            continue
        if path_text:
            seen_paths.add(path_text)
        unique.append(artifact)
    return unique


def _allowed_actions_for(*, run_state: dict, runtime_status: dict, recovery_summary: dict) -> list[str]:
    """Derive allowed actions for the runtime-facing inspection surface."""
    policy_evaluation = kernel_policy.evaluate_policy(
        run_state=run_state,
        result=runtime_status,
        context={"work_dir": str(run_state.get("work_dir", "")).strip()},
    )
    allowed = policy_evaluation.get("allowed_actions", []) if isinstance(policy_evaluation.get("allowed_actions", []), list) else []
    extra = []
    recommended = str(recovery_summary.get("recommended_action", "")).strip()
    if recommended:
        extra.append(recommended)
    for action in extra:
        if action and action not in allowed:
            allowed.append(action)
    return allowed


def _record_runtime_task(*, work_dir: str, task_spec: dict, run_state: dict,
                         migration_mode: str = "") -> tuple[Path, dict]:
    """Persist the stable runtime-task metadata for a work directory."""
    task_path = runtime_task_path(work_dir)
    existing, _ = kernel_state.read_json_file(task_path)
    created_at = str((existing or {}).get("created_at", "") or task_spec.get("created_at", "") or _now_iso()).strip()
    payload = {
        "schema_version": RUNTIME_API_SCHEMA_VERSION,
        "format": RUNTIME_TASK_FORMAT,
        "work_dir": str(work_dir or "").strip(),
        "task_spec": dict(task_spec or {}),
        "run_state": dict(run_state or {}),
        "preferred_path": DEFAULT_RUNTIME_API_MODE,
        "migration_mode": resolve_runtime_api_mode(migration_mode or (existing or {}).get("migration_mode", "")),
        "preferred_commands": list(DEFAULT_RUNTIME_PREFERRED_COMMANDS),
        "compatibility_commands": list(COMPATIBILITY_COMMANDS),
        "created_at": created_at,
        "updated_at": _now_iso(),
    }
    task_path.parent.mkdir(parents=True, exist_ok=True)
    kernel_state.write_json_file(task_path, payload)
    return task_path, payload


def _load_or_build_runtime_contracts(*, work_dir: str, run_id: str = "",
                                     policy_profile: str = "default") -> tuple[Path, dict | None, str, dict, dict, dict, list[dict], dict]:
    """Load persisted task metadata and derive the effective runtime-facing contracts."""
    task_path, task_record, task_error = load_runtime_task(work_dir)
    runtime_status = _runtime_status_for(work_dir)
    persisted_task = task_record.get("task_spec", {}) if isinstance(task_record, dict) and isinstance(task_record.get("task_spec", {}), dict) else {}
    task_spec = _build_task_spec_payload(persisted_task, work_dir=work_dir, migration_mode=(task_record or {}).get("migration_mode", ""))
    persisted_run_state = task_record.get("run_state", {}) if isinstance(task_record, dict) and isinstance(task_record.get("run_state", {}), dict) else {}
    resolved_run_id = str(run_id or persisted_run_state.get("run_id", "")).strip()
    run_state = _build_run_state(
        work_dir=work_dir,
        runtime_status=runtime_status,
        task_spec=task_spec,
        run_id=resolved_run_id,
        policy_profile=policy_profile or str(persisted_run_state.get("policy_profile", "default")).strip() or "default",
    )
    if task_record and resolved_run_id and resolved_run_id != str(run_state.get("run_id", "")).strip():
        task_error = f"run_id_mismatch: expected {run_state.get('run_id', '')}, got {resolved_run_id}"
    processing_state = kernel_recovery.build_processing_state(work_dir, result=runtime_status)
    recovery_summary = kernel_recovery.build_recovery_summary(work_dir, result=runtime_status)
    artifacts = _collect_runtime_artifacts(work_dir=work_dir, run_id=str(run_state.get("run_id", "")).strip())
    artifact_graph = kernel_artifacts.build_artifact_graph(
        run_id=str(run_state.get("run_id", "")).strip(),
        artifacts=artifacts,
    )
    return task_path, task_record, task_error, task_spec, run_state, runtime_status, artifacts, {
        "processing_state": processing_state,
        "recovery_summary": recovery_summary,
        "artifact_graph": artifact_graph,
    }


def create_run(task_spec: dict | None = None, *, work_dir: str = "", task_id: str = "",
               source_ref: str = "", output_mode: str = "markdown", bilingual: bool = False,
               quality_profile: str = "balanced", speed_priority: str = "balanced",
               cost_budget: float = 0.0, latency_budget: float = 0.0,
               allowed_fallbacks=None, human_escalation_policy: str = "on_blocking_failure",
               policy_profile: str = "default", migration_mode: str = "") -> dict:
    """Create or refresh a persisted runtime-task record for outer-agent orchestration."""
    normalized_work_dir = str(work_dir or (task_spec or {}).get("metadata", {}).get("work_dir", "")).strip()
    if not normalized_work_dir:
        return {
            "success": False,
            "created": False,
            "error": "missing_work_dir",
            "message": "work_dir is required for create-run",
            "task_spec": {},
            "run_state": {},
            "runtime": {},
            "effective_runtime_status": "",
        }

    work_path = Path(normalized_work_dir).expanduser().resolve()
    workspace_created = not work_path.exists()
    work_path.mkdir(parents=True, exist_ok=True)

    task_payload = _build_task_spec_payload(
        task_spec,
        work_dir=str(work_path),
        task_id=task_id,
        source_ref=source_ref,
        output_mode=output_mode,
        bilingual=bilingual,
        quality_profile=quality_profile,
        speed_priority=speed_priority,
        cost_budget=cost_budget,
        latency_budget=latency_budget,
        allowed_fallbacks=allowed_fallbacks,
        human_escalation_policy=human_escalation_policy,
        migration_mode=migration_mode,
    )
    runtime_status = _runtime_status_for(str(work_path))
    run_state = _build_run_state(
        work_dir=str(work_path),
        runtime_status=runtime_status,
        task_spec=task_payload,
        policy_profile=policy_profile,
    )
    task_path, task_record = _record_runtime_task(
        work_dir=str(work_path),
        task_spec=task_payload,
        run_state=run_state,
        migration_mode=migration_mode,
    )
    recovery_summary = kernel_recovery.build_recovery_summary(str(work_path), result=runtime_status)
    return {
        "success": True,
        "created": True,
        "workspace_created": workspace_created,
        "work_dir": str(work_path),
        "run_id": str(run_state.get("run_id", "")).strip(),
        "task_id": str(task_payload.get("task_id", "")).strip(),
        "task_record_path": str(task_path),
        "task_record_present": True,
        "task_spec": task_payload,
        "run_state": run_state,
        "runtime": runtime_status.get("runtime", {}) if isinstance(runtime_status.get("runtime", {}), dict) else {},
        "effective_runtime_status": str(run_state.get("effective_runtime_status", "")).strip(),
        "ownership": runtime_status.get("ownership", {}) if isinstance(runtime_status.get("ownership", {}), dict) else {},
        "manifest_present": bool(runtime_status.get("manifest_present", False)),
        "migration_mode": task_record.get("migration_mode", resolve_runtime_api_mode(migration_mode)),
        "preferred_entrypoint": "advance-run",
        "runtime_api": _runtime_api_summary(
            run_id=str(run_state.get("run_id", "")).strip(),
            work_dir=str(work_path),
            migration_mode=task_record.get("migration_mode", migration_mode),
        ),
        "recovery_summary": recovery_summary,
        "message": "runtime task created" if workspace_created else "runtime task refreshed",
    }


def inspect_run(work_dir: str, *, run_id: str = "", policy_profile: str = "default") -> dict:
    """Inspect the current runtime/task state through the stable external API."""
    normalized_work_dir = str(work_dir or "").strip()
    if not normalized_work_dir:
        return {
            "success": False,
            "error": "missing_work_dir",
            "message": "work_dir is required for inspect-run",
            "task_spec": {},
            "run_state": {},
            "runtime": {},
            "effective_runtime_status": "",
        }

    work_path = Path(normalized_work_dir).expanduser().resolve()
    task_path, task_record, task_error, task_spec, run_state, runtime_status, artifacts, derived = _load_or_build_runtime_contracts(
        work_dir=str(work_path),
        run_id=run_id,
        policy_profile=policy_profile,
    )
    if task_error.startswith("run_id_mismatch"):
        return {
            "success": False,
            "error": "run_id_mismatch",
            "message": task_error,
            "work_dir": str(work_path),
            "task_record_path": str(task_path),
            "task_record_present": task_record is not None,
            "task_spec": task_spec,
            "run_state": run_state,
            "runtime": runtime_status.get("runtime", {}) if isinstance(runtime_status.get("runtime", {}), dict) else {},
            "effective_runtime_status": str(run_state.get("effective_runtime_status", "")).strip(),
        }
    recovery_summary = derived["recovery_summary"]
    allowed_actions = _allowed_actions_for(run_state=run_state, runtime_status=runtime_status, recovery_summary=recovery_summary)
    return {
        "success": bool(work_path.exists()),
        "work_dir": str(work_path),
        "run_id": str(run_state.get("run_id", "")).strip(),
        "task_id": str(task_spec.get("task_id", "")).strip(),
        "task_record_path": str(task_path),
        "task_record_present": task_record is not None,
        "task_record_error": task_error,
        "task_spec": task_spec,
        "run_state": run_state,
        "runtime": runtime_status.get("runtime", {}) if isinstance(runtime_status.get("runtime", {}), dict) else {},
        "effective_runtime_status": str(run_state.get("effective_runtime_status", "")).strip(),
        "ownership": runtime_status.get("ownership", {}) if isinstance(runtime_status.get("ownership", {}), dict) else {},
        "manifest_present": bool(runtime_status.get("manifest_present", False)),
        "plan": runtime_status.get("plan", {}) if isinstance(runtime_status.get("plan", {}), dict) else {},
        "status_counts": runtime_status.get("status_counts", {}) if isinstance(runtime_status.get("status_counts", {}), dict) else {},
        "processing_state": derived["processing_state"],
        "recovery_summary": recovery_summary,
        "available_actions": allowed_actions,
        "artifacts": artifacts,
        "artifact_graph": derived["artifact_graph"],
        "migration_mode": resolve_runtime_api_mode((task_record or {}).get("migration_mode", "")),
        "preferred_entrypoint": "advance-run",
        "runtime_api": _runtime_api_summary(
            run_id=str(run_state.get("run_id", "")).strip(),
            work_dir=str(work_path),
            migration_mode=(task_record or {}).get("migration_mode", ""),
        ),
    }


def _manifest_exists(work_dir: str) -> bool:
    """Return whether the canonical manifest exists for a work directory."""
    return kernel_state.manifest_path_for(work_dir).exists()


def _default_prompt_name(inspection: dict, prompt_name: str = "") -> str:
    """Resolve the prompt name for runtime advance calls."""
    explicit_prompt = str(prompt_name or "").strip()
    if explicit_prompt:
        return explicit_prompt
    plan = inspection.get("plan", {}) if isinstance(inspection.get("plan", {}), dict) else {}
    return str(plan.get("prompt_name", "")).strip() or "structure_only"


def _resolve_advance_command(action: str, inspection: dict, *, auto_replan: bool,
                             dry_run: bool) -> tuple[str, str]:
    """Resolve the concrete legacy command that should implement advance-run."""
    normalized = str(action or DEFAULT_RUNTIME_ADVANCE_ACTION).strip().lower().replace("_", "-")
    aliases = {
        "process": "process-chunks",
        "process-chunks": "process-chunks",
        "process-with-replans": "process-chunks-with-replans",
        "process-chunks-with-replans": "process-chunks-with-replans",
        "prepare-resume": "prepare-resume",
        "replan-remaining": "replan-remaining",
    }
    if normalized in aliases:
        selected = aliases[normalized]
        if selected == "process-chunks" and auto_replan and not dry_run:
            return "process-chunks-with-replans", "explicit process command upgraded to bounded replan loop"
        return selected, "explicit runtime action requested"

    recovery_summary = inspection.get("recovery_summary", {}) if isinstance(inspection.get("recovery_summary", {}), dict) else {}
    runtime_status = str(inspection.get("effective_runtime_status", "")).strip()
    recommended_action = str(recovery_summary.get("recommended_action", "")).strip()
    if runtime_status in {"paused", "pause_requested"}:
        return "", "run is paused; resume-run is required before advance-run"
    if recommended_action == "prepare_resume":
        return "prepare-resume", "recovery summary indicates resume-safe preparation is required"
    if recommended_action == "replan_remaining":
        return "replan-remaining", "recovery summary indicates replanning is required"
    if auto_replan and not dry_run:
        return "process-chunks-with-replans", "default bounded processing path"
    return "process-chunks", "default processing path"


def advance_run(work_dir: str, prompt_name: str = "", *, run_id: str = "",
                action: str = DEFAULT_RUNTIME_ADVANCE_ACTION, extra_instruction: str = "",
                config_path: str = None, dry_run: bool = False,
                input_key: str = "raw_path", force: bool = False,
                auto_replan: bool = True, max_replans: int = 3,
                chunk_size: int = 0, policy_profile: str = "default") -> dict:
    """Advance the runtime by selecting and dispatching the next bounded action."""
    inspection = inspect_run(work_dir, run_id=run_id, policy_profile=policy_profile)
    if not inspection.get("success", False):
        return {
            **inspection,
            "success": False,
            "advanced": False,
            "selected_runtime_action": "",
            "delegate_command": "",
        }
    if not _manifest_exists(work_dir):
        return {
            **inspection,
            "success": False,
            "advanced": False,
            "selected_runtime_action": "",
            "delegate_command": "",
            "error": "missing_manifest",
            "message": f"manifest.json not found in {work_dir}",
        }

    selected_command, rationale = _resolve_advance_command(
        action,
        inspection,
        auto_replan=auto_replan,
        dry_run=dry_run,
    )
    if not selected_command:
        return {
            **inspection,
            "success": False,
            "advanced": False,
            "selected_runtime_action": "resume-run",
            "delegate_command": "",
            "error": "advance_blocked",
            "message": rationale,
        }

    resolved_prompt = _default_prompt_name(inspection, prompt_name)
    if selected_command == "prepare-resume":
        result = kernel_execution.prepare_resume(
            work_dir,
            prompt_name=resolved_prompt,
            config_path=config_path,
            input_key=input_key,
        )
    elif selected_command == "replan-remaining":
        result = kernel_execution.replan_remaining(
            work_dir,
            prompt_name=resolved_prompt,
            config_path=config_path,
            chunk_size=chunk_size,
            input_key=input_key,
        )
    elif selected_command == "process-chunks-with-replans":
        result = kernel_execution.process_chunks_with_replans(
            work_dir,
            resolved_prompt,
            extra_instruction=extra_instruction,
            config_path=config_path,
            input_key=input_key,
            force=force,
            max_replans=max_replans,
        )
    else:
        result = kernel_execution.process_chunks(
            work_dir,
            resolved_prompt,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=dry_run,
            input_key=input_key,
            force=force,
        )

    refreshed = inspect_run(work_dir, run_id=str(inspection.get("run_id", "")).strip(), policy_profile=policy_profile)
    return {
        **result,
        "success": bool(result.get("success", False)),
        "advanced": bool(result.get("success", False) or result.get("dry_run", False)),
        "selected_runtime_action": selected_command,
        "delegate_command": selected_command,
        "decision_rationale": rationale,
        "prompt_name": resolved_prompt,
        "run_id": str(refreshed.get("run_id", inspection.get("run_id", ""))).strip(),
        "task_id": str(refreshed.get("task_id", inspection.get("task_id", ""))).strip(),
        "task_spec": refreshed.get("task_spec", inspection.get("task_spec", {})),
        "run_state": refreshed.get("run_state", inspection.get("run_state", {})),
        "manifest_present": refreshed.get("manifest_present", inspection.get("manifest_present", False)),
        "runtime_api": refreshed.get("runtime_api", inspection.get("runtime_api", {})),
    }


def apply_control(work_dir: str, signal: str, *, run_id: str = "", reason: str = "",
                  policy_profile: str = "default") -> dict:
    """Apply a pause or cancellation signal through the stable runtime API."""
    inspection = inspect_run(work_dir, run_id=run_id, policy_profile=policy_profile)
    normalized_signal = str(signal or "").strip().lower().replace("_", "-")
    signal_map = {
        "pause": "pause-run",
        "pause-run": "pause-run",
        "cancel": "cancel-run",
        "cancel-run": "cancel-run",
    }
    if normalized_signal not in signal_map:
        return {
            **inspection,
            "success": False,
            "applied": False,
            "signal": normalized_signal,
            "delegate_command": "",
            "error": "unsupported_control_signal",
            "message": f"Unsupported control signal: {signal}",
            "allowed_signals": ["pause", "cancel"],
        }
    delegate = signal_map[normalized_signal]
    if delegate == "pause-run":
        result = kernel_execution.pause_run(work_dir, reason=reason)
    else:
        result = kernel_execution.cancel_run(work_dir, reason=reason)
    refreshed = inspect_run(work_dir, run_id=str(inspection.get("run_id", "")).strip(), policy_profile=policy_profile)
    return {
        **result,
        "success": bool(result.get("success", False)),
        "applied": bool(result.get("success", False)),
        "signal": normalized_signal,
        "delegate_command": delegate,
        "run_id": str(refreshed.get("run_id", inspection.get("run_id", ""))).strip(),
        "task_id": str(refreshed.get("task_id", inspection.get("task_id", ""))).strip(),
        "task_spec": refreshed.get("task_spec", inspection.get("task_spec", {})),
        "run_state": refreshed.get("run_state", inspection.get("run_state", {})),
        "runtime_api": refreshed.get("runtime_api", inspection.get("runtime_api", {})),
    }


def resume_run(work_dir: str, reason: str = "", *, run_id: str = "",
               policy_profile: str = "default") -> dict:
    """Resume a paused runtime through the stable runtime API."""
    inspection = inspect_run(work_dir, run_id=run_id, policy_profile=policy_profile)
    if not inspection.get("success", False):
        return {
            **inspection,
            "success": False,
            "resumed": False,
            "delegate_command": "resume-run",
        }
    if not _manifest_exists(work_dir):
        return {
            **inspection,
            "success": False,
            "resumed": False,
            "delegate_command": "resume-run",
            "error": "missing_manifest",
            "message": f"manifest.json not found in {work_dir}",
        }
    result = kernel_execution.resume_run(work_dir, reason=reason)
    refreshed = inspect_run(work_dir, run_id=str(inspection.get("run_id", "")).strip(), policy_profile=policy_profile)
    return {
        **result,
        "success": bool(result.get("success", False)),
        "resumed": bool(result.get("resumed", result.get("success", False))),
        "delegate_command": "resume-run",
        "run_id": str(refreshed.get("run_id", inspection.get("run_id", ""))).strip(),
        "task_id": str(refreshed.get("task_id", inspection.get("task_id", ""))).strip(),
        "task_spec": refreshed.get("task_spec", inspection.get("task_spec", {})),
        "run_state": refreshed.get("run_state", inspection.get("run_state", {})),
        "runtime_api": refreshed.get("runtime_api", inspection.get("runtime_api", {})),
    }


def finalize_run(work_dir: str, *, run_id: str = "", output_file: str = "",
                 header: str = "", inspect_only: bool = False,
                 policy_profile: str = "default") -> dict:
    """Finalize the runtime-facing run summary and optionally materialize merged output."""
    inspection = inspect_run(work_dir, run_id=run_id, policy_profile=policy_profile)
    if not inspection.get("success", False):
        return {
            **inspection,
            "success": False,
            "finalized": False,
            "delegate_command": "inspect-run",
        }
    if inspect_only or not str(output_file or "").strip():
        return {
            **inspection,
            "success": True,
            "finalized": True,
            "delegate_command": "inspect-run",
            "output_file": "",
            "message": "runtime finalized by inspection only",
        }
    if not _manifest_exists(work_dir):
        return {
            **inspection,
            "success": False,
            "finalized": False,
            "delegate_command": "merge-content",
            "error": "missing_manifest",
            "message": f"manifest.json not found in {work_dir}",
        }
    merge_result = kernel_merge.merge_content(work_dir, output_file, header_content=header)
    refreshed = inspect_run(work_dir, run_id=str(inspection.get("run_id", "")).strip(), policy_profile=policy_profile)
    artifacts = _collect_runtime_artifacts(
        work_dir=work_dir,
        run_id=str(refreshed.get("run_id", inspection.get("run_id", ""))).strip(),
        output_file=output_file,
    )
    artifact_graph = kernel_artifacts.build_artifact_graph(
        run_id=str(refreshed.get("run_id", inspection.get("run_id", ""))).strip(),
        artifacts=artifacts,
    )
    return {
        **merge_result,
        "success": bool(merge_result.get("success", False)),
        "finalized": bool(merge_result.get("success", False)),
        "delegate_command": "merge-content",
        "run_id": str(refreshed.get("run_id", inspection.get("run_id", ""))).strip(),
        "task_id": str(refreshed.get("task_id", inspection.get("task_id", ""))).strip(),
        "task_spec": refreshed.get("task_spec", inspection.get("task_spec", {})),
        "run_state": refreshed.get("run_state", inspection.get("run_state", {})),
        "runtime_api": refreshed.get("runtime_api", inspection.get("runtime_api", {})),
        "artifacts": artifacts,
        "artifact_graph": artifact_graph,
    }
