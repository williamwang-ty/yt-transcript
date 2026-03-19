"""Regression tests for runtime control and long-text execution behavior."""

from tests._support import *


class RuntimeRegressionTests(unittest.TestCase):
    """Regression coverage for runtime ownership, pause, resume, and replan behavior."""
    def test_run_kernel_command_returns_envelope_and_writes_telemetry(self):
        """Test run kernel command returns envelope and writes telemetry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")

            envelope = utils.run_kernel_command(
                "chunk-text",
                input_path=str(source),
                output_dir=str(work_dir),
                chunk_size=4,
                prompt_name="structure_only",
                config_path=None,
            )

            telemetry_path = Path(envelope["telemetry"]["telemetry_path"])
            self.assertEqual(envelope["format"], utils.COMMAND_RESULT_FORMAT)
            self.assertEqual(envelope["command"], "chunk-text")
            self.assertTrue(envelope["ok"])
            self.assertTrue(telemetry_path.exists())
            lines = [line for line in telemetry_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            event = json.loads(lines[-1])
            self.assertEqual(event["format"], utils.TELEMETRY_EVENT_FORMAT)
            self.assertEqual(event["command"], "chunk-text")
            self.assertEqual(event["trace_id"], envelope["trace_id"])
            self.assertTrue(event["success"])
            self.assertEqual(envelope["result"]["driver"], "chunk-text")

    def test_run_kernel_command_includes_contract_bundle_and_telemetry_summary(self):
        """Test run kernel command includes contract bundle and telemetry contract summary."""
        from kernel.task_runtime import contracts as runtime_contracts

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")

            envelope = utils.run_kernel_command(
                "chunk-text",
                input_path=str(source),
                output_dir=str(work_dir),
                chunk_size=4,
                prompt_name="structure_only",
                config_path=None,
            )

            contracts = envelope["contracts"]
            self.assertEqual(contracts["format"], runtime_contracts.CONTRACT_BUNDLE_FORMAT)
            self.assertEqual(contracts["task_spec"]["format"], runtime_contracts.TASK_SPEC_FORMAT)
            self.assertEqual(contracts["run_state"]["format"], runtime_contracts.RUN_STATE_FORMAT)
            self.assertEqual(contracts["action_result"]["format"], runtime_contracts.ACTION_RESULT_FORMAT)
            self.assertEqual(contracts["run_state"]["active_stage"], "planning")
            self.assertEqual(contracts["action_result"]["tool_name"], "chunk-text")
            self.assertGreaterEqual(len(contracts["artifacts"]), 1)
            self.assertEqual(envelope["telemetry"]["contracts"]["action_type"], "chunk_text")
            self.assertEqual(envelope["telemetry"]["contracts"]["active_stage"], "planning")

            telemetry_path = Path(envelope["telemetry"]["telemetry_path"])
            event = json.loads(telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["contracts"]["active_stage"], "planning")
            self.assertEqual(event["contracts"]["action_type"], "chunk_text")

    def test_contract_bundle_derives_quality_report_from_verify_quality(self):
        """Test runtime contract bundle derives a quality report from verify quality output."""
        from kernel.task_runtime import contracts as runtime_contracts

        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "raw.txt"
            optimized = Path(tmpdir) / "optimized.md"
            raw.write_text("这是一大段没有章节标题的文本。" * 100, encoding="utf-8")
            optimized.write_text("这是一大段没有章节标题的文本。" * 120, encoding="utf-8")

            result = utils.verify_quality(str(optimized), str(raw), bilingual=False)
            bundle = runtime_contracts.build_command_contract_bundle(
                "verify-quality",
                result,
                context={"optimized_text_path": str(optimized)},
                trace_id="trace_verify_contract",
            )

            self.assertIn("quality_report", bundle)
            self.assertEqual(bundle["quality_report"]["format"], runtime_contracts.QUALITY_REPORT_FORMAT)
            self.assertFalse(bundle["quality_report"]["passed"])
            self.assertEqual(bundle["quality_report"]["recommended_action"], "repair_or_replan")

    def test_telemetry_summary_reads_local_journal(self):
        """Test telemetry summary reads local journal."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")

            utils.run_kernel_command(
                "chunk-text",
                input_path=str(source),
                output_dir=str(work_dir),
                chunk_size=4,
                prompt_name="structure_only",
                config_path=None,
            )
            utils.run_kernel_command(
                "runtime-status",
                work_dir=str(work_dir),
            )

            result = utils.telemetry_summary(str(work_dir), recent_limit=2)

            self.assertTrue(result["success"])
            self.assertEqual(result["summary"]["matching_event_count"], 2)
            self.assertEqual(result["summary"]["command_counts"].get("chunk-text"), 1)
            self.assertEqual(result["summary"]["command_counts"].get("runtime-status"), 1)
            self.assertEqual(len(result["recent_events"]), 2)

    def test_telemetry_events_filter_by_command_and_limit(self):
        """Test telemetry events filter by command and limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")

            utils.run_kernel_command(
                "chunk-text",
                input_path=str(source),
                output_dir=str(work_dir),
                chunk_size=4,
                prompt_name="structure_only",
                config_path=None,
            )
            utils.run_kernel_command(
                "pause-run",
                work_dir=str(work_dir),
                reason="query me",
            )
            utils.run_kernel_command(
                "runtime-status",
                work_dir=str(work_dir),
            )

            telemetry_path = work_dir / "telemetry.jsonl"
            result = utils.telemetry_events(
                str(telemetry_path),
                command_filter="pause-run",
                limit=1,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["matching_event_count"], 1)
            self.assertEqual(result["returned_count"], 1)
            self.assertEqual(result["events"][0]["command"], "pause-run")

    def test_cli_api_envelope_wraps_telemetry_summary(self):
        """Test cli api envelope wraps telemetry summary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.run_kernel_command(
                "chunk-text",
                input_path=str(source),
                output_dir=str(work_dir),
                chunk_size=4,
                prompt_name="structure_only",
                config_path=None,
            )

            result = subprocess.run(
                [
                    "python3",
                    str(PROJECT_ROOT / "yt_transcript_utils.py"),
                    "--api-envelope",
                    "telemetry-summary",
                    str(work_dir),
                ],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["format"], utils.COMMAND_RESULT_FORMAT)
            self.assertEqual(payload["command"], "telemetry-summary")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["result"]["success"])
            self.assertGreaterEqual(payload["result"]["summary"]["matching_event_count"], 1)

    def test_build_glossary_extracts_terms_from_work_dir(self):
        """Test build glossary extracts terms from work dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("OpenAI API 与 Deepgram SDK 在 YouTube 工作流里一起使用。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 80, "structure_only")

            result = utils.build_glossary(str(work_dir), max_terms=10, min_occurrences=1)

            self.assertTrue(result["success"])
            glossary_path = Path(result["glossary_path"])
            self.assertTrue(glossary_path.exists())
            terms = [entry["term"] for entry in result["terms"]]
            self.assertIn("OpenAI", terms)
            self.assertIn("API", terms)
            self.assertIn("Deepgram", terms)

    def test_process_chunks_injects_glossary_terms_into_prompt(self):
        """Test process chunks injects glossary terms into prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("OpenAI API 设计。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 80, "structure_only")
            utils.build_glossary(str(work_dir), max_terms=10, min_occurrences=1)

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            captured = {}

            def fake_llm(*args, **kwargs):
                """Fake llm."""
                captured["prompt"] = kwargs["messages"][0]["content"]
                return {
                    "text": "## 结果\n\nOpenAI API 设计。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                }

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=fake_llm,
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            self.assertTrue(result["success"])
            self.assertIn("Terminology Guardrails", captured["prompt"])
            self.assertIn("OpenAI", captured["prompt"])
            self.assertIn("API", captured["prompt"])
            self.assertEqual(result["glossary"]["mode"], "local_file")

    def test_process_chunks_retries_when_glossary_terms_are_missing(self):
        """Test process chunks retries when glossary terms are missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("OpenAI API 发布。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 80, "structure_only")
            utils.build_glossary(str(work_dir), max_terms=10, min_occurrences=1)

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "llm_chunk_recovery_attempts": 1,
            })

            responses = [
                {
                    "text": "## 结果\n\n发布。",
                    "latency_ms": 10,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
                {
                    "text": "## 结果\n\nOpenAI API 发布。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ]

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=responses,
            ) as mocked_call:
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertEqual(mocked_call.call_count, 2)
            self.assertEqual(manifest["chunks"][0]["recovery_attempts"], 1)
            output = (work_dir / manifest["chunks"][0]["processed_path"]).read_text(encoding="utf-8")
            self.assertIn("OpenAI API", output)

    def test_process_chunks_injects_semantic_anchor_guardrails(self):
        """Test process chunks injects semantic anchor guardrails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("访问 https://example.com 于 2026-03-08 完成 32% 进度。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 120, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            captured = {}

            def fake_llm(*args, **kwargs):
                """Fake llm."""
                captured["prompt"] = kwargs["messages"][0]["content"]
                return {
                    "text": "## 结果\n\n访问 https://example.com 于 2026-03-08 完成 32% 进度。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                }

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=fake_llm,
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            self.assertTrue(result["success"])
            self.assertIn("Semantic Anchors", captured["prompt"])
            self.assertIn("https://example.com", captured["prompt"])
            self.assertIn("2026-03-08", captured["prompt"])
            self.assertIn("32%", captured["prompt"])
            self.assertEqual(result["semantic_verification"]["mode"], "anchor_checks")

    def test_process_chunks_retries_when_semantic_anchor_missing(self):
        """Test process chunks retries when semantic anchor missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("本季度增长 32%。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 120, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "llm_chunk_recovery_attempts": 1,
            })

            responses = [
                {
                    "text": "## 结果\n\n本季度增长显著。",
                    "latency_ms": 10,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
                {
                    "text": "## 结果\n\n本季度增长 32%。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ]

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=responses,
            ) as mocked_call:
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertEqual(mocked_call.call_count, 2)
            self.assertEqual(manifest["chunks"][0]["recovery_attempts"], 1)
            self.assertIn("32%", (work_dir / manifest["chunks"][0]["processed_path"]).read_text(encoding="utf-8"))

    def test_verify_quality_reports_missing_semantic_anchors(self):
        """Test verify quality reports missing semantic anchors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "raw.txt"
            optimized = Path(tmpdir) / "optimized.txt"
            raw.write_text("访问 https://example.com 于 2026-03-08 完成 32% 进度。", encoding="utf-8")
            optimized.write_text("## 结果\n\n已完成进度。", encoding="utf-8")

            result = utils.verify_quality(str(optimized), raw_text_path=str(raw), bilingual=False)

            self.assertFalse(result["checks"]["semantic_anchor_coverage_ok"])
            self.assertGreater(result["checks"]["semantic_missing_count"], 0)
            self.assertTrue(any("Semantic anchors" in warning for warning in result["warnings"]))

    def test_verify_quality_rejects_missing_structure(self):
        """Test verify quality rejects missing structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text("这是一大段没有章节标题的文本。" * 120, encoding="utf-8")
            raw.write_text("这是一大段没有章节标题的文本。" * 100, encoding="utf-8")

            result = utils.verify_quality(str(optimized), str(raw), bilingual=False)

            self.assertFalse(result["passed"])
            self.assertTrue(any("section headers" in failure for failure in result["hard_failures"]))

    def test_verify_quality_checks_bilingual_pairs(self):
        """Test verify quality checks bilingual pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "English paragraph one.\n\n"
                "中文段落一。\n\n"
                "English paragraph two.\n\n"
                "中文段落二。\n",
                encoding="utf-8",
            )
            raw.write_text("English paragraph one. English paragraph two.", encoding="utf-8")

            result = utils.verify_quality(str(optimized), str(raw), bilingual=True)

            self.assertTrue(result["passed"], result)
            self.assertGreaterEqual(result["checks"]["bilingual_pairs"], 1)

    def test_verify_quality_rejects_bilingual_without_pairs(self):
        """Test verify quality rejects bilingual without pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "English paragraph one.\n\n"
                "English paragraph two.\n",
                encoding="utf-8",
            )
            raw.write_text("English paragraph one. English paragraph two.", encoding="utf-8")

            result = utils.verify_quality(str(optimized), str(raw), bilingual=True)

            self.assertFalse(result["passed"])
            self.assertTrue(any("paragraph pairs" in failure for failure in result["hard_failures"]))

    def test_verify_quality_passes_when_only_warnings_exist(self):
        """Test verify quality passes when only warnings exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "第一段内容，结构完整。\n\n"
                "第二段内容，也有句号。\n",
                encoding="utf-8",
            )
            raw.write_text("原文。", encoding="utf-8")

            result = utils.verify_quality(str(optimized), str(raw), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["hard_failures"], [])
            self.assertTrue(result["warnings"])

    def test_process_chunks_dry_run_does_not_require_llm_credentials(self):
        """Test process chunks dry run does not require llm credentials."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 50, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "",
                "llm_base_url": "",
                "llm_model": "",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", side_effect=AssertionError("dry-run should not require load_config")), mock.patch.object(
                utils,
                "_load_optional_config",
                return_value=config,
            ):
                result = utils.process_chunks(str(work_dir), "structure_only", dry_run=True)

            self.assertTrue(result["success"])
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["request_url"], "")

    def test_runtime_status_reports_manifest_runtime_ownership_and_counts(self):
        """Test runtime status reports manifest runtime ownership and counts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "done"
            manifest["chunks"][1]["status"] = "failed"
            manifest["runtime"]["status"] = "running"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            ownership = utils.kernel_runtime.acquire_runtime_ownership(str(work_dir), "status-test")
            try:
                result = utils.runtime_status(str(work_dir))
            finally:
                utils.kernel_runtime.release_runtime_ownership(str(work_dir), ownership["owner_id"])

            self.assertTrue(result["success"])
            self.assertTrue(result["manifest_present"])
            self.assertEqual(result["runtime"]["status"], "running")
            self.assertEqual(result["total_chunks"], len(manifest["chunks"]))
            self.assertEqual(result["completed_chunks"], 1)
            self.assertEqual(result["failed_chunks"], 1)
            self.assertEqual(result["ownership"]["status"], "held")
            self.assertTrue(result["ownership"]["held"])
            self.assertFalse(result["cancellation"]["requested"])
            self.assertIn("lifecycle", result)
            self.assertEqual(result["lifecycle"]["command"], "runtime-status")
            self.assertEqual(result["lifecycle"]["active_stage"], "processing")

    def test_pause_run_includes_lifecycle_transition_and_telemetry_summary(self):
        """Test pause run exposes lifecycle transition data directly and via telemetry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            result = utils.pause_run(str(work_dir), reason="operator pause")
            self.assertTrue(result["success"])
            self.assertIn("lifecycle", result)
            self.assertEqual(result["lifecycle"]["command"], "pause-run")
            self.assertEqual(result["lifecycle"]["active_stage"], "processing")
            self.assertEqual(result["lifecycle"]["state_after"], "processing")
            self.assertEqual(result["lifecycle"]["control_signal"], "pause_requested")

            envelope = utils.run_kernel_command(
                "pause-run",
                work_dir=str(work_dir),
                reason="operator pause again",
            )
            telemetry_path = Path(envelope["telemetry"]["telemetry_path"])
            event = json.loads(telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["lifecycle"]["active_stage"], "processing")
            self.assertEqual(event["lifecycle"]["state_after"], "processing")
            self.assertEqual(event["lifecycle"]["control_signal"], "pause_requested")

    def test_cancel_run_marks_runtime_cancel_request(self):
        """Test cancel run marks runtime cancel request."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            result = utils.cancel_run(str(work_dir), reason="user requested stop")
            status = utils.runtime_status(str(work_dir))

            self.assertTrue(result["success"])
            self.assertTrue(result["requested"])
            self.assertEqual(result["reason"], "user requested stop")
            self.assertTrue(status["cancellation"]["requested"])
            self.assertEqual(status["cancellation"]["reason"], "user requested stop")

    def test_process_chunks_aborts_when_cancel_requested(self):
        """Test process chunks aborts when cancel requested."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")
            utils.cancel_run(str(work_dir), reason="operator stop")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=AssertionError("cancelled run should not call LLM"),
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(result["success"])
            self.assertTrue(result["aborted"])
            self.assertIn("Cancellation requested", result["aborted_reason"])
            self.assertTrue(result["cancellation"]["consumed"])
            self.assertTrue(result["cancellation"]["cleared"])
            self.assertFalse((work_dir / utils.kernel_state.RUNTIME_CANCEL_FILENAME).exists())
            self.assertEqual(manifest["runtime"]["status"], "aborted")
            self.assertEqual(manifest["runtime"]["last_cancel_reason"], "operator stop")

    def test_pause_run_marks_runtime_pause_request(self):
        """Test pause run marks runtime pause request."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            result = utils.pause_run(str(work_dir), reason="operator requested pause")
            status = utils.runtime_status(str(work_dir))

            self.assertTrue(result["success"])
            self.assertTrue(result["requested"])
            self.assertEqual(result["reason"], "operator requested pause")
            self.assertTrue(status["pause"]["requested"])
            self.assertEqual(status["pause"]["reason"], "operator requested pause")

    def test_process_chunks_pauses_when_pause_requested(self):
        """Test process chunks pauses when pause requested."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")
            utils.pause_run(str(work_dir), reason="operator pause")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=AssertionError("paused run should not call LLM"),
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(result["success"])
            self.assertTrue(result["paused"])
            self.assertFalse(result["aborted"])
            self.assertEqual(result["pause_reason"], "operator pause")
            self.assertTrue(result["pause"]["requested"])
            self.assertTrue((work_dir / utils.kernel_state.RUNTIME_PAUSE_FILENAME).exists())
            self.assertEqual(manifest["runtime"]["status"], utils.PAUSED_RUNTIME_STATUS)
            self.assertEqual(manifest["runtime"]["last_pause_reason"], "operator pause")

    def test_process_chunks_pauses_at_safe_boundary_between_chunks(self):
        """Test process chunks pauses at safe boundary between chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。第五句。第六句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 6, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            seen_calls = 0

            def fake_llm(*args, **kwargs):
                """Fake llm."""
                nonlocal seen_calls
                seen_calls += 1
                if seen_calls == 1:
                    utils.pause_run(str(work_dir), reason="pause after first chunk")
                return {
                    "text": "## 结果\n\n处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                }

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=fake_llm,
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(result["success"])
            self.assertTrue(result["paused"])
            self.assertEqual(result["processed_count"], 1)
            self.assertEqual(seen_calls, 1)
            self.assertEqual(manifest["runtime"]["status"], utils.PAUSED_RUNTIME_STATUS)
            self.assertEqual(manifest["chunks"][0]["status"], "done")
            self.assertTrue(any(chunk["status"] == "pending" for chunk in manifest["chunks"][1:]))

    def test_resume_run_clears_pause_and_marks_runtime_resumable(self):
        """Test resume run clears pause and marks runtime resumable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")
            utils.pause_run(str(work_dir), reason="operator pause")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runtime"]["status"] = utils.PAUSED_RUNTIME_STATUS
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            result = utils.resume_run(str(work_dir), reason="continue")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertTrue(result["resumed"])
            self.assertEqual(result["runtime_status_before"], utils.PAUSED_RUNTIME_STATUS)
            self.assertEqual(result["runtime_status_after"], utils.RESUMABLE_RUNTIME_STATUS)
            self.assertFalse((work_dir / utils.kernel_state.RUNTIME_PAUSE_FILENAME).exists())
            self.assertEqual(manifest_after["runtime"]["status"], utils.RESUMABLE_RUNTIME_STATUS)
            self.assertEqual(manifest_after["runtime"]["last_resume_reason"], "continue")

    def test_cli_api_envelope_wraps_pause_and_resume_run(self):
        """Test cli api envelope wraps pause and resume run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            pause_envelope = utils.run_kernel_command(
                "pause-run",
                work_dir=str(work_dir),
                reason="api pause",
            )
            self.assertEqual(pause_envelope["command"], "pause-run")
            self.assertTrue(pause_envelope["result"]["success"])

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runtime"]["status"] = utils.PAUSED_RUNTIME_STATUS
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            resume_envelope = utils.run_kernel_command(
                "resume-run",
                work_dir=str(work_dir),
                reason="api resume",
            )
            self.assertEqual(resume_envelope["command"], "resume-run")
            self.assertTrue(resume_envelope["result"]["success"])
            self.assertTrue(resume_envelope["result"]["resumed"])

    def test_cli_api_envelope_wraps_cancel_run(self):
        """Test cli api envelope wraps cancel run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            result = subprocess.run(
                [
                    "python3",
                    str(PROJECT_ROOT / "yt_transcript_utils.py"),
                    "--api-envelope",
                    "cancel-run",
                    str(work_dir),
                    "--reason",
                    "cli stop",
                ],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            telemetry_path = Path(payload["telemetry"]["telemetry_path"])
            self.assertEqual(payload["format"], utils.COMMAND_RESULT_FORMAT)
            self.assertEqual(payload["command"], "cancel-run")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["result"]["requested"])
            self.assertEqual(payload["result"]["reason"], "cli stop")
            self.assertTrue(telemetry_path.exists())

    def test_cli_api_envelope_wraps_runtime_status(self):
        """Test cli api envelope wraps runtime status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            result = subprocess.run(
                [
                    "python3",
                    str(PROJECT_ROOT / "yt_transcript_utils.py"),
                    "--api-envelope",
                    "runtime-status",
                    str(work_dir),
                ],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            telemetry_path = Path(payload["telemetry"]["telemetry_path"])
            self.assertEqual(payload["format"], utils.COMMAND_RESULT_FORMAT)
            self.assertEqual(payload["command"], "runtime-status")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["result"]["manifest_present"])
            self.assertTrue(telemetry_path.exists())

    def test_process_chunks_skips_done_chunks_by_default(self):
        """Test process chunks skips done chunks by default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 50, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "done"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            (work_dir / "processed_000.md").write_text("## 已完成" + chr(10) + chr(10) + "内容", encoding="utf-8")

            with mock.patch.object(utils, "load_config", return_value={
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "llm_timeout_sec": 30,
                "llm_max_retries": 0,
                "llm_backoff_sec": 0.1,
                "llm_stream": "false",
                "llm_stop_after_consecutive_timeouts": 2,
            }), mock.patch.object(utils, "_call_llm_api") as mocked_call:
                result = utils.process_chunks(str(work_dir), "structure_only")

            self.assertTrue(result["success"])
            self.assertEqual(result["skipped_count"], 1)
            mocked_call.assert_not_called()

    def test_process_chunks_rejects_active_runtime_owner(self):
        """Test process chunks rejects active runtime owner."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 8, "structure_only")

            owner_path = work_dir / utils.RUNTIME_OWNER_FILENAME
            owner_path.write_text(
                json.dumps(
                    {
                        "schema_version": utils.RUNTIME_OWNERSHIP_SCHEMA_VERSION,
                        "format": utils.RUNTIME_OWNERSHIP_FORMAT,
                        "owner_id": "other-owner",
                        "operation": "process-chunks",
                        "pid": os.getpid(),
                        "work_dir": str(work_dir.resolve()),
                        "acquired_at": utils._now_iso(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = utils.process_chunks(str(work_dir), "structure_only", dry_run=True)

            self.assertFalse(result["success"])
            self.assertTrue(result["aborted"])
            self.assertEqual(result["ownership"]["status"], "conflict")
            self.assertEqual(result["ownership"]["active_owner"]["owner_id"], "other-owner")
            self.assertTrue(owner_path.exists())

    def test_prepare_resume_recovers_stale_runtime_owner(self):
        """Test prepare resume recovers stale runtime owner."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 8, "structure_only")

            owner_path = work_dir / utils.RUNTIME_OWNER_FILENAME
            owner_path.write_text(
                json.dumps(
                    {
                        "schema_version": utils.RUNTIME_OWNERSHIP_SCHEMA_VERSION,
                        "format": utils.RUNTIME_OWNERSHIP_FORMAT,
                        "owner_id": "stale-owner",
                        "operation": "prepare-resume",
                        "pid": 99999999,
                        "work_dir": str(work_dir.resolve()),
                        "acquired_at": utils._now_iso(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = utils.prepare_resume(str(work_dir), prompt_name="structure_only")

            self.assertTrue(result["success"])
            self.assertEqual(result["ownership"]["status"], "acquired")
            self.assertTrue(result["ownership"]["released"])
            self.assertEqual(result["ownership"]["release_status"], "released")
            self.assertEqual(result["ownership"]["recovered_stale_owner"]["stale_reason"], "dead_process")
            self.assertFalse(owner_path.exists())

    def test_process_chunks_with_replans_shares_runtime_owner_across_steps(self):
        """Test process chunks with replans shares runtime owner across steps."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。第五句。第六句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            seen_calls = []
            process_call_count = {"count": 0}

            def fake_process_chunks(*args, **kwargs):
                """Fake process chunks."""
                ownership = kwargs.get("runtime_ownership") or {}
                seen_calls.append(("process", ownership.get("owner_id", ""), bool(ownership.get("delegated", False))))
                if process_call_count["count"] == 0:
                    process_call_count["count"] += 1
                    return {
                        "success": False,
                        "processed_count": 0,
                        "failed_count": 1,
                        "skipped_count": 0,
                        "superseded_count": 0,
                        "warnings": [],
                        "output_files": [],
                        "request_url": "https://api.example.com/v1/chat/completions",
                        "aborted": True,
                        "aborted_reason": "need replan",
                        "replan_required": True,
                        "replan_reason": "timeout on chunk 0",
                        "plan": {"plan_id": "plan_a"},
                        "control": {"replan": {}},
                    }
                return {
                    "success": True,
                    "processed_count": 1,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "superseded_count": 0,
                    "warnings": [],
                    "output_files": [],
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "aborted": False,
                    "aborted_reason": "",
                    "replan_required": False,
                    "replan_reason": "",
                    "plan": {"plan_id": "plan_b"},
                    "control": {"replan": {}},
                }

            def fake_replan_remaining(*args, **kwargs):
                """Fake replan remaining."""
                ownership = kwargs.get("runtime_ownership") or {}
                seen_calls.append(("replan", ownership.get("owner_id", ""), bool(ownership.get("delegated", False))))
                return {
                    "success": True,
                    "replanned": True,
                    "warnings": [],
                }

            with mock.patch.object(utils, "process_chunks", side_effect=fake_process_chunks), mock.patch.object(
                utils,
                "replan_remaining",
                side_effect=fake_replan_remaining,
            ):
                result = utils.process_chunks_with_replans(str(work_dir), "structure_only", max_replans=1)

            owner_ids = {owner_id for _, owner_id, _ in seen_calls}
            self.assertTrue(result["success"])
            self.assertEqual(result["replan_count"], 1)
            self.assertEqual(len(owner_ids), 1)
            self.assertEqual(result["ownership"]["owner_id"], next(iter(owner_ids)))
            self.assertTrue(all(delegated for _, _, delegated in seen_calls))
            self.assertTrue(result["ownership"]["released"])

    def test_prepare_resume_marks_stale_running_chunk_interrupted(self):
        """Test prepare resume marks stale running chunk interrupted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "running"
            manifest["runtime"]["status"] = "running"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            result = utils.prepare_resume(str(work_dir), prompt_name="structure_only")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertTrue(result["resume"]["repaired"])
            self.assertEqual(manifest_after["chunks"][0]["status"], utils.INTERRUPTED_CHUNK_STATUS)
            self.assertEqual(manifest_after["runtime"]["status"], utils.RESUMABLE_RUNTIME_STATUS)
            self.assertEqual(manifest_after["runtime"]["interrupted_count"], 1)
            self.assertEqual(result["resume"]["interrupted_chunk_ids"], [0])

    def test_prepare_resume_promotes_running_chunk_with_output_to_done(self):
        """Test prepare resume promotes running chunk with output to done."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "running"
            manifest["runtime"]["status"] = "running"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            (work_dir / "processed_000.md").write_text("## 已完成" + chr(10) + chr(10) + "内容", encoding="utf-8")

            result = utils.prepare_resume(str(work_dir), prompt_name="structure_only")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(result["resume"]["repaired"])
            self.assertEqual(manifest_after["chunks"][0]["status"], "done")
            self.assertGreater(manifest_after["chunks"][0]["output_chars"], 0)
            self.assertEqual(result["resume"]["promoted_done_chunk_ids"], [0])

    def test_prepare_resume_demotes_done_chunk_missing_output_to_interrupted(self):
        """Test prepare resume demotes done chunk missing output to interrupted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "done"
            manifest["runtime"]["status"] = "completed"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            result = utils.prepare_resume(str(work_dir), prompt_name="structure_only")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(result["resume"]["repaired"])
            self.assertEqual(manifest_after["chunks"][0]["status"], utils.INTERRUPTED_CHUNK_STATUS)
            self.assertIn(0, result["resume"]["demoted_missing_output_chunk_ids"])
            self.assertEqual(manifest_after["runtime"]["status"], utils.RESUMABLE_RUNTIME_STATUS)

    def test_process_chunks_auto_repairs_resume_state_before_execution(self):
        """Test process chunks auto repairs resume state before execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "running"
            manifest["runtime"]["status"] = "running"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            (work_dir / "processed_000.md").write_text("## 已完成" + chr(10) + chr(10) + "内容", encoding="utf-8")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                return_value={
                    "text": "## 新结果" + chr(10) + chr(10) + "处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ) as mocked_call:
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertTrue(result["resume"]["repaired"])
            self.assertIn(0, result["resume"]["promoted_done_chunk_ids"])
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(mocked_call.call_count, 1)
            self.assertEqual(manifest_after["chunks"][0]["status"], "done")
            self.assertEqual(manifest_after["runtime"]["status"], "completed")

    def test_process_chunks_uses_prompt_specific_max_output_tokens(self):
        """Test process chunks uses prompt specific max output tokens."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 50, "summarize")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "llm_timeout_sec": 30,
                "llm_max_retries": 0,
                "llm_backoff_sec": 0.1,
                "llm_stream": "false",
                "llm_stop_after_consecutive_timeouts": 2,
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                return_value={
                    "text": "总结结果",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ) as mocked_call:
                result = utils.process_chunks(str(work_dir), "summarize")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertEqual(mocked_call.call_args.kwargs["max_tokens"], 384)
            self.assertEqual(manifest["planned_max_output_tokens"], 384)
            self.assertEqual(manifest["chunks"][0]["planned_max_output_tokens"], 384)

    def test_process_chunks_injects_continuity_context(self):
        """Test process chunks injects continuity context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n'
                'chunk_context_tail_sentences: 1\n'
                'chunk_context_summary_tokens: 20\n',
                encoding="utf-8",
            )
            utils.chunk_text(str(source), str(work_dir), 8, config_path=str(config_path))
            manifest_before = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))

            recorded_prompts = []
            fake_outputs = iter([
                "## Intro\n\n整理后的第一部分。",
                "## Follow-up\n\n整理后的第二部分。",
                "## Third\n\n整理后的第三部分。",
                "## Final\n\n整理后的第四部分。",
            ])

            def fake_call(**kwargs):
                """Fake call."""
                recorded_prompts.append(kwargs["messages"][0]["content"])
                return {
                    "text": next(fake_outputs),
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                }

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "chunk_context_tail_sentences": 1,
                "chunk_context_summary_tokens": 20,
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils, "_call_llm_api", side_effect=fake_call
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertNotIn("## Continuity Context", recorded_prompts[0])
            self.assertIn("## Continuity Context", recorded_prompts[1])
            self.assertIn("Do not repeat or rewrite this context in the output.", recorded_prompts[1])
            self.assertIn(manifest_before["chunks"][0]["tail_context_text"], recorded_prompts[1])
            self.assertEqual(manifest["chunks"][1]["continuity_prev_chunk_id"], 0)
            self.assertGreater(manifest["chunks"][1]["continuity_context_tokens"], 0)
            self.assertEqual(manifest["chunks"][0]["last_section_title"], "## Intro")

    def test_process_chunks_uses_manifest_continuity_policy_even_if_runtime_config_disables_it(self):
        """Test process chunks uses manifest continuity policy even if runtime config disables it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "chunk_config.yaml"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            config_path.write_text(
                f"""output_dir: "{tmpdir}"
chunk_mode: "chars"
chunk_context_tail_sentences: 1
chunk_context_summary_tokens: 20
""",
                encoding="utf-8",
            )
            utils.chunk_text(str(source), str(work_dir), 8, config_path=str(config_path))

            runtime_config = utils._default_config_values()
            runtime_config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "chunk_context_tail_sentences": 0,
                "chunk_context_summary_tokens": 0,
            })

            recorded_prompts = []
            fake_outputs = iter([
                "## Intro" + chr(10) + chr(10) + "整理后的第一部分。",
                "## Follow-up" + chr(10) + chr(10) + "整理后的第二部分。",
                "## Third" + chr(10) + chr(10) + "整理后的第三部分。",
                "## Final" + chr(10) + chr(10) + "整理后的第四部分。",
            ])

            with mock.patch.object(utils, "load_config", return_value=runtime_config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=lambda **kwargs: recorded_prompts.append(kwargs["messages"][0]["content"]) or {
                    "text": next(fake_outputs),
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            self.assertTrue(result["success"])
            self.assertIn("## Continuity Context", recorded_prompts[1])
            self.assertIn("Only transform the current chunk body below.", recorded_prompts[1])
            self.assertIn("Do not repeat or rewrite this context in the output.", recorded_prompts[1])

    def test_process_chunks_reuses_manifest_token_estimates_without_remote_probe(self):
        """Test process chunks reuses manifest token estimates without remote probe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 5000)

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            stderr = io.StringIO()
            with mock.patch.object(utils, "load_config", return_value=config), \
                mock.patch.object(utils, "_count_tokens_via_provider", side_effect=AssertionError("should not be called")), \
                mock.patch.object(utils, "_call_llm_api", return_value={
                    "text": "## Done\n\n处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                }), \
                mock.patch("sys.stderr", stderr):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertEqual(manifest["chunks"][0]["token_count_source"], "manifest_cached_input")
            self.assertEqual(manifest["token_count_source"], "manifest_cached_input")
            self.assertIn("est_source=manifest_cached_input", stderr.getvalue())

    def test_process_chunks_uses_processed_tail_for_chained_continuity(self):
        """Test process chunks uses processed tail for chained continuity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("原始甲。原始乙。原始丙。原始丁。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 8)
            manifest_before = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            raw_tail = manifest_before["chunks"][0]["tail_context_text"]

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "chunk_context_tail_sentences": 1,
                "chunk_context_summary_tokens": 20,
            })

            structured_outputs = iter([
                "## Intro\n\n结构化第一块。PROCESSED_TAIL_ONE。",
                "## Follow-up\n\n结构化第二块。PROCESSED_TAIL_TWO。",
                "## Third\n\n结构化第三块。PROCESSED_TAIL_THREE。",
                "## Final\n\n结构化第四块。PROCESSED_TAIL_FOUR。",
            ])
            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=lambda **kwargs: {
                    "text": next(structured_outputs),
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ):
                first_pass = utils.process_chunks(str(work_dir), "structure_only")

            self.assertTrue(first_pass["success"])

            translate_prompts = []
            translated_outputs = iter([
                "[EN1]\n\n" + ("这是第一块的中文翻译。\n" * 6),
                "[EN2]\n\n" + ("这是第二块的中文翻译。\n" * 6),
                "[EN3]\n\n" + ("这是第三块的中文翻译。\n" * 6),
                "[EN4]\n\n" + ("这是第四块的中文翻译。\n" * 6),
            ])
            stderr = io.StringIO()
            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=lambda **kwargs: translate_prompts.append(kwargs["messages"][0]["content"]) or {
                    "text": next(translated_outputs),
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ), mock.patch("sys.stderr", stderr):
                second_pass = utils.process_chunks(str(work_dir), "translate_only", input_key="processed_path", force=True)

            manifest_after = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(second_pass["success"])
            self.assertIn("Previous section title:", translate_prompts[1])
            self.assertIn("## Intro", translate_prompts[1])
            self.assertIn("PROCESSED_TAIL_ONE", translate_prompts[1])
            self.assertNotIn(raw_tail, translate_prompts[1])
            self.assertEqual(manifest_after["chunks"][0]["token_count_source"], "manifest_cached_output")
            self.assertEqual(manifest_after["token_count_source"], "manifest_cached_output")
            self.assertIn("est_source=manifest_cached_output", stderr.getvalue())

    def test_process_chunks_dry_run_preserves_char_manifest_units(self):
        """Test process chunks dry run preserves char manifest units."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text(("第一句。第二句。第三句。第四句。" * 200), encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4000)

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", side_effect=AssertionError("dry-run should not require load_config")), mock.patch.object(
                utils,
                "_load_optional_config",
                return_value=config,
            ):
                result = utils.process_chunks(str(work_dir), "structure_only", dry_run=True)

            self.assertTrue(result["success"])
            self.assertEqual(result["chunk_mode"], "chars")
            self.assertEqual(result["recommended_chunk_size"], 4000)
            self.assertFalse(any("1200" in warning for warning in result["warnings"]))

    def test_process_chunks_rejects_prompt_path_traversal(self):
        """Test process chunks rejects prompt path traversal."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 50, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", return_value=config), self.assertRaises(SystemExit):
                utils.process_chunks(str(work_dir), "../CODE_REVIEW", dry_run=True)

    def test_process_chunks_aborts_after_consecutive_timeouts(self):
        """Test process chunks aborts after consecutive timeouts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            timeout_error = utils.LLMRequestError(
                "timed out",
                error_type="timeout",
                retryable=True,
                request_url="https://api.example.com/v1/chat/completions",
            )
            timeout_error.attempts = 1

            with mock.patch.object(utils, "load_config", return_value={
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "llm_timeout_sec": 30,
                "llm_max_retries": 0,
                "llm_backoff_sec": 0.1,
                "llm_stream": "false",
                "llm_stop_after_consecutive_timeouts": 2,
            }), mock.patch.object(utils, "_call_llm_api", side_effect=timeout_error):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(result["success"])
            self.assertTrue(result["aborted"])
            self.assertTrue(result["replan_required"])
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(manifest["chunks"][0]["status"], "failed")
            self.assertEqual(manifest["chunks"][1]["status"], "pending")
            self.assertTrue(manifest["runtime"]["replan_required"])
            self.assertEqual(manifest["runtime"]["status"], "aborted")

    def test_process_chunks_autotune_shrinks_after_timeout(self):
        """Test process chunks autotune shrinks after timeout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。第五句。第六句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            timeout_error = utils.LLMRequestError(
                "timed out",
                error_type="timeout",
                retryable=True,
                request_url="https://api.example.com/v1/chat/completions",
            )
            timeout_error.attempts = 1
            call_counter = {"count": 0}

            def fake_call(**kwargs):
                """Fake call."""
                if call_counter["count"] == 0:
                    call_counter["count"] += 1
                    raise timeout_error
                call_counter["count"] += 1
                return {
                    "text": "## Done\n\n处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                }

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "enable_chunk_autotune": True,
                "llm_stop_after_consecutive_timeouts": 99,
            })

            stderr = io.StringIO()
            with mock.patch.object(utils, "load_config", return_value=config), \
                mock.patch.object(utils, "_call_llm_api", side_effect=fake_call), \
                mock.patch("sys.stderr", stderr):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            first_chunk = manifest["chunks"][0]

            self.assertFalse(result["success"])
            self.assertTrue(result["replan_required"])
            self.assertEqual(first_chunk["error_type"], "timeout")
            self.assertEqual(first_chunk["autotune_event"], "shrink")
            self.assertLess(first_chunk["autotune_next_target_tokens"], first_chunk["autotune_target_tokens"])
            self.assertEqual(manifest["autotune"]["current_target_tokens"], first_chunk["autotune_next_target_tokens"])
            self.assertIn("chunk_id=0", stderr.getvalue())
            self.assertIn("planned_max_output_tokens=", stderr.getvalue())
            self.assertIn("Autotune chunk_id=0 event=shrink", stderr.getvalue())

    def test_process_chunks_autotune_increases_after_success_window(self):
        """Test process chunks autotune increases after success window."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text(("第一句。第二句。第三句。第四句。" * 6), encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")
            manifest_before = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            base_target = manifest_before["autotune"]["current_target_tokens"]

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "enable_chunk_autotune": True,
                "autotune_success_window": 2,
                "autotune_increase_percent": 0.10,
                "autotune_p95_latency_threshold_ms": 1000,
            })

            stderr = io.StringIO()
            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                side_effect=lambda **kwargs: {
                    "text": "## Done\n\n处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ), mock.patch("sys.stderr", stderr):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue(result["success"])
            self.assertGreater(manifest["autotune"]["current_target_tokens"], base_target)
            self.assertIn("Autotune", stderr.getvalue())
            self.assertTrue(
                any(chunk["autotune_event"] == "increase" for chunk in manifest["chunks"] if chunk["status"] == "done")
            )
            self.assertTrue(
                all("input_tokens" in chunk and "error_type" in chunk for chunk in manifest["chunks"])
            )

    def test_process_chunks_records_attempt_logs_and_aborts_for_retry_timeout(self):
        """Test process chunks records attempt logs and aborts for retry timeout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "enable_chunk_autotune": True,
                "llm_max_retries": 1,
                "llm_backoff_sec": 0.01,
            })

            with mock.patch.object(utils, "load_config", return_value=config), \
                mock.patch("time.sleep"), \
                mock.patch.object(utils, "_execute_llm_request") as mocked_request:
                mocked_request.side_effect = [
                    utils.LLMRequestError(
                        "timed out",
                        error_type="timeout",
                        retryable=True,
                        request_url="https://api.example.com/v1/chat/completions",
                    ),
                    {
                        "text": "## Done\n\n处理完成。",
                        "latency_ms": 12,
                        "request_url": "https://api.example.com/v1/chat/completions",
                        "streaming_used": False,
                    },
                ]
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            first_chunk = manifest["chunks"][0]

            self.assertFalse(result["success"])
            self.assertTrue(result["replan_required"])
            self.assertEqual(first_chunk["status"], "done")
            self.assertEqual(len(first_chunk["attempt_logs"]), 2)
            self.assertEqual(first_chunk["attempt_logs"][0]["error_type"], "timeout")
            self.assertEqual(first_chunk["attempt_logs"][1]["result"], "success")
            self.assertTrue(manifest["runtime"]["replan_required"])
            self.assertEqual(result["control"]["replan"]["trigger"], "timeout_retry_instability")
            self.assertEqual(result["control"]["replan"]["action"], "auto_replan_remaining")
            self.assertEqual(manifest["runtime"]["control"]["last_replan_chunk_id"], first_chunk["id"])

    def test_process_chunks_auto_recovers_suspicious_short_output(self):
        """Test process chunks auto recovers suspicious short output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。" * 40, encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 1000, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "llm_chunk_recovery_attempts": 1,
                "llm_chunk_recovery_backoff_sec": 0.0,
            })

            with mock.patch.object(utils, "load_config", return_value=config), \
                mock.patch.object(utils, "_call_llm_api", side_effect=[
                    {
                        "text": "处理。",
                        "latency_ms": 10,
                        "request_url": "https://api.example.com/v1/chat/completions",
                        "streaming_used": False,
                        "attempts": 1,
                    },
                    {
                        "text": "## Done\n\n" + ("处理完成，保留原始信息。\n" * 12),
                        "latency_ms": 12,
                        "request_url": "https://api.example.com/v1/chat/completions",
                        "streaming_used": False,
                        "attempts": 1,
                    },
                ]) as mocked_call, mock.patch("time.sleep"):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            first_chunk = manifest["chunks"][0]
            output_path = work_dir / first_chunk["processed_path"]

            self.assertTrue(result["success"])
            self.assertEqual(mocked_call.call_count, 2)
            self.assertEqual(first_chunk["status"], "done")
            self.assertEqual(first_chunk["recovery_attempts"], 1)
            self.assertEqual(first_chunk["recovery_logs"][0]["action"], "retry")
            self.assertEqual(first_chunk["recovery_logs"][0]["reasons"], ["short_output"])
            self.assertEqual(first_chunk["attempts"], 2)
            self.assertEqual(result["control"]["repair"]["attempted_count"], 1)
            self.assertEqual(result["control"]["repair"]["exhausted_count"], 0)
            self.assertEqual(manifest["runtime"]["control"]["repair_attempted_count"], 1)
            self.assertEqual(first_chunk["control"]["verification_status"], "passed")
            self.assertIn("## Done", output_path.read_text(encoding="utf-8"))

    def test_process_chunks_marks_repair_exhausted_when_retries_disabled(self):
        """Test process chunks marks repair exhausted when retries disabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。" * 40, encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 1000, "structure_only")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "llm_chunk_recovery_attempts": 0,
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                return_value={
                    "text": "处理。",
                    "latency_ms": 10,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            first_chunk = manifest["chunks"][0]
            self.assertTrue(result["success"])
            self.assertEqual(result["control"]["repair"]["attempted_count"], 0)
            self.assertEqual(result["control"]["repair"]["exhausted_count"], 1)
            self.assertEqual(first_chunk["control"]["verification_status"], "warning")
            self.assertTrue(first_chunk["control"]["repair_exhausted"])
            self.assertEqual(first_chunk["control"]["retry_reasons"], ["short_output"])

    def test_replan_remaining_supersedes_pending_chunks_and_appends_new_plan(self):
        """Test replan remaining supersedes pending chunks and appends new plan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。第五句。第六句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "done"
            manifest["runtime"]["replan_required"] = True
            manifest["runtime"]["replan_reason"] = "timeout on chunk 1"
            manifest["autotune"]["current_target_tokens"] = 3
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            result = utils.replan_remaining(str(work_dir), prompt_name="structure_only")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            superseded = [chunk for chunk in manifest_after["chunks"] if chunk.get("status") == utils.SUPERSEDED_CHUNK_STATUS]
            active_pending = [chunk for chunk in manifest_after["chunks"] if chunk.get("status") == "pending"]
            first_pending_index = next(
                index for index, chunk in enumerate(manifest_after["chunks"])
                if chunk.get("status") == "pending"
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["replanned"])
            self.assertEqual(len(superseded), 5)
            self.assertGreater(len(active_pending), 0)
            self.assertEqual(manifest_after["runtime"]["active_plan_id"], result["plan_id"])
            self.assertEqual(manifest_after["runtime"]["current_chunk_index"], first_pending_index)
            self.assertFalse(manifest_after["runtime"]["replan_required"])
            self.assertEqual(result["chunk_size"], 3)

    def test_process_chunks_clears_stale_replan_flags_after_success(self):
        """Test process chunks clears stale replan flags after success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runtime"]["replan_required"] = True
            manifest["runtime"]["replan_reason"] = "stale flag"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                return_value={
                    "text": "## Done\n\n处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertTrue(result["success"])
            self.assertFalse(result["replan_required"])
            self.assertFalse(manifest_after["runtime"]["replan_required"])
            self.assertEqual(manifest_after["runtime"]["replan_reason"], "")
            self.assertEqual(manifest_after["runtime"]["status"], "completed")

    def test_process_chunks_marks_completed_with_errors_for_nonfatal_failures(self):
        """Test process chunks marks completed with errors for nonfatal failures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            missing_raw_path = work_dir / manifest["chunks"][1]["raw_path"]
            missing_raw_path.unlink()

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
            })

            with mock.patch.object(utils, "load_config", return_value=config), mock.patch.object(
                utils,
                "_call_llm_api",
                return_value={
                    "text": "## Done\n\n处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                },
            ):
                result = utils.process_chunks(str(work_dir), "structure_only")

            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertFalse(result["success"])
            self.assertFalse(result["aborted"])
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(manifest_after["runtime"]["status"], "completed_with_errors")
            self.assertFalse(manifest_after["runtime"]["replan_required"])

    def test_process_chunks_with_replans_auto_recovers_after_canary_abort(self):
        """Test process chunks with replans auto recovers after canary abort."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。第五句。第六句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            timeout_error = utils.LLMRequestError(
                "timed out",
                error_type="timeout",
                retryable=True,
                request_url="https://api.example.com/v1/chat/completions",
            )
            timeout_error.attempts = 1
            call_counter = {"count": 0}

            def fake_call(**kwargs):
                """Fake call."""
                if call_counter["count"] == 0:
                    call_counter["count"] += 1
                    raise timeout_error
                call_counter["count"] += 1
                return {
                    "text": "## Done\n\n处理完成。",
                    "latency_ms": 12,
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "streaming_used": False,
                    "attempts": 1,
                }

            config = utils._default_config_values()
            config.update({
                "llm_api_key": "key",
                "llm_base_url": "https://api.example.com",
                "llm_model": "demo",
                "llm_api_format": "openai",
                "enable_chunk_autotune": True,
                "autotune_canary_chunks": 1,
                "llm_stop_after_consecutive_timeouts": 99,
            })

            with mock.patch.object(utils, "load_config", return_value=config), \
                mock.patch.object(utils, "_call_llm_api", side_effect=fake_call):
                result = utils.process_chunks_with_replans(
                    str(work_dir),
                    "structure_only",
                    max_replans=1,
                )

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertEqual(result["replan_count"], 1)
            self.assertEqual(
                result["superseded_count"],
                sum(1 for chunk in manifest["chunks"] if chunk.get("status") == utils.SUPERSEDED_CHUNK_STATUS),
            )
            self.assertFalse(manifest["runtime"]["replan_required"])
            self.assertEqual(manifest["runtime"]["status"], "completed")
            self.assertTrue(any(chunk["status"] == utils.SUPERSEDED_CHUNK_STATUS for chunk in manifest["chunks"]))
            self.assertEqual(result["control"]["replan"]["auto_replan_count"], 1)
            self.assertEqual(result["control"]["replan"]["max_auto_replans"], 1)

    def test_process_chunks_with_replans_stops_when_replan_step_fails(self):
        """Test process chunks with replans stops when replan step fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            with mock.patch.object(utils, "process_chunks", side_effect=[
                {
                    "success": False,
                    "processed_count": 0,
                    "failed_count": 1,
                    "skipped_count": 0,
                    "superseded_count": 0,
                    "warnings": [],
                    "output_files": [],
                    "request_url": "https://api.example.com/v1/chat/completions",
                    "aborted": True,
                    "aborted_reason": "need replan",
                    "replan_required": True,
                    "replan_reason": "timeout on chunk 0",
                    "plan": {"plan_id": "plan_a"},
                },
            ]), mock.patch.object(utils, "replan_remaining", return_value={
                "success": False,
                "replanned": False,
                "error": "failed to generate replacement plan",
                "warnings": ["controller warning"],
            }):
                result = utils.process_chunks_with_replans(str(work_dir), "structure_only", max_replans=1)

        self.assertFalse(result["success"])
        self.assertTrue(result["aborted"])
        self.assertTrue(result["replan_required"])
        self.assertIn("failed to generate replacement plan", result["aborted_reason"])
        self.assertEqual(result["warning_count"], 1)

    def test_merge_content_keeps_chapter_headers_after_replan_remaining(self):
        """Test merge content keeps chapter headers after replan remaining."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            source.write_text("第一句。第二句。第三句。第四句。第五句。第六句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            (work_dir / "chapter_plan.json").write_text(
                json.dumps([
                    {"start_chunk": 1, "title_en": "Chapter One", "title_zh": "第一章"},
                ], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            manifest["chunks"][0]["status"] = "done"
            (work_dir / manifest["chunks"][0]["processed_path"]).write_text("done-0", encoding="utf-8")
            manifest["runtime"]["replan_required"] = True
            manifest["autotune"]["current_target_tokens"] = 3
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            result = utils.replan_remaining(str(work_dir), prompt_name="structure_only")
            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            for chunk in manifest_after["chunks"]:
                if chunk.get("status") == "pending":
                    (work_dir / chunk["processed_path"]).write_text(
                        f"content-{chunk['chunk_id']}",
                        encoding="utf-8",
                    )
                    chunk["status"] = "done"
            manifest_path.write_text(json.dumps(manifest_after, ensure_ascii=False, indent=2), encoding="utf-8")

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")
            chapter_plan = json.loads((work_dir / "chapter_plan.json").read_text(encoding="utf-8"))

            self.assertTrue(result["success"])
            self.assertTrue(merge_result["success"])
            self.assertEqual(merge_result["chapters_inserted"], 1)
            self.assertIn("Chapter One", merged_text)
            self.assertIn("第一章", merged_text)
            self.assertNotEqual(chapter_plan[0]["start_chunk"], 1)
