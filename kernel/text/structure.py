"""Put section headings and paragraph ends at sentence boundaries of the written text.

The window rounds work on source units, and a unit of unpunctuated subtitles
is a cut at a cue pause, so it often starts or ends inside a sentence.
Sentence boundaries only exist once the model has punctuated the text, so the
program places the structure after the rounds, on the raw stage's output:

- a paragraph that stops inside a sentence (usually where one round ended)
  takes the rest of that sentence from the next paragraph, or hands its
  unfinished sentence over to it, whichever piece is shorter;
- a heading the rounds wrote right after such a paragraph moves before the
  start of the sentence it interrupted;
- each outline section's heading goes before the sentence that holds the
  section's anchor, found in the written text near the section's unit; when
  it is not found there, before the first paragraph whose middle reaches the
  section's unit.

Units only say how far the rounds have read; they are not taken as sentences.
"""

from __future__ import annotations

import re

from kernel.text.outline import (
    ANCHOR_PROBE_KEY,
    HEADING_TOLERANCE_UNITS,
    MIN_ANCHOR_KEY,
    heading_block,
    key_chars,
    text_weight,
)

CLOSERS = "\"'”’」』）)]】》*_"
# A paragraph ending like this finishes its last sentence.
SENTENCE_END = re.compile(r"[。！？!?.…；;][\"'”’」』）)\]】》*_]*$")
# A paragraph may also end on a dash (speech broken off, often where the
# speaker changes) or a colon (the next paragraph is what it introduces).
PARAGRAPH_END = re.compile(r"(?:[。！？!?.…；;]|[—–-]|[:：])[\"'”’」』）)\]】》*_]*$")
# Where a sentence ends inside a paragraph; a heading goes after one of these.
FULL_STOPS = "。！？!?…"
# The longest piece of a sentence that is moved to mend a paragraph end
# (weighing an English word 2 and a Chinese character 1); a longer one is left.
MAX_MOVED_WEIGHT = 150
LIST_OR_QUOTE = re.compile(r"^(?:[-*+]\s|\d+\.\s|>|```|~~~|\||---)")


def sentence_ends(text: str) -> list[int]:
    """Positions just past each sentence end in `text`, closing quotes and brackets included.

    A period counts only before a space, a closing mark or the end, so
    Alibaba.com and 3.5 are not sentence ends.
    """
    ends = []
    index = 0
    while index < len(text):
        char = text[index]
        after = text[index + 1:index + 2]
        if char in FULL_STOPS or (char == "." and (not after or after.isspace() or after in CLOSERS)):
            index += 1
            while index < len(text) and (text[index] in FULL_STOPS or text[index] == "."):
                index += 1
            while index < len(text) and text[index] in CLOSERS:
                index += 1
            ends.append(index)
            continue
        index += 1
    return ends


def _join(left: str, right: str) -> str:
    """Join two pieces of one sentence; a space only between two ASCII characters."""
    space = left[-1:].isascii() and right[:1].isascii() and not left[-1:].isspace()
    return left + (" " if space else "") + right


def _kind(text: str) -> str:
    if text.startswith("#"):
        return "heading"
    return "other" if LIST_OR_QUOTE.match(text) else "body"


def _blocks(paragraphs: list[dict]) -> list[dict]:
    """Split committed paragraphs into blocks, each with the stretch of units it roughly covers.

    A paragraph covering units start..end (1-based) spans [start, end + 1);
    its blocks share that span in proportion to their length.
    """
    blocks = []
    for paragraph in paragraphs:
        parts = []
        for part in re.split(r"\n\s*\n", paragraph["text"] or ""):
            lines = part.strip().split("\n")
            # Header lines at the top of a block are a block of their own.
            count = 0
            while count < len(lines) and lines[count].lstrip().startswith("#"):
                count += 1
            for piece in ("\n".join(lines[:count]).strip(), "\n".join(lines[count:]).strip()):
                if piece:
                    parts.append(piece)
        total = sum(len(part) for part in parts) or 1
        start, span = paragraph["start"], paragraph["end"] - paragraph["start"] + 1
        done = 0
        for part in parts:
            low = start + span * done / total
            done += len(part)
            blocks.append({"text": part, "kind": _kind(part), "low": low, "high": start + span * done / total})
    return blocks


def _mend_sentences(blocks: list[dict], stats: dict) -> None:
    """Move pieces of sentences so that no paragraph stops inside one.

    Only a piece cut off at a sentence end the model wrote is moved: in text
    without punctuation nothing shows where the sentence starts or ends, and
    the paragraph is left as it is.
    """
    index = 0
    while index < len(blocks) - 1:
        block = blocks[index]
        if block["kind"] != "body" or PARAGRAPH_END.search(block["text"]):
            index += 1
            continue
        following = index + 1
        while following < len(blocks) and blocks[following]["kind"] == "heading":
            following += 1
        if following >= len(blocks) or blocks[following]["kind"] != "body":
            index += 1
            continue
        nxt = blocks[following]
        ends = sentence_ends(block["text"])
        kept = block["text"][:ends[-1]].rstrip() if ends else ""
        tail = block["text"][len(kept):].strip()
        if following > index + 1:
            # A heading after an unfinished sentence moves before that sentence.
            if kept:
                block["text"] = kept
                nxt["text"] = _join(tail, nxt["text"])
                stats["moved_headings"] += 1
            else:
                stats["unfinished_paragraphs"] += 1
            index += 1
            continue
        next_ends = sentence_ends(nxt["text"])
        head = nxt["text"][:next_ends[0]].strip() if next_ends else ""
        pieces = [(text_weight(piece), name) for piece, name in ((tail, "tail"), (head, "head"))
                  if (kept if name == "tail" else head) and text_weight(piece) <= MAX_MOVED_WEIGHT]
        if not pieces:
            stats["unfinished_paragraphs"] += 1
        elif min(pieces)[1] == "tail":
            block["text"] = kept
            nxt["text"] = _join(tail, nxt["text"])
            stats["mended_sentences"] += 1
        else:
            block["text"] = _join(block["text"], head)
            rest = nxt["text"][next_ends[0]:].strip()
            if rest:
                nxt["text"] = rest
            else:
                del blocks[following]
            stats["mended_sentences"] += 1
        index += 1


def _distance(block: dict, start: int) -> float:
    middle = start + 0.5
    if block["low"] <= middle < block["high"]:
        return 0.0
    return min(abs(block["low"] - middle), abs(block["high"] - middle))


def find_anchor(blocks: list[dict], first: int, start: int, anchor: str) -> tuple[int, int] | None:
    """Find a section's anchor in body blocks from `first` on that cover units near `start` (1-based).

    Letters and digits are compared, like in `outline.locate_words`, in the
    blocks nearest the section's unit first. When the whole anchor is not
    found (the model fixed a word or dropped a filler), its first and then its
    last ANCHOR_PROBE_KEY characters are tried. Returns (block index,
    position in that block) of where the part found begins, or None.
    """
    key, _ = key_chars(anchor)
    if len(key) < MIN_ANCHOR_KEY:
        return None
    probes = [key]
    if len(key) > ANCHOR_PROBE_KEY:
        probes += [key[:ANCHOR_PROBE_KEY], key[-ANCHOR_PROBE_KEY:]]
    candidates = sorted(
        (index for index in range(first, len(blocks)) if blocks[index]["kind"] == "body"
         and blocks[index]["low"] < start + 1 + HEADING_TOLERANCE_UNITS
         and blocks[index]["high"] > start - HEADING_TOLERANCE_UNITS),
        key=lambda index: (_distance(blocks[index], start), index))
    for probe in probes:
        for index in candidates:
            compact, positions = key_chars(blocks[index]["text"])
            found = compact.find(probe)
            if found >= 0:
                return index, positions[found]
    return None


def _place_sections(blocks: list[dict], sections: list[dict], stats: dict) -> None:
    """Insert each outline section's heading at the start of a sentence, in order."""
    first = 0
    for section in sections:
        start = int(section["start"])
        at = None
        found = find_anchor(blocks, first, start, section["anchor"]) if section.get("anchor") else None
        if found:
            index, position = found
            block = blocks[index]
            cut = max([end for end in sentence_ends(block["text"]) if end <= position], default=0)
            before, after = block["text"][:cut].rstrip(), block["text"][cut:].lstrip()
            if before and after:
                middle = block["low"] + (block["high"] - block["low"]) * len(before) / len(block["text"])
                blocks[index] = dict(block, text=before, high=middle)
                blocks.insert(index + 1, dict(block, text=after, low=middle))
                index += 1
            at = index
            stats["headings_at_sentence"] += 1
        else:
            # Before the first paragraph whose middle is at or past the section's unit, so the
            # heading never goes into a paragraph whose sentences the program cannot see.
            at = next((index for index in range(first, len(blocks)) if blocks[index]["kind"] == "body"
                       and start <= (blocks[index]["low"] + blocks[index]["high"] - 1) / 2), None)
            if at is None:
                stats["unplaced_sections"] += 1
                continue
            stats["headings_between_paragraphs"] += 1
        low = blocks[at]["low"]
        blocks.insert(at, {"text": heading_block(section), "kind": "heading", "low": low, "high": low})
        first = at + 1


def structure_paragraphs(paragraphs: list[dict], sections: list[dict] | None = None) -> tuple[str, dict]:
    """Join a run's committed paragraphs into one text, with structure at sentence boundaries.

    `paragraphs` are {"text", "start", "end"} with the 1-based units each
    covers, in order; `sections` the outline's sections, whose headings are
    added here (the rounds of a run with an outline write none). Returns the
    text and counts of what was moved or placed.
    """
    stats = {"mended_sentences": 0, "unfinished_paragraphs": 0, "moved_headings": 0}
    blocks = _blocks([paragraph for paragraph in paragraphs if (paragraph.get("text") or "").strip()])
    _mend_sentences(blocks, stats)
    if sections:
        stats.update({"headings_at_sentence": 0, "headings_between_paragraphs": 0, "unplaced_sections": 0})
        _place_sections(blocks, sections, stats)
    return "\n\n".join(block["text"] for block in blocks), stats
