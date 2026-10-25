"""Deterministic subtitle cleanup and VTT parsing helpers."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .cjk import is_cjk_family_char, normalize_cjk_spacing
from .overlap import trim_leading_overlap


def _normalize_text_body(text: str) -> str:
    """Normalize newlines and trailing whitespace conservatively."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def build_empty_subtitle_diagnostics() -> dict:
    """Return the default diagnostics payload for subtitle cleanup."""
    return {
        "cue_count": 0,
        "empty_cue_count": 0,
        "exact_duplicate_cue_count": 0,
        "overlap_trim_count": 0,
        "overlap_collapsed_cue_count": 0,
        "overlap_trimmed_chars": 0,
        "markup_tag_count": 0,
        "nbsp_entity_count": 0,
        "inserted_ascii_space_count": 0,
        "collapsed_cjk_spacing_count": 0,
        "tightened_punctuation_spacing_count": 0,
        "tightened_bracket_spacing_count": 0,
        "sound_tag_count": 0,
    }


# Captions mark sounds in square brackets, `[Music]`, `[Applause]`, `[音乐]`
# (YouTube's speech recognition writes nothing else in brackets), and sung
# lines with music notes. Neither is speech, so neither goes into the text.
SOUND_TAG = re.compile(r"\[[^\[\]\n]{1,40}\]")
MUSIC_NOTES = re.compile(r"[♪♫♬♩]")


def _merge_diagnostics(target: dict, update: dict) -> None:
    """Accumulate numeric cleanup diagnostics in-place."""
    if not isinstance(target, dict) or not isinstance(update, dict):
        return
    for key, value in update.items():
        if isinstance(value, int):
            target[key] = int(target.get(key, 0) or 0) + value


def strip_subtitle_markup(text: str) -> tuple[str, dict]:
    """Strip subtitle markup (VTT cue tags) and caption sound tags such as `[Music]` and music notes."""
    source = str(text or "")
    cleaned, tag_count = re.subn(r"<[^>]+>", "", source)
    nbsp_count = cleaned.count("&nbsp;")
    cleaned = cleaned.replace("&nbsp;", " ")
    sound_count = 0
    if SOUND_TAG.search(cleaned) or MUSIC_NOTES.search(cleaned):
        lines = []
        for line in cleaned.split("\n"):
            stripped, count = SOUND_TAG.subn(" ", line)
            stripped = MUSIC_NOTES.sub(" ", stripped)
            sound_count += count
            # A line that held only a tag goes, rather than becoming a paragraph break.
            if stripped.strip() or not line.strip():
                lines.append(stripped)
        cleaned = "\n".join(lines)
    return cleaned, {
        "markup_tag_count": tag_count,
        "nbsp_entity_count": nbsp_count,
        "sound_tag_count": sound_count,
    }


def subtitle_join_needs_space(left: str, right: str) -> bool:
    """Return whether two subtitle fragments need an inserted ASCII space."""
    left_text = str(left or "").rstrip()
    right_text = str(right or "").lstrip()
    if not left_text or not right_text:
        return False

    previous = left_text[-1]
    following = right_text[0]

    if previous in "([{\"'“‘「『《【":
        return False
    if following in ",.;:!?，。！？、；：)]}\"'”’」』）》】":
        return False
    if is_cjk_family_char(previous) or is_cjk_family_char(following):
        return False
    return True


def join_subtitle_fragments(fragments: list[str]) -> tuple[str, dict]:
    """Join subtitle fragments without inventing spaces inside CJK text."""
    parts = []
    diagnostics = {"inserted_ascii_space_count": 0}
    for fragment in fragments:
        cleaned, strip_diag = strip_subtitle_markup(fragment)
        _merge_diagnostics(diagnostics, strip_diag)
        cleaned = cleaned.strip()
        if cleaned:
            parts.append(cleaned)

    if not parts:
        return "", diagnostics

    joined = parts[0]
    for fragment in parts[1:]:
        if subtitle_join_needs_space(joined, fragment):
            joined = f"{joined} {fragment}"
            diagnostics["inserted_ascii_space_count"] += 1
        else:
            joined = f"{joined}{fragment}"
    return joined.strip(), diagnostics


def normalize_subtitle_text_with_report(text: str) -> tuple[str, dict]:
    """Apply conservative cleanup tailored to subtitle-derived text."""
    normalized, strip_diag = strip_subtitle_markup(text)
    diagnostics = build_empty_subtitle_diagnostics()
    _merge_diagnostics(diagnostics, strip_diag)
    normalized = _normalize_text_body(normalized)
    if not normalized:
        return "", diagnostics

    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized, cjk_diag = normalize_cjk_spacing(normalized)
    _merge_diagnostics(diagnostics, cjk_diag)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip(), diagnostics


def normalize_subtitle_text(text: str) -> str:
    """Normalize subtitle-derived multiline text."""
    normalized, _ = normalize_subtitle_text_with_report(text)
    return normalized


def normalize_subtitle_segment_text_with_report(text: str) -> tuple[str, dict]:
    """Normalize a single subtitle cue into a cue-sized text fragment."""
    normalized, diagnostics = normalize_subtitle_text_with_report(text)
    if not normalized:
        return "", diagnostics

    joined, join_diag = join_subtitle_fragments(normalized.splitlines())
    _merge_diagnostics(diagnostics, join_diag)
    return joined, diagnostics


def normalize_subtitle_segment_text(text: str) -> str:
    """Normalize a single subtitle cue into a cue-sized text fragment."""
    normalized, _ = normalize_subtitle_segment_text_with_report(text)
    return normalized


def parse_vtt_timestamp(value: str) -> float | None:
    """Parse a VTT timestamp into seconds."""
    token = str(value or "").strip().replace(",", ".")
    if not token:
        return None

    parts = token.split(":")
    if len(parts) == 3:
        hours_str, minutes_str, seconds_str = parts
    elif len(parts) == 2:
        hours_str = "0"
        minutes_str, seconds_str = parts
    else:
        return None

    try:
        hours = int(hours_str)
        minutes = int(minutes_str)
        seconds = float(seconds_str)
    except ValueError:
        return None
    return round(hours * 3600 + minutes * 60 + seconds, 3)


def parse_vtt_time_range(line: str) -> tuple[float | None, float | None]:
    """Parse a VTT cue timing line into start/end seconds."""
    if "-->" not in str(line or ""):
        return None, None

    start_raw, end_raw = str(line).split("-->", 1)
    start_token = start_raw.strip().split()[0] if start_raw.strip() else ""
    end_token = end_raw.strip().split()[0] if end_raw.strip() else ""
    return parse_vtt_timestamp(start_token), parse_vtt_timestamp(end_token)


def parse_vtt_payload(content: str, *, language: str = "", vtt_path: str = "") -> dict:
    """Parse VTT content into aligned subtitle segments plus cleanup diagnostics."""
    segments = []
    diagnostics = build_empty_subtitle_diagnostics()
    header_language = ""
    in_note = False
    cue_start = None
    cue_end = None
    cue_lines = []

    def flush_cue() -> None:
        """Flush the current cue into the segment list."""
        nonlocal cue_start, cue_end, cue_lines
        if cue_start is None or cue_end is None:
            cue_start = None
            cue_end = None
            cue_lines = []
            return

        diagnostics["cue_count"] += 1
        clean, cue_diag = normalize_subtitle_segment_text_with_report("\n".join(cue_lines))
        _merge_diagnostics(diagnostics, cue_diag)
        if not clean:
            diagnostics["empty_cue_count"] += 1
            cue_start = None
            cue_end = None
            cue_lines = []
            return

        if segments:
            previous_segment = segments[-1]
            previous_end = previous_segment.get("end_time")
            if previous_segment["text"] == clean:
                diagnostics["exact_duplicate_cue_count"] += 1
                if previous_end is None or cue_end > previous_end:
                    previous_segment["end_time"] = cue_end
                cue_start = None
                cue_end = None
                cue_lines = []
                return

            trimmed, overlap_diag = trim_leading_overlap(previous_segment["text"], clean)
            if overlap_diag.get("overlap_trimmed", False):
                diagnostics["overlap_trim_count"] += 1
                diagnostics["overlap_trimmed_chars"] += int(overlap_diag.get("overlap_chars", 0) or 0)
                if overlap_diag.get("collapsed_to_empty", False):
                    diagnostics["overlap_collapsed_cue_count"] += 1
                    if previous_end is None or cue_end > previous_end:
                        previous_segment["end_time"] = cue_end
                    cue_start = None
                    cue_end = None
                    cue_lines = []
                    return
                clean = trimmed

        segments.append({
            "id": len(segments),
            "text": clean,
            "start_time": cue_start,
            "end_time": cue_end,
            "speaker": None,
        })
        cue_start = None
        cue_end = None
        cue_lines = []

    for raw_line in str(content or "").splitlines():
        line = raw_line.strip("\ufeff")
        stripped = line.strip()

        if stripped.startswith("Language:") and not language:
            header_language = stripped.split(":", 1)[1].strip()

        if in_note:
            if not stripped:
                in_note = False
            continue

        if stripped.startswith("NOTE"):
            in_note = True
            continue

        if stripped.startswith("WEBVTT") or stripped.startswith("Kind:") or stripped.startswith("Style:"):
            continue
        if stripped.startswith("STYLE") or stripped.startswith("REGION"):
            continue

        if "-->" in stripped:
            flush_cue()
            cue_start, cue_end = parse_vtt_time_range(stripped)
            cue_lines = []
            continue

        if not stripped:
            flush_cue()
            continue

        if cue_start is None and cue_end is None:
            continue

        cue_lines.append(stripped)

    flush_cue()

    if not segments:
        raise ValueError("No usable VTT segments found")

    return {
        "source": "vtt",
        "language": language or header_language,
        "vtt_path": str(Path(vtt_path).absolute()) if vtt_path else "",
        "segment_count": len(segments),
        "segments": segments,
        "diagnostics": diagnostics,
    }


def _parse_vtt_payload_or_exit(content: str, *, language: str = "", vtt_path: str = "") -> dict:
    """Parse VTT content into aligned subtitle segments."""
    try:
        return parse_vtt_payload(content, language=language, vtt_path=vtt_path)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(2)


def parse_vtt(vtt_path: str) -> str:
    """
    Parse VTT subtitle file, extract plain text

    Processing:
    - Remove VTT header (WEBVTT, Kind:, Language:)
    - Remove timestamp lines (00:00:00.000 --> 00:00:05.000)
    - Remove VTT tags (<c>, </c>, <00:00:01.000>, etc.)
    - Remove cue numbers (pure digit lines)
    - Remove consecutive duplicate lines (common in auto-captions)
    """
    path = Path(vtt_path)
    if not path.exists():
        print(f"Error: File does not exist {vtt_path}", file=sys.stderr)
        sys.exit(1)

    parsed = parse_vtt_segments(vtt_path)
    body = "\n".join(segment.get("text", "") for segment in parsed.get("segments", []) if segment.get("text"))
    return normalize_subtitle_text(body)


def parse_vtt_segments(vtt_path: str, *, language: str = "") -> dict:
    """Parse a VTT file and return aligned segments with timing metadata.

    Output schema (the segments file the normalizer reads):

    {
      "source": "vtt",
      "language": "en" | "zh" | "..." | "",
      "segments": [{"id": 0, "text": "...", "start_time": 0.0, "end_time": 1.2}, ...]
    }
    """
    path = Path(vtt_path)
    if not path.exists():
        print(f"Error: File does not exist {vtt_path}", file=sys.stderr)
        sys.exit(1)

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)
    return _parse_vtt_payload_or_exit(content, language=language, vtt_path=str(path))
