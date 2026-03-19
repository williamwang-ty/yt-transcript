"""Persistent runtime-state and control-file helpers."""

import json
import os
import time
from pathlib import Path

from . import runtime as kernel_runtime


MANIFEST_FILENAME = "manifest.json"
RUNTIME_CANCEL_SCHEMA_VERSION = 1
RUNTIME_CANCEL_FORMAT = "yt_transcript.runtime_cancel/v1"
RUNTIME_CANCEL_FILENAME = ".runtime_cancel.json"
RUNTIME_PAUSE_SCHEMA_VERSION = 1
RUNTIME_PAUSE_FORMAT = "yt_transcript.runtime_pause/v1"
RUNTIME_PAUSE_FILENAME = ".runtime_pause.json"


def _now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace `path` with the provided text content."""
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def write_json_file(path: Path, payload: dict) -> None:
    """Serialize a JSON object with stable formatting and atomic replacement."""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json_file(path: Path) -> tuple[dict | None, str]:
    """Read a JSON object from disk and return a payload-or-error tuple."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except OSError as error:
        return None, f"read_error: {error}"
    except json.JSONDecodeError as error:
        return None, f"invalid_json: {error.msg}"
    if not isinstance(payload, dict):
        return None, "invalid_payload"
    return payload, ""


def manifest_path_for(work_dir: str) -> Path:
    """Return the canonical manifest path for a work directory."""
    return Path(str(work_dir or "")).expanduser().resolve() / MANIFEST_FILENAME


def load_manifest(work_dir: str) -> tuple[Path, dict | None, str]:
    """Load the manifest for a work directory."""
    manifest_path = manifest_path_for(work_dir)
    manifest, error = read_json_file(manifest_path)
    return manifest_path, manifest, error


def write_manifest(manifest_path: Path, manifest: dict) -> None:
    """Persist the manifest atomically."""
    write_json_file(manifest_path, manifest)


def _runtime_signal_path(work_dir: str, filename: str) -> Path:
    """Return the path for a runtime control-file signal."""
    return Path(str(work_dir or "")).expanduser().resolve() / filename


def _summarize_runtime_signal(work_dir: str, *, filename: str,
                              schema_version: int, format_name: str,
                              path_field: str, signal_name: str) -> dict:
    """Summarize the current state of a pause/cancel control file."""
    signal_path = _runtime_signal_path(work_dir, filename)
    payload, error = read_json_file(signal_path)
    if error == "missing":
        return {
            "schema_version": schema_version,
            "format": format_name,
            "signal": signal_name,
            "status": "absent",
            "requested": False,
            "reason": "",
            "requested_at": "",
            path_field: str(signal_path),
        }
    if error:
        return {
            "schema_version": schema_version,
            "format": format_name,
            "signal": signal_name,
            "status": "invalid",
            "requested": False,
            "reason": "",
            "requested_at": "",
            path_field: str(signal_path),
            "error": error,
        }
    return {
        "schema_version": schema_version,
        "format": format_name,
        "signal": signal_name,
        "status": "requested",
        "requested": True,
        "reason": str(payload.get("reason", "")).strip(),
        "requested_at": str(payload.get("requested_at", "")).strip(),
        path_field: str(signal_path),
    }


def _request_runtime_signal(work_dir: str, reason: str = "", *, filename: str,
                            schema_version: int, format_name: str,
                            path_field: str, signal_name: str) -> dict:
    """Create a durable pause/cancel signal for an active runtime."""
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    signal_path = _runtime_signal_path(str(work_path), filename)
    if not work_path.exists():
        return {
            "schema_version": schema_version,
            "format": format_name,
            "signal": signal_name,
            "status": "invalid_work_dir",
            "requested": False,
            "reason": str(reason or "").strip(),
            "requested_at": "",
            path_field: str(signal_path),
            "success": False,
            "error": f"Work directory not found: {work_path}",
        }
    # Control signals live in separate files so operators can pause or
    # cancel a run without rewriting the full manifest.
    payload = {
        "schema_version": schema_version,
        "format": format_name,
        "signal": signal_name,
        "reason": str(reason or "").strip(),
        "requested_at": _now_iso(),
        "work_dir": str(signal_path.parent),
    }
    write_json_file(signal_path, payload)
    result = _summarize_runtime_signal(
        str(work_path),
        filename=filename,
        schema_version=schema_version,
        format_name=format_name,
        path_field=path_field,
        signal_name=signal_name,
    )
    result["success"] = True
    return result


def _clear_runtime_signal(work_dir: str, *, filename: str,
                          schema_version: int, format_name: str,
                          path_field: str, signal_name: str) -> dict:
    """Clear a pause/cancel control file and report the outcome."""
    signal_path = _runtime_signal_path(work_dir, filename)
    current = _summarize_runtime_signal(
        work_dir,
        filename=filename,
        schema_version=schema_version,
        format_name=format_name,
        path_field=path_field,
        signal_name=signal_name,
    )
    try:
        signal_path.unlink()
        removed = True
    except FileNotFoundError:
        removed = False
    except OSError as error:
        return {
            **current,
            "success": False,
            "cleared": False,
            "error": f"failed_to_clear: {error}",
        }
    return {
        **current,
        "success": True,
        "cleared": removed,
        "status": "cleared" if removed else "absent",
        "requested": False,
    }


def runtime_cancel_path(work_dir: str) -> Path:
    """Return the cancel-signal path for a work directory."""
    return _runtime_signal_path(work_dir, RUNTIME_CANCEL_FILENAME)


def summarize_runtime_cancel_request(work_dir: str) -> dict:
    """Summarize the current runtime-cancel request, if any."""
    return _summarize_runtime_signal(
        work_dir,
        filename=RUNTIME_CANCEL_FILENAME,
        schema_version=RUNTIME_CANCEL_SCHEMA_VERSION,
        format_name=RUNTIME_CANCEL_FORMAT,
        path_field="cancel_path",
        signal_name="cancel",
    )


def request_runtime_cancel(work_dir: str, reason: str = "") -> dict:
    """Request cancellation for the active run in `work_dir`."""
    return _request_runtime_signal(
        work_dir,
        reason=reason,
        filename=RUNTIME_CANCEL_FILENAME,
        schema_version=RUNTIME_CANCEL_SCHEMA_VERSION,
        format_name=RUNTIME_CANCEL_FORMAT,
        path_field="cancel_path",
        signal_name="cancel",
    )


def consume_runtime_cancel(work_dir: str) -> dict:
    """Consume and clear a runtime-cancel request exactly once."""
    current = summarize_runtime_cancel_request(work_dir)
    if not current.get("requested", False):
        return {
            **current,
            "success": True,
            "consumed": False,
            "cleared": False,
        }
    cleared = clear_runtime_cancel(work_dir)
    return {
        **current,
        "success": bool(cleared.get("success", False)),
        "consumed": True,
        "cleared": bool(cleared.get("cleared", False)),
        "clear_status": str(cleared.get("status", "")).strip(),
    }


def clear_runtime_cancel(work_dir: str) -> dict:
    """Clear the runtime-cancel control file."""
    return _clear_runtime_signal(
        work_dir,
        filename=RUNTIME_CANCEL_FILENAME,
        schema_version=RUNTIME_CANCEL_SCHEMA_VERSION,
        format_name=RUNTIME_CANCEL_FORMAT,
        path_field="cancel_path",
        signal_name="cancel",
    )


def runtime_pause_path(work_dir: str) -> Path:
    """Return the pause-signal path for a work directory."""
    return _runtime_signal_path(work_dir, RUNTIME_PAUSE_FILENAME)


def summarize_runtime_pause_request(work_dir: str) -> dict:
    """Summarize the current runtime-pause request, if any."""
    return _summarize_runtime_signal(
        work_dir,
        filename=RUNTIME_PAUSE_FILENAME,
        schema_version=RUNTIME_PAUSE_SCHEMA_VERSION,
        format_name=RUNTIME_PAUSE_FORMAT,
        path_field="pause_path",
        signal_name="pause",
    )


def request_runtime_pause(work_dir: str, reason: str = "") -> dict:
    """Request pausing the active run in `work_dir`."""
    return _request_runtime_signal(
        work_dir,
        reason=reason,
        filename=RUNTIME_PAUSE_FILENAME,
        schema_version=RUNTIME_PAUSE_SCHEMA_VERSION,
        format_name=RUNTIME_PAUSE_FORMAT,
        path_field="pause_path",
        signal_name="pause",
    )


def clear_runtime_pause(work_dir: str) -> dict:
    """Clear the runtime-pause control file."""
    return _clear_runtime_signal(
        work_dir,
        filename=RUNTIME_PAUSE_FILENAME,
        schema_version=RUNTIME_PAUSE_SCHEMA_VERSION,
        format_name=RUNTIME_PAUSE_FORMAT,
        path_field="pause_path",
        signal_name="pause",
    )


def _effective_runtime_status(runtime_status: str, *, pause: dict, cancellation: dict) -> str:
    """Combine manifest status with out-of-band pause/cancel signals."""
    normalized_status = str(runtime_status or "").strip() or "pending"
    if pause.get("requested", False):
        if normalized_status == "paused":
            return "paused"
        if normalized_status == "running":
            return "pause_requested"
    if cancellation.get("requested", False) and normalized_status == "running":
        return "cancellation_requested"
    return normalized_status


def summarize_runtime_status(work_dir: str) -> dict:
    """Build an operator-facing summary of runtime, ownership, and chunk status."""
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    manifest_path, manifest, manifest_error = load_manifest(str(work_path))
    ownership = kernel_runtime.read_runtime_ownership(str(work_path))
    cancellation = summarize_runtime_cancel_request(str(work_path))
    pause = summarize_runtime_pause_request(str(work_path))

    # Read persisted manifest state first, then fold in out-of-band control
    # files so callers see the current effective status of the local job.
    runtime = manifest.get("runtime", {}) if isinstance(manifest, dict) and isinstance(manifest.get("runtime", {}), dict) else {}
    plan = manifest.get("plan", {}) if isinstance(manifest, dict) and isinstance(manifest.get("plan", {}), dict) else {}
    chunks = manifest.get("chunks", []) if isinstance(manifest, dict) and isinstance(manifest.get("chunks", []), list) else []

    status_counts = {}
    for chunk in chunks:
        status = str(chunk.get("status", "")).strip() or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "success": work_path.exists(),
        "work_dir": str(work_path),
        "manifest_path": str(manifest_path),
        "manifest_present": manifest is not None,
        "manifest_error": manifest_error,
        "runtime": runtime,
        "effective_runtime_status": _effective_runtime_status(str(runtime.get("status", "")), pause=pause, cancellation=cancellation),
        "plan": {
            "plan_id": str(plan.get("plan_id", "")).strip(),
            "prompt_name": str(plan.get("prompt_name", "")).strip(),
            "chunk_mode": str(plan.get("chunk_mode", "")).strip(),
        },
        "ownership": ownership,
        "cancellation": cancellation,
        "pause": pause,
        "total_chunks": len(chunks),
        "status_counts": status_counts,
        "completed_chunks": status_counts.get("done", 0),
        "failed_chunks": status_counts.get("failed", 0),
        "pending_chunks": status_counts.get("pending", 0),
        "interrupted_chunks": status_counts.get("interrupted", 0),
        "superseded_chunks": status_counts.get("superseded", 0),
    }
