"""Main fixed-chunk processing loop."""

from __future__ import annotations

_LOCAL_NAMES = {
    '_bind_utils_globals',
    '_process_chunks_impl',
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
    """Process all active chunks of the plan with retries, repair, and resume support."""
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
    stream_mode = _normalize_stream_mode(config.get("llm_stream", "auto"))
    request_stream_mode = stream_mode
    stream_decision_reason = "configured_false" if stream_mode == "false" else "configured"
    reasoning_probe = _build_reasoning_probe_record(model, api_format)
    reasoning_probe["enabled"] = _llm_reasoning_probe_enabled(config)
    if stream_decision_reason == "configured_false":
        reasoning_probe["reason"] = "streaming_forced_false"
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
    output_retry_attempt_limit = _parse_int_min(
        config.get("llm_output_retry_attempts"),
        DEFAULT_OUTPUT_RETRY_ATTEMPTS,
        0,
    )
    # Set when the provider rejects a larger max_tokens; later retries stay below it.
    output_retry_ceiling = None
    operation_control = _build_operation_control_contract(
        "chunk",
        prompt_name,
        input_key=input_key,
        config=config,
        bilingual=prompt_name == "translate_only",
    )

    api_request_url = _build_api_url(base_url, api_format) if base_url else ""
    previous_runtime = manifest.get("runtime", {}) if isinstance(manifest.get("runtime", {}), dict) else {}
    # llm_backend=auto sends chunks to the calling agent's own CLI when one is
    # found and answers a probe; the configured API is the fallback.
    backend_decision = kernel_agent_cli.resolve_llm_backend(
        config,
        probe=not dry_run,
        previous=previous_runtime.get("llm_backend"),
    )
    use_cli = backend_decision["backend"] == "cli"
    cli_timeout_sec = _parse_int_min(config.get("llm_cli_timeout_sec"), kernel_agent_cli.DEFAULT_CLI_TIMEOUT_SEC, 1)
    cli_max_retries = _parse_int_min(config.get("llm_cli_max_retries"), kernel_agent_cli.DEFAULT_CLI_MAX_RETRIES, 0)
    request_url = f"cli:{backend_decision['agent']}" if use_cli else api_request_url
    if not dry_run and backend_decision["backend"] == "none":
        print(
            "Error: no LLM backend available. "
            f"{backend_decision['fallback_reason'] or 'Agent CLI unavailable'}; "
            "and the fallback API is not configured (set llm_api_key, llm_base_url, llm_model in config.yaml)",
            file=sys.stderr,
        )
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
    manifest_chunk_size = max(0, _parse_int(plan.get("chunk_size"), manifest.get("chunk_size", 0)))
    manifest_chunk_mode = _normalize_chunk_mode(plan.get("chunk_mode", manifest.get("chunk_mode", config.get("chunk_mode", DEFAULT_CHUNK_MODE))))
    plan_target_tokens = max(1, _parse_int(plan.get("target_input_tokens", plan.get("target_tokens", prompt_budget["target_tokens"])), prompt_budget["target_tokens"]))
    plan_hard_cap_tokens = max(plan_target_tokens, _parse_int(plan.get("hard_cap_tokens"), prompt_budget["hard_cap_tokens"]))
    planned_max_output_tokens = max(1, _parse_int(plan.get("planned_max_output_tokens"), prompt_budget["planned_max_output_tokens"]))
    if manifest_chunk_mode == "chars":
        recommended_chunk_size = _legacy_chunk_target_chars(prompt_name, config)
    else:
        recommended_chunk_size = _get_task_chunk_target(prompt_name, config)
    setup_warnings = []
    # Backend choice is informational (stderr + runtime.llm_backend), not a warning.
    if use_cli:
        fallback_note = "the configured API is the fallback" if backend_decision["api_configured"] else "no API fallback is configured"
        print(
            f"ℹ️ Chunk requests use the {backend_decision['agent']} CLI "
            f"({' '.join(backend_decision['command'])}); {fallback_note}.",
            file=sys.stderr,
        )
    elif backend_decision["mode"] == "auto" and backend_decision["fallback_reason"]:
        print(f"ℹ️ Chunk requests use the configured API: {backend_decision['fallback_reason']}", file=sys.stderr)
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
    runtime["llm_stream_decision"] = {
        "configured_stream_mode": stream_mode,
        "request_stream_mode": request_stream_mode,
        "reason": stream_decision_reason,
    }
    runtime["reasoning_probe"] = reasoning_probe
    runtime["llm_backend"] = backend_decision
    runtime_control = _ensure_runtime_control_state(runtime)
    runtime_control["verification_warning_count"] = 0
    runtime_control["repair_attempted_count"] = 0
    runtime_control["repair_exhausted_count"] = 0
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
    plan["output_ratio"] = prompt_budget["output_ratio"]
    plan["token_count_source"] = prompt_budget["token_count_source"]
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
            "llm_backend": backend_decision,
            "chunk_mode": manifest_chunk_mode,
            "recommended_chunk_size": recommended_chunk_size,
            "planned_max_output_tokens": planned_max_output_tokens,
            "prompt_tokens": prompt_budget["prompt_tokens"],
            "prompt_template_tokens": prompt_budget["prompt_template_tokens"],
            "target_tokens": plan_target_tokens,
            "hard_cap_tokens": plan_hard_cap_tokens,
            "token_count_source": manifest.get("token_count_source", ""),
            "plan": manifest.get("plan", {}),
            "glossary": plan.get("glossary", {}),
            "resume": resume_report,
            "control": _build_process_control_summary(runtime, operation_control),
            "warnings": setup_warnings,
            "cancellation": kernel_state.summarize_runtime_cancel_request(work_dir),
            "pause": kernel_state.summarize_runtime_pause_request(work_dir),
            "message": "Dry run: all validations passed"
        }

    runtime["status"] = "running"
    runtime["run_id"] = kernel_runtime.new_trace_id("process-chunks")
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
    active_index = 0

    def record_stream_decision() -> None:
        """Persist the current stream/probe decision into runtime metadata."""
        runtime["llm_stream_decision"] = {
            "configured_stream_mode": stream_mode,
            "request_stream_mode": request_stream_mode,
            "reason": stream_decision_reason,
        }
        runtime["reasoning_probe"] = reasoning_probe

    def ensure_reasoning_stream_decision() -> None:
        """Probe unknown models once before the first live chunk request."""
        nonlocal request_stream_mode, stream_decision_reason, reasoning_probe
        if use_cli or request_stream_mode == "false" or stream_mode not in {"auto", "true"}:
            return
        if reasoning_probe.get("attempted", False) or reasoning_probe.get("reason") != "not_attempted":
            return

        reasoning_probe = _probe_llm_reasoning_metadata(
            api_key=api_key,
            base_url=base_url,
            model=model,
            api_format=api_format,
            config=config,
        )
        if reasoning_probe.get("detected", False):
            request_stream_mode = "false"
            stream_decision_reason = "reasoning_metadata_probe"
            setup_warning = (
                f"ℹ️ Streaming disabled after reasoning metadata probe for model '{model}' "
                "so truncation retries can see how many tokens went to reasoning."
            )
            warnings.append(setup_warning)
            print(setup_warning, file=sys.stderr)
        elif reasoning_probe.get("reason") == "probe_failed":
            setup_warning = (
                f"⚠️ Reasoning metadata probe failed for model '{model}' "
                f"({reasoning_probe.get('error_type', 'unknown')}); continuing with llm_stream={stream_mode}."
            )
            warnings.append(setup_warning)
            print(setup_warning, file=sys.stderr)
        else:
            stream_decision_reason = "configured"
        record_stream_decision()
        _write_manifest(manifest_path, manifest)

    def call_chunk_llm(prompt_text: str, max_output_tokens: int) -> dict:
        """Send one chunk prompt to the agent CLI, or to the API as fallback."""
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
            except LLMRequestError as error:
                if not backend_decision["api_configured"]:
                    raise
                use_cli = False
                request_url = api_request_url
                backend_decision["backend"] = "api"
                backend_decision["fallback_reason"] = f"Agent CLI failed mid-run: {error}"
                runtime["llm_backend"] = backend_decision
                runtime["last_request_url"] = request_url
                fallback_warning = (
                    f"⚠️ {backend_decision['agent']} CLI failed ({error.error_type}); "
                    "switching the remaining chunks to the configured API."
                )
                warnings.append(fallback_warning)
                print(fallback_warning, file=sys.stderr)
                ensure_reasoning_stream_decision()
        return _call_llm_api(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": prompt_text}],
            api_format=api_format,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            backoff_sec=backoff_sec,
            stream_mode=request_stream_mode,
            max_tokens=max_output_tokens,
        )

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
            "resume": resume_report,
            "plan": manifest.get("plan", {}),
            "glossary": plan.get("glossary", {}),
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
            "resume": resume_report,
            "plan": manifest.get("plan", {}),
            "glossary": plan.get("glossary", {}),
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
            chunk_info["updated_at"] = _now_iso()
            _write_manifest(manifest_path, manifest)
            continue

        ensure_reasoning_stream_decision()
        chunk_text = input_path.read_text(encoding="utf-8")
        chunk_char_count = len(chunk_text)
        planned_max_output_tokens = max(
            1,
            _parse_int(chunk_info.get("planned_max_output_tokens"), plan.get("planned_max_output_tokens", planned_max_output_tokens)),
        )
        estimated_input_tokens, token_count_source = _estimate_chunk_input_tokens(
            chunk_info,
            input_key,
            chunk_text,
            config,
        )
        glossary_context = kernel_glossary.build_glossary_prompt_context(
            glossary_payload,
            chunk_text,
            max_terms=max(0, _parse_int_min(config.get("chunk_glossary_max_prompt_terms"), 8, 0)),
        )
        prompt = _build_chunk_prompt(
            prompt_template,
            chunk_text,
            glossary_text=glossary_context["text"],
        )
        actual_prompt_tokens = (
            prompt_budget["prompt_template_tokens"]
            + _estimate_tokens(glossary_context["text"], "tokens", config)
        )

        chunk_info["input_chars"] = chunk_char_count
        chunk_info["estimated_input_tokens"] = estimated_input_tokens
        chunk_info["input_tokens"] = estimated_input_tokens
        chunk_info["token_count_source"] = token_count_source
        chunk_info["prompt_tokens"] = actual_prompt_tokens
        chunk_info["planned_max_output_tokens"] = planned_max_output_tokens
        chunk_info["glossary_terms"] = [str(entry.get("term", "")).strip() for entry in glossary_context.get("terms", []) if str(entry.get("term", "")).strip()]
        chunk_info["glossary_term_count"] = len(chunk_info["glossary_terms"])
        chunk_info["glossary_context_tokens"] = _estimate_tokens(glossary_context["text"], "tokens", config) if glossary_context.get("text") else 0

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
            f"est_source={token_count_source} prompt_tokens={actual_prompt_tokens} "
            f"planned_max_output_tokens={planned_max_output_tokens} "
            f"model={backend_decision['agent'] if use_cli else model} url={request_url}",
            file=sys.stderr,
        )

        chunk_attempt_logs = []
        pre_expansion_max_tokens = None
        while True:
            try:
                llm_result = call_chunk_llm(prompt, planned_max_output_tokens)
                attempt_logs = _collect_attempt_logs(llm_result)
                request_attempts = max(len(attempt_logs), _parse_int(llm_result.get("attempts"), 1))
                chunk_attempt_logs.extend(attempt_logs)
                chunk_info["attempts"] = chunk_info.get("attempts", 0) + request_attempts
                chunk_info["attempt_logs"] = list(chunk_info.get("attempt_logs", [])) + attempt_logs

                result_text = llm_result["text"]
                chunk_info["finish_reason"] = str(llm_result.get("finish_reason", "") or "")
                chunk_info["usage"] = llm_result.get("usage", {}) if isinstance(llm_result.get("usage", {}), dict) else {}
                reasoning_stats = _reasoning_usage_stats(llm_result)
                chunk_info["reasoning_tokens"] = reasoning_stats["reasoning_tokens"]
                chunk_info["completion_tokens"] = reasoning_stats["completion_tokens"]
                chunk_info["content_tokens"] = reasoning_stats["content_tokens"]
                chunk_info["reasoning_ratio"] = round(reasoning_stats["reasoning_ratio"], 4)
                if reasoning_stats["completion_tokens"] > 0 and reasoning_stats["reasoning_ratio"] > 0.9:
                    reasoning_warning = (
                        f"⚠️ Chunk {chunk_id}: reasoning tokens are "
                        f"{reasoning_stats['reasoning_tokens']}/{reasoning_stats['completion_tokens']} "
                        f"completion tokens ({reasoning_stats['reasoning_ratio']:.0%})."
                    )
                    warnings.append(reasoning_warning)
                    print(reasoning_warning, file=sys.stderr)
                # Any provider that stops at max_tokens reports it in
                # finish_reason; retry with a higher ceiling so the answer has
                # room even after the model's reasoning spends part of it.
                output_truncated = _is_output_budget_exhausted(llm_result, result_text, planned_max_output_tokens)
                pre_expansion_max_tokens = None
                if output_truncated and chunk_info.get("budget_recovery_attempts", 0) < output_retry_attempt_limit:
                    expanded_max_tokens = _next_output_retry_max_tokens(
                        planned_max_output_tokens,
                        config,
                        observed_reasoning_tokens=reasoning_stats["reasoning_tokens"],
                    )
                    if output_retry_ceiling is not None:
                        expanded_max_tokens = min(expanded_max_tokens, output_retry_ceiling)
                    if expanded_max_tokens > planned_max_output_tokens:
                        budget_warning = _build_output_budget_warning(
                            chunk_id,
                            reasoning_stats,
                            chunk_info["finish_reason"],
                            planned_max_output_tokens,
                        )
                        warnings.append(budget_warning)
                        print(budget_warning, file=sys.stderr)
                        _append_chunk_recovery_log(
                            chunk_info,
                            action="expand_max_tokens",
                            reasons=["output_budget_exhausted"],
                            details=[budget_warning],
                            request_attempts=request_attempts,
                            request_url=llm_result.get("request_url", request_url),
                            latency_ms=llm_result.get("latency_ms"),
                            sleep_sec=chunk_recovery_backoff_sec,
                            from_max_tokens=planned_max_output_tokens,
                            to_max_tokens=expanded_max_tokens,
                        )
                        runtime_control["repair_attempted_count"] = runtime_control.get("repair_attempted_count", 0) + 1
                        pre_expansion_max_tokens = planned_max_output_tokens
                        planned_max_output_tokens = expanded_max_tokens
                        chunk_info["planned_max_output_tokens"] = planned_max_output_tokens
                        chunk_info["status"] = "pending"
                        chunk_info["last_error"] = budget_warning
                        chunk_info["last_error_type"] = "output_budget_exhausted"
                        chunk_info["error_type"] = "output_budget_exhausted"
                        chunk_info["request_url"] = llm_result.get("request_url", request_url)
                        chunk_info["latency_ms"] = llm_result.get("latency_ms")
                        chunk_info["streaming_used"] = bool(llm_result.get("streaming_used", False))
                        chunk_info["updated_at"] = _now_iso()
                        _refresh_manifest_token_source_summary(manifest)
                        _sync_manifest_legacy_fields(manifest)
                        _write_manifest(manifest_path, manifest)
                        if chunk_recovery_backoff_sec > 0:
                            time.sleep(chunk_recovery_backoff_sec)
                        continue
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
                if output_truncated:
                    truncation_warning = (
                        f"⚠️ Chunk {chunk_id}: output is still cut off at max_tokens={planned_max_output_tokens} "
                        f"(finish_reason={chunk_info['finish_reason'] or 'n/a'}) after "
                        f"{chunk_info.get('budget_recovery_attempts', 0)} larger-ceiling retries; the chunk is incomplete."
                    )
                    output_health["warnings"].insert(0, truncation_warning)
                    output_health["retry_reasons"].insert(0, "truncated_output")
                # Repeating the same request cannot fix truncation, so only the
                # other reasons trigger a same-budget retry.
                quality_retry_reasons = [reason for reason in output_health["retry_reasons"] if reason != "truncated_output"]

                # Verification-triggered repair retries the same chunk.
                if quality_retry_reasons and chunk_info.get("recovery_attempts", 0) < chunk_recovery_attempt_limit:
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

                repair_exhausted = output_truncated or (
                    bool(quality_retry_reasons) and chunk_info.get("recovery_attempts", 0) >= chunk_recovery_attempt_limit
                )
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
                chunk_info["request_url"] = llm_result["request_url"]
                chunk_info["streaming_used"] = llm_result["streaming_used"]
                chunk_info["completed_at"] = _now_iso()
                chunk_info["updated_at"] = chunk_info["completed_at"]
                print(
                    f"Completed chunk {active_index}/{total} chunk_id={chunk_id} status=done "
                    f"input_chars={chunk_char_count} estimated_input_tokens={estimated_input_tokens} "
                    f"planned_max_output_tokens={planned_max_output_tokens} latency_ms={llm_result['latency_ms']} "
                    f"attempts={chunk_info['attempts']} streaming_used={llm_result['streaming_used']} "
                    f"output_chars={result_char_count} actual_output_tokens={actual_output_tokens} "
                    f"recovery_attempts={chunk_info.get('recovery_attempts', 0)}",
                    file=sys.stderr,
                )
                break
            except LLMRequestError as error:
                attempt_logs = _collect_attempt_logs(error)
                request_attempts = max(len(attempt_logs), _parse_int(getattr(error, "attempts", 1), 1))
                chunk_attempt_logs.extend(attempt_logs)
                if pre_expansion_max_tokens is not None and getattr(error, "status_code", None) in {400, 422}:
                    # The provider rejected the larger ceiling: keep the last
                    # accepted one as this run's limit and redo the chunk at it.
                    rejected_max_tokens = planned_max_output_tokens
                    output_retry_ceiling = pre_expansion_max_tokens
                    planned_max_output_tokens = pre_expansion_max_tokens
                    pre_expansion_max_tokens = None
                    chunk_info["attempts"] = chunk_info.get("attempts", 0) + request_attempts
                    chunk_info["attempt_logs"] = list(chunk_info.get("attempt_logs", [])) + attempt_logs
                    chunk_info["planned_max_output_tokens"] = planned_max_output_tokens
                    rejected_warning = (
                        f"⚠️ Chunk {chunk_id}: provider rejected max_tokens={rejected_max_tokens} "
                        f"({error.error_type}); keeping max_tokens={planned_max_output_tokens} for this run."
                    )
                    warnings.append(rejected_warning)
                    print(rejected_warning, file=sys.stderr)
                    _write_manifest(manifest_path, manifest)
                    continue
                failed_count += 1
                chunk_info["status"] = "failed"
                chunk_info["attempts"] = chunk_info.get("attempts", 0) + request_attempts
                chunk_info["attempt_logs"] = list(chunk_info.get("attempt_logs", [])) + attempt_logs
                chunk_info["last_error"] = str(error)
                chunk_info["last_error_type"] = error.error_type
                chunk_info["error_type"] = error.error_type
                chunk_info["request_url"] = error.request_url or request_url
                chunk_info["updated_at"] = _now_iso()
                print(
                    f"Chunk {active_index}/{total} chunk_id={chunk_id} status=failed "
                    f"input_chars={chunk_char_count} estimated_input_tokens={estimated_input_tokens} "
                    f"planned_max_output_tokens={planned_max_output_tokens} latency_ms={chunk_info.get('latency_ms')} "
                    f"attempts={getattr(error, 'attempts', 1)} streaming_used={chunk_info.get('streaming_used', False)} "
                    f"error_type={error.error_type} url={error.request_url or request_url} error={error}",
                    file=sys.stderr,
                )

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
        "resume": resume_report,
        "plan": manifest.get("plan", {}),
        "glossary": plan.get("glossary", {}),
        "control": _build_process_control_summary(runtime, operation_control),
        "request_url": request_url,
        "llm_backend": backend_decision,
        "cancellation": cancellation,
        "pause": kernel_state.summarize_runtime_pause_request(work_dir),
    }

