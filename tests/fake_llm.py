#!/usr/bin/env python3
"""Deterministic stand-in for an agent CLI, for end-to-end tests.

Plugged in through the agent CLI setting (`llm_agent_cli` or
YT_TRANSCRIPT_AGENT_CLI = "python3 tests/fake_llm.py"); the prompt arrives on
stdin. It answers the requests the pipeline makes:

- the backend probe ("Reply with OK only.") with OK;
- an outline pass with a valid outline JSON: one section every
  SECTION_EVERY units (YouTube chapter hints win when given, with
  placeholder chapter titles replaced), no terms;
- a window round by copying its `[Sn]` units back unchanged, roughly as many
  characters as the round asks for, in paragraphs of PARAGRAPH_UNITS units,
  with `## ` headings from the round's outline block (or one placeholder
  heading every SECTION_EVERY units when there is no outline), then
  `<!--END-->`;
- a translation round (paragraph units) by keeping each English paragraph
  and adding a recognizable placeholder Chinese line under it.

Any other prompt exits 1, so a fallback path the test did not expect fails
loudly. With FAKE_LLM_LOG set, each call appends one JSON line describing it.
With FAKE_LLM_FAIL_FROM_UNIT=<n>, a window round starting at unit n or later
exits 1, like an LLM outage in the middle of a run.
"""

from __future__ import annotations

import json
import os
import re
import sys

SECTION_EVERY = 40
PARAGRAPH_UNITS = 4
PROBE_PROMPT = "Reply with OK only."
UNIT_START = re.compile(r"^\[S(\d+)\] ?", re.MULTILINE)
OUTLINE_LINE = re.compile(r"^- \[S(\d+)\] (.+?)(?: \((?:the section|starts in this window|next section)[^)]*\))?$",
                          re.MULTILINE)
ZH_CHARS = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方"
            "后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它"
            "合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条")
HINT_LINE = re.compile(r"^- \[S(\d+)\] (.+)$", re.MULTILINE)


def log(entry: dict) -> None:
    path = os.environ.get("FAKE_LLM_LOG", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def units_after(text: str, first: int) -> list[tuple[int, str]]:
    """The `[Sn]` units from `[S<first>]` on, as (number, text) pairs."""
    start = re.search(rf"^\[S{first}\] ?", text, re.MULTILINE)
    if not start:
        return []
    body = text[start.start():]
    matches = list(UNIT_START.finditer(body))
    units = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        units.append((int(match.group(1)), body[match.end():end].strip()))
    return units


def placeholder_translation(number: int, text: str) -> str:
    """A recognizable Chinese stand-in, about as long as a real translation.

    Each English word maps to one character, so the text is not one phrase
    repeated (which verify-quality would flag) and stays deterministic.
    """
    words = re.findall(r"[A-Za-z0-9']+", text)
    body = "".join(ZH_CHARS[sum(map(ord, word)) % len(ZH_CHARS)] for word in words)
    return f"（占位译文 S{number}）{body}。"


def zh_title(index: int) -> str:
    return f"第{index}部分"


def outline(prompt: str) -> str:
    bilingual = '"title_zh"' in prompt
    transcript = prompt.split("## Transcript", 1)[1]
    numbers = [int(n) for n in UNIT_START.findall(transcript)]
    first = numbers[0]
    hints = []
    if "## Video Chapters" in prompt:
        block = prompt.split("## Video Chapters", 1)[1].split("## Transcript", 1)[0]
        hints = [(int(n), title.strip()) for n, title in HINT_LINE.findall(block)]
    starts = [n for n, _ in hints] or list(range(first, numbers[-1] + 1, SECTION_EVERY))
    sections = []
    for index, start in enumerate(starts, start=1):
        title = hints[index - 1][1] if hints else ""
        if not title or title.startswith("<Untitled Chapter"):
            # The outline prompt asks to replace yt-dlp's placeholder chapter titles.
            title = f"Part {start}" if bilingual else zh_title(start)
        section = {"start": start, "title": title}
        if bilingual:
            section["title_zh"] = zh_title(start)
        sections.append(section)
    log({"kind": "outline", "first": first, "last": numbers[-1], "sections": len(sections)})
    return json.dumps({"sections": sections, "terms": []}, ensure_ascii=False)


def window_round(prompt: str) -> str:
    first = int(re.search(r"- Start at \[S(\d+)\]", prompt).group(1))
    fail_from = int(os.environ.get("FAKE_LLM_FAIL_FROM_UNIT") or 0)
    if fail_from and first >= fail_from:
        log({"kind": "failed", "first": first})
        raise SystemExit(f"fake_llm: simulated outage at [S{first}]")
    units = units_after(prompt.split("## Sliding-Window Protocol", 1)[1], first)
    if not units:
        raise SystemExit("fake_llm: window prompt has no units")
    finish = "is the end of the transcript. Process every" in prompt
    target = re.search(r"roughly (\d+) characters", prompt)
    budget = int(target.group(1)) if target and not finish else 0
    paragraph_units = "Every source paragraph starts" in prompt
    headings = {int(n): title for n, title in OUTLINE_LINE.findall(prompt.split("## Outline", 1)[1])} \
        if "## Outline (section titles" in prompt else {}

    written = []
    chars = 0
    for count, (number, text) in enumerate(units):
        if budget and count and chars >= budget:
            break
        written.append((number, text))
        chars += len(text)
    out = []
    paragraph: list[str] = []
    for count, (number, text) in enumerate(written):
        heading = ""
        if paragraph_units:
            pass  # translation keeps the structure it is given
        elif headings:
            heading = headings.get(number, "")
        elif "Put `##` headers" in prompt and (number - 1) % SECTION_EVERY == 0:
            heading = f"Part {number}" if re.search(r"[A-Za-z]{3,}", text) and not re.search(r"[一-鿿]", text) \
                else zh_title(number)
        if heading or len(paragraph) >= PARAGRAPH_UNITS:
            if paragraph:
                out.append(" ".join(paragraph))
            paragraph = []
        if heading:
            out.append(f"## {heading}")
        if paragraph_units:
            # A heading unit stays as is (translate_only keeps headers in English).
            out.append(f"[S{number}] {text}" if text.startswith("#") and "\n" not in text
                       else f"[S{number}] {text}\n\n{placeholder_translation(number, text)}")
        else:
            paragraph.append(f"[S{number}] {text}")
    if paragraph:
        out.append(" ".join(paragraph))
    log({"kind": "translate" if paragraph_units else "window", "first": written[0][0], "last": written[-1][0],
         "headings": sum(1 for line in out if line.startswith("## "))})
    return "\n\n".join(out) + "\n\n<!--END-->"


def main() -> int:
    prompt = sys.stdin.read()
    if prompt.strip() == PROBE_PROMPT:
        log({"kind": "probe"})
        print("OK")
        return 0
    if prompt.startswith("# Transcript Outline"):
        print(outline(prompt))
        return 0
    if "## Sliding-Window Protocol" in prompt:
        print(window_round(prompt))
        return 0
    log({"kind": "unexpected", "head": prompt[:200]})
    print("fake_llm: unexpected prompt (not a probe, outline or window round)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
