"""config.yaml loading, the defaults of every setting, and the small value parsers the kernel shares.

This is the bottom layer of the kernel: it imports only the file helpers.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from kernel.files import atomic_write_text

LLM_BACKEND_MODES = ("auto", "api")
DEFAULT_LLM_BACKEND = "auto"
# What auto mode does when the agent CLI cannot be used and the API is configured.
LLM_API_FALLBACK_MODES = ("ask", "auto", "never")
DEFAULT_LLM_API_FALLBACK = "ask"
DEFAULT_CLI_TIMEOUT_SEC = 600
DEFAULT_CLI_MAX_RETRIES = 1
DEFAULT_CLI_PROBE_TIMEOUT_SEC = 120

DEFAULT_WINDOW_READ_TOKENS = 6000
DEFAULT_WINDOW_WRITE_TOKENS = 3000
DEFAULT_WINDOW_TAIL_TOKENS = 400
DEFAULT_WINDOW_MARKER_RETRIES = 1
DEFAULT_OUTLINE_READ_TOKENS = 16000
DEFAULT_OUTLINE_MAX_TERMS = 40

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
DEFAULT_UNKNOWN_CHUNK_TOKENS = 900
DEFAULT_UNKNOWN_OUTPUT_RATIO = 1.0
DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS


def skill_root() -> Path:
    """Return the repository root for this skill implementation."""
    return Path(__file__).resolve().parents[1]


def _default_config_path() -> Path:
    """Return the default config path."""
    return skill_root() / "config.yaml"


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


def parse_bool(value, default: bool = False) -> bool:
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


def parse_int(value, default: int) -> int:
    """Parse an integer value and fall back to the provided default."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_float(value, default: float) -> float:
    """Parse float."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_int_min(value, default: int, minimum: int) -> int:
    """Parse an integer and clamp it to a minimum valid value."""
    parsed = parse_int(value, default)
    if parsed < minimum:
        return default
    return parsed


def parse_float_range(value, default: float, minimum: float, maximum: float) -> float:
    """Parse float range."""
    parsed = parse_float(value, default)
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def normalize_stream_mode(value) -> str:
    """Normalize stream mode."""
    if value is None:
        return "auto"
    text = str(value).strip().lower()
    if text in {"auto", "true", "false"}:
        return text
    return "auto"


def normalize_backend_mode(value) -> str:
    """Normalize `llm_backend` to `auto` or `api`."""
    text = str(value or "").strip().lower()
    return text if text in LLM_BACKEND_MODES else DEFAULT_LLM_BACKEND


def normalize_api_fallback(value) -> str:
    """Normalize `llm_api_fallback` to `ask`, `auto` or `never`."""
    text = str(value or "").strip().lower()
    return text if text in LLM_API_FALLBACK_MODES else DEFAULT_LLM_API_FALLBACK


def _is_disabled_deepgram_model(model: str = "") -> bool:
    """Return whether a Deepgram model is disabled for this workflow."""
    normalized = str(model or "").strip().lower().replace("_", "-")
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}-")
        for prefix in DISABLED_DEEPGRAM_MODEL_PREFIXES
    )


def resolve_deepgram_model(model: str = "") -> tuple[str, str]:
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


def default_config_values(config_path: str = "") -> dict:
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
        "llm_backend": DEFAULT_LLM_BACKEND,
        "llm_api_fallback": DEFAULT_LLM_API_FALLBACK,
        "llm_agent_cli": "",
        "llm_cli_timeout_sec": DEFAULT_CLI_TIMEOUT_SEC,
        "llm_cli_max_retries": DEFAULT_CLI_MAX_RETRIES,
        "llm_cli_probe_timeout_sec": DEFAULT_CLI_PROBE_TIMEOUT_SEC,
        "window_read_tokens": DEFAULT_WINDOW_READ_TOKENS,
        "window_write_tokens": DEFAULT_WINDOW_WRITE_TOKENS,
        "window_tail_tokens": DEFAULT_WINDOW_TAIL_TOKENS,
        "window_marker_retries": DEFAULT_WINDOW_MARKER_RETRIES,
        "window_outline": True,
        "window_outline_read_tokens": DEFAULT_OUTLINE_READ_TOKENS,
        "window_outline_max_terms": DEFAULT_OUTLINE_MAX_TERMS,
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


def get_task_chunk_target(prompt_name: str, config: dict | None = None) -> int:
    """Return task chunk target."""
    config = config or {}
    override = max(0, parse_int(config.get("chunk_size_override"), 0))
    if override > 0:
        return override

    prompt = (prompt_name or "").strip().lower()
    default = TASK_CHUNK_TOKEN_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_CHUNK_TOKENS)
    return max(1, parse_int(config.get(f"chunk_tokens_{prompt}"), default))


def get_task_output_ratio(prompt_name: str, config: dict | None = None) -> float:
    """Return task output ratio."""
    config = config or {}
    prompt = (prompt_name or "").strip().lower()
    default = TASK_OUTPUT_RATIO_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_OUTPUT_RATIO)
    return max(0.01, parse_float(config.get(f"output_ratio_{prompt}"), default))


def get_task_max_output_tokens(prompt_name: str, config: dict | None = None) -> int:
    """Return task max output tokens."""
    config = config or {}
    prompt = (prompt_name or "").strip().lower()
    default = TASK_MAX_OUTPUT_TOKEN_DEFAULTS.get(prompt, DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS)
    return max(1, parse_int(config.get(f"max_output_tokens_{prompt}"), default))


def coerce_float_or_none(value) -> float | None:
    """Coerce float or none."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def set_config_value(config_path: str | None, key: str, value: str) -> Path:
    """Set one `key: value` line in config.yaml and return the file's path.

    Every other line stays as it is, and so does the file's mode (config.yaml
    holds API keys). A missing key is appended; a missing file is created.
    """
    path = (Path(config_path) if config_path else _default_config_path()).resolve()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        lines, mode = [], 0o600
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    new_line = f"{key}: {value}"
    if any(pattern.match(line) for line in lines):
        lines = [new_line if pattern.match(line) else line for line in lines]
    else:
        lines.append(new_line)
    atomic_write_text(path, "\n".join(lines) + "\n", mode=mode)
    return path


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
    defaults = default_config_values(str(path.absolute()))
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

    deepgram_model, deepgram_model_warning = resolve_deepgram_model(
        config.get('deepgram_model', DEFAULT_DEEPGRAM_MODEL),
    )
    if deepgram_model_warning:
        config_warnings.append(deepgram_model_warning)
        config_issue_warnings.append(deepgram_model_warning)
    deepgram_enable_utterances = parse_bool(
        config.get('deepgram_enable_utterances', DEFAULT_DEEPGRAM_ENABLE_UTTERANCES),
        DEFAULT_DEEPGRAM_ENABLE_UTTERANCES,
    )
    deepgram_prefer_structured_output = parse_bool(
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
        "llm_stream": normalize_stream_mode(config.get('llm_stream', 'auto')),
        "llm_backend": normalize_backend_mode(config.get('llm_backend', DEFAULT_LLM_BACKEND)),
        "llm_api_fallback": normalize_api_fallback(config.get('llm_api_fallback', DEFAULT_LLM_API_FALLBACK)),
        "llm_agent_cli": str(config.get('llm_agent_cli', '') or '').strip(),
        "llm_cli_timeout_sec": parse_int_field('llm_cli_timeout_sec', DEFAULT_CLI_TIMEOUT_SEC, minimum=1),
        "llm_cli_max_retries": parse_int_field('llm_cli_max_retries', DEFAULT_CLI_MAX_RETRIES, minimum=0),
        "llm_cli_probe_timeout_sec": parse_int_field('llm_cli_probe_timeout_sec', DEFAULT_CLI_PROBE_TIMEOUT_SEC, minimum=1),
        "window_read_tokens": parse_int_field('window_read_tokens', DEFAULT_WINDOW_READ_TOKENS, minimum=200),
        "window_write_tokens": parse_int_field('window_write_tokens', DEFAULT_WINDOW_WRITE_TOKENS, minimum=100),
        "window_tail_tokens": parse_int_field('window_tail_tokens', DEFAULT_WINDOW_TAIL_TOKENS, minimum=0),
        "window_marker_retries": parse_int_field('window_marker_retries', DEFAULT_WINDOW_MARKER_RETRIES, minimum=0),
        "window_outline": parse_bool(config.get('window_outline', True), True),
        "window_outline_read_tokens": parse_int_field('window_outline_read_tokens', DEFAULT_OUTLINE_READ_TOKENS, minimum=1000),
        "window_outline_max_terms": parse_int_field('window_outline_max_terms', DEFAULT_OUTLINE_MAX_TERMS, minimum=0),
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
