"""HTTP client for the configured LLM API (OpenAI- or Anthropic-format), with retries and streaming."""

from __future__ import annotations

import http.client
import json
import random
import socket
import sys
import time

from kernel.config import normalize_stream_mode, parse_int


def build_api_url(base_url: str, api_format: str = "openai") -> str:
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


def _build_attempt_log_from_result(result: dict, attempt_index: int | None = None) -> dict:
    """Build attempt log from result."""
    attempt_number = max(1, parse_int(attempt_index or result.get("attempts"), 1))
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
        "attempt_index": max(1, parse_int(attempt_index or getattr(error, "attempts", 1), 1)),
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

    completion_tokens = max(0, parse_int(usage.get("completion_tokens"), 0))
    reasoning_tokens = max(
        0,
        parse_int(
            completion_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0)),
            0,
        ),
    )
    content_tokens = max(0, completion_tokens - reasoning_tokens)
    return {
        "prompt_tokens": max(0, parse_int(usage.get("prompt_tokens"), 0)),
        "completion_tokens": completion_tokens,
        "total_tokens": max(0, parse_int(usage.get("total_tokens"), 0)),
        "reasoning_tokens": reasoning_tokens,
        "content_tokens": content_tokens,
        "cached_prompt_tokens": max(
            0,
            parse_int(prompt_details.get("cached_tokens", usage.get("cached_prompt_tokens", 0)), 0),
        ),
        "raw": usage,
    }


def _extract_llm_usage(result: dict, api_format: str) -> dict:
    """Normalize token usage from a provider response."""
    if api_format == "anthropic":
        usage = result.get("usage") if isinstance(result, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = max(0, parse_int(usage.get("input_tokens"), 0))
        output_tokens = max(0, parse_int(usage.get("output_tokens"), 0))
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
        url = build_api_url(base_url, api_format)
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
        url = build_api_url(base_url, api_format)
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


def call_llm_api(api_key: str, base_url: str, model: str, messages: list,
                 api_format: str = "openai", max_tokens: int = 8192,
                 temperature: float = 0.3, timeout_sec: int = 120,
                 max_retries: int = 3, backoff_sec: float = 1.5,
                 stream_mode: str = "auto") -> dict:
    """Call the configured LLM API with retries and streaming fallback."""
    stream_mode = normalize_stream_mode(stream_mode)
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
                    f"ℹ️ Streaming unsupported at {error.request_url or build_api_url(base_url, api_format)}; retrying once without stream.",
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
                f"(attempt {attempt}/{max_retries}, url={error.request_url or build_api_url(base_url, api_format)})",
                file=sys.stderr,
            )
            time.sleep(sleep_sec)

    if last_error is not None:
        last_error.attempt_history = list(attempt_history)
    raise last_error or LLMRequestError("LLM API request failed", retryable=False)
