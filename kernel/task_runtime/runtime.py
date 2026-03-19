"""Generic task-runtime ownership, command envelope, and telemetry helpers."""

import hashlib
import json
import os
import random
import time
from pathlib import Path

from . import contracts as kernel_contracts


COMMAND_RESULT_SCHEMA_VERSION = 1
COMMAND_RESULT_FORMAT = "yt_transcript.command_result/v1"
TELEMETRY_EVENT_SCHEMA_VERSION = 1
TELEMETRY_EVENT_FORMAT = "yt_transcript.telemetry_event/v1"
RUNTIME_OWNERSHIP_SCHEMA_VERSION = 1
RUNTIME_OWNERSHIP_FORMAT = "yt_transcript.runtime_owner/v1"
RUNTIME_OWNER_FILENAME = ".runtime_owner.json"


def _parse_int(value, default: int = 0) -> int:
    """Parse an integer value and fall back to `default` on invalid input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def new_trace_id(command: str = "") -> str:
    """Create a short trace ID for telemetry, ownership, and resumable runs."""
    payload = f"{command}:{time.time_ns()}:{os.getpid()}:{random.random()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _command_warning_count(result) -> int:
    """Count warnings from heterogeneous command result payloads."""
    if not isinstance(result, dict):
        return 0
    if "warning_count" in result:
        return max(0, _parse_int(result.get("warning_count"), 0))
    warnings = result.get("warnings", [])
    return len(warnings) if isinstance(warnings, list) else 0


def _infer_command_success(result) -> bool:
    """Infer a normalized success flag from different command result schemas."""
    if isinstance(result, dict):
        if "success" in result:
            return bool(result.get("success"))
        if "passed" in result:
            return bool(result.get("passed"))
        if "valid" in result:
            return bool(result.get("valid"))
        hard_failures = result.get("hard_failures")
        if isinstance(hard_failures, list):
            return len(hard_failures) == 0
    return True


def _infer_command_document_id(command: str, result, context: dict | None = None) -> str:
    """Infer the most stable document identifier available for telemetry."""
    context = context or {}
    if isinstance(result, dict):
        outputs = result.get("outputs", {}) if isinstance(result.get("outputs", {}), dict) else {}
        if outputs.get("work_dir"):
            return Path(str(outputs["work_dir"])).name
        manifest_path = str(result.get("manifest_path", "") or "").strip()
        if manifest_path:
            return Path(manifest_path).resolve().parent.name
        normalized_document_path = str(result.get("normalized_document_path", "") or "").strip()
        if normalized_document_path:
            return Path(normalized_document_path).resolve().stem
    for key in ("work_dir", "output_dir"):
        value = str(context.get(key, "") or "").strip()
        if value:
            return Path(value).resolve().name
    for key in ("state_path", "state_ref"):
        value = str(context.get(key, "") or "").strip()
        if value:
            return Path(value).resolve().stem
    if command:
        return command
    return ""


def resolve_command_telemetry_path(command: str, result, context: dict | None = None) -> str:
    """Resolve the telemetry file path closest to the current command outputs."""
    del command
    context = context or {}
    candidates = []

    for key in ("work_dir", "output_dir"):
        value = str(context.get(key, "") or "").strip()
        if value:
            candidates.append(Path(value).expanduser())

    if isinstance(result, dict):
        manifest_path = str(result.get("manifest_path", "") or "").strip()
        if manifest_path:
            candidates.append(Path(manifest_path).expanduser().parent)
        result_work_dir = str(result.get("work_dir", "") or "").strip()
        if result_work_dir:
            candidates.append(Path(result_work_dir).expanduser())
        normalized_document_path = str(result.get("normalized_document_path", "") or "").strip()
        if normalized_document_path:
            candidates.append(Path(normalized_document_path).expanduser().parent)
        outputs = result.get("outputs", {}) if isinstance(result.get("outputs", {}), dict) else {}
        output_work_dir = str(outputs.get("work_dir", "") or "").strip()
        if output_work_dir:
            candidates.append(Path(output_work_dir).expanduser())
        for key in ("output_file", "optimized_text"):
            value = str(result.get(key, outputs.get(key, "")) or "").strip()
            if value:
                candidates.append(Path(value).expanduser().parent)

    for key in ("output_file", "optimized_text_path", "output_path", "normalized_document_path", "state_path", "state_ref"):
        value = str(context.get(key, "") or "").strip()
        if value:
            candidates.append(Path(value).expanduser().parent)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        return str(resolved / "telemetry.jsonl")
    return ""


def build_command_telemetry_event(command: str, result, *, trace_id: str,
                                  started_at: float, context: dict | None = None,
                                  telemetry_path: str = "", contract_summary: dict | None = None) -> dict:
    """Build the append-only telemetry event recorded for one command invocation."""
    context = context or {}
    duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
    warning_count = _command_warning_count(result)
    # Keep the base envelope stable so operators can query command history even
    # when individual commands return slightly different result payloads.
    event = {
        "schema_version": TELEMETRY_EVENT_SCHEMA_VERSION,
        "format": TELEMETRY_EVENT_FORMAT,
        "event_type": "command_result",
        "command": str(command or "").strip(),
        "trace_id": trace_id,
        "timestamp": _now_iso(),
        "duration_ms": duration_ms,
        "success": _infer_command_success(result),
        "warning_count": warning_count,
        "document_id": _infer_command_document_id(command, result, context),
        "telemetry_path": telemetry_path,
    }
    if isinstance(result, dict):
        event["request_url"] = str(result.get("request_url", "") or "")
        if "processed_count" in result:
            event["processed_count"] = _parse_int(result.get("processed_count"), 0)
        if "failed_count" in result:
            event["failed_count"] = _parse_int(result.get("failed_count"), 0)
        if "replan_required" in result:
            event["replan_required"] = bool(result.get("replan_required", False))
        if "dry_run" in result:
            event["dry_run"] = bool(result.get("dry_run", False))
    prompt_name = str(context.get("prompt_name", context.get("prompt", "")) or "").strip()
    if not prompt_name and isinstance(result, dict):
        prompt_name = str(result.get("prompt_name", result.get("plan", {}).get("prompt_name", "")) or "").strip()
    if prompt_name:
        event["prompt_name"] = prompt_name
    if isinstance(contract_summary, dict) and contract_summary:
        event["contracts"] = contract_summary
    return event


def append_command_telemetry_event(telemetry_path: str, event: dict) -> str:
    """Append a command telemetry event to the JSONL telemetry log."""
    path_text = str(telemetry_path or "").strip()
    if not path_text:
        return ""
    telemetry_file = Path(path_text)
    try:
        telemetry_file.parent.mkdir(parents=True, exist_ok=True)
        with telemetry_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return str(telemetry_file)
    except OSError:
        return ""


def build_command_result_envelope(command: str, result, *, trace_id: str = "",
                                  started_at: float | None = None,
                                  telemetry_path: str = "",
                                  context: dict | None = None) -> dict:
    """Wrap a raw command result with trace metadata and persisted telemetry."""
    context = context or {}
    resolved_trace_id = str(trace_id or new_trace_id(command)).strip()
    started = started_at if started_at is not None else time.monotonic()
    resolved_telemetry_path = telemetry_path or resolve_command_telemetry_path(command, result, context)
    contract_bundle = kernel_contracts.build_command_contract_bundle(
        command,
        result,
        context=context,
        trace_id=resolved_trace_id,
    )
    contract_summary = kernel_contracts.summarize_contract_bundle(contract_bundle)
    event = build_command_telemetry_event(
        command,
        result,
        trace_id=resolved_trace_id,
        started_at=started,
        context=context,
        telemetry_path=resolved_telemetry_path,
        contract_summary=contract_summary,
    )
    persisted_telemetry_path = append_command_telemetry_event(resolved_telemetry_path, event)
    event["telemetry_path"] = persisted_telemetry_path
    return {
        "schema_version": COMMAND_RESULT_SCHEMA_VERSION,
        "format": COMMAND_RESULT_FORMAT,
        "command": str(command or "").strip(),
        "trace_id": resolved_trace_id,
        "generated_at": event["timestamp"],
        "ok": bool(event["success"]),
        "telemetry": {
            "event_type": event["event_type"],
            "duration_ms": event["duration_ms"],
            "warning_count": event["warning_count"],
            "document_id": event.get("document_id", ""),
            "telemetry_path": persisted_telemetry_path,
            "contracts": contract_summary,
        },
        "contracts": contract_bundle,
        "result": result,
    }


def run_registered_kernel_command(command: str, *, kwargs: dict,
                                  registry: dict[str, object],
                                  process_chunks_handler) -> dict:
    """Dispatch a kernel command and normalize its telemetry envelope."""
    normalized_command = str(command or "").strip()
    started_at = time.monotonic()
    trace_id = new_trace_id(normalized_command)

    if normalized_command == "process-chunks":
        result = process_chunks_handler(**kwargs)
        return build_command_result_envelope(
            normalized_command,
            result,
            trace_id=trace_id,
            started_at=started_at,
            context=kwargs,
        )

    if normalized_command not in registry:
        raise ValueError(f"Unsupported kernel command: {normalized_command}")
    result = registry[normalized_command](**kwargs)
    return build_command_result_envelope(
        normalized_command,
        result,
        trace_id=trace_id,
        started_at=started_at,
        context=kwargs,
    )


def _runtime_owner_path(work_dir: str) -> Path:
    """Return the runtime-owner marker path for a work directory."""
    return Path(str(work_dir or "")).expanduser().resolve() / RUNTIME_OWNER_FILENAME


def _is_process_alive(pid: int) -> bool:
    """Return whether process alive."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _load_runtime_owner(owner_path: Path) -> tuple[dict | None, str]:
    """Load and validate the runtime-owner record from disk."""
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, ""
    except OSError as error:
        return None, f"failed to read owner file: {error}"
    except json.JSONDecodeError as error:
        return None, f"invalid owner JSON: {error.msg}"
    if not isinstance(payload, dict):
        return None, "invalid owner payload"
    return payload, ""


def summarize_runtime_ownership(record: dict | None, owner_path: str | Path = "",
                                status: str = "") -> dict:
    """Normalize an ownership record into an operator-friendly summary payload."""
    payload = record if isinstance(record, dict) else {}
    owner_file = str(owner_path or payload.get("owner_path", "")).strip()
    summary = {
        "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
        "format": RUNTIME_OWNERSHIP_FORMAT,
        "status": str(status or payload.get("status", "")).strip(),
        "owner_id": str(payload.get("owner_id", "")).strip(),
        "operation": str(payload.get("operation", "")).strip(),
        "pid": _parse_int(payload.get("pid"), 0),
        "work_dir": str(payload.get("work_dir", "")).strip(),
        "owner_path": owner_file,
        "acquired_at": str(payload.get("acquired_at", "")).strip(),
    }
    if "stale_reason" in payload:
        summary["stale_reason"] = str(payload.get("stale_reason", "")).strip()
    if "message" in payload:
        summary["message"] = str(payload.get("message", "")).strip()
    if "error" in payload:
        summary["error"] = str(payload.get("error", "")).strip()
    return summary


def _classify_stale_owner(record: dict | None, read_error: str = "") -> str:
    """Classify whether an owner record is stale and safe to recover."""
    if read_error:
        return "invalid_owner_file"
    payload = record if isinstance(record, dict) else {}
    pid = _parse_int(payload.get("pid"), 0)
    if pid <= 0:
        return "missing_pid"
    if not _is_process_alive(pid):
        return "dead_process"
    return ""


def read_runtime_ownership(work_dir: str) -> dict:
    """Read the current ownership state for a work directory."""
    owner_path = _runtime_owner_path(work_dir)
    existing_record, read_error = _load_runtime_owner(owner_path)
    if not owner_path.exists():
        result = summarize_runtime_ownership(None, owner_path, status="absent")
        result.update({
            "success": True,
            "held": False,
        })
        return result

    stale_reason = _classify_stale_owner(existing_record, read_error)
    if read_error:
        result = summarize_runtime_ownership({"error": read_error}, owner_path, status="invalid")
        result.update({
            "success": False,
            "held": False,
            "error": read_error,
        })
        return result
    if stale_reason:
        payload = dict(existing_record or {})
        payload["stale_reason"] = stale_reason
        result = summarize_runtime_ownership(payload, owner_path, status="stale")
        result.update({
            "success": True,
            "held": False,
            "stale": True,
        })
        return result

    result = summarize_runtime_ownership(existing_record, owner_path, status="held")
    result.update({
        "success": True,
        "held": True,
        "held_by_current_process": _parse_int((existing_record or {}).get("pid"), 0) == os.getpid(),
    })
    return result


def acquire_runtime_ownership(work_dir: str, operation: str, owner_id: str = "") -> dict:
    """Acquire exclusive ownership for a runtime mutation in `work_dir`."""
    work_path = Path(str(work_dir or "")).expanduser()
    resolved_work_dir = str(work_path.resolve())
    owner_path = Path(resolved_work_dir) / RUNTIME_OWNER_FILENAME
    operation_name = str(operation or "").strip()
    requested_owner_id = str(owner_id or new_trace_id(f"owner:{operation_name}")).strip()

    if not Path(resolved_work_dir).exists():
        return {
            "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
            "format": RUNTIME_OWNERSHIP_FORMAT,
            "status": "invalid_work_dir",
            "success": False,
            "acquired": False,
            "released": False,
            "owner_id": requested_owner_id,
            "operation": operation_name,
            "requested_pid": os.getpid(),
            "work_dir": resolved_work_dir,
            "owner_path": str(owner_path),
            "error": f"Work directory not found: {resolved_work_dir}",
            "message": f"Cannot acquire runtime ownership because the work directory does not exist: {resolved_work_dir}",
        }

    recovered_stale_owner = None
    for _ in range(3):
        # The owner file is the durable lock record shared by all processes that
        # may mutate the same work directory.
        owner_record = {
            "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
            "format": RUNTIME_OWNERSHIP_FORMAT,
            "owner_id": requested_owner_id,
            "operation": operation_name,
            "pid": os.getpid(),
            "work_dir": resolved_work_dir,
            "acquired_at": _now_iso(),
        }
        try:
            # `O_EXCL` keeps acquisition deterministic: either we created the
            # lock record, or another active owner already holds it.
            fd = os.open(str(owner_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            existing_record, read_error = _load_runtime_owner(owner_path)
            existing_owner_id = str((existing_record or {}).get("owner_id", "")).strip()
            if existing_owner_id and existing_owner_id == requested_owner_id:
                result = summarize_runtime_ownership(existing_record, owner_path, status="already_owned")
                result.update({
                    "success": True,
                    "acquired": False,
                    "released": False,
                    "held_by_current_process": _parse_int((existing_record or {}).get("pid"), 0) == os.getpid(),
                })
                if recovered_stale_owner:
                    result["recovered_stale_owner"] = recovered_stale_owner
                return result

            stale_reason = _classify_stale_owner(existing_record, read_error)
            if stale_reason:
                # Recover only obviously stale owners so interrupted local runs
                # do not permanently wedge the work directory.
                stale_payload = dict(existing_record or {})
                stale_payload["stale_reason"] = stale_reason
                if read_error:
                    stale_payload["error"] = read_error
                recovered_stale_owner = summarize_runtime_ownership(stale_payload, owner_path, status="stale")
                try:
                    owner_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    return {
                        "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
                        "format": RUNTIME_OWNERSHIP_FORMAT,
                        "status": "stale_recovery_failed",
                        "success": False,
                        "acquired": False,
                        "released": False,
                        "owner_id": requested_owner_id,
                        "operation": operation_name,
                        "requested_pid": os.getpid(),
                        "work_dir": resolved_work_dir,
                        "owner_path": str(owner_path),
                        "error": f"Failed to recover stale runtime owner: {error}",
                        "message": f"A stale runtime owner was detected, but the owner file could not be removed: {error}",
                        "recovered_stale_owner": recovered_stale_owner,
                    }
                continue

            active_owner = summarize_runtime_ownership(existing_record, owner_path, status="held")
            active_owner["held_by_current_process"] = _parse_int((existing_record or {}).get("pid"), 0) == os.getpid()
            return {
                "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
                "format": RUNTIME_OWNERSHIP_FORMAT,
                "status": "conflict",
                "success": False,
                "acquired": False,
                "released": False,
                "owner_id": requested_owner_id,
                "operation": operation_name,
                "requested_pid": os.getpid(),
                "work_dir": resolved_work_dir,
                "owner_path": str(owner_path),
                "active_owner": active_owner,
                "error": f"Runtime ownership conflict for {operation_name or 'mutation'}",
                "message": f"Another active runtime owner is already holding {owner_path.name} for {resolved_work_dir}.",
            }
        except OSError as error:
            return {
                "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
                "format": RUNTIME_OWNERSHIP_FORMAT,
                "status": "io_error",
                "success": False,
                "acquired": False,
                "released": False,
                "owner_id": requested_owner_id,
                "operation": operation_name,
                "requested_pid": os.getpid(),
                "work_dir": resolved_work_dir,
                "owner_path": str(owner_path),
                "error": f"Failed to acquire runtime ownership: {error}",
                "message": f"Could not create the runtime owner file: {error}",
            }
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(owner_record, ensure_ascii=False, indent=2))
        except OSError as error:
            try:
                owner_path.unlink()
            except OSError:
                pass
            return {
                "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
                "format": RUNTIME_OWNERSHIP_FORMAT,
                "status": "io_error",
                "success": False,
                "acquired": False,
                "released": False,
                "owner_id": requested_owner_id,
                "operation": operation_name,
                "requested_pid": os.getpid(),
                "work_dir": resolved_work_dir,
                "owner_path": str(owner_path),
                "error": f"Failed to persist runtime ownership: {error}",
                "message": f"The runtime owner file was created but could not be written: {error}",
            }
        result = summarize_runtime_ownership(owner_record, owner_path, status="acquired")
        result.update({
            "success": True,
            "acquired": True,
            "released": False,
            "held_by_current_process": True,
        })
        if recovered_stale_owner:
            result["recovered_stale_owner"] = recovered_stale_owner
        return result

    return {
        "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
        "format": RUNTIME_OWNERSHIP_FORMAT,
        "status": "conflict",
        "success": False,
        "acquired": False,
        "released": False,
        "owner_id": requested_owner_id,
        "operation": operation_name,
        "requested_pid": os.getpid(),
        "work_dir": resolved_work_dir,
        "owner_path": str(owner_path),
        "error": "Failed to acquire runtime ownership after retries",
        "message": "The runtime owner file remained unavailable after stale-owner recovery attempts.",
    }


def release_runtime_ownership(work_dir: str, owner_id: str = "") -> dict:
    """Release a previously acquired runtime-owner marker."""
    owner_path = _runtime_owner_path(work_dir)
    resolved_work_dir = str(owner_path.parent)
    existing_record, read_error = _load_runtime_owner(owner_path)
    requested_owner_id = str(owner_id or "").strip()

    if not owner_path.exists():
        return {
            "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
            "format": RUNTIME_OWNERSHIP_FORMAT,
            "status": "missing",
            "success": True,
            "released": False,
            "owner_id": requested_owner_id,
            "work_dir": resolved_work_dir,
            "owner_path": str(owner_path),
            "message": "Runtime owner file already absent",
        }

    existing_owner_id = str((existing_record or {}).get("owner_id", "")).strip()
    if requested_owner_id and existing_owner_id and existing_owner_id != requested_owner_id:
        return {
            "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
            "format": RUNTIME_OWNERSHIP_FORMAT,
            "status": "not_owner",
            "success": False,
            "released": False,
            "owner_id": requested_owner_id,
            "work_dir": resolved_work_dir,
            "owner_path": str(owner_path),
            "error": "Runtime owner mismatch during release",
            "message": "The current runtime owner does not match the caller attempting to release it.",
            "active_owner": summarize_runtime_ownership(existing_record, owner_path, status="held"),
        }

    try:
        owner_path.unlink()
    except FileNotFoundError:
        return {
            "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
            "format": RUNTIME_OWNERSHIP_FORMAT,
            "status": "missing",
            "success": True,
            "released": False,
            "owner_id": requested_owner_id or existing_owner_id,
            "work_dir": resolved_work_dir,
            "owner_path": str(owner_path),
            "message": "Runtime owner file already absent",
        }
    except OSError as error:
        result = summarize_runtime_ownership(existing_record, owner_path, status="release_failed")
        result.update({
            "success": False,
            "released": False,
            "work_dir": resolved_work_dir,
            "owner_id": requested_owner_id or existing_owner_id,
            "error": f"Failed to release runtime ownership: {error}",
            "message": f"The runtime owner file could not be removed: {error}",
        })
        if read_error:
            result["read_error"] = read_error
        return result

    result = summarize_runtime_ownership(existing_record, owner_path, status="released")
    result.update({
        "success": True,
        "released": True,
        "work_dir": resolved_work_dir,
        "owner_id": requested_owner_id or existing_owner_id,
        "released_at": _now_iso(),
    })
    if read_error:
        result["read_error"] = read_error
    return result


def finalize_runtime_ownership(ownership: dict | None, release_result: dict | None = None) -> dict:
    """Attach release outcome details to an ownership summary payload."""
    if not isinstance(ownership, dict):
        return {}
    summary = dict(ownership)
    if isinstance(release_result, dict):
        summary["released"] = bool(release_result.get("released", False))
        summary["release_status"] = str(release_result.get("status", "")).strip()
        if release_result.get("released_at"):
            summary["released_at"] = str(release_result.get("released_at", "")).strip()
        if release_result.get("error"):
            summary["release_error"] = str(release_result.get("error", "")).strip()
    return summary
