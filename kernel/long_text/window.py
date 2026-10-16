"""Sliding-window long-text executor (`long_text_mode: window`).

Instead of cutting the source into fixed chunks up front, the executor keeps a
cursor into the numbered source sentences and moves it forward round by round:

1. Every source sentence gets a number (`[S12]`).
2. Each round sends a wide read window starting at the cursor, plus the tail
   of what was already written as left context, and asks for about N tokens
   of output that stop at a paragraph or section end.
3. The model ends every paragraph with `<!--S<n>-->`, the number of the last
   source sentence that paragraph covers.
4. Complete paragraphs are written to disk, a trailing paragraph without a
   marker is dropped, and the cursor moves past the last marker.

Progress is read only from the markers, so a cut-off response is not a
failure, it just advances less; nothing depends on `finish_reason` or
`max_tokens`, and the same loop works for API and agent-CLI backends. When a
response has no usable markers twice in a row, that stretch of the source is
processed once with the plain fixed-chunk prompt instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from ..task_runtime import lifecycle as runtime_lifecycle
from ..task_runtime import runtime as kernel_runtime
from ..task_runtime import state as kernel_state
from . import agent_cli as kernel_agent_cli
from . import glossary as kernel_glossary

LONG_TEXT_MODES = ("chunks", "window")
DEFAULT_LONG_TEXT_MODE = "chunks"
# 2a ships the window executor for the monolingual Chinese cleanup stage only.
WINDOW_PROMPTS = ("cleanup_zh",)

DEFAULT_WINDOW_READ_TOKENS = 6000
DEFAULT_WINDOW_WRITE_TOKENS = 3000
DEFAULT_WINDOW_TAIL_TOKENS = 400
DEFAULT_WINDOW_MARKER_RETRIES = 1
# Sentences longer than this are split so one number never covers a huge
# unpunctuated run (common in auto-generated Chinese captions).
DEFAULT_WINDOW_SENTENCE_MAX_TOKENS = 120

WINDOW_DIR_NAME = "window"
STATE_FILE_NAME = "state.json"
STATE_SCHEMA_VERSION = 1
EXECUTOR_MANIFEST_KEY = "long_text_executor"

MARKER_PATTERN = re.compile(r"<!--\s*S(\d+)\s*-->")
SOURCE_TAG_PATTERN = re.compile(r"\[S\d+\]\s?")
CODE_FENCE_LINE = re.compile(r"^```[\w-]*[ \t]*$", re.MULTILINE)
# A paragraph keeping less than this share of its source characters is listed
# in the coverage report as a possible skip (advisory only).
LOW_OUTPUT_RATIO = 0.5

WINDOW_PROTOCOL = """## Sliding-Window Protocol

The input below is one window of a longer transcript. Every source sentence starts with a number tag like [S12].

- Start at [S{first}].{done_note}
- Write about {write_tokens} tokens of output (roughly {write_chars} characters), then stop at the end of the nearest paragraph or section. You do not need to finish the window.{end_note}
- After every paragraph, put a marker on its own line: <!--S<n>-->, where n is the number of the last source sentence that paragraph covers.
- Markers must increase, and the paragraphs must cover the sentences in order without skipping any.
- A `##` header belongs to the paragraph that follows it; do not put a marker after a header alone.
- Do not copy the [S<n>] tags into the output.
- Output only the processed text and the markers."""

ALREADY_WRITTEN_BLOCK = """## Already Written (context only, do not repeat)

{tail}"""

PREVIOUS_CONTEXT_BLOCK = """## Previous Output (context only, do not repeat)

{tail}"""


def resolve_long_text_mode(config: dict | None) -> str:
    """Return `window` or `chunks` from config, defaulting to `chunks`."""
    value = str((config or {}).get("long_text_mode", DEFAULT_LONG_TEXT_MODE) or "").strip().lower()
    return value if value in LONG_TEXT_MODES else DEFAULT_LONG_TEXT_MODE


def window_supported(prompt_name: str, input_key: str = "raw_path") -> bool:
    """Return whether the window executor handles this prompt and input stage."""
    return prompt_name in WINDOW_PROMPTS and (input_key or "raw_path") == "raw_path"


def window_dir_for(work_dir: str) -> Path:
    return Path(work_dir) / WINDOW_DIR_NAME


def state_path_for(work_dir: str) -> Path:
    return window_dir_for(work_dir) / STATE_FILE_NAME


def load_window_state(work_dir: str) -> dict:
    """Load the window run state, or `{}` when none exists or it is unreadable."""
    path = state_path_for(work_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def manifest_uses_window(work_dir: str) -> bool:
    """Return whether the latest executor run in this work dir was the window one."""
    manifest_path = Path(work_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get(EXECUTOR_MANIFEST_KEY) == "window"


def set_manifest_executor(work_dir: str, executor: str) -> None:
    """Record which executor owns the work dir's output (`window` or `chunks`)."""
    manifest_path = Path(work_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict) or manifest.get(EXECUTOR_MANIFEST_KEY, "chunks") == executor:
        return
    manifest[EXECUTOR_MANIFEST_KEY] = executor
    kernel_state.write_manifest(manifest_path, manifest)


# ---------------------------------------------------------------------------
# Source sentences
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


def split_source_sentences(text: str, config: dict | None = None,
                           max_sentence_tokens: int = DEFAULT_WINDOW_SENTENCE_MAX_TOKENS) -> list[str]:
    """Split source text into the numbered units the window protocol works on."""
    import yt_transcript_utils as utils

    sentences = []
    for sentence in utils._split_sentences(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if max_sentence_tokens > 0 and utils._estimate_tokens(sentence, "tokens", config) > max_sentence_tokens:
            sentences.extend(
                part.strip()
                for part in utils._force_split_text_by_tokens(sentence, max_sentence_tokens, config)
                if part.strip()
            )
        else:
            sentences.append(sentence)
    return sentences


def build_source_sentences(work_dir: str, config: dict | None = None,
                           max_sentence_tokens: int = DEFAULT_WINDOW_SENTENCE_MAX_TOKENS) -> tuple[list[str], dict]:
    """Number the whole source; also return each raw chunk's first sentence number.

    Chunks are split one by one, so a chunk boundary is always a sentence
    boundary and chapter plans keyed by chunk id still map onto the output.
    """
    sentences: list[str] = []
    chunk_starts: dict = {}
    for chunk_id, text in load_source_chunks(work_dir):
        chunk_sentences = split_source_sentences(text, config, max_sentence_tokens)
        if chunk_sentences:
            chunk_starts[str(chunk_id)] = len(sentences) + 1
            sentences.extend(chunk_sentences)
    return sentences, chunk_starts


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


def number_sentences(sentences: list[str], start: int, end: int) -> str:
    """Render sentences[start:end] with 1-based `[S<n>]` tags, one per line."""
    return "\n".join(f"[S{index + 1}] {sentences[index]}" for index in range(start, end))


# ---------------------------------------------------------------------------
# Prompt and response protocol
# ---------------------------------------------------------------------------

def build_window_prompt(prompt_template: str, *, sentences: list[str], cursor: int, window_end: int,
                        tail_text: str = "", write_tokens: int = DEFAULT_WINDOW_WRITE_TOKENS,
                        write_chars: int = 0, glossary_text: str = "") -> str:
    """Assemble one window request from the task prompt and the window protocol."""
    import yt_transcript_utils as utils

    total = len(sentences)
    done_note = (
        f" Sentences before [S{cursor + 1}] are already done; the Already Written block shows how that output ends."
        if cursor > 0 else ""
    )
    if window_end >= total:
        end_note = f" [S{window_end}] is the end of the transcript; finish it if you get there."
    else:
        end_note = f" The window ends at [S{window_end}] and the transcript continues after it."
    protocol = WINDOW_PROTOCOL.format(
        first=cursor + 1,
        done_note=done_note,
        write_tokens=write_tokens,
        write_chars=write_chars or write_tokens,
        end_note=end_note,
    )
    context = protocol
    if tail_text.strip():
        context += "\n\n" + ALREADY_WRITTEN_BLOCK.format(tail=tail_text.strip())
    return utils._build_chunk_prompt(
        prompt_template,
        number_sentences(sentences, cursor, window_end),
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
    """Split a window response into complete paragraphs and validate the markers.

    `cursor` is the 0-based index of the first sentence in the window and
    `window_end` the exclusive end, so valid marker numbers are
    `cursor + 1 .. window_end`. Paragraph ranges are contiguous by
    construction; each one starts right after the previous marker.
    """
    paragraphs = []
    position = 0
    previous_last = cursor  # 1-based number of the last sentence already covered
    for match in MARKER_PATTERN.finditer(text or ""):
        number = int(match.group(1))
        if number <= previous_last:
            return _invalid("non_monotonic", f"marker S{number} does not move past S{previous_last}")
        if number > window_end:
            return _invalid("out_of_window", f"marker S{number} is past the window end S{window_end}")
        body = SOURCE_TAG_PATTERN.sub("", text[position:match.start()]).strip()
        position = match.end()
        if not body:
            # A marker with nothing before it (e.g. right after another marker)
            # folds its sentences into the next paragraph.
            continue
        paragraphs.append({"text": body, "start": previous_last, "end": number - 1})
        previous_last = number
    if not paragraphs:
        return _invalid("no_markers", "response has no complete paragraph with a marker")
    tail = (text or "")[position:].strip()
    return {
        "ok": True,
        "paragraphs": paragraphs,
        "next_cursor": previous_last,
        "dropped_tail_chars": len(tail),
        "error_type": "",
        "error": "",
    }


def _invalid(error_type: str, message: str) -> dict:
    return {"ok": False, "paragraphs": [], "next_cursor": None, "dropped_tail_chars": 0,
            "error_type": error_type, "error": message}


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

def _new_state(prompt_name: str, sentences: list[str], params: dict) -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "prompt_name": prompt_name,
        "status": "running",
        "sentence_count": len(sentences),
        "sentences_sha256": sentences_digest(sentences),
        "cursor": 0,
        "params": params,
        "paragraphs": [],
        "rounds": [],
        "llm_backend": {},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _now_iso() -> str:
    import yt_transcript_utils as utils

    return utils._now_iso()


def _write_state(work_dir: str, state: dict) -> None:
    state["updated_at"] = _now_iso()
    kernel_state.atomic_write_text(
        state_path_for(work_dir),
        json.dumps(state, ensure_ascii=False, indent=2),
    )


def _paragraph_text(work_dir: str, entry: dict) -> str:
    path = window_dir_for(work_dir) / str(entry.get("file", ""))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def written_tail(work_dir: str, state: dict, tail_tokens: int, config: dict | None = None) -> str:
    """Return the end of the committed output, capped at about `tail_tokens`."""
    import yt_transcript_utils as utils

    if tail_tokens <= 0:
        return ""
    pieces = []
    used = 0
    for entry in reversed(state.get("paragraphs", [])):
        text = _paragraph_text(work_dir, entry)
        if not text:
            continue
        pieces.insert(0, text)
        used += utils._estimate_tokens(text, "tokens", config)
        if used >= tail_tokens:
            break
    return utils._truncate_tail_text_to_tokens("\n\n".join(pieces), tail_tokens, config)


def _commit_paragraphs(work_dir: str, state: dict, paragraphs: list[dict], sentences: list[str],
                       *, mode: str, round_index: int) -> None:
    """Write paragraphs to disk, then record them and the new cursor in the state."""
    out_dir = window_dir_for(work_dir)
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
    _write_state(work_dir, state)


def coverage_report(state: dict) -> dict:
    """Report which source sentences have no committed output yet."""
    total = int(state.get("sentence_count", 0) or 0)
    covered = [False] * total
    low_ratio = []
    paragraphs = state.get("paragraphs", [])
    for position, entry in enumerate(paragraphs):
        start = int(entry.get("start", 0) or 0)
        end = int(entry.get("end", 0) or 0)
        for number in range(max(1, start), min(total, end) + 1):
            covered[number - 1] = True
        # Paragraph breaks do not always fall on sentence numbers (a long
        # sentence split mid-way can spill into the next paragraph), so the
        # ratio is taken over the paragraph and its neighbours.
        neighbours = paragraphs[max(0, position - 1):position + 2]
        source_chars = sum(int(item.get("source_chars", 0) or 0) for item in neighbours)
        output_chars = sum(int(item.get("output_chars", 0) or 0) for item in neighbours)
        if source_chars and output_chars / source_chars < LOW_OUTPUT_RATIO:
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
        "low_output_ratio_paragraphs": low_ratio,
        "fallback_paragraphs": sum(1 for entry in state.get("paragraphs", []) if entry.get("mode") == "fallback"),
    }


def merged_window_text(work_dir: str, state: dict | None = None,
                       chapter_starts: dict | None = None) -> tuple[str, int]:
    """Join committed paragraphs in order, with chapter headings where chapters start.

    Markers are never stored in paragraph files. `chapter_starts` maps a raw
    chunk id to its chapter list (from chapter_plan.json); a chapter's heading
    goes before the paragraph that covers the chunk's first sentence.
    Returns the body and the number of chapters inserted.
    """
    state = state if state is not None else load_window_state(work_dir)
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
        text = _paragraph_text(work_dir, entry)
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
    """Resolve the window sizes for this run from config."""
    import yt_transcript_utils as utils

    return {
        "read_tokens": utils._parse_int_min(config.get("window_read_tokens"), DEFAULT_WINDOW_READ_TOKENS, 200),
        "write_tokens": utils._parse_int_min(config.get("window_write_tokens"), DEFAULT_WINDOW_WRITE_TOKENS, 100),
        "tail_tokens": utils._parse_int_min(config.get("window_tail_tokens"), DEFAULT_WINDOW_TAIL_TOKENS, 0),
        "marker_retries": utils._parse_int_min(config.get("window_marker_retries"), DEFAULT_WINDOW_MARKER_RETRIES, 0),
        "sentence_max_tokens": DEFAULT_WINDOW_SENTENCE_MAX_TOKENS,
        "fallback_chunk_tokens": utils._get_task_chunk_target(prompt_name, config),
        "max_output_tokens": utils._get_task_max_output_tokens(prompt_name, config),
    }


def run_window_rounds(work_dir: str, prompt_name: str, prompt_template: str, *, config: dict,
                      call_llm, state: dict, sentences: list[str], glossary_payload: dict | None = None,
                      should_stop=None, log=None) -> dict:
    """Advance the cursor until the source is done, a stop is requested, or the LLM fails.

    `call_llm(prompt_text, max_output_tokens)` returns the backend result dict
    (`text`, optional `finish_reason`, `latency_ms`, `request_url`) and raises
    `LLMRequestError` after its own retries. `should_stop()` returns a status
    string (`aborted` / `paused`) to stop at a round boundary, or "".
    """
    import yt_transcript_utils as utils

    log = log or (lambda message: print(message, file=sys.stderr))
    params = state["params"]

    def estimate(text: str) -> int:
        return utils._estimate_tokens(text, "tokens", config)

    max_glossary_terms = max(0, utils._parse_int_min(config.get("chunk_glossary_max_prompt_terms"), 8, 0))
    marker_failures = 0
    total = len(sentences)

    while state["cursor"] < total:
        stop_status = should_stop() if should_stop else ""
        if stop_status:
            state["status"] = stop_status
            _write_state(work_dir, state)
            return {"status": stop_status}

        cursor = state["cursor"]
        round_index = len(state["rounds"]) + 1
        tail_text = written_tail(work_dir, state, params["tail_tokens"], config)
        use_fallback = marker_failures > params["marker_retries"]

        if use_fallback:
            end = build_read_window(sentences, cursor, params["fallback_chunk_tokens"], estimate)
            chunk_text = " ".join(sentences[cursor:end])
            glossary_text = _glossary_text(glossary_payload, chunk_text, max_glossary_terms)
            prompt = build_fallback_prompt(prompt_template, chunk_text, tail_text=tail_text,
                                           glossary_text=glossary_text)
        else:
            end = build_read_window(sentences, cursor, params["read_tokens"], estimate)
            window_text = " ".join(sentences[cursor:end])
            glossary_text = _glossary_text(glossary_payload, window_text, max_glossary_terms)
            window_tokens = max(1, estimate(window_text))
            chars_per_token = len(window_text) / window_tokens
            prompt = build_window_prompt(
                prompt_template,
                sentences=sentences,
                cursor=cursor,
                window_end=end,
                tail_text=tail_text,
                write_tokens=params["write_tokens"],
                write_chars=int(params["write_tokens"] * chars_per_token),
                glossary_text=glossary_text,
            )

        record = {
            "round": round_index,
            "mode": "fallback" if use_fallback else "window",
            "cursor": cursor + 1,
            "window_end": end,
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
            _write_state(work_dir, state)
            log(f"⚠️ Window round {round_index} failed at S{cursor + 1}: {error}")
            return {"status": "failed", "error": str(error), "error_type": error.error_type}

        # Some models wrap the whole answer in a Markdown code fence.
        output = CODE_FENCE_LINE.sub("", str(result.get("text", "") or ""))
        record.update({
            "output_chars": len(output),
            "finish_reason": str(result.get("finish_reason", "") or ""),
            "latency_ms": result.get("latency_ms"),
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
                _write_state(work_dir, state)
                log(f"⚠️ Fallback chunk at S{cursor + 1} returned no text")
                return {"status": "failed", "error": "fallback chunk returned no text", "error_type": "empty_output"}
            record.update({"result": "fallback", "advanced_to": end})
            state["rounds"].append(record)
            _commit_paragraphs(work_dir, state, [{"text": body, "start": cursor, "end": end - 1}],
                               sentences, mode="fallback", round_index=round_index)
            log(f"ℹ️ Window fallback: S{cursor + 1}-S{end} processed as one fixed chunk")
            marker_failures = 0
            continue

        parsed = parse_window_output(output, cursor, end)
        if not parsed["ok"]:
            marker_failures += 1
            record.update({"result": "invalid", "error_type": parsed["error_type"], "error": parsed["error"]})
            state["rounds"].append(record)
            _write_state(work_dir, state)
            retry_note = "retrying" if marker_failures <= params["marker_retries"] else "falling back to a fixed chunk"
            log(f"⚠️ Window round {round_index} at S{cursor + 1}: {parsed['error']}; {retry_note}")
            continue

        marker_failures = 0
        record.update({
            "result": "ok",
            "paragraphs": len(parsed["paragraphs"]),
            "advanced_to": parsed["next_cursor"],
            "dropped_tail_chars": parsed["dropped_tail_chars"],
        })
        state["rounds"].append(record)
        _commit_paragraphs(work_dir, state, parsed["paragraphs"], sentences, mode="window",
                           round_index=round_index)
        log(f"ℹ️ Window round {round_index}: S{cursor + 1}-S{parsed['next_cursor']} done ({total} total)")

    state["status"] = "done"
    _write_state(work_dir, state)
    return {"status": "done"}


def _glossary_text(glossary_payload: dict | None, source_text: str, max_terms: int) -> str:
    if not glossary_payload:
        return ""
    return kernel_glossary.build_glossary_prompt_context(glossary_payload, source_text, max_terms=max_terms)["text"]


def _prepare_state(work_dir: str, prompt_name: str, sentences: list[str], params: dict,
                   force: bool, log, dry_run: bool = False) -> tuple[dict, bool]:
    """Load a resumable state for the same source, or start a new one."""
    state = load_window_state(work_dir)
    digest = sentences_digest(sentences)
    resumable = (
        not force
        and state.get("schema_version") == STATE_SCHEMA_VERSION
        and state.get("prompt_name") == prompt_name
        and state.get("sentences_sha256") == digest
    )
    if resumable:
        # Keep the original sizes so a resumed run stays consistent.
        state.setdefault("params", params)
        return state, True
    if state and not dry_run:
        reason = "--force" if force else "the source or prompt changed"
        log(f"ℹ️ Starting the window run over ({reason}).")
        out_dir = window_dir_for(work_dir)
        for stale in out_dir.glob("out_*.md"):
            stale.unlink()
    return _new_state(prompt_name, sentences, params), False


def _build_result(work_dir: str, state: dict, outcome: dict, *, prompt_name: str, request_url: str,
                  backend_decision: dict, warnings: list, resumed: bool) -> dict:
    status = outcome.get("status", state.get("status", ""))
    paragraphs = state.get("paragraphs", [])
    rounds = state.get("rounds", [])
    coverage = coverage_report(state)
    success = status == "done"
    result = {
        "success": success,
        "long_text_mode": "window",
        "prompt_name": prompt_name,
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
        "params": state.get("params", {}),
        "request_url": request_url,
        "llm_backend": backend_decision,
        "state_path": str(state_path_for(work_dir)),
        "warnings": warnings,
        "warning_count": len(warnings),
        # Fields the chunk-path callers read; in window mode a "chunk" is one LLM request (round).
        "processed_count": sum(1 for r in rounds if r.get("result") in {"ok", "fallback"}),
        "failed_count": 0 if success or status in {"aborted", "paused"} else 1,
        "skipped_count": 0,
        "superseded_count": 0,
        "total_chunks": len(rounds),
        "output_files": [str(window_dir_for(work_dir) / p["file"]) for p in paragraphs],
        "aborted": status == "aborted",
        "aborted_reason": "Cancellation requested" if status == "aborted" else outcome.get("error", ""),
        "paused": status == "paused",
        "replan_required": False,
        "replan_reason": "",
    }
    if coverage["uncovered_ranges"] and success:
        warnings.append(f"Window run left source sentences without output: {', '.join(coverage['uncovered_ranges'][:5])}")
        result["warning_count"] = len(warnings)
    if outcome.get("error"):
        result["error"] = outcome.get("error_type", "")
        result["message"] = outcome["error"]
    return result


def _process_window_impl(work_dir: str, prompt_name: str, extra_instruction: str = "",
                         config_path: str = None, dry_run: bool = False, force: bool = False) -> dict:
    """Run (or resume) the sliding-window executor for one work dir."""
    import yt_transcript_utils as utils

    log = lambda message: print(message, file=sys.stderr)  # noqa: E731
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
    sentences, chunk_starts = build_source_sentences(work_dir, config, params["sentence_max_tokens"])
    if not sentences:
        print(f"Error: no source text found in the raw chunks of {work_dir}", file=sys.stderr)
        sys.exit(1)

    state, resumed = _prepare_state(work_dir, prompt_name, sentences, params, force, log, dry_run=dry_run)
    state["chunk_starts"] = chunk_starts
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
            "sentence_count": len(sentences),
            "cursor": state.get("cursor", 0),
            "resumed": resumed,
            "params": state.get("params", params),
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

    cli_timeout_sec = utils._parse_int_min(config.get("llm_cli_timeout_sec"), kernel_agent_cli.DEFAULT_CLI_TIMEOUT_SEC, 1)
    cli_max_retries = utils._parse_int_min(config.get("llm_cli_max_retries"), kernel_agent_cli.DEFAULT_CLI_MAX_RETRIES, 0)
    stream_mode = utils._normalize_stream_mode(config.get("llm_stream", "auto"))

    def call_llm(prompt_text: str, max_output_tokens: int) -> dict:
        nonlocal use_cli, request_url
        if use_cli:
            try:
                return kernel_agent_cli.call_cli_llm(
                    backend_decision["command"],
                    prompt_text,
                    timeout_sec=cli_timeout_sec,
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

    set_manifest_executor(work_dir, "window")
    state["status"] = "running"
    state["run_id"] = kernel_runtime.new_trace_id("process-window")
    state["llm_backend"] = backend_decision
    _write_state(work_dir, state)
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
    )
    return _build_result(work_dir, state, outcome, prompt_name=prompt_name, request_url=request_url,
                         backend_decision=backend_decision, warnings=warnings, resumed=resumed)


def process_window(work_dir: str, prompt_name: str, extra_instruction: str = "",
                   config_path: str = None, dry_run: bool = False, force: bool = False,
                   runtime_ownership: dict | None = None) -> dict:
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
            ),
        ),
        context={
            "work_dir": work_dir,
            "prompt_name": prompt_name,
            "long_text_mode": "window",
            "extra_instruction": extra_instruction,
            "force": force,
        },
    )
