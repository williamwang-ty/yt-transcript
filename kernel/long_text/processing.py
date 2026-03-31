"""Main chunk-processing and replan execution loops."""

from __future__ import annotations

_LOCAL_NAMES = {
    '_bind_utils_globals',
    '_process_chunks_impl',
    '_replan_remaining_impl',
    '_process_chunks_with_replans_impl'
}


def _bind_utils_globals() -> None:
    """Bind delegated helper names from `yt_transcript_utils` into module globals."""
    import yt_transcript_utils as utils

    for name, value in utils.__dict__.items():
        if name.startswith("__") or name in _LOCAL_NAMES:
            continue
        globals()[name] = value


def _process_chunks_impl(work_dir: str, prompt_name: str, extra_instruction: str = "",
                         config_path: str = None, dry_run: bool = False,
                         input_key: str = "raw_path", force: bool = False) -> dict:
    """Process all active chunks under one plan with retries, repair, and resume support."""
    _bind_utils_globals()
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
    glossary_payload = kernel_glossary.load_glossary(work_dir)
    glossary_auto_built = False
    if not glossary_payload and prompt_name == "cleanup_zh":
        glossary_build = kernel_glossary.build_glossary(work_dir, mode="transcript")
        if glossary_build.get("success", False):
            glossary_payload = glossary_build
            glossary_auto_built = True
    glossary_term_count = len(glossary_payload.get("terms", [])) if isinstance(glossary_payload.get("terms", []), list) else 0
    plan["glossary"] = {
        "mode": "auto_transcript_build" if glossary_auto_built else ("local_file" if glossary_term_count > 0 else "disabled"),
        "glossary_path": str(kernel_glossary.glossary_path_for(work_dir)) if glossary_term_count > 0 else "",
        "term_count": glossary_term_count,
        "source": str(glossary_payload.get("source", "")).strip() if glossary_term_count > 0 else "",
        "auto_built": glossary_auto_built,
        "max_prompt_terms": max(0, _parse_int_min(config.get("chunk_glossary_max_prompt_terms"), 8, 0)),
    }
    plan["semantic_verification"] = {
        "mode": "anchor_checks",
        "max_anchors": max(0, _parse_int_min(config.get("chunk_semantic_max_anchors"), 8, 0)),
        "judge_free": True,
    }
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
        if plan_target_tokens > prompt_budget["target_tokens"]:
            setup_warning = (
                f"⚠️ Planned chunk target {plan_target_tokens} tokens exceeds the current recommended "
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
    resume_report = _prepare_manifest_for_resume(
        manifest,
        work_path,
        prompt_name,
        input_key=input_key,
    )
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

    # Dry-run mode validates configuration, budgets, contracts, and resume state
    # without mutating chunk outputs or starting LLM requests.
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
            "glossary": plan.get("glossary", {}),
            "semantic_verification": plan.get("semantic_verification", {}),
            "resume": resume_report,
            "control": _build_process_control_summary(runtime, operation_control),
            "warnings": setup_warnings,
            "cancellation": kernel_state.summarize_runtime_cancel_request(work_dir),
            "pause": kernel_state.summarize_runtime_pause_request(work_dir),
            "message": "Dry run: all validations passed"
        }

    runtime["status"] = "running"
    runtime["run_id"] = kernel_runtime.new_trace_id("process-chunks")
    runtime["replan_required"] = False
    runtime["replan_reason"] = ""
    runtime["updated_at"] = _now_iso()
    runtime_control["pause_requested"] = False
    runtime_control["pause_reason"] = ""
    _write_manifest(manifest_path, manifest)

    processed_count = 0
    failed_count = 0
    skipped_count = 0
    superseded_count = 0
    warnings = list(setup_warnings)
    output_files = []
    aborted = False
    paused = False
    aborted_reason = ""
    pause_reason = ""
    cancellation = kernel_state.summarize_runtime_cancel_request(work_dir)
    pause = kernel_state.summarize_runtime_pause_request(work_dir)
    consecutive_timeouts = 0
    active_total = sum(1 for chunk in manifest["chunks"] if chunk.get("status") != SUPERSEDED_CHUNK_STATUS)
    total = active_total
    canary_limit = min(
        _parse_int_min(config.get("autotune_canary_chunks"), DEFAULT_AUTOTUNE_CANARY_CHUNKS, 1),
        active_total,
    )
    active_index = 0

    def maybe_abort_for_cancellation() -> bool:
        """Stop at a safe chunk boundary when a cancel signal is present."""
        nonlocal aborted, aborted_reason, cancellation
        cancellation_snapshot = kernel_state.summarize_runtime_cancel_request(work_dir)
        if not cancellation_snapshot.get("requested", False):
            return False
        cancellation = kernel_state.consume_runtime_cancel(work_dir)
        cancel_reason = str(cancellation.get("reason", "")).strip()
        aborted = True
        aborted_reason = "Cancellation requested"
        if cancel_reason:
            aborted_reason += f": {cancel_reason}"
        now = _now_iso()
        runtime["status"] = "aborted"
        runtime["replan_required"] = False
        runtime["replan_reason"] = ""
        runtime["updated_at"] = now
        runtime["last_cancelled_at"] = now
        runtime["last_cancel_reason"] = cancel_reason
        _refresh_manifest_token_source_summary(manifest)
        _sync_manifest_legacy_fields(manifest)
        _write_manifest(manifest_path, manifest)
        print(f"Error: {aborted_reason}", file=sys.stderr)
        return True

    def maybe_pause_for_request() -> bool:
        """Pause at a safe chunk boundary when a pause signal is present."""
        nonlocal paused, pause_reason, pause
        pause_snapshot = kernel_state.summarize_runtime_pause_request(work_dir)
        if not pause_snapshot.get("requested", False):
            return False
        pause = pause_snapshot
        pause_reason = str(pause.get("reason", "")).strip()
        paused = True
        now = _now_iso()
        runtime["status"] = PAUSED_RUNTIME_STATUS
        runtime["replan_required"] = False
        runtime["replan_reason"] = ""
        runtime["updated_at"] = now
        runtime["last_paused_at"] = now
        runtime["last_pause_reason"] = pause_reason
        runtime["pause_count"] = max(0, _parse_int(runtime.get("pause_count"), 0)) + 1
        runtime_control["pause_requested"] = True
        runtime_control["pause_reason"] = pause_reason
        runtime_control["paused_at"] = now
        _refresh_manifest_token_source_summary(manifest)
        _sync_manifest_legacy_fields(manifest)
        _write_manifest(manifest_path, manifest)
        message = "Pause requested"
        if pause_reason:
            message += f": {pause_reason}"
        print(f"ℹ️ {message}", file=sys.stderr)
        return True

    if maybe_abort_for_cancellation():
        runtime["processed_count"] = processed_count
        runtime["failed_count"] = failed_count
        runtime["skipped_count"] = skipped_count
        runtime["superseded_count"] = superseded_count
        return {
            "success": False,
            "processed_count": processed_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "superseded_count": superseded_count,
            "total_chunks": total,
            "warnings": warnings,
            "warning_count": len(warnings),
            "output_files": output_files,
            "aborted": True,
            "aborted_reason": aborted_reason,
            "paused": False,
            "pause_reason": "",
            "replan_required": False,
            "replan_reason": "",
            "resume": resume_report,
            "plan": manifest.get("plan", {}),
            "glossary": plan.get("glossary", {}),
            "semantic_verification": plan.get("semantic_verification", {}),
            "control": _build_process_control_summary(runtime, operation_control),
            "request_url": request_url,
            "cancellation": cancellation,
            "pause": pause,
        }

    if maybe_pause_for_request():
        runtime["processed_count"] = processed_count
        runtime["failed_count"] = failed_count
        runtime["skipped_count"] = skipped_count
        runtime["superseded_count"] = superseded_count
        return {
            "success": False,
            "processed_count": processed_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "superseded_count": superseded_count,
            "total_chunks": total,
            "warnings": warnings,
            "warning_count": len(warnings),
            "output_files": output_files,
            "aborted": False,
            "aborted_reason": "",
            "paused": True,
            "pause_reason": pause_reason,
            "replan_required": False,
            "replan_reason": "",
            "resume": resume_report,
            "plan": manifest.get("plan", {}),
            "glossary": plan.get("glossary", {}),
            "semantic_verification": plan.get("semantic_verification", {}),
            "control": _build_process_control_summary(runtime, operation_control),
            "request_url": request_url,
            "cancellation": cancellation,
            "pause": pause,
        }

    # Control signals are checked only between chunks so partial chunk outputs
    # are never merged with partially updated manifest state.
    for chunk_index, chunk_info in enumerate(manifest["chunks"]):
        chunk_id = chunk_info["id"]
        if chunk_info.get("status") == SUPERSEDED_CHUNK_STATUS:
            superseded_count += 1
            continue
        if maybe_abort_for_cancellation():
            break
        if maybe_pause_for_request():
            break
        active_index += 1
        runtime["current_chunk_index"] = chunk_index
        input_filename = chunk_info.get(input_key, chunk_info["raw_path"])
        input_path = work_path / input_filename

        if is_summary:
            out_filename = f"summary_chunk_{chunk_id:03d}.txt"
        else:
            out_filename = chunk_info["processed_path"]
        out_path = work_path / out_filename
        output_matches_operation = _chunk_output_matches_operation(
            chunk_info,
            out_path,
            prompt_name=prompt_name,
            input_key=input_key,
        )

        # A durable output file plus `done` status is a valid checkpoint; skip it
        # unless the caller explicitly forces regeneration.
        if not force and chunk_info.get("status") == "done" and output_matches_operation:
            skipped_count += 1
            print(f"Skipping chunk {active_index}/{total} (chunk_id={chunk_id}, output exists at {out_path.name})", file=sys.stderr)
            continue
        if not force and chunk_info.get("status") == "done" and out_path.exists() and not output_matches_operation:
            print(
                f"Reprocessing chunk {active_index}/{total} (chunk_id={chunk_id}) because {out_path.name} belongs to a previous operation",
                file=sys.stderr,
            )

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
        glossary_context = kernel_glossary.build_glossary_prompt_context(
            glossary_payload,
            chunk_text,
            max_terms=max(0, _parse_int_min(config.get("chunk_glossary_max_prompt_terms"), 8, 0)),
        )
        semantic_context = kernel_semantic.build_anchor_prompt_context(
            chunk_text,
            max_items=max(0, _parse_int_min(config.get("chunk_semantic_max_anchors"), 8, 0)),
        )
        prompt = _build_chunk_prompt(
            prompt_template,
            chunk_text,
            continuity_context["text"],
            glossary_context["text"],
            semantic_context["text"],
        )
        actual_prompt_tokens = (
            prompt_budget["prompt_template_tokens"]
            + continuity_context["token_count"]
            + _estimate_tokens(glossary_context["text"], "tokens", config)
            + _estimate_tokens(semantic_context["text"], "tokens", config)
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
        chunk_info["glossary_terms"] = [str(entry.get("term", "")).strip() for entry in glossary_context.get("terms", []) if str(entry.get("term", "")).strip()]
        chunk_info["glossary_term_count"] = len(chunk_info["glossary_terms"])
        chunk_info["glossary_context_tokens"] = _estimate_tokens(glossary_context["text"], "tokens", config) if glossary_context.get("text") else 0
        chunk_info["semantic_anchors"] = list(semantic_context.get("anchors", {}).get("ordered", []))
        chunk_info["semantic_anchor_count"] = len(chunk_info["semantic_anchors"])
        chunk_info["semantic_context_tokens"] = _estimate_tokens(semantic_context["text"], "tokens", config) if semantic_context.get("text") else 0
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
                output_health = _evaluate_chunk_output_health(
                    prompt_name,
                    chunk_id,
                    chunk_char_count,
                    result_text,
                    source_text=chunk_text,
                    glossary_payload=glossary_payload,
                    glossary_max_terms=max(0, _parse_int_min(config.get("chunk_glossary_max_prompt_terms"), 8, 0)),
                )
                consecutive_timeouts = 0

                # Verification-triggered repair stays inside the current plan: we
                # retry the same chunk before escalating to full replanning.
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
                _stamp_chunk_output_operation(
                    chunk_info,
                    prompt_name=prompt_name,
                    input_key=input_key,
                )
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
                # Early timeout instability or an aggressive canary shrink means
                # the current plan is unhealthy, so stop and regenerate the rest.
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

                # Provider/context failures that point to a bad plan should not be
                # retried forever at the chunk level; escalate to replan instead.
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

                # This acts as a circuit breaker for provider instability: stop the
                # run once repeated timeout failures indicate the plan is not viable.
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
        if not aborted and not paused:
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
    elif paused:
        runtime["status"] = PAUSED_RUNTIME_STATUS
    elif failed_count > 0:
        runtime["status"] = "completed_with_errors"
    else:
        runtime["status"] = "completed"
    _refresh_manifest_token_source_summary(manifest)
    _sync_manifest_legacy_fields(manifest)
    _write_manifest(manifest_path, manifest)

    return {
        "success": failed_count == 0 and not aborted and not paused,
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
        "paused": paused,
        "pause_reason": pause_reason,
        "replan_required": runtime.get("replan_required", False),
        "replan_reason": runtime.get("replan_reason", ""),
        "resume": resume_report,
        "plan": manifest.get("plan", {}),
        "glossary": plan.get("glossary", {}),
        "semantic_verification": plan.get("semantic_verification", {}),
        "control": _build_process_control_summary(runtime, operation_control),
        "request_url": request_url,
        "cancellation": cancellation,
        "pause": kernel_state.summarize_runtime_pause_request(work_dir),
    }

def _replan_remaining_impl(work_dir: str, prompt_name: str = "", config_path: str = None,
                           chunk_size: int = 0, input_key: str = "raw_path") -> dict:
    """Regenerate chunking for the remaining unprocessed source content."""
    _bind_utils_globals()
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

def _process_chunks_with_replans_impl(work_dir: str, prompt_name: str, extra_instruction: str = "",
                                      config_path: str = None, input_key: str = "raw_path",
                                      force: bool = False, max_replans: int = 3,
                                      runtime_ownership: dict | None = None) -> dict:
    """Run chunk processing with bounded automatic replan attempts."""
    _bind_utils_globals()
    def current_superseded_count() -> int:
        """Return the current superseded count."""
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

    return kernel_controller.run_auto_replan_loop(
        work_dir=work_dir,
        prompt_name=prompt_name,
        extra_instruction=extra_instruction,
        config_path=config_path,
        input_key=input_key,
        force=force,
        max_replans=max_replans,
        runtime_ownership=runtime_ownership,
        process_chunks_fn=process_chunks,
        replan_remaining_fn=replan_remaining,
        current_superseded_count_fn=current_superseded_count,
    )
