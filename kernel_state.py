import json
import os
import time
from pathlib import Path

import kernel_runtime


MANIFEST_FILENAME = "manifest.json"
RUNTIME_CANCEL_SCHEMA_VERSION = 1
RUNTIME_CANCEL_FORMAT = "yt_transcript.runtime_cancel/v1"
RUNTIME_CANCEL_FILENAME = ".runtime_cancel.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def write_json_file(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json_file(path: Path) -> tuple[dict | None, str]:
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
    return Path(str(work_dir or "")).expanduser().resolve() / MANIFEST_FILENAME


def load_manifest(work_dir: str) -> tuple[Path, dict | None, str]:
    manifest_path = manifest_path_for(work_dir)
    manifest, error = read_json_file(manifest_path)
    return manifest_path, manifest, error


def write_manifest(manifest_path: Path, manifest: dict) -> None:
    write_json_file(manifest_path, manifest)


def runtime_cancel_path(work_dir: str) -> Path:
    return Path(str(work_dir or "")).expanduser().resolve() / RUNTIME_CANCEL_FILENAME


def summarize_runtime_cancel_request(work_dir: str) -> dict:
    cancel_path = runtime_cancel_path(work_dir)
    payload, error = read_json_file(cancel_path)
    if error == "missing":
        return {
            "schema_version": RUNTIME_CANCEL_SCHEMA_VERSION,
            "format": RUNTIME_CANCEL_FORMAT,
            "status": "absent",
            "requested": False,
            "reason": "",
            "requested_at": "",
            "cancel_path": str(cancel_path),
        }
    if error:
        return {
            "schema_version": RUNTIME_CANCEL_SCHEMA_VERSION,
            "format": RUNTIME_CANCEL_FORMAT,
            "status": "invalid",
            "requested": False,
            "reason": "",
            "requested_at": "",
            "cancel_path": str(cancel_path),
            "error": error,
        }
    return {
        "schema_version": RUNTIME_CANCEL_SCHEMA_VERSION,
        "format": RUNTIME_CANCEL_FORMAT,
        "status": "requested",
        "requested": True,
        "reason": str(payload.get("reason", "")).strip(),
        "requested_at": str(payload.get("requested_at", "")).strip(),
        "cancel_path": str(cancel_path),
    }


def request_runtime_cancel(work_dir: str, reason: str = "") -> dict:
    cancel_path = runtime_cancel_path(work_dir)
    payload = {
        "schema_version": RUNTIME_CANCEL_SCHEMA_VERSION,
        "format": RUNTIME_CANCEL_FORMAT,
        "reason": str(reason or "").strip(),
        "requested_at": _now_iso(),
        "work_dir": str(cancel_path.parent),
    }
    cancel_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(cancel_path, payload)
    result = summarize_runtime_cancel_request(work_dir)
    result["success"] = True
    return result


def clear_runtime_cancel(work_dir: str) -> dict:
    cancel_path = runtime_cancel_path(work_dir)
    current = summarize_runtime_cancel_request(work_dir)
    try:
        cancel_path.unlink()
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


def summarize_runtime_status(work_dir: str) -> dict:
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    manifest_path, manifest, manifest_error = load_manifest(str(work_path))
    ownership = kernel_runtime.read_runtime_ownership(str(work_path))
    cancellation = summarize_runtime_cancel_request(str(work_path))

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
        "plan": {
            "plan_id": str(plan.get("plan_id", "")).strip(),
            "prompt_name": str(plan.get("prompt_name", "")).strip(),
            "chunk_mode": str(plan.get("chunk_mode", "")).strip(),
        },
        "ownership": ownership,
        "cancellation": cancellation,
        "total_chunks": len(chunks),
        "status_counts": status_counts,
        "completed_chunks": status_counts.get("done", 0),
        "failed_chunks": status_counts.get("failed", 0),
        "pending_chunks": status_counts.get("pending", 0),
        "interrupted_chunks": status_counts.get("interrupted", 0),
        "superseded_chunks": status_counts.get("superseded", 0),
    }
