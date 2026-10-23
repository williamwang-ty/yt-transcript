"""Global outline and terminology pass for the sliding-window executor.

Before the first window round, the whole numbered source goes to the model
once (or in a few parts when it is too long for one request), and the model
returns a short JSON object: the section list (title, first source unit and
the words the section begins with) and a list of names and terms with the
spellings that should be used. The window rounds then get the relevant part
of the outline and the terms that occur in their window; section headings
come from the outline instead of being chosen by each round, and the program
adds them to the written text (`kernel/text/structure.py`).

A section's position is its anchor: the words it begins with, copied from the
source, plus the unit holding them. Unit numbers alone are too coarse, since
a unit of unpunctuated subtitles is a cut at a cue pause and often starts in
the middle of a sentence; the heading goes before the written sentence that
holds the anchor.

YouTube chapters, when the video has them, are mapped to source units and
given to the model as hints, anchored at the chapter's first subtitle cue.
If the outline cannot be produced or fails validation, the run continues
without it and the rounds place their own headings, as before.
"""

from __future__ import annotations

import json
import math
import re

from kernel.config import DEFAULT_OUTLINE_MAX_TERMS, DEFAULT_OUTLINE_READ_TOKENS, parse_bool
from kernel.llm.api import LLMRequestError

# Raw-stage prompts that write section headings, and the language of the titles.
OUTLINE_PROMPTS = {
    "cleanup_zh": "zh",
    "structure_only": "source",
}
# Target section length, in source tokens, for the "aim for" range.
SECTION_TOKENS_MIN = 600
SECTION_TOKENS_MAX = 2500
# An outline is rejected when fewer than this share of its sections is usable.
MIN_VALID_SECTION_SHARE = 0.5
MAX_TITLE_CHARS = 80
MAX_TERM_CHARS = 60
# Terms sent with one window round.
MAX_ROUND_TERMS = 20
TERM_KINDS = ("person", "organization", "product", "term")
HEADER_LINE = re.compile(r"^[ \t]*#{1,6}[ \t]+.*$\n?", re.MULTILINE)
# A section's first words are looked for this far from the unit the model
# gave, in the source and again in the written text, and a chapter takes over
# the section starting this close to it.
HEADING_TOLERANCE_UNITS = 2
# Length of an anchor, weighing an English word 2 and a Chinese character 1:
# about as long as the words it is made from (a cue, the model's first
# words), kept within MIN..MAX, or DEFAULT when there is nothing to go by.
ANCHOR_MIN_WEIGHT = 8
ANCHOR_MAX_WEIGHT = 16
ANCHOR_DEFAULT_WEIGHT = 12
# First words shorter than this (letters and digits only) are not searched for.
MIN_ANCHOR_KEY = 4
# When the whole of the model's first words is not found, their start is.
ANCHOR_PROBE_KEY = 6
ANCHOR_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?|[^\W_]")

OUTLINE_PROMPT = """# Transcript Outline

**Task**: Read the transcript below and plan its section headings and a list of names and terms. Do not rewrite or summarize the transcript.

Every source unit starts with a number tag like [S12]. A unit may start or end in the middle of a sentence.{part_note}

## Output

Return only one JSON object and nothing else:

{{"sections": [{{"start": {first}, "first_words": "...", "title": "..."{title_zh_example}}}], "terms": [{{"term": "...", "variants": ["..."], "kind": "person"{zh_example}}}]}}

- `sections`: the major topics in order. `first_words` are the first words of the sentence that begins the topic (about 6 to 12 Chinese characters, or 4 to 8 English words), copied exactly from the transcript without the tag; `start` is the number of the unit that contains them, and the numbers must increase.{first_note} Aim for {low} to {high} sections; start a section only at a real topic shift, not for a single remark. `title` is a short heading in {title_language} (at most about 20 Chinese characters or 8 English words), without numbering or `#`.{title_zh_note}
- `terms`: at most {max_terms} names of people, organizations and products, and technical terms, that recur or matter. `term` is the name as the speaker uses it, spelled correctly: keep a Chinese name in Chinese when that is what is said, and do not expand it to a full name or swap in a translation. `variants` lists only misrecognized or misspelled forms of that same word that actually appear in the transcript (speech-recognition errors, typos); never a translation, an abbreviation, a short form or a full name, since the speaker's wording is kept. Leave it empty unless you are confident. `kind` is one of person, organization, product, term.{zh_note}{hints}

## Transcript

{numbered}
"""

HINTS_BLOCK = """

## Video Chapters

The video has its own chapters, listed below with the unit and the words where each one starts. Keep every chapter as its own section starting there, with those words as its `first_words`; do not merge chapters. You may split a long chapter into several sections where the topic changes. Keep each chapter's title as written, including its numbers; only translate it into {title_language} if it is in another language, and replace placeholder titles such as "<Untitled Chapter 1>".

{lines}"""

OUTLINE_ROUND_BLOCK = """## Outline (section titles for this part of the transcript)

{lines}"""


def outline_supported(prompt_name: str) -> bool:
    return prompt_name in OUTLINE_PROMPTS


def outline_enabled(config: dict | None) -> bool:
    return parse_bool((config or {}).get("window_outline", True), True)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def split_parts(unit_tokens: list[int], budget: int) -> list[tuple[int, int]]:
    """Split units into the fewest near-equal parts of at most about `budget` tokens.

    Returns `(start, end)` index pairs, end exclusive.
    """
    total = sum(unit_tokens)
    if not unit_tokens:
        return []
    count = max(1, math.ceil(total / max(1, budget)))
    if count == 1:
        return [(0, len(unit_tokens))]
    target = total / count
    parts, start, used = [], 0, 0
    for index, tokens in enumerate(unit_tokens):
        if used and used + tokens > target and len(parts) < count - 1:
            parts.append((start, index))
            start, used = index, 0
        used += tokens
    parts.append((start, len(unit_tokens)))
    return parts


def section_range(source_tokens: int) -> tuple[int, int]:
    """Suggested number of sections for a stretch of source."""
    low = max(1, round(source_tokens / SECTION_TOKENS_MAX))
    high = max(low, round(source_tokens / SECTION_TOKENS_MIN))
    return low, high


def build_outline_prompt(units: list[str], start: int, end: int, *, prompt_name: str,
                         source_tokens: int, hints: list[dict] | None = None,
                         part: int = 1, parts: int = 1, previous_title: str = "",
                         max_terms: int = DEFAULT_OUTLINE_MAX_TERMS) -> str:
    """Build the outline request for units[start:end] (0-based, end exclusive)."""
    bilingual = OUTLINE_PROMPTS.get(prompt_name) == "source"
    title_language = "the transcript's own language" if bilingual else "Simplified Chinese"
    low, high = section_range(source_tokens)
    if parts > 1:
        part_note = (f" This is part {part} of {parts} of a longer transcript; it covers [S{start + 1}] to [S{end}]."
                     " Outline only this part.")
    else:
        part_note = ""
    if part > 1 and previous_title:
        first_note = (f" The section \"{previous_title}\" from the previous part continues at the start of this part;"
                      " list a section only where a new topic begins.")
    else:
        first_note = f" The first section starts at [S{start + 1}]."
    hint_lines = [f"- [S{hint['start']}] {hint['title']}"
                  + (f" (begins with “{hint['anchor']}”)" if hint.get("anchor") else "")
                  for hint in hints or [] if start < int(hint["start"]) <= end]
    hints_text = HINTS_BLOCK.format(title_language=title_language, lines="\n".join(hint_lines)) if hint_lines else ""
    unit_separator = "\n"
    numbered = unit_separator.join(f"[S{index + 1}] {units[index]}" for index in range(start, end))
    return OUTLINE_PROMPT.format(
        part_note=part_note,
        first=start + 1,
        title_zh_example=', "title_zh": "..."' if bilingual else "",
        zh_example=', "zh": "..."' if bilingual else "",
        first_note=first_note,
        low=low,
        high=high,
        title_language=title_language,
        title_zh_note=(" Also give `title_zh`, the title in Simplified Chinese." if bilingual else ""),
        max_terms=max_terms,
        zh_note=(" Also give `zh`, the usual Simplified Chinese rendering of the term (the term itself when it is"
                 " normally kept as is, like API)." if bilingual else ""),
        hints=hints_text,
        numbered=numbered,
    )


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

def _extract_json_object(text: str):
    raw = re.sub(r"^```[\w-]*[ \t]*$", "", text or "", flags=re.MULTILINE).strip()
    start = raw.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    while start >= 0:
        try:
            payload, _ = decoder.raw_decode(raw[start:])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
        start = raw.find("{", start + 1)
    return None


def _unit_number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    match = re.fullmatch(r"\s*\[?S?(\d+)\]?\s*", str(value or ""), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def clean_title(value) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    title = re.sub(r"^#+\s*", "", title)
    title = re.sub(r"^\[S\d+\]\s*", "", title).strip(" \t-—:：")
    return title[:MAX_TITLE_CHARS].strip()


def _title_key(title: str) -> str:
    return re.sub(r"[\W_]+", "", title.lower())


def parse_outline(text: str, first: int, last: int, *, max_terms: int = DEFAULT_OUTLINE_MAX_TERMS) -> dict:
    """Parse and validate one outline response.

    `first` and `last` are the 1-based unit numbers the request covered.
    Sections must start inside that range and in increasing order; entries
    that break this are dropped, and the outline is rejected when no section
    or fewer than half of them survive.
    """
    payload = _extract_json_object(text)
    if payload is None:
        return _invalid("no_json", "response has no JSON object")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        return _invalid("no_sections", "JSON has no sections list")

    sections, dropped = [], 0
    previous = first - 1
    for entry in raw_sections:
        start = _unit_number(entry.get("start")) if isinstance(entry, dict) else None
        title = clean_title(entry.get("title")) if isinstance(entry, dict) else ""
        if start is None or not title or start < first or start > last or start <= previous:
            dropped += 1
            continue
        if sections and _title_key(sections[-1]["title"]) == _title_key(title):
            dropped += 1  # the same topic listed twice in a row
            continue
        section = {"start": start, "title": title}
        title_zh = clean_title(entry.get("title_zh"))
        if title_zh and title_zh != title:
            section["title_zh"] = title_zh
        first_words = re.sub(r"\[S\d+\]|\s+", " ", str(entry.get("first_words", "") or "")).strip(" \"'“”‘’「」")
        if first_words:
            section["first_words"] = first_words[:MAX_TITLE_CHARS]
        sections.append(section)
        previous = start
    if not sections:
        return _invalid("no_valid_sections", "no section starts inside the range in increasing order")
    if len(sections) < MIN_VALID_SECTION_SHARE * len(raw_sections):
        return _invalid("invalid_sections",
                        f"only {len(sections)} of {len(raw_sections)} sections have valid, increasing starts")

    return {"ok": True, "sections": sections, "terms": _parse_terms(payload.get("terms"), max_terms),
            "dropped_sections": dropped, "error_type": "", "error": ""}


def _parse_terms(raw_terms, max_terms: int) -> list[dict]:
    terms, seen = [], set()
    for entry in raw_terms if isinstance(raw_terms, list) else []:
        if isinstance(entry, str):
            entry = {"term": entry}
        if not isinstance(entry, dict):
            continue
        term = re.sub(r"\s+", " ", str(entry.get("term", "") or "")).strip()
        if not term or len(term) > MAX_TERM_CHARS or term.lower() in seen:
            continue
        variants = []
        for variant in entry.get("variants") if isinstance(entry.get("variants"), list) else []:
            variant = re.sub(r"\s+", " ", str(variant or "")).strip()
            if variant and len(variant) <= MAX_TERM_CHARS and variant.lower() != term.lower() \
                    and variant not in variants and _is_misspelling(term, variant):
                variants.append(variant)
        kind = str(entry.get("kind", "") or "").strip().lower()
        item = {"term": term, "variants": variants, "kind": kind if kind in TERM_KINDS else "term"}
        zh = re.sub(r"\s+", " ", str(entry.get("zh", "") or "")).strip()
        if zh and len(zh) <= MAX_TERM_CHARS:
            item["zh"] = zh
        terms.append(item)
        seen.add(term.lower())
        if len(terms) >= max_terms:
            break
    return terms


def _has_cjk(text: str) -> bool:
    return re.search(r"[\u3400-\u9fff]", text) is not None


def _is_misspelling(term: str, variant: str) -> bool:
    """Whether a variant can be a misspelling of the term rather than another way to say it.

    A variant in another script is a translation (万事达 / Mastercard), and one
    contained in the other is a short or full form (Tukey / John Tukey); the
    speaker's wording is kept for both, so they are not corrected.
    """
    if _has_cjk(term) != _has_cjk(variant):
        return False
    if _has_cjk(term):
        return term not in variant and variant not in term
    term_words = set(re.findall(r"[a-z0-9]+", term.lower()))
    variant_words = set(re.findall(r"[a-z0-9]+", variant.lower()))
    return not (term_words <= variant_words or variant_words <= term_words)


def _invalid(error_type: str, message: str) -> dict:
    return {"ok": False, "sections": [], "terms": [], "dropped_sections": 0,
            "error_type": error_type, "error": message}


def merge_parts(parsed_parts: list[dict], max_terms: int = DEFAULT_OUTLINE_MAX_TERMS) -> dict:
    """Join the outlines of consecutive parts into one."""
    sections, terms, by_key = [], [], {}
    for parsed in parsed_parts:
        for section in parsed["sections"]:
            if sections and (section["start"] <= sections[-1]["start"]
                             or _title_key(section["title"]) == _title_key(sections[-1]["title"])):
                continue
            sections.append(section)
        for term in parsed["terms"]:
            key = term["term"].lower()
            if key in by_key:
                known = by_key[key]
                known["variants"] += [v for v in term["variants"] if v not in known["variants"]]
                if term.get("zh") and not known.get("zh"):
                    known["zh"] = term["zh"]
                continue
            if len(terms) < max_terms:
                item = dict(term, variants=list(term["variants"]))
                terms.append(item)
                by_key[key] = item
    return {"sections": sections, "terms": terms}


def generate_outline(units: list[str], *, prompt_name: str, call_llm, max_output_tokens: int,
                     estimate, read_tokens: int = DEFAULT_OUTLINE_READ_TOKENS,
                     max_terms: int = DEFAULT_OUTLINE_MAX_TERMS, hints: list[dict] | None = None,
                     log=None) -> dict:
    """Ask the model for the outline, in parts when the source is too long for one request.

    `call_llm(prompt_text, max_output_tokens)` is the window executor's
    backend call; `LLMRequestError` from it is caught and reported as a
    failed outline. Returns the outline record kept in the window state.
    """
    log = log or (lambda message: None)
    unit_tokens = [estimate(f"[S{index + 1}] {unit}") for index, unit in enumerate(units)]
    parts = split_parts(unit_tokens, read_tokens)
    record = {"status": "failed", "sections": [], "terms": [], "parts": len(parts),
              "hints": list(hints or []), "requests": [], "error_type": "", "error": ""}
    parsed_parts = []
    previous_title = ""
    for number, (start, end) in enumerate(parts, 1):
        prompt = build_outline_prompt(
            units, start, end, prompt_name=prompt_name, source_tokens=sum(unit_tokens[start:end]),
            hints=hints, part=number, parts=len(parts), previous_title=previous_title, max_terms=max_terms,
        )
        request = {"part": number, "first": start + 1, "last": end, "prompt_tokens": estimate(prompt)}
        try:
            result = call_llm(prompt, max_output_tokens)
        except LLMRequestError as error:
            request.update({"result": "error", "error_type": error.error_type, "error": str(error)})
            record["requests"].append(request)
            record.update({"error_type": error.error_type, "error": f"part {number}: {error}"})
            return record
        parsed = parse_outline(str(result.get("text", "") or ""), start + 1, end, max_terms=max_terms)
        request.update({"latency_ms": int(result.get("latency_ms", 0) or 0),
                        "result": "ok" if parsed["ok"] else "invalid",
                        "sections": len(parsed["sections"]), "dropped_sections": parsed["dropped_sections"]})
        if not parsed["ok"]:
            request.update({"error_type": parsed["error_type"], "error": parsed["error"]})
            record["requests"].append(request)
            record.update({"error_type": parsed["error_type"], "error": f"part {number}: {parsed['error']}"})
            return record
        record["requests"].append(request)
        parsed_parts.append(parsed)
        previous_title = parsed["sections"][-1]["title"]
        if len(parts) > 1:
            log(f"ℹ️ Outline part {number}/{len(parts)}: {len(parsed['sections'])} sections")
    merged = merge_parts(parsed_parts, max_terms)
    merged["sections"] = apply_chapters(anchor_sections(merged["sections"], units), hints, prompt_name)
    record.update({"status": "ok", **merged})
    return record


# ---------------------------------------------------------------------------
# Anchors: the words a section begins with
# ---------------------------------------------------------------------------

def key_chars(text: str) -> tuple[str, list[int]]:
    """Lower-cased letters and digits of `text`, and the index in `text` of each."""
    chars, positions = [], []
    for index, char in enumerate(text):
        if char.isalnum():
            lowered = char.lower()
            chars.append(lowered)
            positions.extend([index] * len(lowered))
    return "".join(chars), positions


def _join_units(left: str, right: str) -> str:
    return left + (" " if (left[-1:] + right[:1]).isascii() else "") + right


def text_weight(text: str) -> int:
    return sum(2 if word.isascii() else 1 for word in ANCHOR_WORD.findall(text))


def anchor_text(units: list[str], index: int, position: int, weight: int = ANCHOR_DEFAULT_WEIGHT) -> str:
    """The source text from `position` in units[index], about `weight` long (see ANCHOR_*_WEIGHT).

    It runs into the next units only when the rest of this one is shorter
    than the minimum.
    """
    text = units[index][position:]
    for following in units[index + 1:index + 3]:
        if text_weight(text) >= ANCHOR_MIN_WEIGHT:
            break
        text = _join_units(text, following)
    target = max(ANCHOR_MIN_WEIGHT, min(ANCHOR_MAX_WEIGHT, weight))
    words, total = [], 0
    for word in ANCHOR_WORD.finditer(text):
        words.append(word)
        total += 2 if word.group().isascii() else 1
        if total >= target:
            break
    return text[words[0].start():words[-1].end()] if words else ""


def locate_words(units: list[str], words: str, start: int) -> tuple[int, int] | None:
    """Find a section's first words in the source near its start unit (1-based).

    Only letters and digits are compared, so punctuation and spacing do not
    matter. Returns (unit index, position in that unit) of the occurrence
    nearest the start unit within HEADING_TOLERANCE_UNITS, or None. When the
    whole of the words is not there, their first ANCHOR_PROBE_KEY characters
    are tried, since models sometimes fix a word while copying.
    """
    key, _ = key_chars(words)
    if len(key) < MIN_ANCHOR_KEY:
        return None
    probes = [key, key[:ANCHOR_PROBE_KEY]] if len(key) > ANCHOR_PROBE_KEY else [key]
    first = max(0, start - 1 - HEADING_TOLERANCE_UNITS)
    last = min(len(units), start + HEADING_TOLERANCE_UNITS)
    for probe in probes:
        best = None
        for index in range(first, last):
            # The words may run over the end of the unit into the next one.
            text = _join_units(units[index], units[index + 1]) if index + 1 < len(units) else units[index]
            compact, positions = key_chars(text)
            found = compact.find(probe)
            while found >= 0 and positions[found] < len(units[index]):
                candidate = (abs(index - (start - 1)), index, positions[found])
                best = candidate if best is None else min(best, candidate)
                found = compact.find(probe, found + 1)
        if best is not None:
            return best[1], best[2]
    return None


def anchor_sections(sections: list[dict], units: list[str]) -> list[dict]:
    """Turn the model's `first_words` into anchors copied from the source.

    A section whose words are found moves to the unit holding them and gets
    `anchor`, the source text from there; one whose words are missing keeps
    its unit and has no anchor. A section that would no longer start after
    the one before it is dropped.
    """
    anchored = []
    for section in sections:
        section = dict(section)
        words = section.pop("first_words", "")
        found = locate_words(units, words, section["start"]) if words else None
        if found:
            index, position = found
            section["start"] = index + 1
            section["anchor"] = anchor_text(units, index, position, text_weight(words))
        if anchored and section["start"] <= anchored[-1]["start"]:
            continue
        anchored.append(section)
    return anchored


# ---------------------------------------------------------------------------
# Chapter hints
# ---------------------------------------------------------------------------

def apply_chapters(sections: list[dict], hints: list[dict] | None, prompt_name: str) -> list[dict]:
    """Put the video's own chapters back on the sections that start at them.

    Models tend to reword chapter titles and drop numbers from them even when
    told not to, and to move a start to the beginning of a unit. The section
    nearest a chapter start, within HEADING_TOLERANCE_UNITS, takes the
    chapter's position (unit and anchor) and title, unless that title is a
    placeholder or not in the output language (an English chapter title in a
    Chinese cleanup run), where the model's title is kept.
    """
    if not hints:
        return sections
    sections = [dict(section) for section in sections]
    taken = set()
    for hint in hints:
        start = int(hint["start"])
        nearby = [(abs(section["start"] - start), index) for index, section in enumerate(sections)
                  if index not in taken and abs(section["start"] - start) <= HEADING_TOLERANCE_UNITS]
        if not nearby:
            continue
        index = min(nearby)[1]
        taken.add(index)
        section = sections[index]
        if hint.get("anchor"):
            section["anchor"] = hint["anchor"]
        elif section["start"] != start:
            section.pop("anchor", None)
        section["start"] = start
        title = clean_title(hint.get("title"))
        placeholder = "untitled chapter" in title.lower() or title.lower().startswith("<untitled")
        if not title or placeholder or (OUTLINE_PROMPTS.get(prompt_name) == "zh" and not _has_cjk(title)):
            continue
        if section["title"] != title:
            section["title"] = title
            if section.get("title_zh") == title:
                section.pop("title_zh")
    # A chapter that moved onto or past a neighbouring section wins over it.
    ordered = sorted(range(len(sections)), key=lambda index: (sections[index]["start"], index not in taken))
    kept = []
    for index in ordered:
        if kept and sections[index]["start"] <= kept[-1]["start"]:
            continue
        kept.append(sections[index])
    return kept


def _visible_index(text: str, count: int) -> int:
    """Index in `text` of its non-whitespace character number `count` (0-based)."""
    seen = 0
    for index, char in enumerate(text):
        if char.isspace():
            continue
        if seen == count:
            return index
        seen += 1
    return len(text)


def hints_from_times(chapters: list[dict], cue_starts: list[tuple[int, float]],
                     unit_offsets: list[int | None], units: list[str] | None = None) -> list[dict]:
    """Map YouTube chapter start times to source units through the subtitle cues.

    `cue_starts` is (offset in the whitespace-free cue text, start time) per
    cue; `unit_offsets` the offset where each unit starts in that text, or
    None for units that could not be located. With `units`, each hint also
    gets `anchor`: the source text from the chapter's first cue on. A
    chapter that starts after the last cue has no text and is left out.
    """
    located = [(offset, index) for index, offset in enumerate(unit_offsets) if offset is not None]
    if not located or not cue_starts:
        return []
    last_cue = max(time for _, time in cue_starts)
    cue_ends = {offset: following for (offset, _), (following, _) in zip(cue_starts, cue_starts[1:])}
    hints = []
    for chapter in chapters:
        title = clean_title(chapter.get("title"))
        start_time = chapter.get("start_time")
        if not title or not isinstance(start_time, (int, float)) or float(start_time) > last_cue:
            continue
        # The cue starting closest to the chapter time (the later one on a tie).
        offset = min(cue_starts, key=lambda cue: (abs(cue[1] - float(start_time)), -cue[1]))[0]
        # The unit holding that cue's first character.
        unit_index = None
        for unit_offset, index in located:
            if unit_offset > offset:
                break
            unit_index = index
        if unit_index is None:
            unit_index = located[0][1]
        if hints and hints[-1]["start"] >= unit_index + 1:
            continue
        hint = {"start": unit_index + 1, "title": title}
        if units:
            unit = units[unit_index]
            within = max(0, offset - unit_offsets[unit_index])
            position = _visible_index(unit, within)
            # As long as the chapter's first cue (the part of it in this unit).
            weight = text_weight(unit[position:_visible_index(unit, within + cue_ends[offset] - offset)]) \
                if offset in cue_ends else ANCHOR_DEFAULT_WEIGHT
            anchor = anchor_text(units, unit_index, position, weight)
            if anchor:
                hint["anchor"] = anchor
        hints.append(hint)
    return hints


# ---------------------------------------------------------------------------
# Use in window rounds
# ---------------------------------------------------------------------------

def round_outline_text(sections: list[dict], cursor: int, window_end: int) -> str:
    """The outline lines a round needs: the section it starts in, those starting in its window, and the next.

    The model is asked to start a new paragraph at the beginning of the
    sentence each section in its window begins with, so that the heading the
    program adds there later falls between two paragraphs.
    """
    lines = []
    following = None
    for index, section in enumerate(sections):
        start, title = section["start"], section["title"]
        if start <= cursor and (index + 1 == len(sections) or sections[index + 1]["start"] > cursor):
            lines.append(f"- {title} (the section [S{cursor + 1}] is in)")
        elif cursor < start <= window_end:
            if section.get("anchor"):
                spot = (f"it begins in [S{start}] at the words “{section['anchor']}”; start a new paragraph at the "
                        "beginning of the sentence that contains these words")
            else:
                spot = (f"it begins in or right after [S{start}], where this topic starts; start a new paragraph at "
                        "the beginning of that sentence")
            lines.append(f"- {title} (starts in this window: {spot})")
        elif start > window_end and following is None:
            following = index
            lines.append(f"- {title} (next section, after this window)")
    return OUTLINE_ROUND_BLOCK.format(lines="\n".join(lines)) if lines else ""


def _term_occurs(term: str, text: str) -> bool:
    if term.isascii():
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    return term in text


def round_terms_text(terms: list[dict], source_text: str, *, translation: bool = False,
                     max_terms: int = MAX_ROUND_TERMS) -> str:
    """The glossary block for one round: outline terms whose spelling or variant occurs in its source."""
    lines = []
    for entry in terms:
        found = [v for v in entry.get("variants", []) if _term_occurs(v, source_text)]
        if not found and not _term_occurs(entry["term"], source_text):
            continue
        if translation:
            zh = entry.get("zh", "")
            lines.append(f"- {entry['term']} => {zh}" if zh and zh != entry["term"]
                         else f"- {entry['term']} (keep as is)")
        elif found:
            lines.append(f"- {entry['term']} (the source may write it as {', '.join(found)}; write {entry['term']})")
        else:
            lines.append(f"- {entry['term']} (keep this spelling)")
        if len(lines) >= max_terms:
            break
    if not lines:
        return ""
    header = ["## Terminology", "",
              "Names and terms from the whole transcript. Use these spellings consistently."
              if not translation else "Translate these names and terms consistently.", ""]
    return "\n".join(header + lines)


def strip_headers(text: str) -> str:
    """Remove Markdown header lines a round wrote although the outline places headings."""
    stripped = HEADER_LINE.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


def heading_block(section: dict) -> str:
    lines = [f"## {section['title']}"]
    if section.get("title_zh") and section["title_zh"] != section["title"]:
        lines.append(f"## {section['title_zh']}")
    return "\n".join(lines)
