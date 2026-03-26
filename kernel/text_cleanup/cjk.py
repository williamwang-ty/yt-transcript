"""CJK-aware spacing helpers."""

from __future__ import annotations

import re


CJK_FAMILY_CLASS = r"\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af"
CJK_PUNCTUATION_CLASS = r"，。！？、；：,.!?;:"
OPENING_BRACKETS_CLASS = r"（【《「『"
CLOSING_BRACKETS_CLASS = r"）」』】》"


def is_cjk_char(char: str) -> bool:
    """Return whether a character is a CJK ideograph."""
    first = str(char or "")[:1]
    return bool(first) and "\u3400" <= first <= "\u9fff"


def is_kana_hangul_char(char: str) -> bool:
    """Return whether a character belongs to Kana or Hangul blocks."""
    first = str(char or "")[:1]
    return bool(first) and (("\u3040" <= first <= "\u30ff") or ("\uac00" <= first <= "\ud7af"))


def is_cjk_family_char(char: str) -> bool:
    """Return whether a character belongs to a CJK-family script."""
    return is_cjk_char(char) or is_kana_hangul_char(char)


def normalize_cjk_spacing(text: str) -> tuple[str, dict]:
    """Tighten spacing around CJK text without touching Latin-word spacing."""
    normalized = str(text or "")
    diagnostics = {
        "collapsed_cjk_spacing_count": 0,
        "tightened_punctuation_spacing_count": 0,
        "tightened_bracket_spacing_count": 0,
    }
    if not normalized:
        return "", diagnostics

    for _ in range(8):
        updated = normalized
        updated, collapsed_count = re.subn(
            rf"([{CJK_FAMILY_CLASS}])\s+([{CJK_FAMILY_CLASS}])",
            r"\1\2",
            updated,
        )
        updated, punctuation_count = re.subn(
            rf"([{CJK_FAMILY_CLASS}])\s+([{CJK_PUNCTUATION_CLASS}])",
            r"\1\2",
            updated,
        )
        updated, opening_count = re.subn(
            rf"([{OPENING_BRACKETS_CLASS}])\s+([{CJK_FAMILY_CLASS}])",
            r"\1\2",
            updated,
        )

        diagnostics["collapsed_cjk_spacing_count"] += collapsed_count
        diagnostics["tightened_punctuation_spacing_count"] += punctuation_count
        diagnostics["tightened_bracket_spacing_count"] += opening_count

        if updated == normalized:
            break
        normalized = updated

    normalized, punctuation_count = re.subn(
        rf"\s+([{CJK_PUNCTUATION_CLASS}])",
        r"\1",
        normalized,
    )
    normalized, opening_count = re.subn(
        rf"([{OPENING_BRACKETS_CLASS}])\s+",
        r"\1",
        normalized,
    )
    normalized, closing_count = re.subn(
        rf"\s+([{CLOSING_BRACKETS_CLASS}])",
        r"\1",
        normalized,
    )

    diagnostics["tightened_punctuation_spacing_count"] += punctuation_count
    diagnostics["tightened_bracket_spacing_count"] += opening_count + closing_count
    return normalized, diagnostics
