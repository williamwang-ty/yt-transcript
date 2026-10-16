"""Regression tests for the sliding-window long-text executor (long_text_mode=window)."""

import re

from tests._support import *  # noqa: F401,F403
from kernel.long_text import agent_cli
from kernel.long_text import window


SENTENCE_TAG = re.compile(r"^\[S(\d+)\] (.*)$", re.MULTILINE)


def _sentence(index: int) -> str:
    return f"这是第{index}句话，内容是关于测试的。"


def _window_lines(prompt: str) -> list[tuple[int, str]]:
    return [(int(number), text) for number, text in SENTENCE_TAG.findall(prompt)]


def _api_result(text: str, finish_reason: str = "stop") -> dict:
    return {
        "text": text,
        "latency_ms": 5,
        "request_url": "https://api.example.com/v1/chat/completions",
        "streaming_used": False,
        "finish_reason": finish_reason,
        "usage": {},
        "attempts": 1,
    }


def _marker_writer(per_paragraph: int = 2, paragraphs: int = 3, partial_tail: bool = True):
    """Fake model: cleans sentences in groups, marks each group, then gets cut off mid-paragraph."""
    prompts = []

    def respond(prompt: str) -> str:
        prompts.append(prompt)
        lines = _window_lines(prompt)
        out = []
        index = 0
        for _ in range(paragraphs):
            group = lines[index:index + per_paragraph]
            if not group:
                break
            out.append("".join(text for _, text in group))
            out.append(f"<!--S{group[-1][0]}-->")
            index += per_paragraph
        if partial_tail and index < len(lines):
            out.append(lines[index][1][:4])  # cut off: no marker
        return "\n\n".join(out)

    return respond, prompts


class WindowProtocolTests(unittest.TestCase):
    def test_parse_keeps_marked_paragraphs_and_drops_the_cut_off_tail(self):
        text = "## 标题\n\n第一段。\n<!--S3-->\n\n第二段。\n<!-- S5 -->\n\n半截没写完"
        parsed = window.parse_window_output(text, cursor=0, window_end=10)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual([(p["start"], p["end"]) for p in parsed["paragraphs"]], [(0, 2), (3, 4)])
        self.assertEqual(parsed["paragraphs"][0]["text"], "## 标题\n\n第一段。")
        self.assertEqual(parsed["next_cursor"], 5)
        self.assertEqual(parsed["dropped_tail_chars"], len("半截没写完"))

    def test_parse_rejects_markers_that_go_backwards_or_leave_the_window(self):
        backwards = window.parse_window_output("甲\n<!--S6-->\n乙\n<!--S5-->", cursor=3, window_end=10)
        self.assertEqual(backwards["error_type"], "non_monotonic")
        before_cursor = window.parse_window_output("甲\n<!--S3-->", cursor=3, window_end=10)
        self.assertEqual(before_cursor["error_type"], "non_monotonic")
        past_end = window.parse_window_output("甲\n<!--S11-->", cursor=3, window_end=10)
        self.assertEqual(past_end["error_type"], "out_of_window")
        missing = window.parse_window_output("没有任何标记的输出", cursor=0, window_end=10)
        self.assertEqual(missing["error_type"], "no_markers")

    def test_parse_folds_empty_marker_into_next_paragraph_and_strips_source_tags(self):
        text = "<!--S2-->\n[S3] 甲乙。\n<!--S4-->"
        parsed = window.parse_window_output(text, cursor=0, window_end=10)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(len(parsed["paragraphs"]), 1)
        self.assertEqual((parsed["paragraphs"][0]["start"], parsed["paragraphs"][0]["end"]), (0, 3))
        self.assertEqual(parsed["paragraphs"][0]["text"], "甲乙。")

    def test_read_window_respects_budget_but_always_takes_one_sentence(self):
        sentences = ["a" * 10, "b" * 10, "c" * 10, "d" * 10]
        self.assertEqual(window.build_read_window(sentences, 0, 25, len), 2)
        self.assertEqual(window.build_read_window(sentences, 1, 5, len), 2)
        self.assertEqual(window.build_read_window(sentences, 3, 1000, len), 4)

    def test_prompt_numbers_sentences_and_states_the_protocol(self):
        sentences = [_sentence(i) for i in range(1, 6)]
        prompt = window.build_window_prompt(
            "Clean it.\n\n## Input Text\n\n{RAW_TEXT}\n",
            sentences=sentences, cursor=2, window_end=4, tail_text="上一段结尾。", write_tokens=300,
        )
        self.assertIn("[S3] " + sentences[2], prompt)
        self.assertIn("[S4] " + sentences[3], prompt)
        self.assertNotIn("[S5]", prompt)
        self.assertNotIn("[S2]", prompt)
        self.assertIn("Start at [S3]", prompt)
        self.assertIn("<!--S<n>-->", prompt)
        self.assertIn("transcript continues after it", prompt)
        self.assertIn("上一段结尾。", prompt)

    def test_long_unpunctuated_sentences_are_split_into_numbered_units(self):
        text = "没有标点的自动字幕" * 80
        units = window.split_source_sentences(text, {}, max_sentence_tokens=60)
        self.assertGreater(len(units), 1)
        self.assertEqual("".join(units).replace(" ", ""), text)

    def test_coverage_report_lists_gaps_and_short_stretches(self):
        state = {
            "sentence_count": 10,
            "paragraphs": [
                {"id": 1, "start": 1, "end": 3, "source_chars": 300, "output_chars": 310},
                {"id": 2, "start": 4, "end": 4, "source_chars": 100, "output_chars": 10},
                {"id": 3, "start": 5, "end": 5, "source_chars": 100, "output_chars": 190},
                {"id": 4, "start": 6, "end": 8, "source_chars": 600, "output_chars": 0},
            ],
        }
        report = window.coverage_report(state)
        self.assertEqual(report["covered_sentences"], 8)
        self.assertEqual(report["uncovered_ranges"], ["S9-S10"])
        # Paragraph 2 is short but its neighbour carries the text; paragraph 4 is a real gap.
        flagged = [item["paragraph"] for item in report["low_output_ratio_paragraphs"]]
        self.assertIn(4, flagged)
        self.assertNotIn(2, flagged)

    def test_long_text_mode_defaults_to_chunks(self):
        self.assertEqual(window.resolve_long_text_mode({}), "chunks")
        self.assertEqual(window.resolve_long_text_mode({"long_text_mode": "Window"}), "window")
        self.assertEqual(window.resolve_long_text_mode({"long_text_mode": "weird"}), "chunks")
        self.assertTrue(window.window_supported("cleanup_zh", "raw_path"))
        self.assertTrue(window.window_supported("structure_only", "raw_path"))
        self.assertFalse(window.window_supported("summarize", "raw_path"))
        self.assertFalse(window.window_supported("cleanup_zh", "processed_path"))
        # translate_only needs a finished raw-stage window run in the work dir.
        self.assertFalse(window.window_supported("translate_only", "processed_path"))


class WindowExecutorTests(unittest.TestCase):
    SENTENCE_COUNT = 40

    def _make_work_dir(self, tmpdir: str, count: int = SENTENCE_COUNT) -> Path:
        source = Path(tmpdir) / "raw.txt"
        source.write_text("".join(_sentence(i) for i in range(1, count + 1)), encoding="utf-8")
        work_dir = Path(tmpdir) / "chunks"
        utils.chunk_text(str(source), str(work_dir), 0, "cleanup_zh")
        return work_dir

    def _config(self, **overrides) -> dict:
        config = utils._default_config_values()
        config.update({
            "llm_backend": "api",
            "llm_api_key": "key",
            "llm_base_url": "https://api.example.com",
            "llm_model": "demo",
            "llm_api_format": "openai",
            "long_text_mode": "window",
            "window_read_tokens": 200,
            "window_write_tokens": 100,
        })
        config.update(overrides)
        return config

    def _run(self, work_dir: Path, config: dict, llm, prompt_name: str = "cleanup_zh", **kwargs):
        with mock.patch.object(utils, "load_config", return_value=config), \
            mock.patch.object(utils, "_call_llm_api", side_effect=llm) as api_call, \
            mock.patch("sys.stderr", new_callable=io.StringIO):
            result = utils.process_chunks(str(work_dir), prompt_name, **kwargs)
        return result, api_call

    def test_window_run_covers_every_sentence_and_merges_without_markers(self):
        respond, prompts = _marker_writer()
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            result, api_call = self._run(
                work_dir, self._config(), lambda **kw: _api_result(respond(kw["messages"][0]["content"]))
            )
            output = Path(tmpdir) / "optimized.md"
            merge = utils.merge_content(str(work_dir), str(output))
            merged = output.read_text(encoding="utf-8")
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            report = utils.verify_quality(str(output), work_dir=str(work_dir))

        self.assertTrue(result["success"], result)
        self.assertEqual(result["long_text_mode"], "window")
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)
        self.assertEqual(result["coverage"]["uncovered_ranges"], [])
        self.assertGreater(api_call.call_count, 3)
        self.assertEqual(result["fallback_rounds"], 0)
        # Each round restarts right after the last complete marker.
        starts = [_window_lines(p)[0][0] for p in prompts]
        self.assertEqual(starts, sorted(set(starts)))
        self.assertEqual(manifest["long_text_executor"], "window")
        self.assertTrue(merge["success"], merge)
        self.assertEqual(merge["long_text_mode"], "window")
        self.assertNotIn("<!--", merged)
        self.assertNotIn("[S", merged)
        for i in range(1, self.SENTENCE_COUNT + 1):
            self.assertEqual(merged.count(_sentence(i)), 1, i)
        self.assertEqual(report["checks"]["window_coverage_ratio"], 1.0)

    def test_missing_markers_retry_once_then_fall_back_to_a_fixed_chunk(self):
        respond, _ = _marker_writer()
        calls = {"count": 0}

        def llm(**kwargs):
            calls["count"] += 1
            prompt = kwargs["messages"][0]["content"]
            if calls["count"] <= 2:
                return _api_result("我忘了写标记。")
            if "Sliding-Window Protocol" not in prompt:
                body = prompt.split("## Input Text", 1)[1]
                return _api_result("## 回退\n\n" + body.strip())
            return _api_result(respond(prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            result, _ = self._run(work_dir, self._config(), llm)
            state = window.load_window_state(str(work_dir))

        self.assertTrue(result["success"], result)
        self.assertEqual(result["invalid_rounds"], 2)
        self.assertEqual(result["fallback_rounds"], 1)
        self.assertEqual(state["paragraphs"][0]["mode"], "fallback")
        self.assertEqual(state["paragraphs"][0]["start"], 1)
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)

    def test_round_that_drops_most_of_its_source_is_rejected_then_falls_back(self):
        respond, _ = _marker_writer()
        calls = {"count": 0}

        def llm(**kwargs):
            calls["count"] += 1
            prompt = kwargs["messages"][0]["content"]
            if calls["count"] <= 2:
                last = _window_lines(prompt)[-1][0]
                return _api_result(f"总之讲了测试。\n<!--S{last}-->")
            if "Sliding-Window Protocol" not in prompt:
                return _api_result(prompt.split("## Input Text", 1)[1].strip())
            return _api_result(respond(prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            result, _ = self._run(work_dir, self._config(window_read_tokens=600), llm)
            state = window.load_window_state(str(work_dir))

        self.assertTrue(result["success"], result)
        self.assertEqual([r.get("error_type") for r in state["rounds"][:2]], ["low_output_ratio"] * 2)
        self.assertEqual(result["fallback_rounds"], 1)
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)
        self.assertIn("reread_share", result["efficiency"])

    def test_failed_run_resumes_at_the_cursor_without_duplicates(self):
        respond, prompts = _marker_writer()
        calls = {"count": 0}

        def flaky(**kwargs):
            calls["count"] += 1
            if calls["count"] == 3:
                raise utils.LLMRequestError("provider down", error_type="timeout", retryable=True)
            return _api_result(respond(kwargs["messages"][0]["content"]))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            first, _ = self._run(work_dir, self._config(), flaky)
            failed_state = window.load_window_state(str(work_dir))
            self.assertFalse(first["success"])
            self.assertEqual(first["status"], "failed")
            cursor = failed_state["cursor"]
            self.assertGreater(cursor, 0)

            prompts.clear()
            second, _ = self._run(work_dir, self._config(), lambda **kw: _api_result(respond(kw["messages"][0]["content"])))
            output = Path(tmpdir) / "optimized.md"
            utils.merge_content(str(work_dir), str(output))
            merged = output.read_text(encoding="utf-8")

        self.assertTrue(second["success"], second)
        self.assertTrue(second["resumed"])
        self.assertEqual(_window_lines(prompts[0])[0][0], cursor + 1)
        for i in range(1, self.SENTENCE_COUNT + 1):
            self.assertEqual(merged.count(_sentence(i)), 1, i)

    def test_merge_refuses_an_unfinished_window_run(self):
        def down(**kwargs):
            raise utils.LLMRequestError("provider down", error_type="timeout", retryable=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            self._run(work_dir, self._config(), down)
            merge = utils.merge_content(str(work_dir), str(Path(tmpdir) / "optimized.md"))
        self.assertFalse(merge["success"])
        self.assertIn("failed", merge["message"])

    def test_cli_backend_progresses_from_markers_alone(self):
        respond, _ = _marker_writer()
        cli_decision = {
            "mode": "auto", "backend": "cli", "agent": "claude-code", "agent_source": "detected",
            "command": ["claude", "-p"], "strip_env": [], "api_configured": True,
            "probe": {"attempted": True, "ok": True}, "fallback_reason": "",
        }

        def cli(command, prompt, **kwargs):
            return {"text": respond(prompt), "latency_ms": 5, "request_url": "cli:claude-code",
                    "streaming_used": False, "finish_reason": "", "usage": {}, "attempts": 1}

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            with mock.patch.object(utils, "load_config", return_value=self._config(llm_backend="auto")), \
                mock.patch.object(agent_cli, "resolve_llm_backend", return_value=cli_decision), \
                mock.patch.object(agent_cli, "call_cli_llm", side_effect=cli) as cli_call, \
                mock.patch.object(utils, "_call_llm_api") as api_call, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
                result = utils.process_chunks(str(work_dir), "cleanup_zh")

        self.assertTrue(result["success"], result)
        self.assertGreater(cli_call.call_count, 1)
        api_call.assert_not_called()
        self.assertEqual(result["request_url"], "cli:claude-code")
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)

    def test_auto_replan_entry_point_also_routes_to_the_window_executor(self):
        respond, _ = _marker_writer()
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            with mock.patch.object(utils, "load_config", return_value=self._config()), \
                mock.patch.object(utils, "_call_llm_api",
                                  side_effect=lambda **kw: _api_result(respond(kw["messages"][0]["content"]))), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
                result = utils.process_chunks_with_replans(str(work_dir), "cleanup_zh")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["long_text_mode"], "window")

    def test_chunks_mode_and_unsupported_prompts_keep_the_fixed_chunk_executor(self):
        chunk_output = lambda **kw: _api_result("## 标题\n\n" + kw["messages"][0]["content"][-200:])  # noqa: E731
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            result, _ = self._run(work_dir, self._config(long_text_mode="chunks"), chunk_output)
            self.assertNotEqual(result.get("long_text_mode"), "window")
            self.assertFalse((work_dir / "window").exists())

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            result, _ = self._run(work_dir, self._config(), chunk_output, prompt_name="summarize")
            self.assertNotEqual(result.get("long_text_mode"), "window")
            self.assertFalse((work_dir / "window").exists())

    def test_chapter_plan_headings_land_before_the_chunk_start_paragraph(self):
        respond, _ = _marker_writer()
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir, count=120)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertGreater(manifest["total_chunks"], 1)
            (work_dir / "chapter_plan.json").write_text(
                json.dumps([{"start_chunk": 0, "title": "开场"}, {"start_chunk": 1, "title": "第二章"}],
                           ensure_ascii=False),
                encoding="utf-8",
            )
            self._run(work_dir, self._config(), lambda **kw: _api_result(respond(kw["messages"][0]["content"])))
            state = window.load_window_state(str(work_dir))
            second_start = state["chunk_starts"]["1"]
            output = Path(tmpdir) / "optimized.md"
            merge = utils.merge_content(str(work_dir), str(output))
            merged = output.read_text(encoding="utf-8")

        self.assertEqual(merge["chapters_inserted"], 2)
        self.assertTrue(merged.startswith("## 开场"))
        heading_at = merged.index("## 第二章")
        # The first sentence of chunk 1 comes after its heading.
        self.assertGreater(merged.index(_sentence(second_start)), heading_at)

    def test_find_unfinished_points_window_runs_at_their_cursor(self):
        def down(**kwargs):
            raise utils.LLMRequestError("provider down", error_type="timeout", retryable=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            self._run(work_dir, self._config(), down)
            (Path(tmpdir) / "vid123_state.md").write_text(
                f"vid: vid123\nstep: 4\nwork_dir: {work_dir}\n", encoding="utf-8"
            )
            report = utils.find_unfinished_runs(tmpdir)

        self.assertEqual(len(report["unfinished"]), 1)
        entry = report["unfinished"][0]
        self.assertEqual(entry["chunks"]["long_text_mode"], "window")
        self.assertIn("resume at S1", entry["next_action"])


class WindowSizingTests(unittest.TestCase):
    PARAMS = {"read_tokens": 6000, "write_tokens": 3000, "max_output_tokens": 8192, "expected_ratio": 1.0}

    def test_read_window_follows_write_target_and_measured_ratio(self):
        sizing = window.initial_sizing(self.PARAMS)
        self.assertEqual(sizing["read_tokens"], 3750)  # 3000 / 1.0 * 1.25
        # Translation writes ~2 tokens per source token, so it needs half the read window.
        sizing = window.update_sizing(sizing, self.PARAMS, {"advanced_tokens": 1500, "output_tokens": 3000})
        self.assertAlmostEqual(sizing["ratio"], 2.0)
        self.assertEqual(sizing["read_tokens"], 1875)
        # The read window never exceeds the configured cap.
        sizing = window.update_sizing(sizing, self.PARAMS, {"advanced_tokens": 3000, "output_tokens": 300})
        self.assertLessEqual(sizing["read_tokens"], 6000)

    def test_exhausted_window_grows_and_truncation_or_slowness_shrinks_the_write_target(self):
        sizing = window.initial_sizing(self.PARAMS)
        grown = window.update_sizing(sizing, self.PARAMS, {
            "advanced_tokens": 3750, "output_tokens": 3750, "read_tokens": 3750, "exhausted": True})
        self.assertGreater(grown["read_tokens"], sizing["read_tokens"])
        shrunk = window.update_sizing(sizing, self.PARAMS, {
            "advanced_tokens": 1000, "output_tokens": 1000, "truncated": True})
        self.assertEqual(shrunk["write_tokens"], 2250)
        slow = window.update_sizing(sizing, self.PARAMS, {"advanced_tokens": 1000, "output_tokens": 1000, "slow": True})
        self.assertEqual(slow["write_tokens"], 2250)
        # Clean rounds grow it back, never past the configured value.
        for _ in range(10):
            shrunk = window.update_sizing(shrunk, self.PARAMS, {"advanced_tokens": 1000, "output_tokens": 1000})
        self.assertEqual(shrunk["write_tokens"], 3000)

    def test_last_stretch_is_sent_whole_when_one_round_can_finish_it(self):
        sizing = window.initial_sizing(self.PARAMS)
        finish = window.plan_round(sizing, self.PARAMS, remaining_tokens=4000)
        self.assertTrue(finish["finish"])
        self.assertEqual(finish["read_tokens"], 4000)
        self.assertEqual(finish["write_tokens"], 4400)
        normal = window.plan_round(sizing, self.PARAMS, remaining_tokens=20000)
        self.assertFalse(normal["finish"])
        self.assertEqual(normal["read_tokens"], 3750)

    def test_round_ratio_check_flags_skipped_or_invented_output(self):
        self.assertEqual(window.round_ratio_problem(1000, 1000, 1.0), "")
        self.assertEqual(window.round_ratio_problem(1000, 300, 1.0), "low_output_ratio")
        self.assertEqual(window.round_ratio_problem(1000, 2600, 1.0), "high_output_ratio")
        self.assertEqual(window.round_ratio_problem(100, 5, 1.0), "")  # too small to judge


class WindowBilingualTests(unittest.TestCase):
    """structure_only then translate_only, both in window mode."""

    @staticmethod
    def _structure_writer(prompt: str) -> str:
        lines = _window_lines(prompt)
        out = []
        for index in range(0, min(len(lines), 6), 3):
            group = lines[index:index + 3]
            out.append(" ".join(text for _, text in group))
            out.append(f"<!--S{group[-1][0]}-->")
        return "\n\n".join(out)

    @staticmethod
    def _translate_writer(prompt: str) -> str:
        paragraphs = re.findall(r"^\[S(\d+)\] (.*)$", prompt, re.MULTILINE)
        out = []
        for number, text in paragraphs[:2]:
            out.append(text)
            out.append("【译】" + text)
            out.append(f"<!--S{number}-->")
        return "\n\n".join(out)

    def test_bilingual_stages_both_run_as_windows_and_merge_the_translation(self):
        def llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            writer = self._translate_writer if "Translation-Only" in prompt else self._structure_writer
            return _api_result(writer(prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            source.write_text(" ".join(f"This is sentence number {i}." for i in range(1, 31)), encoding="utf-8")
            work_dir = Path(tmpdir) / "chunks"
            utils.chunk_text(str(source), str(work_dir), 0, "structure_only")
            config = WindowExecutorTests._config(self)
            structure, _ = WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="structure_only")
            translate, _ = WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="translate_only",
                                                    input_key="processed_path")
            output = Path(tmpdir) / "optimized.md"
            merge = utils.merge_content(str(work_dir), str(output))
            merged = output.read_text(encoding="utf-8")

        self.assertTrue(structure["success"], structure)
        self.assertTrue(translate["success"], translate)
        self.assertEqual(translate["input_key"], "processed_path")
        self.assertEqual(translate["coverage"]["coverage_ratio"], 1.0)
        self.assertTrue(merge["success"], merge)
        for i in range(1, 31):
            self.assertEqual(merged.count(f"This is sentence number {i}."), 2, i)  # English + translated copy
        self.assertIn("【译】", merged)
        self.assertNotIn("<!--", merged)

    def test_translation_stays_on_chunks_when_the_raw_stage_used_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir)
            self.assertFalse(window.window_supported("translate_only", "processed_path", str(work_dir)))
