#!/usr/bin/env python3
"""
yt-transcript: one YouTube video to one Markdown article.

`run` drives the whole flow (kernel/pipeline.py); the rest of this module holds
the functions its stages call: VTT parsing, Deepgram transcription, the text
plan, normalization, merge, the quality check and article assembly.

Usage:
    python3 yt_transcript_utils.py <command> [args]

Commands:
    run <url>                      Run or continue the whole pipeline for one video (state.json per video)
    status [video_id]              Show the runs under the work root, or one video's state.json
    cleanup <video_id>             Remove a video's work directory (--force for an unfinished run)

Debug commands:
    subtitle-info                  Detect the spoken language and pick subtitle candidates from yt-dlp metadata on stdin
    verify-quality <optimized_text>  Rerun the quality check on any file
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import hashlib
import http.client
import json
import math
import os
import random  # noqa: F401  (kernel/long_text/llm.py reads it through these module globals)
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

from kernel.files import atomic_write_text as _atomic_write_text
from kernel.text_cleanup import overlap as kernel_overlap
from kernel.text_cleanup import subtitle as kernel_subtitle_cleanup
from kernel.long_text import glossary as kernel_glossary
from kernel.long_text import semantic as kernel_semantic
from kernel.long_text import prompting as kernel_prompting
from kernel.long_text import llm as kernel_llm
from kernel.long_text import agent_cli as kernel_agent_cli
from kernel.long_text import window as kernel_window
from kernel.long_text import outline as kernel_outline
from kernel import subtitle_language as kernel_subtitle_language


def _skill_root() -> Path:
    """Return the repository root for this skill implementation."""
    return Path(__file__).resolve().parent


def _default_config_path() -> Path:
    """Return the default config path."""
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
    """Strip wrapping quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _yaml_string(value: str) -> str:
    # Intentionally quote every scalar. The frontmatter should favor predictable
    # parsing over pretty YAML, especially for titles/channels that may contain
    # punctuation, quotes, or comment-like characters.
    """Yaml string."""
    return json.dumps(str(value).replace("\r\n", "\n"), ensure_ascii=False)


def _single_line_text(value: str) -> str:
    """Single line text."""
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
    """Build api url."""
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


class LLMRequestError(Exception):
    """Structured exception for LLM transport and provider-level request failures."""
    def __init__(self, message: str, *, error_type: str = "unknown",
                 status_code: int = None, retryable: bool = False,
                 request_url: str = "", response_body: str = ""):
        """Initialize the `LLMRequestError` instance."""
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.retryable = retryable
        self.request_url = request_url
        self.response_body = response_body


def _parse_bool(value, default: bool = False) -> bool:
    """Parse bool."""
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
    """Parse an integer value and fall back to the provided default."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value, default: float) -> float:
    """Parse float."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_int_min(value, default: int, minimum: int) -> int:
    """Parse an integer and clamp it to a minimum valid value."""
    parsed = _parse_int(value, default)
    if parsed < minimum:
        return default
    return parsed


def _parse_float_range(value, default: float, minimum: float, maximum: float) -> float:
    """Parse float range."""
    parsed = _parse_float(value, default)
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _normalize_stream_mode(value) -> str:
    """Normalize stream mode."""
    if value is None:
        return "auto"
    text = str(value).strip().lower()
    if text in {"auto", "true", "false"}:
        return text
    return "auto"


def _now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


TASK_CHUNK_TOKEN_DEFAULTS = {
    "structure_only": 1200,
    "cleanup_zh": 1000,
    "translate_only": 900,
}

TASK_OUTPUT_RATIO_DEFAULTS = {
    "structure_only": 1.15,
    "cleanup_zh": 1.05,
    "translate_only": 1.10,
}

# max_tokens is a stop line, not a cost: an unused ceiling is free, while a
# tight one truncates output (and reasoning models spend part of it on hidden
# reasoning). Chunk size, not this ceiling, controls how much a request writes.
DEFAULT_MAX_OUTPUT_TOKENS = 8192
TASK_MAX_OUTPUT_TOKEN_DEFAULTS = {
    "structure_only": DEFAULT_MAX_OUTPUT_TOKENS,
    "cleanup_zh": DEFAULT_MAX_OUTPUT_TOKENS,
    "translate_only": DEFAULT_MAX_OUTPUT_TOKENS,
}

# Largest input (tokens) sent as one non-chunked prompt; longer text is chunked.
# Kept separate from the max_tokens ceiling, which no longer sizes anything.
SINGLE_PASS_TOKEN_LIMITS = {
    "structure_only": 4800,
    "translate_only": 4100,
}

DEFAULT_LLM_MODEL = "deepseek-v4-pro"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"

DEFAULT_CHUNK_HARD_CAP_MULTIPLIER = 1.33
MAX_CHUNK_HARD_CAP_MULTIPLIER = 2.0
DEFAULT_YT_DLP_SOCKET_TIMEOUT_SEC = 15
DEFAULT_YT_DLP_RETRIES = 1
DEFAULT_YT_DLP_EXTRACTOR_RETRIES = 1
DEFAULT_DEEPGRAM_MODEL = "nova-3"
DISABLED_DEEPGRAM_MODEL_PREFIXES = ("nova-2",)
DEFAULT_DEEPGRAM_ENABLE_UTTERANCES = True
DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT = True
DEFAULT_DEEPGRAM_REQUEST_RETRIES = 2
DEFAULT_DEEPGRAM_RETRY_BACKOFF_SEC = 1.0


DEEPGRAM_CHUNK_CACHE_SCHEMA_VERSION = 1
DEEPGRAM_CHUNK_CACHE_FORMAT = "yt_transcript.deepgram_chunk_cache/v1"
DEFAULT_UNKNOWN_CHUNK_TOKENS = 900
DEFAULT_UNKNOWN_OUTPUT_RATIO = 1.0
DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS


def _is_disabled_deepgram_model(model: str = "") -> bool:
    """Return whether a Deepgram model is disabled for this workflow."""
    normalized = str(model or "").strip().lower().replace("_", "-")
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}-")
        for prefix in DISABLED_DEEPGRAM_MODEL_PREFIXES
    )


def _resolve_deepgram_model(model: str = "") -> tuple[str, str]:
    """Resolve the Deepgram model, upgrading disabled Nova 2 variants to Nova 3."""
    requested = str(model or "").strip()
    if not requested:
        return DEFAULT_DEEPGRAM_MODEL, ""
    if _is_disabled_deepgram_model(requested):
        return (
            DEFAULT_DEEPGRAM_MODEL,
            f"deepgram_model={requested!r} is disabled for this skill; using {DEFAULT_DEEPGRAM_MODEL}",
        )
    return requested, ""


def _default_config_values(config_path: str = "") -> dict:
    """Return the default config values."""
    return {
        "output_dir": "",
        "deepgram_api_key": "",
        "deepgram_model": DEFAULT_DEEPGRAM_MODEL,
        "deepgram_enable_utterances": DEFAULT_DEEPGRAM_ENABLE_UTTERANCES,
        "deepgram_prefer_structured_output": DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT,
        "llm_api_key": "",
        "llm_base_url": DEFAULT_LLM_BASE_URL,
        "llm_model": DEFAULT_LLM_MODEL,
        "llm_api_format": "openai",
        "yt_dlp_socket_timeout_sec": DEFAULT_YT_DLP_SOCKET_TIMEOUT_SEC,
        "yt_dlp_retries": DEFAULT_YT_DLP_RETRIES,
        "yt_dlp_extractor_retries": DEFAULT_YT_DLP_EXTRACTOR_RETRIES,
        "yt_dlp_cookies_from_browser": "",
        "yt_dlp_cookies_file": "",
        "llm_timeout_sec": 120,
        "llm_max_retries": 3,
        "llm_backoff_sec": 1.5,
        "llm_stream": "auto",
        "llm_backend": kernel_agent_cli.DEFAULT_LLM_BACKEND,
        "llm_agent_cli": "",
        "llm_cli_timeout_sec": kernel_agent_cli.DEFAULT_CLI_TIMEOUT_SEC,
        "llm_cli_max_retries": kernel_agent_cli.DEFAULT_CLI_MAX_RETRIES,
        "llm_cli_probe_timeout_sec": kernel_agent_cli.DEFAULT_CLI_PROBE_TIMEOUT_SEC,
        "window_read_tokens": kernel_window.DEFAULT_WINDOW_READ_TOKENS,
        "window_write_tokens": kernel_window.DEFAULT_WINDOW_WRITE_TOKENS,
        "window_tail_tokens": kernel_window.DEFAULT_WINDOW_TAIL_TOKENS,
        "window_marker_retries": kernel_window.DEFAULT_WINDOW_MARKER_RETRIES,
        "window_outline": True,
        "window_outline_read_tokens": kernel_outline.DEFAULT_OUTLINE_READ_TOKENS,
        "window_outline_max_terms": kernel_outline.DEFAULT_OUTLINE_MAX_TERMS,
        "chunk_size_override": 0,
        "chunk_tokens_structure_only": TASK_CHUNK_TOKEN_DEFAULTS["structure_only"],
        "chunk_tokens_cleanup_zh": TASK_CHUNK_TOKEN_DEFAULTS["cleanup_zh"],
        "chunk_tokens_translate_only": TASK_CHUNK_TOKEN_DEFAULTS["translate_only"],
        "chunk_hard_cap_multiplier": DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
        "chunk_glossary_max_prompt_terms": 8,
        "output_ratio_structure_only": TASK_OUTPUT_RATIO_DEFAULTS["structure_only"],
        "output_ratio_cleanup_zh": TASK_OUTPUT_RATIO_DEFAULTS["cleanup_zh"],
        "output_ratio_translate_only": TASK_OUTPUT_RATIO_DEFAULTS["translate_only"],
        "max_output_tokens_structure_only": TASK_MAX_OUTPUT_TOKEN_DEFAULTS["structure_only"],
        "max_output_tokens_cleanup_zh": TASK_MAX_OUTPUT_TOKEN_DEFAULTS["cleanup_zh"],
        "max_output_tokens_translate_only": TASK_MAX_OUTPUT_TOKEN_DEFAULTS["translate_only"],
        "config_path": config_path,
        "config_warnings": [],
    }


def _get_task_chunk_target(prompt_name: str, config: dict | None = None) -> int:
    """Return task chunk target."""
    config = config or {}
    override = max(0, _parse_int(config.get("chunk_size_override"), 0))
    if override > 0:
        return override

    prompt = (prompt_name or "").strip().lower()
    default = TASK_CHUNK_TOKEN_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_CHUNK_TOKENS)
    return max(1, _parse_int(config.get(f"chunk_tokens_{prompt}"), default))


def _get_task_output_ratio(prompt_name: str, config: dict | None = None) -> float:
    """Return task output ratio."""
    config = config or {}
    prompt = (prompt_name or "").strip().lower()
    default = TASK_OUTPUT_RATIO_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_OUTPUT_RATIO)
    return max(0.01, _parse_float(config.get(f"output_ratio_{prompt}"), default))


def _get_task_max_output_tokens(prompt_name: str, config: dict | None = None) -> int:
    """Return task max output tokens."""
    config = config or {}
    prompt = (prompt_name or "").strip().lower()
    default = TASK_MAX_OUTPUT_TOKEN_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS)
    return max(1, _parse_int(config.get(f"max_output_tokens_{prompt}"), default))


def _is_cjk_char(char: str) -> bool:
    """Return whether cjk char."""
    return "\u3400" <= char <= "\u9fff"


def _is_kana_hangul_char(char: str) -> bool:
    """Return whether kana hangul char."""
    return ("\u3040" <= char <= "\u30ff") or ("\uac00" <= char <= "\ud7af")


def _is_cjk_family_char(char: str) -> bool:
    """Return whether a character belongs to a CJK-family script."""
    return _is_cjk_char(char) or _is_kana_hangul_char(char)


def _is_latin_word_char(char: str) -> bool:
    """Return whether latin word char."""
    return ("a" <= char <= "z") or ("A" <= char <= "Z") or char.isdigit()


def _new_token_estimate_state() -> dict:
    """Create token estimate state."""
    return {"tokens": 0, "punct_count": 0, "latin_word_len": 0}


def _advance_token_estimate_state(state: dict, char: str, next_char: str = "") -> None:
    """Advance token estimate state."""
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


def _estimate_tokens(text: str) -> int:
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


def _truncate_tail_text_to_tokens(text: str, max_tokens: int,
                                  config: dict | None = None) -> str:
    """Keep the tail-most portion of text within a soft token cap."""
    if not text:
        return ""
    if max_tokens <= 0 or _estimate_tokens(text) <= max_tokens:
        return text.strip()
    segments = _force_split_text_by_tokens(text.strip(), max_tokens, config)
    return segments[-1].strip() if segments else text.strip()


def _inject_context_block(prompt_template: str, context_text: str) -> str:
    """Inject context block."""
    return kernel_prompting._inject_context_block(prompt_template, context_text)


def _inject_glossary_context(prompt_template: str, glossary_text: str) -> str:
    """Inject glossary context."""
    return kernel_prompting._inject_glossary_context(prompt_template, glossary_text)


def _build_chunk_prompt(prompt_template: str, chunk_body: str,
                        context_text: str = "", glossary_text: str = "") -> str:
    """Build chunk prompt."""
    return kernel_prompting._build_chunk_prompt(
        prompt_template,
        chunk_body,
        context_text=context_text,
        glossary_text=glossary_text,
    )


def _build_attempt_log_from_result(result: dict, attempt_index: int | None = None) -> dict:
    """Build attempt log from result."""
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
        "finish_reason": str(result.get("finish_reason", "") or ""),
        "usage": result.get("usage", {}) if isinstance(result.get("usage", {}), dict) else {},
    }


def _build_attempt_log_from_error(error: Exception, attempt_index: int | None = None) -> dict:
    """Build attempt log from error."""
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


def _classify_llm_transport_issue(error_or_reason) -> tuple[str, bool]:
    """Classify llm transport issue."""
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


def _resolve_prompt_template_path(prompt_name: str) -> Path:
    """Resolve prompt template path."""
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


def _force_split_text_by_tokens(text: str, max_tokens: int, config: dict | None = None) -> list[str]:
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


def _is_retryable_status(status_code: int) -> bool:
    """Return whether retryable status."""
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _extract_openai_content_from_message(message) -> str:
    """Extract user-visible content from an OpenAI-compatible message payload."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text_parts.append(str(part.get("text", part.get("content", "")) or ""))
        return "".join(text_parts)
    return str(content or "")


def _extract_llm_finish_reason(result: dict, api_format: str) -> str:
    """Extract finish reason from a provider response when present."""
    if api_format == "anthropic":
        return str(result.get("stop_reason", "") or "")
    choices = result.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason", "") or "")


def _extract_llm_reasoning_text(result: dict, api_format: str) -> str:
    """Extract hidden/reasoning text if the gateway exposes it."""
    if api_format == "anthropic":
        return ""
    choices = result.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        return ""
    candidates = [
        message.get("reasoning_content"),
        message.get("reasoning"),
        message.get("thinking"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("content") or candidate.get("text")
        if candidate:
            return str(candidate)
    return ""


def _extract_openai_usage(usage: dict | None) -> dict:
    """Normalize OpenAI-compatible usage, including reasoning token details."""
    usage = usage if isinstance(usage, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}

    completion_tokens = max(0, _parse_int(usage.get("completion_tokens"), 0))
    reasoning_tokens = max(
        0,
        _parse_int(
            completion_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0)),
            0,
        ),
    )
    content_tokens = max(0, completion_tokens - reasoning_tokens)
    return {
        "prompt_tokens": max(0, _parse_int(usage.get("prompt_tokens"), 0)),
        "completion_tokens": completion_tokens,
        "total_tokens": max(0, _parse_int(usage.get("total_tokens"), 0)),
        "reasoning_tokens": reasoning_tokens,
        "content_tokens": content_tokens,
        "cached_prompt_tokens": max(
            0,
            _parse_int(prompt_details.get("cached_tokens", usage.get("cached_prompt_tokens", 0)), 0),
        ),
        "raw": usage,
    }


def _extract_llm_usage(result: dict, api_format: str) -> dict:
    """Normalize token usage from a provider response."""
    if api_format == "anthropic":
        usage = result.get("usage") if isinstance(result, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = max(0, _parse_int(usage.get("input_tokens"), 0))
        output_tokens = max(0, _parse_int(usage.get("output_tokens"), 0))
        return {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "reasoning_tokens": 0,
            "content_tokens": output_tokens,
            "cached_prompt_tokens": 0,
            "raw": usage,
        }
    return _extract_openai_usage(result.get("usage") if isinstance(result, dict) else {})


def _build_llm_result_metadata(result: dict, api_format: str) -> dict:
    """Build normalized response metadata used by chunk recovery logic."""
    return {
        "finish_reason": _extract_llm_finish_reason(result, api_format),
        "usage": _extract_llm_usage(result, api_format),
        "reasoning_text": _extract_llm_reasoning_text(result, api_format),
    }


def _extract_openai_stream_text(payload: dict) -> str:
    """Extract openai stream text."""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content", "")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def _extract_anthropic_stream_text(payload: dict) -> str:
    """Extract anthropic stream text."""
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
    """Extract llm text."""
    if api_format == "anthropic":
        content = result.get("content") or []
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        if text_parts or "content" in result:
            return "".join(text_parts)
        raise KeyError("content")
    return _extract_openai_content_from_message(result["choices"][0]["message"])


def _read_streaming_response(response, api_format: str) -> dict:
    """Read streaming response."""
    text_parts = []
    finish_reason = ""
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
            # Anthropic streams stop_reason in the message_delta event's delta.
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            finish_reason = (
                str(delta.get("stop_reason") or "")
                or finish_reason
                or _extract_llm_finish_reason(payload, api_format)
            )
        else:
            text = _extract_openai_stream_text(payload)
            choices = payload.get("choices") or []
            if choices and isinstance(choices[0], dict) and choices[0].get("finish_reason"):
                finish_reason = str(choices[0].get("finish_reason") or "")
        if text:
            text_parts.append(text)
    return {
        "text": "".join(text_parts),
        "finish_reason": finish_reason,
        "usage": {},
        "reasoning_text": "",
    }


def _build_llm_request(api_key: str, base_url: str, model: str, messages: list,
                       api_format: str = "openai", max_tokens: int = 8192,
                       temperature: float = 0.3, use_stream: bool = False) -> tuple[str, dict, bytes]:
    """Build llm request."""
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
    """Execute llm request."""
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
                metadata = _read_streaming_response(response, api_format)
                text = metadata["text"]
                streaming_used = True
            else:
                result = json.loads(response.read().decode("utf-8"))
                text = _extract_llm_text(result, api_format)
                metadata = _build_llm_result_metadata(result, api_format)
                streaming_used = False
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return {
            "text": text,
            "latency_ms": latency_ms,
            "request_url": url,
            "streaming_used": streaming_used,
            "finish_reason": metadata.get("finish_reason", ""),
            "usage": metadata.get("usage", {}),
            "reasoning_text": metadata.get("reasoning_text", ""),
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


def _normalize_subtitle_text(text: str) -> str:
    """Apply conservative cleanup tailored to subtitle-derived text."""
    return kernel_subtitle_cleanup.normalize_subtitle_text(text)


def _normalize_subtitle_segment_text(text: str) -> str:
    """Normalize a single subtitle cue into a cue-sized text fragment."""
    return kernel_subtitle_cleanup.normalize_subtitle_segment_text(text)


def _parse_vtt_payload(content: str, *, language: str = "", vtt_path: str = "") -> dict:
    """Parse VTT content into aligned subtitle segments."""
    try:
        return kernel_subtitle_cleanup.parse_vtt_payload(content, language=language, vtt_path=vtt_path)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(2)


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

    parsed = parse_vtt_segments(vtt_path)
    body = "\n".join(segment.get("text", "") for segment in parsed.get("segments", []) if segment.get("text"))
    return _normalize_subtitle_text(body)


def parse_vtt_segments(vtt_path: str, *, language: str = "") -> dict:
    """Parse a VTT file and return aligned segments with timing metadata.

    Output schema (the segments file the normalizer reads):

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
    return _parse_vtt_payload(content, language=language, vtt_path=str(path))


def _coerce_float_or_none(value) -> float | None:
    """Coerce float or none."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_confusable_ascii_token(token: str) -> str:
    """Normalize conservative digit/letter confusions in mostly alphabetic tokens."""
    text = str(token or "")
    if len(text) < 5:
        return text
    if not re.search(r'[A-Za-z]', text) or not re.search(r'[0-9]', text):
        return text

    digit_count = sum(ch.isdigit() for ch in text)
    alpha_count = sum(ch.isalpha() for ch in text)
    if digit_count > 2 or alpha_count < 3:
        return text

    normalized = re.sub(r'(?<=[A-Za-z])0(?=[A-Za-z])', 'o', text)
    normalized = re.sub(r'(?<=[a-z])1(?=[a-z])', 'l', normalized)
    normalized = re.sub(r'(?<=[A-Za-z])5(?=[A-Za-z])', 's', normalized)
    normalized = re.sub(r'^5(?=[A-Za-z]{3,})', 's', normalized)
    normalized = re.sub(r'^0(?=[A-Za-z]{4,})', 'o', normalized)
    return normalized


def _normalize_transcript_text(text: str, *, remove_repeated_phrases: bool = True) -> str:
    """Normalize transcript text."""
    transcript = str(text or "")

    # 1. Remove spaces between Chinese characters (multiple passes for thoroughness)
    for _ in range(10):
        transcript = re.sub(r'([一-鿿])\s+([一-鿿])', r'\1\2', transcript)

    # 2. Fix spaces around punctuation
    transcript = re.sub(r'\s+([。，！？、：；])', r'\1', transcript)

    # 3. Normalize conservative mixed alnum OCR-like tokens without touching model/version IDs.
    transcript = re.sub(r'\b[A-Za-z0-9]{5,}\b', lambda match: _normalize_confusable_ascii_token(match.group(0)), transcript)

    # 4. Remove consecutive repeated phrases (3-20 characters)
    if remove_repeated_phrases:
        transcript = re.sub(r'([一-鿿]{3,20})\1{1,5}', r'\1', transcript)

    return transcript.strip()

def _join_deepgram_words(words: list[dict]) -> str:
    """Join deepgram words."""
    parts = []
    for word in words:
        token = str(word.get('punctuated_word') or word.get('word') or '').strip()
        if token:
            parts.append(token)
    return ' '.join(parts).strip()


def _deepgram_primary_alternative(data: dict) -> dict:
    """Return the primary Deepgram alternative payload."""
    return data['results']['channels'][0]['alternatives'][0]


def _deepgram_paragraphs(alternative: dict) -> list[dict]:
    """Return normalized paragraph structures from a Deepgram alternative."""
    paragraphs = alternative.get('paragraphs', {}).get('paragraphs', [])
    return paragraphs if isinstance(paragraphs, list) else []


def _deepgram_words(alternative: dict) -> list[dict]:
    """Return normalized word structures from a Deepgram alternative."""
    words = alternative.get('words', [])
    return words if isinstance(words, list) else []


def _deepgram_utterances(alternative: dict) -> list[dict]:
    """Return normalized utterance structures from a Deepgram alternative."""
    utterances = alternative.get('utterances', [])
    return utterances if isinstance(utterances, list) else []


def _inspect_deepgram_payload(data: dict) -> dict:
    """Collect observability fields from a Deepgram payload without changing strategy."""
    alternative = _deepgram_primary_alternative(data)
    paragraphs = _deepgram_paragraphs(alternative)
    words = _deepgram_words(alternative)
    utterances = _deepgram_utterances(alternative)

    paragraph_count = 0
    sentence_count = 0
    sentence_text_count = 0
    timed_sentence_count = 0
    speaker_ids = set()

    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        paragraph_count += 1

        sentences = paragraph.get('sentences', [])
        if not isinstance(sentences, list):
            continue

        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            sentence_count += 1
            if str(sentence.get('text', '')).strip():
                sentence_text_count += 1
            if _coerce_float_or_none(sentence.get('start')) is not None and _coerce_float_or_none(sentence.get('end')) is not None:
                timed_sentence_count += 1
            speaker = sentence.get('speaker')
            if speaker is not None:
                speaker_ids.add(str(speaker))

    transcript = str(alternative.get('transcript', '') or '')
    return {
        "transcript": transcript,
        "transcript_source": "alternative.transcript",
        "paragraph_count": paragraph_count,
        "sentence_count": sentence_count,
        "sentence_text_count": sentence_text_count,
        "timed_sentence_count": timed_sentence_count,
        "word_count": len(words),
        "utterance_count": len([utterance for utterance in utterances if isinstance(utterance, dict)]),
        "has_paragraphs": paragraph_count > 0,
        "has_words": bool(words),
        "has_utterances": bool(utterances),
        "speaker_count": len(speaker_ids) if speaker_ids else 1,
    }


def _normalized_deepgram_text(text: str) -> str:
    """Normalize Deepgram-derived text without aggressive phrase de-duplication."""
    return _normalize_transcript_text(text, remove_repeated_phrases=False)


def _extract_utterance_text(utterance: dict) -> str:
    """Return the best text field from a Deepgram utterance."""
    return str(utterance.get('transcript') or utterance.get('text') or '').strip()


def _build_deepgram_transcript(data: dict, *, prefer_structured_output: bool = DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT) -> dict:
    """Build transcript text from a Deepgram payload with optional structured-field priority."""
    inspection = _inspect_deepgram_payload(data)
    if not prefer_structured_output:
        return {
            "transcript": _normalize_transcript_text(inspection["transcript"]),
            "transcript_source": inspection["transcript_source"],
            "warnings": [],
            "prefer_structured_output": False,
        }

    alternative = _deepgram_primary_alternative(data)
    utterances = _deepgram_utterances(alternative)
    paragraphs = _deepgram_paragraphs(alternative)
    words = _deepgram_words(alternative)

    utterance_blocks = []
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        normalized_text = _normalized_deepgram_text(_extract_utterance_text(utterance))
        if normalized_text:
            utterance_blocks.append(normalized_text)
    if utterance_blocks:
        return {
            "transcript": "\n\n".join(utterance_blocks).strip(),
            "transcript_source": "utterances",
            "warnings": [],
            "prefer_structured_output": True,
        }

    paragraph_blocks = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        sentences = paragraph.get('sentences', [])
        if not isinstance(sentences, list):
            continue
        sentence_texts = []
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            normalized_text = _normalized_deepgram_text(str(sentence.get('text', '')).strip())
            if normalized_text:
                sentence_texts.append(normalized_text)
        if sentence_texts:
            paragraph_blocks.append(_normalized_deepgram_text(" ".join(sentence_texts).strip()))
    if paragraph_blocks:
        return {
            "transcript": "\n\n".join(paragraph_blocks).strip(),
            "transcript_source": "paragraphs.sentences[].text",
            "warnings": ["transcript fell back to paragraphs.sentences[].text because no utterances were available"],
            "prefer_structured_output": True,
        }

    word_transcript = _normalized_deepgram_text(_join_deepgram_words(words))
    if word_transcript:
        return {
            "transcript": word_transcript,
            "transcript_source": "words[].punctuated_word",
            "warnings": ["transcript fell back to words[].punctuated_word because no utterances or sentence text were available"],
            "prefer_structured_output": True,
        }

    transcript = _normalize_transcript_text(inspection["transcript"])
    warnings = []
    if transcript:
        warnings.append("transcript fell back to alternative.transcript because richer Deepgram structures were unavailable")
    return {
        "transcript": transcript,
        "transcript_source": inspection["transcript_source"],
        "warnings": warnings,
        "prefer_structured_output": True,
    }


def _extract_deepgram_segments_with_report(data: dict, *, time_offset: float = 0.0,
                                           source_chunk_index: int = 0,
                                           starting_segment_id: int = 0,
                                           prefer_structured_output: bool = DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT) -> tuple[list[dict], dict]:
    """Extract Deepgram segments plus observability metadata."""
    alternative = _deepgram_primary_alternative(data)
    paragraphs = _deepgram_paragraphs(alternative)
    words = _deepgram_words(alternative)
    utterances = _deepgram_utterances(alternative)

    if prefer_structured_output:
        utterance_segments = []
        next_segment_id = starting_segment_id
        for utterance_index, utterance in enumerate(utterances):
            if not isinstance(utterance, dict):
                continue
            normalized_text = _normalized_deepgram_text(_extract_utterance_text(utterance))
            if not normalized_text:
                continue
            start_time = _coerce_float_or_none(utterance.get('start'))
            end_time = _coerce_float_or_none(utterance.get('end'))
            utterance_segments.append({
                'id': next_segment_id,
                'text': normalized_text,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': utterance.get('speaker'),
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': utterance_index,
                'source_sentence_index': 0,
            })
            next_segment_id += 1

        if utterance_segments:
            return utterance_segments, {
                "segment_source": "utterances",
                "segment_count": len(utterance_segments),
                "paragraph_sentence_count": 0,
                "utterance_segment_count": len(utterance_segments),
                "word_aligned_segment_count": 0,
                "sentence_text_fallback_count": 0,
                "transcript_fallback_used": False,
                "warnings": [],
            }

        sentence_segments = []
        next_segment_id = starting_segment_id
        sentence_count = 0
        for para_index, paragraph in enumerate(paragraphs):
            if not isinstance(paragraph, dict):
                continue
            paragraph_speaker = paragraph.get('speaker')
            sentences = paragraph.get('sentences', [])
            if not isinstance(sentences, list):
                continue
            for sentence_index, sentence in enumerate(sentences):
                if not isinstance(sentence, dict):
                    continue
                sentence_count += 1
                normalized_text = _normalized_deepgram_text(str(sentence.get('text', '')).strip())
                if not normalized_text:
                    continue
                start_time = _coerce_float_or_none(sentence.get('start'))
                end_time = _coerce_float_or_none(sentence.get('end'))
                speaker = sentence.get('speaker')
                if speaker is None:
                    speaker = paragraph_speaker
                sentence_segments.append({
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

        if sentence_segments:
            return sentence_segments, {
                "segment_source": "paragraphs.sentences[].text",
                "segment_count": len(sentence_segments),
                "paragraph_sentence_count": sentence_count,
                "utterance_segment_count": 0,
                "word_aligned_segment_count": 0,
                "sentence_text_fallback_count": len(sentence_segments),
                "transcript_fallback_used": False,
                "warnings": ["segments fell back to paragraphs.sentences[].text because no utterances were available"],
            }

        word_transcript = _normalized_deepgram_text(_join_deepgram_words(words))
        if word_transcript:
            word_starts = [_coerce_float_or_none(word.get('start')) for word in words if isinstance(word, dict)]
            word_ends = [_coerce_float_or_none(word.get('end')) for word in words if isinstance(word, dict)]
            start_time = min((value for value in word_starts if value is not None), default=None)
            end_time = max((value for value in word_ends if value is not None), default=None)
            return [{
                'id': starting_segment_id,
                'text': word_transcript,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': None,
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': 0,
                'source_sentence_index': 0,
            }], {
                "segment_source": "words[].punctuated_word",
                "segment_count": 1,
                "paragraph_sentence_count": 0,
                "utterance_segment_count": 0,
                "word_aligned_segment_count": 1,
                "sentence_text_fallback_count": 0,
                "transcript_fallback_used": False,
                "warnings": ["segments fell back to words[].punctuated_word because no utterances or sentence text were available"],
            }

    segments = []
    next_segment_id = starting_segment_id
    paragraph_sentence_count = 0
    word_aligned_segment_count = 0
    sentence_text_fallback_count = 0
    warnings = []

    for para_index, paragraph in enumerate(paragraphs):
        if not isinstance(paragraph, dict):
            continue
        paragraph_speaker = paragraph.get('speaker')
        sentences = paragraph.get('sentences', [])
        if not isinstance(sentences, list):
            continue

        for sentence_index, sentence in enumerate(sentences):
            if not isinstance(sentence, dict):
                continue
            paragraph_sentence_count += 1
            speaker = sentence.get('speaker')
            if speaker is None:
                speaker = paragraph_speaker
            start_time = _coerce_float_or_none(sentence.get('start'))
            end_time = _coerce_float_or_none(sentence.get('end'))

            matched_words = []
            if start_time is not None and end_time is not None and words:
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    word_start = _coerce_float_or_none(word.get('start'))
                    word_end = _coerce_float_or_none(word.get('end'))
                    if word_start is None or word_end is None:
                        continue
                    if word_end <= start_time - 1e-6 or word_start >= end_time + 1e-6:
                        continue
                    matched_words.append(word)

            if matched_words:
                sentence_text = _join_deepgram_words(matched_words)
                word_aligned_segment_count += 1
            else:
                sentence_text = str(sentence.get('text', '')).strip()
                if sentence_text:
                    sentence_text_fallback_count += 1

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

    transcript_fallback_used = False
    if not segments:
        transcript = _normalize_transcript_text(alternative.get('transcript', ''), remove_repeated_phrases=False)
        if transcript:
            transcript_fallback_used = True
            word_starts = [_coerce_float_or_none(word.get('start')) for word in words if isinstance(word, dict)]
            word_ends = [_coerce_float_or_none(word.get('end')) for word in words if isinstance(word, dict)]
            start_time = min((value for value in word_starts if value is not None), default=None)
            end_time = max((value for value in word_ends if value is not None), default=None)
            segments = [{
                'id': starting_segment_id,
                'text': transcript,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': None,
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': 0,
                'source_sentence_index': 0,
            }]

    if transcript_fallback_used:
        warnings.append("segments fell back to alternative.transcript because no paragraph sentences were usable")
        segment_source = "alternative.transcript"
    elif word_aligned_segment_count and sentence_text_fallback_count:
        warnings.append(
            f"segments fell back to sentence.text for {sentence_text_fallback_count} paragraph sentence(s)"
        )
        segment_source = "paragraph_sentence_mixed"
    elif word_aligned_segment_count:
        segment_source = "paragraph_sentence_word_join"
    elif sentence_text_fallback_count:
        warnings.append(
            f"segments used sentence.text for all {sentence_text_fallback_count} paragraph sentence(s)"
        )
        segment_source = "paragraph_sentence_text"
    else:
        segment_source = "none"

    return segments, {
        "segment_source": segment_source,
        "segment_count": len(segments),
        "paragraph_sentence_count": paragraph_sentence_count,
        "utterance_segment_count": 0,
        "word_aligned_segment_count": word_aligned_segment_count,
        "sentence_text_fallback_count": sentence_text_fallback_count,
        "transcript_fallback_used": transcript_fallback_used,
        "warnings": warnings,
    }


def process_deepgram_payload(data: dict, *, prefer_structured_output: bool = DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT) -> dict:
    """Process deepgram payload."""
    inspection = _inspect_deepgram_payload(data)
    transcript_result = _build_deepgram_transcript(data, prefer_structured_output=prefer_structured_output)
    transcript = transcript_result["transcript"]
    return {
        "transcript": transcript.strip(),
        "speaker_count": inspection["speaker_count"],
        "transcript_source": transcript_result["transcript_source"],
        "paragraph_count": inspection["paragraph_count"],
        "sentence_count": inspection["sentence_count"],
        "sentence_text_count": inspection["sentence_text_count"],
        "timed_sentence_count": inspection["timed_sentence_count"],
        "word_count": inspection["word_count"],
        "utterance_count": inspection["utterance_count"],
        "has_paragraphs": inspection["has_paragraphs"],
        "has_words": inspection["has_words"],
        "has_utterances": inspection["has_utterances"],
        "warnings": list(transcript_result.get("warnings", [])),
        "prefer_structured_output": bool(transcript_result.get("prefer_structured_output", False)),
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


def _is_valid_compact_date_fragment(value: str) -> bool:
    """Return whether an 8-digit string is a real YYYYMMDD calendar date."""
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return False
    try:
        time.strptime(text, "%Y%m%d")
    except ValueError:
        return False
    return True


def _normalize_frontmatter_date(value: str) -> str:
    """Normalize compact YYYYMMDD dates for article frontmatter."""
    text = str(value or "").strip()
    if _is_valid_compact_date_fragment(text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


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


def _load_normalized_document(normalized_document_path: str) -> dict:
    """Load normalized document."""
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

    source_payload = payload.get("source", {}) if isinstance(payload.get("source", {}), dict) else {}
    content = payload.get("content", {}) if isinstance(payload.get("content", {}), dict) else {}
    payload.setdefault("content", content)
    payload["content"]["text"] = _normalize_document_text_for_source(content.get("text", ""), source_payload)

    normalized_segments = []
    for index, raw_segment in enumerate(payload.get("segments", [])):
        if not isinstance(raw_segment, dict):
            continue
        segment_text = _normalize_segment_text_for_source(raw_segment.get("text", ""), source_payload)
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


def _call_llm_api(api_key: str, base_url: str, model: str, messages: list,
                  api_format: str = "openai", max_tokens: int = 8192,
                  temperature: float = 0.3, timeout_sec: int = 120,
                  max_retries: int = 3, backoff_sec: float = 1.5,
                  stream_mode: str = "auto") -> dict:
    """Delegate LLM request orchestration to `kernel.long_text.llm`."""
    return kernel_llm._call_llm_api(
        api_key,
        base_url,
        model,
        messages,
        api_format=api_format,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        backoff_sec=backoff_sec,
        stream_mode=stream_mode,
    )


def detect_audio_content_type(audio_path: str) -> str:
    """Detect audio content type."""
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


def _resolve_deepgram_request_settings(config: dict | None = None, *, language: str = "",
                                       model: str = "", enable_utterances: bool | None = None,
                                       prefer_structured_output: bool | None = None) -> dict:
    """Resolve Deepgram request and transcript strategy settings."""
    config = config or {}
    request_model, model_warning = _resolve_deepgram_model(
        model or config.get("deepgram_model", DEFAULT_DEEPGRAM_MODEL),
    )

    if enable_utterances is None:
        utterances_enabled = _parse_bool(
            config.get("deepgram_enable_utterances", DEFAULT_DEEPGRAM_ENABLE_UTTERANCES),
            DEFAULT_DEEPGRAM_ENABLE_UTTERANCES,
        )
    else:
        utterances_enabled = bool(enable_utterances)

    if prefer_structured_output is None:
        structured_output_enabled = _parse_bool(
            config.get("deepgram_prefer_structured_output", DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT),
            DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT,
        )
    else:
        structured_output_enabled = bool(prefer_structured_output)

    settings = {
        "model": request_model,
        "language": language,
        "diarize": True,
        "punctuate": True,
        "paragraphs": True,
        "smart_format": True,
        "utterances": utterances_enabled,
        "prefer_structured_output": structured_output_enabled,
    }
    if _deepgram_detects_language(language):
        settings["detect_language"] = True
    if model_warning:
        settings["model_warning"] = model_warning
    return settings


def _deepgram_detects_language(language: str) -> bool:
    """Return whether a Deepgram request should detect the language itself."""
    return str(language or "").strip().lower() in {"", "auto"}


def _deepgram_detected_language(payload: dict) -> str:
    """Return the language Deepgram detected for a payload, if it reported one."""
    channels = ((payload or {}).get("results") or {}).get("channels") or []
    if channels and isinstance(channels[0], dict):
        return str(channels[0].get("detected_language") or "").strip()
    return ""


def _call_deepgram_api_once(audio_path: str, api_key: str, language: str,
                            timeout: int = 300, *, model: str = DEFAULT_DEEPGRAM_MODEL,
                            enable_utterances: bool = DEFAULT_DEEPGRAM_ENABLE_UTTERANCES) -> dict:
    """Perform one Deepgram API request."""
    import urllib.request

    audio_file = Path(audio_path)
    if not audio_file.exists():
        print(f"Error: Audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    request_model, model_warning = _resolve_deepgram_model(model)
    if model_warning:
        print(f"Warning: {model_warning}", file=sys.stderr)

    if _deepgram_detects_language(language):
        language_param = "detect_language=true"
    else:
        language_param = f"language={urllib.parse.quote(str(language))}"
    params = (
        f"model={urllib.parse.quote(request_model)}&{language_param}"
        "&diarize=true&punctuate=true&paragraphs=true&smart_format=true"
    )
    if enable_utterances:
        params += "&utterances=true"
    url = f"https://api.deepgram.com/v1/listen?{params}"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": detect_audio_content_type(audio_path),
        "User-Agent": "yt-transcript/4.0",
    }

    data = audio_file.read_bytes()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _is_retryable_deepgram_error(error: Exception) -> bool:
    """Return whether a Deepgram request error is worth retrying once or twice."""
    import urllib.error

    if isinstance(error, (socket.timeout, TimeoutError)):
        return True
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 409, 425, 429, 500, 502, 503, 504}
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, 'reason', '')
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        reason_text = str(reason).lower()
        return any(fragment in reason_text for fragment in (
            'timed out',
            'timeout',
            'temporarily unavailable',
            'connection reset',
            'connection aborted',
            'connection refused',
        ))
    error_text = str(error).lower()
    return any(fragment in error_text for fragment in (
        'timed out',
        'timeout',
        'temporarily unavailable',
        'connection reset',
        'connection aborted',
    ))


def _call_deepgram_api(audio_path: str, api_key: str, language: str,
                       timeout: int = 300, request_retries: int = DEFAULT_DEEPGRAM_REQUEST_RETRIES,
                       retry_backoff_sec: float = DEFAULT_DEEPGRAM_RETRY_BACKOFF_SEC, *,
                       model: str = DEFAULT_DEEPGRAM_MODEL,
                       enable_utterances: bool = DEFAULT_DEEPGRAM_ENABLE_UTTERANCES) -> dict:
    """Call Deepgram with bounded retries for transient timeout/network failures."""
    import urllib.error

    attempts = max(1, request_retries + 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return _call_deepgram_api_once(
                audio_path,
                api_key=api_key,
                language=language,
                timeout=timeout,
                model=model,
                enable_utterances=enable_utterances,
            )
        except urllib.error.HTTPError as error:
            last_error = error
            if attempt < attempts and _is_retryable_deepgram_error(error):
                print(
                    f"Warning: Deepgram API returned HTTP {error.code} on attempt {attempt}/{attempts}; retrying in {retry_backoff_sec:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(max(0.0, retry_backoff_sec))
                continue
            error_body = error.read().decode("utf-8", errors="replace")
            print(f"Error: Deepgram API returned HTTP {error.code}: {error_body}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as error:
            last_error = error
            if attempt < attempts and _is_retryable_deepgram_error(error):
                print(
                    f"Warning: Deepgram API network error on attempt {attempt}/{attempts}: {error.reason}. Retrying in {retry_backoff_sec:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(max(0.0, retry_backoff_sec))
                continue
            print(f"Error: Cannot reach Deepgram API: {error.reason}", file=sys.stderr)
            sys.exit(1)
        except Exception as error:
            last_error = error
            if attempt < attempts and _is_retryable_deepgram_error(error):
                print(
                    f"Warning: Deepgram API call failed on attempt {attempt}/{attempts}: {error}. Retrying in {retry_backoff_sec:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(max(0.0, retry_backoff_sec))
                continue
            print(f"Error: Deepgram API call failed: {error}", file=sys.stderr)
            sys.exit(1)

    print(f"Error: Deepgram API call failed: {last_error}", file=sys.stderr)
    sys.exit(1)


def _deepgram_chunk_json_path(output_json: str, chunk_count: int, chunk_index: int) -> Path | None:
    """Return the per-chunk Deepgram JSON path when JSON output is enabled."""
    if not output_json:
        return None
    output_base = Path(output_json)
    if chunk_count == 1:
        return output_base
    return output_base.with_name(f"{output_base.stem}_chunk_{chunk_index:03d}{output_base.suffix or '.json'}")


def _sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest for a file path."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _deepgram_request_cache_fingerprint(request_settings: dict | None = None) -> dict:
    """Return the stable subset of Deepgram request settings that affect payloads."""
    settings = request_settings if isinstance(request_settings, dict) else {}
    return {
        "model": str(settings.get("model", "")).strip(),
        "language": str(settings.get("language", "")).strip(),
        "diarize": bool(settings.get("diarize", False)),
        "punctuate": bool(settings.get("punctuate", False)),
        "paragraphs": bool(settings.get("paragraphs", False)),
        "smart_format": bool(settings.get("smart_format", False)),
        "utterances": bool(settings.get("utterances", False)),
    }


def _build_deepgram_chunk_cache_metadata(chunk_path: str | Path, *, chunk_count: int,
                                         chunk_index: int, request_settings: dict | None = None) -> dict:
    """Build the validation metadata stored alongside a Deepgram chunk payload."""
    path = Path(chunk_path)
    stat = path.stat()
    return {
        "chunk_index": int(chunk_index),
        "chunk_count": int(chunk_count),
        "chunk_name": path.name,
        "chunk_size_bytes": int(stat.st_size),
        "chunk_sha256": _sha256_file(path),
        "request_fingerprint": _deepgram_request_cache_fingerprint(request_settings),
    }


def _build_deepgram_chunk_cache_envelope(payload: dict, *, cache_metadata: dict | None = None) -> dict:
    """Wrap a raw Deepgram payload with resumable cache metadata."""
    return {
        "schema_version": DEEPGRAM_CHUNK_CACHE_SCHEMA_VERSION,
        "format": DEEPGRAM_CHUNK_CACHE_FORMAT,
        "cache_metadata": dict(cache_metadata or {}),
        "payload": payload,
    }


def _deepgram_chunk_cache_matches_expected(cache_metadata: dict | None, expected_metadata: dict | None) -> str:
    """Return an empty string when chunk-cache metadata matches; otherwise a reason."""
    actual = cache_metadata if isinstance(cache_metadata, dict) else {}
    expected = expected_metadata if isinstance(expected_metadata, dict) else {}
    if not actual:
        return "cache_metadata_missing"
    for key in ("chunk_index", "chunk_count", "chunk_size_bytes", "chunk_sha256"):
        if actual.get(key) != expected.get(key):
            return f"{key}_mismatch"
    if actual.get("request_fingerprint") != expected.get("request_fingerprint"):
        return "request_fingerprint_mismatch"
    return ""


def _load_existing_deepgram_chunk_payload(json_path: Path, *, expected_metadata: dict | None = None) -> tuple[dict | None, str]:
    """Best-effort load for a previously completed and validated Deepgram chunk payload."""
    if not json_path.exists():
        return None, "missing"
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "unreadable_or_invalid_json"
    if not isinstance(payload, dict):
        return None, "not_a_json_object"
    if str(payload.get("format", "")).strip() != DEEPGRAM_CHUNK_CACHE_FORMAT:
        return None, "legacy_or_unversioned_payload"
    cached_payload = payload.get("payload")
    if not isinstance(cached_payload, dict):
        return None, "payload_missing_or_invalid"
    mismatch_reason = _deepgram_chunk_cache_matches_expected(
        payload.get("cache_metadata"),
        expected_metadata,
    )
    if mismatch_reason:
        return None, mismatch_reason
    return cached_payload, ""


def transcribe_deepgram(audio_path: str, language: str, config_path: str = None,
                        api_key: str = "", max_size_mb: float = 10.0,
                        max_deviation_sec: float = 60.0, timeout: int = 300,
                        output_json: str = "", output_text: str = "",
                        output_segments: str = "", deepgram_model: str = "",
                        enable_utterances: bool | None = None,
                        prefer_structured_output: bool | None = None,
                        resume_existing_chunks: bool = False) -> dict:
    """
    Transcribe audio via Deepgram. Automatically splits large files and merges
    chunk transcripts into one raw transcript output.

    The returned result now also includes lightweight observability fields such
    as paragraph/sentence/word counts, per-chunk reports, and warnings when
    segment extraction falls back to less structured Deepgram fields.
    """
    config = load_config(config_path, allow_missing=(config_path is None))
    request_settings = _resolve_deepgram_request_settings(
        config,
        language=language,
        model=deepgram_model,
        enable_utterances=enable_utterances,
        prefer_structured_output=prefer_structured_output,
    )

    if not api_key:
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
    chunk_reports = []
    chunk_cache_metadatas = []
    warnings = []
    total_paragraph_count = 0
    total_sentence_count = 0
    total_sentence_text_count = 0
    total_timed_sentence_count = 0
    total_word_count = 0
    total_utterance_count = 0
    detected_languages = []
    if request_settings.get("model_warning"):
        warnings.append(str(request_settings["model_warning"]))

    for idx, chunk_path in enumerate(chunk_paths):
        chunk_json_path = _deepgram_chunk_json_path(output_json, len(chunk_paths), idx)
        cache_metadata = None
        if chunk_json_path is not None:
            try:
                cache_metadata = _build_deepgram_chunk_cache_metadata(
                    chunk_path,
                    chunk_count=len(chunk_paths),
                    chunk_index=idx,
                    request_settings=request_settings,
                )
            except OSError as error:
                print(f"Error: Cannot fingerprint Deepgram chunk {chunk_path}: {error}", file=sys.stderr)
                sys.exit(2)
        payload = None
        reused_existing_payload = False
        if resume_existing_chunks and chunk_json_path is not None:
            payload, reuse_reason = _load_existing_deepgram_chunk_payload(
                chunk_json_path,
                expected_metadata=cache_metadata,
            )
            if payload is not None:
                reused_existing_payload = True
                print(
                    f"Info: Reusing existing Deepgram chunk payload {chunk_json_path.name} ({idx + 1}/{len(chunk_paths)})",
                    file=sys.stderr,
                )
            else:
                if reuse_reason == "missing":
                    print(
                        f"Info: No reusable Deepgram chunk payload for chunk {idx + 1}/{len(chunk_paths)}; requesting it now",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"Info: Existing Deepgram chunk payload {chunk_json_path.name} is not reusable ({reuse_reason}); requesting it now",
                        file=sys.stderr,
                    )

        if payload is None:
            payload = _call_deepgram_api(
                chunk_path,
                api_key=api_key,
                language=language,
                timeout=timeout,
                model=request_settings["model"],
                enable_utterances=request_settings["utterances"],
            )
        raw_payloads.append(payload)
        chunk_detected_language = _deepgram_detected_language(payload)
        if chunk_detected_language:
            detected_languages.append(chunk_detected_language)
        processed = process_deepgram_payload(
            payload,
            prefer_structured_output=request_settings["prefer_structured_output"],
        )
        transcripts.append(processed["transcript"])
        speaker_count = max(speaker_count, processed["speaker_count"])
        total_paragraph_count += _parse_int(processed.get("paragraph_count"), 0)
        total_sentence_count += _parse_int(processed.get("sentence_count"), 0)
        total_sentence_text_count += _parse_int(processed.get("sentence_text_count"), 0)
        total_timed_sentence_count += _parse_int(processed.get("timed_sentence_count"), 0)
        total_word_count += _parse_int(processed.get("word_count"), 0)
        total_utterance_count += _parse_int(processed.get("utterance_count"), 0)
        warnings.extend(
            f"Chunk {idx}: {warning}" for warning in processed.get("warnings", [])
        )

        chunk_report = {
            "chunk_index": idx,
            "transcript_source": str(processed.get("transcript_source", "alternative.transcript") or "alternative.transcript"),
            "paragraph_count": _parse_int(processed.get("paragraph_count"), 0),
            "sentence_count": _parse_int(processed.get("sentence_count"), 0),
            "sentence_text_count": _parse_int(processed.get("sentence_text_count"), 0),
            "timed_sentence_count": _parse_int(processed.get("timed_sentence_count"), 0),
            "word_count": _parse_int(processed.get("word_count"), 0),
            "utterance_count": _parse_int(processed.get("utterance_count"), 0),
            "prefer_structured_output": _parse_bool(processed.get("prefer_structured_output"), False),
            "reused_existing_payload": reused_existing_payload,
        }

        if output_segments:
            chunk_segments, segment_report = _extract_deepgram_segments_with_report(
                payload,
                time_offset=chunk_offsets[idx],
                source_chunk_index=idx,
                starting_segment_id=len(segments),
                prefer_structured_output=request_settings["prefer_structured_output"],
            )
            segments.extend(chunk_segments)
            chunk_report.update({
                "segment_source": segment_report["segment_source"],
                "segment_count": segment_report["segment_count"],
                "paragraph_sentence_count": segment_report["paragraph_sentence_count"],
                "utterance_segment_count": segment_report["utterance_segment_count"],
                "word_aligned_segment_count": segment_report["word_aligned_segment_count"],
                "sentence_text_fallback_count": segment_report["sentence_text_fallback_count"],
                "transcript_fallback_used": segment_report["transcript_fallback_used"],
            })
            warnings.extend(
                f"Chunk {idx}: {warning}" for warning in segment_report.get("warnings", [])
            )

        chunk_reports.append(chunk_report)
        chunk_cache_metadatas.append(cache_metadata or {})

        if chunk_json_path is not None:
            json_path = chunk_json_path
            json_path.write_text(
                json.dumps(
                    _build_deepgram_chunk_cache_envelope(payload, cache_metadata=cache_metadata),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            json_outputs.append(str(json_path))

    transcript = "\n\n".join(t for t in transcripts if t).strip()
    used_split_mode = len(chunk_paths) > 1
    detected_language = Counter(detected_languages).most_common(1)[0][0] if detected_languages else ""
    effective_language = detected_language if _deepgram_detects_language(language) else language
    transcript_sources = {report.get("transcript_source", "alternative.transcript") for report in chunk_reports}
    transcript_source = transcript_sources.pop() if len(transcript_sources) == 1 else "mixed"
    warnings = list(dict.fromkeys(warnings))

    if output_json and used_split_mode:
        output_base = Path(output_json)
        output_base.write_text(
            json.dumps(
                {
                    "deepgram_request": request_settings,
                    "chunk_count": len(chunk_paths),
                    "split_points": split_points,
                    "chunk_reports": chunk_reports,
                    "warnings": warnings,
                    "chunks": [
                        {
                            "index": idx,
                            "chunk_path": str(chunk_path),
                            "cache_metadata": cache_metadata,
                            "payload": payload,
                        }
                        for idx, (chunk_path, cache_metadata, payload) in enumerate(
                            zip(chunk_paths, chunk_cache_metadatas, raw_payloads)
                        )
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
                    "deepgram_request": request_settings,
                    "language": effective_language,
                    "used_split_mode": used_split_mode,
                    "chunk_count": len(chunk_paths),
                    "split_points": split_points,
                    "chunk_reports": chunk_reports,
                    "warnings": warnings,
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
        "language": effective_language,
        "detected_language": detected_language,
        "deepgram_request": request_settings,
        "transcript_source": transcript_source,
        "chunk_count": len(chunk_paths),
        "json_outputs": json_outputs,
        "split_points": split_points,
        "used_split_mode": used_split_mode,
        "paragraph_count": total_paragraph_count,
        "sentence_count": total_sentence_count,
        "sentence_text_count": total_sentence_text_count,
        "timed_sentence_count": total_timed_sentence_count,
        "word_count": total_word_count,
        "utterance_count": total_utterance_count,
        "chunk_reports": chunk_reports,
        "warnings": warnings,
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
    frontmatter_date = _normalize_frontmatter_date(date)
    frontmatter_created = _normalize_frontmatter_date(created)
    
    # Build YAML frontmatter
    frontmatter_lines = [
        "---",
        f"title: {_yaml_string(title)}",
        f"source: {_yaml_string(source)}",
        f"url: {_yaml_string(source)}",
        f"channel: {_yaml_string(channel)}",
        f"author: {_yaml_string(channel)}",
    ]
    if frontmatter_date:
        frontmatter_lines.append(f"date: {_yaml_string(frontmatter_date)}")
    frontmatter_lines.extend([
        f"created: {_yaml_string(frontmatter_created)}",
        "type: video-transcript",
        f"bilingual: {bilingual_str}",
        f"duration: {_yaml_string(f'{duration_min}m')}",
        f"transcript_source: {_yaml_string(transcript_source)}",
        "tags: [youtube, video-transcript]",
        f"via: {_yaml_string('youtube')}",
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


def _has_unbalanced_terminal_markers(text: str) -> bool:
    """Return whether the trailing line has obvious unclosed quotes or brackets."""
    sample = str(text or "")
    pairs = [("(", ")"), ("（", "）"), ("[", "]"), ("{", "}"), ('"', '"'), ("“", "”"), ("‘", "’")]
    for opener, closer in pairs:
        if opener == closer:
            if sample.count(opener) % 2 == 1:
                return True
            continue
        if sample.count(opener) > sample.count(closer):
            return True
    return False


def _line_looks_truncated(last_line: str) -> bool:
    """Return whether the final visible line has strong cut-off signals."""
    text = str(last_line or "").strip()
    if not text:
        return True

    proper_endings = ('.', '!', '?', '。', '！', '？', '*', '`', '"', ')', '）', '」', '>', '-', ':', '：')
    if text.endswith(proper_endings) or text.startswith('#') or len(text) < 10:
        return False

    unfinished_tail_patterns = [
        r'[，,、；;—-]$',
        r'(?:and|or|but|because|so|to|with|for|of|in|on|at|by|the|a|an)$',
        r'(?:的|了|和|与|并|但|而|及|在|把|将|对|让|给|从|向|为|等)$',
    ]
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in unfinished_tail_patterns):
        return True
    if _has_unbalanced_terminal_markers(text):
        return True
    return len(text) > 120 and text[-1].isalnum()


QUALITY_PUNCTUATION_CHARS = set(",.;:!?，。！？、；：()[]{}\"'“”‘’`")
QUALITY_SENTENCE_ENDINGS = ("。", "！", "？", "!", "?", "；", ";")
QUALITY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
QUALITY_CJK_SPACE_PATTERNS = (
    re.compile(r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+(?=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])"),
    re.compile(r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+(?=[，。！？、；：,.!?;:])"),
    re.compile(r"(?<=[（《「『【(])\s+(?=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])"),
    re.compile(r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+(?=[）》」』】)])"),
)


def _count_quality_chars(text: str, *, include_punctuation: bool = False) -> int:
    """Count non-space characters for quality heuristics."""
    return sum(
        1
        for char in str(text or "")
        if not char.isspace() and (include_punctuation or char not in QUALITY_PUNCTUATION_CHARS)
    )


def _count_cjk_chars(text: str) -> int:
    """Count CJK-family characters."""
    return sum(1 for char in str(text or "") if _is_cjk_family_char(char))


def _tokenize_quality_text(text: str) -> list[str]:
    """Tokenize text into Latin words and single CJK-family characters."""
    tokens = []
    for token in QUALITY_TOKEN_RE.findall(str(text or "")):
        tokens.append(token.lower() if token.isascii() else token)
    return tokens


def _is_quality_fragment_paragraph(text: str) -> bool:
    """Return whether a paragraph looks like a short subtitle fragment."""
    block = str(text or "").strip()
    if not block or re.match(r"^(?:[-*+]\s|\d+\.\s)", block):
        return False

    meaningful_chars = _count_quality_chars(block)
    cjk_chars = _count_cjk_chars(block)
    if cjk_chars < 4:
        return False
    if meaningful_chars <= 10:
        return True
    return meaningful_chars <= 22 and not block.endswith(QUALITY_SENTENCE_ENDINGS)


def _is_strong_quality_overlap(text: str) -> bool:
    """Return whether an adjacent overlap is suspicious enough to warn about."""
    candidate = str(text or "").strip()
    if not candidate:
        return False

    meaningful_chars = _count_quality_chars(candidate)
    if any(_is_cjk_family_char(char) for char in candidate):
        return meaningful_chars >= 4
    return meaningful_chars >= 12 and len(_tokenize_quality_text(candidate)) >= 3


def _count_chunk_seam_warnings(paragraphs: list[str]) -> int:
    """Count duplicated adjacent paragraph seams that survived post-merge cleanup."""
    warning_count = 0
    normalized_blocks = [re.sub(r"\s+", " ", block.strip()) for block in paragraphs if block.strip()]
    for previous, current in zip(normalized_blocks, normalized_blocks[1:]):
        if previous == current:
            warning_count += 1
            continue
        overlap = kernel_overlap.find_leading_overlap(previous, current)
        if _is_strong_quality_overlap(overlap):
            warning_count += 1
    return warning_count


def _compute_duplicate_ngram_metrics(paragraphs: list[str], *, ngram_size: int = 5) -> dict:
    """Measure repeated phrase density inside paragraph bodies."""
    windows = []
    for block in paragraphs:
        tokens = _tokenize_quality_text(block)
        if len(tokens) < ngram_size:
            continue
        for idx in range(len(tokens) - ngram_size + 1):
            window = tuple(tokens[idx:idx + ngram_size])
            if len(set(window)) < 2:
                continue
            windows.append(window)

    total_windows = len(windows)
    if total_windows == 0:
        return {
            "duplicate_ngram_ratio": 0.0,
            "duplicate_ngram_count": 0,
            "duplicate_ngram_windows": 0,
        }

    counts = Counter(windows)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    ratio = duplicate_count / total_windows if total_windows else 0.0
    return {
        "duplicate_ngram_ratio": round(ratio, 3),
        "duplicate_ngram_count": duplicate_count,
        "duplicate_ngram_windows": total_windows,
    }


def _compute_cjk_readability_metrics(body_paragraphs: list[str]) -> dict:
    """Compute advisory readability signals for Chinese-heavy body text."""
    cjk_body_paragraphs = [block for block in body_paragraphs if _count_cjk_chars(block) >= 4]
    cjk_text = "\n\n".join(cjk_body_paragraphs)
    cjk_chars = _count_cjk_chars(cjk_text)
    cjk_applicable = cjk_chars >= 24 and len(cjk_body_paragraphs) >= 2

    spacing_anomaly_count = sum(
        1
        for pattern in QUALITY_CJK_SPACE_PATTERNS
        for _ in pattern.finditer(cjk_text)
    )
    cjk_space_ratio = spacing_anomaly_count / cjk_chars if cjk_chars else 0.0

    short_paragraph_count = sum(1 for block in cjk_body_paragraphs if _is_quality_fragment_paragraph(block))
    short_paragraph_ratio = short_paragraph_count / len(cjk_body_paragraphs) if cjk_body_paragraphs else 0.0

    punctuation_count = sum(1 for char in cjk_text if char in QUALITY_PUNCTUATION_CHARS)
    meaningful_chars = _count_quality_chars(cjk_text)
    punctuation_density = punctuation_count / meaningful_chars if meaningful_chars else 0.0

    duplicate_metrics = _compute_duplicate_ngram_metrics(cjk_body_paragraphs)
    cjk_spacing_ok = (not cjk_applicable) or not (
        spacing_anomaly_count >= 2 and cjk_space_ratio > 0.01
    )
    duplicate_ngram_ok = (not cjk_applicable) or not (
        duplicate_metrics["duplicate_ngram_count"] >= 4 and duplicate_metrics["duplicate_ngram_ratio"] > 0.06
    )
    short_paragraph_ok = (not cjk_applicable) or not (
        len(cjk_body_paragraphs) >= 4
        and short_paragraph_count >= 4
        and short_paragraph_ratio > 0.45
    )
    punctuation_density_ok = (not cjk_applicable) or not (
        cjk_chars >= 120 and punctuation_density < 0.01
    )

    return {
        "cjk_readability_applicable": cjk_applicable,
        "cjk_body_paragraph_count": len(cjk_body_paragraphs),
        "cjk_body_char_count": cjk_chars,
        "cjk_spacing_anomaly_count": spacing_anomaly_count,
        "cjk_space_ratio": round(cjk_space_ratio, 3),
        "cjk_spacing_ok": cjk_spacing_ok,
        "duplicate_ngram_ratio": duplicate_metrics["duplicate_ngram_ratio"],
        "duplicate_ngram_count": duplicate_metrics["duplicate_ngram_count"],
        "duplicate_ngram_windows": duplicate_metrics["duplicate_ngram_windows"],
        "duplicate_ngram_ok": duplicate_ngram_ok,
        "short_paragraph_count": short_paragraph_count,
        "short_paragraph_ratio": round(short_paragraph_ratio, 3),
        "fragment_paragraph_ratio": round(short_paragraph_ratio, 3),
        "short_paragraph_ratio_ok": short_paragraph_ok,
        "punctuation_density": round(punctuation_density, 3),
        "punctuation_density_ok": punctuation_density_ok,
    }


def _count_bilingual_paragraph_pairs(paragraphs: list[str]) -> int:
    """Count adjacent English-to-Chinese body paragraph pairs."""
    normalized_blocks = [str(block or "").strip() for block in paragraphs if str(block or "").strip()]
    paired_blocks = 0
    for first, second in zip(normalized_blocks, normalized_blocks[1:]):
        first_has_english = any(ch.isascii() and ch.isalpha() for ch in first)
        second_has_chinese = any('\u4e00' <= ch <= '\u9fff' for ch in second)
        if first_has_english and second_has_chinese:
            paired_blocks += 1
    return paired_blocks


def _load_quality_glossary_payload(*, work_dir: str = "", glossary_path: str = "") -> dict:
    """Load glossary payload for final quality verification."""
    explicit_path_text = str(glossary_path or "").strip()
    if explicit_path_text:
        explicit_path = Path(explicit_path_text).expanduser()
        try:
            payload = json.loads(explicit_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    resolved_work_dir = str(work_dir or "").strip()
    if resolved_work_dir:
        return kernel_glossary.load_glossary(resolved_work_dir)
    return {}


def verify_quality(optimized_text_path: str, raw_text_path: str = None,
                   bilingual: bool = False, work_dir: str = "",
                   glossary_path: str = "", glossary_max_terms: int = 12) -> dict:
    """
    Verify quality of optimized text file with structural checks.
    
    Pure file-based validation. The Agent only reads the JSON report,
    never the actual text content. Checks:
    1. File exists and is non-empty
    2. Has section headers (##)
    3. No abrupt truncation (last line is complete)
    4. Bilingual balance (Chinese char ratio in expected range)
    5. Chinese readability warnings (spacing, duplication, paragraph fragmentation)
    6. Size ratio vs raw text (if raw_text_path provided)
    7. Glossary drift warnings (if glossary context is available)
    
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
    lines = [l for l in text.strip().split("\n") if l.strip()]
    if lines:
        last_line = lines[-1].strip()
        checks["no_truncation"] = not _line_looks_truncated(last_line)
        if not checks["no_truncation"]:
            warnings.append(f'Possible truncation: last line looks incomplete: "{last_line[-50:]}"')
    else:
        checks["no_truncation"] = False
        warnings.append("No non-empty lines found")

    chunk_seam_warning_count = _count_chunk_seam_warnings(body_paragraphs)
    checks["chunk_seam_warning_count"] = chunk_seam_warning_count
    checks["chunk_seam_duplication_ok"] = chunk_seam_warning_count == 0
    if chunk_seam_warning_count:
        warnings.append(
            "Chunk seam duplication signals detected between adjacent paragraphs "
            f"({chunk_seam_warning_count} suspect boundary/boundaries)"
        )

    body_char_count = sum(len(block) for block in body_paragraphs)
    header_density = len(section_headers) / len(body_paragraphs) if body_paragraphs else float(len(section_headers))
    avg_chars_per_section = body_char_count / len(section_headers) if section_headers else float(body_char_count)
    checks["header_density"] = round(header_density, 3)
    checks["avg_chars_per_section"] = round(avg_chars_per_section, 1)
    checks["header_fragment_balance_ok"] = not (
        len(section_headers) >= 4
        and len(body_paragraphs) >= 4
        and header_density > 0.6
        and avg_chars_per_section < 220
    )
    if not checks["header_fragment_balance_ok"]:
        warnings.append(
            "Header density looks unusually high for the available body text "
            f"({len(section_headers)} sections across {len(body_paragraphs)} body paragraphs)"
        )

    cjk_readability_checks = _compute_cjk_readability_metrics(body_paragraphs)
    checks.update(cjk_readability_checks)
    if not checks["cjk_spacing_ok"]:
        warnings.append(
            "Chinese spacing anomalies detected "
            f"({checks['cjk_spacing_anomaly_count']} issue(s), ratio {checks['cjk_space_ratio']:.3f})"
        )
    if not checks["duplicate_ngram_ok"]:
        warnings.append(
            "Repeated phrase density is high in Chinese body text "
            f"(ratio {checks['duplicate_ngram_ratio']:.3f})"
        )
    if not checks["short_paragraph_ratio_ok"]:
        warnings.append(
            "Too many short Chinese body paragraphs look like subtitle fragments "
            f"({checks['short_paragraph_count']} of {checks['cjk_body_paragraph_count']})"
        )
    if not checks["punctuation_density_ok"]:
        warnings.append(
            "Chinese punctuation density looks unusually low "
            f"(density {checks['punctuation_density']:.3f})"
        )

    checks["glossary_drift_applicable"] = False
    checks["glossary_selected_term_count"] = 0
    checks["glossary_matched_term_count"] = 0
    checks["glossary_drift_count"] = 0
    checks["glossary_preservation_ratio"] = 1.0
    checks["glossary_drift_ok"] = True

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
        paired_blocks = _count_bilingual_paragraph_pairs(text_blocks)
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

                    semantic_evaluation = kernel_semantic.evaluate_semantic_anchors(raw_text, text, max_items=12)
                    checks["semantic_anchor_count"] = len(semantic_evaluation["anchors"].get("ordered", []))
                    checks["semantic_missing_count"] = len(semantic_evaluation["missing_anchors"])
                    checks["semantic_anchor_coverage_ok"] = len(semantic_evaluation["missing_anchors"]) == 0
                    if semantic_evaluation["missing_anchors"]:
                        warnings.extend(semantic_evaluation["warnings"])

                    glossary_payload = _load_quality_glossary_payload(
                        work_dir=work_dir,
                        glossary_path=glossary_path,
                    )
                    glossary_evaluation = kernel_glossary.evaluate_glossary_drift(
                        glossary_payload,
                        raw_text,
                        text,
                        max_terms=max(0, int(glossary_max_terms)),
                    ) if glossary_payload else {
                        "warnings": [],
                        "selected_terms": [],
                        "preserved_terms": [],
                        "missing_terms": [],
                        "glossary_drift_count": 0,
                        "preservation_ratio": 1.0,
                    }
                    selected_terms = glossary_evaluation.get("selected_terms", []) if isinstance(glossary_evaluation.get("selected_terms", []), list) else []
                    preserved_terms = glossary_evaluation.get("preserved_terms", []) if isinstance(glossary_evaluation.get("preserved_terms", []), list) else []
                    checks["glossary_drift_applicable"] = bool(selected_terms)
                    checks["glossary_selected_term_count"] = len(selected_terms)
                    checks["glossary_matched_term_count"] = len(preserved_terms)
                    checks["glossary_drift_count"] = int(glossary_evaluation.get("glossary_drift_count", 0) or 0)
                    checks["glossary_preservation_ratio"] = float(glossary_evaluation.get("preservation_ratio", 1.0) or 0.0)
                    checks["glossary_drift_ok"] = checks["glossary_drift_count"] == 0
                    if glossary_evaluation["warnings"]:
                        warnings.extend(glossary_evaluation["warnings"])
            except Exception:
                pass  # Non-critical, skip if raw text unreadable

    window_state = kernel_window.load_window_state(work_dir) if work_dir else {}
    if window_state:
        window_coverage = kernel_window.coverage_report(window_state)
        checks["window_coverage_ratio"] = window_coverage["coverage_ratio"]
        checks["window_uncovered_ranges"] = window_coverage["uncovered_ranges"]
        checks["window_low_output_ratio_paragraphs"] = window_coverage["low_output_ratio_paragraphs"]
        checks["window_fallback_paragraphs"] = window_coverage["fallback_paragraphs"]
        if window_coverage["uncovered_ranges"]:
            preview = ", ".join(window_coverage["uncovered_ranges"][:5])
            warnings.append(f"Window run left source sentences without output: {preview}")
        if window_coverage["low_output_ratio_paragraphs"]:
            preview = ", ".join(item["sentences"] for item in window_coverage["low_output_ratio_paragraphs"][:5])
            warnings.append(f"Window paragraphs much shorter than their source (possible skipped content): {preview}")
    
    # Overall result
    passed = len(hard_failures) == 0
    
    return {
        "passed": passed,
        "checks": checks,
        "warnings": warnings,
        "hard_failures": hard_failures,
    }


NORMALIZED_DOCUMENT_SCHEMA_VERSION = 1
NORMALIZED_DOCUMENT_FORMAT = "yt_transcript.normalized_document/v1"


def _normalize_text_body(text: str) -> str:
    """Normalize text body."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _is_youtube_subtitle_source(source_payload: dict) -> bool:
    """Return whether the normalized document source came from YouTube subtitles."""
    if not isinstance(source_payload, dict):
        return False
    source_type = str(source_payload.get("type", "")).strip().lower()
    subtitle_source = str(source_payload.get("subtitle_source", "")).strip().lower()
    return source_type == "youtube" and "subtitle" in subtitle_source


def _normalize_document_text_for_source(text: str, source_payload: dict) -> str:
    """Normalize document-level text according to the source family."""
    if _is_youtube_subtitle_source(source_payload):
        return _normalize_subtitle_text(text)
    return _normalize_text_body(text)


def _normalize_segment_text_for_source(text: str, source_payload: dict) -> str:
    """Normalize segment-level text according to the source family."""
    if _is_youtube_subtitle_source(source_payload):
        return _normalize_subtitle_segment_text(text)
    return " ".join(_normalize_text_body(text).split())


def _prefer_text_chunking_for_source(source_payload: dict, workflow_payload: dict) -> bool:
    """Return whether chunking should prefer text despite timed segments."""
    if not _is_youtube_subtitle_source(source_payload):
        return False
    mode = str((workflow_payload or {}).get("mode", "")).strip().lower()
    return mode == "chinese"


def _subtitle_quality_penalty(density: float, *, safe: float, scale: float, max_penalty: float) -> float:
    """Convert a cleanup density into a bounded subtitle-quality penalty."""
    bounded_density = max(0.0, _parse_float(density, 0.0))
    return round(min(max_penalty, max(0.0, bounded_density - safe) * scale), 4)


def _classify_subtitle_quality_band(score: float) -> str:
    """Return a stable quality band for subtitle-derived source text."""
    bounded_score = max(0.0, min(1.0, _parse_float(score, 0.0)))
    if bounded_score >= 0.92:
        return "excellent"
    if bounded_score >= 0.78:
        return "good"
    if bounded_score >= 0.58:
        return "fair"
    if bounded_score >= 0.35:
        return "poor"
    return "critical"


def _default_subtitle_quality_signal() -> dict:
    """Return an empty subtitle-quality payload for non-applicable routes."""
    return {
        "applicable": False,
        "evaluated": False,
        "subtitle_quality_score": None,
        "subtitle_quality_band": "",
        "subtitle_quality_reasons": [],
        "overlap_reduction_count": 0,
        "metrics": {
            "cue_count": 0,
            "line_count": 0,
            "fragment_line_count": 0,
            "fragment_line_ratio": 0.0,
            "avg_line_char_count": 0.0,
            "exact_duplicate_cue_ratio": 0.0,
            "overlap_reduction_ratio": 0.0,
            "spacing_cleanup_ratio": 0.0,
            "markup_cleanup_ratio": 0.0,
            "empty_cue_ratio": 0.0,
        },
    }


def _evaluate_subtitle_quality(normalized_text: str, cleanup_diagnostics: dict | None = None, *,
                               source_payload: dict | None = None, workflow_payload: dict | None = None,
                               segment_count: int = 0) -> dict:
    """Build explainable quality signals for subtitle-derived YouTube source text."""
    source_payload = source_payload if isinstance(source_payload, dict) else {}
    workflow_payload = workflow_payload if isinstance(workflow_payload, dict) else {}
    if not _is_youtube_subtitle_source(source_payload):
        return _default_subtitle_quality_signal()

    diagnostics = cleanup_diagnostics if isinstance(cleanup_diagnostics, dict) else {}
    mode = str(workflow_payload.get("mode", "")).strip().lower()
    lines = [line.strip() for line in str(normalized_text or "").splitlines() if line.strip()]
    cue_count = max(1, _parse_int(diagnostics.get("cue_count"), 0) or int(segment_count) or len(lines) or 1)
    density_base = max(cue_count, 20)

    exact_duplicate_cue_count = max(0, _parse_int(diagnostics.get("exact_duplicate_cue_count"), 0))
    overlap_trim_count = max(0, _parse_int(diagnostics.get("overlap_trim_count"), 0))
    overlap_collapsed_cue_count = max(0, _parse_int(diagnostics.get("overlap_collapsed_cue_count"), 0))
    collapsed_cjk_spacing_count = max(0, _parse_int(diagnostics.get("collapsed_cjk_spacing_count"), 0))
    tightened_punctuation_spacing_count = max(0, _parse_int(diagnostics.get("tightened_punctuation_spacing_count"), 0))
    tightened_bracket_spacing_count = max(0, _parse_int(diagnostics.get("tightened_bracket_spacing_count"), 0))
    markup_tag_count = max(0, _parse_int(diagnostics.get("markup_tag_count"), 0))
    nbsp_entity_count = max(0, _parse_int(diagnostics.get("nbsp_entity_count"), 0))
    empty_cue_count = max(0, _parse_int(diagnostics.get("empty_cue_count"), 0))

    overlap_reduction_count = overlap_trim_count + overlap_collapsed_cue_count
    spacing_cleanup_count = (
        collapsed_cjk_spacing_count
        + tightened_punctuation_spacing_count
        + tightened_bracket_spacing_count
    )
    markup_cleanup_count = markup_tag_count + nbsp_entity_count
    exact_duplicate_cue_ratio = exact_duplicate_cue_count / density_base
    overlap_reduction_ratio = overlap_reduction_count / density_base
    spacing_cleanup_ratio = spacing_cleanup_count / density_base
    markup_cleanup_ratio = markup_cleanup_count / density_base
    empty_cue_ratio = empty_cue_count / density_base

    fragment_line_count = sum(1 for line in lines if _is_quality_fragment_paragraph(line))
    fragment_line_ratio = fragment_line_count / len(lines) if lines else 0.0
    avg_line_char_count = (
        sum(_count_quality_chars(line) for line in lines) / len(lines)
        if lines else 0.0
    )

    duplicate_penalty = _subtitle_quality_penalty(
        exact_duplicate_cue_ratio,
        safe=0.04,
        scale=3.5,
        max_penalty=0.35,
    )
    overlap_penalty = _subtitle_quality_penalty(
        overlap_reduction_ratio,
        safe=0.06,
        scale=3.0,
        max_penalty=0.30,
    )
    spacing_penalty = _subtitle_quality_penalty(
        spacing_cleanup_ratio,
        safe=0.10,
        scale=2.0,
        max_penalty=0.20,
    )
    markup_penalty = _subtitle_quality_penalty(
        markup_cleanup_ratio,
        safe=0.15,
        scale=1.2,
        max_penalty=0.08,
    )
    empty_penalty = _subtitle_quality_penalty(
        empty_cue_ratio,
        safe=0.05,
        scale=2.0,
        max_penalty=0.10,
    )
    fragmentation_penalty = 0.0
    if mode == "chinese" and len(lines) >= 10:
        fragmentation_penalty = _subtitle_quality_penalty(
            fragment_line_ratio,
            safe=0.55,
            scale=0.6,
            max_penalty=0.12,
        )

    penalties = (
        duplicate_penalty
        + overlap_penalty
        + spacing_penalty
        + markup_penalty
        + empty_penalty
        + fragmentation_penalty
    )
    subtitle_quality_score = round(max(0.0, 1.0 - penalties), 3)
    subtitle_quality_band = _classify_subtitle_quality_band(subtitle_quality_score)
    subtitle_quality_reasons = []
    if duplicate_penalty > 0:
        subtitle_quality_reasons.append("duplicate_cue_density_high")
    if overlap_penalty > 0:
        subtitle_quality_reasons.append("overlap_reduction_density_high")
    if spacing_penalty > 0:
        subtitle_quality_reasons.append("spacing_cleanup_density_high")
    if markup_penalty > 0:
        subtitle_quality_reasons.append("markup_cleanup_density_high")
    if empty_penalty > 0:
        subtitle_quality_reasons.append("empty_cue_density_high")
    if fragmentation_penalty > 0:
        subtitle_quality_reasons.append("subtitle_fragmentation_high")

    return {
        "applicable": True,
        "evaluated": True,
        "subtitle_quality_score": subtitle_quality_score,
        "subtitle_quality_band": subtitle_quality_band,
        "subtitle_quality_reasons": subtitle_quality_reasons,
        "overlap_reduction_count": overlap_reduction_count,
        "metrics": {
            "cue_count": cue_count,
            "line_count": len(lines),
            "fragment_line_count": fragment_line_count,
            "fragment_line_ratio": round(fragment_line_ratio, 3),
            "avg_line_char_count": round(avg_line_char_count, 1),
            "exact_duplicate_cue_ratio": round(exact_duplicate_cue_ratio, 3),
            "overlap_reduction_ratio": round(overlap_reduction_ratio, 3),
            "spacing_cleanup_ratio": round(spacing_cleanup_ratio, 3),
            "markup_cleanup_ratio": round(markup_cleanup_ratio, 3),
            "empty_cue_ratio": round(empty_cue_ratio, 3),
        },
    }


def _build_source_routing_signal(source: str, mode: str, subtitle_quality: dict | None = None) -> dict:
    """Return explainable source-routing fields for planning and audits."""
    quality_payload = dict(_default_subtitle_quality_signal())
    if isinstance(subtitle_quality, dict):
        quality_payload.update(subtitle_quality)
    quality_payload["metrics"] = dict(quality_payload.get("metrics", {}))

    score = quality_payload.get("subtitle_quality_score")
    band = str(quality_payload.get("subtitle_quality_band", "")).strip()
    reasons = quality_payload.get("subtitle_quality_reasons", [])
    subtitle_quality_reasons = reasons if isinstance(reasons, list) else []
    overlap_reduction_count = max(0, _parse_int(quality_payload.get("overlap_reduction_count"), 0))

    source_route_reason = "source_route_unknown"
    reroute_recommended = False
    reroute_target = ""
    reroute_reasons = []

    normalized_source = str(source or "").strip().lower()
    normalized_mode = str(mode or "").strip().lower()
    if normalized_source == "deepgram":
        source_route_reason = "deepgram_source_selected"
    elif quality_payload.get("applicable"):
        if quality_payload.get("evaluated"):
            if band == "critical" and normalized_mode == "chinese":
                source_route_reason = "subtitle_quality_critical_deepgram_recommended"
                reroute_recommended = True
                reroute_target = "deepgram"
                reroute_reasons = list(subtitle_quality_reasons)
            elif band == "critical":
                source_route_reason = "subtitle_quality_critical_manual_review_only"
            elif band == "poor":
                source_route_reason = "subtitle_quality_poor_review_subtitle_path"
            else:
                source_route_reason = "subtitle_quality_acceptable"
        else:
            source_route_reason = "subtitle_quality_pending"
    elif normalized_source == "youtube":
        source_route_reason = "subtitle_quality_pending"
    elif normalized_source:
        source_route_reason = f"{normalized_source}_source_selected"

    return {
        "source_route_reason": source_route_reason,
        "reroute_recommended": reroute_recommended,
        "reroute_target": reroute_target,
        "reroute_reasons": reroute_reasons,
        "subtitle_quality_score": score,
        "subtitle_quality_band": band,
        "subtitle_quality_reasons": subtitle_quality_reasons,
        "overlap_reduction_count": overlap_reduction_count,
        "subtitle_quality": quality_payload,
    }


def build_normalized_document(*, document_id: str, raw_text_path: str, segments_path: str,
                              normalized_document_path: str, source_payload: dict | None = None,
                              workflow_payload: dict | None = None) -> dict:
    """Write the normalized document from a raw text file and/or a segments file.

    `source_payload` says where the text came from (`type` youtube or deepgram,
    `subtitle_source`, title, channel, ...) and `workflow_payload` carries the
    output `mode`; together they decide how the text is cleaned, whether
    chunking prefers text over timed segments, and whether the subtitle
    quality score applies. Timed segments are used when the segments file
    exists, else the raw text. The result also names the raw text and segments files that were actually
    used (`raw_text_path`, `segments_path`).
    """
    source_payload = source_payload if isinstance(source_payload, dict) else {}
    workflow_payload = workflow_payload if isinstance(workflow_payload, dict) else {}

    def failure(message: str) -> dict:
        return {
            "passed": False,
            "warnings": [],
            "hard_failures": [message],
            "normalized_document_path": normalized_document_path,
            "materialized": False,
        }

    if not document_id:
        return failure("Missing document id for normalization")

    raw_text_file = Path(raw_text_path) if raw_text_path else None
    segments_file = Path(segments_path) if segments_path else None
    use_segments = bool(segments_file and segments_file.exists())
    if not use_segments and not (raw_text_file and raw_text_file.exists()):
        return failure(f"Raw text artifact not found: {raw_text_path}")

    prefer_text_chunking = _prefer_text_chunking_for_source(source_payload, workflow_payload)

    normalized_segments = []
    has_timing = False
    source_adapter = "raw_text_file"
    preferred_chunk_source = "text"
    cleanup_diagnostics = {}
    warnings = []

    if use_segments:
        try:
            payload = json.loads(segments_file.read_text(encoding="utf-8"))
        except Exception as e:
            return failure(f"Cannot read segments artifact: {e}")
        if not isinstance(payload, dict) or not isinstance(payload.get("segments", []), list):
            return failure("Segments artifact must be a JSON object with a segments list")
        payload_diagnostics = payload.get("diagnostics", {}) if isinstance(payload.get("diagnostics", {}), dict) else {}
        if _is_youtube_subtitle_source(source_payload) or str(payload.get("source", "")).strip().lower() == "vtt":
            cleanup_diagnostics = payload_diagnostics
        for seg in payload.get("segments", []):
            if not isinstance(seg, dict):
                continue
            clean_text = _normalize_segment_text_for_source(seg.get("text", ""), source_payload)
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
        preferred_chunk_source = "text" if prefer_text_chunking else "segments"
        if prefer_text_chunking and raw_text_file and raw_text_file.exists():
            try:
                normalized_text = _normalize_document_text_for_source(
                    raw_text_file.read_text(encoding="utf-8"),
                    source_payload,
                )
            except Exception as e:
                warnings.append(
                    f"Cannot read subtitle raw text artifact, fell back to normalized segments: {e}"
                )
    else:
        try:
            normalized_text = _normalize_document_text_for_source(
                raw_text_file.read_text(encoding="utf-8"),
                source_payload,
            )
        except Exception as e:
            return failure(f"Cannot read raw text artifact: {e}")

    used_raw_text = raw_text_path if raw_text_file and raw_text_file.exists() else ""
    used_segments = segments_path if segments_file and segments_file.exists() else ""
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
            "mode": workflow_payload.get("mode", ""),
        },
        "artifacts": {
            "raw_text": used_raw_text,
            "segments_path": used_segments,
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
    subtitle_quality = _evaluate_subtitle_quality(
        normalized_text,
        cleanup_diagnostics,
        source_payload=source_payload,
        workflow_payload=workflow_payload,
        segment_count=len(normalized_segments),
    )
    if cleanup_diagnostics or subtitle_quality.get("applicable"):
        normalized_doc["diagnostics"] = {}
        if cleanup_diagnostics:
            normalized_doc["diagnostics"]["subtitle_cleanup"] = cleanup_diagnostics
        if subtitle_quality.get("applicable"):
            normalized_doc["diagnostics"]["subtitle_quality"] = subtitle_quality
    _atomic_write_text(Path(normalized_document_path), json.dumps(normalized_doc, ensure_ascii=False, indent=2))
    return {
        "passed": True,
        "warnings": warnings,
        "hard_failures": [],
        "normalized_document_path": normalized_document_path,
        "materialized": True,
        "source_adapter": source_adapter,
        "preferred_chunk_source": preferred_chunk_source,
        "segment_count": len(normalized_segments),
        "has_timing": has_timing,
        "char_count": len(normalized_text),
        "cleanup_diagnostics": cleanup_diagnostics,
        "subtitle_quality": subtitle_quality,
        "raw_text_path": used_raw_text,
        "segments_path": used_segments,
    }


def _estimate_single_pass_input_tokens(normalized_document_path: str, raw_text_path: str,
                                       config: dict | None = None) -> tuple[int, int]:
    """Estimate normalized input size for short-video single-pass routing."""
    config = config or {}
    candidates = []
    normalized_path = Path(str(normalized_document_path or "").strip())
    if normalized_path.exists():
        candidates.append((normalized_path, 'normalized_document'))
    raw_path = Path(str(raw_text_path or "").strip())
    if raw_path.exists():
        candidates.append((raw_path, 'raw_text'))

    for candidate_path, source_kind in candidates:
        try:
            if source_kind == 'normalized_document':
                payload = json.loads(candidate_path.read_text(encoding='utf-8'))
                text = str(payload.get('content', {}).get('text', '') or '')
            else:
                text = candidate_path.read_text(encoding='utf-8')
        except Exception:
            continue
        normalized_text = _normalize_text_body(text)
        if normalized_text:
            return _estimate_tokens(normalized_text), len(normalized_text)
    return 0, 0


def _single_pass_token_limit(mode: str, source: str, config: dict | None = None) -> int:
    """Return the largest input we should still send through one non-chunked prompt."""
    del config
    structure_limit = SINGLE_PASS_TOKEN_LIMITS['structure_only']
    translate_limit = SINGLE_PASS_TOKEN_LIMITS['translate_only']
    limit = structure_limit
    if str(mode or '').strip() == 'bilingual':
        limit = min(limit, translate_limit)
    if str(source or '').strip() == 'deepgram':
        limit = min(limit, structure_limit - 300)
    return max(3200, limit)


def plan_text_operations(*, video_id: str, duration: int, mode: str, source: str, normalization: dict,
                         raw_text_path: str, segments_path: str, work_dir: str, structured_text: str,
                         optimized_text: str, config: dict | None = None) -> dict:
    """Choose the text operations for a source and estimate their size.

    `normalization` is the result of `build_normalized_document` for that
    source; `source` is youtube or deepgram and `mode` chinese or bilingual.
    """
    planner_config = config or {}
    duration_bucket = "long" if duration >= 1800 else "short"
    raw_text_output = raw_text_path
    estimated_input_tokens, estimated_input_chars = _estimate_single_pass_input_tokens(
        normalization.get("normalized_document_path", ""),
        raw_text_output,
        planner_config,
    )
    single_pass_token_limit = _single_pass_token_limit(mode, source, planner_config)
    oversized_short_input = duration_bucket == "short" and estimated_input_tokens > single_pass_token_limit
    routing_reason = "duration_threshold" if duration_bucket == "long" else (
        "oversized_short_input" if oversized_short_input else "duration_short_single_pass"
    )
    video_path = "long" if duration_bucket == "long" or oversized_short_input else "short"
    source_routing = _build_source_routing_signal(
        source,
        mode,
        normalization.get("subtitle_quality", {}),
    )
    plan_warnings = list(normalization.get("warnings", []))
    if oversized_short_input:
        plan_warnings.append(
            f"Short-duration input is oversized for single-pass optimization ({estimated_input_tokens} estimated tokens > {single_pass_token_limit}); using chunked processing."
        )
    if source == "deepgram":
        plan_warnings.append(
            "Deepgram fallback is active; review proper nouns and mixed-language terms carefully in the final output."
        )
    elif source_routing["reroute_recommended"] and source_routing["reroute_target"] == "deepgram":
        plan_warnings.append(
            "Subtitle quality is critical; reroute to Deepgram before text optimization is recommended "
            f"(subtitle_quality_score={source_routing['subtitle_quality_score']:.3f})."
        )
    elif source_routing["subtitle_quality_band"] == "critical":
        plan_warnings.append(
            "Subtitle quality is critical on the current subtitle route; continue only with manual review "
            f"(subtitle_quality_score={source_routing['subtitle_quality_score']:.3f})."
        )
    elif source_routing["subtitle_quality_band"] == "poor":
        plan_warnings.append(
            "Subtitle quality looks poor; keep the subtitle path only with extra review "
            f"(subtitle_quality_score={source_routing['subtitle_quality_score']:.3f})."
        )

    outputs = {
        "raw_text": raw_text_output,
        "structured_text": structured_text,
        "optimized_text": optimized_text,
        "normalized_document": normalization.get("normalized_document_path", ""),
        "work_dir": work_dir,
    }

    chinese_cleanup_prompt = "cleanup_zh"

    if video_path == "short":
        if mode == "bilingual":
            operations = [
                {
                    "kind": "prompt",
                    "prompt": "structure_only",
                    "input": outputs["raw_text"],
                    "output": outputs["structured_text"],
                    "extra_instruction": "",
                },
                {
                    "kind": "prompt",
                    "prompt": "translate_only",
                    "input": outputs["structured_text"],
                    "output": outputs["optimized_text"],
                    "extra_instruction": "",
                },
            ]
        else:
            extra_instruction = ""
            if source == "deepgram":
                extra_instruction = (
                    "Source is Deepgram ASR. Also repair obvious ASR artifacts such as broken Chinese spacing, "
                    "missing punctuation, and repeated fragments when confidence is high."
                )
            operations = [
                {
                    "kind": "prompt",
                    "prompt": chinese_cleanup_prompt,
                    "input": outputs["raw_text"],
                    "output": outputs["optimized_text"],
                    "extra_instruction": extra_instruction,
                }
            ]
    else:
        extra_instruction = ""
        if mode == "chinese" and source == "deepgram":
            extra_instruction = (
                "Source is Deepgram ASR. Also repair obvious ASR artifacts such as broken Chinese spacing, "
                "missing punctuation, and repeated fragments when confidence is high."
            )

        operations = [
            {
                "kind": "chunk",
                "prompt": chinese_cleanup_prompt if mode == "chinese" else "structure_only",
                "input_key": "raw_path",
                "extra_instruction": extra_instruction,
            }
        ]
        if mode == "bilingual":
            operations.append(
                {
                    "kind": "chunk",
                    "prompt": "translate_only",
                    "input_key": "processed_path",
                    "extra_instruction": "",
                }
            )

    return {
        "checks": {
            "duration": duration,
            "duration_bucket": duration_bucket,
            "mode": mode,
            "source": source,
            "video_path": video_path,
            "routing_reason": routing_reason,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_input_chars": estimated_input_chars,
            "single_pass_token_limit": single_pass_token_limit,
            "subtitle_quality_score": source_routing["subtitle_quality_score"],
            "subtitle_quality_band": source_routing["subtitle_quality_band"],
            "overlap_reduction_count": source_routing["overlap_reduction_count"],
            "source_route_reason": source_routing["source_route_reason"],
            "reroute_recommended": source_routing["reroute_recommended"],
            "reroute_target": source_routing["reroute_target"],
        },
        "warnings": plan_warnings,
        "hard_failures": [],
        "video_path": video_path,
        "duration_bucket": duration_bucket,
        "routing_reason": routing_reason,
        "source_route_reason": source_routing["source_route_reason"],
        "reroute_recommended": source_routing["reroute_recommended"],
        "reroute_target": source_routing["reroute_target"],
        "reroute_reasons": source_routing["reroute_reasons"],
        "subtitle_quality_score": source_routing["subtitle_quality_score"],
        "subtitle_quality_band": source_routing["subtitle_quality_band"],
        "subtitle_quality_reasons": source_routing["subtitle_quality_reasons"],
        "normalization": {
            "materialized": normalization.get("materialized", False),
            "source_adapter": normalization.get("source_adapter", ""),
            "preferred_chunk_source": normalization.get("preferred_chunk_source", ""),
            "segment_count": normalization.get("segment_count", 0),
            "normalized_document_path": normalization.get("normalized_document_path", ""),
            "subtitle_quality": normalization.get("subtitle_quality", {}),
        },
        "estimated_input_tokens": estimated_input_tokens,
        "single_pass_token_limit": single_pass_token_limit,
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
    config_issue_warnings = []

    def parse_int_field(key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        """Parse an integer config field with warnings and range enforcement."""
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
        """Parse float field."""
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

    deepgram_model, deepgram_model_warning = _resolve_deepgram_model(
        config.get('deepgram_model', DEFAULT_DEEPGRAM_MODEL),
    )
    if deepgram_model_warning:
        config_warnings.append(deepgram_model_warning)
        config_issue_warnings.append(deepgram_model_warning)
    deepgram_enable_utterances = _parse_bool(
        config.get('deepgram_enable_utterances', DEFAULT_DEEPGRAM_ENABLE_UTTERANCES),
        DEFAULT_DEEPGRAM_ENABLE_UTTERANCES,
    )
    deepgram_prefer_structured_output = _parse_bool(
        config.get('deepgram_prefer_structured_output', DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT),
        DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT,
    )

    yt_dlp_socket_timeout_sec = parse_int_field(
        'yt_dlp_socket_timeout_sec',
        DEFAULT_YT_DLP_SOCKET_TIMEOUT_SEC,
        minimum=1,
    )
    yt_dlp_retries = parse_int_field(
        'yt_dlp_retries',
        DEFAULT_YT_DLP_RETRIES,
        minimum=0,
    )
    yt_dlp_extractor_retries = parse_int_field(
        'yt_dlp_extractor_retries',
        DEFAULT_YT_DLP_EXTRACTOR_RETRIES,
        minimum=0,
    )
    yt_dlp_cookies_from_browser = str(config.get('yt_dlp_cookies_from_browser', '') or '').strip()
    yt_dlp_cookies_file = str(config.get('yt_dlp_cookies_file', '') or '').strip()
    if yt_dlp_cookies_file:
        yt_dlp_cookies_file = os.path.expanduser(yt_dlp_cookies_file)
        if not os.path.isfile(yt_dlp_cookies_file):
            print(f"Warning: yt_dlp_cookies_file does not exist: {yt_dlp_cookies_file}", file=sys.stderr)

    llm_timeout_sec = parse_int_field('llm_timeout_sec', 120, minimum=1)
    llm_max_retries = parse_int_field('llm_max_retries', 3, minimum=0)
    llm_backoff_sec = parse_float_field('llm_backoff_sec', 1.5, minimum=0.1)
    task_budgets = {}
    for prompt in TASK_CHUNK_TOKEN_DEFAULTS:
        task_budgets[f"chunk_tokens_{prompt}"] = parse_int_field(
            f"chunk_tokens_{prompt}", TASK_CHUNK_TOKEN_DEFAULTS[prompt], minimum=1)
        task_budgets[f"output_ratio_{prompt}"] = parse_float_field(
            f"output_ratio_{prompt}", TASK_OUTPUT_RATIO_DEFAULTS[prompt], minimum=0.01)
        task_budgets[f"max_output_tokens_{prompt}"] = parse_int_field(
            f"max_output_tokens_{prompt}", TASK_MAX_OUTPUT_TOKEN_DEFAULTS[prompt], minimum=1)

    parsed = dict(defaults)
    parsed.update({
        "output_dir": output_dir,
        "deepgram_api_key": config.get('deepgram_api_key', ''),
        "deepgram_model": deepgram_model,
        "deepgram_enable_utterances": deepgram_enable_utterances,
        "deepgram_prefer_structured_output": deepgram_prefer_structured_output,
        "llm_api_key": config.get('llm_api_key', ''),
        "llm_base_url": config.get('llm_base_url', DEFAULT_LLM_BASE_URL),
        "llm_model": str(config.get('llm_model', DEFAULT_LLM_MODEL) or '').strip() or DEFAULT_LLM_MODEL,
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
        "llm_backend": kernel_agent_cli.normalize_backend_mode(config.get('llm_backend', kernel_agent_cli.DEFAULT_LLM_BACKEND)),
        "llm_agent_cli": str(config.get('llm_agent_cli', '') or '').strip(),
        "llm_cli_timeout_sec": parse_int_field('llm_cli_timeout_sec', kernel_agent_cli.DEFAULT_CLI_TIMEOUT_SEC, minimum=1),
        "llm_cli_max_retries": parse_int_field('llm_cli_max_retries', kernel_agent_cli.DEFAULT_CLI_MAX_RETRIES, minimum=0),
        "llm_cli_probe_timeout_sec": parse_int_field('llm_cli_probe_timeout_sec', kernel_agent_cli.DEFAULT_CLI_PROBE_TIMEOUT_SEC, minimum=1),
        "window_read_tokens": parse_int_field('window_read_tokens', kernel_window.DEFAULT_WINDOW_READ_TOKENS, minimum=200),
        "window_write_tokens": parse_int_field('window_write_tokens', kernel_window.DEFAULT_WINDOW_WRITE_TOKENS, minimum=100),
        "window_tail_tokens": parse_int_field('window_tail_tokens', kernel_window.DEFAULT_WINDOW_TAIL_TOKENS, minimum=0),
        "window_marker_retries": parse_int_field('window_marker_retries', kernel_window.DEFAULT_WINDOW_MARKER_RETRIES, minimum=0),
        "window_outline": _parse_bool(config.get('window_outline', True), True),
        "window_outline_read_tokens": parse_int_field('window_outline_read_tokens', kernel_outline.DEFAULT_OUTLINE_READ_TOKENS, minimum=1000),
        "window_outline_max_terms": parse_int_field('window_outline_max_terms', kernel_outline.DEFAULT_OUTLINE_MAX_TERMS, minimum=0),
        "chunk_size_override": parse_int_field('chunk_size_override', 0, minimum=0),
        "chunk_hard_cap_multiplier": parse_float_field(
            'chunk_hard_cap_multiplier',
            DEFAULT_CHUNK_HARD_CAP_MULTIPLIER,
            minimum=1.0,
            maximum=MAX_CHUNK_HARD_CAP_MULTIPLIER,
        ),
        "chunk_glossary_max_prompt_terms": parse_int_field('chunk_glossary_max_prompt_terms', 8, minimum=0),
        **task_budgets,
        "config_path": str(path.absolute()),
        "config_warnings": config_warnings,
    })
    if config_warnings:
        numeric_warnings = [warning for warning in config_warnings if warning not in config_issue_warnings]
        if numeric_warnings:
            print(f"Warning: Invalid numeric config values in {path}:", file=sys.stderr)
        for warning in numeric_warnings:
            print(f"  - {warning}", file=sys.stderr)
        if config_issue_warnings:
            print(f"Warning: Config issues in {path}:", file=sys.stderr)
            for warning in config_issue_warnings:
                print(f"  - {warning}", file=sys.stderr)
    return parsed


def main():
    """Main."""
    from kernel import pipeline as kernel_pipeline

    parser = argparse.ArgumentParser(
        description='yt-transcript utility script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    run_parser = subparsers.add_parser(
        'run',
        help='Run or continue the whole pipeline for one video (one JSON result, fixed exit codes)'
    )
    run_parser.add_argument('url', help='YouTube video URL (or an 11-character video id)')
    run_parser.add_argument('--source', choices=['subtitles', 'deepgram'], default=None,
                            help='Force the text source; remembered for later reruns of this video')
    run_parser.add_argument('--mode', choices=['chinese', 'bilingual'], default=None,
                            help='Force the article mode; remembered for later reruns of this video')
    run_parser.add_argument('--from', dest='from_stage', choices=list(kernel_pipeline.STAGES), default=None,
                            help='Redo this stage and every later one')
    run_parser.add_argument('--stage', choices=[kernel_pipeline.PLAN_STAGE], default=None,
                            help='Stop after the plan stage (routing check; no LLM, no Deepgram)')
    run_parser.add_argument('--max-seconds', type=float, default=None,
                            help='Return in_progress at the next safe point after this many seconds')
    run_parser.add_argument('--restart', action='store_true', help="Discard this video's work directory first")
    run_parser.add_argument('--config-path', default=None, help='Path to config.yaml')

    status_parser = subparsers.add_parser(
        'status',
        help='Show the runs under the work root, or one video\'s state.json'
    )
    status_parser.add_argument('video_id', nargs='?', default='', help='Video id (omit to list every run)')

    cleanup_parser = subparsers.add_parser(
        'cleanup',
        help="Remove a video's work directory (an unfinished run needs --force)"
    )
    cleanup_parser.add_argument('video_id', help='Video id')
    cleanup_parser.add_argument('--force', action='store_true', help='Also remove an unfinished run')

    # Debug command; download.sh also pipes yt-dlp metadata through it.
    subtitle_info_parser = subparsers.add_parser(
        'subtitle-info',
        help='Detect the spoken language and pick subtitle candidates from yt-dlp metadata on stdin'
    )
    subtitle_info_parser.add_argument('--video-id', default='', help='Video id to report when metadata lacks one')
    subtitle_info_parser.add_argument('--from-list-subs', action='store_true',
                                      help='Read `yt-dlp --list-subs` output instead of `yt-dlp -J` JSON')
    subtitle_info_parser.add_argument('--yt-dlp-runtime-json', default='',
                                      help='yt-dlp runtime JSON to echo back in the result')

    # Debug command: the quality check `run` applies in its verify stage.
    vq_parser = subparsers.add_parser(
        'verify-quality',
        help='Verify quality of optimized text file (structural checks)'
    )
    vq_parser.add_argument('optimized_text_path', help='Path to optimized text file')
    vq_parser.add_argument('--raw-text', default=None, help='Path to raw text file for size comparison')
    vq_parser.add_argument('--bilingual', action='store_true', help='Whether content should be bilingual')
    vq_parser.add_argument('--work-dir', default='', help='Optional work_dir used to load glossary.json for drift checks')
    vq_parser.add_argument('--glossary-path', default='', help='Optional glossary.json path for final drift checks')
    vq_parser.add_argument('--glossary-max-terms', type=int, default=12,
                           help='Maximum glossary terms to verify in final output')

    args = parser.parse_args()

    if args.command == 'run':
        # Progress goes to stderr; stdout carries exactly one JSON result.
        stderr_tail = kernel_pipeline._StderrTail(sys.stderr)
        with contextlib.redirect_stderr(stderr_tail), contextlib.redirect_stdout(stderr_tail):
            result, code = kernel_pipeline.run(
                args.url,
                source=args.source,
                mode=args.mode,
                from_stage=args.from_stage,
                stage=args.stage,
                max_seconds=args.max_seconds,
                restart=args.restart,
                config_path=args.config_path,
                stderr_tail=stderr_tail,
            )
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(code)

    elif args.command == 'status':
        result, code = kernel_pipeline.status(args.video_id)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(code)

    elif args.command == 'cleanup':
        result, code = kernel_pipeline.cleanup(args.video_id, force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(code)

    elif args.command == 'subtitle-info':
        raw = sys.stdin.read()
        if args.from_list_subs:
            payload = kernel_subtitle_language.metadata_from_list_subs(raw, video_id=args.video_id)
        else:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as error:
                print(f"Error: subtitle-info expects yt-dlp -J metadata on stdin: {error}", file=sys.stderr)
                sys.exit(1)
        try:
            runtime = json.loads(args.yt_dlp_runtime_json) if args.yt_dlp_runtime_json else {}
        except json.JSONDecodeError:
            runtime = {}
        result = kernel_subtitle_language.build_subtitle_info(payload, video_id=args.video_id, yt_dlp_runtime=runtime)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'verify-quality':
        result = verify_quality(
            args.optimized_text_path,
            raw_text_path=args.raw_text,
            bilingual=args.bilingual,
            work_dir=args.work_dir,
            glossary_path=args.glossary_path,
            glossary_max_terms=args.glossary_max_terms,
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
