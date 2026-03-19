import random
import time


def _utils():
    import yt_transcript_utils as utils

    return utils


def new_plan_id() -> str:
    return f"plan_{time.strftime('%Y%m%d%H%M%S', time.localtime())}_{random.randint(1000, 9999)}"


def build_manifest_chunk_contract(source_kind: str = "text", *, driver: str = "",
                                  normalized_document_path: str = "", source_adapter: str = "",
                                  has_timing: bool = False, chapters_enabled: bool = False) -> dict:
    utils = _utils()
    normalized_source_kind = "segments" if str(source_kind).strip() == "segments" else "text"
    default_driver = "chunk-segments" if normalized_source_kind == "segments" else "chunk-text"
    return {
        "version": utils.CHUNK_CONTRACT_SCHEMA_VERSION,
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


def build_manifest_continuity_policy(config: dict | None = None, *, tail_sentences: int | None = None,
                                     summary_token_cap: int | None = None) -> dict:
    utils = _utils()
    config = config or {}
    resolved_tail_sentences = max(
        0,
        utils._parse_int(
            tail_sentences,
            utils._parse_int_min(
                config.get("chunk_context_tail_sentences"),
                utils.DEFAULT_CHUNK_CONTEXT_TAIL_SENTENCES,
                0,
            ),
        ),
    )
    resolved_summary_token_cap = max(
        0,
        utils._parse_int(
            summary_token_cap,
            utils._parse_int_min(
                config.get("chunk_context_summary_tokens"),
                utils.DEFAULT_CHUNK_CONTEXT_SUMMARY_TOKENS,
                0,
            ),
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


def build_manifest_plan(prompt_name: str, chunk_mode: str, recommended_chunk_size: int,
                        effective_chunk_size: int, budget: dict, *, source_file: str = "",
                        plan_id: str = "", prior_plan_id: str = "",
                        chunk_contract: dict | None = None,
                        continuity_policy: dict | None = None) -> dict:
    utils = _utils()
    return {
        "plan_id": plan_id or new_plan_id(),
        "prior_plan_id": prior_plan_id,
        "prompt_name": prompt_name,
        "chunk_mode": utils._normalize_chunk_mode(chunk_mode),
        "chunk_size": effective_chunk_size,
        "recommended_chunk_size": recommended_chunk_size,
        "target_input_tokens": budget.get("target_tokens", 0),
        "target_tokens": budget.get("target_tokens", 0),
        "hard_cap_tokens": budget.get("hard_cap_tokens", 0),
        "planned_max_output_tokens": budget.get("planned_max_output_tokens", 0),
        "prompt_tokens": budget.get("prompt_tokens", 0),
        "prompt_template_tokens": budget.get("prompt_template_tokens", 0),
        "effective_budget_tokens": budget.get("effective_budget_tokens", 0),
        "output_ratio": budget.get("output_ratio", utils.DEFAULT_UNKNOWN_OUTPUT_RATIO),
        "chunk_safety_buffer_tokens": budget.get("safety_buffer_tokens", 0),
        "continuity_reserve_tokens": budget.get("continuity_reserve_tokens", 0),
        "token_count_source": budget.get("token_count_source", ""),
        "source_file": source_file,
        "chunk_contract": chunk_contract if isinstance(chunk_contract, dict) else build_manifest_chunk_contract(),
        "continuity": continuity_policy if isinstance(continuity_policy, dict) else build_manifest_continuity_policy(),
        "created_at": utils._now_iso(),
    }


def normalize_operation_input_key(input_key: str = "") -> str:
    normalized = str(input_key or "").strip()
    return normalized or "raw_path"


def resolve_replan_action(input_key: str = "") -> str:
    return "auto_replan_remaining" if normalize_operation_input_key(input_key) == "raw_path" else "stop_and_review"


def build_quality_gate_contract(*, bilingual: bool = False) -> dict:
    utils = _utils()
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
        "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
        "scope": "final_output",
        "stop_rule": "hard_failures_stop",
        "warning_rule": "warnings_require_review",
        "hard_failure_checks": hard_failure_checks,
        "warning_checks": warning_checks,
    }


def build_chunk_verification_contract(prompt_name: str, *, applicable: bool = True) -> dict:
    utils = _utils()
    if not applicable:
        return {
            "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
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
            "min_output_input_ratio": utils.SHORT_OUTPUT_WARNING_RATIO,
        })
    if prompt_name in ("structure_only", "quick_cleanup"):
        retryable_checks.append({
            "id": "missing_headers",
            "severity": "repairable_warning",
            "retry_action": "retry_same_chunk_same_plan",
            "required_header_prefix": "## ",
            "min_input_chars": utils.STRUCTURE_HEADER_WARNING_MIN_CHARS,
        })
    if prompt_name == "translate_only":
        retryable_checks.append({
            "id": "translation_skipped",
            "severity": "repairable_warning",
            "retry_action": "retry_same_chunk_same_plan",
            "min_cn_char_ratio": utils.TRANSLATION_WARNING_CN_RATIO,
        })
    return {
        "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
        "scope": "chunk_output",
        "retryable_checks": retryable_checks,
        "warning_rule": "warnings_are_recorded",
    }


def build_repair_contract(prompt_name: str, config: dict | None = None, *, applicable: bool = True) -> dict:
    utils = _utils()
    config = config or {}
    if not applicable:
        return {
            "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
            "mode": "not_applicable",
            "max_retries_per_chunk": 0,
            "backoff_sec": 0.0,
            "retry_on_checks": [],
            "after_max_retries": "not_applicable",
        }

    retry_on_checks = [check["id"] for check in build_chunk_verification_contract(prompt_name)["retryable_checks"]]
    return {
        "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
        "mode": "bounded_retry",
        "max_retries_per_chunk": utils._parse_int_min(
            config.get("llm_chunk_recovery_attempts"),
            utils.DEFAULT_LLM_CHUNK_RECOVERY_ATTEMPTS,
            0,
        ),
        "backoff_sec": round(utils._parse_float_min(
            config.get("llm_chunk_recovery_backoff_sec"),
            utils.DEFAULT_LLM_CHUNK_RECOVERY_BACKOFF_SEC,
            0.0,
        ), 2),
        "retry_on_checks": retry_on_checks,
        "retry_action": "retry_same_chunk_same_plan",
        "after_max_retries": "accept_with_warnings",
    }


def build_replan_contract(input_key: str = "raw_path", *, applicable: bool = True,
                          canary_chunks: int = 0,
                          max_auto_replans: int | None = None) -> dict:
    utils = _utils()
    if not applicable:
        return {
            "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
            "mode": "not_applicable",
            "supports_auto_replan": False,
            "recommended_cli_flags": [],
            "on_replan_required": "not_applicable",
            "trigger_conditions": [],
        }

    action = resolve_replan_action(input_key)
    supports_auto_replan = action == "auto_replan_remaining"
    contract = {
        "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
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
                "canary_chunk_limit": max(
                    1,
                    utils._parse_int_min(canary_chunks, utils.DEFAULT_AUTOTUNE_CANARY_CHUNKS, 1),
                ),
            },
        ],
    }
    if max_auto_replans is not None:
        contract["max_auto_replans"] = max(0, utils._parse_int(max_auto_replans, 0))
    return contract


def build_operation_control_contract(kind: str, prompt_name: str, *,
                                     input_key: str = "raw_path",
                                     config: dict | None = None,
                                     bilingual: bool = False,
                                     max_auto_replans: int | None = None) -> dict:
    utils = _utils()
    config = config or {}
    is_chunk = kind == "chunk"
    return {
        "version": utils.CONTROL_CONTRACT_SCHEMA_VERSION,
        "kind": "chunk" if is_chunk else "single_pass",
        "prompt_name": str(prompt_name or "").strip(),
        "input_key": normalize_operation_input_key(input_key) if is_chunk else "",
        "verification": build_chunk_verification_contract(prompt_name, applicable=is_chunk),
        "repair": build_repair_contract(prompt_name, config, applicable=is_chunk),
        "replan": build_replan_contract(
            input_key,
            applicable=is_chunk,
            canary_chunks=utils._parse_int_min(
                config.get("autotune_canary_chunks"),
                utils.DEFAULT_AUTOTUNE_CANARY_CHUNKS,
                1,
            ),
            max_auto_replans=max_auto_replans,
        ),
        "quality_gate": build_quality_gate_contract(bilingual=bilingual),
    }


def build_runtime_control_state() -> dict:
    return {
        "verification_warning_count": 0,
        "repair_attempted_count": 0,
        "repair_exhausted_count": 0,
        "last_replan_trigger": "",
        "last_replan_action": "",
        "last_replan_chunk_id": None,
        "pause_requested": False,
        "pause_reason": "",
        "paused_at": "",
        "resumed_at": "",
    }


def ensure_runtime_control_state(runtime: dict) -> dict:
    control = runtime.get("control")
    if not isinstance(control, dict):
        control = build_runtime_control_state()
        runtime["control"] = control
    control.setdefault("verification_warning_count", 0)
    control.setdefault("repair_attempted_count", 0)
    control.setdefault("repair_exhausted_count", 0)
    control.setdefault("last_replan_trigger", "")
    control.setdefault("last_replan_action", "")
    control.setdefault("last_replan_chunk_id", None)
    control.setdefault("pause_requested", False)
    control.setdefault("pause_reason", "")
    control.setdefault("paused_at", "")
    control.setdefault("resumed_at", "")
    return control


def build_chunk_control_state() -> dict:
    return {
        "verification_status": "pending",
        "warning_count": 0,
        "warnings": [],
        "retry_reasons": [],
        "repair_exhausted": False,
        "last_verified_at": "",
    }


def ensure_chunk_control_state(chunk_info: dict) -> dict:
    control = chunk_info.get("control")
    if not isinstance(control, dict):
        control = build_chunk_control_state()
        chunk_info["control"] = control
    control.setdefault("verification_status", "pending")
    control.setdefault("warning_count", 0)
    control.setdefault("warnings", [])
    control.setdefault("retry_reasons", [])
    control.setdefault("repair_exhausted", False)
    control.setdefault("last_verified_at", "")
    return control


def record_chunk_verification(chunk_info: dict, *, status: str, warnings: list[str],
                              retry_reasons: list[str], repair_exhausted: bool = False) -> None:
    utils = _utils()
    control = ensure_chunk_control_state(chunk_info)
    control["verification_status"] = str(status or "pending")
    control["warning_count"] = len([warning for warning in (warnings or []) if str(warning).strip()])
    control["warnings"] = [str(warning) for warning in (warnings or []) if str(warning).strip()]
    control["retry_reasons"] = [str(reason) for reason in (retry_reasons or []) if str(reason).strip()]
    control["repair_exhausted"] = bool(repair_exhausted)
    control["last_verified_at"] = utils._now_iso()


def classify_replan_trigger(error: Exception | None = None, *,
                            had_timeout_retry: bool = False,
                            autotune_last_event: str = "") -> str:
    utils = _utils()
    if had_timeout_retry:
        return "timeout_retry_instability"
    if autotune_last_event == "shrink":
        return "canary_autotune_shrink"
    if error is not None:
        if utils._is_timeout_error(error):
            return "timeout_retry_instability"
        status_code = getattr(error, "status_code", None)
        response_hint = str(getattr(error, "response_body", "") or "").lower()
        if status_code in {413, 422}:
            return "context_or_budget_error"
        if status_code == 400 and any(
            token in response_hint
            for token in ("context", "prompt", "max token", "too long", "token limit")
        ):
            return "context_or_budget_error"
        if str(getattr(error, "error_type", "") or "") == "bad_response":
            return "bad_response_requires_replan"
    return "manual_review"


def mark_runtime_replan(runtime: dict, *, reason: str, trigger: str,
                        input_key: str, chunk_id: int | None = None) -> None:
    runtime["replan_required"] = True
    runtime["replan_reason"] = str(reason or "").strip()
    control = ensure_runtime_control_state(runtime)
    control["last_replan_trigger"] = str(trigger or "manual_review")
    control["last_replan_action"] = resolve_replan_action(input_key)
    control["last_replan_chunk_id"] = chunk_id


def build_process_control_summary(runtime: dict, operation_control: dict) -> dict:
    runtime_control = ensure_runtime_control_state(runtime)
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
            "action": str(
                runtime_control.get(
                    "last_replan_action",
                    replan_contract.get("on_replan_required", "not_applicable"),
                )
                or ""
            ),
            "supports_auto_replan": bool(replan_contract.get("supports_auto_replan", False)),
            "last_replan_chunk_id": runtime_control.get("last_replan_chunk_id"),
        },
        "pause": {
            "requested": bool(runtime_control.get("pause_requested", False)),
            "reason": str(runtime_control.get("pause_reason", "") or ""),
            "paused_at": str(runtime_control.get("paused_at", "") or ""),
            "resumed_at": str(runtime_control.get("resumed_at", "") or ""),
        },
    }


def build_manifest_runtime(plan_id: str, request_url: str = "") -> dict:
    utils = _utils()
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
        "last_paused_at": "",
        "last_pause_reason": "",
        "pause_count": 0,
        "last_resumed_at": "",
        "last_resume_reason": "",
        "run_id": "",
        "operation_prompt_name": "",
        "operation_input_key": "raw_path",
        "operation_control": {},
        "control": build_runtime_control_state(),
        "updated_at": utils._now_iso(),
    }
