"""Cut the normalized document into the raw chunks the window executor reads.

The text stage of `run` calls `chunk_document` once, before the first window
run. A chunk boundary stays a unit boundary in the window run (see
`window.build_source_sentences`): on the text path chunks end at sentence
ends, on the segment path at subtitle cues and before every YouTube chapter
start. `manifest.json` in the work dir lists the chunk files, the normalized
document (for cue timings) and the chapters file (for outline hints).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from kernel.config import (
    DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
    MAX_CHUNK_HARD_CAP_MULTIPLIER,
    get_task_chunk_target,
    parse_float_range,
)
from kernel.files import atomic_write_text
from kernel.text.measure import estimate_tokens, force_split_text_by_tokens, split_sentences
from kernel.text.normalize import load_normalized_document

MANIFEST_FILE = "manifest.json"
CHUNK_SEPARATOR = "\n\n"
DEFAULT_CHAPTER_BOUNDARY_TOLERANCE_SEC = 0.35


def chunk_document(normalized_document_path: str, output_dir: str, prompt_name: str, config: dict,
                   chapters_path: str = "") -> dict:
    """Write `chunk_NNN.txt` files sized for `prompt_name`, and the manifest listing them.

    Timed segments are used when the normalized document prefers them;
    otherwise its text is cut at sentence ends.
    """
    payload = load_normalized_document(normalized_document_path)
    content = payload.get("content", {}) if isinstance(payload.get("content", {}), dict) else {}
    segments = payload.get("segments", []) if isinstance(payload.get("segments", []), list) else []
    use_segments = bool(segments) and str(content.get("preferred_chunk_source", "")).strip().lower() == "segments"

    target = get_task_chunk_target(prompt_name, config)
    multiplier = parse_float_range(config.get("chunk_hard_cap_multiplier"), DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
                                   1.0, MAX_CHUNK_HARD_CAP_MULTIPLIER)
    hard_cap = max(target, int(math.ceil(target * multiplier)))

    if use_segments:
        chunks, warnings = _chunk_segments(segments, target, hard_cap, chapters_path)
    else:
        text = str(content.get("text", "")).strip()
        if not text:
            print("Error: Normalized document does not contain usable text", file=sys.stderr)
            sys.exit(2)
        chunks, warnings = _chunk_sentences(split_sentences(text), target, hard_cap)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, chunk in enumerate(chunks):
        file_name = f"chunk_{index:03d}.txt"
        atomic_write_text(out_dir / file_name, chunk)
        entries.append({"id": index, "raw_path": file_name})
    manifest = {
        "source_kind": "segments" if use_segments else "text",
        "normalized_document_path": str(Path(normalized_document_path).absolute()),
        "chapters_path": str(Path(chapters_path).absolute()) if chapters_path else "",
        "target_tokens": target,
        "hard_cap_tokens": hard_cap,
        "chunks": entries,
    }
    atomic_write_text(out_dir / MANIFEST_FILE, json.dumps(manifest, ensure_ascii=False, indent=2))
    for warning in warnings:
        print(f"⚠️ {warning}", file=sys.stderr)
    return {"total_chunks": len(chunks), "warnings": warnings, **manifest}


def _chunk_sentences(sentences: list[str], target: int, hard_cap: int) -> tuple[list[str], list[str]]:
    """Pack sentences into chunks of about `target` tokens; a sentence over `hard_cap` is cut."""
    chunks = []
    current = []
    current_size = 0
    warnings = []
    separator_size = estimate_tokens(CHUNK_SEPARATOR)

    for index, sentence in enumerate(sentences):
        pieces = [sentence]
        sentence_size = estimate_tokens(sentence)
        if sentence_size > hard_cap:
            pieces = force_split_text_by_tokens(sentence, hard_cap)
            warnings.append(f"Sentence {index} exceeds chunk_size ({sentence_size} > {hard_cap}), "
                            f"split into {len(pieces)} fixed-width segment(s)")
        for piece in pieces:
            piece_size = estimate_tokens(piece)
            candidate = current_size + piece_size + (separator_size if current else 0)
            if current and (candidate > target or candidate > hard_cap):
                chunks.append(CHUNK_SEPARATOR.join(current))
                current = [piece]
                current_size = piece_size
            else:
                current.append(piece)
                current_size = candidate

    if current:
        chunks.append(CHUNK_SEPARATOR.join(current))
    return chunks, warnings


def _chunk_segments(segments: list[dict], target: int, hard_cap: int,
                    chapters_path: str = "") -> tuple[list[str], list[str]]:
    """Pack timed segments into chunks, starting a new chunk at every chapter start."""
    prepared = []
    warnings = []
    for index, segment in enumerate(segments):
        parts, split_warnings = _split_timed_segment(segment, hard_cap, index)
        prepared.extend(parts)
        warnings.extend(split_warnings)

    break_ids: set[int] = set()
    if chapters_path:
        break_ids, chapter_warnings = _chapter_break_ids(_load_chapters(chapters_path), segments)
        warnings.extend(chapter_warnings)

    specs = []
    current = []
    current_size = 0
    separator_size = estimate_tokens(CHUNK_SEPARATOR)

    def finalize(parts: list[dict]) -> None:
        if not parts:
            return
        specs.append({
            "content": CHUNK_SEPARATOR.join(part["text"] for part in parts if part.get("text")),
            "segment_ids": [part.get("id") for part in parts if part.get("id") is not None],
            "starts_with_chapter_break": _starts_chapter(parts[0], break_ids),
        })

    for part in prepared:
        part_size = estimate_tokens(part["text"])
        if current and _starts_chapter(part, break_ids):
            finalize(current)
            current = [part]
            current_size = part_size
            continue
        candidate = current_size + part_size + (separator_size if current else 0)
        if current and (candidate > target or candidate > hard_cap):
            finalize(current)
            current = [part]
            current_size = part_size
        else:
            current.append(part)
            current_size = candidate
    finalize(current)

    specs, merge_warnings = _coalesce_small_chunks(specs, target, hard_cap)
    warnings.extend(merge_warnings)
    return [spec["content"] for spec in specs], warnings


def _starts_chapter(part: dict, break_ids: set[int]) -> bool:
    part_index = part.get("segment_part_index")
    return part.get("id") is not None and (part_index is None or int(part_index) == 0) and \
        int(part["id"]) in break_ids


def _split_timed_segment(segment: dict, max_tokens: int, index: int) -> tuple[list[dict], list[str]]:
    """Cut a segment over `max_tokens` into pieces; timings are spread by character share."""
    text = segment["text"]
    size = estimate_tokens(text)
    if size <= max_tokens:
        return [segment], []

    pieces = force_split_text_by_tokens(text, max_tokens)
    warnings = [f"Segment {index} exceeds chunk_size ({size} > {max_tokens}), "
                f"split into {len(pieces)} fixed-width segment(s)"]
    start_time = segment.get("start_time")
    end_time = segment.get("end_time")
    duration = None if start_time is None or end_time is None else max(0.0, end_time - start_time)
    total_chars = sum(len(piece) for piece in pieces) or 1
    offset_chars = 0
    parts = []
    for part_index, piece in enumerate(pieces):
        part_start, part_end = start_time, end_time
        if duration is not None:
            part_start = start_time + (offset_chars / total_chars) * duration
            offset_chars += len(piece)
            part_end = start_time + (offset_chars / total_chars) * duration
        parts.append({
            **segment,
            "text": piece,
            "start_time": None if part_start is None else round(part_start, 3),
            "end_time": None if part_end is None else round(part_end, 3),
            "segment_part_index": part_index,
        })
    return parts, warnings


def _load_chapters(chapters_path: str) -> list[dict]:
    try:
        data = json.loads(Path(chapters_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    chapters = data.get("chapters", []) if isinstance(data, dict) else data
    return [chapter for chapter in chapters if isinstance(chapter, dict)] if isinstance(chapters, list) else []


def _chapter_break_ids(chapters: list[dict], segments: list[dict]) -> tuple[set[int], list[str]]:
    """Ids of the segments each chapter start falls into (or just before)."""
    timed = [segment for segment in segments
             if segment.get("start_time") is not None and segment.get("end_time") is not None]
    if not timed:
        return set(), ["No timed segments found; cannot apply chapter-aware chunking"]

    durations = [segment["end_time"] - segment["start_time"] for segment in timed
                 if segment["end_time"] > segment["start_time"]]
    tolerance = max(0.05, min(DEFAULT_CHAPTER_BOUNDARY_TOLERANCE_SEC,
                              min(durations, default=DEFAULT_CHAPTER_BOUNDARY_TOLERANCE_SEC) * 0.2))
    warnings = []
    break_ids = set()
    for index, chapter in enumerate(chapters):
        try:
            start = float(chapter.get("start_time"))
        except (TypeError, ValueError):
            continue
        segment, tolerated = _segment_at(start, timed, tolerance)
        break_ids.add(int(segment.get("id", 0)))
        if not tolerated:
            warnings.append(f"Chapter {index} did not start inside a timed segment; using the next one")
    return break_ids, warnings


def _segment_at(timestamp: float, timed: list[dict], tolerance: float) -> tuple[dict, bool]:
    """The segment holding `timestamp` (or starting within `tolerance` after it), else the next one.

    The flag is False when the time only fell between or after the segments.
    """
    for index, segment in enumerate(timed):
        start = float(segment["start_time"])
        end = float(segment["end_time"])
        following = timed[index + 1] if index + 1 < len(timed) else None
        if following is not None and 0.0 <= float(following["start_time"]) - timestamp <= tolerance:
            return following, True
        if start <= timestamp < end or math.isclose(timestamp, start, abs_tol=tolerance / 4):
            return segment, True
        if timestamp < start:
            return segment, False
    return timed[-1], False


def _coalesce_small_chunks(specs: list[dict], target: int, hard_cap: int) -> tuple[list[dict], list[str]]:
    """Merge tiny chunks into a neighbor when the result stays within the hard cap."""
    if len(specs) < 3 or target <= 0:
        return specs, []
    minimum = max(60, min(120, max(1, target // 6)))
    merged = list(specs)
    warnings: list[str] = []
    merged_count = 0
    slack = max(10, minimum // 2)

    def joined(left: dict, right: dict) -> str:
        return CHUNK_SEPARATOR.join(part for part in (left["content"], right["content"]) if part)

    index = 0
    while index < len(merged):
        if len(merged) < 2:
            break
        current = merged[index]
        size = estimate_tokens(current["content"])
        if size >= minimum or (len(set(current["segment_ids"])) > 1 and size > max(20, minimum // 2)):
            index += 1
            continue

        chapter_start = current["starts_with_chapter_break"]
        candidates = []
        if index + 1 < len(merged):
            total = estimate_tokens(joined(current, merged[index + 1]))
            candidates.append(("next", total, total <= hard_cap, 0 if chapter_start else 1))
        if index > 0 and not chapter_start:
            total = estimate_tokens(joined(merged[index - 1], current))
            candidates.append(("prev", total, total <= hard_cap, 0))
        if not candidates:
            index += 1
            continue

        candidates.sort(key=lambda item: (0 if item[2] else 1, item[3], item[1]))
        direction, combined, within_cap, _ = candidates[0]
        if not within_cap and combined > hard_cap + slack:
            warnings.append(f"Chunk {index} remains below the preferred floor ({size} tokens); "
                            "neighbor merge would exceed hard cap")
            index += 1
            continue

        left, right = (merged[index - 1], current) if direction == "prev" else (current, merged[index + 1])
        combined_spec = {
            "content": joined(left, right),
            "segment_ids": left["segment_ids"] + right["segment_ids"],
            "starts_with_chapter_break": left["starts_with_chapter_break"],
        }
        if direction == "prev":
            merged[index - 1] = combined_spec
            del merged[index]
            index = max(0, index - 1)
        else:
            merged[index] = combined_spec
            del merged[index + 1]
        merged_count += 1

    if merged_count:
        warnings.append(f"Merged {merged_count} undersized chunk fragment(s) below the preferred floor of "
                        f"{minimum} tokens")
    remaining = sum(1 for spec in merged if estimate_tokens(spec["content"]) < minimum)
    if remaining:
        warnings.append(f"{remaining} chunk(s) remain below the preferred floor of {minimum} tokens")
    return merged, warnings
