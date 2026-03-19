"""Autotune helpers for adaptive chunk sizing and token-source summaries."""

import math


def _utils():
    """Import and return the main utility module for delegated helpers."""
    import yt_transcript_utils as utils

    return utils


def build_autotune_state(prompt_budget: dict, config: dict | None = None,
                         existing: dict | None = None) -> dict:
    """Build persistent autotune state from budget defaults and prior history."""
    utils = _utils()
    config = config or {}
    existing = existing if isinstance(existing, dict) else {}

    enabled = utils._parse_bool(config.get("enable_chunk_autotune"), utils.DEFAULT_ENABLE_CHUNK_AUTOTUNE)
    reduce_percent = utils._parse_float_range(
        config.get("autotune_reduce_percent"),
        utils.DEFAULT_AUTOTUNE_REDUCE_PERCENT,
        0.01,
        0.90,
    )
    increase_percent = utils._parse_float_range(
        config.get("autotune_increase_percent"),
        utils.DEFAULT_AUTOTUNE_INCREASE_PERCENT,
        0.01,
        0.50,
    )
    success_window = utils._parse_int_min(
        config.get("autotune_success_window"),
        utils.DEFAULT_AUTOTUNE_SUCCESS_WINDOW,
        1,
    )
    p95_latency_threshold_ms = utils._parse_int_min(
        config.get("autotune_p95_latency_threshold_ms"),
        utils.DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS,
        1,
    )

    base_target_tokens = max(1, utils._parse_int(prompt_budget.get("target_tokens"), utils.DEFAULT_UNKNOWN_CHUNK_TOKENS))
    max_target_tokens = max(
        base_target_tokens,
        utils._parse_int(prompt_budget.get("hard_cap_tokens"), base_target_tokens),
    )
    min_target_tokens = max(1, int(math.floor(base_target_tokens * utils.DEFAULT_AUTOTUNE_MIN_TARGET_RATIO)))
    min_target_tokens = min(min_target_tokens, base_target_tokens)

    latency_window_ms = [
        max(0, utils._parse_int(value, 0))
        for value in existing.get("latency_window_ms", [])
        if max(0, utils._parse_int(value, 0)) > 0
    ][-success_window:]

    current_target_tokens = max(1, utils._parse_int(existing.get("current_target_tokens"), base_target_tokens))
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
        "p95_latency_ms": utils._estimate_p95(latency_window_ms),
        "consecutive_successes": max(0, utils._parse_int(existing.get("consecutive_successes"), 0)),
        "last_event": str(existing.get("last_event", "")).strip(),
        "last_reason": str(existing.get("last_reason", "")).strip(),
        "updated_at": str(existing.get("updated_at", "")).strip(),
        "current_planned_max_output_tokens": max(
            1,
            utils._parse_int(
                existing.get("current_planned_max_output_tokens"),
                prompt_budget.get("planned_max_output_tokens", utils.DEFAULT_UNKNOWN_MAX_OUTPUT_TOKENS),
            ),
        ),
    }
    return state


def update_autotune_state(autotune_state: dict | None, *, success: bool,
                          latency_ms: int | None = None, timeout: bool = False,
                          error_type: str = "", chunk_id: int | None = None) -> dict:
    """Update autotune state after one chunk succeeds, times out, or fails."""
    utils = _utils()
    state = dict(autotune_state or {})
    state.setdefault("enabled", False)
    state.setdefault("current_target_tokens", 0)
    state.setdefault("min_target_tokens", 1)
    state.setdefault("max_target_tokens", max(1, state.get("current_target_tokens", 1)))
    state.setdefault("reduce_percent", utils.DEFAULT_AUTOTUNE_REDUCE_PERCENT)
    state.setdefault("increase_percent", utils.DEFAULT_AUTOTUNE_INCREASE_PERCENT)
    state.setdefault("success_window", utils.DEFAULT_AUTOTUNE_SUCCESS_WINDOW)
    state.setdefault("p95_latency_threshold_ms", utils.DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS)
    state.setdefault("latency_window_ms", [])
    state["last_event"] = ""
    state["last_reason"] = ""
    state["updated_at"] = utils._now_iso()

    if not state.get("enabled"):
        return state

    current_target_tokens = max(1, utils._parse_int(state.get("current_target_tokens"), 1))
    min_target_tokens = max(1, utils._parse_int(state.get("min_target_tokens"), 1))
    max_target_tokens = max(current_target_tokens, utils._parse_int(state.get("max_target_tokens"), current_target_tokens))
    reduce_percent = utils._parse_float_range(state.get("reduce_percent"), utils.DEFAULT_AUTOTUNE_REDUCE_PERCENT, 0.01, 0.90)
    increase_percent = utils._parse_float_range(state.get("increase_percent"), utils.DEFAULT_AUTOTUNE_INCREASE_PERCENT, 0.01, 0.50)
    success_window = utils._parse_int_min(state.get("success_window"), utils.DEFAULT_AUTOTUNE_SUCCESS_WINDOW, 1)
    threshold_ms = utils._parse_int_min(
        state.get("p95_latency_threshold_ms"),
        utils.DEFAULT_AUTOTUNE_P95_LATENCY_THRESHOLD_MS,
        1,
    )

    latency_window_ms = [
        max(0, utils._parse_int(value, 0))
        for value in state.get("latency_window_ms", [])
        if max(0, utils._parse_int(value, 0)) > 0
    ][-success_window:]

    if success:
        parsed_latency = max(0, utils._parse_int(latency_ms, 0))
        if parsed_latency > 0:
            latency_window_ms = (latency_window_ms + [parsed_latency])[-success_window:]
        state["latency_window_ms"] = latency_window_ms
        state["p95_latency_ms"] = utils._estimate_p95(latency_window_ms)
        state["consecutive_successes"] = max(0, utils._parse_int(state.get("consecutive_successes"), 0)) + 1

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


def estimate_chunk_input_tokens(chunk_info: dict, input_key: str, text: str,
                                config: dict | None = None) -> tuple[int, str]:
    """Estimate input tokens without adding another network probe.

    For `processed_path` chains we reuse `actual_output_tokens`, because that is
    the closest measurement of the text now being fed into the next stage.
    """
    utils = _utils()
    chunk_info = chunk_info or {}
    config = config or {}

    if input_key == "processed_path":
        cached_tokens = max(0, utils._parse_int(chunk_info.get("actual_output_tokens"), 0))
        if cached_tokens > 0:
            return cached_tokens, "manifest_cached_output"
    else:
        cached_tokens = max(0, utils._parse_int(chunk_info.get("estimated_input_tokens"), 0))
        if cached_tokens > 0:
            return cached_tokens, "manifest_cached_input"

    return utils._estimate_tokens(text, "tokens", config), "local_estimate"


def refresh_manifest_token_source_summary(manifest: dict) -> None:
    """Refresh manifest token source summary."""
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
