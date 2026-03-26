"""Deterministic cleanup for merged long-text markdown output."""

from __future__ import annotations

import re

from .cjk import is_cjk_family_char
from .overlap import find_leading_overlap


SENTENCE_TERMINATORS = "。！？!?；;"
SOFT_CONTINUATION_ENDINGS = "，、；：,:([{（【《「『-"
LIST_PREFIX_RE = re.compile(r"^(?:[-*+]\s|\d+\.\s)")
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?")
CHUNK_SEAM_MARKER = "<!-- yt-transcript-chunk-seam -->"


def build_empty_post_merge_diagnostics() -> dict:
    """Return the default diagnostics payload for post-merge cleanup."""
    return {
        "duplicate_body_blocks_removed": 0,
        "seam_overlap_trim_count": 0,
        "seam_overlap_trimmed_chars": 0,
        "short_paragraph_merge_count": 0,
        "duplicate_heading_block_count": 0,
        "heading_line_dedup_count": 0,
        "blank_line_groups_collapsed": 0,
    }


def _normalize_text_body(text: str) -> str:
    """Normalize newlines and trailing whitespace conservatively."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return normalized.strip()


def _split_blocks(text: str) -> list[str]:
    """Split markdown-ish text into non-empty blocks."""
    normalized = _normalize_text_body(text)
    if not normalized:
        return []
    return [block.strip() for block in re.split(r"\n\s*\n", normalized) if block.strip()]


def _block_type(block: str) -> str:
    """Classify a block into a small set of markdown-safe categories."""
    lines = [line.strip() for line in str(block or "").splitlines() if line.strip()]
    if not lines:
        return "empty"
    if len(lines) == 1 and lines[0] == CHUNK_SEAM_MARKER:
        return "seam"
    if len(lines) == 1 and re.fullmatch(r"-{3,}", lines[0]):
        return "hr"
    if lines[0].startswith(("```", "~~~")):
        return "code"
    if all(line.startswith("#") for line in lines):
        return "heading"
    if all(line.startswith(">") for line in lines):
        return "quote"
    if LIST_PREFIX_RE.match(lines[0]):
        return "list"
    return "body"


def _canonical_block_text(block: str) -> str:
    """Build a whitespace-stable representation for block comparisons."""
    return re.sub(r"\s+", " ", str(block or "").strip())


def _meaningful_char_count(text: str) -> int:
    """Count non-space, non-punctuation characters in a seam candidate."""
    return sum(
        1
        for char in str(text or "")
        if not char.isspace() and char not in ",.;:!?，。！？、；：()[]{}\"'“”‘’"
    )


def _is_strong_seam_overlap(text: str) -> bool:
    """Return whether an overlap candidate is strong enough to trim post-merge."""
    candidate = str(text or "").strip()
    if not candidate:
        return False

    meaningful_chars = _meaningful_char_count(candidate)
    if any(is_cjk_family_char(char) for char in candidate):
        return meaningful_chars >= 4 and (
            "\n" in candidate or candidate.endswith(tuple(SENTENCE_TERMINATORS))
        )

    tokens = [token for token in re.split(r"\s+", candidate) if token]
    return meaningful_chars >= 12 and (
        len(tokens) >= 3 or "\n" in candidate or candidate.endswith(tuple(SENTENCE_TERMINATORS))
    )


def _trim_seam_overlap(previous_block: str, current_block: str) -> tuple[str, int]:
    """Trim a repeated prefix from the current body block when the seam is strong."""
    overlap = find_leading_overlap(previous_block, current_block)
    if not overlap or not _is_strong_seam_overlap(overlap):
        return str(current_block or "").strip(), 0
    return str(current_block or "")[len(overlap):].lstrip(), len(overlap)


def _ends_with_terminal_punctuation(text: str) -> bool:
    """Return whether a body block ends with strong terminal punctuation."""
    stripped = str(text or "").rstrip()
    return bool(stripped) and stripped[-1] in SENTENCE_TERMINATORS


def _contains_cjk(text: str) -> bool:
    """Return whether the block contains any CJK-family character."""
    return any(is_cjk_family_char(char) for char in str(text or ""))


def _ascii_word_tokens(text: str) -> list[str]:
    """Return lightweight ASCII word tokens for heading/fragment heuristics."""
    return ASCII_WORD_RE.findall(str(text or ""))


def _looks_like_titleish_ascii_line(text: str) -> bool:
    """Return whether an ASCII line looks more like a short heading than a sentence fragment."""
    stripped = str(text or "").strip()
    if not stripped or "\n" in stripped:
        return False
    if re.search(r"[.!?,:;]", stripped):
        return False

    tokens = _ascii_word_tokens(stripped)
    if not tokens or len(tokens) > 6:
        return False

    titleish_tokens = sum(1 for token in tokens if token.isupper() or token[:1].isupper())
    return titleish_tokens >= max(1, len(tokens) - 1)


def _looks_like_incomplete_fragment(text: str) -> bool:
    """Return whether a body block looks like a split fragment."""
    stripped = str(text or "").rstrip()
    if not stripped:
        return False
    if _ends_with_terminal_punctuation(stripped):
        return False
    if LIST_PREFIX_RE.match(stripped) or stripped.startswith(("#", ">")):
        return False
    if stripped[-1] in SOFT_CONTINUATION_ENDINGS:
        return True
    if _contains_cjk(stripped):
        punctuation_count = sum(1 for char in stripped if char in "，。！？；：,:;!?")
        return punctuation_count == 0 and _meaningful_char_count(stripped) >= 8

    tokens = _ascii_word_tokens(stripped)
    if tokens:
        if len(tokens) < 3 or _looks_like_titleish_ascii_line(stripped):
            return False
        return len(stripped) >= 12

    return len(stripped) >= 16


def _should_merge_short_body_blocks(previous_block: str, current_block: str) -> bool:
    """Return whether two adjacent body blocks should become one paragraph."""
    previous = str(previous_block or "").strip()
    current = str(current_block or "").strip()
    if not previous or not current:
        return False
    if len(previous) + len(current) > 280:
        return False
    return _looks_like_incomplete_fragment(previous)


def _merge_body_blocks(previous_block: str, current_block: str) -> str:
    """Merge two body blocks into one paragraph using a single newline seam."""
    previous = str(previous_block or "").rstrip()
    current = str(current_block or "").lstrip()
    if not previous:
        return current
    if not current:
        return previous
    return f"{previous}\n{current}"


def _strip_duplicate_heading_lines(previous_heading_block: str, current_block: str) -> tuple[str, int]:
    """Strip heading lines duplicated immediately after an injected chapter block."""
    previous_lines = {
        line.strip()
        for line in str(previous_heading_block or "").splitlines()
        if line.strip().startswith("#")
    }
    if not previous_lines:
        return str(current_block or "").strip(), 0

    current_lines = str(current_block or "").splitlines()
    trimmed_lines = list(current_lines)
    removed = 0
    while trimmed_lines:
        candidate = trimmed_lines[0].strip()
        if not candidate:
            trimmed_lines.pop(0)
            removed += 1
            continue
        if candidate.startswith("#") and candidate in previous_lines:
            trimmed_lines.pop(0)
            removed += 1
            continue
        break
    return "\n".join(trimmed_lines).strip(), removed


def _cleanup_body_blocks(text: str, diagnostics: dict) -> str:
    """Run markdown-safe cleanup on the merge body."""
    blocks = _split_blocks(text)
    if not blocks:
        return ""

    cleaned_blocks: list[str] = []
    for raw_block in blocks:
        current_block = raw_block
        if _block_type(current_block) == "seam":
            continue
        if cleaned_blocks and _block_type(cleaned_blocks[-1]) == "heading":
            stripped_block, removed_heading_lines = _strip_duplicate_heading_lines(cleaned_blocks[-1], current_block)
            if removed_heading_lines:
                diagnostics["heading_line_dedup_count"] += removed_heading_lines
                current_block = stripped_block
                if not current_block:
                    continue

        current_type = _block_type(current_block)
        if cleaned_blocks:
            previous_block = cleaned_blocks[-1]
            previous_type = _block_type(previous_block)
            if current_type == "heading" and previous_type == "heading":
                if _canonical_block_text(previous_block) == _canonical_block_text(current_block):
                    diagnostics["duplicate_heading_block_count"] += 1
                    continue
            if current_type == "body" and previous_type == "body":
                if _canonical_block_text(previous_block) == _canonical_block_text(current_block):
                    diagnostics["duplicate_body_blocks_removed"] += 1
                    continue
                trimmed_block, trimmed_chars = _trim_seam_overlap(previous_block, current_block)
                if trimmed_chars:
                    diagnostics["seam_overlap_trim_count"] += 1
                    diagnostics["seam_overlap_trimmed_chars"] += trimmed_chars
                    current_block = trimmed_block
                    if not current_block:
                        diagnostics["duplicate_body_blocks_removed"] += 1
                        continue
                if _should_merge_short_body_blocks(previous_block, current_block):
                    cleaned_blocks[-1] = _merge_body_blocks(previous_block, current_block)
                    diagnostics["short_paragraph_merge_count"] += 1
                    continue

        cleaned_blocks.append(current_block)

    return "\n\n".join(block.strip() for block in cleaned_blocks if block.strip()).strip()


def post_merge_cleanup(text: str, *, has_header: bool = False) -> tuple[str, dict]:
    """Clean merge-body seams while preserving markdown structure.

    Callers should pass the merge body only. ``has_header`` is retained for
    backward compatibility and is otherwise ignored.
    """
    del has_header
    diagnostics = build_empty_post_merge_diagnostics()
    normalized = _normalize_text_body(text)
    if not normalized:
        return "", diagnostics

    cleaned_text = _cleanup_body_blocks(normalized, diagnostics)
    cleaned_text, collapsed_count = re.subn(r"\n{3,}", "\n\n", cleaned_text)
    diagnostics["blank_line_groups_collapsed"] = collapsed_count
    return cleaned_text.strip(), diagnostics
