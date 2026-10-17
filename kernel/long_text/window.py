"""Sliding-window long-text executor (`long_text_mode: window`).

Instead of cutting the source into fixed chunks up front, the executor keeps a
cursor into numbered source units and moves it forward round by round:

1. Every source unit gets a number (`[S12]`). On the raw stage a unit is a
   sentence; on the processed stage (translation of the structured text) it
   is a paragraph of the previous stage's output.
2. Each round sends a read window starting at the cursor, plus the tail of
   what was already written as left context, and asks for about N tokens of
   output that stop at a paragraph or section end.
3. The model ends every paragraph with `<!--S<n>-->`, the number of the last
   source unit that paragraph covers.
4. Complete paragraphs are written to disk, a trailing paragraph without a
   marker is dropped, and the cursor moves past the last marker.

Progress is read only from the markers, so a cut-off response is not a
failure, it just advances less; nothing depends on `finish_reason` or
`max_tokens`, and the same loop works for API and agent-CLI backends. When a
response has no usable markers (or an implausible amount of output) twice in
a row, that stretch of the source is processed once with the plain
fixed-chunk prompt instead.

Window sizes adapt per round. The write target N starts at
`window_write_tokens`; the read window is sized from N and the measured
output/source ratio so that a round reads only a little more than it is
expected to write, which keeps re-reading low. The last stretch of the source
is sent whole when one round can finish it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path

from ..task_runtime import lifecycle as runtime_lifecycle
from ..task_runtime import runtime as kernel_runtime
from ..task_runtime import state as kernel_state
from . import agent_cli as kernel_agent_cli
from . import glossary as kernel_glossary

LONG_TEXT_MODES = ("chunks", "window")
DEFAULT_LONG_TEXT_MODE = "chunks"
RAW_STAGE = "raw_path"
PROCESSED_STAGE = "processed_path"
# Prompts the window executor runs, per input stage.
WINDOW_PROMPTS = {
    RAW_STAGE: ("cleanup_zh", "structure_only", "quick_cleanup"),
    PROCESSED_STAGE: ("translate_only",),
}

DEFAULT_WINDOW_READ_TOKENS = 6000
DEFAULT_WINDOW_WRITE_TOKENS = 3000
DEFAULT_WINDOW_TAIL_TOKENS = 400
DEFAULT_WINDOW_MARKER_RETRIES = 1
# Raw sentences longer than this are split so one number never covers a huge
# unpunctuated run (common in auto-generated Chinese captions).
DEFAULT_WINDOW_SENTENCE_MAX_TOKENS = 120
# A forced-split remainder smaller than this share of the cap joins the piece before it.
MIN_SPLIT_PIECE_SHARE = 0.25
# A cut at a subtitle cue pause should leave at least this share of the cap,
# unless no cue end in range does.
MIN_CUE_PIECE_SHARE = 0.5
# Processed-stage units are paragraphs; only very long ones are split.
DEFAULT_WINDOW_PARAGRAPH_MAX_TOKENS = 600

# Adaptive sizing. The read window is the source the write target is expected
# to consume, plus this much look-ahead so the model can end at a natural break.
READ_LOOKAHEAD = 1.25
MIN_READ_TOKENS = 400
MIN_WRITE_TOKENS = 300
# Share of the output ceiling a write target may use.
WRITE_CEILING_SHARE = 0.75
WRITE_SHRINK = 0.75
WRITE_GROW = 1.1
CLEAN_ROUNDS_BEFORE_GROW = 2
# A round that took more than this share of the request timeout shrinks N.
SLOW_ROUND_SHARE = 0.5
RATIO_EMA_WEIGHT = 0.5
# Output/source token ratio outside [LOW, HIGH] x expected means the model
# skipped or invented content; the round is rejected like a marker failure.
LOW_ROUND_RATIO = 0.4
HIGH_ROUND_RATIO = 2.5
# Rounds advancing fewer source tokens than this are not ratio-checked.
RATIO_CHECK_MIN_TOKENS = 200
TRUNCATION_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}

WINDOW_DIR_NAME = "window"
PROCESSED_DIR_NAME = "processed"
STATE_FILE_NAME = "state.json"
STATE_SCHEMA_VERSION = 2
EXECUTOR_MANIFEST_KEY = "long_text_executor"
STAGE_MANIFEST_KEY = "long_text_window_stage"

# Legacy per-paragraph markers; still stripped from output if a model writes them.
MARKER_PATTERN = re.compile(r"<!--\s*S(\d+)\s*-->")
SOURCE_TAG_PATTERN = re.compile(r"\[S\d+\]\s?")
SOURCE_TAG_NUMBER = re.compile(r"\[S(\d+)\]")
END_PATTERN = re.compile(r"<!--\s*END\s*-->")
CODE_FENCE_LINE = re.compile(r"^```[\w-]*[ \t]*$", re.MULTILINE)
# In the coverage report, a stretch keeping less than this share of the run's
# typical output/source ratio is listed as a possible skip (advisory only).
LOW_OUTPUT_RATIO = 0.5

WINDOW_PROTOCOL = """## Sliding-Window Protocol

The input below is one window of a longer transcript. Every source {unit} starts with a number tag like [S12].

- Start at [S{first}].{done_note}
- {write_note}
- Keep every [S<n>] tag in the output: write it right before the text that comes from that source {unit}, even in the middle of a sentence or paragraph. Do not add, drop or reorder tags, and cover the {units} in order without skipping any.
- The tags are bookkeeping only; source {units} may start mid-sentence. Break paragraphs and place headers by meaning, never just because a tag starts there.
- Separate paragraphs with a blank line, and keep them as short as the task asks: split a long passage into several paragraphs rather than one block. Put `##` headers on their own line.{task_note}
- When you stop, end the output with a line <!--END--> after the last complete paragraph.
- Output only the processed text, the tags and that end line."""

TASK_PROTOCOL_NOTES = {
    # Window rounds see many sentences at once and tend to merge a whole
    # exchange into one paragraph; keep dialogue readable.
    "structure_only": (
        "\n- Start a new paragraph whenever the speaker changes (for example where the source has `-` or `>>`): "
        "each speaker's turn is its own paragraph, separated by a blank line."
    ),
    "translate_only": (
        "\n- For each source paragraph, write its tag and the English paragraph unchanged, then a blank line "
        "and its Chinese translation. Copy `##` headers as they are."
    ),
}

ALREADY_WRITTEN_BLOCK = """## Already Written (context only, do not repeat)

{tail}"""

PREVIOUS_CONTEXT_BLOCK = """## Previous Output (context only, do not repeat)

{tail}"""


def resolve_long_text_mode(config: dict | None) -> str:
    """Return `window` or `chunks` from config, defaulting to `chunks`."""
    value = str((config or {}).get("long_text_mode", DEFAULT_LONG_TEXT_MODE) or "").strip().lower()
    return value if value in LONG_TEXT_MODES else DEFAULT_LONG_TEXT_MODE


def _stage(input_key: str) -> str:
    return PROCESSED_STAGE if (input_key or RAW_STAGE) == PROCESSED_STAGE else RAW_STAGE


def window_supported(prompt_name: str, input_key: str = RAW_STAGE, work_dir: str = "") -> bool:
    """Return whether the window executor handles this prompt and input stage.

    The processed stage reads the raw stage's window output, so it is only
    supported when the raw stage of `work_dir` finished in window mode.
    """
    stage = _stage(input_key)
    if prompt_name not in WINDOW_PROMPTS[stage]:
        return False
    if stage == RAW_STAGE:
        return True
    return bool(work_dir) and manifest_uses_window(work_dir) and \
        load_window_state(work_dir, RAW_STAGE).get("status") == "done"


def stage_dir_for(work_dir: str, input_key: str = RAW_STAGE) -> Path:
    base = Path(work_dir) / WINDOW_DIR_NAME
    return base / PROCESSED_DIR_NAME if _stage(input_key) == PROCESSED_STAGE else base


def window_dir_for(work_dir: str) -> Path:
    return stage_dir_for(work_dir, RAW_STAGE)


def state_path_for(work_dir: str, input_key: str = RAW_STAGE) -> Path:
    return stage_dir_for(work_dir, input_key) / STATE_FILE_NAME


def _read_manifest(work_dir: str) -> dict:
    try:
        manifest = json.loads((Path(work_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def final_stage(work_dir: str) -> str:
    """Return the stage of the latest window run, whose output merge-content uses."""
    return _stage(_read_manifest(work_dir).get(STAGE_MANIFEST_KEY, RAW_STAGE))


def load_window_state(work_dir: str, input_key: str | None = None) -> dict:
    """Load a stage's window state (default: the latest stage), or `{}`."""
    stage = final_stage(work_dir) if input_key is None else _stage(input_key)
    path = state_path_for(work_dir, stage)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def manifest_uses_window(work_dir: str) -> bool:
    """Return whether the latest executor run in this work dir was the window one."""
    return _read_manifest(work_dir).get(EXECUTOR_MANIFEST_KEY) == "window"


def set_manifest_executor(work_dir: str, executor: str, stage: str = "") -> None:
    """Record which executor (and window stage) owns the work dir's output."""
    manifest_path = Path(work_dir) / "manifest.json"
    manifest = _read_manifest(work_dir)
    if not manifest:
        return
    updates = {EXECUTOR_MANIFEST_KEY: executor}
    if executor == "window" and stage:
        updates[STAGE_MANIFEST_KEY] = _stage(stage)
    current = {key: manifest.get(key, "chunks" if key == EXECUTOR_MANIFEST_KEY else None) for key in updates}
    if current == updates:
        return
    manifest.update(updates)
    kernel_state.write_manifest(manifest_path, manifest)


# ---------------------------------------------------------------------------
# Source units
# ---------------------------------------------------------------------------

def load_source_chunks(work_dir: str) -> list[tuple[int, str]]:
    """Return `(chunk_id, raw_text)` for the manifest's active raw chunks, in order."""
    import yt_transcript_utils as utils

    work_path = Path(work_dir)
    manifest = json.loads((work_path / "manifest.json").read_text(encoding="utf-8"))
    chunks = []
    for chunk in manifest.get("chunks", []):
        if not isinstance(chunk, dict) or chunk.get("status") == utils.SUPERSEDED_CHUNK_STATUS:
            continue
        raw_name = str(chunk.get("raw_path", "") or "").strip()
        raw_path = work_path / raw_name
        if raw_name and raw_path.is_file():
            text = raw_path.read_text(encoding="utf-8").strip()
            if text:
                chunks.append((int(chunk.get("id", len(chunks))), text))
    return chunks


def load_cue_stream(work_dir: str) -> dict | None:
    """Read the subtitle cues behind the raw text, for cutting long units at cue pauses.

    Returns the cues' text with all whitespace removed and, for each cue end
    inside it, the silence before the next cue (0 when timings are missing or
    the cues touch). None when the work dir has no timed segments.
    """
    path_value = str(_read_manifest(work_dir).get("normalized_document_path", "") or "").strip()
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(work_dir) / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list):
        return None

    parts: list[str] = []
    pauses: dict[int, float] = {}
    offset = 0
    previous_end = None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = re.sub(r"\s+", "", str(segment.get("text", "") or ""))
        if not text:
            continue
        start, end = segment.get("start_time"), segment.get("end_time")
        if parts:
            pause = 0.0
            if isinstance(start, (int, float)) and isinstance(previous_end, (int, float)):
                pause = max(0.0, float(start) - float(previous_end))
            pauses[offset] = round(pause, 1)
        parts.append(text)
        offset += len(text)
        previous_end = end if isinstance(end, (int, float)) else None
    if not pauses:
        return None
    return {"text": "".join(parts), "pauses": pauses}


def _new_split_report() -> dict:
    return {"cue_alignment": "none", "aligned_chunks": 0, "unaligned_chunks": 0,
            "cue_cuts": 0, "hard_cuts": 0}


def _split_at_cues(sentence: str, pauses: dict[int, float], max_tokens: int,
                   config: dict | None, report: dict) -> list[str]:
    """Cut an over-long unit only between subtitle cues, preferring the longest pauses.

    `pauses` maps a cut position (counted in non-whitespace characters of
    `sentence`) to the silence there. Each piece stays within `max_tokens`
    except a single cue longer than that, which is hard-cut.
    """
    import yt_transcript_utils as utils

    def tokens(text: str) -> int:
        return utils._estimate_tokens(text, "tokens", config)

    visible = [index for index, char in enumerate(sentence) if not char.isspace()]
    cuts = sorted((visible[offset], pause) for offset, pause in pauses.items() if 0 < offset < len(visible))
    floor = max_tokens * MIN_CUE_PIECE_SHARE
    pieces: list[str] = []
    start = 0
    last_cut = ""
    while tokens(sentence[start:]) > max_tokens:
        fitting = []
        for cut, pause in cuts:
            if cut <= start:
                continue
            size = tokens(sentence[start:cut])
            if size > max_tokens:
                break
            fitting.append((cut, pause, size))
        if not fitting:
            # One cue alone is over the cap: hard-cut it. Its last piece is
            # kept with what follows, so the next cut is at a cue end again.
            next_cut = next((cut for cut, _ in cuts if cut > start), len(sentence))
            hard = [part for part in utils._force_split_text_by_tokens(sentence[start:next_cut], max_tokens, config)
                    if part]
            if len(hard) < 2:
                pieces.append(sentence[start:next_cut])
                start = next_cut
                if next_cut < len(sentence):
                    last_cut = "cue_cuts"
                    report["cue_cuts"] += 1
                continue
            pieces.extend(hard[:-1])
            report["hard_cuts"] += len(hard) - 1
            last_cut = "hard_cuts"
            start = next_cut - len(hard[-1])
            continue
        # Prefer the longest pause among cuts that leave a reasonably sized
        # piece; on equal pauses take the later cut so units stay large.
        eligible = [item for item in fitting if item[2] >= floor] or fitting
        cut = max(eligible, key=lambda item: (item[1], item[0]))[0]
        pieces.append(sentence[start:cut])
        report["cue_cuts"] += 1
        last_cut = "cue_cuts"
        start = cut
    pieces.append(sentence[start:])
    pieces = [piece for piece in pieces if piece.strip()]
    if len(pieces) > 1 and tokens(pieces[-1]) < max_tokens * MIN_SPLIT_PIECE_SHARE:
        pieces[-2:] = [pieces[-2] + pieces[-1]]
        if last_cut:
            report[last_cut] -= 1
    return [piece.strip() for piece in pieces]


def split_source_sentences(text: str, config: dict | None = None,
                           max_sentence_tokens: int = DEFAULT_WINDOW_SENTENCE_MAX_TOKENS,
                           cue_pauses: dict[int, float] | None = None,
                           report: dict | None = None) -> list[str]:
    """Split source text into the numbered units the window protocol works on.

    A sentence over `max_sentence_tokens` is cut at subtitle cue ends when
    `cue_pauses` (cue end -> pause, counted in non-whitespace characters of
    `text`) is given, otherwise every `max_sentence_tokens` tokens.
    """
    import yt_transcript_utils as utils

    report = report if report is not None else _new_split_report()
    sentences = []
    offset = 0
    for sentence in utils._split_sentences(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence_start = offset
        offset += len(re.sub(r"\s+", "", sentence))
        if max_sentence_tokens > 0 and utils._estimate_tokens(sentence, "tokens", config) > max_sentence_tokens:
            if cue_pauses:
                local = {position - sentence_start: pause for position, pause in cue_pauses.items()
                         if sentence_start < position < offset}
                sentences.extend(_split_at_cues(sentence, local, max_sentence_tokens, config, report))
                continue
            parts = [part.strip() for part in
                     utils._force_split_text_by_tokens(sentence, max_sentence_tokens, config) if part.strip()]
            # A forced split can leave a scrap of a few characters at the end;
            # the model folds it into the paragraph before without its tag, so
            # keep it with the previous piece instead of numbering it alone.
            if len(parts) > 1 and utils._estimate_tokens(parts[-1], "tokens", config) < \
                    max_sentence_tokens * MIN_SPLIT_PIECE_SHARE:
                parts[-2:] = [parts[-2] + parts[-1]]
            report["hard_cuts"] += len(parts) - 1
            sentences.extend(parts)
        else:
            sentences.append(sentence)
    return sentences


def build_source_sentences(work_dir: str, config: dict | None = None,
                           max_sentence_tokens: int = DEFAULT_WINDOW_SENTENCE_MAX_TOKENS
                           ) -> tuple[list[str], dict, dict]:
    """Number the raw source; also return each raw chunk's first unit number and a split report.

    Each chunk is located in the subtitle cues (ignoring whitespace) so long
    unpunctuated sentences can be cut at cue pauses; a chunk that cannot be
    located falls back to fixed-size cuts. A chunk boundary stays a unit
    boundary, except one the chunker put inside a cue: those chunks are
    joined and cut again at cue ends, and the later chunk starts at the unit
    holding its first character.
    """
    report = _new_split_report()
    cues = load_cue_stream(work_dir)
    # Chunks split as one text. `members` holds (chunk_id, text, start offset
    # in the run, whitespace ignored); `pauses` is None for an unaligned chunk.
    runs: list[dict] = []
    stream_position = 0
    for chunk_id, text in load_source_chunks(work_dir):
        compact = re.sub(r"\s+", "", text)
        found = cues["text"].find(compact, stream_position) if cues else -1
        if found < 0:
            if cues:
                report["unaligned_chunks"] += 1
            runs.append({"members": [(chunk_id, text, 0)], "pauses": None})
            continue
        report["aligned_chunks"] += 1
        end = found + len(compact)
        pauses = {position - found: pause for position, pause in cues["pauses"].items() if found < position <= end}
        run = runs[-1] if runs else None
        if run and run["pauses"] is not None and run["end"] == found and found not in cues["pauses"]:
            # The chunker cut inside a cue here: keep both chunks in one run.
            offset = run["length"]
            run["members"].append((chunk_id, text, offset))
            run["pauses"].update({offset + position: pause for position, pause in pauses.items()})
        else:
            offset = 0
            run = {"members": [(chunk_id, text, 0)], "pauses": pauses}
            runs.append(run)
        run["length"] = offset + len(compact)
        run["end"] = end
        stream_position = end

    sentences: list[str] = []
    chunk_starts: dict = {}
    for run in runs:
        members = run["members"]
        text = members[0][1]
        for _, chunk_text, _ in members[1:]:
            ascii_join = text[-1:].isascii() and text[-1:].isalnum() and \
                chunk_text[:1].isascii() and chunk_text[:1].isalnum()
            text += (" " if ascii_join else "") + chunk_text
        run_units = split_source_sentences(text, config, max_sentence_tokens, run["pauses"], report)
        if not run_units:
            continue
        unit_ends, total = [], 0
        for unit in run_units:
            total += len(re.sub(r"\s+", "", unit))
            unit_ends.append(total)
        for chunk_id, _, offset in members:
            unit_index = next((index for index, unit_end in enumerate(unit_ends) if offset < unit_end),
                              len(run_units) - 1)
            chunk_starts.setdefault(str(chunk_id), len(sentences) + unit_index + 1)
        sentences.extend(run_units)
    if cues:
        report["cue_alignment"] = "full" if not report["unaligned_chunks"] else (
            "partial" if report["aligned_chunks"] else "failed")
    return sentences, chunk_starts, report


def split_paragraph_units(text: str, config: dict | None = None,
                          max_paragraph_tokens: int = DEFAULT_WINDOW_PARAGRAPH_MAX_TOKENS) -> list[str]:
    """Split structured text into paragraph units; only very long paragraphs are split further."""
    import yt_transcript_utils as utils

    units = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if not block:
            continue
        if not block.startswith("#") and max_paragraph_tokens > 0 and \
                utils._estimate_tokens(block, "tokens", config) > max_paragraph_tokens:
            units.extend(_group_sentences(split_source_sentences(block, config, 0), max_paragraph_tokens, config))
        else:
            units.append(block)
    return units


def _group_sentences(sentences: list[str], max_tokens: int, config: dict | None) -> list[str]:
    import yt_transcript_utils as utils

    groups, current, used = [], [], 0
    for sentence in sentences:
        cost = utils._estimate_tokens(sentence, "tokens", config)
        if current and used + cost > max_tokens:
            groups.append(" ".join(current))
            current, used = [], 0
        current.append(sentence)
        used += cost
    if current:
        groups.append(" ".join(current))
    return groups


def build_processed_units(work_dir: str, config: dict | None = None) -> tuple[list[str], dict]:
    """Number the raw stage's window output by paragraph for the processed stage.

    Chapter starts carry over: a chunk that started inside raw paragraph p now
    starts at p's first unit.
    """
    raw_state = load_window_state(work_dir, RAW_STAGE)
    raw_dir = stage_dir_for(work_dir, RAW_STAGE)
    units: list[str] = []
    paragraph_first_unit = []
    for entry in raw_state.get("paragraphs", []):
        paragraph_first_unit.append((int(entry.get("start", 0)), int(entry.get("end", 0)), len(units) + 1))
        units.extend(split_paragraph_units(_paragraph_text(raw_dir, entry), config))
    chunk_starts = {}
    for chunk_id, sentence_number in (raw_state.get("chunk_starts") or {}).items():
        for start, end, first_unit in paragraph_first_unit:
            if start <= int(sentence_number) <= end:
                chunk_starts[str(chunk_id)] = first_unit
                break
    return units, chunk_starts


def sentences_digest(sentences: list[str]) -> str:
    return hashlib.sha256("\n".join(sentences).encode("utf-8")).hexdigest()


def build_read_window(sentences: list[str], cursor: int, read_tokens: int, estimate) -> int:
    """Return the exclusive end index of the read window that starts at `cursor`."""
    end = cursor
    used = 0
    while end < len(sentences):
        cost = estimate(sentences[end])
        if end > cursor and used + cost > read_tokens:
            break
        used += cost
        end += 1
    return end


def number_sentences(sentences: list[str], start: int, end: int, separator: str = "\n") -> str:
    """Render sentences[start:end] with 1-based `[S<n>]` tags."""
    return separator.join(f"[S{index + 1}] {sentences[index]}" for index in range(start, end))


# ---------------------------------------------------------------------------
# Prompt and response protocol
# ---------------------------------------------------------------------------

def build_window_prompt(prompt_template: str, *, sentences: list[str], cursor: int, window_end: int,
                        tail_text: str = "", write_tokens: int = DEFAULT_WINDOW_WRITE_TOKENS,
                        write_chars: int = 0, glossary_text: str = "", prompt_name: str = "",
                        unit: str = "sentence", finish: bool = False) -> str:
    """Assemble one window request from the task prompt and the window protocol.

    `finish=True` means the window holds the rest of the transcript and the
    write target was sized to cover it, so the model is asked to finish.
    """
    import yt_transcript_utils as utils

    total = len(sentences)
    done_note = (
        f" {unit.capitalize()}s before [S{cursor + 1}] are already done; "
        "the Already Written block shows how that output ends."
        if cursor > 0 else ""
    )
    size_note = f"about {write_tokens} tokens of output (roughly {write_chars or write_tokens} characters)"
    if finish and window_end >= total:
        write_note = (f"[S{window_end}] is the end of the transcript. Process every {unit} through [S{window_end}]; "
                      f"this should take {size_note}.")
    else:
        write_note = (f"Write {size_note}, then stop at the end of the nearest paragraph or section. "
                      "You do not need to finish the window.")
        if window_end >= total:
            write_note += f" [S{window_end}] is the end of the transcript; finish it if you get there."
        else:
            write_note += f" The window ends at [S{window_end}] and the transcript continues after it."
    protocol = WINDOW_PROTOCOL.format(
        unit=unit,
        units=f"{unit}s",
        first=cursor + 1,
        done_note=done_note,
        write_note=write_note,
        task_note=TASK_PROTOCOL_NOTES.get(prompt_name, ""),
    )
    context = protocol
    if tail_text.strip():
        context += "\n\n" + ALREADY_WRITTEN_BLOCK.format(tail=tail_text.strip())
    separator = "\n\n" if unit == "paragraph" else "\n"
    return utils._build_chunk_prompt(
        prompt_template,
        number_sentences(sentences, cursor, window_end, separator),
        continuity_text=context,
        glossary_text=glossary_text,
    )


def build_fallback_prompt(prompt_template: str, chunk_text: str, *, tail_text: str = "",
                          glossary_text: str = "") -> str:
    """Assemble a plain fixed-chunk request for one stretch of the source."""
    import yt_transcript_utils as utils

    context = PREVIOUS_CONTEXT_BLOCK.format(tail=tail_text.strip()) if tail_text.strip() else ""
    return utils._build_chunk_prompt(prompt_template, chunk_text, continuity_text=context,
                                     glossary_text=glossary_text)


def parse_window_output(text: str, cursor: int, window_end: int) -> dict:
    """Split a window response into complete paragraphs from its inline [Sn] tags.

    `cursor` is the 0-based index of the first unit in the window and
    `window_end` the exclusive end, so valid tag numbers are
    `cursor + 1 .. window_end`. The model copies each unit's tag in front of
    the text that comes from it; the program, not the model, works out where
    each paragraph ends, which keeps the protocol cheap to follow.

    Blocks are grouped so that a group starts at a block opening with a tag
    (headers attach to the group after them, untagged blocks to the one
    before). A group covers units up to the one before the next group's first
    tag. Without the closing <!--END--> line the response may have been cut
    off, so its last group is dropped.
    """
    raw = text or ""
    end_match = END_PATTERN.search(raw)
    complete = end_match is not None
    if complete:
        raw = raw[:end_match.start()]
    raw = MARKER_PATTERN.sub("", raw)

    numbers = [int(n) for n in SOURCE_TAG_NUMBER.findall(raw)]
    previous = cursor
    for number in numbers:
        # A repeated tag (one unit split across two paragraphs) is fine;
        # going backwards or behind the cursor is not.
        if number <= cursor or number < previous:
            return _invalid("non_monotonic", f"tag S{number} does not move past S{previous}")
        if number > window_end:
            return _invalid("out_of_window", f"tag S{number} is past the window end S{window_end}")
        previous = number
    if not numbers:
        return _invalid("no_markers", "response has no [S<n>] tags")

    groups = []  # each: {"blocks": [...], "first": int|None, "last": int|None}
    pending_headers = []
    for block in re.split(r"\n\s*\n", raw):
        stripped = block.strip()
        if not stripped:
            continue
        tags = [int(n) for n in SOURCE_TAG_NUMBER.findall(stripped)]
        if stripped.startswith("#") and not tags:
            pending_headers.append(stripped)
            continue
        if not groups or stripped.startswith("[S"):
            groups.append({"blocks": pending_headers + [stripped], "first": tags[0] if tags else None,
                           "last": tags[-1] if tags else None})
            pending_headers = []
            continue
        group = groups[-1]
        group["blocks"].extend(pending_headers + [stripped])
        pending_headers = []
        if tags:
            group["first"] = group["first"] or tags[0]
            group["last"] = tags[-1]
    trailing_chars = sum(len(h) for h in pending_headers)

    if not complete and groups:
        trailing_chars += sum(len(b) for b in groups[-1]["blocks"])
        cut_first = groups[-1]["first"]
        groups = groups[:-1]
    else:
        cut_first = None

    paragraphs = []
    previous_last = cursor  # 1-based number of the last unit already covered
    for position, group in enumerate(groups):
        if position + 1 < len(groups):
            next_first = groups[position + 1]["first"]
        else:
            next_first = cut_first
        if next_first:
            last = next_first - 1
        else:
            last = group["last"] or previous_last
        if last <= previous_last:
            # Nothing new in this group (e.g. a tagless lead-in); fold it into the next.
            if position + 1 < len(groups):
                groups[position + 1]["blocks"] = group["blocks"] + groups[position + 1]["blocks"]
            continue
        body = SOURCE_TAG_PATTERN.sub("", "\n\n".join(group["blocks"])).strip()
        if body:
            paragraphs.append({"text": body, "start": previous_last, "end": last - 1})
            previous_last = last
    if not paragraphs:
        return _invalid("no_markers", "response has no complete paragraph")
    return {
        "ok": True,
        "paragraphs": paragraphs,
        "next_cursor": previous_last,
        "dropped_tail_chars": trailing_chars,
        "complete": complete,
        "error_type": "",
        "error": "",
    }


def _invalid(error_type: str, message: str) -> dict:
    return {"ok": False, "paragraphs": [], "next_cursor": None, "dropped_tail_chars": 0,
            "error_type": error_type, "error": message}


# ---------------------------------------------------------------------------
# Adaptive window sizing
# ---------------------------------------------------------------------------

def initial_sizing(params: dict) -> dict:
    """Starting write target, read window and output/source ratio for a run."""
    write = params["write_tokens"]
    ratio = params["expected_ratio"]
    return {
        "write_tokens": write,
        "read_tokens": _read_for(write, ratio, params),
        "ratio": ratio,
        "ratio_samples": 0,
        "clean_rounds": 0,
    }


def _read_for(write_tokens: int, ratio: float, params: dict) -> int:
    wanted = math.ceil(write_tokens / max(ratio, 0.05) * READ_LOOKAHEAD)
    return min(params["read_tokens"], max(MIN_READ_TOKENS, wanted))


def plan_round(sizing: dict, params: dict, remaining_tokens: int) -> dict:
    """Pick this round's read window and write target.

    When the rest of the source fits the read cap and its expected output
    fits the output ceiling, the round reads all of it and asks the model to
    finish, so the run does not end with a string of small rounds.
    """
    write = sizing["write_tokens"]
    read = sizing["read_tokens"]
    ratio = sizing["ratio"]
    finish_write = math.ceil(remaining_tokens * ratio * 1.1)
    write_ceiling = max(MIN_WRITE_TOKENS, int(params["max_output_tokens"] * WRITE_CEILING_SHARE))
    if remaining_tokens <= params["read_tokens"] and finish_write <= write * 1.5 \
            and finish_write <= write_ceiling:
        return {"read_tokens": remaining_tokens, "write_tokens": max(MIN_WRITE_TOKENS, finish_write), "finish": True}
    return {"read_tokens": read, "write_tokens": write, "finish": False}


def update_sizing(sizing: dict, params: dict, signals: dict) -> dict:
    """Adjust N and the read window from one accepted round's measurements.

    signals: advanced_tokens, output_tokens, exhausted (the round consumed the
    whole read window before the source end), truncated, slow.
    """
    sizing = dict(sizing)
    advanced = signals.get("advanced_tokens", 0)
    output = signals.get("output_tokens", 0)
    if advanced >= RATIO_CHECK_MIN_TOKENS and output > 0:
        observed = output / advanced
        if sizing["ratio_samples"] == 0:
            sizing["ratio"] = observed
        else:
            sizing["ratio"] = RATIO_EMA_WEIGHT * observed + (1 - RATIO_EMA_WEIGHT) * sizing["ratio"]
        sizing["ratio_samples"] += 1

    write_cap = min(params["write_tokens"], int(params["max_output_tokens"] * WRITE_CEILING_SHARE))
    if signals.get("truncated") or signals.get("slow"):
        sizing["write_tokens"] = max(MIN_WRITE_TOKENS, int(sizing["write_tokens"] * WRITE_SHRINK))
        sizing["clean_rounds"] = 0
    else:
        sizing["clean_rounds"] += 1
        if sizing["clean_rounds"] >= CLEAN_ROUNDS_BEFORE_GROW and sizing["write_tokens"] < write_cap:
            sizing["write_tokens"] = min(write_cap, math.ceil(sizing["write_tokens"] * WRITE_GROW))
            sizing["clean_rounds"] = 0
    sizing["write_tokens"] = max(MIN_WRITE_TOKENS, min(sizing["write_tokens"], max(write_cap, MIN_WRITE_TOKENS)))

    read = _read_for(sizing["write_tokens"], sizing["ratio"], params)
    if signals.get("exhausted"):
        # The model used up the window: give it more room next time.
        read = min(params["read_tokens"], max(read, math.ceil(signals.get("read_tokens", read) * READ_LOOKAHEAD)))
    sizing["read_tokens"] = read
    return sizing


def round_ratio_problem(advanced_tokens: int, output_tokens: int, expected_ratio: float) -> str:
    """Return an error type when a round's output/source ratio is implausible."""
    if advanced_tokens < RATIO_CHECK_MIN_TOKENS or expected_ratio <= 0:
        return ""
    observed = output_tokens / advanced_tokens
    if observed < expected_ratio * LOW_ROUND_RATIO:
        return "low_output_ratio"
    if observed > expected_ratio * HIGH_ROUND_RATIO:
        return "high_output_ratio"
    return ""


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

def _new_state(prompt_name: str, input_key: str, sentences: list[str], params: dict) -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "prompt_name": prompt_name,
        "input_key": _stage(input_key),
        "status": "running",
        "sentence_count": len(sentences),
        "sentences_sha256": sentences_digest(sentences),
        "cursor": 0,
        "params": params,
        "sizing": initial_sizing(params),
        "paragraphs": [],
        "rounds": [],
        "llm_backend": {},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _now_iso() -> str:
    import yt_transcript_utils as utils

    return utils._now_iso()


def _write_state(out_dir: Path, state: dict) -> None:
    state["updated_at"] = _now_iso()
    kernel_state.atomic_write_text(out_dir / STATE_FILE_NAME, json.dumps(state, ensure_ascii=False, indent=2))


def _paragraph_text(out_dir: Path, entry: dict) -> str:
    path = Path(out_dir) / str(entry.get("file", ""))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def written_tail(out_dir: Path, state: dict, tail_tokens: int, config: dict | None = None) -> str:
    """Return the end of the committed output, capped at about `tail_tokens`."""
    import yt_transcript_utils as utils

    if tail_tokens <= 0:
        return ""
    pieces = []
    used = 0
    for entry in reversed(state.get("paragraphs", [])):
        text = _paragraph_text(out_dir, entry)
        if not text:
            continue
        pieces.insert(0, text)
        used += utils._estimate_tokens(text, "tokens", config)
        if used >= tail_tokens:
            break
    return utils._truncate_tail_text_to_tokens("\n\n".join(pieces), tail_tokens, config)


def _commit_paragraphs(out_dir: Path, state: dict, paragraphs: list[dict], sentences: list[str],
                       *, mode: str, round_index: int) -> None:
    """Write paragraphs to disk, then record them and the new cursor in the state."""
    for paragraph in paragraphs:
        paragraph_id = len(state["paragraphs"]) + 1
        file_name = f"out_{paragraph_id:05d}.md"
        kernel_state.atomic_write_text(out_dir / file_name, paragraph["text"] + "\n")
        source_chars = sum(len(sentences[i]) for i in range(paragraph["start"], paragraph["end"] + 1))
        state["paragraphs"].append({
            "id": paragraph_id,
            "start": paragraph["start"] + 1,
            "end": paragraph["end"] + 1,
            "file": file_name,
            "mode": mode,
            "round": round_index,
            "source_chars": source_chars,
            "output_chars": len(paragraph["text"]),
        })
        state["cursor"] = paragraph["end"] + 1
    _write_state(out_dir, state)


def coverage_report(state: dict) -> dict:
    """Report which source units have no committed output yet."""
    total = int(state.get("sentence_count", 0) or 0)
    covered = [False] * total
    paragraphs = state.get("paragraphs", [])
    ratios = [
        int(item.get("output_chars", 0) or 0) / int(item.get("source_chars", 0))
        for item in paragraphs if int(item.get("source_chars", 0) or 0) > 0
    ]
    # Compare against this run's typical ratio, so the check works for
    # cleanup (~1x) and bilingual translation (~2x) alike.
    typical = statistics.median(ratios) if ratios else 1.0
    low_ratio = []
    for position, entry in enumerate(paragraphs):
        start = int(entry.get("start", 0) or 0)
        end = int(entry.get("end", 0) or 0)
        for number in range(max(1, start), min(total, end) + 1):
            covered[number - 1] = True
        # Paragraph breaks do not always fall on unit numbers (a long
        # sentence split mid-way can spill into the next paragraph), so the
        # ratio is taken over the paragraph and its neighbours.
        neighbours = paragraphs[max(0, position - 1):position + 2]
        source_chars = sum(int(item.get("source_chars", 0) or 0) for item in neighbours)
        output_chars = sum(int(item.get("output_chars", 0) or 0) for item in neighbours)
        if source_chars and output_chars / source_chars < LOW_OUTPUT_RATIO * typical:
            low_ratio.append({
                "paragraph": entry.get("id"),
                "sentences": f"S{start}-S{end}",
                "output_ratio": round(output_chars / source_chars, 3),
            })
    uncovered_ranges = []
    index = 0
    while index < total:
        if covered[index]:
            index += 1
            continue
        start = index
        while index < total and not covered[index]:
            index += 1
        uncovered_ranges.append(f"S{start + 1}-S{index}" if index - start > 1 else f"S{start + 1}")
    covered_count = sum(covered)
    return {
        "sentence_count": total,
        "covered_sentences": covered_count,
        "coverage_ratio": round(covered_count / total, 4) if total else 1.0,
        "uncovered_ranges": uncovered_ranges,
        "typical_output_ratio": round(typical, 3),
        "low_output_ratio_paragraphs": low_ratio,
        "fallback_paragraphs": sum(1 for entry in paragraphs if entry.get("mode") == "fallback"),
    }


def efficiency_report(state: dict) -> dict:
    """Summarize how much of what each round read it actually consumed."""
    rounds = [r for r in state.get("rounds", []) if r.get("read_tokens")]
    read = sum(int(r.get("read_tokens", 0) or 0) for r in rounds)
    advanced = sum(int(r.get("advanced_tokens", 0) or 0) for r in rounds)
    return {
        "prompt_tokens": sum(int(r.get("prompt_tokens", 0) or 0) for r in state.get("rounds", [])),
        "read_tokens": read,
        "advanced_tokens": advanced,
        "reread_share": round(1 - advanced / read, 3) if read else 0.0,
        "latency_ms": sum(int(r.get("latency_ms", 0) or 0) for r in state.get("rounds", [])),
    }


def merged_window_text(work_dir: str, state: dict | None = None,
                       chapter_starts: dict | None = None) -> tuple[str, int]:
    """Join the latest stage's committed paragraphs, with chapter headings.

    Markers are never stored in paragraph files. `chapter_starts` maps a raw
    chunk id to its chapter list (from chapter_plan.json); a chapter's heading
    goes before the paragraph that covers the chunk's first unit.
    Returns the body and the number of chapters inserted.
    """
    state = state if state is not None else load_window_state(work_dir)
    out_dir = stage_dir_for(work_dir, state.get("input_key", RAW_STAGE))
    pending = []
    for chunk_id, chapters in (chapter_starts or {}).items():
        start = (state.get("chunk_starts") or {}).get(str(chunk_id))
        if start:
            pending.append((int(start), chapters))
    pending.sort(key=lambda item: item[0])

    parts = []
    inserted = 0
    for entry in state.get("paragraphs", []):
        end = int(entry.get("end", 0) or 0)
        while pending and pending[0][0] <= end:
            _, chapters = pending.pop(0)
            for chapter in chapters:
                headings = _chapter_headings(chapter)
                if headings:
                    parts.append("\n".join(f"## {heading}" for heading in headings))
                    inserted += 1
        text = _paragraph_text(out_dir, entry)
        if text:
            parts.append(text)
    return "\n\n".join(parts), inserted


def _chapter_headings(chapter: dict) -> list[str]:
    title = str(chapter.get("title", "")).strip()
    title_en = str(chapter.get("title_en", "")).strip()
    title_zh = str(chapter.get("title_zh", "")).strip()
    headings = [title_en] if title_en else []
    if title_zh and title_zh not in headings:
        headings.append(title_zh)
    if not headings and title:
        headings.append(title)
    return headings


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

def window_params(prompt_name: str, config: dict) -> dict:
    """Resolve the window limits for this run from config.

    `read_tokens` and `write_tokens` are the caps / starting values; each
    round's actual sizes live in the state's `sizing`.
    """
    import yt_transcript_utils as utils

    return {
        "read_tokens": utils._parse_int_min(config.get("window_read_tokens"), DEFAULT_WINDOW_READ_TOKENS, 200),
        "write_tokens": utils._parse_int_min(config.get("window_write_tokens"), DEFAULT_WINDOW_WRITE_TOKENS, 100),
        "tail_tokens": utils._parse_int_min(config.get("window_tail_tokens"), DEFAULT_WINDOW_TAIL_TOKENS, 0),
        "marker_retries": utils._parse_int_min(config.get("window_marker_retries"), DEFAULT_WINDOW_MARKER_RETRIES, 0),
        "sentence_max_tokens": DEFAULT_WINDOW_SENTENCE_MAX_TOKENS,
        "fallback_chunk_tokens": utils._get_task_chunk_target(prompt_name, config),
        "max_output_tokens": utils._get_task_max_output_tokens(prompt_name, config),
        "expected_ratio": utils._get_task_output_ratio(prompt_name, config),
        "timeout_sec": utils._parse_int_min(config.get("llm_timeout_sec"), 120, 1),
        "cli_timeout_sec": utils._parse_int_min(
            config.get("llm_cli_timeout_sec"), kernel_agent_cli.DEFAULT_CLI_TIMEOUT_SEC, 1),
    }


def run_window_rounds(work_dir: str, prompt_name: str, prompt_template: str, *, config: dict,
                      call_llm, state: dict, sentences: list[str], glossary_payload: dict | None = None,
                      should_stop=None, log=None, input_key: str = RAW_STAGE) -> dict:
    """Advance the cursor until the source is done, a stop is requested, or the LLM fails.

    `call_llm(prompt_text, max_output_tokens)` returns the backend result dict
    (`text`, optional `finish_reason`, `latency_ms`, `request_url`) and raises
    `LLMRequestError` after its own retries. `should_stop()` returns a status
    string (`aborted` / `paused`) to stop at a round boundary, or "".
    """
    import yt_transcript_utils as utils

    log = log or (lambda message: print(message, file=sys.stderr))
    params = state["params"]
    out_dir = stage_dir_for(work_dir, input_key)
    unit = "paragraph" if _stage(input_key) == PROCESSED_STAGE else "sentence"
    joiner = "\n\n" if unit == "paragraph" else " "
    sizing = state.get("sizing") or initial_sizing(params)

    token_cache: dict[int, int] = {}

    def unit_tokens(index: int) -> int:
        if index not in token_cache:
            token_cache[index] = utils._estimate_tokens(sentences[index], "tokens", config)
        return token_cache[index]

    def estimate(text: str) -> int:
        return utils._estimate_tokens(text, "tokens", config)

    def span_tokens(start: int, end: int) -> int:
        return sum(unit_tokens(i) for i in range(start, end))

    max_glossary_terms = max(0, utils._parse_int_min(config.get("chunk_glossary_max_prompt_terms"), 8, 0))
    marker_failures = 0
    total = len(sentences)

    while state["cursor"] < total:
        stop_status = should_stop() if should_stop else ""
        if stop_status:
            state["status"] = stop_status
            _write_state(out_dir, state)
            return {"status": stop_status}

        cursor = state["cursor"]
        round_index = len(state["rounds"]) + 1
        tail_text = written_tail(out_dir, state, params["tail_tokens"], config)
        use_fallback = marker_failures > params["marker_retries"]
        plan = plan_round(sizing, params, span_tokens(cursor, total))

        if use_fallback:
            end = build_read_window(sentences, cursor, params["fallback_chunk_tokens"], estimate)
            chunk_text = joiner.join(sentences[cursor:end])
            glossary_text = _glossary_text(glossary_payload, chunk_text, max_glossary_terms)
            prompt = build_fallback_prompt(prompt_template, chunk_text, tail_text=tail_text,
                                           glossary_text=glossary_text)
        else:
            end = total if plan["finish"] else build_read_window(sentences, cursor, plan["read_tokens"], estimate)
            window_text = joiner.join(sentences[cursor:end])
            glossary_text = _glossary_text(glossary_payload, window_text, max_glossary_terms)
            chars_per_token = len(window_text) / max(1, span_tokens(cursor, end))
            prompt = build_window_prompt(
                prompt_template,
                sentences=sentences,
                cursor=cursor,
                window_end=end,
                tail_text=tail_text,
                write_tokens=plan["write_tokens"],
                write_chars=int(plan["write_tokens"] * chars_per_token),
                glossary_text=glossary_text,
                prompt_name=prompt_name,
                unit=unit,
                finish=plan["finish"] and end >= total,
            )

        record = {
            "round": round_index,
            "mode": "fallback" if use_fallback else "window",
            "cursor": cursor + 1,
            "window_end": end,
            "read_tokens": span_tokens(cursor, end),
            "write_target": None if use_fallback else plan["write_tokens"],
            "finish_round": bool(plan["finish"]) and not use_fallback,
            "prompt_tokens": estimate(prompt),
            "started_at": _now_iso(),
        }
        try:
            result = call_llm(prompt, params["max_output_tokens"])
        except utils.LLMRequestError as error:
            record.update({"result": "error", "error_type": error.error_type, "error": str(error)})
            state["rounds"].append(record)
            state["status"] = "failed"
            state["last_error"] = {"error_type": error.error_type, "message": str(error), "cursor": cursor + 1}
            _write_state(out_dir, state)
            log(f"⚠️ Window round {round_index} failed at S{cursor + 1}: {error}")
            return {"status": "failed", "error": str(error), "error_type": error.error_type}

        # Some models wrap the whole answer in a Markdown code fence.
        output = CODE_FENCE_LINE.sub("", str(result.get("text", "") or ""))
        finish_reason = str(result.get("finish_reason", "") or "").strip().lower()
        latency_ms = int(result.get("latency_ms", 0) or 0)
        timeout_sec = params["cli_timeout_sec"] if str(result.get("request_url", "")).startswith("cli:") \
            else params["timeout_sec"]
        record.update({
            "output_chars": len(output),
            "finish_reason": finish_reason,
            "latency_ms": latency_ms,
            "request_url": result.get("request_url", ""),
        })

        if use_fallback:
            body = MARKER_PATTERN.sub("", SOURCE_TAG_PATTERN.sub("", output)).strip()
            if not body:
                record.update({"result": "error", "error_type": "empty_output"})
                state["rounds"].append(record)
                state["status"] = "failed"
                state["last_error"] = {"error_type": "empty_output", "message": "fallback chunk returned no text",
                                       "cursor": cursor + 1}
                _write_state(out_dir, state)
                log(f"⚠️ Fallback chunk at S{cursor + 1} returned no text")
                return {"status": "failed", "error": "fallback chunk returned no text", "error_type": "empty_output"}
            record.update({"result": "fallback", "advanced_to": end, "advanced_tokens": record["read_tokens"],
                           "output_tokens": estimate(body)})
            state["rounds"].append(record)
            _commit_paragraphs(out_dir, state, [{"text": body, "start": cursor, "end": end - 1}],
                               sentences, mode="fallback", round_index=round_index)
            log(f"ℹ️ Window fallback: S{cursor + 1}-S{end} processed as one fixed chunk")
            marker_failures = 0
            continue

        parsed = parse_window_output(output, cursor, end)
        if parsed["ok"]:
            advanced_tokens = span_tokens(cursor, parsed["next_cursor"])
            output_tokens = sum(estimate(p["text"]) for p in parsed["paragraphs"])
            expected = sizing["ratio"] if sizing["ratio_samples"] else params["expected_ratio"]
            ratio_error = round_ratio_problem(advanced_tokens, output_tokens, expected)
            if ratio_error:
                parsed = _invalid(
                    ratio_error,
                    f"output/source ratio {output_tokens / advanced_tokens:.2f} is far from the expected {expected:.2f}",
                )

        if not parsed["ok"]:
            marker_failures += 1
            record.update({"result": "invalid", "error_type": parsed["error_type"], "error": parsed["error"]})
            state["rounds"].append(record)
            _write_state(out_dir, state)
            retry_note = "retrying" if marker_failures <= params["marker_retries"] else "falling back to a fixed chunk"
            log(f"⚠️ Window round {round_index} at S{cursor + 1}: {parsed['error']}; {retry_note}")
            continue

        marker_failures = 0
        truncated = finish_reason in TRUNCATION_FINISH_REASONS
        slow = latency_ms > SLOW_ROUND_SHARE * timeout_sec * 1000
        exhausted = parsed["next_cursor"] >= end and end < total
        record.update({
            "result": "ok",
            "paragraphs": len(parsed["paragraphs"]),
            "advanced_to": parsed["next_cursor"],
            "advanced_tokens": advanced_tokens,
            "output_tokens": output_tokens,
            "dropped_tail_chars": parsed["dropped_tail_chars"],
            "truncated": truncated,
            "slow": slow,
        })
        sizing = update_sizing(sizing, params, {
            "advanced_tokens": advanced_tokens,
            "output_tokens": output_tokens,
            "read_tokens": record["read_tokens"],
            "exhausted": exhausted,
            "truncated": truncated,
            "slow": slow,
        })
        record["next_sizing"] = {"read_tokens": sizing["read_tokens"], "write_tokens": sizing["write_tokens"],
                                 "ratio": round(sizing["ratio"], 3)}
        state["sizing"] = sizing
        state["rounds"].append(record)
        _commit_paragraphs(out_dir, state, parsed["paragraphs"], sentences, mode="window",
                           round_index=round_index)
        log(f"ℹ️ Window round {round_index}: S{cursor + 1}-S{parsed['next_cursor']} done ({total} total)")

    state["status"] = "done"
    _write_state(out_dir, state)
    return {"status": "done"}


def _glossary_text(glossary_payload: dict | None, source_text: str, max_terms: int) -> str:
    if not glossary_payload:
        return ""
    return kernel_glossary.build_glossary_prompt_context(glossary_payload, source_text, max_terms=max_terms)["text"]


def _prepare_state(work_dir: str, prompt_name: str, input_key: str, sentences: list[str], params: dict,
                   force: bool, log, dry_run: bool = False) -> tuple[dict, bool]:
    """Load a resumable state for the same source, or start a new one."""
    state = load_window_state(work_dir, input_key)
    digest = sentences_digest(sentences)
    resumable = (
        not force
        and state.get("schema_version") == STATE_SCHEMA_VERSION
        and state.get("prompt_name") == prompt_name
        and state.get("sentences_sha256") == digest
    )
    if resumable:
        # Keep the original limits so a resumed run stays consistent.
        state.setdefault("params", params)
        state.setdefault("sizing", initial_sizing(state["params"]))
        return state, True
    if state and not dry_run:
        reason = "--force" if force else "the source, prompt or state format changed"
        log(f"ℹ️ Starting the window run over ({reason}).")
        for stale in stage_dir_for(work_dir, input_key).glob("out_*.md"):
            stale.unlink()
    return _new_state(prompt_name, input_key, sentences, params), False


def _build_result(work_dir: str, state: dict, outcome: dict, *, prompt_name: str, input_key: str,
                  request_url: str, backend_decision: dict, warnings: list, resumed: bool) -> dict:
    status = outcome.get("status", state.get("status", ""))
    paragraphs = state.get("paragraphs", [])
    rounds = state.get("rounds", [])
    coverage = coverage_report(state)
    out_dir = stage_dir_for(work_dir, input_key)
    success = status == "done"
    result = {
        "success": success,
        "long_text_mode": "window",
        "prompt_name": prompt_name,
        "input_key": _stage(input_key),
        "status": status,
        "resumed": resumed,
        "sentence_count": state.get("sentence_count", 0),
        "cursor": state.get("cursor", 0),
        "paragraph_count": len(paragraphs),
        "round_count": len(rounds),
        "window_rounds": sum(1 for r in rounds if r.get("mode") == "window"),
        "invalid_rounds": sum(1 for r in rounds if r.get("result") == "invalid"),
        "fallback_rounds": sum(1 for r in rounds if r.get("result") == "fallback"),
        "coverage": coverage,
        "efficiency": efficiency_report(state),
        "params": state.get("params", {}),
        "sizing": state.get("sizing", {}),
        "request_url": request_url,
        "llm_backend": backend_decision,
        "state_path": str(out_dir / STATE_FILE_NAME),
        "warnings": warnings,
        "warning_count": len(warnings),
        # Fields the chunk-path callers read; in window mode a "chunk" is one LLM request (round).
        "processed_count": sum(1 for r in rounds if r.get("result") in {"ok", "fallback"}),
        "failed_count": 0 if success or status in {"aborted", "paused"} else 1,
        "skipped_count": 0,
        "superseded_count": 0,
        "total_chunks": len(rounds),
        "output_files": [str(out_dir / p["file"]) for p in paragraphs],
        "aborted": status == "aborted",
        "aborted_reason": "Cancellation requested" if status == "aborted" else outcome.get("error", ""),
        "paused": status == "paused",
        "replan_required": False,
        "replan_reason": "",
    }
    if coverage["uncovered_ranges"] and success:
        warnings.append(f"Window run left source units without output: {', '.join(coverage['uncovered_ranges'][:5])}")
        result["warning_count"] = len(warnings)
    if outcome.get("error"):
        result["error"] = outcome.get("error_type", "")
        result["message"] = outcome["error"]
    return result


def _process_window_impl(work_dir: str, prompt_name: str, extra_instruction: str = "",
                         config_path: str = None, dry_run: bool = False, force: bool = False,
                         input_key: str = RAW_STAGE) -> dict:
    """Run (or resume) the sliding-window executor for one stage of a work dir."""
    import yt_transcript_utils as utils

    log = lambda message: print(message, file=sys.stderr)  # noqa: E731
    stage = _stage(input_key)
    work_path = Path(work_dir)
    if not (work_path / "manifest.json").exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        prompt_path = utils._resolve_prompt_template_path(prompt_name)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    prompt_template = prompt_path.read_text(encoding="utf-8")
    if extra_instruction:
        prompt_template += f"\n\n**Additional Instructions**: {extra_instruction}\n"

    config = utils._load_optional_config(config_path) if dry_run else utils.load_config(config_path)
    params = window_params(prompt_name, config)
    if stage == PROCESSED_STAGE:
        sentences, chunk_starts = build_processed_units(work_dir, config)
        unit_split = {}
        missing = "no finished raw-stage window output to process"
    else:
        sentences, chunk_starts, unit_split = build_source_sentences(work_dir, config, params["sentence_max_tokens"])
        missing = "no source text found in the raw chunks"
    if not sentences:
        print(f"Error: {missing} in {work_dir}", file=sys.stderr)
        sys.exit(1)

    state, resumed = _prepare_state(work_dir, prompt_name, stage, sentences, params, force, log, dry_run=dry_run)
    state["chunk_starts"] = chunk_starts
    if unit_split:
        state["unit_split"] = unit_split
    previous_backend = state.get("llm_backend") or None
    backend_decision = kernel_agent_cli.resolve_llm_backend(config, probe=not dry_run, previous=previous_backend)
    api_request_url = utils._build_api_url(config.get("llm_base_url", ""), config.get("llm_api_format", "openai")) \
        if config.get("llm_base_url") else ""
    use_cli = backend_decision["backend"] == "cli"
    request_url = f"cli:{backend_decision['agent']}" if use_cli else api_request_url

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "long_text_mode": "window",
            "prompt_name": prompt_name,
            "input_key": stage,
            "sentence_count": len(sentences),
            "unit_split": unit_split,
            "cursor": state.get("cursor", 0),
            "resumed": resumed,
            "params": state.get("params", params),
            "sizing": state.get("sizing", {}),
            "request_url": request_url,
            "llm_backend": backend_decision,
            "warnings": [],
            "message": "Dry run: window executor ready",
        }

    if backend_decision["backend"] == "none":
        print(
            "Error: no LLM backend available. "
            f"{backend_decision['fallback_reason'] or 'Agent CLI unavailable'}; "
            "and the fallback API is not configured (set llm_api_key, llm_base_url, llm_model in config.yaml)",
            file=sys.stderr,
        )
        sys.exit(1)

    warnings: list[str] = []
    if use_cli:
        log(f"ℹ️ Window requests use the {backend_decision['agent']} CLI ({' '.join(backend_decision['command'])}).")
    elif backend_decision["mode"] == "auto" and backend_decision["fallback_reason"]:
        log(f"ℹ️ Window requests use the configured API: {backend_decision['fallback_reason']}")

    glossary_payload = kernel_glossary.load_glossary(work_dir)
    if not glossary_payload and prompt_name == "cleanup_zh":
        built = kernel_glossary.build_glossary(work_dir, mode="transcript")
        if built.get("success", False):
            glossary_payload = built

    cli_max_retries = utils._parse_int_min(config.get("llm_cli_max_retries"), kernel_agent_cli.DEFAULT_CLI_MAX_RETRIES, 0)
    stream_mode = utils._normalize_stream_mode(config.get("llm_stream", "auto"))

    def call_llm(prompt_text: str, max_output_tokens: int) -> dict:
        nonlocal use_cli, request_url
        if use_cli:
            try:
                return kernel_agent_cli.call_cli_llm(
                    backend_decision["command"],
                    prompt_text,
                    timeout_sec=params["cli_timeout_sec"],
                    max_retries=cli_max_retries,
                    strip_env=backend_decision.get("strip_env", []),
                    agent=backend_decision["agent"],
                )
            except utils.LLMRequestError as error:
                if not backend_decision["api_configured"]:
                    raise
                use_cli = False
                request_url = api_request_url
                backend_decision["backend"] = "api"
                backend_decision["fallback_reason"] = f"Agent CLI failed mid-run: {error}"
                state["llm_backend"] = backend_decision
                warning = (f"⚠️ {backend_decision['agent']} CLI failed ({error.error_type}); "
                           "switching the remaining window rounds to the configured API.")
                warnings.append(warning)
                log(warning)
        return utils._call_llm_api(
            api_key=config.get("llm_api_key", ""),
            base_url=config.get("llm_base_url", ""),
            model=config.get("llm_model", ""),
            messages=[{"role": "user", "content": prompt_text}],
            api_format=config.get("llm_api_format", "openai"),
            timeout_sec=config.get("llm_timeout_sec", 120),
            max_retries=config.get("llm_max_retries", 3),
            backoff_sec=config.get("llm_backoff_sec", 1.5),
            stream_mode=stream_mode,
            max_tokens=max_output_tokens,
        )

    def should_stop() -> str:
        if kernel_state.summarize_runtime_cancel_request(work_dir).get("requested", False):
            kernel_state.consume_runtime_cancel(work_dir)
            return "aborted"
        if kernel_state.summarize_runtime_pause_request(work_dir).get("requested", False):
            return "paused"
        return ""

    set_manifest_executor(work_dir, "window", stage)
    out_dir = stage_dir_for(work_dir, stage)
    state["status"] = "running"
    state["run_id"] = kernel_runtime.new_trace_id("process-window")
    state["llm_backend"] = backend_decision
    _write_state(out_dir, state)
    if resumed and state.get("cursor", 0):
        log(f"ℹ️ Resuming the window run at S{state['cursor'] + 1} of {len(sentences)}.")

    outcome = run_window_rounds(
        work_dir,
        prompt_name,
        prompt_template,
        config=config,
        call_llm=call_llm,
        state=state,
        sentences=sentences,
        glossary_payload=glossary_payload,
        should_stop=should_stop,
        log=log,
        input_key=stage,
    )
    return _build_result(work_dir, state, outcome, prompt_name=prompt_name, input_key=stage,
                         request_url=request_url, backend_decision=backend_decision,
                         warnings=warnings, resumed=resumed)


def process_window(work_dir: str, prompt_name: str, extra_instruction: str = "",
                   config_path: str = None, dry_run: bool = False, force: bool = False,
                   runtime_ownership: dict | None = None, input_key: str = RAW_STAGE) -> dict:
    """Run the window executor under the same runtime ownership as process-chunks."""
    import yt_transcript_utils as utils
    from ..task_runtime import controller as kernel_controller

    if not (Path(work_dir) / "manifest.json").exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    return runtime_lifecycle.execute_lifecycle_command(
        "process-chunks",
        lambda: kernel_controller.run_owned_mutation(
            work_dir,
            "process-chunks",
            runtime_ownership=runtime_ownership,
            conflict_result_builder=utils._build_process_ownership_conflict_result,
            mutation_fn=lambda ownership: _process_window_impl(
                work_dir,
                prompt_name,
                extra_instruction=extra_instruction,
                config_path=config_path,
                dry_run=dry_run,
                force=force,
                input_key=input_key,
            ),
        ),
        context={
            "work_dir": work_dir,
            "prompt_name": prompt_name,
            "long_text_mode": "window",
            "input_key": _stage(input_key),
            "extra_instruction": extra_instruction,
            "force": force,
        },
    )
