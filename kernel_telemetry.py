import json
from pathlib import Path


TELEMETRY_FILENAME = "telemetry.jsonl"


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_telemetry_file(work_dir: str = "", telemetry_path: str = "") -> tuple[Path, dict]:
    candidate = str(telemetry_path or "").strip()
    if candidate:
        path = Path(candidate).expanduser().resolve()
        return path, {
            "work_dir": str(path.parent),
            "telemetry_path": str(path),
            "source": "telemetry_path",
        }

    work_dir_text = str(work_dir or "").strip()
    if work_dir_text:
        work_path = Path(work_dir_text).expanduser().resolve()
        return work_path / TELEMETRY_FILENAME, {
            "work_dir": str(work_path),
            "telemetry_path": str(work_path / TELEMETRY_FILENAME),
            "source": "work_dir",
        }

    path = Path(TELEMETRY_FILENAME).expanduser().resolve()
    return path, {
        "work_dir": str(path.parent),
        "telemetry_path": str(path),
        "source": "default",
    }


def _load_events(path: Path) -> tuple[list[dict], int]:
    invalid_line_count = 0
    events = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], 0
    except OSError:
        return [], 0

    for line in lines:
        raw = str(line or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            invalid_line_count += 1
            continue
        if not isinstance(payload, dict):
            invalid_line_count += 1
            continue
        events.append(payload)
    return events, invalid_line_count


def _matches_filters(event: dict, *, command: str = "", trace_id: str = "",
                     document_id: str = "", success: bool | None = None) -> bool:
    if command and str(event.get("command", "")).strip() != str(command).strip():
        return False
    if trace_id and str(event.get("trace_id", "")).strip() != str(trace_id).strip():
        return False
    if document_id and str(event.get("document_id", "")).strip() != str(document_id).strip():
        return False
    if success is not None and bool(event.get("success", False)) != bool(success):
        return False
    return True


def read_telemetry_events(*, work_dir: str = "", telemetry_path: str = "",
                          limit: int = 20, command: str = "", trace_id: str = "",
                          document_id: str = "", success: bool | None = None) -> dict:
    path, resolved = _resolve_telemetry_file(work_dir=work_dir, telemetry_path=telemetry_path)
    if not path.exists():
        return {
            **resolved,
            "success": False,
            "events": [],
            "returned_count": 0,
            "matching_event_count": 0,
            "invalid_line_count": 0,
            "error": f"Telemetry file not found: {path}",
        }

    all_events, invalid_line_count = _load_events(path)
    matching = [
        event for event in all_events
        if _matches_filters(
            event,
            command=command,
            trace_id=trace_id,
            document_id=document_id,
            success=success,
        )
    ]
    capped_limit = max(0, _parse_int(limit, 20))
    returned = matching[-capped_limit:] if capped_limit > 0 else list(matching)
    return {
        **resolved,
        "success": True,
        "events": returned,
        "returned_count": len(returned),
        "matching_event_count": len(matching),
        "total_event_count": len(all_events),
        "invalid_line_count": invalid_line_count,
        "filters": {
            "command": str(command or "").strip(),
            "trace_id": str(trace_id or "").strip(),
            "document_id": str(document_id or "").strip(),
            "success": success,
            "limit": capped_limit,
        },
    }


def summarize_telemetry(*, work_dir: str = "", telemetry_path: str = "",
                        command: str = "", document_id: str = "",
                        success: bool | None = None, recent_limit: int = 5) -> dict:
    path, resolved = _resolve_telemetry_file(work_dir=work_dir, telemetry_path=telemetry_path)
    if not path.exists():
        return {
            **resolved,
            "success": False,
            "summary": {},
            "recent_events": [],
            "error": f"Telemetry file not found: {path}",
        }

    all_events, invalid_line_count = _load_events(path)
    matching = [
        event for event in all_events
        if _matches_filters(
            event,
            command=command,
            document_id=document_id,
            success=success,
        )
    ]
    durations = [max(0, _parse_int(event.get("duration_ms"), 0)) for event in matching]
    warning_counts = [max(0, _parse_int(event.get("warning_count"), 0)) for event in matching]
    success_count = sum(1 for event in matching if bool(event.get("success", False)))
    failure_count = len(matching) - success_count

    command_counts = {}
    document_counts = {}
    prompt_counts = {}
    for event in matching:
        command_name = str(event.get("command", "")).strip() or "unknown"
        command_counts[command_name] = command_counts.get(command_name, 0) + 1

        document_name = str(event.get("document_id", "")).strip() or ""
        if document_name:
            document_counts[document_name] = document_counts.get(document_name, 0) + 1

        prompt_name = str(event.get("prompt_name", "")).strip() or ""
        if prompt_name:
            prompt_counts[prompt_name] = prompt_counts.get(prompt_name, 0) + 1

    capped_recent_limit = max(0, _parse_int(recent_limit, 5))
    recent_events = matching[-capped_recent_limit:] if capped_recent_limit > 0 else list(matching)

    summary = {
        "matching_event_count": len(matching),
        "total_event_count": len(all_events),
        "invalid_line_count": invalid_line_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "warning_event_count": sum(1 for value in warning_counts if value > 0),
        "warning_count_total": sum(warning_counts),
        "duration_ms_total": sum(durations),
        "duration_ms_avg": int(sum(durations) / len(durations)) if durations else 0,
        "duration_ms_max": max(durations) if durations else 0,
        "first_timestamp": str(matching[0].get("timestamp", "")).strip() if matching else "",
        "last_timestamp": str(matching[-1].get("timestamp", "")).strip() if matching else "",
        "command_counts": dict(sorted(command_counts.items())),
        "document_counts": dict(sorted(document_counts.items())),
        "prompt_counts": dict(sorted(prompt_counts.items())),
    }

    return {
        **resolved,
        "success": True,
        "filters": {
            "command": str(command or "").strip(),
            "document_id": str(document_id or "").strip(),
            "success": success,
            "recent_limit": capped_recent_limit,
        },
        "summary": summary,
        "recent_events": recent_events,
    }
