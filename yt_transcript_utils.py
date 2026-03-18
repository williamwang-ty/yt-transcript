#!/usr/bin/env python3
"""
yt-transcript utility script
Provides VTT parsing, Deepgram result processing, audio splitting, filename sanitization, etc.

This module also owns the script-first workflow checkpoints:
- `validate-state` for stage-based state validation
- `plan-optimization` for canonical short/long routing
- `verify-quality` for final stop/go gating

Usage:
    python3 yt_transcript_utils.py [--api-envelope] <command> [args]

Commands:
    parse-vtt <vtt_path>           Parse VTT subtitle file, output plain text
    parse-vtt-segments <vtt_path>  Parse VTT subtitle file, output aligned segments JSON
    process-deepgram <json_path>   Process Deepgram JSON, output cleaned text
    transcribe-deepgram <audio_path>  Call Deepgram API and auto-merge split chunks
    sanitize-filename "<title>"    Clean illegal filename characters
    test-deepgram-api <api_key>    Test Deepgram API key validity
    test-llm-api                   Probe configured LLM API reachability
    test-token-count               Probe provider token counting support with local fallback
    split-audio <audio_path>       Split large audio at silence points (--max-size, --max-deviation)
    chunk-text <input> <output_dir> Split text file into chunks by sentence boundary
    chunk-segments <segments_json> <output_dir> Split aligned source segments into timed chunks (--chapters for chapter-aware boundaries)
    chunk-document <normalized_document> <output_dir> Chunk a normalized document using its preferred source shape
    get-chapters <video_url>       Fetch YouTube video chapter metadata
    build-chapter-plan <chapters_json> <work_dir> <output_json>  Map YouTube chapters onto timed chunks
    merge-content <work_dir> <output_file>  Merge processed chunks with chapter headers
    process-chunks <work_dir> --prompt <name>  Process chunks with isolated LLM API calls (--input-key for chained processing)
    prepare-resume <work_dir>      Repair stale chunk/runtime state before resuming a run
    replan-remaining <work_dir>    Re-plan unfinished raw chunks after canary/autotune aborts
    runtime-status <work_dir>      Inspect manifest runtime, ownership, and local runtime-control status
    assemble-final <optimized_text> <output_file>  Assemble final markdown from optimized text + metadata
    verify-quality <optimized_text>  Verify quality of optimized text (structural checks)
    sync-state <state_ref>         Sync legacy state.md and authoritative machine_state.json
    normalize-document <state_ref> Materialize normalized_document.json from raw text or segments
    validate-state <state_path>    Validate workflow state fields for a given stage
    plan-optimization <state_path> Generate a structured optimization plan from workflow state

Global flags:
    --api-envelope               Emit stable `yt_transcript.command_result/v1` envelopes for kernel commands
"""

import argparse
import bisect
import hashlib
import http.client
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

import kernel_runtime
import kernel_state


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
DEFAULT_LLM_CHUNK_RECOVERY_ATTEMPTS = 1
DEFAULT_LLM_CHUNK_RECOVERY_BACKOFF_SEC = 1.0
MANIFEST_SCHEMA_VERSION = 5
CHUNK_CONTRACT_SCHEMA_VERSION = 1
CONTROL_CONTRACT_SCHEMA_VERSION = 1
COMMAND_RESULT_SCHEMA_VERSION = kernel_runtime.COMMAND_RESULT_SCHEMA_VERSION
COMMAND_RESULT_FORMAT = kernel_runtime.COMMAND_RESULT_FORMAT
TELEMETRY_EVENT_SCHEMA_VERSION = kernel_runtime.TELEMETRY_EVENT_SCHEMA_VERSION
TELEMETRY_EVENT_FORMAT = kernel_runtime.TELEMETRY_EVENT_FORMAT
RUNTIME_OWNERSHIP_SCHEMA_VERSION = kernel_runtime.RUNTIME_OWNERSHIP_SCHEMA_VERSION
RUNTIME_OWNERSHIP_FORMAT = kernel_runtime.RUNTIME_OWNERSHIP_FORMAT
RUNTIME_OWNER_FILENAME = kernel_runtime.RUNTIME_OWNER_FILENAME
DEFAULT_UNKNOWN_CHUNK_TOKENS = 900
CHUNK_SEPARATOR = "\n\n"
DEFAULT_UNKNOWN_OUTPUT_RATIO = 1.0
DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS = 1400
DEFAULT_UNKNOWN_REQUEST_CAP = 2600
DEFAULT_UNKNOWN_LEGACY_CHARS = 8000
SUPERSEDED_CHUNK_STATUS = "superseded"
INTERRUPTED_CHUNK_STATUS = "interrupted"
RESUMABLE_RUNTIME_STATUS = "resumable"
SHORT_OUTPUT_WARNING_RATIO = 0.5
TRANSLATION_WARNING_CN_RATIO = 0.1
STRUCTURE_HEADER_WARNING_MIN_CHARS = 2000


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
        "yt_dlp_socket_timeout_sec": -1,
        "yt_dlp_retries": -1,
        "yt_dlp_extractor_retries": -1,
        "yt_dlp_cookies_from_browser": "",
        "yt_dlp_cookies_file": "",
        "llm_timeout_sec": 120,
        "llm_max_retries": 3,
        "llm_backoff_sec": 1.5,
        "llm_stream": "auto",
        "llm_probe_timeout_sec": 20,
        "llm_probe_max_tokens": 16,
        "llm_stop_after_consecutive_timeouts": 2,
        "llm_chunk_recovery_attempts": DEFAULT_LLM_CHUNK_RECOVERY_ATTEMPTS,
        "llm_chunk_recovery_backoff_sec": DEFAULT_LLM_CHUNK_RECOVERY_BACKOFF_SEC,
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


def _continuity_tail_sentence_count(config: dict | None = None,
                                     continuity_policy: dict | None = None) -> int:
    policy = continuity_policy if isinstance(continuity_policy, dict) else {}
    if "tail_sentences" in policy:
        return max(0, _parse_int(policy.get("tail_sentences"), 0))
    return _parse_int_min(
        (config or {}).get("chunk_context_tail_sentences"),
        DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
        0,
    )


def _continuity_summary_token_cap(config: dict | None = None,
                                  continuity_policy: dict | None = None) -> int:
    policy = continuity_policy if isinstance(continuity_policy, dict) else {}
    if "summary_token_cap" in policy:
        return max(0, _parse_int(policy.get("summary_token_cap"), 0))
    return _parse_int_min(
        (config or {}).get("chunk_context_summary_tokens"),
        DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS,
        0,
    )


def _resolve_previous_tail_text(previous_chunk: dict | None, work_path: Path,
                                input_key: str, config: dict | None = None,
                                continuity_policy: dict | None = None) -> str:
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
                        _continuity_tail_sentence_count(config, continuity_policy),
                        config,
                    )
                except OSError:
                    pass

    return str(previous_chunk.get("tail_context_text", "")).strip()


def _build_continuity_context(previous_chunk: dict | None, work_path: Path,
                              config: dict | None = None,
                              input_key: str = "raw_path",
                              continuity_policy: dict | None = None) -> dict:
    """Build the lightweight continuity block inserted before the next chunk.

    `input_key` decides whether continuity should be derived from the raw-stage
    chunk files (`raw_path`) or from prior-stage processed files (`processed_path`).
    """
    previous_chunk = previous_chunk or {}
    continuity_policy = continuity_policy if isinstance(continuity_policy, dict) else {}
    tail_text = _resolve_previous_tail_text(
        previous_chunk,
        work_path,
        input_key,
        config,
        continuity_policy=continuity_policy,
    )
    section_title = _resolve_previous_section_title(previous_chunk, work_path, input_key)
    if not tail_text and not section_title:
        return {
            "text": "",
            "tail_text": "",
            "section_title": "",
            "source_chunk_id": None,
            "token_count": 0,
            "mode": str(continuity_policy.get("mode", "")).strip() or "reference_only",
        }

    boundary_rule = str(continuity_policy.get("boundary_rule", "")).strip() or "Only transform the current chunk body below."
    output_rule = str(continuity_policy.get("output_rule", "")).strip() or "Do not repeat or rewrite this context in the output."
    parts = [
        "## Continuity Context",
        "",
        "Use this only as continuity reference from the previous chunk.",
        boundary_rule,
        output_rule,
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
        "mode": str(continuity_policy.get("mode", "")).strip() or "reference_only",
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


def _estimate_continuity_reserve_tokens(config: dict | None = None,
                                        continuity_policy: dict | None = None) -> int:
    config = config or {}
    tail_sentences = _continuity_tail_sentence_count(config, continuity_policy)
    summary_token_cap = _continuity_summary_token_cap(config, continuity_policy)
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
        continuity_policy=continuity_policy,
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


def _build_manifest_chunk_contract(source_kind: str = "text", *, driver: str = "",
                                   normalized_document_path: str = "", source_adapter: str = "",
                                   has_timing: bool = False, chapters_enabled: bool = False) -> dict:
    normalized_source_kind = "segments" if str(source_kind).strip() == "segments" else "text"
    default_driver = "chunk-segments" if normalized_source_kind == "segments" else "chunk-text"
    return {
        "version": CHUNK_CONTRACT_SCHEMA_VERSION,
        "driver": str(driver or default_driver).strip(),
        "source_kind": normalized_source_kind,
        "boundary_mode": "strict",
        "output_scope": "current_chunk_only",
        "continuity_mode": "reference_only",
        "merge_strategy": "ordered_concat",
        "overlap_strategy": "context_only_no_output_overlap",
        "normalized_document_path": str(normalized_document_path).strip(),
        "source_adapter": str(source_adapter).strip(),
        "has_timing": bool(has_timing),
        "chapters_enabled": bool(chapters_enabled),
    }


def _build_manifest_continuity_policy(config: dict | None = None, *, tail_sentences: int | None = None,
                                      summary_token_cap: int | None = None) -> dict:
    config = config or {}
    resolved_tail_sentences = max(
        0,
        _parse_int(
            tail_sentences,
            _parse_int_min(config.get("chunk_context_tail_sentences"), DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES, 0),
        ),
    )
    resolved_summary_token_cap = max(
        0,
        _parse_int(
            summary_token_cap,
            _parse_int_min(config.get("chunk_context_summary_tokens"), DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS, 0),
        ),
    )
    return {
        "mode": "reference_only",
        "tail_sentences": resolved_tail_sentences,
        "summary_token_cap": resolved_summary_token_cap,
        "carry_section_title": True,
        "carry_tail_text": resolved_tail_sentences > 0,
        "boundary_rule": "Only transform the current chunk body below.",
        "output_rule": "Do not repeat or rewrite this context in the output.",
    }


def _build_manifest_plan(prompt_name: str, chunk_mode: str, recommended_chunk_size: int,
                         effective_chunk_size: int, budget: dict, *, source_file: str = "",
                         plan_id: str = "", prior_plan_id: str = "",
                         chunk_contract: dict | None = None,
                         continuity_policy: dict | None = None) -> dict:
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
        "chunk_contract": chunk_contract if isinstance(chunk_contract, dict) else _build_manifest_chunk_contract(),
        "continuity": continuity_policy if isinstance(continuity_policy, dict) else _build_manifest_continuity_policy(),
        "created_at": _now_iso(),
    }


def _normalize_operation_input_key(input_key: str = "") -> str:
    normalized = str(input_key or "").strip()
    return normalized or "raw_path"


def _resolve_replan_action(input_key: str = "") -> str:
    return "auto_replan_remaining" if _normalize_operation_input_key(input_key) == "raw_path" else "stop_and_review"


def _build_quality_gate_contract(*, bilingual: bool = False) -> dict:
    hard_failure_checks = [
        {"id": "file_exists", "severity": "hard_failure"},
        {"id": "file_readable", "severity": "hard_failure"},
        {"id": "non_empty", "severity": "hard_failure"},
        {
            "id": "section_headers_for_long_text",
            "severity": "hard_failure",
            "min_chars": 1200,
            "required_header_prefix": "## ",
        },
    ]
    warning_checks = [
        {
            "id": "body_paragraph_count",
            "severity": "warning",
            "min_paragraphs": 2,
            "min_chars": 400,
        },
        {"id": "no_truncation", "severity": "warning"},
        {
            "id": "size_ratio_vs_raw",
            "severity": "warning",
            "expected_range": [1.2, 4.0] if bilingual else [0.7, 2.0],
        },
    ]
    if bilingual:
        hard_failure_checks.append({
            "id": "bilingual_pairs",
            "severity": "hard_failure",
            "required_pair_order": ["english", "chinese"],
        })
        warning_checks.append({
            "id": "bilingual_balance",
            "severity": "warning",
            "min_cn_ratio": 0.1,
            "min_en_ratio": 0.05,
        })
    return {
        "version": CONTROL_CONTRACT_SCHEMA_VERSION,
        "scope": "final_output",
        "stop_rule": "hard_failures_stop",
        "warning_rule": "warnings_require_review",
        "hard_failure_checks": hard_failure_checks,
        "warning_checks": warning_checks,
    }


def _build_chunk_verification_contract(prompt_name: str, *, applicable: bool = True) -> dict:
    if not applicable:
        return {
            "version": CONTROL_CONTRACT_SCHEMA_VERSION,
            "scope": "single_pass_output",
            "retryable_checks": [],
            "warning_rule": "warnings_are_recorded",
        }

    retryable_checks = []
    if prompt_name != "summarize":
        retryable_checks.append({
            "id": "short_output",
            "severity": "repairable_warning",
            "retry_action": "retry_same_chunk_same_plan",
            "min_output_input_ratio": SHORT_OUTPUT_WARNING_RATIO,
        })
    if prompt_name in ("structure_only", "quick_cleanup"):
        retryable_checks.append({
            "id": "missing_headers",
            "severity": "repairable_warning",
            "retry_action": "retry_same_chunk_same_plan",
            "required_header_prefix": "## ",
            "min_input_chars": STRUCTURE_HEADER_WARNING_MIN_CHARS,
        })
    if prompt_name == "translate_only":
        retryable_checks.append({
            "id": "translation_skipped",
            "severity": "repairable_warning",
            "retry_action": "retry_same_chunk_same_plan",
            "min_cn_char_ratio": TRANSLATION_WARNING_CN_RATIO,
        })
    return {
        "version": CONTROL_CONTRACT_SCHEMA_VERSION,
        "scope": "chunk_output",
        "retryable_checks": retryable_checks,
        "warning_rule": "warnings_are_recorded",
    }


def _build_repair_contract(prompt_name: str, config: dict | None = None, *, applicable: bool = True) -> dict:
    config = config or {}
    if not applicable:
        return {
            "version": CONTROL_CONTRACT_SCHEMA_VERSION,
            "mode": "not_applicable",
            "max_retries_per_chunk": 0,
            "backoff_sec": 0.0,
            "retry_on_checks": [],
            "after_max_retries": "not_applicable",
        }

    retry_on_checks = [check["id"] for check in _build_chunk_verification_contract(prompt_name)["retryable_checks"]]
    return {
        "version": CONTROL_CONTRACT_SCHEMA_VERSION,
        "mode": "bounded_retry",
        "max_retries_per_chunk": _parse_int_min(
            config.get("llm_chunk_recovery_attempts"),
            DEFAULT_LLM_CHUNK_RECOVERY_ATTEMPTS,
            0,
        ),
        "backoff_sec": round(_parse_float_min(
            config.get("llm_chunk_recovery_backoff_sec"),
            DEFAULT_LLM_CHUNK_RECOVERY_BACKOFF_SEC,
            0.0,
        ), 2),
        "retry_on_checks": retry_on_checks,
        "retry_action": "retry_same_chunk_same_plan",
        "after_max_retries": "accept_with_warnings",
    }


def _build_replan_contract(input_key: str = "raw_path", *, applicable: bool = True,
                           canary_chunks: int = DEFAULT_AUTOTUNE_CANARY_CHUNKS,
                           max_auto_replans: int | None = None) -> dict:
    if not applicable:
        return {
            "version": CONTROL_CONTRACT_SCHEMA_VERSION,
            "mode": "not_applicable",
            "supports_auto_replan": False,
            "recommended_cli_flags": [],
            "on_replan_required": "not_applicable",
            "trigger_conditions": [],
        }

    action = _resolve_replan_action(input_key)
    supports_auto_replan = action == "auto_replan_remaining"
    contract = {
        "version": CONTROL_CONTRACT_SCHEMA_VERSION,
        "mode": "document_abort_and_replan",
        "supports_auto_replan": supports_auto_replan,
        "recommended_cli_flags": ["--auto-replan"] if supports_auto_replan else [],
        "on_replan_required": action,
        "auto_replan_eligible_input_keys": ["raw_path"],
        "trigger_conditions": [
            {
                "id": "timeout_retry_instability",
                "effect": "abort_current_run_and_replan_remaining",
            },
            {
                "id": "context_or_budget_error",
                "effect": "abort_current_run_and_replan_remaining",
            },
            {
                "id": "bad_response_requires_replan",
                "effect": "abort_current_run_and_replan_remaining",
            },
            {
                "id": "canary_autotune_shrink",
                "effect": "abort_current_run_and_replan_remaining",
                "canary_chunk_limit": max(1, _parse_int_min(canary_chunks, DEFAULT_AUTOTUNE_CANARY_CHUNKS, 1)),
            },
        ],
    }
    if max_auto_replans is not None:
        contract["max_auto_replans"] = max(0, _parse_int(max_auto_replans, 0))
    return contract


def _build_operation_control_contract(kind: str, prompt_name: str, *,
                                      input_key: str = "raw_path",
                                      config: dict | None = None,
                                      bilingual: bool = False,
                                      max_auto_replans: int | None = None) -> dict:
    config = config or {}
    is_chunk = kind == "chunk"
    return {
        "version": CONTROL_CONTRACT_SCHEMA_VERSION,
        "kind": "chunk" if is_chunk else "single_pass",
        "prompt_name": str(prompt_name or "").strip(),
        "input_key": _normalize_operation_input_key(input_key) if is_chunk else "",
        "verification": _build_chunk_verification_contract(prompt_name, applicable=is_chunk),
        "repair": _build_repair_contract(prompt_name, config, applicable=is_chunk),
        "replan": _build_replan_contract(
            input_key,
            applicable=is_chunk,
            canary_chunks=_parse_int_min(
                config.get("autotune_canary_chunks"),
                DEFAULT_AUTOTUNE_CANARY_CHUNKS,
                1,
            ),
            max_auto_replans=max_auto_replans,
        ),
        "quality_gate": _build_quality_gate_contract(bilingual=bilingual),
    }


def _build_runtime_control_state() -> dict:
    return {
        "verification_warning_count": 0,
        "repair_attempted_count": 0,
        "repair_exhausted_count": 0,
        "last_replan_trigger": "",
        "last_replan_action": "",
        "last_replan_chunk_id": None,
    }


def _ensure_runtime_control_state(runtime: dict) -> dict:
    control = runtime.get("control")
    if not isinstance(control, dict):
        control = _build_runtime_control_state()
        runtime["control"] = control
    control.setdefault("verification_warning_count", 0)
    control.setdefault("repair_attempted_count", 0)
    control.setdefault("repair_exhausted_count", 0)
    control.setdefault("last_replan_trigger", "")
    control.setdefault("last_replan_action", "")
    control.setdefault("last_replan_chunk_id", None)
    return control


def _build_chunk_control_state() -> dict:
    return {
        "verification_status": "pending",
        "warning_count": 0,
        "warnings": [],
        "retry_reasons": [],
        "repair_exhausted": False,
        "last_verified_at": "",
    }


def _ensure_chunk_control_state(chunk_info: dict) -> dict:
    control = chunk_info.get("control")
    if not isinstance(control, dict):
        control = _build_chunk_control_state()
        chunk_info["control"] = control
    control.setdefault("verification_status", "pending")
    control.setdefault("warning_count", 0)
    control.setdefault("warnings", [])
    control.setdefault("retry_reasons", [])
    control.setdefault("repair_exhausted", False)
    control.setdefault("last_verified_at", "")
    return control


def _record_chunk_verification(chunk_info: dict, *, status: str, warnings: list[str],
                               retry_reasons: list[str], repair_exhausted: bool = False) -> None:
    control = _ensure_chunk_control_state(chunk_info)
    control["verification_status"] = str(status or "pending")
    control["warning_count"] = len([warning for warning in (warnings or []) if str(warning).strip()])
    control["warnings"] = [str(warning) for warning in (warnings or []) if str(warning).strip()]
    control["retry_reasons"] = [str(reason) for reason in (retry_reasons or []) if str(reason).strip()]
    control["repair_exhausted"] = bool(repair_exhausted)
    control["last_verified_at"] = _now_iso()


def _classify_replan_trigger(error: Exception | None = None, *,
                             had_timeout_retry: bool = False,
                             autotune_last_event: str = "") -> str:
    if had_timeout_retry:
        return "timeout_retry_instability"
    if autotune_last_event == "shrink":
        return "canary_autotune_shrink"
    if error is not None:
        if _is_timeout_error(error):
            return "timeout_retry_instability"
        status_code = getattr(error, "status_code", None)
        response_hint = str(getattr(error, "response_body", "") or "").lower()
        if status_code in {413, 422}:
            return "context_or_budget_error"
        if status_code == 400 and any(token in response_hint for token in ("context", "prompt", "max token", "too long", "token limit")):
            return "context_or_budget_error"
        if str(getattr(error, "error_type", "") or "") == "bad_response":
            return "bad_response_requires_replan"
    return "manual_review"


def _mark_runtime_replan(runtime: dict, *, reason: str, trigger: str,
                         input_key: str, chunk_id: int | None = None) -> None:
    runtime["replan_required"] = True
    runtime["replan_reason"] = str(reason or "").strip()
    control = _ensure_runtime_control_state(runtime)
    control["last_replan_trigger"] = str(trigger or "manual_review")
    control["last_replan_action"] = _resolve_replan_action(input_key)
    control["last_replan_chunk_id"] = chunk_id


def _build_process_control_summary(runtime: dict, operation_control: dict) -> dict:
    runtime_control = _ensure_runtime_control_state(runtime)
    replan_contract = operation_control.get("replan", {}) if isinstance(operation_control.get("replan", {}), dict) else {}
    repair_contract = operation_control.get("repair", {}) if isinstance(operation_control.get("repair", {}), dict) else {}
    return {
        "operation": operation_control,
        "verification": {
            "warning_count": runtime_control.get("verification_warning_count", 0),
        },
        "repair": {
            "attempted_count": runtime_control.get("repair_attempted_count", 0),
            "exhausted_count": runtime_control.get("repair_exhausted_count", 0),
            "max_retries_per_chunk": repair_contract.get("max_retries_per_chunk", 0),
            "backoff_sec": repair_contract.get("backoff_sec", 0.0),
            "after_max_retries": repair_contract.get("after_max_retries", "not_applicable"),
        },
        "replan": {
            "required": bool(runtime.get("replan_required", False)),
            "reason": str(runtime.get("replan_reason", "") or ""),
            "trigger": str(runtime_control.get("last_replan_trigger", "") or ""),
            "action": str(runtime_control.get("last_replan_action", replan_contract.get("on_replan_required", "not_applicable")) or ""),
            "supports_auto_replan": bool(replan_contract.get("supports_auto_replan", False)),
            "last_replan_chunk_id": runtime_control.get("last_replan_chunk_id"),
        },
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
        "interrupted_count": 0,
        "current_chunk_index": 0,
        "replan_required": False,
        "replan_reason": "",
        "last_replanned_at": "",
        "last_resume_check_at": "",
        "last_resume_repair_at": "",
        "resume_repair_count": 0,
        "operation_prompt_name": "",
        "operation_input_key": "raw_path",
        "operation_control": {},
        "control": _build_runtime_control_state(),
        "updated_at": _now_iso(),
    }


def _new_chunk_manifest_entry(chunk_id: int, chunk_content: str, budget: dict,
                              config: dict | None = None, *, raw_path: str = "",
                              processed_path: str = "", plan_id: str = "",
                              continuity_prev_chunk_id: int | None = None,
                              chunk_contract: dict | None = None,
                              continuity_policy: dict | None = None) -> dict:
    config = config or {}
    chunk_contract = chunk_contract if isinstance(chunk_contract, dict) else _build_manifest_chunk_contract()
    continuity_policy = continuity_policy if isinstance(continuity_policy, dict) else _build_manifest_continuity_policy(config)
    return {
        "id": chunk_id,
        "chunk_id": chunk_id,
        "plan_id": plan_id,
        "raw_path": raw_path,
        "processed_path": processed_path,
        "source_kind": str(chunk_contract.get("source_kind", "text")).strip() or "text",
        "boundary_mode": str(chunk_contract.get("boundary_mode", "strict")).strip() or "strict",
        "output_scope": str(chunk_contract.get("output_scope", "current_chunk_only")).strip() or "current_chunk_only",
        "continuity_mode": str(continuity_policy.get("mode", "reference_only")).strip() or "reference_only",
        "char_count": len(chunk_content),
        "input_chars": len(chunk_content),
        "estimated_input_tokens": _estimate_tokens(chunk_content, "tokens", config),
        "input_tokens": _estimate_tokens(chunk_content, "tokens", config),
        "token_count_source": budget.get("token_count_source", ""),
        "tail_context_text": _extract_tail_sentences(
            chunk_content,
            max(0, _parse_int(continuity_policy.get("tail_sentences"), DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES)),
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
        "recovery_attempts": 0,
        "recovery_logs": [],
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
        "control": _build_chunk_control_state(),
    }


def _resolve_chunk_output_filename(chunk_info: dict, prompt_name: str = "") -> str:
    chunk_id = _parse_int(chunk_info.get("id", chunk_info.get("chunk_id", 0)), 0)
    if prompt_name == "summarize":
        return f"summary_chunk_{chunk_id:03d}.txt"
    filename = str(chunk_info.get("processed_path", "")).strip()
    return filename or f"processed_{chunk_id:03d}.md"


def _resolve_chunk_output_path(work_path: Path, chunk_info: dict, prompt_name: str = "") -> Path:
    return work_path / _resolve_chunk_output_filename(chunk_info, prompt_name)


def _ensure_chunk_runtime_defaults(manifest: dict, runtime: dict, plan: dict,
                                   prompt_budget: dict, request_url: str,
                                   planned_max_output_tokens: int,
                                   autotune_state: dict) -> None:
    for chunk_info in manifest.get("chunks", []):
        chunk_info.setdefault("chunk_id", chunk_info.get("id", 0))
        chunk_info.setdefault("plan_id", runtime.get("active_plan_id", plan.get("plan_id", "")))
        chunk_info.setdefault("status", "pending")
        chunk_info.setdefault("attempts", 0)
        chunk_info.setdefault("attempt_logs", [])
        chunk_info.setdefault("recovery_attempts", 0)
        chunk_info.setdefault("recovery_logs", [])
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
        _ensure_chunk_control_state(chunk_info)


def _infer_resume_runtime_status(runtime: dict, chunks: list[dict]) -> str:
    active_chunks = [chunk for chunk in (chunks or []) if chunk.get("status") != SUPERSEDED_CHUNK_STATUS]
    active_statuses = {str(chunk.get("status", "pending")).strip() or "pending" for chunk in active_chunks}
    previous_status = str(runtime.get("status", "pending")).strip() or "pending"

    if runtime.get("replan_required", False):
        return "aborted"
    if active_chunks and active_statuses == {"done"}:
        return "completed"
    if INTERRUPTED_CHUNK_STATUS in active_statuses:
        return RESUMABLE_RUNTIME_STATUS
    if active_statuses and active_statuses.issubset({"done", "failed"}) and "failed" in active_statuses:
        return "completed_with_errors"
    if previous_status == "running":
        return RESUMABLE_RUNTIME_STATUS
    if "pending" in active_statuses:
        if previous_status in {RESUMABLE_RUNTIME_STATUS, "aborted", "completed", "completed_with_errors"}:
            return RESUMABLE_RUNTIME_STATUS
        return "pending"
    if "failed" in active_statuses:
        if previous_status in {RESUMABLE_RUNTIME_STATUS, "aborted"}:
            return RESUMABLE_RUNTIME_STATUS
        return "completed_with_errors"
    return previous_status


def _prepare_manifest_for_resume(manifest: dict, work_path: Path, prompt_name: str = "") -> dict:
    runtime = manifest.get("runtime", {}) if isinstance(manifest.get("runtime", {}), dict) else {}
    now = _now_iso()
    report = {
        "repaired": False,
        "repair_count": 0,
        "promoted_done_chunk_ids": [],
        "interrupted_chunk_ids": [],
        "demoted_missing_output_chunk_ids": [],
        "runtime_status_before": str(runtime.get("status", "pending")).strip() or "pending",
        "runtime_status_after": "",
        "interrupted_count": 0,
        "warnings": [],
    }

    def mark_promoted_done(chunk_info: dict, output_path: Path, reason: str) -> None:
        try:
            output_text = output_path.read_text(encoding="utf-8")
        except OSError:
            output_text = ""
        chunk_info["status"] = "done"
        chunk_info["output_chars"] = len(output_text)
        chunk_info["actual_output_tokens"] = max(
            _parse_int(chunk_info.get("actual_output_tokens"), 0),
            _estimate_tokens(output_text, "tokens") if output_text else 0,
        )
        chunk_info["last_error"] = ""
        chunk_info["last_error_type"] = ""
        chunk_info["error_type"] = ""
        chunk_info["completed_at"] = str(chunk_info.get("completed_at", "")).strip() or now
        chunk_info["updated_at"] = now
        report["promoted_done_chunk_ids"].append(_parse_int(chunk_info.get("id"), 0))
        report["warnings"].append(reason)

    def mark_interrupted(chunk_info: dict, reason: str, bucket: str) -> None:
        chunk_info["status"] = INTERRUPTED_CHUNK_STATUS
        chunk_info["last_error"] = reason
        chunk_info["last_error_type"] = "resume_interrupted"
        chunk_info["error_type"] = "resume_interrupted"
        chunk_info["completed_at"] = ""
        chunk_info["updated_at"] = now
        report[bucket].append(_parse_int(chunk_info.get("id"), 0))
        report["warnings"].append(reason)

    for chunk_info in manifest.get("chunks", []):
        if chunk_info.get("status") == SUPERSEDED_CHUNK_STATUS:
            continue
        output_path = _resolve_chunk_output_path(work_path, chunk_info, prompt_name)
        output_exists = output_path.exists()
        status = str(chunk_info.get("status", "pending")).strip() or "pending"

        if status == "running":
            if output_exists:
                mark_promoted_done(
                    chunk_info,
                    output_path,
                    f"Resume repair: promoted stale running chunk {_parse_int(chunk_info.get('id'), 0)} to done from {output_path.name}",
                )
            else:
                mark_interrupted(
                    chunk_info,
                    f"Resume repair: marked stale running chunk {_parse_int(chunk_info.get('id'), 0)} as interrupted because {output_path.name} is missing",
                    "interrupted_chunk_ids",
                )
        elif status == "done" and not output_exists:
            mark_interrupted(
                chunk_info,
                f"Resume repair: demoted done chunk {_parse_int(chunk_info.get('id'), 0)} because checkpoint file {output_path.name} is missing",
                "demoted_missing_output_chunk_ids",
            )
        elif status == INTERRUPTED_CHUNK_STATUS and output_exists:
            mark_promoted_done(
                chunk_info,
                output_path,
                f"Resume repair: promoted interrupted chunk {_parse_int(chunk_info.get('id'), 0)} to done from {output_path.name}",
            )

    runtime_status_after = _infer_resume_runtime_status(runtime, manifest.get("chunks", []))
    runtime["last_resume_check_at"] = now
    runtime["interrupted_count"] = sum(
        1
        for chunk in manifest.get("chunks", [])
        if chunk.get("status") == INTERRUPTED_CHUNK_STATUS
    )
    if report["promoted_done_chunk_ids"] or report["interrupted_chunk_ids"] or report["demoted_missing_output_chunk_ids"]:
        runtime["last_resume_repair_at"] = now
        runtime["resume_repair_count"] = max(0, _parse_int(runtime.get("resume_repair_count"), 0)) +             len(report["promoted_done_chunk_ids"]) + len(report["interrupted_chunk_ids"]) + len(report["demoted_missing_output_chunk_ids"])
    runtime["status"] = runtime_status_after
    runtime["updated_at"] = now

    report["runtime_status_after"] = runtime_status_after
    report["interrupted_count"] = runtime["interrupted_count"]
    report["repair_count"] = len(report["promoted_done_chunk_ids"]) + len(report["interrupted_chunk_ids"]) + len(report["demoted_missing_output_chunk_ids"])
    report["repaired"] = report["repair_count"] > 0 or report["runtime_status_before"] != runtime_status_after
    return report


def _format_resume_report(report: dict) -> str:
    if not isinstance(report, dict) or not report.get("repaired", False):
        return ""
    parts = []
    if report.get("promoted_done_chunk_ids"):
        parts.append(f"promoted={len(report['promoted_done_chunk_ids'])}")
    if report.get("interrupted_chunk_ids"):
        parts.append(f"interrupted={len(report['interrupted_chunk_ids'])}")
    if report.get("demoted_missing_output_chunk_ids"):
        parts.append(f"demoted_missing_output={len(report['demoted_missing_output_chunk_ids'])}")
    parts.append(f"runtime={report.get('runtime_status_before', '')}->{report.get('runtime_status_after', '')}")
    return "Resume repair: " + ", ".join(parts)


def _resolve_runtime_mutation_ownership(work_dir: str, operation: str,
                                      runtime_ownership: dict | None = None) -> tuple[dict, bool]:
    if isinstance(runtime_ownership, dict) and str(runtime_ownership.get("owner_id", "")).strip():
        borrowed = dict(runtime_ownership)
        borrowed["delegated"] = True
        return borrowed, False
    return kernel_runtime.acquire_runtime_ownership(work_dir, operation), True


def _finalize_mutation_result(result: dict, ownership: dict | None,
                              release_result: dict | None = None) -> dict:
    if isinstance(result, dict) and isinstance(ownership, dict):
        result["ownership"] = kernel_runtime.finalize_runtime_ownership(ownership, release_result)
    return result


def _runtime_ownership_error_parts(ownership: dict) -> tuple[str, str]:
    error = str(ownership.get("error") or "Runtime ownership conflict").strip()
    message = str(ownership.get("message") or error).strip()
    return error, message


def _build_prepare_resume_ownership_conflict_result(manifest_path: Path, prompt_name: str,
                                                    ownership: dict) -> dict:
    error, message = _runtime_ownership_error_parts(ownership)
    return _finalize_mutation_result({
        "success": False,
        "manifest_path": str(manifest_path),
        "prompt_name": str(prompt_name or "").strip(),
        "resume": {"repaired": False, "warnings": []},
        "runtime": {},
        "error": error,
        "message": message,
    }, ownership)


def _build_process_ownership_conflict_result(ownership: dict) -> dict:
    error, message = _runtime_ownership_error_parts(ownership)
    return _finalize_mutation_result({
        "success": False,
        "processed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "superseded_count": 0,
        "total_chunks": 0,
        "warnings": [],
        "warning_count": 0,
        "output_files": [],
        "aborted": True,
        "aborted_reason": message,
        "replan_required": False,
        "replan_reason": "",
        "resume": {"repaired": False, "warnings": []},
        "plan": {},
        "control": {},
        "request_url": "",
        "error": error,
        "message": message,
    }, ownership)


def _build_replan_ownership_conflict_result(ownership: dict) -> dict:
    error, message = _runtime_ownership_error_parts(ownership)
    return _finalize_mutation_result({
        "success": False,
        "replanned": False,
        "warnings": [],
        "error": error,
        "message": message,
    }, ownership)


def _build_process_with_replans_ownership_conflict_result(ownership: dict) -> dict:
    error, message = _runtime_ownership_error_parts(ownership)
    return _finalize_mutation_result({
        "processed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "superseded_count": 0,
        "warnings": [],
        "warning_count": 0,
        "output_files": [],
        "replan_count": 0,
        "request_url": "",
        "aborted": True,
        "aborted_reason": message,
        "success": False,
        "replan_required": False,
        "replan_reason": "",
        "plan": {},
        "control": {},
        "error": error,
        "message": message,
    }, ownership)


def prepare_resume(work_dir: str, prompt_name: str = "", config_path: str = None,
                   input_key: str = "raw_path", runtime_ownership: dict | None = None) -> dict:
    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    ownership, owned_here = _resolve_runtime_mutation_ownership(
        work_dir,
        "prepare-resume",
        runtime_ownership=runtime_ownership,
    )
    if owned_here and not ownership.get("success", False):
        return _build_prepare_resume_ownership_conflict_result(manifest_path, prompt_name, ownership)

    release_result = None
    try:
        result = _prepare_resume_impl(
            work_dir,
            prompt_name=prompt_name,
            config_path=config_path,
            input_key=input_key,
        )
    finally:
        if owned_here and ownership.get("success", False):
            release_result = kernel_runtime.release_runtime_ownership(work_dir, ownership.get("owner_id", ""))
    return _finalize_mutation_result(result, ownership, release_result)


def _prepare_resume_impl(work_dir: str, prompt_name: str = "", config_path: str = None,
                         input_key: str = "raw_path") -> dict:
    del input_key
    work_path = Path(work_dir)
    manifest_path = work_path / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = _load_optional_config(config_path)
    resolved_prompt_name = str(prompt_name or manifest.get("plan", {}).get("prompt_name", manifest.get("prompt_name", ""))).strip()
    prompt_template = ""
    if resolved_prompt_name:
        prompt_template = _resolve_prompt_template_path(resolved_prompt_name).read_text(encoding="utf-8")
    prompt_budget = _calculate_chunk_budget(resolved_prompt_name, prompt_template, config)
    manifest = _ensure_manifest_structure(
        manifest,
        prompt_name=resolved_prompt_name,
        prompt_budget=prompt_budget,
        recommended_chunk_size=_recommended_chunk_size(resolved_prompt_name, config),
        source_file=str(manifest.get("source_file", "")).strip(),
        config=config,
    )
    plan = manifest["plan"]
    runtime = manifest["runtime"]
    planned_max_output_tokens = max(1, _parse_int(plan.get("planned_max_output_tokens"), prompt_budget["planned_max_output_tokens"]))
    autotune_state = _build_autotune_state(plan, config, manifest.get("autotune"))
    _ensure_chunk_runtime_defaults(
        manifest,
        runtime,
        plan,
        prompt_budget,
        request_url=str(runtime.get("last_request_url", "")).strip(),
        planned_max_output_tokens=planned_max_output_tokens,
        autotune_state=autotune_state,
    )
    resume = _prepare_manifest_for_resume(manifest, work_path, resolved_prompt_name)
    _refresh_manifest_token_source_summary(manifest)
    _sync_manifest_legacy_fields(manifest)
    _write_manifest(manifest_path, manifest)
    return {
        "success": True,
        "manifest_path": str(manifest_path),
        "prompt_name": resolved_prompt_name,
        "resume": resume,
        "runtime": manifest.get("runtime", {}),
    }


def _sync_manifest_legacy_fields(manifest: dict) -> dict:
    plan = manifest.get("plan", {}) if isinstance(manifest, dict) else {}
    runtime = manifest.get("runtime", {}) if isinstance(manifest, dict) else {}
    autotune = manifest.get("autotune", {}) if isinstance(manifest, dict) else {}
    chunk_contract = plan.get("chunk_contract", {}) if isinstance(plan.get("chunk_contract", {}), dict) else {}
    continuity = plan.get("continuity", {}) if isinstance(plan.get("continuity", {}), dict) else {}

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
    manifest["chunk_context_tail_sentences"] = _parse_int(
        continuity.get("tail_sentences"),
        manifest.get("chunk_context_tail_sentences", DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES),
    )
    manifest["chunk_context_summary_tokens"] = _parse_int(
        continuity.get("summary_token_cap"),
        manifest.get("chunk_context_summary_tokens", DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS),
    )
    manifest["normalized_document_path"] = str(
        chunk_contract.get("normalized_document_path", manifest.get("normalized_document_path", ""))
    ).strip()
    manifest["chunk_driver"] = str(chunk_contract.get("driver", manifest.get("chunk_driver", ""))).strip()
    manifest["source_adapter"] = str(chunk_contract.get("source_adapter", manifest.get("source_adapter", ""))).strip()
    manifest["chunk_source_kind"] = str(chunk_contract.get("source_kind", manifest.get("chunk_source_kind", ""))).strip()
    manifest["boundary_mode"] = str(chunk_contract.get("boundary_mode", manifest.get("boundary_mode", "strict"))).strip() or "strict"
    manifest["continuity_mode"] = str(continuity.get("mode", manifest.get("continuity_mode", "reference_only"))).strip() or "reference_only"
    manifest["autotune"] = autotune
    manifest["replan_required"] = runtime.get("replan_required", manifest.get("replan_required", False))
    manifest["replan_reason"] = runtime.get("replan_reason", manifest.get("replan_reason", ""))
    manifest["interrupted_count"] = runtime.get("interrupted_count", manifest.get("interrupted_count", 0))
    return manifest


def _ensure_manifest_structure(manifest: dict, *, prompt_name: str = "", prompt_budget: dict | None = None,
                               recommended_chunk_size: int = 0, request_url: str = "",
                               source_file: str = "", config: dict | None = None) -> dict:
    manifest = manifest if isinstance(manifest, dict) else {}
    prompt_budget = prompt_budget or {}
    config = config or {}
    chunk_mode = _normalize_chunk_mode(
        manifest.get("chunk_mode", prompt_budget.get("chunk_mode", DEFAULT_CHUNK_MODE))
    )
    effective_chunk_size = max(0, _parse_int(manifest.get("chunk_size"), 0))
    existing_plan = manifest.get("plan", {}) if isinstance(manifest.get("plan", {}), dict) else {}
    source_kind = str(
        manifest.get("chunk_source_kind")
        or existing_plan.get("chunk_contract", {}).get("source_kind", "")
        or ("segments" if str(manifest.get("source_segments_file", "")).strip() else "text")
    ).strip()
    has_timing = any(
        chunk.get("start_time") is not None or chunk.get("end_time") is not None
        for chunk in manifest.get("chunks", [])
        if isinstance(chunk, dict)
    )
    normalized_document_path = str(
        manifest.get("normalized_document_path")
        or existing_plan.get("chunk_contract", {}).get("normalized_document_path", "")
    ).strip()
    chunk_contract = existing_plan.get("chunk_contract") if isinstance(existing_plan.get("chunk_contract", {}), dict) else None
    if not chunk_contract:
        chunk_contract = _build_manifest_chunk_contract(
            source_kind,
            driver=str(manifest.get("chunk_driver", "")).strip() or ("chunk-segments" if source_kind == "segments" else "chunk-text"),
            normalized_document_path=normalized_document_path,
            source_adapter=str(manifest.get("source_adapter", "")).strip(),
            has_timing=has_timing,
            chapters_enabled=False,
        )
    continuity_policy = existing_plan.get("continuity") if isinstance(existing_plan.get("continuity", {}), dict) else None
    if not continuity_policy:
        continuity_policy = _build_manifest_continuity_policy(
            config,
            tail_sentences=_parse_int(
                manifest.get("chunk_context_tail_sentences"),
                _parse_int_min(config.get("chunk_context_tail_sentences"), DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES, 0),
            ),
            summary_token_cap=_parse_int(
                manifest.get("chunk_context_summary_tokens"),
                _parse_int_min(config.get("chunk_context_summary_tokens"), DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS, 0),
            ),
        )
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
            chunk_contract=chunk_contract,
            continuity_policy=continuity_policy,
        )
    else:
        manifest["plan"].setdefault("chunk_contract", chunk_contract)
        manifest["plan"].setdefault("continuity", continuity_policy)
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
    runtime.setdefault("interrupted_count", 0)
    runtime.setdefault("current_chunk_index", 0)
    runtime.setdefault("replan_required", False)
    runtime.setdefault("replan_reason", "")
    runtime.setdefault("last_replanned_at", "")
    runtime.setdefault("last_resume_check_at", "")
    runtime.setdefault("last_resume_repair_at", "")
    runtime.setdefault("resume_repair_count", 0)
    runtime.setdefault("operation_prompt_name", "")
    runtime.setdefault("operation_input_key", "raw_path")
    runtime.setdefault("operation_control", {})
    runtime.setdefault("updated_at", _now_iso())
    _ensure_runtime_control_state(runtime)
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


def _classify_llm_transport_issue(error_or_reason) -> tuple[str, bool]:
    reason = getattr(error_or_reason, "reason", error_or_reason)
    message = str(reason or error_or_reason or "").strip()
    message_lower = message.lower()

    if isinstance(reason, socket.timeout) or isinstance(error_or_reason, (socket.timeout, TimeoutError)):
        return "timeout", True
    if "timed out" in message_lower:
        return "timeout", True

    transient_reason_types = tuple(
        cls for cls in (
            getattr(http.client, "RemoteDisconnected", None),
            getattr(http.client, "IncompleteRead", None),
            getattr(http.client, "BadStatusLine", None),
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
        )
        if isinstance(cls, type)
    )
    transient_tokens = (
        "remote end closed connection without response",
        "remote disconnected",
        "connection reset",
        "connection aborted",
        "broken pipe",
        "incomplete read",
        "bad status line",
        "connection closed",
        "connection lost",
    )
    if transient_reason_types and isinstance(reason, transient_reason_types):
        return "remote_disconnect", True
    if any(token in message_lower for token in transient_tokens):
        return "remote_disconnect", True

    return "network", False


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


def _evaluate_chunk_output_health(prompt_name: str, chunk_id: int, chunk_char_count: int,
                                  result_text: str) -> dict:
    result_char_count = len(result_text)
    ratio = result_char_count / chunk_char_count if chunk_char_count > 0 else 0
    warnings = []
    retry_reasons = []

    if prompt_name != "summarize" and ratio < SHORT_OUTPUT_WARNING_RATIO:
        warnings.append(
            f"⚠️ Chunk {chunk_id}: output is only {ratio:.0%} of input size "
            f"({result_char_count} vs {chunk_char_count} chars). Possible summarization instead of structuring."
        )
        retry_reasons.append("short_output")

    if (
        prompt_name in ("structure_only", "quick_cleanup")
        and "##" not in result_text
        and chunk_char_count > STRUCTURE_HEADER_WARNING_MIN_CHARS
    ):
        warnings.append(
            f"⚠️ Chunk {chunk_id}: no section headers (##) found in output "
            f"({chunk_char_count} chars input). Structuring may have failed."
        )
        retry_reasons.append("missing_headers")

    if prompt_name == "translate_only":
        cn_chars = sum(1 for char in result_text if '一' <= char <= '鿿')
        cn_ratio = cn_chars / result_char_count if result_char_count > 0 else 0
        if cn_ratio < TRANSLATION_WARNING_CN_RATIO:
            warnings.append(
                f"⚠️ Chunk {chunk_id}: Chinese character ratio is only {cn_ratio:.0%}. "
                f"Translation may have been skipped."
            )
            retry_reasons.append("translation_skipped")

    return {
        "warnings": warnings,
        "retry_reasons": list(dict.fromkeys(retry_reasons)),
        "ratio": ratio,
        "result_chars": result_char_count,
    }


def _append_chunk_recovery_log(chunk_info: dict, *, action: str, reasons: list[str],
                               details: list[str], request_attempts: int,
                               request_url: str = "", latency_ms: int | None = None,
                               sleep_sec: float = 0.0) -> None:
    recovery_logs = list(chunk_info.get("recovery_logs", []))
    recovery_logs.append({
        "attempt_index": len(recovery_logs) + 1,
        "action": action,
        "reasons": [str(reason) for reason in reasons if str(reason).strip()],
        "details": [str(detail) for detail in details if str(detail).strip()],
        "request_attempts": max(1, _parse_int(request_attempts, 1)),
        "request_url": str(request_url or ""),
        "latency_ms": latency_ms,
        "sleep_sec": round(max(0.0, _parse_float(sleep_sec, 0.0)), 2),
        "updated_at": _now_iso(),
    })
    chunk_info["recovery_logs"] = recovery_logs
    if action == "retry":
        chunk_info["recovery_attempts"] = max(0, _parse_int(chunk_info.get("recovery_attempts"), 0)) + 1


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


_atomic_write_text = kernel_state.atomic_write_text
_write_manifest = kernel_state.write_manifest


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
        error_type, retryable = _classify_llm_transport_issue(reason)
        raise LLMRequestError(
            f"Cannot reach LLM API: {message}",
            error_type=error_type,
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
        error_type, retryable = _classify_llm_transport_issue(e)
        raise LLMRequestError(
            f"LLM API call failed: {e}",
            error_type=error_type if retryable else "unknown",
            retryable=retryable,
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


def _parse_vtt_timestamp(value: str) -> float | None:
    token = str(value or "").strip().replace(",", ".")
    if not token:
        return None

    parts = token.split(":")
    if len(parts) == 3:
        hours_str, minutes_str, seconds_str = parts
    elif len(parts) == 2:
        hours_str = "0"
        minutes_str, seconds_str = parts
    else:
        return None

    try:
        hours = int(hours_str)
        minutes = int(minutes_str)
        seconds = float(seconds_str)
    except ValueError:
        return None

    return round(hours * 3600 + minutes * 60 + seconds, 3)


def _parse_vtt_time_range(line: str) -> tuple[float | None, float | None]:
    if "-->" not in line:
        return None, None

    start_raw, end_raw = line.split("-->", 1)
    start_token = start_raw.strip().split()[0] if start_raw.strip() else ""
    end_token = end_raw.strip().split()[0] if end_raw.strip() else ""
    return _parse_vtt_timestamp(start_token), _parse_vtt_timestamp(end_token)


def parse_vtt_segments(vtt_path: str, *, language: str = "") -> dict:
    """Parse a VTT file and return aligned segments with timing metadata.

    Output schema matches `chunk-segments` expectations:

    {
      "source": "vtt",
      "language": "en" | "zh" | "..." | "",
      "segments": [{"id": 0, "text": "...", "start_time": 0.0, "end_time": 1.2}, ...]
    }
    """
    path = Path(vtt_path)
    if not path.exists():
        print(f"Error: File does not exist {vtt_path}", file=sys.stderr)
        sys.exit(1)

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    segments = []
    header_language = ""
    in_note = False

    cue_start = None
    cue_end = None
    cue_lines = []

    def flush_cue():
        nonlocal cue_start, cue_end, cue_lines
        if cue_start is None or cue_end is None:
            cue_start = None
            cue_end = None
            cue_lines = []
            return

        joined = " ".join(line.strip() for line in cue_lines if line.strip())
        clean = re.sub(r"<[^>]+>", "", joined)
        clean = " ".join(clean.split()).strip()
        if not clean:
            cue_start = None
            cue_end = None
            cue_lines = []
            return

        if segments and segments[-1]["text"] == clean:
            previous_end = _coerce_float_or_none(segments[-1].get("end_time"))
            if previous_end is None or cue_end > previous_end:
                segments[-1]["end_time"] = cue_end
            cue_start = None
            cue_end = None
            cue_lines = []
            return

        segments.append({
            "id": len(segments),
            "text": clean,
            "start_time": cue_start,
            "end_time": cue_end,
            "speaker": None,
        })
        cue_start = None
        cue_end = None
        cue_lines = []

    for raw_line in content.splitlines():
        line = raw_line.strip("\ufeff")
        stripped = line.strip()

        if stripped.startswith("Language:") and not language:
            header_language = stripped.split(":", 1)[1].strip()

        if in_note:
            if not stripped:
                in_note = False
            continue

        if stripped.startswith("NOTE"):
            in_note = True
            continue

        if stripped.startswith("WEBVTT") or stripped.startswith("Kind:") or stripped.startswith("Style:"):
            continue
        if stripped.startswith("STYLE") or stripped.startswith("REGION"):
            continue

        if "-->" in stripped:
            flush_cue()
            cue_start, cue_end = _parse_vtt_time_range(stripped)
            cue_lines = []
            continue

        if not stripped:
            flush_cue()
            continue

        # Ignore cue identifiers that appear before timestamps.
        if cue_start is None and cue_end is None:
            continue

        cue_lines.append(stripped)

    flush_cue()

    if not segments:
        print("Error: No usable VTT segments found", file=sys.stderr)
        sys.exit(2)

    return {
        "source": "vtt",
        "language": language or header_language,
        "vtt_path": str(path.absolute()),
        "segment_count": len(segments),
        "segments": segments,
    }


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


def _coerce_float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_transcript_text(text: str, *, remove_repeated_phrases: bool = True) -> str:
    transcript = str(text or "")

    # 1. Remove spaces between Chinese characters (multiple passes for thoroughness)
    for _ in range(10):
        transcript = re.sub(r'([一-鿿])\s+([一-鿿])', r'\1\2', transcript)

    # 2. Fix spaces around punctuation
    transcript = re.sub(r'\s+([。，！？、：；])', r'\1', transcript)

    # 3. Remove consecutive repeated phrases (3-20 characters)
    if remove_repeated_phrases:
        transcript = re.sub(r'([一-鿿]{3,20})\1{1,5}', r'\1', transcript)

    return transcript.strip()


def _join_deepgram_words(words: list[dict]) -> str:
    parts = []
    for word in words:
        token = str(word.get('punctuated_word') or word.get('word') or '').strip()
        if token:
            parts.append(token)
    return ' '.join(parts).strip()


def extract_deepgram_segments(data: dict, *, time_offset: float = 0.0,
                              source_chunk_index: int = 0, starting_segment_id: int = 0) -> list[dict]:
    alternative = data['results']['channels'][0]['alternatives'][0]
    paragraphs = alternative.get('paragraphs', {}).get('paragraphs', [])
    words = alternative.get('words', []) if isinstance(alternative.get('words', []), list) else []

    segments = []
    next_segment_id = starting_segment_id

    for para_index, paragraph in enumerate(paragraphs):
        paragraph_speaker = paragraph.get('speaker')
        for sentence_index, sentence in enumerate(paragraph.get('sentences', [])):
            speaker = sentence.get('speaker')
            if speaker is None:
                speaker = paragraph_speaker
            start_time = _coerce_float_or_none(sentence.get('start'))
            end_time = _coerce_float_or_none(sentence.get('end'))

            matched_words = []
            if start_time is not None and end_time is not None and words:
                for word in words:
                    word_start = _coerce_float_or_none(word.get('start'))
                    word_end = _coerce_float_or_none(word.get('end'))
                    if word_start is None or word_end is None:
                        continue
                    if word_end <= start_time - 1e-6 or word_start >= end_time + 1e-6:
                        continue
                    matched_words.append(word)

            sentence_text = _join_deepgram_words(matched_words) or str(sentence.get('text', '')).strip()
            normalized_text = _normalize_transcript_text(sentence_text, remove_repeated_phrases=False)
            if not normalized_text:
                continue

            segments.append({
                'id': next_segment_id,
                'text': normalized_text,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': speaker,
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': para_index,
                'source_sentence_index': sentence_index,
            })
            next_segment_id += 1

    if segments:
        return segments

    transcript = _normalize_transcript_text(alternative.get('transcript', ''), remove_repeated_phrases=False)
    if not transcript:
        return []

    word_starts = [_coerce_float_or_none(word.get('start')) for word in words]
    word_ends = [_coerce_float_or_none(word.get('end')) for word in words]
    start_time = min((value for value in word_starts if value is not None), default=None)
    end_time = max((value for value in word_ends if value is not None), default=None)

    return [{
        'id': starting_segment_id,
        'text': transcript,
        'start_time': None if start_time is None else round(start_time + time_offset, 3),
        'end_time': None if end_time is None else round(end_time + time_offset, 3),
        'speaker': None,
        'source_chunk_index': source_chunk_index,
        'source_paragraph_index': 0,
        'source_sentence_index': 0,
    }]


def process_deepgram_payload(data: dict) -> dict:
    transcript = _normalize_transcript_text(
        data['results']['channels'][0]['alternatives'][0]['transcript']
    )

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


build_command_result_envelope = kernel_runtime.build_command_result_envelope


def runtime_status(work_dir: str) -> dict:
    return kernel_state.summarize_runtime_status(work_dir)


def _kernel_command_registry() -> dict[str, object]:
    return {
        "validate-state": validate_state,
        "normalize-document": normalize_document,
        "plan-optimization": plan_optimization,
        "chunk-text": chunk_text,
        "chunk-segments": chunk_segments,
        "chunk-document": chunk_document,
        "prepare-resume": prepare_resume,
        "replan-remaining": replan_remaining,
        "runtime-status": runtime_status,
        "merge-content": lambda *, work_dir, output_file, header="": merge_content(work_dir, output_file, header_content=header),
        "assemble-final": assemble_final,
        "verify-quality": verify_quality,
    }


def _dispatch_process_chunks_command(*, work_dir: str, prompt_name: str, extra_instruction: str = "",
                                     config_path: str = None, dry_run: bool = False,
                                     input_key: str = "raw_path", force: bool = False,
                                     auto_replan: bool = False, max_replans: int = 3) -> dict:
    if auto_replan and not dry_run:
        return process_chunks_with_replans(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            input_key=input_key,
            force=force,
            max_replans=max_replans,
        )
    return process_chunks(
        work_dir,
        prompt_name,
        extra_instruction=extra_instruction,
        config_path=config_path,
        dry_run=dry_run,
        input_key=input_key,
        force=force,
    )


def run_kernel_command(command: str, **kwargs) -> dict:
    return kernel_runtime.run_registered_kernel_command(
        command,
        kwargs=kwargs,
        registry=_kernel_command_registry(),
        process_chunks_handler=_dispatch_process_chunks_command,
    )

def _prepare_chunking_context(prompt_name: str = "", chunk_size: int = 0,
                              config_path: str = None) -> dict:
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
    return {
        "config": config,
        "prompt_template": prompt_template,
        "budget": budget,
        "chunk_mode": chunk_mode,
        "use_legacy_char_override": use_legacy_char_override,
        "recommended_chunk_size": recommended_chunk_size,
        "effective_chunk_size": effective_chunk_size,
        "hard_cap_size": hard_cap_size,
        "target_tokens": target_tokens,
        "hard_cap_tokens": hard_cap_tokens,
        "autotune_state": autotune_state,
    }


def _chunk_text_payload(text: str, source_file: str, output_dir: str, chunk_size: int = 0,
                        prompt_name: str = "", config_path: str = None, *, driver: str = "chunk-text",
                        source_kind: str = "text", normalized_document_path: str = "",
                        source_adapter: str = "") -> dict:
    chunking = _prepare_chunking_context(prompt_name, chunk_size, config_path)
    config = chunking["config"]
    budget = chunking["budget"]
    chunk_mode = chunking["chunk_mode"]
    use_legacy_char_override = chunking["use_legacy_char_override"]
    recommended_chunk_size = chunking["recommended_chunk_size"]
    effective_chunk_size = chunking["effective_chunk_size"]
    hard_cap_size = chunking["hard_cap_size"]
    target_tokens = chunking["target_tokens"]
    hard_cap_tokens = chunking["hard_cap_tokens"]
    autotune_state = chunking["autotune_state"]

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sentences = _split_sentences(text)
    chunks, warnings = _split_text_into_chunks(
        sentences,
        chunk_mode,
        effective_chunk_size,
        hard_cap_size,
        config,
    )

    plan_id = _new_plan_id()
    continuity_policy = _build_manifest_continuity_policy(config)
    chunk_contract = _build_manifest_chunk_contract(
        source_kind,
        driver=driver,
        normalized_document_path=normalized_document_path,
        source_adapter=source_adapter,
        has_timing=False,
        chapters_enabled=False,
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "total_chunks": len(chunks),
        "chunk_context_tail_sentences": continuity_policy["tail_sentences"],
        "chunk_context_summary_tokens": continuity_policy["summary_token_cap"],
        "source_file": str(Path(source_file).absolute()) if source_file else "",
        "source_adapter": str(source_adapter).strip(),
        "work_dir": str(out_dir.absolute()),
        "normalized_document_path": str(normalized_document_path).strip(),
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
            source_file=str(Path(source_file).absolute()) if source_file else "",
            plan_id=plan_id,
            chunk_contract=chunk_contract,
            continuity_policy=continuity_policy,
        ),
        "runtime": _build_manifest_runtime(plan_id),
        "autotune": autotune_state,
        "plan_history": [],
        "chunks": [],
    }

    for index, chunk_content in enumerate(chunks):
        chunk_filename = f"chunk_{index:03d}.txt"
        chunk_path = out_dir / chunk_filename
        _atomic_write_text(chunk_path, chunk_content)

        chunk_entry = _new_chunk_manifest_entry(
            index,
            chunk_content,
            budget,
            config,
            raw_path=chunk_filename,
            processed_path=f"processed_{index:03d}.md",
            plan_id=plan_id,
            continuity_prev_chunk_id=index - 1 if index > 0 else None,
            chunk_contract=chunk_contract,
            continuity_policy=continuity_policy,
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
        "chunks": [chunk["raw_path"] for chunk in manifest["chunks"]],
        "warnings": warnings,
        "chunk_size": effective_chunk_size,
        "recommended_chunk_size": manifest["recommended_chunk_size"],
        "chunk_mode": chunk_mode,
        "target_tokens": manifest["target_tokens"],
        "hard_cap_tokens": manifest["hard_cap_tokens"],
        "source_kind": chunk_contract["source_kind"],
        "driver": chunk_contract["driver"],
        "normalized_document_path": str(normalized_document_path).strip(),
    }


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

    try:
        text = path.read_text(encoding='utf-8')
    except Exception as error:
        print(f"Error: Cannot read file {error}", file=sys.stderr)
        sys.exit(2)

    return _chunk_text_payload(
        text,
        str(path.absolute()),
        output_dir,
        chunk_size,
        prompt_name,
        config_path,
        driver="chunk-text",
        source_kind="text",
    )


def _load_segment_document(segments_path: str) -> tuple[dict, list[dict]]:
    path = Path(segments_path)
    if not path.exists():
        print(f"Error: File does not exist {segments_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: JSON parsing failed {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    if isinstance(data, dict):
        metadata = {key: value for key, value in data.items() if key != "segments"}
        raw_segments = data.get("segments", [])
    elif isinstance(data, list):
        metadata = {}
        raw_segments = data
    else:
        print("Error: Segment document must be a JSON object or array", file=sys.stderr)
        sys.exit(2)

    normalized_segments = []
    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            continue
        segment_text = str(raw_segment.get("text", "")).strip()
        if not segment_text:
            continue
        normalized_segments.append({
            "id": _parse_int(raw_segment.get("id"), index),
            "text": segment_text,
            "start_time": _coerce_float_or_none(raw_segment.get("start_time")),
            "end_time": _coerce_float_or_none(raw_segment.get("end_time")),
            "speaker": raw_segment.get("speaker"),
        })

    if not normalized_segments:
        print("Error: No usable segments found", file=sys.stderr)
        sys.exit(2)

    return metadata, normalized_segments


def _load_chapters_document(chapters_path: str) -> list[dict]:
    path = Path(chapters_path)
    if not path.exists():
        print(f"Error: File does not exist {chapters_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: JSON parsing failed {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    if isinstance(data, dict):
        chapters = data.get("chapters", [])
    elif isinstance(data, list):
        chapters = data
    else:
        print("Error: Chapters JSON must be an array or an object with 'chapters'", file=sys.stderr)
        sys.exit(2)

    return [chapter for chapter in chapters if isinstance(chapter, dict)]


def _map_chapter_starts_to_segment_break_ids(chapters: list[dict], segments: list[dict]) -> tuple[set[int], list[str]]:
    timed_segments = [
        seg for seg in segments
        if seg.get("start_time") is not None and seg.get("end_time") is not None
    ]
    if not timed_segments:
        return set(), ["No timed segments found; cannot apply chapter-aware chunking"]

    warnings = []

    def map_time(timestamp: float | None) -> tuple[int, str]:
        if timestamp is None:
            return int(timed_segments[0].get("id", 0)), "missing_time"

        for seg in timed_segments:
            start_time = float(seg["start_time"])
            end_time = float(seg["end_time"])
            if start_time <= timestamp < end_time or math.isclose(timestamp, start_time):
                return int(seg.get("id", 0)), "time_contains"
            if timestamp < start_time:
                return int(seg.get("id", 0)), "next_segment"

        return int(timed_segments[-1].get("id", 0)), "after_last_segment"

    break_ids = set()
    for index, chapter in enumerate(chapters):
        start_time = _coerce_float_or_none(chapter.get("start_time"))
        if start_time is None:
            continue

        segment_id, match_strategy = map_time(start_time)
        break_ids.add(segment_id)
        if match_strategy != "time_contains":
            warnings.append(f"Chapter {index} used fallback strategy '{match_strategy}'")

    return break_ids, warnings


def _split_timed_segment(segment: dict, max_size: int, chunk_mode: str,
                         config: dict, warning_index: int) -> tuple[list[dict], list[str]]:
    text = segment["text"]
    segment_len = _estimate_tokens(text, chunk_mode, config)
    if segment_len <= max_size:
        return [segment], []

    parts = _force_split_text(text, max_size, chunk_mode, config)
    warnings = [
        f"Segment {warning_index} exceeds chunk_size ({segment_len} > {max_size}), split into {len(parts)} fixed-width segment(s)"
    ]

    start_time = segment.get("start_time")
    end_time = segment.get("end_time")
    duration = None
    if start_time is not None and end_time is not None:
        duration = max(0.0, end_time - start_time)

    total_chars = sum(len(part) for part in parts) or 1
    offset_chars = 0
    split_segments = []

    for part_index, part in enumerate(parts):
        part_start = start_time
        part_end = end_time
        if duration is not None and start_time is not None:
            part_start = start_time + (offset_chars / total_chars) * duration
            offset_chars += len(part)
            part_end = start_time + (offset_chars / total_chars) * duration

        split_segments.append({
            **segment,
            "text": part,
            "start_time": None if part_start is None else round(part_start, 3),
            "end_time": None if part_end is None else round(part_end, 3),
            "segment_part_index": part_index,
        })

    return split_segments, warnings


def _load_normalized_document(normalized_document_path: str) -> dict:
    path = Path(normalized_document_path)
    if not path.exists():
        print(f"Error: File does not exist {normalized_document_path}", file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"Error: JSON parsing failed {error}", file=sys.stderr)
        sys.exit(2)
    except Exception as error:
        print(f"Error: Cannot read file {error}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(payload, dict):
        print("Error: Normalized document must be a JSON object", file=sys.stderr)
        sys.exit(2)

    content = payload.get("content", {}) if isinstance(payload.get("content", {}), dict) else {}
    payload.setdefault("content", content)
    payload["content"]["text"] = _normalize_text_body(content.get("text", ""))

    normalized_segments = []
    for index, raw_segment in enumerate(payload.get("segments", [])):
        if not isinstance(raw_segment, dict):
            continue
        segment_text = " ".join(_normalize_text_body(raw_segment.get("text", "")).split())
        if not segment_text:
            continue
        normalized_segments.append({
            "id": _parse_int(raw_segment.get("id"), index),
            "text": segment_text,
            "start_time": _coerce_float_or_none(raw_segment.get("start_time")),
            "end_time": _coerce_float_or_none(raw_segment.get("end_time")),
            "speaker": raw_segment.get("speaker"),
            "segment_part_index": raw_segment.get("segment_part_index"),
        })
    payload["segments"] = normalized_segments

    if not payload["segments"] and not str(payload.get("content", {}).get("text", "")).strip():
        print("Error: Normalized document does not contain usable text or segments", file=sys.stderr)
        sys.exit(2)

    return payload


def _chunk_segments_payload(metadata: dict, segments: list[dict], source_file: str, output_dir: str,
                            chunk_size: int = 0, prompt_name: str = "", config_path: str = None,
                            *, chapters_path: str = "", driver: str = "chunk-segments",
                            source_kind: str = "segments", normalized_document_path: str = "",
                            source_adapter: str = "", source_segments_file: str = "") -> dict:
    if not segments:
        print("Error: No usable segments found", file=sys.stderr)
        sys.exit(2)

    chunking = _prepare_chunking_context(prompt_name, chunk_size, config_path)
    config = chunking["config"]
    budget = chunking["budget"]
    chunk_mode = chunking["chunk_mode"]
    use_legacy_char_override = chunking["use_legacy_char_override"]
    recommended_chunk_size = chunking["recommended_chunk_size"]
    effective_chunk_size = chunking["effective_chunk_size"]
    hard_cap_size = chunking["hard_cap_size"]
    target_tokens = chunking["target_tokens"]
    hard_cap_tokens = chunking["hard_cap_tokens"]
    autotune_state = chunking["autotune_state"]

    prepared_segments = []
    warnings = []
    for index, segment in enumerate(segments):
        split_segments, split_warnings = _split_timed_segment(segment, hard_cap_size, chunk_mode, config, index)
        prepared_segments.extend(split_segments)
        warnings.extend(split_warnings)

    chapter_break_ids: set[int] = set()
    if chapters_path:
        chapters = _load_chapters_document(chapters_path)
        chapter_break_ids, chapter_warnings = _map_chapter_starts_to_segment_break_ids(chapters, segments)
        warnings.extend(chapter_warnings)

    chunk_specs = []
    current_parts = []
    current_size = 0
    separator_size = len(CHUNK_SEPARATOR) if chunk_mode == "chars" else _estimate_tokens(CHUNK_SEPARATOR, "tokens", config)

    def finalize_chunk(parts: list[dict]):
        if not parts:
            return
        content = CHUNK_SEPARATOR.join(part["text"] for part in parts if part.get("text"))
        start_time = next((part.get("start_time") for part in parts if part.get("start_time") is not None), None)
        end_time = next((part.get("end_time") for part in reversed(parts) if part.get("end_time") is not None), None)
        segment_ids = [part.get("id") for part in parts if part.get("id") is not None]
        chunk_specs.append({
            "content": content,
            "start_time": start_time,
            "end_time": end_time,
            "duration_sec": None if start_time is None or end_time is None else round(max(0.0, end_time - start_time), 3),
            "source_segment_start": segment_ids[0] if segment_ids else None,
            "source_segment_end": segment_ids[-1] if segment_ids else None,
            "source_segments_count": len({seg_id for seg_id in segment_ids}),
        })

    for part in prepared_segments:
        part_len = _estimate_tokens(part["text"], chunk_mode, config)

        part_id = part.get("id")
        part_index = part.get("segment_part_index")
        is_first_part = part_index is None or int(part_index) == 0
        if current_parts and is_first_part and part_id is not None and int(part_id) in chapter_break_ids:
            finalize_chunk(current_parts)
            current_parts = [part]
            current_size = part_len
            continue

        candidate_size = current_size + part_len + (separator_size if current_parts else 0)
        if current_parts and (candidate_size > effective_chunk_size or candidate_size > hard_cap_size):
            finalize_chunk(current_parts)
            current_parts = [part]
            current_size = part_len
        else:
            current_parts.append(part)
            current_size = candidate_size

    if current_parts:
        finalize_chunk(current_parts)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_id = _new_plan_id()
    continuity_policy = _build_manifest_continuity_policy(config)
    has_timing = any(segment.get("start_time") is not None or segment.get("end_time") is not None for segment in segments)
    chunk_contract = _build_manifest_chunk_contract(
        source_kind,
        driver=driver,
        normalized_document_path=normalized_document_path,
        source_adapter=source_adapter,
        has_timing=has_timing,
        chapters_enabled=bool(chapters_path),
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "total_chunks": len(chunk_specs),
        "chunk_context_tail_sentences": continuity_policy["tail_sentences"],
        "chunk_context_summary_tokens": continuity_policy["summary_token_cap"],
        "source_file": str(Path(source_file).absolute()) if source_file else "",
        "source_segments_file": str(Path(source_segments_file or source_file).absolute()) if (source_segments_file or source_file) else "",
        "source_segments_count": len(segments),
        "source_kind": str(metadata.get("source", "")).strip(),
        "source_adapter": str(source_adapter or metadata.get("source", "")).strip(),
        "work_dir": str(out_dir.absolute()),
        "normalized_document_path": str(normalized_document_path).strip(),
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
            source_file=str(Path(source_file).absolute()) if source_file else "",
            plan_id=plan_id,
            chunk_contract=chunk_contract,
            continuity_policy=continuity_policy,
        ),
        "runtime": _build_manifest_runtime(plan_id),
        "autotune": autotune_state,
        "plan_history": [],
        "chunks": [],
    }

    for index, chunk_spec in enumerate(chunk_specs):
        chunk_filename = f"chunk_{index:03d}.txt"
        chunk_path = out_dir / chunk_filename
        _atomic_write_text(chunk_path, chunk_spec["content"])

        chunk_entry = _new_chunk_manifest_entry(
            index,
            chunk_spec["content"],
            budget,
            config,
            raw_path=chunk_filename,
            processed_path=f"processed_{index:03d}.md",
            plan_id=plan_id,
            continuity_prev_chunk_id=index - 1 if index > 0 else None,
            chunk_contract=chunk_contract,
            continuity_policy=continuity_policy,
        )
        chunk_entry["autotune_target_tokens"] = autotune_state["current_target_tokens"]
        chunk_entry["autotune_next_target_tokens"] = autotune_state["current_target_tokens"]
        chunk_entry["start_time"] = chunk_spec["start_time"]
        chunk_entry["end_time"] = chunk_spec["end_time"]
        chunk_entry["duration_sec"] = chunk_spec["duration_sec"]
        chunk_entry["source_segment_start"] = chunk_spec["source_segment_start"]
        chunk_entry["source_segment_end"] = chunk_spec["source_segment_end"]
        chunk_entry["source_segments_count"] = chunk_spec["source_segments_count"]
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
        "total_chunks": len(chunk_specs),
        "manifest_path": str(manifest_path),
        "plan_id": plan_id,
        "chunks": [chunk["raw_path"] for chunk in manifest["chunks"]],
        "warnings": warnings,
        "chunk_size": effective_chunk_size,
        "recommended_chunk_size": manifest["recommended_chunk_size"],
        "chunk_mode": chunk_mode,
        "target_tokens": manifest["target_tokens"],
        "hard_cap_tokens": manifest["hard_cap_tokens"],
        "source_segments_count": len(segments),
        "source_kind": chunk_contract["source_kind"],
        "driver": chunk_contract["driver"],
        "normalized_document_path": str(normalized_document_path).strip(),
    }


def chunk_segments(segments_path: str, output_dir: str, chunk_size: int = 0,
                   prompt_name: str = "", config_path: str = None,
                   chapters_path: str = "") -> dict:
    """Split aligned source segments into timed chunks."""
    metadata, segments = _load_segment_document(segments_path)
    return _chunk_segments_payload(
        metadata,
        segments,
        str(Path(segments_path).absolute()),
        output_dir,
        chunk_size,
        prompt_name,
        config_path,
        chapters_path=chapters_path,
        driver="chunk-segments",
        source_kind="segments",
        source_adapter=str(metadata.get("source", "")).strip(),
        source_segments_file=str(Path(segments_path).absolute()),
    )


def chunk_document(normalized_document_path: str, output_dir: str, chunk_size: int = 0,
                   prompt_name: str = "", config_path: str = None,
                   chapters_path: str = "", prefer: str = "auto") -> dict:
    """Chunk a normalized document using its preferred canonical source shape."""
    payload = _load_normalized_document(normalized_document_path)
    preference = str(prefer or "auto").strip().lower()
    if preference not in {"auto", "segments", "text"}:
        print(f"Error: Unsupported chunk-document preference: {prefer}", file=sys.stderr)
        sys.exit(2)

    content = payload.get("content", {}) if isinstance(payload.get("content", {}), dict) else {}
    preferred_source = str(content.get("preferred_chunk_source", "")).strip().lower()
    segments = payload.get("segments", []) if isinstance(payload.get("segments", []), list) else []
    has_segments = bool(segments)
    source_adapter = str(payload.get("source_adapter", "")).strip()
    artifacts = payload.get("artifacts", {}) if isinstance(payload.get("artifacts", {}), dict) else {}
    normalized_path = str(Path(normalized_document_path).absolute())

    if preference == "segments":
        use_segments = has_segments
        if not use_segments:
            print("Error: Normalized document does not contain segments", file=sys.stderr)
            sys.exit(2)
    elif preference == "text":
        use_segments = False
    else:
        use_segments = preferred_source == "segments" and has_segments

    if use_segments:
        metadata = {
            "source": source_adapter,
            "document_id": payload.get("document_id", ""),
        }
        result = _chunk_segments_payload(
            metadata,
            segments,
            normalized_path,
            output_dir,
            chunk_size,
            prompt_name,
            config_path,
            chapters_path=chapters_path,
            driver="chunk-document",
            source_kind="segments",
            normalized_document_path=normalized_path,
            source_adapter=source_adapter,
            source_segments_file=str(artifacts.get("segments_path", "") or normalized_path),
        )
    else:
        text_body = str(content.get("text", "")).strip()
        if not text_body:
            print("Error: Normalized document does not contain usable text", file=sys.stderr)
            sys.exit(2)
        result = _chunk_text_payload(
            text_body,
            normalized_path,
            output_dir,
            chunk_size,
            prompt_name,
            config_path,
            driver="chunk-document",
            source_kind="text",
            normalized_document_path=normalized_path,
            source_adapter=source_adapter,
        )

    result["preferred_source_kind"] = preferred_source or ("segments" if has_segments else "text")
    return result


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


def _resolve_manifest_path(manifest_ref: str) -> Path:
    path = Path(manifest_ref)
    if path.is_dir():
        return path / "manifest.json"
    return path


def build_chapter_plan(chapters_path: str, manifest_ref: str, output_path: str = "") -> dict:
    chapters_file = Path(chapters_path)
    if not chapters_file.exists():
        print(f"Error: File does not exist {chapters_path}", file=sys.stderr)
        sys.exit(1)

    manifest_path = _resolve_manifest_path(manifest_ref)
    if not manifest_path.exists():
        print(f"Error: manifest.json not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        chapters_data = json.loads(chapters_file.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: JSON parsing failed {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    if isinstance(chapters_data, dict):
        chapters = chapters_data.get("chapters", [])
    elif isinstance(chapters_data, list):
        chapters = chapters_data
    else:
        print("Error: Chapters JSON must be an array or an object with 'chapters'", file=sys.stderr)
        sys.exit(2)

    timed_chunks = []
    missing_timing = 0
    for chunk in manifest.get("chunks", []):
        status = str(chunk.get("status", "")).strip().lower()
        if status == SUPERSEDED_CHUNK_STATUS:
            continue
        start_time = _coerce_float_or_none(chunk.get("start_time"))
        end_time = _coerce_float_or_none(chunk.get("end_time"))
        if start_time is None or end_time is None:
            missing_timing += 1
            continue
        timed_chunks.append({**chunk, "start_time": start_time, "end_time": end_time})

    if not timed_chunks:
        print("Error: Manifest does not contain timed chunks; run chunk-segments first", file=sys.stderr)
        sys.exit(2)

    timed_chunks.sort(key=lambda item: int(item.get("id", 0)))

    def map_time(timestamp: float | None) -> tuple[dict, str, str]:
        if timestamp is None:
            return timed_chunks[0], "missing_time", "low"
        for chunk in timed_chunks:
            start_time = chunk["start_time"]
            end_time = chunk["end_time"]
            if start_time <= timestamp < end_time or math.isclose(timestamp, start_time):
                return chunk, "time_contains", "high"
            if timestamp < start_time:
                return chunk, "next_chunk", "medium"
        return timed_chunks[-1], "after_last_chunk", "low"

    def is_untitled(title: str) -> bool:
        if not title:
            return True
        lowered = title.strip().lower()
        return lowered.startswith("<untitled") or "untitled chapter" in lowered

    warnings = []
    plan_entries = []

    if missing_timing:
        warnings.append(f"Skipped {missing_timing} chunks without timing metadata")

    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue

        start_time = _coerce_float_or_none(chapter.get("start_time"))
        end_time = _coerce_float_or_none(chapter.get("end_time"))
        start_chunk, match_strategy, confidence = map_time(start_time)
        end_chunk = start_chunk
        if end_time is not None:
            end_probe = max(start_time or end_time, end_time - 1e-6)
            end_chunk, _, _ = map_time(end_probe)

        title = str(chapter.get("title", "")).strip()
        if title and is_untitled(title):
            title = ""
        title_en = str(chapter.get("title_en", "")).strip()
        title_zh = str(chapter.get("title_zh", "")).strip()

        if not title_zh and not title_en and title and not is_untitled(title):
            title_zh = title

        if match_strategy != "time_contains":
            warnings.append(f"Chapter {index} used fallback strategy '{match_strategy}'")

        plan_entries.append({
            "chapter_index": index,
            "title": title,
            "title_en": title_en,
            "title_zh": title_zh,
            "start_time": start_time,
            "end_time": end_time,
            "start_chunk": int(start_chunk.get("id", 0)),
            "end_chunk": int(end_chunk.get("id", start_chunk.get("id", 0))),
            "anchor_segment_id": start_chunk.get("source_segment_start"),
            "match_strategy": match_strategy,
            "confidence": confidence,
        })

    output_file = str(output_path or (manifest_path.parent / "chapter_plan.json"))
    Path(output_file).write_text(json.dumps(plan_entries, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "success": True,
        "output_file": output_file,
        "chapter_count": len(chapters),
        "mapped_count": len(plan_entries),
        "warnings": warnings,
    }


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
    chapter_starts = {}  # {chunk_id: [{"title": ..., "title_en": ..., "title_zh": ...}, ...]}
    if chapter_plan_path.exists():
        try:
            chapter_plan = json.loads(chapter_plan_path.read_text(encoding='utf-8'))
            if isinstance(chapter_plan, list):
                for chapter in chapter_plan:
                    if not isinstance(chapter, dict):
                        continue

                    start_chunk = chapter.get("start_chunk")
                    if start_chunk is None:
                        continue

                    try:
                        start_chunk_int = int(start_chunk)
                    except (ValueError, TypeError):
                        print(f"Warning: Invalid start_chunk value: {start_chunk}", file=sys.stderr)
                        continue

                    chapter_starts.setdefault(start_chunk_int, []).append({
                        "title": str(chapter.get("title", "")),
                        "title_en": str(chapter.get("title_en", "")),
                        "title_zh": str(chapter.get("title_zh", "")),
                    })
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
            for chapter in chapter_starts[chunk_id]:
                title = str(chapter.get("title", "")).strip()
                title_en = str(chapter.get("title_en", "")).strip()
                title_zh = str(chapter.get("title_zh", "")).strip()

                headings = []
                if title_en:
                    headings.append(title_en)
                if title_zh and title_zh not in headings:
                    headings.append(title_zh)
                if not headings and title:
                    headings.append(title)

                if not headings:
                    continue

                output_lines.append("\n")
                for heading in headings:
                    output_lines.append(f"## {heading}\n")
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
                   input_key: str = "raw_path", force: bool = False,
                   runtime_ownership: dict | None = None) -> dict:
    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    ownership, owned_here = _resolve_runtime_mutation_ownership(
        work_dir,
        "process-chunks",
        runtime_ownership=runtime_ownership,
    )
    if owned_here and not ownership.get("success", False):
        return _build_process_ownership_conflict_result(ownership)

    release_result = None
    try:
        result = _process_chunks_impl(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=dry_run,
            input_key=input_key,
            force=force,
        )
    finally:
        if owned_here and ownership.get("success", False):
            release_result = kernel_runtime.release_runtime_ownership(work_dir, ownership.get("owner_id", ""))
    return _finalize_mutation_result(result, ownership, release_result)


def _process_chunks_impl(work_dir: str, prompt_name: str, extra_instruction: str = "",
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
    chunk_recovery_attempt_limit = _parse_int_min(
        config.get("llm_chunk_recovery_attempts"),
        DEFAULT_LLM_CHUNK_RECOVERY_ATTEMPTS,
        0,
    )
    chunk_recovery_backoff_sec = _parse_float_min(
        config.get("llm_chunk_recovery_backoff_sec"),
        DEFAULT_LLM_CHUNK_RECOVERY_BACKOFF_SEC,
        0.0,
    )
    operation_control = _build_operation_control_contract(
        "chunk",
        prompt_name,
        input_key=input_key,
        config=config,
        bilingual=prompt_name == "translate_only",
    )

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
        config=config,
    )
    plan = manifest["plan"]
    runtime = manifest["runtime"]
    plan_continuity = plan.get("continuity", {}) if isinstance(plan.get("continuity", {}), dict) else _build_manifest_continuity_policy(config)
    plan_chunk_contract = plan.get("chunk_contract", {}) if isinstance(plan.get("chunk_contract", {}), dict) else _build_manifest_chunk_contract()
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

    _ensure_chunk_runtime_defaults(
        manifest,
        runtime,
        plan,
        prompt_budget,
        request_url,
        planned_max_output_tokens,
        autotune_state,
    )
    resume_report = _prepare_manifest_for_resume(manifest, work_path, prompt_name)
    resume_summary = _format_resume_report(resume_report)
    if resume_summary:
        setup_warnings.append(resume_summary)
        print(f"ℹ️ {resume_summary}", file=sys.stderr)

    runtime["active_plan_id"] = plan.get("plan_id", runtime.get("active_plan_id", _new_plan_id()))
    runtime["last_request_url"] = request_url
    runtime["operation_prompt_name"] = prompt_name
    runtime["operation_input_key"] = _normalize_operation_input_key(input_key)
    runtime["operation_control"] = operation_control
    runtime_control = _ensure_runtime_control_state(runtime)
    runtime_control["verification_warning_count"] = 0
    runtime_control["repair_attempted_count"] = 0
    runtime_control["repair_exhausted_count"] = 0
    runtime_control["last_replan_trigger"] = ""
    runtime_control["last_replan_action"] = operation_control.get("replan", {}).get("on_replan_required", "not_applicable")
    runtime_control["last_replan_chunk_id"] = None
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
    if resume_report.get("repaired", False) and not dry_run:
        _write_manifest(manifest_path, manifest)

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
            "resume": resume_report,
            "control": _build_process_control_summary(runtime, operation_control),
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
        continuity_context = _build_continuity_context(
            previous_chunk,
            work_path,
            config,
            input_key=input_key,
            continuity_policy=plan_continuity,
        )
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

        chunk_attempt_logs = []
        while True:
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
                request_attempts = max(len(attempt_logs), _parse_int(llm_result.get("attempts"), 1))
                chunk_attempt_logs.extend(attempt_logs)
                chunk_info["attempts"] = chunk_info.get("attempts", 0) + request_attempts
                chunk_info["attempt_logs"] = list(chunk_info.get("attempt_logs", [])) + attempt_logs

                result_text = llm_result["text"]
                actual_output_tokens = _estimate_tokens(result_text, "tokens", config)
                output_health = _evaluate_chunk_output_health(prompt_name, chunk_id, chunk_char_count, result_text)
                consecutive_timeouts = 0

                if output_health["retry_reasons"] and chunk_info.get("recovery_attempts", 0) < chunk_recovery_attempt_limit:
                    _append_chunk_recovery_log(
                        chunk_info,
                        action="retry",
                        reasons=output_health["retry_reasons"],
                        details=output_health["warnings"],
                        request_attempts=request_attempts,
                        request_url=llm_result.get("request_url", request_url),
                        latency_ms=llm_result.get("latency_ms"),
                        sleep_sec=chunk_recovery_backoff_sec,
                    )
                    runtime_control["repair_attempted_count"] = runtime_control.get("repair_attempted_count", 0) + 1
                    _record_chunk_verification(
                        chunk_info,
                        status="repairable_failure",
                        warnings=output_health["warnings"],
                        retry_reasons=output_health["retry_reasons"],
                        repair_exhausted=False,
                    )
                    chunk_info["status"] = "pending"
                    chunk_info["last_error"] = " | ".join(output_health["warnings"])
                    chunk_info["last_error_type"] = "quality_retry"
                    chunk_info["error_type"] = "quality_retry"
                    chunk_info["request_url"] = llm_result.get("request_url", request_url)
                    chunk_info["latency_ms"] = llm_result.get("latency_ms")
                    chunk_info["streaming_used"] = bool(llm_result.get("streaming_used", False))
                    chunk_info["updated_at"] = _now_iso()
                    print(
                        f"Retrying chunk {active_index}/{total} chunk_id={chunk_id} after suspicious output "
                        f"reasons={','.join(output_health['retry_reasons'])} recovery_attempt={chunk_info['recovery_attempts']}/{chunk_recovery_attempt_limit} "
                        f"latency_ms={llm_result.get('latency_ms')} url={llm_result.get('request_url', request_url)}",
                        file=sys.stderr,
                    )
                    _refresh_manifest_token_source_summary(manifest)
                    _sync_manifest_legacy_fields(manifest)
                    _write_manifest(manifest_path, manifest)
                    if chunk_recovery_backoff_sec > 0:
                        time.sleep(chunk_recovery_backoff_sec)
                    continue

                repair_exhausted = bool(output_health["retry_reasons"]) and chunk_info.get("recovery_attempts", 0) >= chunk_recovery_attempt_limit
                if output_health["warnings"]:
                    runtime_control["verification_warning_count"] = runtime_control.get("verification_warning_count", 0) + len(output_health["warnings"])
                if repair_exhausted:
                    runtime_control["repair_exhausted_count"] = runtime_control.get("repair_exhausted_count", 0) + 1
                _record_chunk_verification(
                    chunk_info,
                    status="warning" if output_health["warnings"] else "passed",
                    warnings=output_health["warnings"],
                    retry_reasons=output_health["retry_reasons"],
                    repair_exhausted=repair_exhausted,
                )
                for warning in output_health["warnings"]:
                    warnings.append(warning)
                    print(warning, file=sys.stderr)

                result_char_count = output_health["result_chars"]
                _atomic_write_text(out_path, result_text)
                output_files.append(str(out_path))
                processed_count += 1

                chunk_info["status"] = "done"
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
                had_timeout_retry = _has_timeout_attempt(chunk_attempt_logs)
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
                    f"attempts={chunk_info['attempts']} streaming_used={llm_result['streaming_used']} "
                    f"output_chars={result_char_count} actual_output_tokens={actual_output_tokens} "
                    f"recovery_attempts={chunk_info.get('recovery_attempts', 0)} "
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
                    _mark_runtime_replan(
                        runtime,
                        reason=autotune_state["last_reason"] or "Observed unstable retries under the current plan",
                        trigger=_classify_replan_trigger(
                            had_timeout_retry=had_timeout_retry,
                            autotune_last_event=autotune_state["last_event"],
                        ),
                        input_key=input_key,
                        chunk_id=chunk_id,
                    )
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
                request_attempts = max(len(attempt_logs), _parse_int(getattr(error, "attempts", 1), 1))
                chunk_attempt_logs.extend(attempt_logs)
                failed_count += 1
                chunk_info["status"] = "failed"
                chunk_info["attempts"] = chunk_info.get("attempts", 0) + request_attempts
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
                    _mark_runtime_replan(
                        runtime,
                        reason=autotune_state["last_reason"] or str(error),
                        trigger=_classify_replan_trigger(error, autotune_last_event=autotune_state["last_event"]),
                        input_key=input_key,
                        chunk_id=chunk_id,
                    )
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
                break
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
        "resume": resume_report,
        "plan": manifest.get("plan", {}),
        "control": _build_process_control_summary(runtime, operation_control),
        "request_url": request_url,
    }


def replan_remaining(work_dir: str, prompt_name: str = "", config_path: str = None,
                     chunk_size: int = 0, input_key: str = "raw_path",
                     runtime_ownership: dict | None = None) -> dict:
    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    ownership, owned_here = _resolve_runtime_mutation_ownership(
        work_dir,
        "replan-remaining",
        runtime_ownership=runtime_ownership,
    )
    if owned_here and not ownership.get("success", False):
        return _build_replan_ownership_conflict_result(ownership)

    release_result = None
    try:
        result = _replan_remaining_impl(
            work_dir,
            prompt_name=prompt_name,
            config_path=config_path,
            chunk_size=chunk_size,
            input_key=input_key,
        )
    finally:
        if owned_here and ownership.get("success", False):
            release_result = kernel_runtime.release_runtime_ownership(work_dir, ownership.get("owner_id", ""))
    return _finalize_mutation_result(result, ownership, release_result)


def _replan_remaining_impl(work_dir: str, prompt_name: str = "", config_path: str = None,
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
        config=config,
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
    plan_chunk_contract = manifest.get("plan", {}).get("chunk_contract", {}) if isinstance(manifest.get("plan", {}).get("chunk_contract", {}), dict) else _build_manifest_chunk_contract()
    plan_continuity = manifest.get("plan", {}).get("continuity", {}) if isinstance(manifest.get("plan", {}).get("continuity", {}), dict) else _build_manifest_continuity_policy(config)
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
            chunk_contract=plan_chunk_contract,
            continuity_policy=plan_continuity,
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
                                force: bool = False, max_replans: int = 3,
                                runtime_ownership: dict | None = None) -> dict:
    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    ownership, owned_here = _resolve_runtime_mutation_ownership(
        work_dir,
        "process-chunks-with-replans",
        runtime_ownership=runtime_ownership,
    )
    if owned_here and not ownership.get("success", False):
        return _build_process_with_replans_ownership_conflict_result(ownership)

    release_result = None
    try:
        result = _process_chunks_with_replans_impl(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            input_key=input_key,
            force=force,
            max_replans=max_replans,
            runtime_ownership=ownership,
        )
    finally:
        if owned_here and ownership.get("success", False):
            release_result = kernel_runtime.release_runtime_ownership(work_dir, ownership.get("owner_id", ""))
    return _finalize_mutation_result(result, ownership, release_result)


def _process_chunks_with_replans_impl(work_dir: str, prompt_name: str, extra_instruction: str = "",
                                      config_path: str = None, input_key: str = "raw_path",
                                      force: bool = False, max_replans: int = 3,
                                      runtime_ownership: dict | None = None) -> dict:
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

    delegated_runtime_ownership = runtime_ownership
    if isinstance(runtime_ownership, dict) and str(runtime_ownership.get("owner_id", "")).strip():
        delegated_runtime_ownership = dict(runtime_ownership)
        delegated_runtime_ownership["delegated"] = True

    if input_key != "raw_path":
        result = process_chunks(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=False,
            input_key=input_key,
            force=force,
            runtime_ownership=delegated_runtime_ownership,
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
        "control": {},
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
            runtime_ownership=delegated_runtime_ownership,
        )
        aggregate["processed_count"] += last_result.get("processed_count", 0)
        aggregate["failed_count"] += last_result.get("failed_count", 0)
        aggregate["skipped_count"] += last_result.get("skipped_count", 0)
        aggregate["warnings"].extend(last_result.get("warnings", []))
        aggregate["output_files"].extend(last_result.get("output_files", []))
        aggregate["request_url"] = last_result.get("request_url", aggregate["request_url"])
        aggregate["control"] = last_result.get("control", aggregate.get("control", {}))
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
            if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
                aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
                aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
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
            if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
                aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
                aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
            aggregate["warning_count"] = len(aggregate["warnings"])
            aggregate["superseded_count"] = current_superseded_count()
            return aggregate

        replan_result = replan_remaining(
            work_dir,
            prompt_name=prompt_name,
            config_path=config_path,
            input_key=input_key,
            runtime_ownership=delegated_runtime_ownership,
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
            if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
                aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
                aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
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
    if isinstance(aggregate.get("control"), dict) and isinstance(aggregate["control"].get("replan"), dict):
        aggregate["control"]["replan"]["auto_replan_count"] = aggregate["replan_count"]
        aggregate["control"]["replan"]["max_auto_replans"] = max(0, max_replans)
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
                        output_json: str = "", output_text: str = "",
                        output_segments: str = "") -> dict:
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
    split_points = [float(point) for point in split_result.get("split_points", [])]

    chunk_offsets = [0.0] + split_points
    if len(chunk_offsets) < len(chunk_paths):
        chunk_offsets.extend([chunk_offsets[-1] if chunk_offsets else 0.0] * (len(chunk_paths) - len(chunk_offsets)))

    transcripts = []
    speaker_count = 1
    json_outputs = []
    raw_payloads = []
    segments = []

    for idx, chunk_path in enumerate(chunk_paths):
        payload = _call_deepgram_api(chunk_path, api_key=api_key, language=language, timeout=timeout)
        raw_payloads.append(payload)
        processed = process_deepgram_payload(payload)
        transcripts.append(processed["transcript"])
        speaker_count = max(speaker_count, processed["speaker_count"])

        if output_segments:
            segments.extend(
                extract_deepgram_segments(
                    payload,
                    time_offset=chunk_offsets[idx],
                    source_chunk_index=idx,
                    starting_segment_id=len(segments),
                )
            )

        if output_json:
            output_base = Path(output_json)
            if len(chunk_paths) == 1:
                json_path = output_base
            else:
                json_path = output_base.with_name(f"{output_base.stem}_chunk_{idx:03d}{output_base.suffix or '.json'}")
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            json_outputs.append(str(json_path))

    transcript = "\n\n".join(t for t in transcripts if t).strip()
    used_split_mode = len(chunk_paths) > 1

    if output_json and used_split_mode:
        output_base = Path(output_json)
        output_base.write_text(
            json.dumps(
                {
                    "chunk_count": len(chunk_paths),
                    "split_points": split_points,
                    "chunks": [
                        {
                            "index": idx,
                            "chunk_path": str(chunk_path),
                            "payload": payload,
                        }
                        for idx, (chunk_path, payload) in enumerate(zip(chunk_paths, raw_payloads))
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if output_text:
        Path(output_text).write_text(transcript, encoding="utf-8")

    if output_segments:
        Path(output_segments).write_text(
            json.dumps(
                {
                    "source": "deepgram",
                    "language": language,
                    "used_split_mode": used_split_mode,
                    "chunk_count": len(chunk_paths),
                    "split_points": split_points,
                    "segments": segments,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return {
        "transcript": transcript,
        "speaker_count": speaker_count,
        "chunk_count": len(chunk_paths),
        "json_outputs": json_outputs,
        "split_points": split_points,
        "used_split_mode": used_split_mode,
        "segment_count": len(segments),
        "segments_output": output_segments,
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

MACHINE_STATE_SCHEMA_VERSION = 1
MACHINE_STATE_FORMAT = "yt_transcript.machine_state/v1"
NORMALIZED_DOCUMENT_SCHEMA_VERSION = 1
NORMALIZED_DOCUMENT_FORMAT = "yt_transcript.normalized_document/v1"
LEGACY_STATE_MACHINE_SUFFIX = "_machine_state.json"


def _derive_machine_state_path(state_path: str | Path) -> Path:
    path = Path(state_path)
    if path.suffix.lower() == ".json":
        return path
    if path.name.endswith("_state.md"):
        return path.with_name(path.name[:-len("_state.md")] + LEGACY_STATE_MACHINE_SUFFIX)
    stem = path.stem
    if stem.endswith("_state"):
        stem = stem[:-len("_state")]
    return path.with_name(f"{stem}{LEGACY_STATE_MACHINE_SUFFIX}")


def _derive_legacy_state_path(machine_state_path: str | Path) -> Path:
    path = Path(machine_state_path)
    if path.suffix.lower() == ".md":
        return path
    if path.name.endswith(LEGACY_STATE_MACHINE_SUFFIX):
        return path.with_name(path.name[:-len(LEGACY_STATE_MACHINE_SUFFIX)] + "_state.md")
    return path.with_suffix(".md")


def _default_normalized_document_path(document_id: str) -> str:
    token = str(document_id or "").strip() or "unknown"
    return f"/tmp/{token}_normalized_document.json"


def _parse_legacy_state_content(content: str) -> dict:
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


def _read_legacy_state_file(state_path: str | Path) -> tuple[dict, str]:
    path = Path(state_path)
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error: Cannot read state file: {e}", file=sys.stderr)
        sys.exit(2)
    return _parse_legacy_state_content(content), content


def _compat_fields_to_machine_state(compat_fields: dict, legacy_state_path: Path,
                                 machine_state_path: Path, existing_payload: dict | None = None) -> dict:
    compat = {str(key).strip(): str(value).strip() for key, value in (compat_fields or {}).items() if str(key).strip()}
    content = "\n".join(f"{key}: {value}" for key, value in sorted(compat.items()))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing_payload = existing_payload if isinstance(existing_payload, dict) else {}
    existing_artifacts = existing_payload.get("artifacts", {}) if isinstance(existing_payload.get("artifacts", {}), dict) else {}
    existing_normalization = existing_payload.get("normalization", {}) if isinstance(existing_payload.get("normalization", {}), dict) else {}

    source = {
        "type": compat.get("src", ""),
        "url": compat.get("url", ""),
        "title": compat.get("title", ""),
        "channel": compat.get("channel", ""),
        "upload_date": compat.get("upload_date", ""),
        "duration": compat.get("duration", ""),
        "source_language": compat.get("source_language", ""),
        "subtitle_source": compat.get("subtitle_source", ""),
    }
    artifacts = {
        "output_dir": compat.get("output_dir", existing_artifacts.get("output_dir", "")),
        "work_dir": compat.get("work_dir", existing_artifacts.get("work_dir", "")),
        "output_file": compat.get("output_file", existing_artifacts.get("output_file", "")),
        "raw_text": compat.get("raw_text", existing_artifacts.get("raw_text", "")),
        "structured_text": compat.get("structured_text", existing_artifacts.get("structured_text", "")),
        "optimized_text": compat.get("optimized_text", existing_artifacts.get("optimized_text", "")),
        "segments_path": compat.get("segments_path", existing_artifacts.get("segments_path", "")),
        "normalized_document": compat.get("normalized_document", existing_artifacts.get("normalized_document", "")),
    }
    workflow = {
        "mode": compat.get("mode", ""),
        "step": compat.get("step", ""),
        "last_action": compat.get("last_action", ""),
        "chunk": compat.get("chunk", ""),
        "total": compat.get("total", ""),
    }
    return {
        "schema_version": MACHINE_STATE_SCHEMA_VERSION,
        "format": MACHINE_STATE_FORMAT,
        "updated_at": _now_iso(),
        "document_id": compat.get("vid", ""),
        "legacy_state_path": str(legacy_state_path),
        "machine_state_path": str(machine_state_path),
        "compat_projection": {
            "fields": compat,
            "source_hash": content_hash,
        },
        "source": source,
        "artifacts": artifacts,
        "workflow": workflow,
        "normalization": existing_normalization,
    }


def _read_machine_state(machine_state_path: str | Path) -> dict:
    path = Path(machine_state_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error: Cannot read machine state: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(payload, dict):
        print("Error: Machine state must be a JSON object", file=sys.stderr)
        sys.exit(2)
    return payload


def _write_machine_state(machine_state_path: str | Path, payload: dict) -> None:
    path = Path(machine_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _machine_state_to_compat_fields(payload: dict) -> dict:
    compat = payload.get("compat_projection", {}).get("fields", {}) if isinstance(payload, dict) else {}
    if not isinstance(compat, dict):
        return {}
    return {str(key).strip(): str(value).strip() for key, value in compat.items() if str(key).strip()}


def sync_machine_state(state_ref: str, write_legacy: bool = False) -> dict:
    path = Path(state_ref)
    if path.suffix.lower() == ".json":
        if not path.exists():
            print(f"Error: Machine state file not found: {state_ref}", file=sys.stderr)
            sys.exit(1)
        machine_state = _read_machine_state(path)
        legacy_state_path = _derive_legacy_state_path(path)
        compat_fields = _machine_state_to_compat_fields(machine_state)
        if write_legacy:
            lines = ["# State"]
            for key, value in compat_fields.items():
                lines.append(f"{key}: {value}")
            _atomic_write_text(legacy_state_path, "\n".join(lines) + "\n")
        return {
            "machine_state_path": str(path),
            "legacy_state_path": str(legacy_state_path),
            "compat_fields": compat_fields,
            "updated_machine_state": False,
            "updated_legacy_state": write_legacy,
        }

    if not path.exists():
        machine_state_path = _derive_machine_state_path(path)
        if machine_state_path.exists():
            machine_state = _read_machine_state(machine_state_path)
            return {
                "machine_state_path": str(machine_state_path),
                "legacy_state_path": str(path),
                "compat_fields": _machine_state_to_compat_fields(machine_state),
                "updated_machine_state": False,
                "updated_legacy_state": False,
            }
        print(f"Error: State file not found: {state_ref}", file=sys.stderr)
        sys.exit(1)

    compat_fields, content = _read_legacy_state_file(path)
    machine_state_path = _derive_machine_state_path(path)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = _read_machine_state(machine_state_path) if machine_state_path.exists() else None
    existing_hash = ""
    if isinstance(existing, dict):
        existing_hash = str(existing.get("compat_projection", {}).get("source_hash", "")).strip()
    updated_machine_state = not existing or existing_hash != content_hash
    if updated_machine_state:
        machine_state = _compat_fields_to_machine_state(compat_fields, path, machine_state_path, existing_payload=existing)
        _write_machine_state(machine_state_path, machine_state)
    else:
        machine_state = existing
    return {
        "machine_state_path": str(machine_state_path),
        "legacy_state_path": str(path),
        "compat_fields": _machine_state_to_compat_fields(machine_state),
        "updated_machine_state": updated_machine_state,
        "updated_legacy_state": False,
    }


def load_state(state_path: str) -> dict:
    """
    Load workflow state through the authoritative machine-state bridge.

    Compatibility behavior:
    - legacy markdown state files remain accepted inputs
    - if a legacy state file is provided, a sibling machine_state.json is
      created or refreshed automatically
    - direct machine_state.json inputs are also supported
    """
    sync_result = sync_machine_state(state_path)
    return sync_result["compat_fields"]


def _normalize_text_body(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _update_machine_state_normalization(machine_state_path: str | Path, *, normalized_document_path: str,
                                        source_adapter: str, preferred_chunk_source: str,
                                        segment_count: int, char_count: int,
                                        raw_text_path: str = "", segments_path: str = "") -> None:
    payload = _read_machine_state(machine_state_path)
    artifacts = payload.get("artifacts", {}) if isinstance(payload.get("artifacts", {}), dict) else {}
    if raw_text_path:
        artifacts["raw_text"] = raw_text_path
    if segments_path:
        artifacts["segments_path"] = segments_path
    artifacts["normalized_document"] = normalized_document_path
    payload["artifacts"] = artifacts
    payload["normalization"] = {
        "materialized_at": _now_iso(),
        "source_adapter": source_adapter,
        "preferred_chunk_source": preferred_chunk_source,
        "segment_count": segment_count,
        "char_count": char_count,
        "normalized_document_path": normalized_document_path,
    }
    payload["updated_at"] = _now_iso()
    _write_machine_state(machine_state_path, payload)


def normalize_document(state_ref: str, output_path: str = "", prefer: str = "auto",
                       allow_missing: bool = False) -> dict:
    sync_result = sync_machine_state(state_ref)
    compat = sync_result["compat_fields"]
    machine_state_path = sync_result["machine_state_path"]
    machine_state = _read_machine_state(machine_state_path)
    document_id = str(compat.get("vid", machine_state.get("document_id", ""))).strip()
    normalized_document_path = str(output_path or _default_normalized_document_path(document_id)).strip()
    if not document_id:
        return {
            "passed": False,
            "warnings": [],
            "hard_failures": ["Missing document id for normalization"],
            "machine_state_path": machine_state_path,
            "legacy_state_path": sync_result["legacy_state_path"],
            "normalized_document_path": normalized_document_path,
            "materialized": False,
        }

    preference = str(prefer or "auto").strip().lower()
    if preference not in {"auto", "segments", "raw_text"}:
        return {
            "passed": False,
            "warnings": [],
            "hard_failures": [f"Unsupported normalization preference: {prefer}"],
            "machine_state_path": machine_state_path,
            "legacy_state_path": sync_result["legacy_state_path"],
            "normalized_document_path": normalized_document_path,
            "materialized": False,
        }

    machine_artifacts = machine_state.get("artifacts", {}) if isinstance(machine_state.get("artifacts", {}), dict) else {}
    raw_text_path = str(compat.get("raw_text") or machine_artifacts.get("raw_text") or f"/tmp/{document_id}_raw_text.txt").strip()
    segments_path = str(compat.get("segments_path") or machine_artifacts.get("segments_path") or f"/tmp/{document_id}_segments.json").strip()
    normalized_document_path = str(output_path or machine_artifacts.get("normalized_document") or normalized_document_path).strip()

    raw_text_file = Path(raw_text_path) if raw_text_path else None
    segments_file = Path(segments_path) if segments_path else None
    use_segments = False
    if preference in {"auto", "segments"} and segments_file and segments_file.exists():
        use_segments = True
    elif preference == "segments":
        return {
            "passed": False,
            "warnings": [],
            "hard_failures": [f"Segments artifact not found: {segments_path}"],
            "machine_state_path": machine_state_path,
            "legacy_state_path": sync_result["legacy_state_path"],
            "normalized_document_path": normalized_document_path,
            "materialized": False,
        }

    if not use_segments and not (raw_text_file and raw_text_file.exists()):
        if allow_missing:
            return {
                "passed": True,
                "warnings": ["Normalization skipped: no raw text or segments artifact found"],
                "hard_failures": [],
                "machine_state_path": machine_state_path,
                "legacy_state_path": sync_result["legacy_state_path"],
                "normalized_document_path": normalized_document_path,
                "materialized": False,
                "source_adapter": "",
                "preferred_chunk_source": "",
                "segment_count": 0,
            }
        return {
            "passed": False,
            "warnings": [],
            "hard_failures": [f"Raw text artifact not found: {raw_text_path}"],
            "machine_state_path": machine_state_path,
            "legacy_state_path": sync_result["legacy_state_path"],
            "normalized_document_path": normalized_document_path,
            "materialized": False,
        }

    normalized_segments = []
    has_timing = False
    source_adapter = "raw_text_file"
    preferred_chunk_source = "text"

    if use_segments:
        try:
            payload = json.loads(segments_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {
                "passed": False,
                "warnings": [],
                "hard_failures": [f"Cannot read segments artifact: {e}"],
                "machine_state_path": machine_state_path,
                "legacy_state_path": sync_result["legacy_state_path"],
                "normalized_document_path": normalized_document_path,
                "materialized": False,
            }
        if not isinstance(payload, dict) or not isinstance(payload.get("segments", []), list):
            return {
                "passed": False,
                "warnings": [],
                "hard_failures": ["Segments artifact must be a JSON object with a segments list"],
                "machine_state_path": machine_state_path,
                "legacy_state_path": sync_result["legacy_state_path"],
                "normalized_document_path": normalized_document_path,
                "materialized": False,
            }
        for seg in payload.get("segments", []):
            if not isinstance(seg, dict):
                continue
            clean_text = " ".join(_normalize_text_body(seg.get("text", "")).split())
            if not clean_text:
                continue
            start_time = _coerce_float_or_none(seg.get("start_time"))
            end_time = _coerce_float_or_none(seg.get("end_time"))
            has_timing = has_timing or start_time is not None or end_time is not None
            normalized_segments.append({
                "id": len(normalized_segments),
                "text": clean_text,
                "start_time": start_time,
                "end_time": end_time,
                "speaker": seg.get("speaker"),
            })
        normalized_text = "\n".join(segment["text"] for segment in normalized_segments).strip()
        source_adapter = "segments_json"
        preferred_chunk_source = "segments"
    else:
        try:
            normalized_text = _normalize_text_body(raw_text_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {
                "passed": False,
                "warnings": [],
                "hard_failures": [f"Cannot read raw text artifact: {e}"],
                "machine_state_path": machine_state_path,
                "legacy_state_path": sync_result["legacy_state_path"],
                "normalized_document_path": normalized_document_path,
                "materialized": False,
            }

    source_payload = machine_state.get("source", {}) if isinstance(machine_state.get("source", {}), dict) else {}
    workflow_payload = machine_state.get("workflow", {}) if isinstance(machine_state.get("workflow", {}), dict) else {}
    normalized_doc = {
        "schema_version": NORMALIZED_DOCUMENT_SCHEMA_VERSION,
        "format": NORMALIZED_DOCUMENT_FORMAT,
        "updated_at": _now_iso(),
        "document_id": document_id,
        "source_adapter": source_adapter,
        "source": {
            "type": source_payload.get("type", ""),
            "url": source_payload.get("url", ""),
            "title": source_payload.get("title", ""),
            "channel": source_payload.get("channel", ""),
            "upload_date": source_payload.get("upload_date", ""),
            "duration": source_payload.get("duration", ""),
            "source_language": source_payload.get("source_language", ""),
            "subtitle_source": source_payload.get("subtitle_source", ""),
        },
        "workflow": {
            "mode": workflow_payload.get("mode", compat.get("mode", "")),
        },
        "artifacts": {
            "raw_text": raw_text_path if raw_text_file and raw_text_file.exists() else "",
            "segments_path": segments_path if segments_file and segments_file.exists() else "",
            "normalized_document": normalized_document_path,
        },
        "content": {
            "text": normalized_text,
            "char_count": len(normalized_text),
            "line_count": len(normalized_text.splitlines()) if normalized_text else 0,
            "segment_count": len(normalized_segments),
            "has_timing": has_timing,
            "preferred_chunk_source": preferred_chunk_source,
        },
        "segments": normalized_segments,
    }
    _atomic_write_text(Path(normalized_document_path), json.dumps(normalized_doc, ensure_ascii=False, indent=2))
    _update_machine_state_normalization(
        machine_state_path,
        normalized_document_path=normalized_document_path,
        source_adapter=source_adapter,
        preferred_chunk_source=preferred_chunk_source,
        segment_count=len(normalized_segments),
        char_count=len(normalized_text),
        raw_text_path=raw_text_path if raw_text_file and raw_text_file.exists() else "",
        segments_path=segments_path if segments_file and segments_file.exists() else "",
    )
    return {
        "passed": True,
        "warnings": [],
        "hard_failures": [],
        "machine_state_path": machine_state_path,
        "legacy_state_path": sync_result["legacy_state_path"],
        "normalized_document_path": normalized_document_path,
        "materialized": True,
        "source_adapter": source_adapter,
        "preferred_chunk_source": preferred_chunk_source,
        "segment_count": len(normalized_segments),
        "has_timing": has_timing,
        "char_count": len(normalized_text),
    }


def validate_state(state_path: str, stage: str = "", require: list[str] | None = None) -> dict:
    """
    Validate workflow state fields for a stage or explicit required fields.

    Compatibility behavior:
    - legacy state.md inputs are accepted
    - machine_state.json is materialized and refreshed automatically when the
      legacy projection changes
    """
    sync_result = sync_machine_state(state_path)
    state = sync_result["compat_fields"]
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
        "machine_state_path": sync_result["machine_state_path"],
        "legacy_state_path": sync_result["legacy_state_path"],
        "updated_machine_state": sync_result["updated_machine_state"],
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
            "machine_state_path": validation.get("machine_state_path", ""),
            "legacy_state_path": validation.get("legacy_state_path", ""),
        }

    normalization = normalize_document(state_path, allow_missing=True)
    if not normalization["passed"]:
        return {
            "passed": False,
            "checks": validation["checks"],
            "warnings": normalization.get("warnings", []),
            "hard_failures": normalization.get("hard_failures", []),
            "machine_state_path": validation.get("machine_state_path", ""),
            "legacy_state_path": validation.get("legacy_state_path", ""),
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
    planner_config = load_config(None, allow_missing=True)
    bilingual_quality_gate = mode == "bilingual"

    video_path = "long" if duration >= 1800 else "short"
    outputs = {
        "raw_text": str(state.get("raw_text", "") or f"/tmp/{video_id}_raw_text.txt"),
        "structured_text": str(state.get("structured_text", "") or f"/tmp/{video_id}_structured.txt"),
        "optimized_text": str(state.get("optimized_text", "") or f"/tmp/{video_id}_optimized.txt"),
        "normalized_document": normalization.get("normalized_document_path", ""),
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

    def build_control_contract(kind: str, prompt: str, input_key: str = "", *, bilingual_output: bool = False) -> dict:
        return _build_operation_control_contract(
            kind,
            prompt,
            input_key=input_key,
            config=planner_config,
            bilingual=bilingual_output,
        )

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
                    "control": build_control_contract("prompt", "structure_only", bilingual_output=False),
                },
                {
                    "kind": "prompt",
                    "prompt": "translate_only",
                    "input": outputs["structured_text"],
                    "output": outputs["optimized_text"],
                    "extra_instruction": "",
                    "execution": build_execution_contract("prompt"),
                    "control": build_control_contract("prompt", "translate_only", bilingual_output=True),
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
                    "control": build_control_contract("prompt", "structure_only", bilingual_output=False),
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
                "control": build_control_contract("chunk", "structure_only", "raw_path", bilingual_output=False),
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
                    "control": build_control_contract("chunk", "translate_only", "processed_path", bilingual_output=True),
                }
            )

    chunk_driver = "chunk-document" if normalization.get("materialized") else ("chunk-segments" if Path(str(state.get("segments_path", "") or f"/tmp/{video_id}_segments.json")).exists() else "chunk-text")
    chunking = {
        "driver": chunk_driver,
        "normalized_document_path": normalization.get("normalized_document_path", ""),
        "preferred_source_kind": normalization.get("preferred_chunk_source", ""),
        "boundary_mode": "strict",
        "continuity_mode": "reference_only",
        "merge_strategy": "ordered_concat",
    }

    return {
        "passed": True,
        "machine_state_path": validation.get("machine_state_path", ""),
        "legacy_state_path": validation.get("legacy_state_path", ""),
        "checks": {
            "state_stage": "post-source",
            "duration": duration,
            "mode": mode,
            "source": source,
            "video_path": video_path,
        },
        "warnings": normalization.get("warnings", []),
        "hard_failures": [],
        "video_path": video_path,
        "normalization": {
            "materialized": normalization.get("materialized", False),
            "source_adapter": normalization.get("source_adapter", ""),
            "preferred_chunk_source": normalization.get("preferred_chunk_source", ""),
            "segment_count": normalization.get("segment_count", 0),
            "normalized_document_path": normalization.get("normalized_document_path", ""),
        },
        "chunking": chunking,
        "requires_llm_preflight": video_path == "long",
        "requires_quality_check": True,
        "quality_contract": _build_quality_gate_contract(bilingual=bilingual_quality_gate),
        "replan_contract": {
            "raw_path": _build_replan_contract("raw_path", applicable=True, canary_chunks=planner_config.get("autotune_canary_chunks", DEFAULT_AUTOTUNE_CANARY_CHUNKS)),
            "processed_path": _build_replan_contract("processed_path", applicable=True, canary_chunks=planner_config.get("autotune_canary_chunks", DEFAULT_AUTOTUNE_CANARY_CHUNKS)),
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

    yt_dlp_socket_timeout_sec = parse_int_field('yt_dlp_socket_timeout_sec', -1, minimum=-1)
    yt_dlp_retries = parse_int_field('yt_dlp_retries', -1, minimum=-1)
    yt_dlp_extractor_retries = parse_int_field('yt_dlp_extractor_retries', -1, minimum=-1)
    yt_dlp_cookies_from_browser = str(config.get('yt_dlp_cookies_from_browser', '') or '').strip()
    yt_dlp_cookies_file = str(config.get('yt_dlp_cookies_file', '') or '').strip()
    if yt_dlp_cookies_file:
        yt_dlp_cookies_file = os.path.expanduser(yt_dlp_cookies_file)
        if not os.path.isfile(yt_dlp_cookies_file):
            print(f"Warning: yt_dlp_cookies_file does not exist: {yt_dlp_cookies_file}", file=sys.stderr)

    llm_timeout_sec = parse_int_field('llm_timeout_sec', 120, minimum=1)
    llm_max_retries = parse_int_field('llm_max_retries', 3, minimum=0)
    llm_backoff_sec = parse_float_field('llm_backoff_sec', 1.5, minimum=0.1)
    llm_probe_timeout_sec = parse_int_field('llm_probe_timeout_sec', 20, minimum=1)
    llm_probe_max_tokens = parse_int_field('llm_probe_max_tokens', 16, minimum=1)
    llm_stop_after_consecutive_timeouts = parse_int_field('llm_stop_after_consecutive_timeouts', 2, minimum=1)
    llm_chunk_recovery_attempts = parse_int_field(
        'llm_chunk_recovery_attempts',
        DEFAULT_LLM_CHUNK_RECOVERY_ATTEMPTS,
        minimum=0,
    )
    llm_chunk_recovery_backoff_sec = parse_float_field(
        'llm_chunk_recovery_backoff_sec',
        DEFAULT_LLM_CHUNK_RECOVERY_BACKOFF_SEC,
        minimum=0.0,
    )

    parsed = dict(defaults)
    parsed.update({
        "output_dir": output_dir,
        "deepgram_api_key": config.get('deepgram_api_key', ''),
        "llm_api_key": config.get('llm_api_key', ''),
        "llm_base_url": config.get('llm_base_url', ''),
        "llm_model": config.get('llm_model', ''),
        "llm_api_format": config.get('llm_api_format', 'openai'),
        "yt_dlp_socket_timeout_sec": yt_dlp_socket_timeout_sec,
        "yt_dlp_retries": yt_dlp_retries,
        "yt_dlp_extractor_retries": yt_dlp_extractor_retries,
        "yt_dlp_cookies_from_browser": yt_dlp_cookies_from_browser,
        "yt_dlp_cookies_file": yt_dlp_cookies_file,
        "llm_timeout_sec": llm_timeout_sec,
        "llm_max_retries": llm_max_retries,
        "llm_backoff_sec": llm_backoff_sec,
        "llm_stream": _normalize_stream_mode(config.get('llm_stream', 'auto')),
        "llm_probe_timeout_sec": llm_probe_timeout_sec,
        "llm_probe_max_tokens": llm_probe_max_tokens,
        "llm_stop_after_consecutive_timeouts": llm_stop_after_consecutive_timeouts,
        "llm_chunk_recovery_attempts": llm_chunk_recovery_attempts,
        "llm_chunk_recovery_backoff_sec": llm_chunk_recovery_backoff_sec,
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
    parser.add_argument(
        '--api-envelope',
        action='store_true',
        help='Emit stable kernel-command result envelopes instead of legacy flat JSON for kernel commands',
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # parse-vtt command
    vtt_parser = subparsers.add_parser(
        'parse-vtt',
        help='Parse VTT subtitle file, output plain text'
    )
    vtt_parser.add_argument('vtt_path', help='VTT file path')

    # parse-vtt-segments command
    vtt_segments_parser = subparsers.add_parser(
        'parse-vtt-segments',
        help='Parse VTT subtitle file, output aligned segments JSON'
    )
    vtt_segments_parser.add_argument('vtt_path', help='VTT file path')
    vtt_segments_parser.add_argument('--language', default='',
                                     help='Optional language override for output metadata')

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
    tdg_parser.add_argument('--output-json', default='', help='Optional JSON output path; split mode also writes sibling chunk payload files')
    tdg_parser.add_argument('--output-text', default='', help='Optional path to write merged transcript text')
    tdg_parser.add_argument('--output-segments', default='', help='Optional path to write aligned source segments JSON')

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

    # chunk-segments command
    chunk_segments_parser = subparsers.add_parser(
        'chunk-segments',
        help='Split aligned source segments into timed chunks'
    )
    chunk_segments_parser.add_argument('segments_path', help='Aligned source segments JSON path')
    chunk_segments_parser.add_argument('output_dir', help='Output directory for chunks')
    chunk_segments_parser.add_argument('--chunk-size', type=int, default=0,
                                       help='Target chunk size in the active chunk_mode; without --prompt it keeps legacy character sizing')
    chunk_segments_parser.add_argument('--prompt', default='',
                                       help='Optional prompt name for task-aware auto chunk sizing')
    chunk_segments_parser.add_argument('--config-path', default=None,
                                       help='Optional path to config file for chunk planning')
    chunk_segments_parser.add_argument('--chapters', default='',
                                       help='Optional YouTube chapters JSON path; forces chunk boundaries at chapter starts')

    # chunk-document command
    chunk_document_parser = subparsers.add_parser(
        'chunk-document',
        help='Chunk a normalized document using its preferred source shape'
    )
    chunk_document_parser.add_argument('normalized_document_path', help='normalized_document.json path')
    chunk_document_parser.add_argument('output_dir', help='Output directory for chunks')
    chunk_document_parser.add_argument('--chunk-size', type=int, default=0,
                                       help='Target chunk size in the active chunk_mode; without --prompt it keeps legacy character sizing')
    chunk_document_parser.add_argument('--prompt', default='',
                                       help='Optional prompt name for task-aware auto chunk sizing')
    chunk_document_parser.add_argument('--config-path', default=None,
                                       help='Optional path to config file for chunk planning')
    chunk_document_parser.add_argument('--chapters', default='',
                                       help='Optional YouTube chapters JSON path; forces chunk boundaries at chapter starts when timed segments exist')
    chunk_document_parser.add_argument('--prefer', default='auto', choices=['auto', 'segments', 'text'],
                                       help='Preferred normalized source shape for chunking')

    # get-chapters command
    chapters_parser = subparsers.add_parser(
        'get-chapters',
        help='Fetch YouTube video chapter metadata'
    )
    chapters_parser.add_argument('video_url', help='YouTube video URL')

    # build-chapter-plan command
    chapter_plan_parser = subparsers.add_parser(
        'build-chapter-plan',
        help='Map YouTube chapters onto timed chunks'
    )
    chapter_plan_parser.add_argument('chapters_path', help='Chapters JSON path')
    chapter_plan_parser.add_argument('manifest_ref', help='Manifest path or work directory containing manifest.json')
    chapter_plan_parser.add_argument('output_path', help='Output chapter_plan.json path')

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

    # prepare-resume command
    resume_parser = subparsers.add_parser(
        'prepare-resume',
        help='Repair stale chunk/runtime state before resuming a run'
    )
    resume_parser.add_argument('work_dir', help='Working directory with manifest.json')
    resume_parser.add_argument('--prompt', default='',
                               help='Optional prompt override; defaults to manifest plan prompt')
    resume_parser.add_argument('--config-path', default=None,
                               help='Optional path to config file')
    resume_parser.add_argument('--input-key', default='raw_path',
                               help='Reserved for future resume checks; currently informational only')

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

    # runtime-status command
    runtime_status_parser = subparsers.add_parser(
        'runtime-status',
        help='Inspect manifest runtime, ownership, and local runtime-control state'
    )
    runtime_status_parser.add_argument('work_dir', help='Working directory to inspect')

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

    # sync-state command
    sync_state_parser = subparsers.add_parser(
        'sync-state',
        help='Sync legacy state.md with authoritative machine_state.json'
    )
    sync_state_parser.add_argument('state_ref', help='Path to legacy state.md or machine_state.json')
    sync_state_parser.add_argument('--write-legacy', action='store_true',
                                   help='When given a machine_state.json, write the legacy state.md projection')

    # normalize-document command
    normalize_parser = subparsers.add_parser(
        'normalize-document',
        help='Materialize normalized_document.json from raw text or segments artifacts'
    )
    normalize_parser.add_argument('state_ref', help='Path to legacy state.md or machine_state.json')
    normalize_parser.add_argument('--output', default='', help='Optional output path for normalized_document.json')
    normalize_parser.add_argument('--prefer', default='auto', choices=['auto', 'segments', 'raw_text'],
                                  help='Preferred source artifact for normalization')
    normalize_parser.add_argument('--allow-missing', action='store_true',
                                  help='Return success when no raw text or segments artifact is available')

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

    elif args.command == 'parse-vtt-segments':
        result = parse_vtt_segments(args.vtt_path, language=args.language)
        print(json.dumps(result, ensure_ascii=False))

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
            output_text=args.output_text,
            output_segments=args.output_segments
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
        envelope = run_kernel_command(
            'chunk-text',
            input_path=args.input_path,
            output_dir=args.output_dir,
            chunk_size=args.chunk_size,
            prompt_name=args.prompt,
            config_path=args.config_path,
        )
        print(json.dumps(envelope if args.api_envelope else envelope['result'], ensure_ascii=False))

    elif args.command == 'chunk-segments':
        envelope = run_kernel_command(
            'chunk-segments',
            segments_path=args.segments_path,
            output_dir=args.output_dir,
            chunk_size=args.chunk_size,
            prompt_name=args.prompt,
            config_path=args.config_path,
            chapters_path=args.chapters,
        )
        print(json.dumps(envelope if args.api_envelope else envelope['result'], ensure_ascii=False))

    elif args.command == 'chunk-document':
        envelope = run_kernel_command(
            'chunk-document',
            normalized_document_path=args.normalized_document_path,
            output_dir=args.output_dir,
            chunk_size=args.chunk_size,
            prompt_name=args.prompt,
            config_path=args.config_path,
            chapters_path=args.chapters,
            prefer=args.prefer,
        )
        print(json.dumps(envelope if args.api_envelope else envelope['result'], ensure_ascii=False))

    elif args.command == 'get-chapters':
        result = get_chapters(args.video_url)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'build-chapter-plan':
        result = build_chapter_plan(args.chapters_path, args.manifest_ref, args.output_path)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'merge-content':
        envelope = run_kernel_command(
            'merge-content',
            work_dir=args.work_dir,
            output_file=args.output_file,
            header=args.header,
        )
        print(json.dumps(envelope if args.api_envelope else envelope['result'], ensure_ascii=False))

    elif args.command == 'process-chunks':
        envelope = run_kernel_command(
            'process-chunks',
            work_dir=args.work_dir,
            prompt_name=args.prompt,
            extra_instruction=args.extra_instruction,
            config_path=args.config_path,
            dry_run=args.dry_run,
            input_key=args.input_key,
            force=args.force,
            auto_replan=args.auto_replan,
            max_replans=args.max_replans,
        )
        result = envelope['result']
        print(json.dumps(envelope if args.api_envelope else result, ensure_ascii=False))
        if not result.get('success', False) and not result.get('dry_run', False):
            sys.exit(1)

    elif args.command == 'prepare-resume':
        envelope = run_kernel_command(
            'prepare-resume',
            work_dir=args.work_dir,
            prompt_name=args.prompt,
            config_path=args.config_path,
            input_key=args.input_key,
        )
        print(json.dumps(envelope if args.api_envelope else envelope['result'], ensure_ascii=False))

    elif args.command == 'replan-remaining':
        envelope = run_kernel_command(
            'replan-remaining',
            work_dir=args.work_dir,
            prompt_name=args.prompt,
            config_path=args.config_path,
            chunk_size=args.chunk_size,
            input_key=args.input_key,
        )
        result = envelope['result']
        print(json.dumps(envelope if args.api_envelope else result, ensure_ascii=False))
        if not result.get('success', False):
            sys.exit(1)

    elif args.command == 'runtime-status':
        envelope = run_kernel_command(
            'runtime-status',
            work_dir=args.work_dir,
        )
        print(json.dumps(envelope if args.api_envelope else envelope['result'], ensure_ascii=False))

    elif args.command == 'assemble-final':
        envelope = run_kernel_command(
            'assemble-final',
            optimized_text_path=args.optimized_text_path,
            output_file=args.output_file,
            title=args.title,
            source=args.source,
            channel=args.channel,
            date=args.date,
            created=args.created,
            duration=args.duration,
            transcript_source=args.transcript_source,
            bilingual=args.bilingual,
        )
        print(json.dumps(envelope if args.api_envelope else envelope['result'], ensure_ascii=False))

    elif args.command == 'verify-quality':
        envelope = run_kernel_command(
            'verify-quality',
            optimized_text_path=args.optimized_text_path,
            raw_text_path=args.raw_text,
            bilingual=args.bilingual,
        )
        result = envelope['result']
        print(json.dumps(envelope if args.api_envelope else result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    elif args.command == 'load-config':
        result = load_config(args.config_path)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'sync-state':
        result = sync_machine_state(args.state_ref, write_legacy=args.write_legacy)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'normalize-document':
        envelope = run_kernel_command(
            'normalize-document',
            state_ref=args.state_ref,
            output_path=args.output,
            prefer=args.prefer,
            allow_missing=args.allow_missing,
        )
        result = envelope['result']
        print(json.dumps(envelope if args.api_envelope else result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    elif args.command == 'validate-state':
        envelope = run_kernel_command(
            'validate-state',
            state_path=args.state_path,
            stage=args.stage,
            require=args.require,
        )
        result = envelope['result']
        print(json.dumps(envelope if args.api_envelope else result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    elif args.command == 'plan-optimization':
        envelope = run_kernel_command(
            'plan-optimization',
            state_path=args.state_path,
        )
        result = envelope['result']
        print(json.dumps(envelope if args.api_envelope else result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
