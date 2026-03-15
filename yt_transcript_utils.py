#!/usr/bin/env python3
"""
yt-transcript utility script
Provides VTT parsing, Deepgram result processing, audio splitting, filename sanitization, etc.

This module also owns the script-first workflow checkpoints:
- `validate-state` for stage-based state validation
- `plan-optimization` for canonical short/long routing
- `verify-quality` for final stop/go gating

Usage:
    python3 yt_transcript_utils.py <command> [args]

Commands:
    parse-vtt <vtt_path>           Parse VTT subtitle file, output plain text
    process-deepgram <json_path>   Process Deepgram JSON, output cleaned text
    transcribe-deepgram <audio_path>  Call Deepgram API and auto-merge split chunks
    sanitize-filename "<title>"    Clean illegal filename characters
    test-deepgram-api <api_key>    Test Deepgram API key validity
    test-llm-api                   Probe configured LLM API reachability
    test-token-count               Probe provider token counting support with local fallback
    split-audio <audio_path>       Split large audio at silence points (--max-size, --max-deviation)
    chunk-text <input> <output_dir> Split text file into chunks by sentence boundary
    get-chapters <video_url>       Fetch YouTube video chapter metadata
    merge-content <work_dir> <output_file>  Merge processed chunks with chapter headers
    process-chunks <work_dir> --prompt <name>  Process chunks with isolated LLM API calls (--input-key for chained processing)
    replan-remaining <work_dir>    Re-plan unfinished raw chunks after canary/autotune aborts
    assemble-final <optimized_text> <output_file>  Assemble final markdown from optimized text + metadata
    verify-quality <optimized_text>  Verify quality of optimized text (structural checks)
    validate-state <state_path>    Validate workflow state fields for a given stage
    plan-optimization <state_path> Generate a structured optimization plan from workflow state
"""

import argparse
import bisect
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path


def _skill_root() -> Path:
    return Path(__file__).resolve().parent


def _default_config_path() -> Path:
    return _skill_root() / "config.yaml"


def _strip_inline_comment(value: str) -> str:
    """
    Remove inline comments while preserving # inside quotes.
    """
    in_single = False
    in_double = False
    escaped = False

    for idx, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            return value[:idx]
    return value


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _yaml_string(value: str) -> str:
    # Intentionally quote every scalar. The frontmatter should favor predictable
    # parsing over pretty YAML, especially for titles/channels that may contain
    # punctuation, quotes, or comment-like characters.
    return json.dumps(str(value).replace("\r\n", "\n"), ensure_ascii=False)


def _single_line_text(value: str) -> str:
    return " ".join(str(value).split())


def _escape_markdown_text(value: str) -> str:
    """
    Escape Markdown-significant characters in inline text contexts.
    """
    text = _single_line_text(value)
    return re.sub(r'([\\`*_{}\[\]()#+!<>|])', r'\\\1', text)


def _sanitize_markdown_url(value: str) -> str:
    """
    Encode a URL so it remains valid inside Markdown link destinations.
    """
    text = _single_line_text(value)
    return urllib.parse.quote(text, safe=":/?&=#%@+,-._~")


def _build_api_url(base_url: str, api_format: str = "openai") -> str:
    base = base_url.rstrip("/")

    if api_format == "anthropic":
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _build_token_count_url(base_url: str, api_format: str = "openai") -> str:
    base = base_url.rstrip("/")

    if api_format != "anthropic":
        return ""
    if base.endswith("/messages/count_tokens"):
        return base
    if base.endswith("/messages"):
        return f"{base}/count_tokens"
    if base.endswith("/v1"):
        return f"{base}/messages/count_tokens"
    return f"{base}/v1/messages/count_tokens"


class LLMRequestError(Exception):
    def __init__(self, message: str, *, error_type: str = "unknown",
                 status_code: int = None, retryable: bool = False,
                 request_url: str = "", response_body: str = ""):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.retryable = retryable
        self.request_url = request_url
        self.response_body = response_body


def _parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_int_min(value, default: int, minimum: int) -> int:
    parsed = _parse_int(value, default)
    if parsed < minimum:
        return default
    return parsed


def _parse_float_min(value, default: float, minimum: float) -> float:
    parsed = _parse_float(value, default)
    if parsed < minimum:
        return default
    return parsed


def _parse_float_range(value, default: float, minimum: float, maximum: float) -> float:
    parsed = _parse_float(value, default)
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _normalize_stream_mode(value) -> str:
    if value is None:
        return "auto"
    text = str(value).strip().lower()
    if text in {"auto", "true", "false"}:
        return text
    return "auto"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


LEGACY_CHAR_CHUNK_DEFAULTS = {
    "translate_only": 3000,
    "structure_only": 4000,
    "quick_cleanup": 4000,
    "summarize": 8000,
}

TASK_CHUNK_TOKEN_DEFAULTS = {
    "structure_only": 1200,
    "quick_cleanup": 1000,
    "translate_only": 900,
    "summarize": 2500,
}

TASK_OUTPUT_RATIO_DEFAULTS = {
    "structure_only": 1.15,
    "quick_cleanup": 1.05,
    "translate_only": 1.10,
    "summarize": 0.15,
}

TASK_MAX_OUTPUT_TOKEN_DEFAULTS = {
    "structure_only": 1800,
    "quick_cleanup": 1400,
    "translate_only": 1500,
    "summarize": 384,
}

TASK_REQUEST_CAP_DEFAULTS = {
    "structure_only": 3000,
    "quick_cleanup": 3000,
    "translate_only": 2600,
    "summarize": 4500,
}

DEFAULT_CHUNK_MODE = "tokens"
DEFAULT_CONTEXT_WINDOW = 32000
DEFAULT_CONTEXT_UTILIZATION_LIMIT = 0.12
DEFAULT_CHUNK_HARD_CAP_MULTIPLIER = 1.33
MAX_CHUNK_HARD_CAP_MULTIPLIER = 2.0
DEFAULT_CHUNK_SAFETY_BUFFER_TOKENS = 400
DEFAULT_CHUNK_OVERLAP_SENTENCES = 0
DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES = 1
DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS = 60
DEFAULT_ENABLE_TOKEN_COUNT_PROBE = True
DEFAULT_ENABLE_CHUNK_AUTOTUNE = False
DEFAULT_AUTOTUNE_REDUCE_PERCENT = 0.25
DEFAULT_AUTOTUNE_INCREASE_PERCENT = 0.10
DEFAULT_AUTOTUNE_SUCCESS_WINDOW = 20
DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS = 45000
DEFAULT_AUTOTUNE_MIN_TARGET_RATIO = 0.5
DEFAULT_AUTOTUNE_CANARY_CHUNKS = 3
MANIFEST_SCHEMA_VERSION = 2
DEFAULT_UNKNOWN_CHUNK_TOKENS = 900
CHUNK_SEPARATOR = "\n\n"
DEFAULT_UNKNOWN_OUTPUT_RATIO = 1.0
DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS = 1400
DEFAULT_UNKNOWN_REQUEST_CAP = 2600
DEFAULT_UNKNOWN_LEGACY_CHARS = 8000
SUPERSEDED_CHUNK_STATUS = "superseded"


def _normalize_chunk_mode(value) -> str:
    if value is None:
        return DEFAULT_CHUNK_MODE
    text = str(value).strip().lower()
    if text in {"tokens", "chars"}:
        return text
    return DEFAULT_CHUNK_MODE


def _default_config_values(config_path: str = "") -> dict:
    return {
        "output_dir": "",
        "deepgram_api_key": "",
        "llm_api_key": "",
        "llm_base_url": "",
        "llm_model": "",
        "llm_api_format": "openai",
        "llm_timeout_sec": 120,
        "llm_max_retries": 3,
        "llm_backoff_sec": 1.5,
        "llm_stream": "auto",
        "llm_probe_timeout_sec": 20,
        "llm_probe_max_tokens": 16,
        "llm_stop_after_consecutive_timeouts": 2,
        "chunk_mode": DEFAULT_CHUNK_MODE,
        "chunk_size_override": 0,
        "chunk_tokens_structure_only": TASK_CHUNK_TOKEN_DEFAULTS["structure_only"],
        "chunk_tokens_quick_cleanup": TASK_CHUNK_TOKEN_DEFAULTS["quick_cleanup"],
        "chunk_tokens_translate_only": TASK_CHUNK_TOKEN_DEFAULTS["translate_only"],
        "chunk_tokens_summarize": TASK_CHUNK_TOKEN_DEFAULTS["summarize"],
        "chunk_hard_cap_multiplier": DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
        "chunk_safety_buffer_tokens": DEFAULT_CHUNK_SAFETY_BUFFER_TOKENS,
        "chunk_overlap_sentences": DEFAULT_CHUNK_OVERLAP_SENTENCES,
        "chunk_context_tail_sentences": DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
        "chunk_context_summary_tokens": DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS,
        "output_ratio_structure_only": TASK_OUTPUT_RATIO_DEFAULTS["structure_only"],
        "output_ratio_quick_cleanup": TASK_OUTPUT_RATIO_DEFAULTS["quick_cleanup"],
        "output_ratio_translate_only": TASK_OUTPUT_RATIO_DEFAULTS["translate_only"],
        "output_ratio_summarize": TASK_OUTPUT_RATIO_DEFAULTS["summarize"],
        "max_output_tokens_structure_only": TASK_MAX_OUTPUT_TOKEN_DEFAULTS["structure_only"],
        "max_output_tokens_quick_cleanup": TASK_MAX_OUTPUT_TOKEN_DEFAULTS["quick_cleanup"],
        "max_output_tokens_translate_only": TASK_MAX_OUTPUT_TOKEN_DEFAULTS["translate_only"],
        "max_output_tokens_summarize": TASK_MAX_OUTPUT_TOKEN_DEFAULTS["summarize"],
        "enable_token_count_probe": DEFAULT_ENABLE_TOKEN_COUNT_PROBE,
        "enable_chunk_autotune": DEFAULT_ENABLE_CHUNK_AUTOTUNE,
        "autotune_reduce_percent": DEFAULT_AUTOTUNE_REDUCE_PERCENT,
        "autotune_increase_percent": DEFAULT_AUTOTUNE_INCREASE_PERCENT,
        "autotune_success_window": DEFAULT_AUTOTUNE_SUCCESS_WINDOW,
        "autotune_p95_latency_threshold_ms": DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS,
        "autotune_canary_chunks": DEFAULT_AUTOTUNE_CANARY_CHUNKS,
        "config_path": config_path,
        "config_warnings": [],
    }


def _legacy_chunk_target_chars(prompt_name: str = "", config: dict | None = None) -> int:
    config = config or {}
    override = max(0, _parse_int(config.get("chunk_size_override"), 0))
    if override > 0:
        return override

    prompt = (prompt_name or "").strip().lower()
    return LEGACY_CHAR_CHUNK_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_LEGACY_CHARS)


def _get_task_chunk_target(prompt_name: str, config: dict | None = None) -> int:
    config = config or {}
    override = max(0, _parse_int(config.get("chunk_size_override"), 0))
    if override > 0:
        return override

    prompt = (prompt_name or "").strip().lower()
    key = f"chunk_tokens_{prompt}"
    default = TASK_CHUNK_TOKEN_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_CHUNK_TOKENS)
    return max(1, _parse_int(config.get(key), default))


def _get_task_output_ratio(prompt_name: str, config: dict | None = None) -> float:
    config = config or {}
    prompt = (prompt_name or "").strip().lower()
    key = f"output_ratio_{prompt}"
    default = TASK_OUTPUT_RATIO_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_OUTPUT_RATIO)
    return max(0.01, _parse_float(config.get(key), default))


def _get_task_max_output_tokens(prompt_name: str, config: dict | None = None) -> int:
    config = config or {}
    prompt = (prompt_name or "").strip().lower()
    key = f"max_output_tokens_{prompt}"
    default = TASK_MAX_OUTPUT_TOKEN_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS)
    return max(1, _parse_int(config.get(key), default))


def _get_task_request_cap(prompt_name: str) -> int:
    prompt = (prompt_name or "").strip().lower()
    return TASK_REQUEST_CAP_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_REQUEST_CAP)


def _is_cjk_char(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff"


def _is_kana_hangul_char(char: str) -> bool:
    return ("\u3040" <= char <= "\u30ff") or ("\uac00" <= char <= "\ud7af")


def _is_latin_word_char(char: str) -> bool:
    return ("a" <= char <= "z") or ("A" <= char <= "Z") or char.isdigit()


def _new_token_estimate_state() -> dict:
    return {"tokens": 0, "punct_count": 0, "latin_word_len": 0}


def _advance_token_estimate_state(state: dict, char: str, next_char: str = "") -> None:
    if _is_cjk_char(char) or _is_kana_hangul_char(char):
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



def _estimate_tokens_local(text: str, mode: str = "tokens", config: dict | None = None) -> int:
    # Heuristic estimation only. This intentionally over-approximates common
    # transcript shapes and is not a substitute for provider-side token counting.
    del config
    if not text:
        return 0
    if _normalize_chunk_mode(mode) == "chars":
        return len(text)

    state = _new_token_estimate_state()
    for index, char in enumerate(text):
        next_char = text[index + 1] if index + 1 < len(text) else ""
        _advance_token_estimate_state(state, char, next_char)
    return max(1, state["tokens"])


# Runtime chunk planning still uses the local estimator to avoid one remote
# token-count round trip per sentence or segment. Provider probing is wired via
# _count_tokens_via_provider()/test_token_count() and can be promoted later.
def _estimate_tokens(text: str, mode: str = "tokens", config: dict | None = None) -> int:
    return _estimate_tokens_local(text, mode, config)


def _truncate_tail_text_to_tokens(text: str, max_tokens: int,
                                  config: dict | None = None) -> str:
    """Keep the tail-most portion of text within a soft token cap."""
    if not text:
        return ""
    if max_tokens <= 0 or _estimate_tokens(text, "tokens", config) <= max_tokens:
        return text.strip()
    segments = _force_split_text_by_tokens(text.strip(), max_tokens, config)
    return segments[-1].strip() if segments else text.strip()


def _extract_tail_sentences(text: str, sentence_count: int,
                            config: dict | None = None) -> str:
    if not text or sentence_count <= 0:
        return ""
    sentences = _split_sentences(text)
    if sentences:
        tail_text = " ".join(sentences[-sentence_count:]).strip()
    else:
        tail_text = text.strip()

    summary_token_cap = _parse_int_min(
        (config or {}).get("chunk_context_summary_tokens"),
        DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS,
        0,
    )
    if summary_token_cap > 0:
        tail_text = _truncate_tail_text_to_tokens(tail_text, summary_token_cap, config)
    return tail_text


def _extract_last_section_title(text: str) -> str:
    matches = re.findall(r"^##\s+(.+?)\s*$", text or "", re.MULTILINE)
    if not matches:
        return ""
    return f"## {matches[-1].strip()}"


def _resolve_previous_section_title(previous_chunk: dict | None,
                                    work_path: Path,
                                    input_key: str = "raw_path") -> str:
    previous_chunk = previous_chunk or {}

    if input_key == "processed_path":
        cached_processed_title = str(previous_chunk.get("processed_input_section_title", "")).strip()
        if cached_processed_title:
            return cached_processed_title

    cached_title = str(previous_chunk.get("last_section_title", "")).strip()
    if cached_title:
        return cached_title

    processed_name = str(previous_chunk.get("processed_path", "")).strip()
    if not processed_name:
        return ""

    processed_path = work_path / processed_name
    if not processed_path.exists():
        return ""

    try:
        return _extract_last_section_title(processed_path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _resolve_previous_tail_text(previous_chunk: dict | None, work_path: Path,
                                input_key: str, config: dict | None = None) -> str:
    """Resolve continuity tail text for the previous chunk.

    Priority for `processed_path` chains:
    1. cached processed-input tail captured from the previous stage
    2. cached tail from the previous stage output file
    3. re-read previous `processed_path` and derive a tail on demand
    4. raw-stage tail as a last-resort fallback
    """
    previous_chunk = previous_chunk or {}
    config = config or {}

    if input_key == "processed_path":
        cached_input_tail = str(previous_chunk.get("processed_input_tail_context_text", "")).strip()
        if cached_input_tail:
            return cached_input_tail

        cached_tail = str(previous_chunk.get("processed_tail_context_text", "")).strip()
        if cached_tail:
            return cached_tail

        processed_name = str(previous_chunk.get("processed_path", "")).strip()
        if processed_name:
            processed_path = work_path / processed_name
            if processed_path.exists():
                try:
                    processed_text = processed_path.read_text(encoding="utf-8")
                    return _extract_tail_sentences(
                        processed_text,
                        _parse_int_min(
                            config.get("chunk_context_tail_sentences"),
                            DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
                            0,
                        ),
                        config,
                    )
                except OSError:
                    pass

    return str(previous_chunk.get("tail_context_text", "")).strip()


def _build_continuity_context(previous_chunk: dict | None, work_path: Path,
                              config: dict | None = None,
                              input_key: str = "raw_path") -> dict:
    """Build the lightweight continuity block inserted before the next chunk.

    `input_key` decides whether continuity should be derived from the raw-stage
    chunk files (`raw_path`) or from prior-stage processed files (`processed_path`).
    """
    previous_chunk = previous_chunk or {}
    tail_text = _resolve_previous_tail_text(previous_chunk, work_path, input_key, config)
    section_title = _resolve_previous_section_title(previous_chunk, work_path, input_key)
    if not tail_text and not section_title:
        return {
            "text": "",
            "tail_text": "",
            "section_title": "",
            "source_chunk_id": None,
            "token_count": 0,
        }

    parts = [
        "## Continuity Context",
        "",
        "Use this only as continuity reference from the previous chunk.",
        "Do not repeat or rewrite this context in the output.",
        "",
    ]
    if section_title:
        parts.extend(["Previous section title:", section_title, ""])
    if tail_text:
        parts.extend(["Previous chunk tail:", tail_text])

    context_text = "\n".join(parts).strip()
    return {
        "text": context_text,
        "tail_text": tail_text,
        "section_title": section_title,
        "source_chunk_id": previous_chunk.get("id"),
        "token_count": _estimate_tokens(context_text, "tokens", config),
    }


def _inject_continuity_context(prompt_template: str, continuity_text: str) -> str:
    if not continuity_text:
        return prompt_template

    input_anchor = "\n## Input Text\n"
    if input_anchor in prompt_template:
        return prompt_template.replace(input_anchor, f"\n{continuity_text}\n\n## Input Text\n", 1)
    return prompt_template.rstrip() + "\n\n" + continuity_text + "\n"


def _build_chunk_prompt(prompt_template: str, chunk_body: str,
                        continuity_text: str = "") -> str:
    template = _inject_continuity_context(prompt_template, continuity_text)
    if "{RAW_TEXT}" in template:
        return template.replace("{RAW_TEXT}", chunk_body)
    if "{STRUCTURED_TEXT}" in template:
        return template.replace("{STRUCTURED_TEXT}", chunk_body)
    return template.rstrip() + "\n\n" + chunk_body


def _estimate_continuity_reserve_tokens(config: dict | None = None) -> int:
    config = config or {}
    tail_sentences = _parse_int_min(
        config.get("chunk_context_tail_sentences"),
        DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
        0,
    )
    summary_token_cap = _parse_int_min(
        config.get("chunk_context_summary_tokens"),
        DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS,
        0,
    )
    if tail_sentences <= 0:
        return 0

    placeholder_previous_chunk = {
        "id": 0,
        "tail_context_text": _truncate_tail_text_to_tokens(
            "Reference continuity tail sentence for planning.",
            max(1, summary_token_cap),
            config,
        ),
        "last_section_title": "## Previous Section",
    }
    continuity_text = _build_continuity_context(
        placeholder_previous_chunk,
        _skill_root(),
        config,
    )["text"]
    return _estimate_tokens(continuity_text, "tokens", config)


def _calculate_chunk_budget(prompt_name: str, prompt_template: str,
                            config: dict | None = None, model_info: dict = None) -> dict:
    config = config or {}
    prompt_template_tokens = _estimate_tokens(prompt_template or "", "tokens", config)
    continuity_reserve_tokens = _estimate_continuity_reserve_tokens(config)
    prompt_tokens = prompt_template_tokens + continuity_reserve_tokens
    target_tokens = _get_task_chunk_target(prompt_name, config)
    output_ratio = _get_task_output_ratio(prompt_name, config)
    task_max_output_tokens = _get_task_max_output_tokens(prompt_name, config)
    safety_buffer_tokens = _parse_int_min(
        config.get("chunk_safety_buffer_tokens"), DEFAULT_CHUNK_SAFETY_BUFFER_TOKENS, 0
    )
    hard_cap_multiplier = _parse_float_range(
        config.get("chunk_hard_cap_multiplier"),
        DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
        1.0,
        MAX_CHUNK_HARD_CAP_MULTIPLIER,
    )
    task_request_cap = _get_task_request_cap(prompt_name)
    context_window = DEFAULT_CONTEXT_WINDOW
    if model_info and model_info.get("context_window"):
        context_window = max(1024, _parse_int(model_info.get("context_window"), DEFAULT_CONTEXT_WINDOW))
    effective_budget = min(task_request_cap, int(context_window * DEFAULT_CONTEXT_UTILIZATION_LIMIT))

    expected_output_tokens = min(task_max_output_tokens, int(math.ceil(target_tokens * output_ratio)))
    for _ in range(2):
        available_input_tokens = max(1, effective_budget - prompt_tokens - expected_output_tokens - safety_buffer_tokens)
        target_tokens = min(target_tokens, available_input_tokens)
        expected_output_tokens = min(task_max_output_tokens, int(math.ceil(target_tokens * output_ratio)))

    available_input_tokens = max(1, effective_budget - prompt_tokens - expected_output_tokens - safety_buffer_tokens)
    planned_max_output_tokens = max(
        expected_output_tokens,
        min(task_max_output_tokens, effective_budget - prompt_tokens - target_tokens - safety_buffer_tokens),
    )
    hard_cap_tokens = max(target_tokens, int(math.ceil(target_tokens * hard_cap_multiplier)))
    hard_cap_tokens = min(hard_cap_tokens, available_input_tokens)
    hard_cap_tokens = max(target_tokens, hard_cap_tokens)

    return {
        "prompt_name": prompt_name,
        "prompt_template_tokens": prompt_template_tokens,
        "continuity_reserve_tokens": continuity_reserve_tokens,
        "prompt_tokens": prompt_tokens,
        "target_tokens": target_tokens,
        "hard_cap_tokens": hard_cap_tokens,
        "output_ratio": output_ratio,
        "planned_max_output_tokens": planned_max_output_tokens,
        "expected_output_tokens": expected_output_tokens,
        "available_input_tokens": available_input_tokens,
        "effective_budget_tokens": effective_budget,
        "safety_buffer_tokens": safety_buffer_tokens,
        "chunk_mode": _normalize_chunk_mode(config.get("chunk_mode", DEFAULT_CHUNK_MODE)),
        "request_cap_tokens": task_request_cap,
        "token_count_source": "local_estimate",
    }


def _recommended_chunk_size(prompt_name: str = "", config: dict | None = None) -> int:
    config = config or {}
    if _normalize_chunk_mode(config.get("chunk_mode", DEFAULT_CHUNK_MODE)) == "chars":
        return _legacy_chunk_target_chars(prompt_name, config)
    return _get_task_chunk_target(prompt_name, config)


def _new_plan_id() -> str:
    return f"plan_{time.strftime('%Y%m%d%H%M%S', time.localtime())}_{random.randint(1000, 9999)}"


def _build_manifest_plan(prompt_name: str, chunk_mode: str, recommended_chunk_size: int,
                         effective_chunk_size: int, budget: dict, *, source_file: str = "",
                         plan_id: str = "", prior_plan_id: str = "") -> dict:
    return {
        "plan_id": plan_id or _new_plan_id(),
        "prior_plan_id": prior_plan_id,
        "prompt_name": prompt_name,
        "chunk_mode": _normalize_chunk_mode(chunk_mode),
        "chunk_size": effective_chunk_size,
        "recommended_chunk_size": recommended_chunk_size,
        "target_input_tokens": budget.get("target_tokens", 0),
        "target_tokens": budget.get("target_tokens", 0),
        "hard_cap_tokens": budget.get("hard_cap_tokens", 0),
        "planned_max_output_tokens": budget.get("planned_max_output_tokens", 0),
        "prompt_tokens": budget.get("prompt_tokens", 0),
        "prompt_template_tokens": budget.get("prompt_template_tokens", 0),
        "effective_budget_tokens": budget.get("effective_budget_tokens", 0),
        "output_ratio": budget.get("output_ratio", DEFAULT_UNKNOWN_OUTPUT_RATIO),
        "chunk_safety_buffer_tokens": budget.get("safety_buffer_tokens", 0),
        "continuity_reserve_tokens": budget.get("continuity_reserve_tokens", 0),
        "token_count_source": budget.get("token_count_source", ""),
        "source_file": source_file,
        "created_at": _now_iso(),
    }


def _build_manifest_runtime(plan_id: str, request_url: str = "") -> dict:
    return {
        "status": "pending",
        "active_plan_id": plan_id,
        "last_request_url": request_url,
        "processed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "superseded_count": 0,
        "current_chunk_index": 0,
        "replan_required": False,
        "replan_reason": "",
        "last_replanned_at": "",
        "updated_at": _now_iso(),
    }


def _new_chunk_manifest_entry(chunk_id: int, chunk_content: str, budget: dict,
                              config: dict | None = None, *, raw_path: str = "",
                              processed_path: str = "", plan_id: str = "",
                              continuity_prev_chunk_id: int | None = None) -> dict:
    config = config or {}
    return {
        "id": chunk_id,
        "chunk_id": chunk_id,
        "plan_id": plan_id,
        "raw_path": raw_path,
        "processed_path": processed_path,
        "char_count": len(chunk_content),
        "input_chars": len(chunk_content),
        "estimated_input_tokens": _estimate_tokens(chunk_content, "tokens", config),
        "input_tokens": _estimate_tokens(chunk_content, "tokens", config),
        "token_count_source": budget.get("token_count_source", ""),
        "tail_context_text": _extract_tail_sentences(
            chunk_content,
            _parse_int_min(
                config.get("chunk_context_tail_sentences"),
                DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
                0,
            ),
            config,
        ),
        "processed_tail_context_text": "",
        "processed_input_tail_context_text": "",
        "processed_input_section_title": "",
        "continuity_prev_chunk_id": continuity_prev_chunk_id,
        "continuity_context_chars": 0,
        "continuity_context_tokens": 0,
        "continuity_section_title": "",
        "last_section_title": "",
        "output_chars": 0,
        "actual_output_tokens": 0,
        "planned_max_output_tokens": budget.get("planned_max_output_tokens", 0),
        "status": "pending",
        "attempts": 0,
        "attempt_logs": [],
        "last_error": "",
        "last_error_type": "",
        "error_type": "",
        "latency_ms": None,
        "request_url": "",
        "streaming_used": False,
        "autotune_target_tokens": 0,
        "autotune_next_target_tokens": 0,
        "autotune_event": "",
        "autotune_reason": "",
        "superseded_by_plan_id": "",
        "updated_at": "",
        "started_at": "",
        "completed_at": "",
    }


def _sync_manifest_legacy_fields(manifest: dict) -> dict:
    plan = manifest.get("plan", {}) if isinstance(manifest, dict) else {}
    runtime = manifest.get("runtime", {}) if isinstance(manifest, dict) else {}
    autotune = manifest.get("autotune", {}) if isinstance(manifest, dict) else {}

    manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["last_prompt"] = plan.get("prompt_name", manifest.get("last_prompt", ""))
    manifest["last_request_url"] = runtime.get("last_request_url", manifest.get("last_request_url", ""))
    manifest["recommended_chunk_size"] = plan.get("recommended_chunk_size", manifest.get("recommended_chunk_size", 0))
    manifest["chunk_size"] = plan.get("chunk_size", manifest.get("chunk_size", 0))
    manifest["chunk_mode"] = plan.get("chunk_mode", manifest.get("chunk_mode", DEFAULT_CHUNK_MODE))
    manifest["prompt_name"] = plan.get("prompt_name", manifest.get("prompt_name", ""))
    manifest["target_tokens"] = plan.get("target_input_tokens", manifest.get("target_tokens", 0))
    manifest["hard_cap_tokens"] = plan.get("hard_cap_tokens", manifest.get("hard_cap_tokens", 0))
    manifest["prompt_tokens"] = plan.get("prompt_tokens", manifest.get("prompt_tokens", 0))
    manifest["planned_max_output_tokens"] = plan.get("planned_max_output_tokens", manifest.get("planned_max_output_tokens", 0))
    manifest["effective_budget_tokens"] = plan.get("effective_budget_tokens", manifest.get("effective_budget_tokens", 0))
    manifest["output_ratio"] = plan.get("output_ratio", manifest.get("output_ratio", DEFAULT_UNKNOWN_OUTPUT_RATIO))
    manifest["chunk_safety_buffer_tokens"] = plan.get("chunk_safety_buffer_tokens", manifest.get("chunk_safety_buffer_tokens", 0))
    manifest["continuity_reserve_tokens"] = plan.get("continuity_reserve_tokens", manifest.get("continuity_reserve_tokens", 0))
    manifest["token_count_source"] = str(manifest.get("token_count_source", "")).strip() or plan.get("token_count_source", "")
    manifest["autotune"] = autotune
    manifest["replan_required"] = runtime.get("replan_required", manifest.get("replan_required", False))
    manifest["replan_reason"] = runtime.get("replan_reason", manifest.get("replan_reason", ""))
    return manifest


def _ensure_manifest_structure(manifest: dict, *, prompt_name: str = "", prompt_budget: dict | None = None,
                               recommended_chunk_size: int = 0, request_url: str = "",
                               source_file: str = "") -> dict:
    manifest = manifest if isinstance(manifest, dict) else {}
    prompt_budget = prompt_budget or {}
    chunk_mode = _normalize_chunk_mode(
        manifest.get("chunk_mode", prompt_budget.get("chunk_mode", DEFAULT_CHUNK_MODE))
    )
    effective_chunk_size = max(0, _parse_int(manifest.get("chunk_size"), 0))
    if not isinstance(manifest.get("plan"), dict):
        manifest["plan"] = _build_manifest_plan(
            prompt_name or str(manifest.get("last_prompt", manifest.get("prompt_name", ""))).strip(),
            chunk_mode,
            recommended_chunk_size or max(0, _parse_int(manifest.get("recommended_chunk_size"), effective_chunk_size)),
            effective_chunk_size,
            {
                "target_tokens": max(0, _parse_int(manifest.get("target_tokens"), prompt_budget.get("target_tokens", 0))),
                "hard_cap_tokens": max(0, _parse_int(manifest.get("hard_cap_tokens"), prompt_budget.get("hard_cap_tokens", 0))),
                "planned_max_output_tokens": max(0, _parse_int(manifest.get("planned_max_output_tokens"), prompt_budget.get("planned_max_output_tokens", 0))),
                "prompt_tokens": max(0, _parse_int(manifest.get("prompt_tokens"), prompt_budget.get("prompt_tokens", 0))),
                "prompt_template_tokens": max(0, _parse_int(manifest.get("prompt_template_tokens"), prompt_budget.get("prompt_template_tokens", 0))),
                "effective_budget_tokens": max(0, _parse_int(manifest.get("effective_budget_tokens"), prompt_budget.get("effective_budget_tokens", 0))),
                "output_ratio": _parse_float(manifest.get("output_ratio"), prompt_budget.get("output_ratio", DEFAULT_UNKNOWN_OUTPUT_RATIO)),
                "safety_buffer_tokens": max(0, _parse_int(manifest.get("chunk_safety_buffer_tokens"), prompt_budget.get("safety_buffer_tokens", 0))),
                "continuity_reserve_tokens": max(0, _parse_int(manifest.get("continuity_reserve_tokens"), prompt_budget.get("continuity_reserve_tokens", 0))),
                "token_count_source": str(manifest.get("token_count_source", prompt_budget.get("token_count_source", ""))).strip(),
            },
            source_file=source_file or str(manifest.get("source_file", "")).strip(),
            plan_id=str(manifest.get("plan_id", "")).strip(),
        )
    if not isinstance(manifest.get("runtime"), dict):
        manifest["runtime"] = _build_manifest_runtime(
            manifest["plan"].get("plan_id", _new_plan_id()),
            request_url=request_url or str(manifest.get("last_request_url", "")).strip(),
        )
    runtime = manifest["runtime"]
    runtime.setdefault("status", "pending")
    runtime.setdefault("active_plan_id", manifest["plan"].get("plan_id", _new_plan_id()))
    runtime.setdefault("last_request_url", request_url or str(manifest.get("last_request_url", "")).strip())
    runtime.setdefault("processed_count", 0)
    runtime.setdefault("failed_count", 0)
    runtime.setdefault("skipped_count", 0)
    runtime.setdefault("superseded_count", 0)
    runtime.setdefault("current_chunk_index", 0)
    runtime.setdefault("replan_required", False)
    runtime.setdefault("replan_reason", "")
    runtime.setdefault("last_replanned_at", "")
    runtime.setdefault("updated_at", _now_iso())
    manifest.setdefault("plan_history", [])
    _sync_manifest_legacy_fields(manifest)
    return manifest


def _estimate_p95(values: list[int]) -> int | None:
    cleaned = sorted(
        max(0, _parse_int(value, 0))
        for value in (values or [])
        if max(0, _parse_int(value, 0)) > 0
    )
    if not cleaned:
        return None
    index = max(0, min(len(cleaned) - 1, int(math.ceil(len(cleaned) * 0.95)) - 1))
    return cleaned[index]


def _build_autotune_state(prompt_budget: dict, config: dict | None = None,
                          existing: dict | None = None) -> dict:
    config = config or {}
    existing = existing if isinstance(existing, dict) else {}

    enabled = _parse_bool(config.get("enable_chunk_autotune"), DEFAULT_ENABLE_CHUNK_AUTOTUNE)
    reduce_percent = _parse_float_range(
        config.get("autotune_reduce_percent"),
        DEFAULT_AUTOTUNE_REDUCE_PERCENT,
        0.01,
        0.90,
    )
    increase_percent = _parse_float_range(
        config.get("autotune_increase_percent"),
        DEFAULT_AUTOTUNE_INCREASE_PERCENT,
        0.01,
        0.50,
    )
    success_window = _parse_int_min(
        config.get("autotune_success_window"),
        DEFAULT_AUTOTUNE_SUCCESS_WINDOW,
        1,
    )
    p95_latency_threshold_ms = _parse_int_min(
        config.get("autotune_p95_latency_threshold_ms"),
        DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS,
        1,
    )

    base_target_tokens = max(1, _parse_int(prompt_budget.get("target_tokens"), DEFAULT_UNKNOWN_CHUNK_TOKENS))
    max_target_tokens = max(
        base_target_tokens,
        _parse_int(prompt_budget.get("hard_cap_tokens"), base_target_tokens),
    )
    min_target_tokens = max(1, int(math.floor(base_target_tokens * DEFAULT_AUTOTUNE_MIN_TARGET_RATIO)))
    min_target_tokens = min(min_target_tokens, base_target_tokens)

    latency_window_ms = [
        max(0, _parse_int(value, 0))
        for value in existing.get("latency_window_ms", [])
        if max(0, _parse_int(value, 0)) > 0
    ][-success_window:]

    current_target_tokens = max(1, _parse_int(existing.get("current_target_tokens"), base_target_tokens))
    current_target_tokens = max(min_target_tokens, min(max_target_tokens, current_target_tokens))

    state = {
        "enabled": enabled,
        "base_target_tokens": base_target_tokens,
        "current_target_tokens": current_target_tokens,
        "min_target_tokens": min_target_tokens,
        "max_target_tokens": max_target_tokens,
        "reduce_percent": reduce_percent,
        "increase_percent": increase_percent,
        "success_window": success_window,
        "p95_latency_threshold_ms": p95_latency_threshold_ms,
        "latency_window_ms": latency_window_ms,
        "p95_latency_ms": _estimate_p95(latency_window_ms),
        "consecutive_successes": max(0, _parse_int(existing.get("consecutive_successes"), 0)),
        "last_event": str(existing.get("last_event", "")).strip(),
        "last_reason": str(existing.get("last_reason", "")).strip(),
        "updated_at": str(existing.get("updated_at", "")).strip(),
        "current_planned_max_output_tokens": max(
            1,
            _parse_int(
                existing.get("current_planned_max_output_tokens"),
                prompt_budget.get("planned_max_output_tokens", DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS),
            ),
        ),
    }
    return state


def _update_autotune_state(autotune_state: dict | None, *, success: bool,
                           latency_ms: int | None = None, timeout: bool = False,
                           error_type: str = "", chunk_id: int | None = None) -> dict:
    state = dict(autotune_state or {})
    state.setdefault("enabled", False)
    state.setdefault("current_target_tokens", 0)
    state.setdefault("min_target_tokens", 1)
    state.setdefault("max_target_tokens", max(1, state.get("current_target_tokens", 1)))
    state.setdefault("reduce_percent", DEFAULT_AUTOTUNE_REDUCE_PERCENT)
    state.setdefault("increase_percent", DEFAULT_AUTOTUNE_INCREASE_PERCENT)
    state.setdefault("success_window", DEFAULT_AUTOTUNE_SUCCESS_WINDOW)
    state.setdefault("p95_latency_threshold_ms", DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS)
    state.setdefault("latency_window_ms", [])
    state["last_event"] = ""
    state["last_reason"] = ""
    state["updated_at"] = _now_iso()

    if not state.get("enabled"):
        return state

    current_target_tokens = max(1, _parse_int(state.get("current_target_tokens"), 1))
    min_target_tokens = max(1, _parse_int(state.get("min_target_tokens"), 1))
    max_target_tokens = max(current_target_tokens, _parse_int(state.get("max_target_tokens"), current_target_tokens))
    reduce_percent = _parse_float_range(state.get("reduce_percent"), DEFAULT_AUTOTUNE_REDUCE_PERCENT, 0.01, 0.90)
    increase_percent = _parse_float_range(state.get("increase_percent"), DEFAULT_AUTOTUNE_INCREASE_PERCENT, 0.01, 0.50)
    success_window = _parse_int_min(state.get("success_window"), DEFAULT_AUTOTUNE_SUCCESS_WINDOW, 1)
    threshold_ms = _parse_int_min(
        state.get("p95_latency_threshold_ms"),
        DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS,
        1,
    )

    latency_window_ms = [
        max(0, _parse_int(value, 0))
        for value in state.get("latency_window_ms", [])
        if max(0, _parse_int(value, 0)) > 0
    ][-success_window:]

    if success:
        parsed_latency = max(0, _parse_int(latency_ms, 0))
        if parsed_latency > 0:
            latency_window_ms = (latency_window_ms + [parsed_latency])[-success_window:]
        state["latency_window_ms"] = latency_window_ms
        state["p95_latency_ms"] = _estimate_p95(latency_window_ms)
        state["consecutive_successes"] = max(0, _parse_int(state.get("consecutive_successes"), 0)) + 1

        if state["p95_latency_ms"] is not None and state["p95_latency_ms"] > threshold_ms:
            next_target = max(
                min_target_tokens,
                int(math.floor(current_target_tokens * (1.0 - reduce_percent))),
            )
            next_target = min(max_target_tokens, max(1, next_target))
            if next_target < current_target_tokens:
                state["current_target_tokens"] = next_target
                state["last_event"] = "shrink"
                state["last_reason"] = (
                    f"p95 latency {state['p95_latency_ms']}ms exceeded threshold {threshold_ms}ms"
                )
            else:
                state["last_event"] = "steady"
                state["last_reason"] = "p95 latency exceeded threshold but target already at minimum"
            state["consecutive_successes"] = 0
            return state

        if state["consecutive_successes"] >= success_window:
            next_target = min(
                max_target_tokens,
                int(math.ceil(current_target_tokens * (1.0 + increase_percent))),
            )
            next_target = max(min_target_tokens, max(1, next_target))
            if next_target > current_target_tokens:
                state["current_target_tokens"] = next_target
                state["last_event"] = "increase"
                state["last_reason"] = (
                    f"{success_window} consecutive successful chunks stayed within latency threshold"
                )
            else:
                state["last_event"] = "steady"
                state["last_reason"] = "success window reached but target already at maximum"
            state["consecutive_successes"] = 0
            return state

        return state

    state["consecutive_successes"] = 0
    if timeout:
        next_target = max(
            min_target_tokens,
            int(math.floor(current_target_tokens * (1.0 - reduce_percent))),
        )
        next_target = min(max_target_tokens, max(1, next_target))
        if next_target < current_target_tokens:
            state["current_target_tokens"] = next_target
            state["last_event"] = "shrink"
            chunk_label = f" chunk {chunk_id}" if chunk_id is not None else ""
            state["last_reason"] = f"timeout on{chunk_label}; reduce target for future requests"
        else:
            state["last_event"] = "steady"
            state["last_reason"] = "timeout observed but target already at minimum"
        return state

    if error_type:
        state["last_event"] = "observe"
        state["last_reason"] = f"{error_type} did not change target"
    return state


def _build_attempt_log_from_result(result: dict, attempt_index: int | None = None) -> dict:
    attempt_number = max(1, _parse_int(attempt_index or result.get("attempts"), 1))
    return {
        "attempt_index": attempt_number,
        "result": "success",
        "error_type": "",
        "status_code": None,
        "latency_ms": result.get("latency_ms"),
        "request_url": result.get("request_url", ""),
        "streaming_used": bool(result.get("streaming_used", False)),
        "retryable": False,
    }


def _build_attempt_log_from_error(error: Exception, attempt_index: int | None = None) -> dict:
    status_code = getattr(error, "status_code", None)
    error_type = str(getattr(error, "error_type", "unknown") or "unknown")
    return {
        "attempt_index": max(1, _parse_int(attempt_index or getattr(error, "attempts", 1), 1)),
        "result": "failure",
        "error_type": error_type,
        "status_code": status_code,
        "latency_ms": None,
        "request_url": str(getattr(error, "request_url", "") or ""),
        "streaming_used": False,
        "retryable": bool(getattr(error, "retryable", False)),
    }


def _collect_attempt_logs(result_or_error) -> list[dict]:
    attempt_logs = getattr(result_or_error, "attempt_history", None)
    if attempt_logs is None and isinstance(result_or_error, dict):
        attempt_logs = result_or_error.get("attempt_history")
    if isinstance(attempt_logs, list) and attempt_logs:
        return [dict(log) for log in attempt_logs if isinstance(log, dict)]
    if isinstance(result_or_error, dict):
        return [_build_attempt_log_from_result(result_or_error)]
    return [_build_attempt_log_from_error(result_or_error)]


def _has_timeout_attempt(attempt_logs: list[dict]) -> bool:
    for attempt_log in attempt_logs or []:
        if str(attempt_log.get("error_type", "")).strip() in {"timeout", "socket_timeout", "read_timeout"}:
            return True
    return False


def _should_replan_after_error(error: Exception) -> bool:
    if _is_timeout_error(error):
        return True
    status_code = getattr(error, "status_code", None)
    response_hint = str(getattr(error, "response_body", "") or "").lower()
    error_type = str(getattr(error, "error_type", "") or "")
    if status_code in {413, 422}:
        return True
    if status_code == 400 and any(token in response_hint for token in ("context", "prompt", "max token", "too long", "token limit")):
        return True
    return error_type in {"bad_response"}


def _find_previous_active_chunk(chunks: list[dict], current_index: int) -> dict | None:
    for index in range(current_index - 1, -1, -1):
        previous_chunk = chunks[index]
        if previous_chunk.get("status") == SUPERSEDED_CHUNK_STATUS:
            continue
        return previous_chunk
    return None


def _available_prompt_names() -> list[str]:
    return sorted(p.stem for p in (_skill_root() / "prompts").glob("*.md"))


def _resolve_prompt_template_path(prompt_name: str) -> Path:
    prompt = (prompt_name or "").strip()
    if not prompt:
        raise ValueError("Prompt name is required")
    if not re.fullmatch(r"[A-Za-z0-9_]+", prompt):
        raise ValueError(f"Invalid prompt name: {prompt}")

    prompt_path = (_skill_root() / "prompts" / f"{prompt}.md").resolve()
    prompts_root = (_skill_root() / "prompts").resolve()
    try:
        prompt_path.relative_to(prompts_root)
    except ValueError as exc:
        raise ValueError(f"Invalid prompt name: {prompt}") from exc
    if not prompt_path.exists():
        raise ValueError(f"Prompt template not found: {prompt_path}")
    return prompt_path


def _load_optional_config(config_path: str = None) -> dict:
    if config_path is None or not str(config_path).strip():
        return load_config(None, allow_missing=True)
    return load_config(config_path, allow_missing=False)


def _force_split_text_by_tokens(text: str, max_tokens: int, config: dict | None = None) -> list[str]:
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


def _force_split_text(text: str, max_size: int, chunk_mode: str,
                      config: dict | None = None) -> list[str]:
    if _normalize_chunk_mode(chunk_mode) == "chars":
        return _hard_split_text(text, max_size)
    return _force_split_text_by_tokens(text, max_size, config)


def _build_chunk_plan(prompt_name: str, chunk_size: int, config: dict,
                      prompt_template: str) -> dict:
    requested_chunk_mode = _normalize_chunk_mode(config.get("chunk_mode", DEFAULT_CHUNK_MODE))
    use_legacy_char_override = requested_chunk_mode == "tokens" and chunk_size and chunk_size > 0 and not prompt_name
    chunk_mode = "chars" if use_legacy_char_override else requested_chunk_mode
    budget = _calculate_chunk_budget(prompt_name, prompt_template, config)
    recommended_chunk_size = (
        _legacy_chunk_target_chars(prompt_name, config)
        if chunk_mode == "chars"
        else _get_task_chunk_target(prompt_name, config)
    )
    hard_cap_multiplier = _parse_float_range(
        config.get("chunk_hard_cap_multiplier"),
        DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
        1.0,
        MAX_CHUNK_HARD_CAP_MULTIPLIER,
    )

    if chunk_mode == "chars":
        effective_chunk_size = chunk_size if chunk_size and chunk_size > 0 else recommended_chunk_size
        hard_cap_size = effective_chunk_size
        target_tokens = budget["target_tokens"]
        hard_cap_tokens = budget["hard_cap_tokens"]
    else:
        effective_chunk_size = chunk_size if chunk_size and chunk_size > 0 else budget["target_tokens"]
        effective_chunk_size = max(1, min(effective_chunk_size, budget["available_input_tokens"]))
        hard_cap_size = max(effective_chunk_size, int(math.ceil(effective_chunk_size * hard_cap_multiplier)))
        hard_cap_size = min(hard_cap_size, budget["hard_cap_tokens"])
        hard_cap_size = max(effective_chunk_size, hard_cap_size)
        target_tokens = effective_chunk_size
        hard_cap_tokens = hard_cap_size

    return {
        "budget": budget,
        "chunk_mode": chunk_mode,
        "use_legacy_char_override": use_legacy_char_override,
        "recommended_chunk_size": recommended_chunk_size,
        "effective_chunk_size": effective_chunk_size,
        "hard_cap_size": hard_cap_size,
        "target_tokens": target_tokens,
        "hard_cap_tokens": hard_cap_tokens,
    }


def _split_text_into_chunks(sentences: list[str], chunk_mode: str,
                            effective_chunk_size: int, hard_cap_size: int,
                            config: dict) -> tuple[list[str], list[str]]:
    chunks = []
    current_chunk = []
    current_size = 0
    warnings = []
    separator_size = len(CHUNK_SEPARATOR) if chunk_mode == "chars" else _estimate_tokens(CHUNK_SEPARATOR, "tokens", config)

    for i, sentence in enumerate(sentences):
        sentence_segments = [sentence]
        sentence_len = _estimate_tokens(sentence, chunk_mode, config)

        if sentence_len > hard_cap_size:
            sentence_segments = _force_split_text(sentence, hard_cap_size, chunk_mode, config)
            warnings.append(
                f"Sentence {i} exceeds chunk_size ({sentence_len} > {hard_cap_size}), split into {len(sentence_segments)} fixed-width segment(s)"
            )

        for segment in sentence_segments:
            segment_len = _estimate_tokens(segment, chunk_mode, config)
            candidate_size = current_size + segment_len + (separator_size if current_chunk else 0)
            if current_chunk and (candidate_size > effective_chunk_size or candidate_size > hard_cap_size):
                chunks.append(CHUNK_SEPARATOR.join(current_chunk))
                current_chunk = [segment]
                current_size = segment_len
            else:
                current_chunk.append(segment)
                current_size = candidate_size

    if current_chunk:
        chunks.append(CHUNK_SEPARATOR.join(current_chunk))
    return chunks, warnings


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _write_manifest(manifest_path: Path, manifest: dict) -> None:
    _atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _is_timeout_error(error: Exception) -> bool:
    if isinstance(error, LLMRequestError):
        if error.status_code in {408, 504}:
            return True
        return error.error_type in {"timeout", "socket_timeout", "read_timeout"}
    return False


def _extract_openai_stream_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content", "")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def _extract_anthropic_stream_text(payload: dict) -> str:
    event_type = payload.get("type", "")
    if event_type == "content_block_start":
        block = payload.get("content_block") or {}
        if block.get("type") == "text":
            return block.get("text", "")
    if event_type == "content_block_delta":
        delta = payload.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
    return ""


def _extract_llm_text(result: dict, api_format: str) -> str:
    if api_format == "anthropic":
        content = result.get("content") or []
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        if text_parts:
            return "".join(text_parts)
        raise KeyError("content")
    return result["choices"][0]["message"]["content"]


def _read_streaming_response(response, api_format: str) -> str:
    text_parts = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith("event:"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if api_format == "anthropic":
            text = _extract_anthropic_stream_text(payload)
        else:
            text = _extract_openai_stream_text(payload)
        if text:
            text_parts.append(text)
    return "".join(text_parts)


def _build_llm_request(api_key: str, base_url: str, model: str, messages: list,
                       api_format: str = "openai", max_tokens: int = 8192,
                       temperature: float = 0.3, use_stream: bool = False) -> tuple[str, dict, bytes]:
    if api_format == "anthropic":
        url = _build_api_url(base_url, api_format)
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": "yt-transcript/4.0"
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages
        }
    else:
        url = _build_api_url(base_url, api_format)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "yt-transcript/4.0"
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages
        }

    if use_stream:
        body["stream"] = True

    return url, headers, json.dumps(body).encode("utf-8")


def _execute_llm_request(api_key: str, base_url: str, model: str, messages: list,
                         api_format: str = "openai", max_tokens: int = 8192,
                         temperature: float = 0.3, timeout_sec: int = 120,
                         use_stream: bool = False) -> dict:
    import urllib.error
    import urllib.request

    url, headers, data = _build_llm_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=messages,
        api_format=api_format,
        max_tokens=max_tokens,
        temperature=temperature,
        use_stream=use_stream,
    )
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        started_at = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if use_stream and "text/event-stream" in content_type:
                text = _read_streaming_response(response, api_format)
                streaming_used = True
            else:
                result = json.loads(response.read().decode("utf-8"))
                text = _extract_llm_text(result, api_format)
                streaming_used = False
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return {
            "text": text,
            "latency_ms": latency_ms,
            "request_url": url,
            "streaming_used": streaming_used,
        }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        status_code = e.code
        raise LLMRequestError(
            f"HTTP {status_code}: {error_body}",
            error_type=f"http_{status_code}",
            status_code=status_code,
            retryable=_is_retryable_status(status_code),
            request_url=url,
            response_body=error_body,
        ) from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        message = str(reason)
        retryable = isinstance(reason, socket.timeout) or "timed out" in message.lower()
        raise LLMRequestError(
            f"Cannot reach LLM API: {message}",
            error_type="timeout" if retryable else "network",
            retryable=retryable,
            request_url=url,
        ) from e
    except (socket.timeout, TimeoutError) as e:
        raise LLMRequestError(
            f"LLM API call timed out: {e}",
            error_type="timeout",
            retryable=True,
            request_url=url,
        ) from e
    except (KeyError, IndexError) as e:
        raise LLMRequestError(
            f"Unexpected API response structure: {e}",
            error_type="bad_response",
            retryable=False,
            request_url=url,
        ) from e
    except Exception as e:
        message = str(e)
        is_timeout = "timed out" in message.lower()
        raise LLMRequestError(
            f"LLM API call failed: {e}",
            error_type="timeout" if is_timeout else "unknown",
            retryable=is_timeout,
            request_url=url,
        ) from e


def _split_sentences(text: str) -> list[str]:
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
        for char in normalized[index + 1:]:
            if not char.isspace():
                return char
        return ""

    def previous_ascii_word(index: int) -> str:
        match = re.search(r"([A-Za-z]+)$", normalized[:index])
        return match.group(1) if match else ""

    def acronym_before_period(index: int) -> bool:
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


def _hard_split_text(text: str, max_len: int) -> list[str]:
    """
    Force-split overlong text into fixed-width chunks as a last-resort fallback.
    """
    if max_len <= 0:
        return [text]
    return [text[i:i + max_len].strip() for i in range(0, len(text), max_len) if text[i:i + max_len].strip()]


def parse_vtt(vtt_path: str) -> str:
    """
    Parse VTT subtitle file, extract plain text

    Processing:
    - Remove VTT header (WEBVTT, Kind:, Language:)
    - Remove timestamp lines (00:00:00.000 --> 00:00:05.000)
    - Remove VTT tags (<c>, </c>, <00:00:01.000>, etc.)
    - Remove cue numbers (pure digit lines)
    - Remove consecutive duplicate lines (common in auto-captions)
    """
    path = Path(vtt_path)
    if not path.exists():
        print(f"Error: File does not exist {vtt_path}", file=sys.stderr)
        sys.exit(1)

    try:
        content = path.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    lines = content.split('\n')
    text_lines = []

    for line in lines:
        # Skip timestamp lines
        if '-->' in line:
            continue
        # Skip VTT header
        if line.startswith('WEBVTT') or line.startswith('Kind:') or line.startswith('Language:'):
            continue
        # Skip empty lines and pure digit lines (cue numbers)
        if not line.strip() or line.strip().isdigit():
            continue
        # Remove VTT tags
        clean_line = re.sub(r'<[^>]+>', '', line)
        if clean_line.strip():
            text_lines.append(clean_line.strip())

    # Remove consecutive duplicate lines
    deduplicated = []
    for line in text_lines:
        if not deduplicated or line != deduplicated[-1]:
            deduplicated.append(line)

    return ' '.join(deduplicated)


def process_deepgram(json_path: str) -> dict:
    """
    Process Deepgram API JSON result

    Processing:
    - Extract complete transcript text
    - Remove spaces between Chinese characters (multiple passes for thoroughness)
    - Fix spaces around punctuation
    - Remove consecutive repeated phrases
    - Count number of speakers

    Returns:
        {"transcript": "cleaned text", "speaker_count": N}
    """
    path = Path(json_path)
    if not path.exists():
        print(f"Error: File does not exist {json_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        print(f"Error: JSON parsing failed {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    try:
        return process_deepgram_payload(data)
    except (KeyError, IndexError) as e:
        print(f"Error: Deepgram JSON structure unexpected {e}", file=sys.stderr)
        sys.exit(2)


def process_deepgram_payload(data: dict) -> dict:
    transcript = data['results']['channels'][0]['alternatives'][0]['transcript']

    # 1. Remove spaces between Chinese characters (multiple passes for thoroughness)
    for _ in range(10):
        transcript = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', transcript)

    # 2. Fix spaces around punctuation
    transcript = re.sub(r'\s+([。，！？、：；])', r'\1', transcript)

    # 3. Remove consecutive repeated phrases (3-20 characters)
    transcript = re.sub(r'([\u4e00-\u9fff]{3,20})\1{1,5}', r'\1', transcript)

    speakers = set()
    try:
        paragraphs = data['results']['channels'][0]['alternatives'][0].get('paragraphs', {}).get('paragraphs', [])
        for para in paragraphs:
            for sent in para.get('sentences', []):
                speaker = sent.get('speaker')
                if speaker is not None:
                    speakers.add(speaker)
    except (KeyError, TypeError):
        pass

    speaker_count = len(speakers) if speakers else 1
    return {
        "transcript": transcript.strip(),
        "speaker_count": speaker_count
    }


def sanitize_filename(title: str) -> str:
    """
    Clean illegal characters from filename

    Processing:
    - Replace illegal characters: / \\ : * ? " < > |
    - Remove leading/trailing spaces and periods
    - Limit length to 200 characters
    """
    # Replace illegal characters
    sanitized = re.sub(r'[/\\:*?"<>|]', '_', title)
    # Remove leading/trailing spaces and periods
    sanitized = sanitized.strip(' .')
    # Limit length
    if len(sanitized) > 200:
        sanitized = sanitized[:200]
    return sanitized


def split_audio(audio_path: str, max_size_mb: float = 10.0, max_deviation_sec: float = 60.0) -> dict:
    """
    Split large audio file based on silence detection
    
    Algorithm:
    1. Calculate rough split points (based on file size and max_size_mb interval)
    2. Use FFmpeg silencedetect to find all silence intervals
    3. For each rough split point, find the nearest silence point (before or after)
    4. If both silence points exceed max_deviation_sec, force split at the rough point
    
    Args:
        audio_path: Path to audio file
        max_size_mb: Max chunk size in MB, default 10MB
        max_deviation_sec: Max allowed deviation in seconds, default 60s
    
    Returns:
        {"chunks": ["path1.mp3", ...], "total_chunks": N, "split_points": [t1, t2, ...]}
    """
    path = Path(audio_path)
    if not path.exists():
        print(f"Error: File does not exist {audio_path}", file=sys.stderr)
        sys.exit(1)
    
    if max_size_mb <= 0:
        print(f"Error: max_size_mb must be positive, got {max_size_mb}", file=sys.stderr)
        sys.exit(1)
    
    if max_deviation_sec < 0:
        print(f"Error: max_deviation_sec must be non-negative, got {max_deviation_sec}", file=sys.stderr)
        sys.exit(1)
    
    file_size = path.stat().st_size
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # If file size is within limit, no splitting needed
    if file_size <= max_size_bytes:
        return {
            "chunks": [str(path)],
            "total_chunks": 1,
            "split_points": [],
            "message": "File size within limit, no splitting needed"
        }
    
    # 1. Get audio duration
    duration_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]
    try:
        result = subprocess.run(duration_cmd, capture_output=True, text=True, check=True)
        total_duration = float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"Error: Cannot get audio duration: {e}", file=sys.stderr)
        sys.exit(2)
    
    # 2. Calculate rough split points (based on file size ratio)
    num_chunks = math.ceil(file_size / max_size_bytes)
    rough_split_times = []
    for i in range(1, num_chunks):
        rough_time = (i / num_chunks) * total_duration
        rough_split_times.append(rough_time)
    
    # 3. Detect silence intervals using FFmpeg
    silence_cmd = [
        "ffmpeg", "-i", str(path), "-af",
        "silencedetect=noise=-30dB:d=0.5",
        "-f", "null", "-"
    ]
    result = subprocess.run(silence_cmd, capture_output=True, text=True)
    # silencedetect output is in stderr, even if returncode is not 0
    silence_output = result.stderr
    
    # Parse silence intervals: silence_start: 10.5 | silence_end: 11.2
    silence_points = []  # [(start, end), ...]
    starts = re.findall(r'silence_start: ([\d.]+)', silence_output)
    ends = re.findall(r'silence_end: ([\d.]+)', silence_output)
    for s, e in zip(starts, ends):
        silence_points.append((float(s), float(e)))
    
    # Calculate midpoint of each silence interval
    silence_midpoints = [(s + e) / 2 for s, e in silence_points]
    
    # Log warning if no silence detected
    if not silence_midpoints:
        print("⚠️ No silence detected in audio, using rough split points", file=sys.stderr)
    
    # 4. Find best split point for each rough point
    actual_split_times = []
    for rough_time in rough_split_times:
        best_point = _find_best_split_point(rough_time, silence_midpoints, max_deviation_sec)
        actual_split_times.append(best_point)
    
    # Deduplicate and sort (avoid selecting same silence point for adjacent rough points)
    actual_split_times = sorted(set(actual_split_times))
    
    # 5. Split audio using FFmpeg
    output_dir = path.parent
    base_name = path.stem
    chunks = []
    
    split_times = [0] + actual_split_times + [total_duration]
    for i in range(len(split_times) - 1):
        start_time = split_times[i]
        end_time = split_times[i + 1]
        duration = end_time - start_time
        chunk_path = output_dir / f"{base_name}_chunk_{i:03d}.mp3"
        
        # -ss before -i for fast seek, using -t (duration) instead of -to
        split_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", str(path),
            "-t", str(duration),
            "-c:a", "libmp3lame", "-q:a", "2",
            str(chunk_path)
        ]
        try:
            subprocess.run(split_cmd, capture_output=True, check=True)
            chunks.append(str(chunk_path))
        except subprocess.CalledProcessError as e:
            print(f"Error: FFmpeg split failed for chunk {i}: {e}", file=sys.stderr)
            sys.exit(2)
    
    return {
        "chunks": chunks,
        "total_chunks": len(chunks),
        "split_points": actual_split_times
    }


def _find_best_split_point(rough_time: float, silence_midpoints: list, max_deviation: float) -> float:
    """
    Find best split point near rough split point (using binary search optimization)
    
    Args:
        rough_time: Rough split time point
        silence_midpoints: List of silence interval midpoints (sorted)
        max_deviation: Max allowed deviation in seconds
    
    Returns:
        Actual split time point
    """
    if not silence_midpoints:
        return rough_time
    
    # Use binary search to find insertion position
    idx = bisect.bisect_left(silence_midpoints, rough_time)
    
    # Get nearest silence points before and after
    prev_silence = silence_midpoints[idx - 1] if idx > 0 else None
    next_silence = silence_midpoints[idx] if idx < len(silence_midpoints) else None
    
    # Calculate distances
    prev_dist = rough_time - prev_silence if prev_silence is not None else float('inf')
    next_dist = next_silence - rough_time if next_silence is not None else float('inf')
    
    # Choose the nearer one
    if prev_dist <= next_dist and prev_dist <= max_deviation:
        return prev_silence
    elif next_dist < prev_dist and next_dist <= max_deviation:
        return next_silence
    else:
        # Both exceed limit, force split at rough point
        return rough_time


def test_deepgram_api(api_key: str) -> dict:
    """
    Quick test of Deepgram API key validity
    
    Makes a minimal request to verify:
    - API key is valid
    - Network connectivity works
    - Account has credits
    
    Returns:
        {"valid": bool, "error": str or None, "balance_warning": bool}
    """
    import urllib.request
    import urllib.error
    
    url = "https://api.deepgram.com/v1/listen?model=nova-2&language=en"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav"
    }
    
    # Send empty audio to trigger auth check (will fail with audio error if auth works)
    req = urllib.request.Request(url, data=b'', headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            # Unexpected success with empty audio
            return {"valid": True, "error": None, "balance_warning": False}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"valid": False, "error": "Invalid API key (401 Unauthorized)", "balance_warning": False}
        elif e.code == 402:
            return {"valid": False, "error": "Insufficient credits (402 Payment Required)", "balance_warning": True}
        elif e.code == 400:
            # Bad request usually means auth worked but audio was invalid
            return {"valid": True, "error": None, "balance_warning": False}
        else:
            return {"valid": False, "error": f"HTTP Error {e.code}: {e.reason}", "balance_warning": False}
    except urllib.error.URLError as e:
        return {"valid": False, "error": f"Network error: {e.reason}", "balance_warning": False}
    except Exception as e:
        return {"valid": False, "error": f"Unexpected error: {e}", "balance_warning": False}


def chunk_text(input_path: str, output_dir: str, chunk_size: int = 0,
               prompt_name: str = "", config_path: str = None) -> dict:
    """
    Split text file into chunks by sentence boundary.

    When chunk_size is omitted or non-positive, choose a prompt-aware default.
    """
    path = Path(input_path)
    if not path.exists():
        print(f"Error: File does not exist {input_path}", file=sys.stderr)
        sys.exit(1)

    config = _load_optional_config(config_path)
    prompt_template = ""
    if prompt_name:
        try:
            prompt_path = _resolve_prompt_template_path(prompt_name)
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            print(f"Available prompts: {_available_prompt_names()}", file=sys.stderr)
            sys.exit(1)
        prompt_template = prompt_path.read_text(encoding="utf-8")

    chunk_plan = _build_chunk_plan(prompt_name, chunk_size, config, prompt_template)
    budget = chunk_plan["budget"]
    chunk_mode = chunk_plan["chunk_mode"]
    use_legacy_char_override = chunk_plan["use_legacy_char_override"]
    recommended_chunk_size = chunk_plan["recommended_chunk_size"]
    effective_chunk_size = chunk_plan["effective_chunk_size"]
    hard_cap_size = chunk_plan["hard_cap_size"]
    target_tokens = chunk_plan["target_tokens"]
    hard_cap_tokens = chunk_plan["hard_cap_tokens"]
    autotune_budget = dict(budget)
    autotune_budget["target_tokens"] = target_tokens
    autotune_budget["hard_cap_tokens"] = hard_cap_tokens
    autotune_budget["chunk_mode"] = chunk_mode
    autotune_state = _build_autotune_state(autotune_budget, config)
    autotune_state["enabled"] = autotune_state["enabled"] and chunk_mode == "tokens"

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        text = path.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    sentences = _split_sentences(text)
    chunks, warnings = _split_text_into_chunks(
        sentences,
        chunk_mode,
        effective_chunk_size,
        hard_cap_size,
        config,
    )

    plan_id = _new_plan_id()
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "total_chunks": len(chunks),
        "chunk_context_tail_sentences": _parse_int_min(
            config.get("chunk_context_tail_sentences"),
            DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
            0,
        ),
        "source_file": str(path.absolute()),
        "work_dir": str(out_dir.absolute()),
        "plan": _build_manifest_plan(
            prompt_name,
            chunk_mode,
            recommended_chunk_size,
            effective_chunk_size,
            {
                **budget,
                "target_tokens": target_tokens,
                "hard_cap_tokens": hard_cap_tokens,
            },
            source_file=str(path.absolute()),
            plan_id=plan_id,
        ),
        "runtime": _build_manifest_runtime(plan_id),
        "autotune": autotune_state,
        "plan_history": [],
        "chunks": [],
    }

    for i, chunk_content in enumerate(chunks):
        chunk_filename = f"chunk_{i:03d}.txt"
        chunk_path = out_dir / chunk_filename
        _atomic_write_text(chunk_path, chunk_content)

        chunk_entry = _new_chunk_manifest_entry(
            i,
            chunk_content,
            budget,
            config,
            raw_path=chunk_filename,
            processed_path=f"processed_{i:03d}.md",
            plan_id=plan_id,
            continuity_prev_chunk_id=i - 1 if i > 0 else None,
        )
        chunk_entry["autotune_target_tokens"] = autotune_state["current_target_tokens"]
        chunk_entry["autotune_next_target_tokens"] = autotune_state["current_target_tokens"]
        manifest["chunks"].append(chunk_entry)

    manifest_path = out_dir / "manifest.json"
    _sync_manifest_legacy_fields(manifest)
    _write_manifest(manifest_path, manifest)

    for warning in warnings:
        print(f"⚠️ {warning}", file=sys.stderr)

    if use_legacy_char_override:
        print(
            "ℹ️ Interpreting explicit chunk_size as characters for backward compatibility; add --prompt to use token-aware auto sizing.",
            file=sys.stderr,
        )

    if prompt_name and chunk_size <= 0:
        print(
            f"ℹ️ Auto-selected chunk_size={effective_chunk_size} ({chunk_mode}) for prompt '{prompt_name}'",
            file=sys.stderr,
        )

    return {
        "total_chunks": len(chunks),
        "manifest_path": str(manifest_path),
        "plan_id": plan_id,
        "chunks": [c["raw_path"] for c in manifest["chunks"]],
        "warnings": warnings,
        "chunk_size": effective_chunk_size,
        "recommended_chunk_size": manifest["recommended_chunk_size"],
        "chunk_mode": chunk_mode,
        "target_tokens": manifest["target_tokens"],
        "hard_cap_tokens": manifest["hard_cap_tokens"],
    }
def get_chapters(video_url: str, timeout: int = 30) -> dict:
    """
    Fetch YouTube video chapter metadata using yt-dlp
    
    Args:
        video_url: YouTube video URL
        timeout: Timeout in seconds for yt-dlp command (default 30)
    
    Returns:
        {"has_chapters": bool, "chapters": [{"title": ..., "start_time": ..., "end_time": ...}, ...]}
    """
    try:
        cmd = [
            "yt-dlp", "--print", "%(chapters)j", video_url
        ]
        # Don't use check=True - some warnings may cause non-zero exit but still output data
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout.strip()
        
        # Handle various empty/null cases
        if not output or output == 'null' or output == 'NA' or output == 'None':
            return {"has_chapters": False, "chapters": []}
        
        try:
            chapters = json.loads(output)
        except json.JSONDecodeError:
            # Sometimes yt-dlp outputs non-JSON error messages
            return {"has_chapters": False, "chapters": []}
        
        if not chapters or not isinstance(chapters, list):
            return {"has_chapters": False, "chapters": []}
        
        return {
            "has_chapters": True,
            "chapters": chapters
        }
    except subprocess.TimeoutExpired:
        print(f"Error: yt-dlp timed out after {timeout}s", file=sys.stderr)
        return {"has_chapters": False, "chapters": [], "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        print("Error: yt-dlp not found. Please install it: pip install yt-dlp", file=sys.stderr)
        return {"has_chapters": False, "chapters": [], "error": "yt-dlp not found"}
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return {"has_chapters": False, "chapters": [], "error": str(e)}


def merge_content(work_dir: str, output_file: str, header_content: str = "") -> dict:
    """
    Merge processed chunks with chapter headers based on chapter_plan.json
    
    Algorithm:
    1. Read manifest.json to get chunk list
    2. Read chapter_plan.json (if exists) to get chapter structure
    3. For each chunk:
       - If chunk ID matches a chapter start, insert chapter header
       - Append processed chunk content
    4. Write final merged file
    
    Args:
        work_dir: Directory containing manifest.json, chapter_plan.json, and processed_*.md files
        output_file: Path to write merged output
        header_content: Optional header content to prepend (e.g., YAML frontmatter)
    
    Returns:
        {"success": bool, "output_file": str, "total_lines": int, "chapters_inserted": int}
    """
    work_path = Path(work_dir)
    
    # Read manifest
    manifest_path = work_path / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)
    
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    
    # Read chapter plan (optional)
    chapter_plan_path = work_path / "chapter_plan.json"
    chapter_starts = {}  # {chunk_id: {"title_en": ..., "title_zh": ...}}
    if chapter_plan_path.exists():
        try:
            chapter_plan = json.loads(chapter_plan_path.read_text(encoding='utf-8'))
            if isinstance(chapter_plan, list):
                for chapter in chapter_plan:
                    if not isinstance(chapter, dict):
                        continue
                        
                    start_chunk = chapter.get("start_chunk")
                    # Ensure start_chunk is an integer
                    if start_chunk is not None:
                        try:
                            start_chunk_int = int(start_chunk)
                            chapter_starts[start_chunk_int] = {
                                "title_en": str(chapter.get("title_en", "")),
                                "title_zh": str(chapter.get("title_zh", ""))
                            }
                        except (ValueError, TypeError):
                            print(f"Warning: Invalid start_chunk value: {start_chunk}", file=sys.stderr)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Could not parse chapter_plan.json: {e}", file=sys.stderr)
    
    # Merge content
    output_lines = []
    chapters_inserted = 0  # Counts logical chapters, not individual title lines
    missing_files = []
    
    # Smart header handling - avoid duplicate separators
    if header_content:
        header_content = header_content.strip()
        output_lines.append(header_content)
        # Only add separator if header doesn't already end with one
        if not header_content.endswith('---'):
            output_lines.append("\n---\n")
        else:
            output_lines.append("\n")
    
    for chunk_info in manifest["chunks"]:
        chunk_id = chunk_info["id"]
        processed_path = work_path / chunk_info["processed_path"]
        status = str(chunk_info.get("status", "")).strip().lower()

        if status == SUPERSEDED_CHUNK_STATUS:
            continue

        # Check if this is the start of a new chapter
        if chunk_id in chapter_starts:
            chapter = chapter_starts[chunk_id]
            title_en = chapter["title_en"]
            title_zh = chapter["title_zh"]
            if title_en or title_zh:
                output_lines.append(f"\n## {title_en}\n")
                if title_zh:
                    output_lines.append(f"## {title_zh}\n")
                output_lines.append("\n")
                chapters_inserted += 1
        
        # Read and append processed content
        if processed_path.exists():
            content = processed_path.read_text(encoding='utf-8')
            output_lines.append(content)
            output_lines.append("\n")
        elif status == "done":
            missing_files.append(str(processed_path))
            print(f"Warning: Processed file not found: {processed_path}", file=sys.stderr)
    
    # Write output file
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_content = ''.join(output_lines)
    output_path.write_text(final_content, encoding='utf-8')
    
    return {
        "success": len(missing_files) == 0,
        "output_file": str(output_path),
        "total_lines": final_content.count('\n'),
        "total_chars": len(final_content),
        "chapters_inserted": chapters_inserted,
        "missing_files": missing_files
    }


def _call_llm_api(api_key: str, base_url: str, model: str, messages: list,
                  api_format: str = "openai", max_tokens: int = 8192,
                  temperature: float = 0.3, timeout_sec: int = 120,
                  max_retries: int = 3, backoff_sec: float = 1.5,
                  stream_mode: str = "auto") -> dict:
    """
    Call LLM API with configurable timeout, bounded retries, and optional streaming.

    Returns:
        {
            "text": "...",
            "latency_ms": 1234,
            "request_url": "...",
            "streaming_used": True,
            "attempts": 2,
        }
    """
    stream_mode = _normalize_stream_mode(stream_mode)
    use_stream = stream_mode in {"auto", "true"}
    last_error = None
    attempt_history = []

    for attempt in range(1, max_retries + 2):
        try:
            result = _execute_llm_request(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=messages,
                api_format=api_format,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_sec=timeout_sec,
                use_stream=use_stream,
            )
            result["attempts"] = attempt
            result["stream_mode"] = stream_mode
            attempt_history.append(_build_attempt_log_from_result(result, attempt))
            result["attempt_history"] = attempt_history
            return result
        except LLMRequestError as error:
            error.attempts = attempt
            attempt_history.append(_build_attempt_log_from_error(error, attempt))
            error.attempt_history = list(attempt_history)
            last_error = error

            response_hint = (error.response_body or "").lower()
            stream_unsupported = any(token in response_hint for token in ("stream", "sse", "event-stream"))
            if stream_mode == "auto" and use_stream and error.status_code in {400, 422} and stream_unsupported:
                print(
                    f"ℹ️ Streaming unsupported at {error.request_url or _build_api_url(base_url, api_format)}; retrying once without stream.",
                    file=sys.stderr,
                )
                use_stream = False
                continue

            if not error.retryable or attempt > max_retries:
                raise error

            sleep_sec = (backoff_sec * (2 ** (attempt - 1))) + random.uniform(0, max(backoff_sec, 0.1))
            print(
                f"Retrying LLM request after {error.error_type} in {sleep_sec:.1f}s "
                f"(attempt {attempt}/{max_retries}, url={error.request_url or _build_api_url(base_url, api_format)})",
                file=sys.stderr,
            )
            time.sleep(sleep_sec)

    if last_error is not None:
        last_error.attempt_history = list(attempt_history)
    raise last_error or LLMRequestError("LLM API request failed", retryable=False)


def test_llm_api(config_path: str = None, api_key: str = "", base_url: str = "",
                 model: str = "", api_format: str = "", timeout_sec: int = 0,
                 stream_mode: str = "") -> dict:
    config = _load_optional_config(config_path)
    api_key = api_key or config.get("llm_api_key", "")
    base_url = base_url or config.get("llm_base_url", "")
    model = model or config.get("llm_model", "")
    api_format = api_format or config.get("llm_api_format", "openai")
    timeout_sec = timeout_sec or config.get("llm_probe_timeout_sec", 20)
    stream_mode = stream_mode or config.get("llm_stream", "auto")
    max_tokens = config.get("llm_probe_max_tokens", 16)

    if not api_key or not base_url or not model:
        return {
            "valid": False,
            "error": "LLM API is not fully configured",
            "error_type": "config",
            "request_url": _build_api_url(base_url, api_format) if base_url else "",
        }

    try:
        result = _call_llm_api(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": "Reply with OK only."}],
            api_format=api_format,
            max_tokens=max_tokens,
            temperature=0.0,
            timeout_sec=timeout_sec,
            max_retries=1,
            backoff_sec=min(config.get("llm_backoff_sec", 1.5), 1.0),
            stream_mode=stream_mode,
        )
        return {
            "valid": True,
            "model": model,
            "api_format": api_format,
            "request_url": result["request_url"],
            "latency_ms": result["latency_ms"],
            "attempts": result["attempts"],
            "streaming_used": result["streaming_used"],
            "preview": result["text"][:80],
        }
    except LLMRequestError as error:
        return {
            "valid": False,
            "model": model,
            "api_format": api_format,
            "request_url": error.request_url,
            "status_code": error.status_code,
            "error_type": error.error_type,
            "error": str(error),
            "attempts": getattr(error, "attempts", 1),
        }


def _count_tokens_via_provider(text: str, config: dict | None = None,
                               api_key: str = "", base_url: str = "",
                               model: str = "", api_format: str = "",
                               timeout_sec: int = 0) -> dict:
    import urllib.error
    import urllib.request

    config = config or {}
    local_estimate = _estimate_tokens_local(text, "tokens", config)
    api_format = (api_format or config.get("llm_api_format", "openai") or "openai").strip().lower()
    api_key = api_key or config.get("llm_api_key", "")
    base_url = base_url or config.get("llm_base_url", "")
    model = model or config.get("llm_model", "")
    timeout_sec = timeout_sec or config.get("llm_probe_timeout_sec", 20)

    if not _parse_bool(config.get("enable_token_count_probe"), DEFAULT_ENABLE_TOKEN_COUNT_PROBE):
        return {
            "valid": False,
            "provider_supported": False,
            "token_count": local_estimate,
            "token_count_source": "local_estimate",
            "error_type": "disabled",
            "error": "Token count probe disabled in config",
            "request_url": "",
            "latency_ms": None,
            "api_format": api_format,
        }

    if api_format != "anthropic":
        return {
            "valid": False,
            "provider_supported": False,
            "token_count": local_estimate,
            "token_count_source": "local_estimate",
            "error_type": "unsupported_api_format",
            "error": f"Provider token counting is not implemented for api_format={api_format}",
            "request_url": "",
            "latency_ms": None,
            "api_format": api_format,
        }

    if not api_key or not base_url or not model:
        return {
            "valid": False,
            "provider_supported": False,
            "token_count": local_estimate,
            "token_count_source": "local_estimate",
            "error_type": "config",
            "error": "LLM API is not fully configured",
            "request_url": _build_token_count_url(base_url, api_format) if base_url else "",
            "latency_ms": None,
            "api_format": api_format,
        }

    url = _build_token_count_url(base_url, api_format)
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "User-Agent": "yt-transcript/4.0",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": text or " "}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        started_at = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            result = json.loads(response.read().decode("utf-8"))
        latency_ms = int((time.monotonic() - started_at) * 1000)
        input_tokens = max(0, _parse_int(result.get("input_tokens"), 0))
        return {
            "valid": True,
            "provider_supported": True,
            "token_count": input_tokens,
            "token_count_source": "provider",
            "request_url": url,
            "latency_ms": latency_ms,
            "api_format": api_format,
            "model": model,
        }
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        return {
            "valid": False,
            "provider_supported": False,
            "token_count": local_estimate,
            "token_count_source": "local_estimate",
            "request_url": url,
            "latency_ms": None,
            "api_format": api_format,
            "status_code": error.code,
            "error_type": f"http_{error.code}",
            "error": error_body,
        }
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        return {
            "valid": False,
            "provider_supported": False,
            "token_count": local_estimate,
            "token_count_source": "local_estimate",
            "request_url": url,
            "latency_ms": None,
            "api_format": api_format,
            "error_type": "network",
            "error": str(reason),
        }
    except (socket.timeout, TimeoutError) as error:
        return {
            "valid": False,
            "provider_supported": False,
            "token_count": local_estimate,
            "token_count_source": "local_estimate",
            "request_url": url,
            "latency_ms": None,
            "api_format": api_format,
            "error_type": "timeout",
            "error": str(error),
        }
    except Exception as error:
        return {
            "valid": False,
            "provider_supported": False,
            "token_count": local_estimate,
            "token_count_source": "local_estimate",
            "request_url": url,
            "latency_ms": None,
            "api_format": api_format,
            "error_type": "unknown",
            "error": str(error),
        }


def test_token_count(config_path: str = None, api_key: str = "", base_url: str = "",
                     model: str = "", api_format: str = "", timeout_sec: int = 0,
                     sample_text: str = "Reply with OK only.") -> dict:
    config = _load_optional_config(config_path)
    api_key = api_key or config.get("llm_api_key", "")
    base_url = base_url or config.get("llm_base_url", "")
    model = model or config.get("llm_model", "")
    api_format = api_format or config.get("llm_api_format", "openai")
    timeout_sec = timeout_sec or config.get("llm_probe_timeout_sec", 20)
    sample_text = sample_text or "Reply with OK only."

    provider_probe = _count_tokens_via_provider(
        sample_text,
        config=config,
        api_key=api_key,
        base_url=base_url,
        model=model,
        api_format=api_format,
        timeout_sec=timeout_sec,
    )
    provider_probe["sample_text"] = sample_text
    provider_probe["probe_enabled"] = _parse_bool(
        config.get("enable_token_count_probe"),
        DEFAULT_ENABLE_TOKEN_COUNT_PROBE,
    )
    provider_probe["fallback_used"] = not provider_probe.get("valid", False)

    if provider_probe.get("valid", False):
        return provider_probe

    provider_probe["valid"] = True
    provider_probe.setdefault("provider_supported", False)
    provider_probe.setdefault("token_count_source", "local_estimate")
    provider_probe.setdefault(
        "error",
        "Provider token count unavailable; using local heuristic fallback",
    )
    return provider_probe


def _estimate_chunk_input_tokens(chunk_info: dict, input_key: str, text: str,
                                 config: dict | None = None) -> tuple[int, str]:
    """Estimate input tokens without adding another network probe.

    For `processed_path` chains we reuse `actual_output_tokens`, because that is
    the closest measurement of the text now being fed into the next stage.
    """
    chunk_info = chunk_info or {}
    config = config or {}

    if input_key == "processed_path":
        cached_tokens = max(0, _parse_int(chunk_info.get("actual_output_tokens"), 0))
        if cached_tokens > 0:
            return cached_tokens, "manifest_cached_output"
    else:
        cached_tokens = max(0, _parse_int(chunk_info.get("estimated_input_tokens"), 0))
        if cached_tokens > 0:
            return cached_tokens, "manifest_cached_input"

    return _estimate_tokens(text, "tokens", config), "local_estimate"


def _refresh_manifest_token_source_summary(manifest: dict) -> None:
    chunks = manifest.get("chunks", []) if isinstance(manifest, dict) else []
    sources = sorted({
        str(chunk.get("token_count_source", "")).strip()
        for chunk in chunks
        if str(chunk.get("token_count_source", "")).strip()
    })
    manifest["token_count_sources"] = sources
    if not sources:
        manifest["token_count_source"] = ""
    elif len(sources) == 1:
        manifest["token_count_source"] = sources[0]
    else:
        manifest["token_count_source"] = "mixed"


def process_chunks(work_dir: str, prompt_name: str, extra_instruction: str = "",
                   config_path: str = None, dry_run: bool = False,
                   input_key: str = "raw_path", force: bool = False) -> dict:
    """
    Process each chunk with isolated LLM API calls for context isolation.

    Adds resumability, bounded retries, atomic writes, and chunk-level telemetry.
    """
    work_path = Path(work_dir)
    manifest_path = work_path / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    try:
        prompt_path = _resolve_prompt_template_path(prompt_name)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        print(f"Available prompts: {_available_prompt_names()}", file=sys.stderr)
        sys.exit(1)

    prompt_template = prompt_path.read_text(encoding="utf-8")
    if extra_instruction:
        prompt_template += f"\n\n**Additional Instructions**: {extra_instruction}\n"

    if dry_run:
        config = _load_optional_config(config_path)
    else:
        config = load_config(config_path)
    api_key = config.get("llm_api_key", "")
    base_url = config.get("llm_base_url", "")
    model = config.get("llm_model", "")
    api_format = config.get("llm_api_format", "openai")
    timeout_sec = config.get("llm_timeout_sec", 120)
    max_retries = config.get("llm_max_retries", 3)
    backoff_sec = config.get("llm_backoff_sec", 1.5)
    stream_mode = config.get("llm_stream", "auto")
    stop_after_timeouts = config.get("llm_stop_after_consecutive_timeouts", 2)

    request_url = _build_api_url(base_url, api_format) if base_url else ""
    if not dry_run and (not api_key or not base_url or not model):
        print("Error: LLM API not configured. Set llm_api_key, llm_base_url, llm_model in config.yaml", file=sys.stderr)
        sys.exit(1)

    is_summary = (prompt_name == "summarize")
    prompt_budget = _calculate_chunk_budget(prompt_name, prompt_template, config)
    manifest = _ensure_manifest_structure(
        manifest,
        prompt_name=prompt_name,
        prompt_budget=prompt_budget,
        recommended_chunk_size=_recommended_chunk_size(prompt_name, config),
        request_url=request_url,
        source_file=str(manifest.get("source_file", "")).strip(),
    )
    plan = manifest["plan"]
    runtime = manifest["runtime"]
    manifest_chunk_size = max(0, _parse_int(plan.get("chunk_size"), manifest.get("chunk_size", 0)))
    manifest_chunk_mode = _normalize_chunk_mode(plan.get("chunk_mode", manifest.get("chunk_mode", config.get("chunk_mode", DEFAULT_CHUNK_MODE))))
    plan_target_tokens = max(1, _parse_int(plan.get("target_input_tokens", plan.get("target_tokens", prompt_budget["target_tokens"])), prompt_budget["target_tokens"]))
    plan_hard_cap_tokens = max(plan_target_tokens, _parse_int(plan.get("hard_cap_tokens"), prompt_budget["hard_cap_tokens"]))
    planned_max_output_tokens = max(1, _parse_int(plan.get("planned_max_output_tokens"), prompt_budget["planned_max_output_tokens"]))
    controller_budget = dict(prompt_budget)
    controller_budget["target_tokens"] = plan_target_tokens
    controller_budget["hard_cap_tokens"] = plan_hard_cap_tokens
    controller_budget["planned_max_output_tokens"] = planned_max_output_tokens
    controller_budget["chunk_mode"] = manifest_chunk_mode
    autotune_state = _build_autotune_state(controller_budget, config, manifest.get("autotune"))
    autotune_state["enabled"] = _parse_bool(config.get("enable_chunk_autotune"), DEFAULT_ENABLE_CHUNK_AUTOTUNE) and manifest_chunk_mode == "tokens"
    autotune_state["current_planned_max_output_tokens"] = planned_max_output_tokens
    if manifest_chunk_mode == "chars":
        recommended_chunk_size = _legacy_chunk_target_chars(prompt_name, config)
    else:
        recommended_chunk_size = _get_task_chunk_target(prompt_name, config)
    setup_warnings = []

    if manifest_chunk_mode == "tokens":
        manifest_target_tokens = max(0, _parse_int(manifest.get("target_tokens"), 0))
        if manifest_target_tokens and manifest_target_tokens > prompt_budget["target_tokens"]:
            setup_warning = (
                f"⚠️ Chunk target {manifest_target_tokens} tokens is larger than the recommended "
                f"{prompt_budget['target_tokens']} for prompt '{prompt_name}'. Long outputs may time out."
            )
            setup_warnings.append(setup_warning)
            print(setup_warning, file=sys.stderr)
    elif manifest_chunk_size and manifest_chunk_size > recommended_chunk_size:
        setup_warning = (
            f"⚠️ Chunk size {manifest_chunk_size} chars is larger than the recommended {recommended_chunk_size} chars "
            f"for prompt '{prompt_name}'. Long outputs may time out."
        )
        setup_warnings.append(setup_warning)
        print(setup_warning, file=sys.stderr)

    for chunk_info in manifest.get("chunks", []):
        chunk_info.setdefault("chunk_id", chunk_info.get("id", 0))
        chunk_info.setdefault("plan_id", runtime.get("active_plan_id", plan.get("plan_id", "")))
        chunk_info.setdefault("status", "pending")
        chunk_info.setdefault("attempts", 0)
        chunk_info.setdefault("attempt_logs", [])
        chunk_info.setdefault("last_error", "")
        chunk_info.setdefault("last_error_type", "")
        chunk_info.setdefault("error_type", chunk_info.get("last_error_type", ""))
        chunk_info.setdefault("latency_ms", None)
        chunk_info.setdefault("input_chars", chunk_info.get("char_count", 0))
        chunk_info.setdefault("estimated_input_tokens", 0)
        chunk_info.setdefault("input_tokens", chunk_info.get("estimated_input_tokens", 0))
        chunk_info.setdefault("token_count_source", prompt_budget["token_count_source"])
        chunk_info.setdefault("tail_context_text", "")
        chunk_info.setdefault("processed_tail_context_text", "")
        chunk_info.setdefault("processed_input_tail_context_text", "")
        chunk_info.setdefault("processed_input_section_title", "")
        chunk_info.setdefault("continuity_prev_chunk_id", None)
        chunk_info.setdefault("continuity_context_chars", 0)
        chunk_info.setdefault("continuity_context_tokens", 0)
        chunk_info.setdefault("continuity_section_title", "")
        chunk_info.setdefault("last_section_title", "")
        chunk_info.setdefault("output_chars", 0)
        chunk_info.setdefault("actual_output_tokens", 0)
        chunk_info.setdefault("prompt_tokens", prompt_budget["prompt_tokens"])
        chunk_info.setdefault("planned_max_output_tokens", planned_max_output_tokens)
        chunk_info.setdefault("request_url", request_url)
        chunk_info.setdefault("streaming_used", False)
        chunk_info.setdefault("autotune_target_tokens", autotune_state["current_target_tokens"])
        chunk_info.setdefault("autotune_next_target_tokens", autotune_state["current_target_tokens"])
        chunk_info.setdefault("autotune_event", "")
        chunk_info.setdefault("autotune_reason", "")
        chunk_info.setdefault("superseded_by_plan_id", "")
        chunk_info.setdefault("updated_at", "")
        chunk_info.setdefault("started_at", "")
        chunk_info.setdefault("completed_at", "")

    runtime["active_plan_id"] = plan.get("plan_id", runtime.get("active_plan_id", _new_plan_id()))
    runtime["last_request_url"] = request_url
    runtime["updated_at"] = _now_iso()
    plan["prompt_name"] = plan.get("prompt_name", prompt_name) or prompt_name
    plan["recommended_chunk_size"] = recommended_chunk_size
    plan["chunk_mode"] = manifest_chunk_mode
    plan["target_input_tokens"] = plan_target_tokens
    plan["target_tokens"] = plan_target_tokens
    plan["hard_cap_tokens"] = plan_hard_cap_tokens
    plan["prompt_tokens"] = prompt_budget["prompt_tokens"]
    plan["prompt_template_tokens"] = prompt_budget["prompt_template_tokens"]
    plan["planned_max_output_tokens"] = planned_max_output_tokens
    plan["effective_budget_tokens"] = prompt_budget["effective_budget_tokens"]
    plan["output_ratio"] = prompt_budget["output_ratio"]
    plan["chunk_safety_buffer_tokens"] = prompt_budget["safety_buffer_tokens"]
    plan["continuity_reserve_tokens"] = prompt_budget["continuity_reserve_tokens"]
    plan["token_count_source"] = prompt_budget["token_count_source"]
    autotune_state["current_planned_max_output_tokens"] = planned_max_output_tokens
    manifest["autotune"] = autotune_state
    _sync_manifest_legacy_fields(manifest)
    _refresh_manifest_token_source_summary(manifest)

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "total_chunks": manifest["total_chunks"],
            "prompt_name": prompt_name,
            "prompt_length": len(prompt_template),
            "model": model,
            "api_format": api_format,
            "request_url": request_url,
            "chunk_mode": manifest_chunk_mode,
            "recommended_chunk_size": recommended_chunk_size,
            "planned_max_output_tokens": planned_max_output_tokens,
            "prompt_tokens": prompt_budget["prompt_tokens"],
            "prompt_template_tokens": prompt_budget["prompt_template_tokens"],
            "continuity_reserve_tokens": prompt_budget["continuity_reserve_tokens"],
            "target_tokens": plan_target_tokens,
            "hard_cap_tokens": plan_hard_cap_tokens,
            "token_count_source": manifest.get("token_count_source", ""),
            "autotune": manifest.get("autotune", {}),
            "plan": manifest.get("plan", {}),
            "warnings": setup_warnings,
            "message": "Dry run: all validations passed"
        }

    runtime["status"] = "running"
    runtime["replan_required"] = False
    runtime["replan_reason"] = ""
    runtime["updated_at"] = _now_iso()
    _write_manifest(manifest_path, manifest)

    processed_count = 0
    failed_count = 0
    skipped_count = 0
    superseded_count = 0
    warnings = list(setup_warnings)
    output_files = []
    aborted = False
    aborted_reason = ""
    consecutive_timeouts = 0
    active_total = sum(1 for chunk in manifest["chunks"] if chunk.get("status") != SUPERSEDED_CHUNK_STATUS)
    total = active_total
    canary_limit = min(
        _parse_int_min(config.get("autotune_canary_chunks"), DEFAULT_AUTOTUNE_CANARY_CHUNKS, 1),
        active_total,
    )
    active_index = 0

    for chunk_index, chunk_info in enumerate(manifest["chunks"]):
        chunk_id = chunk_info["id"]
        if chunk_info.get("status") == SUPERSEDED_CHUNK_STATUS:
            superseded_count += 1
            continue
        active_index += 1
        runtime["current_chunk_index"] = chunk_index
        input_filename = chunk_info.get(input_key, chunk_info["raw_path"])
        input_path = work_path / input_filename

        if is_summary:
            out_filename = f"summary_chunk_{chunk_id:03d}.txt"
        else:
            out_filename = chunk_info["processed_path"]
        out_path = work_path / out_filename

        if not force and chunk_info.get("status") == "done" and out_path.exists():
            skipped_count += 1
            print(f"Skipping chunk {active_index}/{total} (chunk_id={chunk_id}, output exists at {out_path.name})", file=sys.stderr)
            continue

        if not input_path.exists():
            error_message = f"Input file not found: {input_path}"
            print(f"Error: {error_message}", file=sys.stderr)
            failed_count += 1
            consecutive_timeouts = 0
            chunk_info["status"] = "failed"
            chunk_info["last_error"] = error_message
            chunk_info["last_error_type"] = "input_missing"
            chunk_info["error_type"] = "input_missing"
            autotune_state = _update_autotune_state(
                autotune_state,
                success=False,
                timeout=False,
                error_type="input_missing",
                chunk_id=chunk_id,
            )
            manifest["autotune"] = autotune_state
            chunk_info["autotune_next_target_tokens"] = autotune_state["current_target_tokens"]
            chunk_info["autotune_event"] = autotune_state["last_event"]
            chunk_info["autotune_reason"] = autotune_state["last_reason"]
            chunk_info["updated_at"] = _now_iso()
            _write_manifest(manifest_path, manifest)
            continue

        chunk_text = input_path.read_text(encoding="utf-8")
        chunk_char_count = len(chunk_text)
        planned_max_output_tokens = max(
            1,
            _parse_int(chunk_info.get("planned_max_output_tokens"), plan.get("planned_max_output_tokens", planned_max_output_tokens)),
        )
        manifest["autotune"]["current_planned_max_output_tokens"] = planned_max_output_tokens
        estimated_input_tokens, token_count_source = _estimate_chunk_input_tokens(
            chunk_info,
            input_key,
            chunk_text,
            config,
        )
        previous_chunk = _find_previous_active_chunk(manifest["chunks"], chunk_index)
        continuity_context = _build_continuity_context(previous_chunk, work_path, config, input_key=input_key)
        prompt = _build_chunk_prompt(prompt_template, chunk_text, continuity_context["text"])
        actual_prompt_tokens = (
            prompt_budget["prompt_template_tokens"] + continuity_context["token_count"]
        )

        chunk_info["input_chars"] = chunk_char_count
        chunk_info["estimated_input_tokens"] = estimated_input_tokens
        chunk_info["input_tokens"] = estimated_input_tokens
        chunk_info["token_count_source"] = token_count_source
        chunk_info["prompt_tokens"] = actual_prompt_tokens
        chunk_info["planned_max_output_tokens"] = planned_max_output_tokens
        chunk_info["continuity_prev_chunk_id"] = continuity_context["source_chunk_id"]
        chunk_info["continuity_context_chars"] = len(continuity_context["text"])
        chunk_info["continuity_context_tokens"] = continuity_context["token_count"]
        chunk_info["continuity_section_title"] = continuity_context["section_title"]
        chunk_info["autotune_target_tokens"] = autotune_state["current_target_tokens"]
        chunk_info["autotune_next_target_tokens"] = autotune_state["current_target_tokens"]
        chunk_info["autotune_event"] = ""
        chunk_info["autotune_reason"] = ""

        chunk_info["status"] = "running"
        chunk_info["started_at"] = _now_iso()
        chunk_info["updated_at"] = chunk_info["started_at"]
        chunk_info["request_url"] = request_url
        chunk_info["latency_ms"] = None
        chunk_info["streaming_used"] = False
        _refresh_manifest_token_source_summary(manifest)
        _sync_manifest_legacy_fields(manifest)
        _write_manifest(manifest_path, manifest)

        print(
            f"Processing chunk {active_index}/{total} chunk_id={chunk_id} status=running "
            f"input_chars={chunk_char_count} estimated_input_tokens={estimated_input_tokens} input_tokens={estimated_input_tokens} "
            f"est_source={token_count_source} prompt_tokens={actual_prompt_tokens} continuity_tokens={continuity_context['token_count']} "
            f"planned_max_output_tokens={planned_max_output_tokens} autotune_target_tokens={autotune_state['current_target_tokens']} "
            f"model={model} url={request_url}",
            file=sys.stderr,
        )

        try:
            llm_result = _call_llm_api(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=[{"role": "user", "content": prompt}],
                api_format=api_format,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                backoff_sec=backoff_sec,
                stream_mode=stream_mode,
                max_tokens=planned_max_output_tokens,
            )
            attempt_logs = _collect_attempt_logs(llm_result)
            result_text = llm_result["text"]
            result_char_count = len(result_text)
            actual_output_tokens = _estimate_tokens(result_text, "tokens", config)
            ratio = result_char_count / chunk_char_count if chunk_char_count > 0 else 0
            consecutive_timeouts = 0

            if not is_summary:
                if ratio < 0.5:
                    warning = (
                        f"⚠️ Chunk {chunk_id}: output is only {ratio:.0%} of input size "
                        f"({result_char_count} vs {chunk_char_count} chars). Possible summarization instead of structuring."
                    )
                    warnings.append(warning)
                    print(warning, file=sys.stderr)

                if prompt_name in ("structure_only", "quick_cleanup") and "##" not in result_text and chunk_char_count > 2000:
                    warning = (
                        f"⚠️ Chunk {chunk_id}: no section headers (##) found in output "
                        f"({chunk_char_count} chars input). Structuring may have failed."
                    )
                    warnings.append(warning)
                    print(warning, file=sys.stderr)

                if prompt_name == "translate_only":
                    cn_chars = sum(1 for c in result_text if '一' <= c <= '鿿')
                    cn_ratio = cn_chars / result_char_count if result_char_count > 0 else 0
                    if cn_ratio < 0.1:
                        warning = (
                            f"⚠️ Chunk {chunk_id}: Chinese character ratio is only {cn_ratio:.0%}. "
                            f"Translation may have been skipped."
                        )
                        warnings.append(warning)
                        print(warning, file=sys.stderr)

            _atomic_write_text(out_path, result_text)
            output_files.append(str(out_path))
            processed_count += 1

            chunk_info["status"] = "done"
            chunk_info["attempts"] = chunk_info.get("attempts", 0) + max(len(attempt_logs), llm_result["attempts"])
            chunk_info["attempt_logs"] = list(chunk_info.get("attempt_logs", [])) + attempt_logs
            chunk_info["last_error"] = ""
            chunk_info["last_error_type"] = ""
            chunk_info["error_type"] = ""
            chunk_info["latency_ms"] = llm_result["latency_ms"]
            chunk_info["output_chars"] = result_char_count
            chunk_info["actual_output_tokens"] = actual_output_tokens
            chunk_info["processed_tail_context_text"] = _extract_tail_sentences(
                result_text,
                _parse_int_min(
                    config.get("chunk_context_tail_sentences"),
                    DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
                    0,
                ),
                config,
            )
            if input_key == "raw_path":
                chunk_info["processed_input_tail_context_text"] = chunk_info["processed_tail_context_text"]
                chunk_info["processed_input_section_title"] = _extract_last_section_title(result_text)
            chunk_info["last_section_title"] = _extract_last_section_title(result_text)
            chunk_info["request_url"] = llm_result["request_url"]
            chunk_info["streaming_used"] = llm_result["streaming_used"]
            had_timeout_retry = _has_timeout_attempt(attempt_logs)
            if had_timeout_retry:
                autotune_state = _update_autotune_state(
                    autotune_state,
                    success=False,
                    timeout=True,
                    error_type="timeout",
                    chunk_id=chunk_id,
                )
            else:
                autotune_state = _update_autotune_state(
                    autotune_state,
                    success=True,
                    latency_ms=llm_result["latency_ms"],
                    chunk_id=chunk_id,
                )
            autotune_state["current_planned_max_output_tokens"] = planned_max_output_tokens
            manifest["autotune"] = autotune_state
            chunk_info["autotune_next_target_tokens"] = autotune_state["current_target_tokens"]
            chunk_info["autotune_event"] = autotune_state["last_event"]
            chunk_info["autotune_reason"] = autotune_state["last_reason"]
            chunk_info["completed_at"] = _now_iso()
            chunk_info["updated_at"] = chunk_info["completed_at"]
            print(
                f"Completed chunk {active_index}/{total} chunk_id={chunk_id} status=done "
                f"input_chars={chunk_char_count} estimated_input_tokens={estimated_input_tokens} "
                f"planned_max_output_tokens={planned_max_output_tokens} latency_ms={llm_result['latency_ms']} "
                f"attempts={llm_result['attempts']} streaming_used={llm_result['streaming_used']} "
                f"output_chars={result_char_count} actual_output_tokens={actual_output_tokens} "
                f"autotune_event={autotune_state['last_event']} next_autotune_target_tokens={autotune_state['current_target_tokens']}",
                file=sys.stderr,
            )
            if autotune_state["last_event"]:
                print(
                    f"Autotune chunk_id={chunk_id} event={autotune_state['last_event']} "
                    f"target_tokens={autotune_state['current_target_tokens']} reason={autotune_state['last_reason']}",
                    file=sys.stderr,
                )
            if had_timeout_retry or (autotune_state["last_event"] == "shrink" and active_index <= canary_limit):
                runtime["replan_required"] = True
                runtime["replan_reason"] = autotune_state["last_reason"] or "Observed unstable retries under the current plan"
                runtime["status"] = "aborted"
                aborted = True
                aborted_reason = (
                    "Current plan requires replanning before continuing. "
                    f"{runtime['replan_reason']}"
                )
                print(f"Error: {aborted_reason}", file=sys.stderr)
                break
        except LLMRequestError as error:
            attempt_logs = _collect_attempt_logs(error)
            failed_count += 1
            chunk_info["status"] = "failed"
            chunk_info["attempts"] = chunk_info.get("attempts", 0) + max(len(attempt_logs), getattr(error, "attempts", 1))
            chunk_info["attempt_logs"] = list(chunk_info.get("attempt_logs", [])) + attempt_logs
            chunk_info["last_error"] = str(error)
            chunk_info["last_error_type"] = error.error_type
            chunk_info["error_type"] = error.error_type
            chunk_info["request_url"] = error.request_url or request_url
            autotune_state = _update_autotune_state(
                autotune_state,
                success=False,
                timeout=_is_timeout_error(error),
                error_type=error.error_type,
                chunk_id=chunk_id,
            )
            autotune_state["current_planned_max_output_tokens"] = planned_max_output_tokens
            manifest["autotune"] = autotune_state
            chunk_info["autotune_next_target_tokens"] = autotune_state["current_target_tokens"]
            chunk_info["autotune_event"] = autotune_state["last_event"]
            chunk_info["autotune_reason"] = autotune_state["last_reason"]
            chunk_info["updated_at"] = _now_iso()
            print(
                f"Chunk {active_index}/{total} chunk_id={chunk_id} status=failed "
                f"input_chars={chunk_char_count} estimated_input_tokens={estimated_input_tokens} "
                f"planned_max_output_tokens={planned_max_output_tokens} latency_ms={chunk_info.get('latency_ms')} "
                f"attempts={getattr(error, 'attempts', 1)} streaming_used={chunk_info.get('streaming_used', False)} "
                f"error_type={error.error_type} url={error.request_url or request_url} error={error} "
                f"autotune_event={autotune_state['last_event']} next_autotune_target_tokens={autotune_state['current_target_tokens']}",
                file=sys.stderr,
            )
            if autotune_state["last_event"]:
                print(
                    f"Autotune chunk_id={chunk_id} event={autotune_state['last_event']} "
                    f"target_tokens={autotune_state['current_target_tokens']} reason={autotune_state['last_reason']}",
                    file=sys.stderr,
                )

            if _should_replan_after_error(error) or (autotune_state["last_event"] == "shrink" and active_index <= canary_limit):
                runtime["replan_required"] = True
                runtime["replan_reason"] = autotune_state["last_reason"] or str(error)
                runtime["status"] = "aborted"
                aborted = True
                aborted_reason = (
                    "Current plan requires replanning before continuing. "
                    f"{runtime['replan_reason']}"
                )
                print(f"Error: {aborted_reason}", file=sys.stderr)
                break

            if _is_timeout_error(error):
                consecutive_timeouts += 1
            else:
                consecutive_timeouts = 0

            if stop_after_timeouts > 0 and consecutive_timeouts >= stop_after_timeouts:
                aborted = True
                aborted_reason = (
                    f"Stopped after {consecutive_timeouts} consecutive timeout failures. "
                    f"Check provider/gateway latency or reduce chunk size."
                )
                print(f"Error: {aborted_reason}", file=sys.stderr)
                _write_manifest(manifest_path, manifest)
                break
        finally:
            runtime["processed_count"] = processed_count
            runtime["failed_count"] = failed_count
            runtime["skipped_count"] = skipped_count
            runtime["superseded_count"] = superseded_count
            runtime["last_request_url"] = request_url
            runtime["updated_at"] = _now_iso()
            if not aborted:
                runtime["status"] = "running"
            _refresh_manifest_token_source_summary(manifest)
            _sync_manifest_legacy_fields(manifest)
            _write_manifest(manifest_path, manifest)

        if aborted:
            break

    runtime["processed_count"] = processed_count
    runtime["failed_count"] = failed_count
    runtime["skipped_count"] = skipped_count
    runtime["superseded_count"] = superseded_count
    runtime["updated_at"] = _now_iso()
    if aborted:
        runtime["status"] = "aborted"
    elif failed_count > 0:
        runtime["status"] = "completed_with_errors"
    else:
        runtime["status"] = "completed"
    _refresh_manifest_token_source_summary(manifest)
    _sync_manifest_legacy_fields(manifest)
    _write_manifest(manifest_path, manifest)

    return {
        "success": failed_count == 0 and not aborted,
        "processed_count": processed_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "superseded_count": superseded_count,
        "total_chunks": total,
        "warnings": warnings,
        "warning_count": len(warnings),
        "output_files": output_files,
        "aborted": aborted,
        "aborted_reason": aborted_reason,
        "replan_required": runtime.get("replan_required", False),
        "replan_reason": runtime.get("replan_reason", ""),
        "plan": manifest.get("plan", {}),
        "request_url": request_url,
    }


def replan_remaining(work_dir: str, prompt_name: str = "", config_path: str = None,
                     chunk_size: int = 0, input_key: str = "raw_path") -> dict:
    work_path = Path(work_dir)
    manifest_path = work_path / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    if input_key != "raw_path":
        return {
            "success": False,
            "replanned": False,
            "error": "replan-remaining currently supports raw_path only",
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = _load_optional_config(config_path)
    plan_prompt_name = prompt_name or str(manifest.get("plan", {}).get("prompt_name", manifest.get("prompt_name", ""))).strip()
    prompt_template = ""
    if plan_prompt_name:
        prompt_template = _resolve_prompt_template_path(plan_prompt_name).read_text(encoding="utf-8")
    prompt_budget = _calculate_chunk_budget(plan_prompt_name, prompt_template, config)
    manifest = _ensure_manifest_structure(
        manifest,
        prompt_name=plan_prompt_name,
        prompt_budget=prompt_budget,
        recommended_chunk_size=_recommended_chunk_size(plan_prompt_name, config),
        source_file=str(manifest.get("source_file", "")).strip(),
    )

    pending_indices = [
        index for index, chunk in enumerate(manifest["chunks"])
        if chunk.get("status") not in {"done", SUPERSEDED_CHUNK_STATUS}
    ]
    if not pending_indices:
        return {
            "success": True,
            "replanned": False,
            "plan_id": manifest["plan"].get("plan_id", ""),
            "message": "No remaining chunks to replan",
        }

    first_pending_index = pending_indices[0]
    previous_chunk = _find_previous_active_chunk(manifest["chunks"], first_pending_index)
    pending_chunks = []
    for index in pending_indices:
        chunk_info = manifest["chunks"][index]
        input_path = work_path / chunk_info["raw_path"]
        if not input_path.exists():
            return {
                "success": False,
                "replanned": False,
                "error": f"Input file not found: {input_path}",
            }
        pending_chunks.append({
            "chunk_id": chunk_info["id"],
            "text": input_path.read_text(encoding="utf-8"),
        })

    replan_chunk_size = chunk_size
    if replan_chunk_size <= 0:
        suggested_target = max(0, _parse_int(manifest.get("autotune", {}).get("current_target_tokens"), 0))
        if suggested_target > 0:
            replan_chunk_size = suggested_target

    chunk_plan = _build_chunk_plan(plan_prompt_name, replan_chunk_size, config, prompt_template)
    budget = chunk_plan["budget"]
    chunk_mode = chunk_plan["chunk_mode"]
    effective_chunk_size = chunk_plan["effective_chunk_size"]
    hard_cap_size = chunk_plan["hard_cap_size"]
    recommended_chunk_size = chunk_plan["recommended_chunk_size"]
    target_tokens = chunk_plan["target_tokens"]
    hard_cap_tokens = chunk_plan["hard_cap_tokens"]

    chapter_plan_path = work_path / "chapter_plan.json"
    chapter_plan_entries = None
    chapter_start_ids = set()
    if chapter_plan_path.exists():
        try:
            loaded_chapter_plan = json.loads(chapter_plan_path.read_text(encoding="utf-8"))
            if isinstance(loaded_chapter_plan, list):
                chapter_plan_entries = loaded_chapter_plan
                for chapter in chapter_plan_entries:
                    if not isinstance(chapter, dict):
                        continue
                    start_chunk = chapter.get("start_chunk")
                    try:
                        chapter_start_ids.add(int(start_chunk))
                    except (TypeError, ValueError):
                        continue
        except json.JSONDecodeError:
            chapter_plan_entries = None

    pending_chapter_start_ids = {chunk["chunk_id"] for chunk in pending_chunks if chunk["chunk_id"] in chapter_start_ids}
    replan_segments = []
    current_segment = []
    for chunk in pending_chunks:
        if current_segment and chunk["chunk_id"] in pending_chapter_start_ids:
            replan_segments.append(current_segment)
            current_segment = []
        current_segment.append(chunk)
    if current_segment:
        replan_segments.append(current_segment)

    chunk_specs = []
    warnings = []
    segment_start_remap = {}
    for segment in replan_segments:
        segment_text = CHUNK_SEPARATOR.join(part["text"] for part in segment if part["text"])
        if not segment_text.strip():
            continue
        sentences = _split_sentences(segment_text)
        segment_chunks, segment_warnings = _split_text_into_chunks(
            sentences,
            chunk_mode,
            effective_chunk_size,
            hard_cap_size,
            config,
        )
        warnings.extend(segment_warnings)
        if not segment_chunks:
            segment_chunks = [segment_text]
        segment_start_chunk_id = segment[0]["chunk_id"]
        for offset, chunk_content in enumerate(segment_chunks):
            chunk_specs.append({
                "content": chunk_content,
                "segment_start_chunk_id": segment_start_chunk_id,
                "starts_segment": offset == 0,
            })

    prior_plan = dict(manifest.get("plan", {}))
    prior_plan_id = str(prior_plan.get("plan_id", "")).strip()
    new_plan_id = _new_plan_id()
    manifest.setdefault("plan_history", []).append(prior_plan)
    manifest["plan"] = _build_manifest_plan(
        plan_prompt_name,
        chunk_mode,
        recommended_chunk_size,
        effective_chunk_size,
        {
            **budget,
            "target_tokens": target_tokens,
            "hard_cap_tokens": hard_cap_tokens,
        },
        source_file=str(manifest.get("source_file", "")).strip(),
        plan_id=new_plan_id,
        prior_plan_id=prior_plan_id,
    )

    now = _now_iso()
    superseded_count = 0
    for index in pending_indices:
        chunk_info = manifest["chunks"][index]
        if chunk_info.get("status") == SUPERSEDED_CHUNK_STATUS:
            continue
        chunk_info["status"] = SUPERSEDED_CHUNK_STATUS
        chunk_info["superseded_by_plan_id"] = new_plan_id
        chunk_info["updated_at"] = now
        superseded_count += 1

    next_chunk_id = max((_parse_int(chunk.get("id"), 0) for chunk in manifest["chunks"]), default=-1) + 1
    new_chunk_start_index = len(manifest["chunks"])
    previous_continuity_chunk_id = previous_chunk.get("id") if previous_chunk else None
    autotune_state = _build_autotune_state(manifest["plan"], config, manifest.get("autotune"))
    autotune_state["enabled"] = _parse_bool(config.get("enable_chunk_autotune"), DEFAULT_ENABLE_CHUNK_AUTOTUNE) and chunk_mode == "tokens"

    for offset, chunk_spec in enumerate(chunk_specs):
        chunk_id = next_chunk_id + offset
        chunk_filename = f"chunk_{chunk_id:03d}.txt"
        chunk_path = work_path / chunk_filename
        _atomic_write_text(chunk_path, chunk_spec["content"])
        chunk_entry = _new_chunk_manifest_entry(
            chunk_id,
            chunk_spec["content"],
            budget,
            config,
            raw_path=chunk_filename,
            processed_path=f"processed_{chunk_id:03d}.md",
            plan_id=new_plan_id,
            continuity_prev_chunk_id=previous_continuity_chunk_id,
        )
        chunk_entry["autotune_target_tokens"] = autotune_state["current_target_tokens"]
        chunk_entry["autotune_next_target_tokens"] = autotune_state["current_target_tokens"]
        manifest["chunks"].append(chunk_entry)
        if chunk_spec["starts_segment"]:
            segment_start_remap[chunk_spec["segment_start_chunk_id"]] = chunk_id
        previous_continuity_chunk_id = chunk_id

    if chapter_plan_entries is not None and segment_start_remap:
        chapter_plan_changed = False
        for chapter in chapter_plan_entries:
            if not isinstance(chapter, dict):
                continue
            try:
                start_chunk_id = int(chapter.get("start_chunk"))
            except (TypeError, ValueError):
                continue
            if start_chunk_id not in segment_start_remap:
                continue
            chapter.setdefault("original_start_chunk", start_chunk_id)
            chapter["start_chunk"] = segment_start_remap[start_chunk_id]
            chapter_plan_changed = True
        if chapter_plan_changed:
            _atomic_write_text(
                chapter_plan_path,
                json.dumps(chapter_plan_entries, ensure_ascii=False, indent=2),
            )

    runtime = manifest["runtime"]
    runtime["status"] = "pending"
    runtime["active_plan_id"] = new_plan_id
    runtime["replan_required"] = False
    runtime["replan_reason"] = ""
    runtime["last_replanned_at"] = now
    runtime["updated_at"] = now
    runtime["current_chunk_index"] = new_chunk_start_index
    runtime["superseded_count"] = sum(
        1 for chunk in manifest["chunks"]
        if chunk.get("status") == SUPERSEDED_CHUNK_STATUS
    )
    manifest["autotune"] = autotune_state
    manifest["total_chunks"] = len(manifest["chunks"])
    _sync_manifest_legacy_fields(manifest)
    _refresh_manifest_token_source_summary(manifest)
    _write_manifest(manifest_path, manifest)

    for warning in warnings:
        print(f"⚠️ {warning}", file=sys.stderr)

    return {
        "success": True,
        "replanned": True,
        "plan_id": new_plan_id,
        "prior_plan_id": prior_plan_id,
        "superseded_count": superseded_count,
        "new_chunk_count": len(chunk_specs),
        "chunk_size": effective_chunk_size,
        "warnings": warnings,
    }


def process_chunks_with_replans(work_dir: str, prompt_name: str, extra_instruction: str = "",
                                config_path: str = None, input_key: str = "raw_path",
                                force: bool = False, max_replans: int = 3) -> dict:
    def current_superseded_count() -> int:
        manifest_path = Path(work_dir) / "manifest.json"
        if not manifest_path.exists():
            return 0
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        return sum(
            1 for chunk in manifest.get("chunks", [])
            if chunk.get("status") == SUPERSEDED_CHUNK_STATUS
        )

    if input_key != "raw_path":
        result = process_chunks(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=False,
            input_key=input_key,
            force=force,
        )
        result.setdefault(
            "message",
            "auto-replan is available only for raw_path plans; rerun manually if replanning is required.",
        )
        return result

    aggregate = {
        "processed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "superseded_count": 0,
        "warnings": [],
        "warning_count": 0,
        "output_files": [],
        "replan_count": 0,
        "request_url": "",
        "aborted": False,
        "aborted_reason": "",
        "success": False,
    }

    next_force = force
    last_result = {}
    for _ in range(max(0, max_replans) + 1):
        last_result = process_chunks(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=False,
            input_key=input_key,
            force=next_force,
        )
        aggregate["processed_count"] += last_result.get("processed_count", 0)
        aggregate["failed_count"] += last_result.get("failed_count", 0)
        aggregate["skipped_count"] += last_result.get("skipped_count", 0)
        aggregate["warnings"].extend(last_result.get("warnings", []))
        aggregate["output_files"].extend(last_result.get("output_files", []))
        aggregate["request_url"] = last_result.get("request_url", aggregate["request_url"])
        aggregate["superseded_count"] = current_superseded_count()

        if not last_result.get("replan_required", False):
            aggregate.update({
                "success": last_result.get("success", False),
                "aborted": last_result.get("aborted", False),
                "aborted_reason": last_result.get("aborted_reason", ""),
                "replan_required": False,
                "replan_reason": "",
                "plan": last_result.get("plan", {}),
            })
            aggregate["warning_count"] = len(aggregate["warnings"])
            aggregate["superseded_count"] = current_superseded_count()
            return aggregate

        if aggregate["replan_count"] >= max(0, max_replans):
            aggregate.update({
                "success": False,
                "aborted": True,
                "aborted_reason": last_result.get("aborted_reason", "Reached max auto-replan limit"),
                "replan_required": True,
                "replan_reason": last_result.get("replan_reason", ""),
                "plan": last_result.get("plan", {}),
            })
            aggregate["warning_count"] = len(aggregate["warnings"])
            aggregate["superseded_count"] = current_superseded_count()
            return aggregate

        replan_result = replan_remaining(
            work_dir,
            prompt_name=prompt_name,
            config_path=config_path,
            input_key=input_key,
        )
        aggregate["replan_count"] += 1
        aggregate["warnings"].extend(replan_result.get("warnings", []))
        aggregate["superseded_count"] = current_superseded_count()
        if not replan_result.get("success", False):
            replan_error = replan_result.get("error") or replan_result.get("message") or "unknown error"
            aggregate.update({
                "success": False,
                "aborted": True,
                "aborted_reason": f"Auto-replan failed: {replan_error}",
                "replan_required": True,
                "replan_reason": last_result.get("replan_reason", "") or replan_error,
                "plan": last_result.get("plan", {}),
            })
            aggregate["warning_count"] = len(aggregate["warnings"])
            return aggregate
        next_force = False

    aggregate.update({
        "success": False,
        "aborted": True,
        "aborted_reason": last_result.get("aborted_reason", "Reached max auto-replan limit"),
        "replan_required": last_result.get("replan_required", False),
        "replan_reason": last_result.get("replan_reason", ""),
        "plan": last_result.get("plan", {}),
    })
    aggregate["warning_count"] = len(aggregate["warnings"])
    aggregate["superseded_count"] = current_superseded_count()
    return aggregate


def detect_audio_content_type(audio_path: str) -> str:
    ext = Path(audio_path).suffix.lower().lstrip(".")
    return {
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "webm": "audio/webm",
        "opus": "audio/opus",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
    }.get(ext, "application/octet-stream")


def _call_deepgram_api(audio_path: str, api_key: str, language: str,
                       timeout: int = 300) -> dict:
    import urllib.request
    import urllib.error

    audio_file = Path(audio_path)
    if not audio_file.exists():
        print(f"Error: Audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    params = (
        f"model=nova-2&language={language}"
        "&diarize=true&punctuate=true&paragraphs=true&smart_format=true"
    )
    url = f"https://api.deepgram.com/v1/listen?{params}"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": detect_audio_content_type(audio_path),
        "User-Agent": "yt-transcript/4.0",
    }

    data = audio_file.read_bytes()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"Error: Deepgram API returned HTTP {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Cannot reach Deepgram API: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: Deepgram API call failed: {e}", file=sys.stderr)
        sys.exit(1)


def transcribe_deepgram(audio_path: str, language: str, config_path: str = None,
                        api_key: str = "", max_size_mb: float = 10.0,
                        max_deviation_sec: float = 60.0, timeout: int = 300,
                        output_json: str = "", output_text: str = "") -> dict:
    """
    Transcribe audio via Deepgram. Automatically splits large files and merges
    chunk transcripts into one raw transcript output.
    """
    if not api_key:
        config = load_config(config_path)
        api_key = config.get("deepgram_api_key", "")

    if not api_key:
        print("Error: Deepgram API key not configured", file=sys.stderr)
        sys.exit(1)

    path = Path(audio_path)
    if not path.exists():
        print(f"Error: Audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    split_result = split_audio(audio_path, max_size_mb=max_size_mb, max_deviation_sec=max_deviation_sec)
    chunk_paths = split_result["chunks"]

    transcripts = []
    speaker_count = 1
    json_outputs = []

    for idx, chunk_path in enumerate(chunk_paths):
        payload = _call_deepgram_api(chunk_path, api_key=api_key, language=language, timeout=timeout)
        processed = process_deepgram_payload(payload)
        transcripts.append(processed["transcript"])
        speaker_count = max(speaker_count, processed["speaker_count"])

        if output_json:
            output_base = Path(output_json)
            if len(chunk_paths) == 1:
                json_path = output_base
            else:
                json_path = output_base.with_name(f"{output_base.stem}_chunk_{idx:03d}{output_base.suffix or '.json'}")
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            json_outputs.append(str(json_path))

    transcript = "\n\n".join(t for t in transcripts if t).strip()

    if output_text:
        Path(output_text).write_text(transcript, encoding="utf-8")

    return {
        "transcript": transcript,
        "speaker_count": speaker_count,
        "chunk_count": len(chunk_paths),
        "json_outputs": json_outputs,
        "split_points": split_result.get("split_points", []),
        "used_split_mode": len(chunk_paths) > 1,
    }


def assemble_final(optimized_text_path: str, output_file: str,
                    title: str = "", source: str = "", channel: str = "",
                    date: str = "", created: str = "", duration: int = 0,
                    transcript_source: str = "", bilingual: bool = False) -> dict:
    """
    Assemble final markdown file from optimized text and metadata.
    
    Pure file operation: reads optimized text, prepends YAML frontmatter
    and metadata header, appends footer, writes to output file.
    The Agent never needs to read the optimized text into its context.
    
    Args:
        optimized_text_path: Path to the optimized text file
        output_file: Path to write the final markdown file
        title: Video title
        source: Video URL
        channel: Channel name
        date: Video upload date
        created: File creation date (today)
        duration: Video duration in seconds
        transcript_source: 'youtube' or 'deepgram'
        bilingual: Whether the content is bilingual
    
    Returns:
        {"success": bool, "output_file": str, "total_chars": int, "total_lines": int}
    """
    # Read optimized text
    opt_path = Path(optimized_text_path)
    if not opt_path.exists():
        print(f"Error: Optimized text file not found: {optimized_text_path}", file=sys.stderr)
        sys.exit(1)
    
    try:
        optimized_text = opt_path.read_text(encoding='utf-8').strip()
    except Exception as e:
        print(f"Error: Cannot read optimized text file: {e}", file=sys.stderr)
        sys.exit(2)
    
    # Calculate duration in minutes
    duration_min = duration // 60 if duration > 0 else 0
    bilingual_str = "true" if bilingual else "false"
    language_mode = "Bilingual" if bilingual else "Chinese"
    safe_title = _escape_markdown_text(title)
    safe_channel = _escape_markdown_text(channel)
    safe_source = _sanitize_markdown_url(source)
    
    # Build YAML frontmatter
    frontmatter_lines = [
        "---",
        f"title: {_yaml_string(title)}",
        f"source: {_yaml_string(source)}",
        f"channel: {_yaml_string(channel)}",
    ]
    if date:
        frontmatter_lines.append(f"date: {_yaml_string(date)}")
    frontmatter_lines.extend([
        f"created: {_yaml_string(created)}",
        "type: video-transcript",
        f"bilingual: {bilingual_str}",
        f"duration: {_yaml_string(f'{duration_min}m')}",
        f"transcript_source: {_yaml_string(transcript_source)}",
        "---",
    ])
    
    # Build header section
    header_lines = [
        "",
        f"# {safe_title}",
        "",
        f"> Video source: [YouTube - {safe_channel}]({safe_source})",
        f"> Language mode: {language_mode}",
        f"> Duration: {duration_min} minutes",
        "",
        "---",
        "",
    ]
    
    # Build footer
    footer_lines = [
        "",
        "---",
        "",
        f"*This article was generated by AI voice transcription ({transcript_source}), for reference only.*",
        "",
    ]
    
    # Assemble final content
    final_content = '\n'.join(frontmatter_lines) + '\n' + '\n'.join(header_lines) + optimized_text + '\n' + '\n'.join(footer_lines)
    
    # Write output file
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(final_content, encoding='utf-8')
    
    return {
        "success": True,
        "output_file": str(out_path.absolute()),
        "total_chars": len(final_content),
        "total_lines": final_content.count('\n') + 1
    }


def verify_quality(optimized_text_path: str, raw_text_path: str = None,
                   bilingual: bool = False) -> dict:
    """
    Verify quality of optimized text file with structural checks.
    
    Pure file-based validation. The Agent only reads the JSON report,
    never the actual text content. Checks:
    1. File exists and is non-empty
    2. Has section headers (##)
    3. No abrupt truncation (last line is complete)
    4. Bilingual balance (Chinese char ratio in expected range)
    5. Size ratio vs raw text (if raw_text_path provided)
    
    Args:
        optimized_text_path: Path to the optimized text file
        raw_text_path: Optional path to raw text file for comparison
        bilingual: Whether the content should be bilingual
    
    Returns:
        {"passed": bool, "checks": {...}, "warnings": [...], "hard_failures": [...]}

    Stop/go contract:
        - non-empty hard_failures => STOP
        - warnings only => review before proceeding
    """
    warnings = []
    hard_failures = []
    checks = {}
    
    # Read optimized text
    opt_path = Path(optimized_text_path)
    if not opt_path.exists():
        return {
            "passed": False,
            "checks": {"file_exists": False},
            "warnings": [],
            "hard_failures": ["Optimized text file not found"],
        }
    
    try:
        text = opt_path.read_text(encoding='utf-8')
    except Exception as e:
        return {
            "passed": False,
            "checks": {"file_exists": True, "file_readable": False},
            "warnings": [],
            "hard_failures": [f"Cannot read file: {e}"],
        }
    
    total_chars = len(text)
    total_lines = text.count('\n') + 1
    
    checks["file_exists"] = True
    checks["total_chars"] = total_chars
    checks["total_lines"] = total_lines
    paragraphs = [block.strip() for block in re.split(r'\n\s*\n', text) if block.strip()]
    body_paragraphs = [
        block for block in paragraphs
        if not block.startswith('#') and not block.startswith('>') and not block.startswith('---')
    ]
    checks["paragraph_count"] = len(body_paragraphs)
    
    # Check 1: Non-empty
    checks["non_empty"] = total_chars > 0
    if not checks["non_empty"]:
        hard_failures.append("File is empty")
    
    # Check 2: Has section headers
    section_headers = re.findall(r'^##\s+.+', text, re.MULTILINE)
    checks["section_count"] = len(section_headers)
    checks["has_sections"] = len(section_headers) > 0
    if not checks["has_sections"] and total_chars > 1200:
        hard_failures.append(f"No section headers (##) found in {total_chars} chars of text")
    if len(body_paragraphs) < 2 and total_chars > 400:
        warnings.append(f"Only {len(body_paragraphs)} body paragraph found in {total_chars} chars of text")
    
    # Check 3: No abrupt truncation
    # Check if last non-empty line ends with proper punctuation or closing marker
    lines = [l for l in text.strip().split('\n') if l.strip()]
    if lines:
        last_line = lines[-1].strip()
        # Consider proper endings: punctuation, markdown markers, closing quotes
        proper_endings = ('.', '!', '?', '。', '！', '？', '*', '`', '"', '"', 
                         ')', '）', '」', '>', '-', ':', '：')
        checks["no_truncation"] = (
            last_line.endswith(proper_endings) or 
            last_line.startswith('#') or
            len(last_line) < 10  # very short last lines are likely intentional
        )
        if not checks["no_truncation"]:
            warnings.append(f"Possible truncation: last line does not end with punctuation: \"{last_line[-50:]}\"")
    else:
        checks["no_truncation"] = False
        warnings.append("No non-empty lines found")
    
    # Check 4: Bilingual balance
    if bilingual:
        cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        en_chars = sum(1 for c in text if c.isascii() and c.isalpha())
        cn_ratio = cn_chars / total_chars if total_chars > 0 else 0
        en_ratio = en_chars / total_chars if total_chars > 0 else 0
        
        checks["cn_char_ratio"] = round(cn_ratio, 3)
        checks["en_char_ratio"] = round(en_ratio, 3)
        
        # Expect both languages present in reasonable proportions
        checks["bilingual_balanced"] = cn_ratio > 0.1 and en_ratio > 0.05
        if not checks["bilingual_balanced"]:
            if cn_ratio < 0.1:
                warnings.append(f"Chinese character ratio too low ({cn_ratio:.1%}), translation may be missing")
            if en_ratio < 0.05:
                warnings.append(f"English character ratio too low ({en_ratio:.1%}), original text may be missing")

        text_blocks = body_paragraphs
        paired_blocks = 0
        for idx in range(0, len(text_blocks) - 1, 2):
            first = text_blocks[idx]
            second = text_blocks[idx + 1]
            first_en = any(ch.isascii() and ch.isalpha() for ch in first)
            second_cn = any('\u4e00' <= ch <= '\u9fff' for ch in second)
            if first_en and second_cn:
                paired_blocks += 1
        checks["bilingual_pairs"] = paired_blocks
        if text_blocks and paired_blocks == 0:
            hard_failures.append("No English/Chinese paragraph pairs detected in bilingual output")
    
    # Check 5: Size ratio vs raw text
    if raw_text_path:
        raw_path = Path(raw_text_path)
        if raw_path.exists():
            try:
                raw_text = raw_path.read_text(encoding='utf-8')
                raw_chars = len(raw_text)
                if raw_chars > 0:
                    size_ratio = total_chars / raw_chars
                    checks["raw_text_chars"] = raw_chars
                    checks["size_ratio_vs_raw"] = round(size_ratio, 2)
                    
                    # For bilingual, expect ~1.5-3x; for monolingual, expect ~0.8-1.5x
                    if bilingual:
                        checks["size_ratio_ok"] = 1.2 <= size_ratio <= 4.0
                        if not checks["size_ratio_ok"]:
                            warnings.append(f"Size ratio {size_ratio:.2f}x vs raw text is outside expected range (1.2-4.0x for bilingual)")
                    else:
                        checks["size_ratio_ok"] = 0.7 <= size_ratio <= 2.0
                        if not checks["size_ratio_ok"]:
                            warnings.append(f"Size ratio {size_ratio:.2f}x vs raw text is outside expected range (0.7-2.0x for monolingual)")
            except Exception:
                pass  # Non-critical, skip if raw text unreadable
    
    # Overall result
    passed = len(hard_failures) == 0
    
    return {
        "passed": passed,
        "checks": checks,
        "warnings": warnings,
        "hard_failures": hard_failures,
    }


STATE_STAGE_FIELDS = {
    "metadata": ["vid", "url", "title", "channel", "upload_date", "duration", "output_dir"],
    "post-source": ["vid", "url", "title", "channel", "upload_date", "duration", "output_dir",
                    "mode", "src", "source_language", "subtitle_source"],
    "pre-assemble": ["vid", "url", "title", "channel", "upload_date", "duration", "output_dir",
                     "mode", "src", "source_language", "subtitle_source"],
    "final": ["vid", "url", "title", "channel", "upload_date", "duration", "output_dir",
              "mode", "src", "source_language", "subtitle_source", "output_file"],
}


def load_state(state_path: str) -> dict:
    """
    Load the workflow state markdown file as a simple flat key/value mapping.
    """
    path = Path(state_path)
    if not path.exists():
        print(f"Error: State file not found: {state_path}", file=sys.stderr)
        sys.exit(1)

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error: Cannot read state file: {e}", file=sys.stderr)
        sys.exit(2)

    state = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        state[key.strip()] = value.strip()
    return state


def validate_state(state_path: str, stage: str = "", require: list[str] | None = None) -> dict:
    """
    Validate the state file for a workflow stage or explicit required fields.

    This is the canonical workflow checkpoint validator. Stages intentionally
    model the real execution order instead of enforcing one global required
    field set.
    """
    state = load_state(state_path)
    warnings = []
    hard_failures = []

    required_fields = []
    if stage:
        if stage not in STATE_STAGE_FIELDS:
            print(f"Error: Unknown state validation stage: {stage}", file=sys.stderr)
            sys.exit(2)
        required_fields.extend(STATE_STAGE_FIELDS[stage])
    if require:
        for field in require:
            if field not in required_fields:
                required_fields.append(field)

    missing_fields = []
    placeholder_fields = []
    present_fields = []

    for field in required_fields:
        value = state.get(field, "")
        if not value:
            missing_fields.append(field)
        elif value.lower() == "unknown":
            placeholder_fields.append(field)
        else:
            present_fields.append(field)

    if missing_fields:
        hard_failures.append("Missing required state fields: " + ", ".join(missing_fields))
    if placeholder_fields:
        hard_failures.append("Unresolved placeholder state fields: " + ", ".join(placeholder_fields))

    checks = {
        "stage": stage or "custom",
        "required_fields": required_fields,
        "present_fields": present_fields,
        "missing_fields": missing_fields,
        "placeholder_fields": placeholder_fields,
    }

    return {
        "passed": len(hard_failures) == 0,
        "checks": checks,
        "warnings": warnings,
        "hard_failures": hard_failures,
    }


def plan_optimization(state_path: str) -> dict:
    """
    Build a structured optimization plan from validated workflow state.

    This is the canonical routing source for text optimization. Workflow docs
    should consume this JSON instead of re-deriving short/long or bilingual
    branches in prose.
    """
    validation = validate_state(state_path, stage="post-source")
    if not validation["passed"]:
        return {
            "passed": False,
            "checks": validation["checks"],
            "warnings": validation["warnings"],
            "hard_failures": validation["hard_failures"],
        }

    state = load_state(state_path)
    try:
        duration = int(state.get("duration", "0") or 0)
    except ValueError:
        duration = 0

    mode = state.get("mode", "")
    source = state.get("src", "")
    work_dir = state.get("work_dir", "/tmp/unknown_chunks")
    video_id = state.get("vid", "")

    video_path = "long" if duration >= 1800 else "short"
    outputs = {
        "raw_text": f"/tmp/{video_id}_raw_text.txt",
        "structured_text": f"/tmp/{video_id}_structured.txt",
        "optimized_text": f"/tmp/{video_id}_optimized.txt",
        "work_dir": work_dir,
    }

    def build_execution_contract(kind: str, input_key: str = "") -> dict:
        if kind != "chunk":
            return {
                "mode": "single_pass",
                "supports_auto_replan": False,
                "recommended_cli_flags": [],
                "on_replan_required": "not_applicable",
            }

        normalized_input_key = str(input_key or "").strip() or "raw_path"
        if normalized_input_key == "raw_path":
            return {
                "mode": "chunked",
                "supports_auto_replan": True,
                "recommended_cli_flags": ["--auto-replan"],
                "on_replan_required": "auto_replan_remaining",
            }

        return {
            "mode": "chunked",
            "supports_auto_replan": False,
            "recommended_cli_flags": [],
            "on_replan_required": "stop_and_review",
        }

    if video_path == "short":
        if mode == "bilingual":
            operations = [
                {
                    "kind": "prompt",
                    "prompt": "structure_only",
                    "input": outputs["raw_text"],
                    "output": outputs["structured_text"],
                    "extra_instruction": "",
                    "execution": build_execution_contract("prompt"),
                },
                {
                    "kind": "prompt",
                    "prompt": "translate_only",
                    "input": outputs["structured_text"],
                    "output": outputs["optimized_text"],
                    "extra_instruction": "",
                    "execution": build_execution_contract("prompt"),
                },
            ]
        else:
            extra_instruction = ""
            if source == "deepgram":
                extra_instruction = "Also fix: Chinese character spacing, add punctuation based on context, remove repeated phrases"
            operations = [
                {
                    "kind": "prompt",
                    "prompt": "structure_only",
                    "input": outputs["raw_text"],
                    "output": outputs["optimized_text"],
                    "extra_instruction": extra_instruction,
                    "execution": build_execution_contract("prompt"),
                }
            ]
    else:
        extra_instruction = ""
        if mode == "chinese" and source == "deepgram":
            extra_instruction = "Also fix: Chinese character spacing, add punctuation based on context, remove repeated phrases"

        operations = [
            {
                "kind": "chunk",
                "prompt": "structure_only",
                "input_key": "raw_path",
                "extra_instruction": extra_instruction,
                "execution": build_execution_contract("chunk", "raw_path"),
            }
        ]
        if mode == "bilingual":
            operations.append(
                {
                    "kind": "chunk",
                    "prompt": "translate_only",
                    "input_key": "processed_path",
                    "extra_instruction": "",
                    "execution": build_execution_contract("chunk", "processed_path"),
                }
            )

    return {
        "passed": True,
        "checks": {
            "state_stage": "post-source",
            "duration": duration,
            "mode": mode,
            "source": source,
            "video_path": video_path,
        },
        "warnings": [],
        "hard_failures": [],
        "video_path": video_path,
        "requires_llm_preflight": video_path == "long",
        "requires_quality_check": True,
        "replan_contract": {
            "raw_path": {
                "supports_auto_replan": True,
                "recommended_cli_flags": ["--auto-replan"],
                "on_replan_required": "auto_replan_remaining",
            },
            "processed_path": {
                "supports_auto_replan": False,
                "recommended_cli_flags": [],
                "on_replan_required": "stop_and_review",
            },
        },
        "operations": operations,
        "outputs": outputs,
    }


def load_config(config_path: str = None, allow_missing: bool = False) -> dict:
    """
    Load configuration from config.yaml

    Args:
        config_path: Optional path to config file.
                     Defaults to <skill-root>/config.yaml

    Returns:
        Parsed flat config with typed LLM tuning settings.
    """
    if config_path is None:
        config_path = str(_default_config_path())

    path = Path(config_path)
    defaults = _default_config_values(str(path.absolute()))
    if not path.exists():
        if allow_missing:
            return defaults
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        content = path.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Error: Cannot read config file: {e}", file=sys.stderr)
        sys.exit(2)

    config = {}
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            key, _, value = line.partition(':')
            key = key.strip()
            value = _strip_inline_comment(value)
            value = _strip_wrapping_quotes(value.strip())
            if key:
                config[key] = value

    config_warnings = []

    def parse_int_field(key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        raw = config.get(key)
        if raw is None or raw == '':
            return default
        try:
            parsed = int(str(raw).strip())
        except (TypeError, ValueError):
            config_warnings.append(f"{key}={raw!r} is not a valid integer; using default {default}")
            return default
        if minimum is not None and parsed < minimum:
            if maximum is None:
                config_warnings.append(f"{key}={raw!r} is below minimum {minimum}; using default {default}")
            else:
                config_warnings.append(f"{key}={raw!r} is outside [{minimum}, {maximum}]; using default {default}")
            return default
        if maximum is not None and parsed > maximum:
            config_warnings.append(f"{key}={raw!r} is outside [{minimum}, {maximum}]; using default {default}")
            return default
        return parsed

    def parse_float_field(key: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
        raw = config.get(key)
        if raw is None or raw == '':
            return default
        try:
            parsed = float(str(raw).strip())
        except (TypeError, ValueError):
            config_warnings.append(f"{key}={raw!r} is not a valid number; using default {default}")
            return default
        if minimum is not None and parsed < minimum:
            if maximum is None:
                config_warnings.append(f"{key}={raw!r} is below minimum {minimum}; using default {default}")
            else:
                config_warnings.append(f"{key}={raw!r} is outside [{minimum}, {maximum}]; using default {default}")
            return default
        if maximum is not None and parsed > maximum:
            config_warnings.append(f"{key}={raw!r} is outside [{minimum}, {maximum}]; using default {default}")
            return default
        return parsed

    output_dir = config.get('output_dir', '')
    if output_dir:
        output_dir = os.path.expanduser(output_dir)
        if not os.path.isdir(output_dir):
            print(f"Warning: output_dir does not exist: {output_dir}", file=sys.stderr)

    llm_timeout_sec = parse_int_field('llm_timeout_sec', 120, minimum=1)
    llm_max_retries = parse_int_field('llm_max_retries', 3, minimum=0)
    llm_backoff_sec = parse_float_field('llm_backoff_sec', 1.5, minimum=0.1)
    llm_probe_timeout_sec = parse_int_field('llm_probe_timeout_sec', 20, minimum=1)
    llm_probe_max_tokens = parse_int_field('llm_probe_max_tokens', 16, minimum=1)
    llm_stop_after_consecutive_timeouts = parse_int_field('llm_stop_after_consecutive_timeouts', 2, minimum=1)

    parsed = dict(defaults)
    parsed.update({
        "output_dir": output_dir,
        "deepgram_api_key": config.get('deepgram_api_key', ''),
        "llm_api_key": config.get('llm_api_key', ''),
        "llm_base_url": config.get('llm_base_url', ''),
        "llm_model": config.get('llm_model', ''),
        "llm_api_format": config.get('llm_api_format', 'openai'),
        "llm_timeout_sec": llm_timeout_sec,
        "llm_max_retries": llm_max_retries,
        "llm_backoff_sec": llm_backoff_sec,
        "llm_stream": _normalize_stream_mode(config.get('llm_stream', 'auto')),
        "llm_probe_timeout_sec": llm_probe_timeout_sec,
        "llm_probe_max_tokens": llm_probe_max_tokens,
        "llm_stop_after_consecutive_timeouts": llm_stop_after_consecutive_timeouts,
        "chunk_mode": _normalize_chunk_mode(config.get('chunk_mode', DEFAULT_CHUNK_MODE)),
        "chunk_size_override": parse_int_field('chunk_size_override', 0, minimum=0),
        "chunk_tokens_structure_only": parse_int_field('chunk_tokens_structure_only', TASK_CHUNK_TOKEN_DEFAULTS['structure_only'], minimum=1),
        "chunk_tokens_quick_cleanup": parse_int_field('chunk_tokens_quick_cleanup', TASK_CHUNK_TOKEN_DEFAULTS['quick_cleanup'], minimum=1),
        "chunk_tokens_translate_only": parse_int_field('chunk_tokens_translate_only', TASK_CHUNK_TOKEN_DEFAULTS['translate_only'], minimum=1),
        "chunk_tokens_summarize": parse_int_field('chunk_tokens_summarize', TASK_CHUNK_TOKEN_DEFAULTS['summarize'], minimum=1),
        "chunk_hard_cap_multiplier": parse_float_field(
            'chunk_hard_cap_multiplier',
            DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
            minimum=1.0,
            maximum=MAX_CHUNK_HARD_CAP_MULTIPLIER,
        ),
        "chunk_safety_buffer_tokens": parse_int_field('chunk_safety_buffer_tokens', DEFAULT_CHUNK_SAFETY_BUFFER_TOKENS, minimum=0),
        "chunk_overlap_sentences": parse_int_field('chunk_overlap_sentences', DEFAULT_CHUNK_OVERLAP_SENTENCES, minimum=0),
        "chunk_context_tail_sentences": parse_int_field('chunk_context_tail_sentences', DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES, minimum=0),
        "chunk_context_summary_tokens": parse_int_field('chunk_context_summary_tokens', DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS, minimum=0),
        "output_ratio_structure_only": parse_float_field('output_ratio_structure_only', TASK_OUTPUT_RATIO_DEFAULTS['structure_only'], minimum=0.01),
        "output_ratio_quick_cleanup": parse_float_field('output_ratio_quick_cleanup', TASK_OUTPUT_RATIO_DEFAULTS['quick_cleanup'], minimum=0.01),
        "output_ratio_translate_only": parse_float_field('output_ratio_translate_only', TASK_OUTPUT_RATIO_DEFAULTS['translate_only'], minimum=0.01),
        "output_ratio_summarize": parse_float_field('output_ratio_summarize', TASK_OUTPUT_RATIO_DEFAULTS['summarize'], minimum=0.01),
        "max_output_tokens_structure_only": parse_int_field('max_output_tokens_structure_only', TASK_MAX_OUTPUT_TOKEN_DEFAULTS['structure_only'], minimum=1),
        "max_output_tokens_quick_cleanup": parse_int_field('max_output_tokens_quick_cleanup', TASK_MAX_OUTPUT_TOKEN_DEFAULTS['quick_cleanup'], minimum=1),
        "max_output_tokens_translate_only": parse_int_field('max_output_tokens_translate_only', TASK_MAX_OUTPUT_TOKEN_DEFAULTS['translate_only'], minimum=1),
        "max_output_tokens_summarize": parse_int_field('max_output_tokens_summarize', TASK_MAX_OUTPUT_TOKEN_DEFAULTS['summarize'], minimum=1),
        "enable_token_count_probe": _parse_bool(config.get('enable_token_count_probe'), DEFAULT_ENABLE_TOKEN_COUNT_PROBE),
        "enable_chunk_autotune": _parse_bool(config.get('enable_chunk_autotune'), DEFAULT_ENABLE_CHUNK_AUTOTUNE),
        "autotune_reduce_percent": parse_float_field('autotune_reduce_percent', DEFAULT_AUTOTUNE_REDUCE_PERCENT, minimum=0.01, maximum=0.90),
        "autotune_increase_percent": parse_float_field('autotune_increase_percent', DEFAULT_AUTOTUNE_INCREASE_PERCENT, minimum=0.01, maximum=0.50),
        "autotune_success_window": parse_int_field('autotune_success_window', DEFAULT_AUTOTUNE_SUCCESS_WINDOW, minimum=1),
        "autotune_p95_latency_threshold_ms": parse_int_field('autotune_p95_latency_threshold_ms', DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS, minimum=1),
        "autotune_canary_chunks": parse_int_field('autotune_canary_chunks', DEFAULT_AUTOTUNE_CANARY_CHUNKS, minimum=1),
        "config_path": str(path.absolute()),
        "config_warnings": config_warnings,
    })
    if config_warnings:
        print(f"Warning: Invalid numeric config values in {path}:", file=sys.stderr)
        for warning in config_warnings:
            print(f"  - {warning}", file=sys.stderr)
    return parsed


def main():
    parser = argparse.ArgumentParser(
        description='yt-transcript utility script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # parse-vtt command
    vtt_parser = subparsers.add_parser(
        'parse-vtt',
        help='Parse VTT subtitle file, output plain text'
    )
    vtt_parser.add_argument('vtt_path', help='VTT file path')

    # process-deepgram command
    dg_parser = subparsers.add_parser(
        'process-deepgram',
        help='Process Deepgram JSON, output cleaned text and speaker count'
    )
    dg_parser.add_argument('json_path', help='Deepgram JSON file path')

    # sanitize-filename command
    fn_parser = subparsers.add_parser(
        'sanitize-filename',
        help='Clean illegal filename characters'
    )
    fn_parser.add_argument('title', help='Original title')

    # transcribe-deepgram command
    tdg_parser = subparsers.add_parser(
        'transcribe-deepgram',
        help='Call Deepgram API and merge split chunks automatically'
    )
    tdg_parser.add_argument('audio_path', help='Audio file path')
    tdg_parser.add_argument('--language', required=True, help='Transcription language code')
    tdg_parser.add_argument('--config-path', default=None, help='Optional path to config.yaml')
    tdg_parser.add_argument('--api-key', default='', help='Optional Deepgram API key override')
    tdg_parser.add_argument('--max-size', type=float, default=10.0,
                            help='Max chunk size in MB before splitting (default: 10)')
    tdg_parser.add_argument('--max-deviation', type=float, default=60.0,
                            help='Max deviation from silence split point in seconds (default: 60)')
    tdg_parser.add_argument('--timeout', type=int, default=300, help='Request timeout in seconds')
    tdg_parser.add_argument('--output-json', default='', help='Optional JSON output path (or prefix for chunked outputs)')
    tdg_parser.add_argument('--output-text', default='', help='Optional path to write merged transcript text')

    # test-deepgram-api command
    api_parser = subparsers.add_parser(
        'test-deepgram-api',
        help='Test Deepgram API key validity'
    )
    api_parser.add_argument('api_key', help='Deepgram API key')

    # test-llm-api command
    llm_api_parser = subparsers.add_parser(
        'test-llm-api',
        help='Test configured LLM API reachability and latency'
    )
    llm_api_parser.add_argument('--config-path', default=None, help='Optional path to config.yaml')
    llm_api_parser.add_argument('--api-key', default='', help='Optional LLM API key override')
    llm_api_parser.add_argument('--base-url', default='', help='Optional LLM base URL override')
    llm_api_parser.add_argument('--model', default='', help='Optional model override')
    llm_api_parser.add_argument('--api-format', default='', help='Optional API format override')
    llm_api_parser.add_argument('--timeout', type=int, default=0, help='Probe timeout in seconds')
    llm_api_parser.add_argument('--stream', default='', help='Streaming mode override: auto|true|false')

    token_probe_parser = subparsers.add_parser(
        'test-token-count',
        help='Probe provider token counting support with local fallback'
    )
    token_probe_parser.add_argument('--config-path', default=None, help='Optional path to config.yaml')
    token_probe_parser.add_argument('--api-key', default='', help='Optional LLM API key override')
    token_probe_parser.add_argument('--base-url', default='', help='Optional LLM base URL override')
    token_probe_parser.add_argument('--model', default='', help='Optional model override')
    token_probe_parser.add_argument('--api-format', default='', help='Optional API format override')
    token_probe_parser.add_argument('--timeout', type=int, default=0, help='Probe timeout in seconds')
    token_probe_parser.add_argument('--sample-text', default='Reply with OK only.', help='Sample text for token probe')

    # split-audio command
    split_parser = subparsers.add_parser(
        'split-audio',
        help='Split large audio file at silence points'
    )
    split_parser.add_argument('audio_path', help='Audio file path')
    split_parser.add_argument('--max-size', type=float, default=10.0,
                              help='Max chunk size in MB (default: 10)')
    split_parser.add_argument('--max-deviation', type=float, default=60.0,
                              help='Max deviation from split point in seconds (default: 60)')

    # chunk-text command
    chunk_parser = subparsers.add_parser(
        'chunk-text',
        help='Split text file into chunks by sentence boundary'
    )
    chunk_parser.add_argument('input_path', help='Input text file path')
    chunk_parser.add_argument('output_dir', help='Output directory for chunks')
    chunk_parser.add_argument('--chunk-size', type=int, default=0,
                              help='Target chunk size in the active chunk_mode; without --prompt it keeps legacy character sizing')
    chunk_parser.add_argument('--prompt', default='',
                              help='Optional prompt name for task-aware auto chunk sizing')
    chunk_parser.add_argument('--config-path', default=None,
                              help='Optional path to config file for chunk planning')

    # get-chapters command
    chapters_parser = subparsers.add_parser(
        'get-chapters',
        help='Fetch YouTube video chapter metadata'
    )
    chapters_parser.add_argument('video_url', help='YouTube video URL')

    # merge-content command
    merge_parser = subparsers.add_parser(
        'merge-content',
        help='Merge processed chunks with chapter headers'
    )
    merge_parser.add_argument('work_dir', help='Working directory with manifest.json')
    merge_parser.add_argument('output_file', help='Output file path')
    merge_parser.add_argument('--header', default='', help='Optional header content to prepend')

    # process-chunks command
    pc_parser = subparsers.add_parser(
        'process-chunks',
        help='Process chunks with isolated LLM API calls'
    )
    pc_parser.add_argument('work_dir', help='Working directory with manifest.json')
    pc_parser.add_argument('--prompt', required=True,
                           help='Prompt template name (e.g., structure_only, translate_only, summarize)')
    pc_parser.add_argument('--extra-instruction', default='',
                           help='Additional instruction to append to prompt')
    pc_parser.add_argument('--input-key', default='raw_path',
                           help='Manifest key for input files (default: raw_path, use processed_path for chained processing)')
    pc_parser.add_argument('--config-path', default=None,
                           help='Optional path to config file')
    pc_parser.add_argument('--dry-run', action='store_true',
                           help='Validate setup without calling API')
    pc_parser.add_argument('--force', action='store_true',
                           help='Reprocess chunks even if manifest status is done and output exists')
    pc_parser.add_argument('--auto-replan', action='store_true',
                           help='Automatically run replan-remaining and resume until the plan stabilizes')
    pc_parser.add_argument('--max-replans', type=int, default=3,
                           help='Maximum automatic replans when --auto-replan is enabled')

    # replan-remaining command
    replan_parser = subparsers.add_parser(
        'replan-remaining',
        help='Re-plan unfinished raw chunks after a controller abort'
    )
    replan_parser.add_argument('work_dir', help='Working directory with manifest.json')
    replan_parser.add_argument('--prompt', default='',
                               help='Optional prompt override; defaults to manifest plan prompt')
    replan_parser.add_argument('--chunk-size', type=int, default=0,
                               help='Optional override for the next plan target size')
    replan_parser.add_argument('--input-key', default='raw_path',
                               help='Input manifest key to replan (currently raw_path only)')
    replan_parser.add_argument('--config-path', default=None,
                               help='Optional path to config file')

    # assemble-final command
    af_parser = subparsers.add_parser(
        'assemble-final',
        help='Assemble final markdown file from optimized text and metadata'
    )
    af_parser.add_argument('optimized_text_path', help='Path to optimized text file')
    af_parser.add_argument('output_file', help='Path to write final markdown file')
    af_parser.add_argument('--title', default='', help='Video title')
    af_parser.add_argument('--source', default='', help='Video URL')
    af_parser.add_argument('--channel', default='', help='Channel name')
    af_parser.add_argument('--date', default='', help='Video upload date')
    af_parser.add_argument('--created', default='', help='File creation date')
    af_parser.add_argument('--duration', type=int, default=0, help='Video duration in seconds')
    af_parser.add_argument('--transcript-source', default='', help='youtube or deepgram')
    af_parser.add_argument('--bilingual', action='store_true', help='Whether content is bilingual')

    # verify-quality command
    vq_parser = subparsers.add_parser(
        'verify-quality',
        help='Verify quality of optimized text file (structural checks)'
    )
    vq_parser.add_argument('optimized_text_path', help='Path to optimized text file')
    vq_parser.add_argument('--raw-text', default=None, help='Path to raw text file for size comparison')
    vq_parser.add_argument('--bilingual', action='store_true', help='Whether content should be bilingual')

    # load-config command
    config_parser = subparsers.add_parser(
        'load-config',
        help='Load and return configuration from config.yaml'
    )
    config_parser.add_argument('--config-path', default=None,
                               help='Optional path to config file')

    # validate-state command
    state_parser = subparsers.add_parser(
        'validate-state',
        help='Validate workflow state fields for a given stage'
    )
    state_parser.add_argument('state_path', help='Path to state markdown file')
    state_parser.add_argument(
        '--stage',
        default='',
        choices=sorted(STATE_STAGE_FIELDS.keys()),
        help='Named validation stage to enforce',
    )
    state_parser.add_argument(
        '--require',
        action='append',
        default=[],
        help='Additional required field name (can be repeated)',
    )

    # plan-optimization command
    plan_parser = subparsers.add_parser(
        'plan-optimization',
        help='Generate a structured optimization plan from workflow state'
    )
    plan_parser.add_argument('state_path', help='Path to state markdown file')

    args = parser.parse_args()

    if args.command == 'parse-vtt':
        result = parse_vtt(args.vtt_path)
        print(result)

    elif args.command == 'process-deepgram':
        result = process_deepgram(args.json_path)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'sanitize-filename':
        result = sanitize_filename(args.title)
        print(result)

    elif args.command == 'transcribe-deepgram':
        result = transcribe_deepgram(
            args.audio_path,
            language=args.language,
            config_path=args.config_path,
            api_key=args.api_key,
            max_size_mb=args.max_size,
            max_deviation_sec=args.max_deviation,
            timeout=args.timeout,
            output_json=args.output_json,
            output_text=args.output_text
        )
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'test-deepgram-api':
        result = test_deepgram_api(args.api_key)
        print(json.dumps(result, ensure_ascii=False))
        if not result['valid']:
            sys.exit(1)

    elif args.command == 'test-llm-api':
        result = test_llm_api(
            config_path=args.config_path,
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            api_format=args.api_format,
            timeout_sec=args.timeout,
            stream_mode=args.stream,
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result['valid']:
            sys.exit(1)

    elif args.command == 'test-token-count':
        result = test_token_count(
            config_path=args.config_path,
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            api_format=args.api_format,
            timeout_sec=args.timeout,
            sample_text=args.sample_text,
        )
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'split-audio':
        result = split_audio(args.audio_path, args.max_size, args.max_deviation)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'chunk-text':
        result = chunk_text(args.input_path, args.output_dir, args.chunk_size, args.prompt, args.config_path)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'get-chapters':
        result = get_chapters(args.video_url)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'merge-content':
        result = merge_content(args.work_dir, args.output_file, args.header)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'process-chunks':
        if args.auto_replan and not args.dry_run:
            result = process_chunks_with_replans(
                args.work_dir,
                args.prompt,
                extra_instruction=args.extra_instruction,
                config_path=args.config_path,
                input_key=args.input_key,
                force=args.force,
                max_replans=args.max_replans,
            )
        else:
            result = process_chunks(
                args.work_dir, args.prompt, args.extra_instruction,
                args.config_path, args.dry_run, args.input_key, args.force
            )
        print(json.dumps(result, ensure_ascii=False))
        if not result.get('success', False) and not result.get('dry_run', False):
            sys.exit(1)

    elif args.command == 'replan-remaining':
        result = replan_remaining(
            args.work_dir,
            prompt_name=args.prompt,
            config_path=args.config_path,
            chunk_size=args.chunk_size,
            input_key=args.input_key,
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result.get('success', False):
            sys.exit(1)

    elif args.command == 'assemble-final':
        result = assemble_final(
            args.optimized_text_path, args.output_file,
            title=args.title, source=args.source, channel=args.channel,
            date=args.date, created=args.created, duration=args.duration,
            transcript_source=args.transcript_source, bilingual=args.bilingual
        )
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'verify-quality':
        result = verify_quality(
            args.optimized_text_path,
            raw_text_path=args.raw_text,
            bilingual=args.bilingual
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    elif args.command == 'load-config':
        result = load_config(args.config_path)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'validate-state':
        result = validate_state(args.state_path, stage=args.stage, require=args.require)
        print(json.dumps(result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    elif args.command == 'plan-optimization':
        result = plan_optimization(args.state_path)
        print(json.dumps(result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
