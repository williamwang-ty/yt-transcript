"""LLM request orchestration helpers for long-text processing."""

from __future__ import annotations

_LOCAL_NAMES = {
    '_bind_utils_globals',
    '_call_llm_api'
}


def _bind_utils_globals() -> None:
    """Bind delegated helper names from `yt_transcript_utils` into module globals."""
    import yt_transcript_utils as utils

    for name, value in utils.__dict__.items():
        if name.startswith("__") or name in _LOCAL_NAMES:
            continue
        globals()[name] = value


def _call_llm_api(api_key: str, base_url: str, model: str, messages: list,
                  api_format: str = "openai", max_tokens: int = 8192,
                  temperature: float = 0.3, timeout_sec: int = 120,
                  max_retries: int = 3, backoff_sec: float = 1.5,
                  stream_mode: str = "auto") -> dict:
    """Call the configured LLM API with retries and streaming fallback."""
    _bind_utils_globals()
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
            # Some OpenAI-compatible gateways accept normal chat requests but
            # reject streaming. In auto mode we retry once without stream before
            # treating it as a real failure.
            if stream_mode == "auto" and use_stream and error.status_code in {400, 422} and stream_unsupported:
                print(
                    f"ℹ️ Streaming unsupported at {error.request_url or _build_api_url(base_url, api_format)}; retrying once without stream.",
                    file=sys.stderr,
                )
                use_stream = False
                continue

            if not error.retryable or attempt > max_retries:
                raise error

            # Retries are bounded and back off exponentially so transient provider
            # failures do not immediately abort the whole chunk run.
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
