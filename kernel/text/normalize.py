"""The normalized document: raw text plus timed segments in one JSON file, the input of the text stage."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from kernel.config import coerce_float_or_none, parse_int
from kernel.files import atomic_write_text, now_iso
from kernel.text.cleanup.subtitle import normalize_subtitle_segment_text, normalize_subtitle_text

NORMALIZED_DOCUMENT_SCHEMA_VERSION = 1
NORMALIZED_DOCUMENT_FORMAT = "yt_transcript.normalized_document/v1"


def is_youtube_subtitle_source(source_payload: dict) -> bool:
    """Return whether the normalized document source came from YouTube subtitles."""
    if not isinstance(source_payload, dict):
        return False
    source_type = str(source_payload.get("type", "")).strip().lower()
    subtitle_source = str(source_payload.get("subtitle_source", "")).strip().lower()
    return source_type == "youtube" and "subtitle" in subtitle_source


def load_normalized_document(normalized_document_path: str) -> dict:
    """Load normalized document."""
    path = Path(normalized_document_path)
    if not path.exists():
        print(f"Error: File does not exist {normalized_document_path}", file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"Error: JSON parsing failed {error}", file=sys.stderr)
        sys.exit(2)
    except Exception as error:
        print(f"Error: Cannot read file {error}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(payload, dict):
        print("Error: Normalized document must be a JSON object", file=sys.stderr)
        sys.exit(2)

    source_payload = payload.get("source", {}) if isinstance(payload.get("source", {}), dict) else {}
    content = payload.get("content", {}) if isinstance(payload.get("content", {}), dict) else {}
    payload.setdefault("content", content)
    payload["content"]["text"] = _normalize_document_text_for_source(content.get("text", ""), source_payload)

    normalized_segments = []
    for index, raw_segment in enumerate(payload.get("segments", [])):
        if not isinstance(raw_segment, dict):
            continue
        segment_text = _normalize_segment_text_for_source(raw_segment.get("text", ""), source_payload)
        if not segment_text:
            continue
        normalized_segments.append({
            "id": parse_int(raw_segment.get("id"), index),
            "text": segment_text,
            "start_time": coerce_float_or_none(raw_segment.get("start_time")),
            "end_time": coerce_float_or_none(raw_segment.get("end_time")),
            "speaker": raw_segment.get("speaker"),
            "segment_part_index": raw_segment.get("segment_part_index"),
        })
    payload["segments"] = normalized_segments

    if not payload["segments"] and not str(payload.get("content", {}).get("text", "")).strip():
        print("Error: Normalized document does not contain usable text or segments", file=sys.stderr)
        sys.exit(2)

    return payload


def normalize_text_body(text: str) -> str:
    """Normalize text body."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _normalize_document_text_for_source(text: str, source_payload: dict) -> str:
    """Normalize document-level text according to the source family."""
    if is_youtube_subtitle_source(source_payload):
        return normalize_subtitle_text(text)
    return normalize_text_body(text)


def _normalize_segment_text_for_source(text: str, source_payload: dict) -> str:
    """Normalize segment-level text according to the source family."""
    if is_youtube_subtitle_source(source_payload):
        return normalize_subtitle_segment_text(text)
    return " ".join(normalize_text_body(text).split())


def _prefer_text_chunking_for_source(source_payload: dict, workflow_payload: dict) -> bool:
    """Return whether chunking should prefer text despite timed segments."""
    if not is_youtube_subtitle_source(source_payload):
        return False
    mode = str((workflow_payload or {}).get("mode", "")).strip().lower()
    return mode == "chinese"


def build_normalized_document(*, document_id: str, raw_text_path: str, segments_path: str,
                              normalized_document_path: str, source_payload: dict | None = None,
                              workflow_payload: dict | None = None) -> dict:
    """Write the normalized document from a raw text file and/or a segments file.

    `source_payload` says where the text came from (`type` youtube or deepgram,
    `subtitle_source`, title, channel, ...) and `workflow_payload` carries the
    output `mode`; together they decide how the text is cleaned, whether
    chunking prefers text over timed segments, and whether the subtitle
    quality score applies. Timed segments are used when the segments file
    exists, else the raw text. The result also names the raw text and segments files that were actually
    used (`raw_text_path`, `segments_path`).
    """
    source_payload = source_payload if isinstance(source_payload, dict) else {}
    workflow_payload = workflow_payload if isinstance(workflow_payload, dict) else {}

    def failure(message: str) -> dict:
        return {
            "passed": False,
            "warnings": [],
            "hard_failures": [message],
            "normalized_document_path": normalized_document_path,
            "materialized": False,
        }

    if not document_id:
        return failure("Missing document id for normalization")

    raw_text_file = Path(raw_text_path) if raw_text_path else None
    segments_file = Path(segments_path) if segments_path else None
    use_segments = bool(segments_file and segments_file.exists())
    if not use_segments and not (raw_text_file and raw_text_file.exists()):
        return failure(f"Raw text artifact not found: {raw_text_path}")

    prefer_text_chunking = _prefer_text_chunking_for_source(source_payload, workflow_payload)

    normalized_segments = []
    has_timing = False
    source_adapter = "raw_text_file"
    preferred_chunk_source = "text"
    cleanup_diagnostics = {}
    warnings = []

    if use_segments:
        try:
            payload = json.loads(segments_file.read_text(encoding="utf-8"))
        except Exception as e:
            return failure(f"Cannot read segments artifact: {e}")
        if not isinstance(payload, dict) or not isinstance(payload.get("segments", []), list):
            return failure("Segments artifact must be a JSON object with a segments list")
        payload_diagnostics = payload.get("diagnostics", {}) if isinstance(payload.get("diagnostics", {}), dict) else {}
        if is_youtube_subtitle_source(source_payload) or str(payload.get("source", "")).strip().lower() == "vtt":
            cleanup_diagnostics = payload_diagnostics
        for seg in payload.get("segments", []):
            if not isinstance(seg, dict):
                continue
            clean_text = _normalize_segment_text_for_source(seg.get("text", ""), source_payload)
            if not clean_text:
                continue
            start_time = coerce_float_or_none(seg.get("start_time"))
            end_time = coerce_float_or_none(seg.get("end_time"))
            has_timing = has_timing or start_time is not None or end_time is not None
            normalized_segments.append({
                "id": len(normalized_segments),
                "text": clean_text,
                "start_time": start_time,
                "end_time": end_time,
                "speaker": seg.get("speaker"),
            })
        normalized_text = "\n".join(segment["text"] for segment in normalized_segments).strip()
        source_adapter = "segments_json"
        preferred_chunk_source = "text" if prefer_text_chunking else "segments"
        if prefer_text_chunking and raw_text_file and raw_text_file.exists():
            try:
                normalized_text = _normalize_document_text_for_source(
                    raw_text_file.read_text(encoding="utf-8"),
                    source_payload,
                )
            except Exception as e:
                warnings.append(
                    f"Cannot read subtitle raw text artifact, fell back to normalized segments: {e}"
                )
    else:
        try:
            normalized_text = _normalize_document_text_for_source(
                raw_text_file.read_text(encoding="utf-8"),
                source_payload,
            )
        except Exception as e:
            return failure(f"Cannot read raw text artifact: {e}")

    used_raw_text = raw_text_path if raw_text_file and raw_text_file.exists() else ""
    used_segments = segments_path if segments_file and segments_file.exists() else ""
    normalized_doc = {
        "schema_version": NORMALIZED_DOCUMENT_SCHEMA_VERSION,
        "format": NORMALIZED_DOCUMENT_FORMAT,
        "updated_at": now_iso(),
        "document_id": document_id,
        "source_adapter": source_adapter,
        "source": {
            "type": source_payload.get("type", ""),
            "url": source_payload.get("url", ""),
            "title": source_payload.get("title", ""),
            "channel": source_payload.get("channel", ""),
            "upload_date": source_payload.get("upload_date", ""),
            "duration": source_payload.get("duration", ""),
            "source_language": source_payload.get("source_language", ""),
            "subtitle_source": source_payload.get("subtitle_source", ""),
        },
        "workflow": {
            "mode": workflow_payload.get("mode", ""),
        },
        "artifacts": {
            "raw_text": used_raw_text,
            "segments_path": used_segments,
            "normalized_document": normalized_document_path,
        },
        "content": {
            "text": normalized_text,
            "char_count": len(normalized_text),
            "line_count": len(normalized_text.splitlines()) if normalized_text else 0,
            "segment_count": len(normalized_segments),
            "has_timing": has_timing,
            "preferred_chunk_source": preferred_chunk_source,
        },
        "segments": normalized_segments,
    }
    if cleanup_diagnostics:
        normalized_doc["diagnostics"] = {"subtitle_cleanup": cleanup_diagnostics}
    atomic_write_text(Path(normalized_document_path), json.dumps(normalized_doc, ensure_ascii=False, indent=2))
    return {
        "passed": True,
        "warnings": warnings,
        "hard_failures": [],
        "normalized_document_path": normalized_document_path,
        "materialized": True,
        "source_adapter": source_adapter,
        "preferred_chunk_source": preferred_chunk_source,
        "segment_count": len(normalized_segments),
        "has_timing": has_timing,
        "char_count": len(normalized_text),
        "cleanup_diagnostics": cleanup_diagnostics,
        "raw_text_path": used_raw_text,
        "segments_path": used_segments,
    }
