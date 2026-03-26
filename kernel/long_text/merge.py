"""Deterministic merge and chapter-plan helpers for processed chunk output."""

import json
import math
import sys
from pathlib import Path

from kernel.text_cleanup import post_merge as kernel_post_merge_cleanup


def _resolve_manifest_path(manifest_ref: str) -> Path:
    """Resolve a manifest reference to an absolute manifest path."""
    path = Path(manifest_ref)
    if path.is_dir():
        return path / "manifest.json"
    return path


def build_chapter_plan(chapters_path: str, manifest_ref: str, output_path: str = "") -> dict:
    """Map source chapters onto chunk boundaries for deterministic final assembly."""
    import yt_transcript_utils as utils

    chapters_file = Path(chapters_path)
    if not chapters_file.exists():
        print(f"Error: File does not exist {chapters_path}", file=sys.stderr)
        sys.exit(1)

    manifest_path = _resolve_manifest_path(manifest_ref)
    if not manifest_path.exists():
        print(f"Error: manifest.json not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        chapters_data = json.loads(chapters_file.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"Error: JSON parsing failed {error}", file=sys.stderr)
        sys.exit(2)
    except Exception as error:
        print(f"Error: Cannot read file {error}", file=sys.stderr)
        sys.exit(2)

    if isinstance(chapters_data, dict):
        chapters = chapters_data.get("chapters", [])
    elif isinstance(chapters_data, list):
        chapters = chapters_data
    else:
        print("Error: Chapters JSON must be an array or an object with 'chapters'", file=sys.stderr)
        sys.exit(2)

    timed_chunks = []
    missing_timing = 0
    for chunk in manifest.get("chunks", []):
        status = str(chunk.get("status", "")).strip().lower()
        if status == utils.SUPERSEDED_CHUNK_STATUS:
            continue
        start_time = utils._coerce_float_or_none(chunk.get("start_time"))
        end_time = utils._coerce_float_or_none(chunk.get("end_time"))
        if start_time is None or end_time is None:
            missing_timing += 1
            continue
        timed_chunks.append({**chunk, "start_time": start_time, "end_time": end_time})

    if not timed_chunks:
        print("Error: Manifest does not contain timed chunks; run chunk-segments first", file=sys.stderr)
        sys.exit(2)

    timed_chunks.sort(key=lambda item: int(item.get("id", 0)))

    def map_time(timestamp: float | None) -> tuple[dict, str, str, dict]:
        """Map a timestamp onto the chunk that first covers that source position."""
        return utils._map_timestamp_to_timed_item(
            timestamp,
            timed_chunks,
            next_strategy="next_chunk",
            after_last_strategy="after_last_chunk",
        )

    def is_untitled(title: str) -> bool:
        """Return whether a chapter title should be treated as effectively empty."""
        if not title:
            return True
        lowered = title.strip().lower()
        return lowered.startswith("<untitled") or "untitled chapter" in lowered

    warnings = []
    plan_entries = []

    if missing_timing:
        warnings.append(f"Skipped {missing_timing} chunks without timing metadata")

    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue

        start_time = utils._coerce_float_or_none(chapter.get("start_time"))
        end_time = utils._coerce_float_or_none(chapter.get("end_time"))
        start_chunk, match_strategy, confidence, diagnostics = map_time(start_time)
        end_chunk = start_chunk
        end_diagnostics = diagnostics
        if end_time is not None:
            end_probe = max(start_time or end_time, end_time - 1e-6)
            end_chunk, _, _, end_diagnostics = map_time(end_probe)

        title = str(chapter.get("title", "")).strip()
        if title and is_untitled(title):
            title = ""
        title_en = str(chapter.get("title_en", "")).strip()
        title_zh = str(chapter.get("title_zh", "")).strip()

        if not title_zh and not title_en and title and not is_untitled(title):
            title_zh = title

        if match_strategy not in {"time_contains", "near_next_start"}:
            warnings.append(f"Chapter {index} used fallback strategy '{match_strategy}'")

        plan_entries.append({
            "chapter_index": index,
            "title": title,
            "title_en": title_en,
            "title_zh": title_zh,
            "start_time": start_time,
            "end_time": end_time,
            "start_chunk": int(start_chunk.get("id", 0)),
            "end_chunk": int(end_chunk.get("id", start_chunk.get("id", 0))),
            "anchor_segment_id": start_chunk.get("source_segment_start"),
            "match_strategy": match_strategy,
            "confidence": confidence,
            "mapping_diagnostics": diagnostics,
            "end_mapping_diagnostics": end_diagnostics,
        })

    output_file = str(output_path or (manifest_path.parent / "chapter_plan.json"))
    Path(output_file).write_text(json.dumps(plan_entries, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "success": True,
        "output_file": output_file,
        "chapter_count": len(chapters),
        "mapped_count": len(plan_entries),
        "warnings": warnings,
    }


def _normalize_header_content(header_content: str) -> str:
    """Normalize optional prefixed header content without touching merge-body cleanup."""
    normalized = str(header_content or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    if not normalized:
        return ""
    if normalized.endswith("---"):
        return normalized
    return f"{normalized}\n\n---"


def merge_content(work_dir: str, output_file: str, header_content: str = "") -> dict:
    """Merge processed chunk outputs into the final Markdown document."""
    import yt_transcript_utils as utils

    work_path = Path(work_dir)
    manifest_path = work_path / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    chapter_plan_path = work_path / "chapter_plan.json"
    chapter_starts: dict[int, list[dict]] = {}
    if chapter_plan_path.exists():
        try:
            chapter_plan = json.loads(chapter_plan_path.read_text(encoding="utf-8"))
            if isinstance(chapter_plan, list):
                for chapter in chapter_plan:
                    if not isinstance(chapter, dict):
                        continue
                    start_chunk = chapter.get("start_chunk")
                    if start_chunk is None:
                        continue
                    try:
                        start_chunk_int = int(start_chunk)
                    except (ValueError, TypeError):
                        print(f"Warning: Invalid start_chunk value: {start_chunk}", file=sys.stderr)
                        continue
                    chapter_starts.setdefault(start_chunk_int, []).append({
                        "title": str(chapter.get("title", "")),
                        "title_en": str(chapter.get("title_en", "")),
                        "title_zh": str(chapter.get("title_zh", "")),
                    })
        except (json.JSONDecodeError, KeyError) as error:
            print(f"Warning: Could not parse chapter_plan.json: {error}", file=sys.stderr)

    header_prefix = _normalize_header_content(header_content)
    body_lines: list[str] = []
    chapters_inserted = 0
    missing_files: list[str] = []

    for chunk_info in manifest["chunks"]:
        chunk_id = chunk_info["id"]
        processed_path = work_path / chunk_info["processed_path"]
        status = str(chunk_info.get("status", "")).strip().lower()

        if status == utils.SUPERSEDED_CHUNK_STATUS:
            continue

        if chunk_id in chapter_starts:
            for chapter in chapter_starts[chunk_id]:
                title = str(chapter.get("title", "")).strip()
                title_en = str(chapter.get("title_en", "")).strip()
                title_zh = str(chapter.get("title_zh", "")).strip()

                headings: list[str] = []
                if title_en:
                    headings.append(title_en)
                if title_zh and title_zh not in headings:
                    headings.append(title_zh)
                if not headings and title:
                    headings.append(title)
                if not headings:
                    continue

                body_lines.append("\n")
                for heading in headings:
                    body_lines.append(f"## {heading}\n")
                body_lines.append("\n")
                chapters_inserted += 1

        if processed_path.exists():
            content = processed_path.read_text(encoding="utf-8")
            body_lines.append(content)
            body_lines.append(f"\n\n{kernel_post_merge_cleanup.CHUNK_SEAM_MARKER}\n\n")
        elif status == "done":
            missing_files.append(str(processed_path))
            print(f"Warning: Processed file not found: {processed_path}", file=sys.stderr)

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    body_content = "".join(body_lines)
    cleaned_body, cleanup_diagnostics = kernel_post_merge_cleanup.post_merge_cleanup(body_content)
    cleanup_applied = cleaned_body != body_content

    sections = []
    if header_prefix:
        sections.append(header_prefix)
    if cleaned_body:
        sections.append(cleaned_body)
    final_content = "\n\n".join(section.strip() for section in sections if section.strip()).strip()
    output_path.write_text(final_content, encoding="utf-8")

    return {
        "success": len(missing_files) == 0,
        "output_file": str(output_path),
        "total_lines": final_content.count("\n"),
        "total_chars": len(final_content),
        "chapters_inserted": chapters_inserted,
        "missing_files": missing_files,
        "post_merge_cleanup_applied": cleanup_applied,
        "cleanup_diagnostics": cleanup_diagnostics,
    }
