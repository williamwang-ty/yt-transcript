import json
import math
import sys
from pathlib import Path


def _resolve_manifest_path(manifest_ref: str) -> Path:
    path = Path(manifest_ref)
    if path.is_dir():
        return path / "manifest.json"
    return path


def build_chapter_plan(chapters_path: str, manifest_ref: str, output_path: str = "") -> dict:
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

    def map_time(timestamp: float | None) -> tuple[dict, str, str]:
        if timestamp is None:
            return timed_chunks[0], "missing_time", "low"
        for chunk in timed_chunks:
            start_time = chunk["start_time"]
            end_time = chunk["end_time"]
            if start_time <= timestamp < end_time or math.isclose(timestamp, start_time):
                return chunk, "time_contains", "high"
            if timestamp < start_time:
                return chunk, "next_chunk", "medium"
        return timed_chunks[-1], "after_last_chunk", "low"

    def is_untitled(title: str) -> bool:
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
        start_chunk, match_strategy, confidence = map_time(start_time)
        end_chunk = start_chunk
        if end_time is not None:
            end_probe = max(start_time or end_time, end_time - 1e-6)
            end_chunk, _, _ = map_time(end_probe)

        title = str(chapter.get("title", "")).strip()
        if title and is_untitled(title):
            title = ""
        title_en = str(chapter.get("title_en", "")).strip()
        title_zh = str(chapter.get("title_zh", "")).strip()

        if not title_zh and not title_en and title and not is_untitled(title):
            title_zh = title

        if match_strategy != "time_contains":
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


def merge_content(work_dir: str, output_file: str, header_content: str = "") -> dict:
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

    output_lines: list[str] = []
    chapters_inserted = 0
    missing_files: list[str] = []

    if header_content:
        header_content = header_content.strip()
        output_lines.append(header_content)
        if not header_content.endswith("---"):
            output_lines.append("\n---\n")
        else:
            output_lines.append("\n")

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

                output_lines.append("\n")
                for heading in headings:
                    output_lines.append(f"## {heading}\n")
                output_lines.append("\n")
                chapters_inserted += 1

        if processed_path.exists():
            content = processed_path.read_text(encoding="utf-8")
            output_lines.append(content)
            output_lines.append("\n")
        elif status == "done":
            missing_files.append(str(processed_path))
            print(f"Warning: Processed file not found: {processed_path}", file=sys.stderr)

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_content = "".join(output_lines)
    output_path.write_text(final_content, encoding="utf-8")

    return {
        "success": len(missing_files) == 0,
        "output_file": str(output_path),
        "total_lines": final_content.count("\n"),
        "total_chars": len(final_content),
        "chapters_inserted": chapters_inserted,
        "missing_files": missing_files,
    }
