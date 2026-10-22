"""Overlap-trimming helpers for subtitle cues and chunk seams."""

from __future__ import annotations

import re

from .cjk import is_cjk_family_char


PUNCTUATION_CHARS = set(",.;:!?，。！？、；：()[]{}\"'“”‘’")


def _has_meaningful_content(text: str) -> bool:
    """Return whether a candidate overlap contains non-punctuation content."""
    return any(char.isalnum() or is_cjk_family_char(char) for char in str(text or ""))


def _meaningful_cjk_char_count(text: str) -> int:
    """Count non-space, non-punctuation chars in a CJK overlap candidate."""
    return sum(
        1
        for char in str(text or "")
        if not char.isspace() and char not in PUNCTUATION_CHARS
    )


def _is_meaningful_overlap(text: str) -> bool:
    """Return whether an exact overlap candidate is strong enough to trim."""
    candidate = str(text or "").strip()
    if not candidate or not _has_meaningful_content(candidate):
        return False

    if any(is_cjk_family_char(char) for char in candidate):
        return _meaningful_cjk_char_count(candidate) >= 2

    tokens = [token for token in re.split(r"\s+", candidate) if token]
    return len(tokens) >= 2 or len(candidate) >= 8


def find_leading_overlap(previous_text: str, current_text: str) -> str:
    """Find the longest meaningful suffix/prefix overlap between adjacent cues."""
    previous = str(previous_text or "").rstrip()
    current = str(current_text or "").lstrip()
    if not previous or not current:
        return ""

    max_length = min(len(previous), len(current))
    for size in range(max_length, 0, -1):
        candidate = current[:size]
        if previous.endswith(candidate) and _is_meaningful_overlap(candidate):
            return candidate
    return ""


def trim_leading_overlap(previous_text: str, current_text: str) -> tuple[str, dict]:
    """Trim a duplicated leading overlap from the current cue."""
    current = str(current_text or "").strip()
    diagnostics = {
        "overlap_trimmed": False,
        "collapsed_to_empty": False,
        "overlap_chars": 0,
    }
    if not current:
        diagnostics["collapsed_to_empty"] = True
        return "", diagnostics

    overlap = find_leading_overlap(previous_text, current)
    if not overlap:
        return current, diagnostics

    trimmed = current[len(overlap):].lstrip()
    diagnostics["overlap_trimmed"] = True
    diagnostics["collapsed_to_empty"] = not bool(trimmed)
    diagnostics["overlap_chars"] = len(overlap)
    return trimmed, diagnostics

