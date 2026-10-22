"""Token estimates, sentence splitting and character counts shared by the text and quality layers."""

from __future__ import annotations

import re

from kernel.text.cleanup.cjk import is_cjk_char, is_cjk_family_char, is_kana_hangul_char

QUALITY_PUNCTUATION_CHARS = set(",.;:!?，。！？、；：()[]{}\"'“”‘’`")
QUALITY_SENTENCE_ENDINGS = ("。", "！", "？", "!", "?", "；", ";")


def _is_latin_word_char(char: str) -> bool:
    """Return whether latin word char."""
    return ("a" <= char <= "z") or ("A" <= char <= "Z") or char.isdigit()


def _new_token_estimate_state() -> dict:
    """Create token estimate state."""
    return {"tokens": 0, "punct_count": 0, "latin_word_len": 0}


def _advance_token_estimate_state(state: dict, char: str, next_char: str = "") -> None:
    """Advance token estimate state."""
    if is_cjk_char(char) or is_kana_hangul_char(char):
        state["latin_word_len"] = 0
        state["tokens"] += 1
        return

    if _is_latin_word_char(char):
        word_len = state["latin_word_len"]
        if word_len == 0 or word_len % 4 == 0:
            state["tokens"] += 1
        state["latin_word_len"] = word_len + 1
        return

    if char in "'-" and state["latin_word_len"] > 0 and _is_latin_word_char(next_char):
        word_len = state["latin_word_len"]
        if word_len % 4 == 0:
            state["tokens"] += 1
        state["latin_word_len"] = word_len + 1
        return

    state["latin_word_len"] = 0
    if char.isspace():
        return

    punct_count = state["punct_count"]
    if punct_count % 4 == 0:
        state["tokens"] += 1
    state["punct_count"] = punct_count + 1


def estimate_tokens(text: str) -> int:
    """Estimate tokens with a local heuristic.

    It over-approximates common transcript shapes on purpose and is not a
    substitute for provider-side token counting.
    """
    if not text:
        return 0

    state = _new_token_estimate_state()
    for index, char in enumerate(text):
        next_char = text[index + 1] if index + 1 < len(text) else ""
        _advance_token_estimate_state(state, char, next_char)
    return max(1, state["tokens"])


def truncate_tail_text_to_tokens(text: str, max_tokens: int,
                                  config: dict | None = None) -> str:
    """Keep the tail-most portion of text within a soft token cap."""
    if not text:
        return ""
    if max_tokens <= 0 or estimate_tokens(text) <= max_tokens:
        return text.strip()
    segments = force_split_text_by_tokens(text.strip(), max_tokens, config)
    return segments[-1].strip() if segments else text.strip()


def force_split_text_by_tokens(text: str, max_tokens: int, config: dict | None = None) -> list[str]:
    """Force split text by tokens."""
    del config
    if max_tokens <= 0:
        return [text]

    segments = []
    current_chars = []
    current_state = _new_token_estimate_state()

    for index, char in enumerate(text):
        next_char = text[index + 1] if index + 1 < len(text) else ""
        candidate_state = dict(current_state)
        _advance_token_estimate_state(candidate_state, char, next_char)
        if current_chars and candidate_state["tokens"] > max_tokens:
            segments.append("".join(current_chars))
            current_chars = [char]
            current_state = _new_token_estimate_state()
            _advance_token_estimate_state(current_state, char, next_char)
        else:
            current_chars.append(char)
            current_state = candidate_state

    if current_chars:
        segments.append("".join(current_chars))
    return segments


def split_sentences(text: str) -> list[str]:
    """
    Split text into sentences without requiring spaces after punctuation.

    The splitter is intentionally conservative around English periods so it
    does not break on decimals, initials, or common honorific abbreviations.
    """
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return []

    honorific_abbreviations = {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st",
    }
    closers = '”"\'’」)]}'

    def next_non_space(index: int) -> str:
        """Next non space."""
        for char in normalized[index + 1:]:
            if not char.isspace():
                return char
        return ""

    def previous_ascii_word(index: int) -> str:
        """Previous ascii word."""
        match = re.search(r"([A-Za-z]+)$", normalized[:index])
        return match.group(1) if match else ""

    def acronym_before_period(index: int) -> bool:
        """Acronym before period."""
        return bool(re.search(r"(?:\b[A-Za-z]\.){2,}$", normalized[:index + 1]))

    sentences = []
    start = 0
    i = 0

    while i < len(normalized):
        char = normalized[i]
        boundary = False

        if char in "。！？!?":
            boundary = True
        elif char == ".":
            prev_char = normalized[i - 1] if i > 0 else ""
            next_char = normalized[i + 1] if i + 1 < len(normalized) else ""
            next_next_char = normalized[i + 2] if i + 2 < len(normalized) else ""
            next_visible = next_non_space(i)
            lower_word = previous_ascii_word(i).lower()

            if next_char == ".":
                i += 1
                continue
            if prev_char.isdigit() and next_char.isdigit():
                boundary = False
            elif prev_char.isascii() and prev_char.isalnum() and next_char.isascii() and next_char.isalnum():
                # A dot inside a token such as "Alibaba.com" or "v2.x".
                boundary = False
            elif prev_char.isalpha() and next_char.isalpha() and next_next_char == ".":
                boundary = False
            elif acronym_before_period(i):
                boundary = not next_visible or not next_visible.islower()
            elif lower_word in honorific_abbreviations and next_visible.isupper():
                boundary = False
            else:
                boundary = True

        if not boundary:
            i += 1
            continue

        end = i
        while end + 1 < len(normalized) and normalized[end + 1] in closers:
            end += 1

        sentence = normalized[start:end + 1].strip()
        if sentence:
            sentences.append(sentence)

        start = end + 1
        while start < len(normalized) and normalized[start].isspace():
            start += 1
        i = start

    if start < len(normalized):
        tail = normalized[start:].strip()
        if tail:
            sentences.append(tail)

    return sentences


def count_quality_chars(text: str, *, include_punctuation: bool = False) -> int:
    """Count non-space characters for quality heuristics."""
    return sum(
        1
        for char in str(text or "")
        if not char.isspace() and (include_punctuation or char not in QUALITY_PUNCTUATION_CHARS)
    )


def count_cjk_chars(text: str) -> int:
    """Count CJK-family characters."""
    return sum(1 for char in str(text or "") if is_cjk_family_char(char))


def is_quality_fragment_paragraph(text: str) -> bool:
    """Return whether a paragraph looks like a short subtitle fragment."""
    block = str(text or "").strip()
    if not block or re.match(r"^(?:[-*+]\s|\d+\.\s)", block):
        return False

    meaningful_chars = count_quality_chars(block)
    cjk_chars = count_cjk_chars(block)
    if cjk_chars < 4:
        return False
    if meaningful_chars <= 10:
        return True
    return meaningful_chars <= 22 and not block.endswith(QUALITY_SENTENCE_ENDINGS)
