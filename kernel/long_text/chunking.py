"""Public chunking commands for text, segments, and normalized documents."""

import sys
from pathlib import Path


def chunk_text(input_path: str, output_dir: str, chunk_size: int = 0,
               prompt_name: str = "", config_path: str = None) -> dict:
    """Chunk a plain-text source file into a long-text work directory."""
    import yt_transcript_utils as utils

    path = Path(input_path)
    if not path.exists():
        print(f"Error: File does not exist {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as error:
        print(f"Error: Cannot read file {error}", file=sys.stderr)
        sys.exit(2)

    return utils._chunk_text_payload(
        text,
        str(path.absolute()),
        output_dir,
        chunk_size,
        prompt_name,
        config_path,
        driver="chunk-text",
        source_kind="text",
    )


def chunk_segments(segments_path: str, output_dir: str, chunk_size: int = 0,
                   prompt_name: str = "", config_path: str = None,
                   chapters_path: str = "") -> dict:
    """Chunk a timed-segment document into a long-text work directory."""
    import yt_transcript_utils as utils

    metadata, segments = utils._load_segment_document(segments_path)
    return utils._chunk_segments_payload(
        metadata,
        segments,
        segments_path,
        output_dir,
        chunk_size,
        prompt_name,
        config_path,
        chapters_path=chapters_path,
        driver="chunk-segments",
        source_kind="segments",
        source_segments_file=str(Path(segments_path).absolute()),
    )


def chunk_document(normalized_document_path: str, output_dir: str, chunk_size: int = 0,
                   prompt_name: str = "", config_path: str = None,
                   chapters_path: str = "", prefer: str = "auto") -> dict:
    """Chunk a normalized document, preferring text or segments as configured."""
    import yt_transcript_utils as utils

    payload = utils._load_normalized_document(normalized_document_path)
    preference = str(prefer or "auto").strip().lower()
    if preference not in {"auto", "segments", "text"}:
        print(f"Error: Unsupported chunk-document preference: {prefer}", file=sys.stderr)
        sys.exit(2)

    content = payload.get("content", {}) if isinstance(payload.get("content", {}), dict) else {}
    preferred_source = str(content.get("preferred_chunk_source", "")).strip().lower()
    segments = payload.get("segments", []) if isinstance(payload.get("segments", []), list) else []
    has_segments = bool(segments)
    source_adapter = str(payload.get("source_adapter", "")).strip()
    artifacts = payload.get("artifacts", {}) if isinstance(payload.get("artifacts", {}), dict) else {}
    normalized_path = str(Path(normalized_document_path).absolute())

    if preference == "segments":
        use_segments = has_segments
        if not use_segments:
            print("Error: Normalized document does not contain segments", file=sys.stderr)
            sys.exit(2)
    elif preference == "text":
        use_segments = False
    else:
        use_segments = preferred_source == "segments" and has_segments

    if use_segments:
        metadata = {
            "source": source_adapter,
            "document_id": payload.get("document_id", ""),
        }
        result = utils._chunk_segments_payload(
            metadata,
            segments,
            normalized_path,
            output_dir,
            chunk_size,
            prompt_name,
            config_path,
            chapters_path=chapters_path,
            driver="chunk-document",
            source_kind="segments",
            normalized_document_path=normalized_path,
            source_adapter=source_adapter,
            source_segments_file=str(artifacts.get("segments_path", "") or normalized_path),
        )
    else:
        text_body = str(content.get("text", "")).strip()
        if not text_body:
            print("Error: Normalized document does not contain usable text", file=sys.stderr)
            sys.exit(2)
        result = utils._chunk_text_payload(
            text_body,
            normalized_path,
            output_dir,
            chunk_size,
            prompt_name,
            config_path,
            driver="chunk-document",
            source_kind="text",
            normalized_document_path=normalized_path,
            source_adapter=source_adapter,
        )

    result["preferred_source_kind"] = preferred_source or ("segments" if has_segments else "text")
    return result
