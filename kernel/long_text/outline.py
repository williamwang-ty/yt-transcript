"""Global outline and terminology pass for the sliding-window executor.

Before the first window round, the whole numbered source goes to the model
once (or in a few parts when it is too long for one request), and the model
returns a short JSON object: the section list (title + first source unit) and
a list of names and terms with the spellings that should be used. The window
rounds then get the relevant part of the outline and the terms that occur in
their window; section headings are inserted by the program at the outline's
section starts instead of being chosen by each round.

YouTube chapters, when the video has them, are mapped to source units and
given to the model as hints. If the outline cannot be produced or fails
validation, the run continues without it and the rounds place their own
headings, as before.
"""

from __future__ import annotations

import json
import math
import re

# Raw-stage prompts that write section headings, and the language of the titles.
OUTLINE_PROMPTS = {
    "cleanup_zh": "zh",
    "structure_only": "source",
}
DEFAULT_OUTLINE_READ_TOKENS = 16000
DEFAULT_OUTLINE_MAX_TERMS = 40
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
# A heading the model writes for a section is accepted when its paragraph
# covers units within this distance of the section's first unit.
HEADING_TOLERANCE_UNITS = 2

OUTLINE_PROMPT = """# Transcript Outline

**Task**: Read the transcript below and plan its section headings and a list of names and terms. Do not rewrite or summarize the transcript.

Every source unit starts with a number tag like [S12].{part_note}

## Output

Return only one JSON object and nothing else:

{{"sections": [{{"start": {first}, "title": "..."{title_zh_example}}}], "terms": [{{"term": "...", "variants": ["..."], "kind": "person"{zh_example}}}]}}

- `sections`: the major topics in order. `start` is the number of the unit where the topic begins, and the numbers must increase.{first_note} Aim for {low} to {high} sections; start a section only at a real topic shift, not for a single remark. `title` is a short heading in {title_language} (at most about 20 Chinese characters or 8 English words), without numbering or `#`.{title_zh_note}
- `terms`: at most {max_terms} names of people, organizations and products, and technical terms, that recur or matter. `term` is the correct spelling. `variants` lists other spellings of the same thing that actually appear in the transcript (speech-recognition errors, inconsistent spellings); leave it empty unless you are confident. `kind` is one of person, organization, product, term.{zh_note}{hints}

## Transcript

{numbered}
"""

HINTS_BLOCK = """

## Video Chapters

The video has its own chapters, listed below at the unit where each one starts. Keep each chapter as a section starting at or near that unit (move the start a few units if the topic clearly begins elsewhere), split a long chapter into several sections where the topic changes, and write the titles in {title_language}.

{lines}"""

OUTLINE_ROUND_BLOCK = """## Outline (section titles for this part of the transcript)

{lines}"""


def outline_supported(prompt_name: str) -> bool:
    return prompt_name in OUTLINE_PROMPTS


def outline_enabled(config: dict | None) -> bool:
    import yt_transcript_utils as utils

    return utils._parse_bool((config or {}).get("window_outline", True), True)


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
    hint_lines = [f"- [S{hint['start']}] {hint['title']}" for hint in hints or []
                  if start < int(hint["start"]) <= end]
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
                    and variant not in variants:
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
    import yt_transcript_utils as utils

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
        except utils.LLMRequestError as error:
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
    record.update({"status": "ok", **merged})
    return record


# ---------------------------------------------------------------------------
# Chapter hints
# ---------------------------------------------------------------------------

def hints_from_times(chapters: list[dict], cue_starts: list[tuple[int, float]],
                     unit_offsets: list[int | None]) -> list[dict]:
    """Map YouTube chapter start times to source units through the subtitle cues.

    `cue_starts` is (offset in the whitespace-free cue text, start time) per
    cue; `unit_offsets` the offset where each unit starts in that text, or
    None for units that could not be located.
    """
    located = [(offset, index) for index, offset in enumerate(unit_offsets) if offset is not None]
    if not located or not cue_starts:
        return []
    hints = []
    for chapter in chapters:
        title = clean_title(chapter.get("title"))
        start_time = chapter.get("start_time")
        if not title or not isinstance(start_time, (int, float)):
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
        hints.append({"start": unit_index + 1, "title": title})
    return hints


def hints_from_chunk_plan(chapter_plan: list, chunk_starts: dict) -> list[dict]:
    """Chapter hints from chapter_plan.json, at the first unit of each chapter's start chunk."""
    hints = []
    for chapter in chapter_plan if isinstance(chapter_plan, list) else []:
        if not isinstance(chapter, dict):
            continue
        start = (chunk_starts or {}).get(str(chapter.get("start_chunk")))
        title = clean_title(chapter.get("title_en") or chapter.get("title") or chapter.get("title_zh"))
        if start and title and (not hints or int(start) > hints[-1]["start"]):
            hints.append({"start": int(start), "title": title})
    return hints


# ---------------------------------------------------------------------------
# Use in window rounds
# ---------------------------------------------------------------------------

def round_outline_text(sections: list[dict], cursor: int, window_end: int, next_section: int = 0) -> str:
    """The outline lines a round needs: the section it starts in, those starting in its window, and the next.

    Sections from `next_section` on have no heading yet; the model is asked
    to write the heading of those that begin in its window.
    """
    lines = []
    current = None
    for index, section in enumerate(sections):
        if section["start"] <= cursor + 1:
            current = index
    following = None
    for index, section in enumerate(sections):
        start = section["start"]
        if index == current and index < next_section:
            lines.append(f"- [S{start}] {section['title']} (the section [S{cursor + 1}] is in; already headed)")
        elif index >= next_section and (index == current or cursor + 1 < start <= window_end):
            lines.append(f"- [S{start}] {section['title']} (starts in this window: where this topic begins, at or "
                         f"right around [S{start}], start a new paragraph and put the line `## {section['title']}` "
                         "before it)")
        elif start > window_end and following is None:
            following = index
            lines.append(f"- [S{start}] {section['title']} (next section, after this window)")
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


def place_headings(sections: list[dict], next_section: int, text: str, start: int, end: int) -> tuple[str, list, int]:
    """Put the outline's headings into one committed paragraph group (units start..end, 1-based).

    The model is asked to write a section's title, as given, where the topic
    really begins near the section's first unit. Such a heading is kept (in
    the canonical form); any other header line is removed. A section the
    model did not head gets its heading from the program, before the first
    paragraph whose midpoint reaches the section start. Returns the text, the
    starts of the sections placed here, and the next unplaced section index.
    """
    placed: list[int] = []
    out_lines: list[str] = []
    model_placed = False
    for line in (text or "").split("\n"):
        if not re.match(r"^[ \t]*#{1,6}[ \t]+", line):
            out_lines.append(line)
            continue
        key = _title_key(clean_title(line))
        match = None
        for index in range(next_section, len(sections)):
            section = sections[index]
            near = start - HEADING_TOLERANCE_UNITS <= section["start"] <= end + HEADING_TOLERANCE_UNITS
            if near and key and key in {_title_key(section["title"]), _title_key(section.get("title_zh", ""))}:
                match = index
                break
        if match is None:
            continue
        # Sections skipped before this one get their headings here too, in order.
        out_lines.append("\n\n".join(heading_block(section) for section in sections[next_section:match + 1]))
        placed.extend(section["start"] for section in sections[next_section:match + 1])
        next_section = match + 1
        model_placed = True
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    if not model_placed:
        last = sections_before(sections, next_section, start, end)
        if last > next_section:
            body = "\n\n".join([heading_block(section) for section in sections[next_section:last]] + [body])
            placed.extend(section["start"] for section in sections[next_section:last])
            next_section = last
    return body, placed, next_section


def sections_before(sections: list[dict], next_section: int, start: int, end: int) -> int:
    """Return the index past the sections whose heading goes before the paragraph covering units start..end (1-based).

    A section heading goes before the first paragraph whose midpoint is at or
    after the section's first unit, so a section start that falls late in a
    paragraph moves to the next paragraph instead of splitting it.
    """
    midpoint = (start + end) / 2
    index = next_section
    while index < len(sections) and sections[index]["start"] <= midpoint:
        index += 1
    return index
