"""Regression tests for the sliding-window long-text executor."""

import re

from tests._support import *  # noqa: F401,F403
from kernel.llm import agent_cli
from kernel.text import chunking
from kernel.text import merge as kernel_merge
from kernel.text import outline
from kernel.text import window
from tests import fake_llm


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
    """Fake model: copies each sentence's tag into paragraphs of a few sentences, then gets cut off.

    Without a cut-off tail it ends with the <!--END--> line.
    """
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
            out.append("".join(f"[S{number}] {text}" for number, text in group))
            index += per_paragraph
        if partial_tail and index < len(lines):
            out.append(f"[S{lines[index][0]}] " + lines[index][1][:4])  # cut off: no end line
        else:
            out.append("<!--END-->")
        return "\n\n".join(out)

    return respond, prompts


class WindowProtocolTests(unittest.TestCase):
    def test_parse_groups_paragraphs_by_tags_and_drops_the_cut_off_tail(self):
        text = "## 标题\n\n[S1] 第一段[S2] 接着[S3] 说完。\n\n[S4] 第二段[S5] 也完了。\n\n[S6] 半截没写完"
        parsed = window.parse_window_output(text, cursor=0, window_end=10)
        self.assertTrue(parsed["ok"], parsed)
        self.assertFalse(parsed["complete"])
        self.assertEqual([(p["start"], p["end"]) for p in parsed["paragraphs"]], [(0, 2), (3, 4)])
        self.assertEqual(parsed["paragraphs"][0]["text"], "## 标题\n\n第一段接着说完。")
        self.assertEqual(parsed["next_cursor"], 5)
        self.assertEqual(parsed["dropped_tail_chars"], len("[S6] 半截没写完"))

    def test_parse_keeps_the_last_paragraph_when_the_end_line_is_there(self):
        text = "[S4] 甲。\n\n[S5] 乙[S6] 丙。\n\n<!--END-->"
        parsed = window.parse_window_output(text, cursor=3, window_end=10)
        self.assertTrue(parsed["ok"], parsed)
        self.assertTrue(parsed["complete"])
        self.assertEqual([(p["start"], p["end"]) for p in parsed["paragraphs"]], [(3, 3), (4, 5)])
        self.assertEqual(parsed["next_cursor"], 6)

    def test_parse_attaches_untagged_blocks_to_the_paragraph_before(self):
        # A sentence that spills into the next paragraph stays with its tag, so
        # the cursor never lands in the middle of it.
        text = "[S1] 甲乙\n\n丙丁。\n\n[S2] 戊。\n\n<!--END-->"
        parsed = window.parse_window_output(text, cursor=0, window_end=10)
        self.assertEqual([(p["start"], p["end"]) for p in parsed["paragraphs"]], [(0, 0), (1, 1)])
        self.assertEqual(parsed["paragraphs"][0]["text"], "甲乙\n\n丙丁。")

    def test_parse_rejects_tags_that_go_backwards_or_leave_the_window(self):
        backwards = window.parse_window_output("[S6] 甲\n\n[S5] 乙\n<!--END-->", cursor=3, window_end=10)
        self.assertEqual(backwards["error_type"], "non_monotonic")
        before_cursor = window.parse_window_output("[S3] 甲\n<!--END-->", cursor=3, window_end=10)
        self.assertEqual(before_cursor["error_type"], "non_monotonic")
        past_end = window.parse_window_output("[S11] 甲\n<!--END-->", cursor=3, window_end=10)
        self.assertEqual(past_end["error_type"], "out_of_window")
        missing = window.parse_window_output("没有任何编号的输出", cursor=0, window_end=10)
        self.assertEqual(missing["error_type"], "no_markers")

    def test_parse_accepts_a_tag_repeated_across_a_paragraph_break(self):
        text = "[S1] 甲[S2] 乙\n\n[S2] 丙。[S3] 丁。\n\n<!--END-->"
        parsed = window.parse_window_output(text, cursor=0, window_end=10)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual([(p["start"], p["end"]) for p in parsed["paragraphs"]], [(0, 0), (1, 2)])

    def test_parse_skipped_tags_count_toward_the_paragraph_before(self):
        text = "[S1] 甲[S2] 乙。\n\n[S5] 丙。\n\n<!--END-->"
        parsed = window.parse_window_output(text, cursor=0, window_end=10)
        self.assertEqual([(p["start"], p["end"]) for p in parsed["paragraphs"]], [(0, 3), (4, 4)])

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
        self.assertIn("Keep every [S<n>] tag", prompt)
        self.assertIn("<!--END-->", prompt)
        self.assertIn("transcript continues after it", prompt)
        self.assertIn("上一段结尾。", prompt)

    def test_forced_split_does_not_leave_a_tiny_last_unit(self):
        # Seen on a real video: the last forced piece was three characters, the
        # model folded them into the paragraph before without the tag, and the
        # executor ran two more rounds for it, duplicating the words.
        text = "没有标点的自动字幕" * 60 + "的主题"
        units = window.split_source_sentences(text, {}, max_sentence_tokens=60)
        self.assertEqual("".join(units), text)
        self.assertTrue(units[-1].endswith("的主题"))
        self.assertGreater(len(units[-1]), len("的主题"))

    def test_structure_prompt_keeps_speaker_turns_in_separate_paragraphs(self):
        sentences = [_sentence(i) for i in range(1, 4)]
        kwargs = dict(sentences=sentences, cursor=0, window_end=3, write_tokens=300)
        structure = window.build_window_prompt("{RAW_TEXT}", prompt_name="structure_only", **kwargs)
        cleanup = window.build_window_prompt("{RAW_TEXT}", prompt_name="cleanup_zh", **kwargs)
        self.assertIn("whenever the speaker changes", structure)
        self.assertIn("Drop the `-` or `>>` marker", structure)
        self.assertNotIn("whenever the speaker changes", cleanup)

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

class WindowExecutorTests(unittest.TestCase):
    SENTENCE_COUNT = 40

    def _make_work_dir(self, tmpdir: str, count: int = SENTENCE_COUNT) -> Path:
        return chunk_plain_text("".join(_sentence(i) for i in range(1, count + 1)), Path(tmpdir) / "chunks")

    def _config(self, **overrides) -> dict:
        config = kernel_config.default_config_values()
        config.update({
            "llm_backend": "api",
            "llm_api_key": "key",
            "llm_base_url": "https://api.example.com",
            "llm_model": "demo",
            "llm_api_format": "openai",
            "window_read_tokens": 200,
            "window_write_tokens": 100,
            # The outline pass has its own tests (WindowOutlineTests).
            "window_outline": False,
        })
        config.update(overrides)
        return config

    def _run(self, work_dir: Path, config: dict, llm, prompt_name: str = "cleanup_zh", **kwargs):
        with mock.patch.object(kernel_config, "load_config", return_value=config), \
            mock.patch.object(kernel_llm_api, "call_llm_api", side_effect=llm) as api_call, \
            mock.patch("sys.stderr", new_callable=io.StringIO):
            result = window.process_window(str(work_dir), prompt_name, **kwargs)
        return result, api_call

    def test_window_run_covers_every_sentence_and_merges_without_markers(self):
        respond, prompts = _marker_writer()
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            result, api_call = self._run(
                work_dir, self._config(), lambda **kw: _api_result(respond(kw["messages"][0]["content"]))
            )
            output = Path(tmpdir) / "optimized.md"
            merge = kernel_merge.merge_content(str(work_dir), str(output))
            merged = output.read_text(encoding="utf-8")
            report = kernel_verify.verify_quality(str(output), work_dir=str(work_dir))

        self.assertTrue(result["success"], result)
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)
        self.assertEqual(result["coverage"]["uncovered_ranges"], [])
        self.assertGreater(api_call.call_count, 3)
        self.assertEqual(result["fallback_rounds"], 0)
        # Each round restarts right after the last complete marker.
        starts = [_window_lines(p)[0][0] for p in prompts]
        self.assertEqual(starts, sorted(set(starts)))
        self.assertTrue(merge["success"], merge)
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
                return _api_result(f"[S{last}] 总之讲了测试。\n\n<!--END-->")
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
                raise kernel_llm_api.LLMRequestError("provider down", error_type="timeout", retryable=True)
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
            kernel_merge.merge_content(str(work_dir), str(output))
            merged = output.read_text(encoding="utf-8")

        self.assertTrue(second["success"], second)
        self.assertTrue(second["resumed"])
        self.assertEqual(_window_lines(prompts[0])[0][0], cursor + 1)
        for i in range(1, self.SENTENCE_COUNT + 1):
            self.assertEqual(merged.count(_sentence(i)), 1, i)

    def test_merge_refuses_an_unfinished_window_run(self):
        def down(**kwargs):
            raise kernel_llm_api.LLMRequestError("provider down", error_type="timeout", retryable=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            self._run(work_dir, self._config(), down)
            merge = kernel_merge.merge_content(str(work_dir), str(Path(tmpdir) / "optimized.md"))
        self.assertFalse(merge["success"])
        self.assertIn("failed", merge["message"])

    def test_cleanup_zh_builds_a_transcript_glossary_for_rounds_without_an_outline(self):
        prompts = []

        def llm(**kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            return _api_result(tag_echo(prompts[-1]))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = chunk_plain_text("OpenAI API 发布流程。我们用 OpenAI API 做测试。", Path(tmpdir) / "chunks")
            normalized = Path(tmpdir) / "chunks_normalized_document.json"
            document = json.loads(normalized.read_text(encoding="utf-8"))
            document["source"] = {"title": "OpenAI API Deep Dive", "channel": "DeepgramLabs"}
            normalized.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            result, _ = self._run(work_dir, self._config(), llm)
            glossary_written = (work_dir / "glossary.json").exists()

        self.assertTrue(result["success"], result)
        self.assertIn("Terminology Guardrails", prompts[0])
        self.assertIn("OpenAI", prompts[0].split("Terminology Guardrails", 1)[1])
        self.assertTrue(glossary_written)

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
            with mock.patch.object(kernel_config, "load_config", return_value=self._config(llm_backend="auto")), \
                mock.patch.object(agent_cli, "resolve_llm_backend", return_value=cli_decision), \
                mock.patch.object(agent_cli, "call_cli_llm", side_effect=cli) as cli_call, \
                mock.patch.object(kernel_llm_api, "call_llm_api") as api_call, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
                result = window.process_window(str(work_dir), "cleanup_zh")

        self.assertTrue(result["success"], result)
        self.assertGreater(cli_call.call_count, 1)
        api_call.assert_not_called()
        self.assertEqual(result["request_url"], "cli:claude-code")
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)

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
            out.append(" ".join(f"[S{number}] {text}" for number, text in group))
        out.append("<!--END-->")
        return "\n\n".join(out)

    @staticmethod
    def _translate_writer(prompt: str) -> str:
        paragraphs = re.findall(r"^\[S(\d+)\] (.*)$", prompt, re.MULTILINE)
        out = []
        for number, text in paragraphs[:2]:
            out.append(f"[S{number}] {text}")
            out.append("【译】" + text)
        out.append("<!--END-->")
        return "\n\n".join(out)

    def test_bilingual_stages_both_run_as_windows_and_merge_the_translation(self):
        def llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            writer = self._translate_writer if "Translation-Only" in prompt else self._structure_writer
            return _api_result(writer(prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = chunk_plain_text(" ".join(f"This is sentence number {i}." for i in range(1, 31)),
                                        Path(tmpdir) / "chunks", "structure_only")
            config = WindowExecutorTests._config(self)
            structure, _ = WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="structure_only")
            translate, _ = WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="translate_only",
                                                    input_key="processed_path")
            output = Path(tmpdir) / "optimized.md"
            merge = kernel_merge.merge_content(str(work_dir), str(output))
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

    def test_short_speaker_turns_and_their_translations_stay_separate_paragraphs(self):
        # Chunk merging joins a short block that looks unfinished to the next one;
        # window paragraphs have no seams, so that must not glue a pair together.
        def llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            if "Translation-Only" not in prompt:
                lines = _window_lines(prompt)[:4]
                return _api_result("\n\n".join([f"[S{number}] {text}" for number, text in lines] + ["<!--END-->"]))
            paragraphs = re.findall(r"^\[S(\d+)\] (.*)$", prompt, re.MULTILINE)
            return _api_result("\n\n".join([f"[S{number}] {text}\n\n译文{number}" for number, text in paragraphs[:4]]
                                             + ["<!--END-->"]))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = chunk_plain_text(" ".join(f"Yes, that is turn {i}." for i in range(1, 9)),
                                        Path(tmpdir) / "chunks", "structure_only")
            config = WindowExecutorTests._config(self)
            WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="structure_only")
            translate, _ = WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="translate_only",
                                                    input_key="processed_path")
            output = Path(tmpdir) / "optimized.md"
            kernel_merge.merge_content(str(work_dir), str(output))
            merged = output.read_text(encoding="utf-8")

        self.assertTrue(translate["success"], translate)
        blocks = merged.split("\n\n")
        self.assertEqual(16, len(blocks), merged)
        self.assertEqual(["Yes, that is turn 1.", "译文1"], blocks[:2])


# Unpunctuated caption phrases; with 0.1s gaps inside a thought and 0.8s after
# every third cue (the "real" pauses).
CUE_PHRASES = ["我们今天来聊一聊", "这个滑动窗口的设计", "其实它的核心思想很简单",
               "就是每一轮只往前推进一点", "然后根据模型的输出", "来决定下一轮从哪里开始"]


def _cue_segments(repeat: int = 12) -> list[dict]:
    segments, time = [], 0.0
    for index, text in enumerate(CUE_PHRASES * repeat):
        segments.append({"id": index, "text": text, "start_time": time, "end_time": time + 2.0})
        time += 2.0 + (0.8 if index % 3 == 2 else 0.1)
    return segments


def _cue_ends(segments: list[dict]) -> set[int]:
    ends, offset = set(), 0
    for segment in segments:
        offset += len(segment["text"])
        ends.add(offset)
    return ends


class WindowCueSplitTests(unittest.TestCase):
    def _work_dir(self, tmpdir: str, segments: list[dict], text: str | None = None) -> Path:
        document = {
            "source": {"type": "youtube"},
            "workflow": {"mode": "chinese"},
            "content": {"text": text if text is not None else "".join(s["text"] for s in segments),
                        "preferred_chunk_source": "text"},
            "segments": segments,
        }
        path = Path(tmpdir) / "normalized_document.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        work_dir = Path(tmpdir) / "chunks"
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            chunking.chunk_document(str(path), str(work_dir), "cleanup_zh", kernel_config.default_config_values())
        return work_dir

    def test_long_unpunctuated_units_end_at_cue_pauses(self):
        segments = _cue_segments()
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._work_dir(tmpdir, segments)
            units, report = window.build_source_sentences(str(work_dir), {}, 60)

        text = "".join(s["text"] for s in segments)
        self.assertEqual("".join(units), text)
        self.assertEqual(report["cue_alignment"], "full")
        self.assertEqual(report["hard_cuts"], 0)
        self.assertGreater(report["cue_cuts"], 5)
        offset, ends = 0, _cue_ends(segments)
        for unit in units:
            offset += len(unit)
            self.assertIn(offset, ends, unit)
            # The units cover whole thoughts: they end where the long pauses are.
            self.assertTrue(unit.endswith("来决定下一轮从哪里开始") or unit.endswith("其实它的核心思想很简单"), unit)
            self.assertLessEqual(kernel_measure.estimate_tokens(unit), 60)

    def test_without_matching_cues_units_fall_back_to_fixed_cuts(self):
        segments = _cue_segments()
        with tempfile.TemporaryDirectory() as tmpdir:
            # The text no longer matches the cues, so they cannot be used.
            work_dir = self._work_dir(tmpdir, segments, text="另外一段完全不同的文字没有标点" * 40)
            units, report = window.build_source_sentences(str(work_dir), {}, 60)
        self.assertEqual(report["cue_alignment"], "failed")
        self.assertEqual(report["cue_cuts"], 0)
        self.assertGreater(report["hard_cuts"], 0)
        self.assertEqual("".join(units), "另外一段完全不同的文字没有标点" * 40)

    def test_work_dir_without_normalized_document_keeps_fixed_cuts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = chunk_plain_text("没有标点的自动字幕" * 80, Path(tmpdir) / "chunks")
            units, report = window.build_source_sentences(str(work_dir), {}, 60)
        self.assertEqual(report["cue_alignment"], "none")
        self.assertEqual(units, window.split_source_sentences("没有标点的自动字幕" * 80, {}, 60))

    def test_a_cue_longer_than_the_cap_is_cut_inside_only(self):
        long_cue = "这一条字幕特别长中间完全没有停顿" * 12
        segments = [
            {"id": 0, "text": "开头一句", "start_time": 0.0, "end_time": 1.0},
            {"id": 1, "text": long_cue, "start_time": 1.5, "end_time": 30.0},
            {"id": 2, "text": "后面接着说别的事情", "start_time": 31.0, "end_time": 33.0},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._work_dir(tmpdir, segments)
            units, report = window.build_source_sentences(str(work_dir), {}, 60)
        text = "".join(s["text"] for s in segments)
        self.assertEqual("".join(units), text)
        self.assertGreater(report["hard_cuts"], 0)
        # The hard cuts stay inside the long cue; nothing else is cut mid-cue.
        self.assertEqual(units[0], "开头一句")
        self.assertTrue(units[-1].endswith("后面接着说别的事情"))
        self.assertTrue(all(len(unit) <= 75 for unit in units), units)

    def test_cue_cuts_ignore_whitespace_and_punctuated_sentences(self):
        sentences = "第一句话很短。" + " ".join(CUE_PHRASES * 4)
        pauses = {}
        offset = len("第一句话很短。")
        for index, phrase in enumerate(CUE_PHRASES * 4):
            offset += len(phrase)
            pauses[offset] = 0.8 if index % 3 == 2 else 0.1
        report = window._new_split_report()
        units = window.split_source_sentences(sentences, {}, 60, pauses, report)
        self.assertEqual(units[0], "第一句话很短。")
        self.assertEqual("".join(units).replace(" ", ""), sentences.replace(" ", ""))
        self.assertTrue(all(unit.endswith("来决定下一轮从哪里开始") or unit.endswith("其实它的核心思想很简单")
                            for unit in units[1:]), units)
        self.assertEqual(report["hard_cuts"], 0)

    def test_chunk_boundary_inside_a_cue_is_cut_again_at_cue_ends(self):
        # One unpunctuated run is longer than a chunk, so the chunker cuts it
        # at a fixed width, inside a cue.
        segments = _cue_segments(30)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._work_dir(tmpdir, segments)
            chunks = window.load_source_chunks(str(work_dir))
            units, report = window.build_source_sentences(str(work_dir), {}, 60)

        self.assertGreater(len(chunks), 1)
        ends = _cue_ends(segments)
        first_chunk_end = len(re.sub(r"\s+", "", chunks[0][1]))
        self.assertNotIn(first_chunk_end, ends)  # the chunker did cut mid-cue
        offset = 0
        for unit in units:
            offset += len(re.sub(r"\s+", "", unit))
            self.assertIn(offset, ends, unit)
        self.assertEqual(report["hard_cuts"], 0)

    def test_chunk_boundary_at_a_cue_end_stays_a_unit_boundary(self):
        phrases = [phrase + ("。" if index % 6 == 5 else "") for index, phrase in enumerate(CUE_PHRASES * 40)]
        segments = [{"id": index, "text": text, "start_time": index * 2.0, "end_time": index * 2.0 + 1.9}
                    for index, text in enumerate(phrases)]
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._work_dir(tmpdir, segments)
            chunks = window.load_source_chunks(str(work_dir))
            units, _ = window.build_source_sentences(str(work_dir), {}, 60)
        self.assertGreater(len(chunks), 1)
        unit_starts, position = set(), 0
        for unit in units:
            unit_starts.add(position)
            position += len(re.sub(r"\s+", "", unit))
        position = 0
        for _, text in chunks:
            self.assertIn(position, unit_starts)
            position += len(re.sub(r"\s+", "", text))

    def test_dot_inside_a_domain_is_not_a_sentence_end(self):
        # Seen on a real video: "Alibaba.com" split into "Alibaba." and "com...".
        units = window.split_source_sentences("他们做了Alibaba.com这个网站。后来又做了别的", {}, 120)
        self.assertEqual(units, ["他们做了Alibaba.com这个网站。", "后来又做了别的"])


def _outline_json(sections: list[tuple[int, str]], terms: list[dict] | None = None, **extra) -> str:
    payload = {"sections": [{"start": start, "title": title, **extra} for start, title in sections],
               "terms": terms or []}
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def _headed_marker_writer():
    """Like _marker_writer, but the model also writes its own heading, as the task prompt asks."""
    respond, prompts = _marker_writer()

    def write(prompt: str) -> str:
        return "## 模型自己的标题\n\n" + respond(prompt)

    return write, prompts


class WindowOutlineTests(unittest.TestCase):
    """Issue #12: global outline + term list before the window rounds."""

    SENTENCE_COUNT = 40
    SECTIONS = [(1, "开场"), (15, "第二部分"), (28, "第三部分")]

    def _config(self, **overrides) -> dict:
        config = WindowExecutorTests._config(self, window_outline=True)
        config.update(overrides)
        return config

    def _llm(self, outline_text: str, writer=None):
        writer = writer or _headed_marker_writer()[0]
        calls = {"outline": [], "rounds": []}

        def llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            if prompt.startswith("# Transcript Outline"):
                calls["outline"].append(prompt)
                return _api_result(outline_text)
            calls["rounds"].append(prompt)
            return _api_result(writer(prompt))

        return llm, calls

    def _merged(self, work_dir: Path, tmpdir: str) -> tuple[dict, str]:
        output = Path(tmpdir) / "optimized.md"
        merge = kernel_merge.merge_content(str(work_dir), str(output))
        return merge, output.read_text(encoding="utf-8")

    # --- parsing and validation -------------------------------------------

    def test_parse_accepts_fenced_json_and_unit_numbers_written_as_tags(self):
        text = "Here it is:\n```json\n" + json.dumps({
            "sections": [{"start": "S1", "title": "## 开场"}, {"start": "[S7]", "title": "细节"}],
            "terms": [{"term": "OpenAI", "variants": ["Open AI", "openai", "OpenAI"], "kind": "organization"},
                      {"term": "openai"}, "GPU"],
        }, ensure_ascii=False) + "\n```"
        parsed = outline.parse_outline(text, 1, 10)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["sections"], [{"start": 1, "title": "开场"}, {"start": 7, "title": "细节"}])
        # Duplicates dropped; a variant equal to the term (ignoring case) is not a variant.
        self.assertEqual([t["term"] for t in parsed["terms"]], ["OpenAI", "GPU"])
        self.assertEqual(parsed["terms"][0]["variants"], ["Open AI"])

    def test_translations_and_short_or_full_forms_are_not_variants(self):
        text = json.dumps({"sections": [{"start": 1, "title": "开场"}], "terms": [
            {"term": "Mastercard", "variants": ["万事达", "MMastercard"]},
            {"term": "John Tukey", "variants": ["Tukey", "John Tookey"]},
            {"term": "英伟达", "variants": ["因为达", "Nvidia"]},
        ]}, ensure_ascii=False)
        parsed = outline.parse_outline(text, 1, 10)
        self.assertEqual([t["variants"] for t in parsed["terms"]], [["MMastercard"], ["John Tookey"], ["因为达"]])

    def test_parse_drops_sections_out_of_range_or_out_of_order(self):
        text = json.dumps({"sections": [
            {"start": 11, "title": "甲"}, {"start": 20, "title": "乙"}, {"start": 15, "title": "倒退"},
            {"start": 25, "title": "乙"}, {"start": 31, "title": "越界"}, {"start": 26, "title": "丙"},
            {"start": 27, "title": ""}, {"start": 28, "title": "丁"},
        ]}, ensure_ascii=False)
        parsed = outline.parse_outline(text, 11, 30)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual([s["start"] for s in parsed["sections"]], [11, 20, 26, 28])
        self.assertEqual(parsed["dropped_sections"], 4)

    def test_parse_rejects_missing_json_or_mostly_invalid_sections(self):
        self.assertEqual(outline.parse_outline("没有 JSON", 1, 10)["error_type"], "no_json")
        self.assertEqual(outline.parse_outline('{"terms": []}', 1, 10)["error_type"], "no_sections")
        mostly_bad = json.dumps({"sections": [{"start": 50, "title": "a"}, {"start": 60, "title": "b"},
                                              {"start": 2, "title": "c"}]})
        self.assertEqual(outline.parse_outline(mostly_bad, 1, 10)["error_type"], "invalid_sections")
        none_valid = json.dumps({"sections": [{"start": 0, "title": "a"}]})
        self.assertEqual(outline.parse_outline(none_valid, 1, 10)["error_type"], "no_valid_sections")

    def test_parts_are_near_equal_and_merge_without_repeating_a_continued_section(self):
        self.assertEqual(outline.split_parts([10] * 10, 1000), [(0, 10)])
        parts = outline.split_parts([10] * 10, 40)
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], 10)
        self.assertEqual([b[0] for b in parts[1:]], [a[1] for a in parts[:-1]])
        merged = outline.merge_parts([
            {"sections": [{"start": 1, "title": "甲"}, {"start": 4, "title": "乙"}],
             "terms": [{"term": "GPU", "variants": [], "kind": "term"}]},
            {"sections": [{"start": 6, "title": "乙"}, {"start": 8, "title": "丙"}],
             "terms": [{"term": "gpu", "variants": ["G P U"], "kind": "term"}]},
        ])
        self.assertEqual([s["title"] for s in merged["sections"]], ["甲", "乙", "丙"])
        self.assertEqual(merged["terms"], [{"term": "GPU", "variants": ["G P U"], "kind": "term"}])

    def test_heading_goes_before_the_paragraph_whose_middle_is_past_the_section_start(self):
        sections = [{"start": 1, "title": "a"}, {"start": 12, "title": "b"}, {"start": 20, "title": "c"}]
        self.assertEqual(outline.sections_before(sections, 0, 1, 4), 1)
        # Section b starts late in S9-S12: it waits for the next paragraph.
        self.assertEqual(outline.sections_before(sections, 1, 9, 12), 1)
        self.assertEqual(outline.sections_before(sections, 1, 13, 15), 2)
        # Early in a paragraph: the heading goes before it.
        self.assertEqual(outline.sections_before(sections, 2, 19, 25), 3)

    def test_model_heading_for_a_nearby_section_is_kept_where_the_model_put_it(self):
        sections = [{"start": 1, "title": "开场"}, {"start": 10, "title": "第二部分", "title_zh": "第二部分"},
                    {"start": 20, "title": "第三部分"}]
        # The topic really starts a little after S10's tag: the model's heading stays there.
        text = "前一个话题的结尾。\n\n## 第二部分\n\n新话题开始了。\n\n## 自己编的标题\n\n继续。"
        body, placed, next_section, written = outline.place_headings(sections, 1, text, 9, 12)
        self.assertEqual(body, "前一个话题的结尾。\n\n## 第二部分\n\n新话题开始了。\n\n继续。")
        self.assertEqual((placed, next_section, written), ([10], 2, 1))
        # A matching title far from its section start is not trusted; the program places it.
        body, placed, next_section, written = outline.place_headings(sections, 1, "## 第三部分\n\n甲。", 9, 11)
        self.assertEqual((body, placed, next_section, written), ("## 第二部分\n\n甲。", [10], 2, 0))
        # Skipped sections before a model-placed one are headed with it, in order.
        body, placed, next_section, written = outline.place_headings(sections, 1, "甲。\n\n## 第三部分\n\n乙。",
                                                                     18, 21)
        self.assertEqual(body, "甲。\n\n## 第二部分\n\n## 第三部分\n\n乙。")
        self.assertEqual((placed, next_section, written), ([10, 20], 3, 1))

    def test_round_context_lists_nearby_sections_and_terms_found_in_the_window(self):
        sections = [{"start": 1, "title": "甲"}, {"start": 10, "title": "乙", "anchor": "可不可以具体举一个例子"},
                    {"start": 14, "title": "戊"}, {"start": 30, "title": "丙"}, {"start": 50, "title": "丁"}]
        text = outline.round_outline_text(sections, cursor=4, window_end=20, next_section=1)
        self.assertIn("- 甲 (the section [S5] is in; already headed)", text)
        # The heading goes before the sentence holding the section's first words, not at the unit tag.
        self.assertIn("- 乙 (starts in this window; it begins in [S10] at the words “可不可以具体举一个例子”: start a "
                      "new paragraph at the beginning of the sentence that contains these words, and put the line "
                      "`## 乙` before it)", text)
        self.assertIn("- 戊 (starts in this window; it begins in or right after [S14], where this topic starts", text)
        self.assertIn("- 丙 (next section", text)
        self.assertNotIn("丁", text)
        self.assertNotIn("[S10] 乙", text)
        # A section that began before the window and has no heading yet opens the output.
        text = outline.round_outline_text(sections, cursor=11, window_end=20, next_section=1)
        self.assertIn("- 乙 (starts in this window; it began before [S12], so start the output with the line `## 乙`)",
                      text)
        text = outline.round_outline_text(sections, cursor=9, window_end=20, next_section=1)
        self.assertIn("- 乙 (starts in this window; it begins in [S10] at the words", text)
        terms = [{"term": "英伟达", "variants": ["因为达"], "kind": "organization"},
                 {"term": "CUDA", "variants": [], "kind": "product"},
                 {"term": "黄仁勋", "variants": [], "kind": "person"}]
        glossary = outline.round_terms_text(terms, "因为达的cuda很强")
        self.assertIn("英伟达 (the source may write it as 因为达; write 英伟达)", glossary)
        self.assertIn("CUDA (keep this spelling)", glossary)
        self.assertNotIn("黄仁勋", glossary)
        self.assertEqual(outline.round_terms_text(terms, "没有相关的词"), "")

    def test_chapters_replace_reworded_section_titles_and_positions(self):
        sections = [{"start": 1, "title": "开场"}, {"start": 11, "title": "军火暴利与杠杆", "anchor": "模型挑的开头"},
                    {"start": 20, "title": "被细分的小节"}, {"start": 40, "title": "Gauss", "anchor": "So Gauss"}]
        hints = [{"start": 1, "title": "<Untitled Chapter 1>", "anchor": "大家好"},
                 {"start": 12, "title": "军火暴利4451%？TransDigm", "anchor": "接下来说说军火"},
                 {"start": 40, "title": "Gauss's Discovery"}]
        restored = outline.apply_chapters(sections, hints, "cleanup_zh")
        # The placeholder and the English title (not the output language) keep the model's titles.
        self.assertEqual([s["title"] for s in restored], ["开场", "军火暴利4451%？TransDigm", "被细分的小节", "Gauss"])
        # The chapter's unit and words win over where the model started the section.
        self.assertEqual([(s["start"], s.get("anchor")) for s in restored],
                         [(1, "大家好"), (12, "接下来说说军火"), (20, None), (40, "So Gauss")])
        restored = outline.apply_chapters(sections, hints, "structure_only")
        self.assertEqual(restored[3]["title"], "Gauss's Discovery")
        # A chapter moved onto the unit of the model's next section replaces that section.
        crowded = [{"start": 10, "title": "甲"}, {"start": 12, "title": "乙"}, {"start": 20, "title": "丙"}]
        restored = outline.apply_chapters(crowded, [{"start": 12, "title": "第一章"}, {"start": 11, "title": "第二章"}],
                                          "cleanup_zh")
        self.assertEqual([(s["start"], s["title"]) for s in restored], [(11, "第二章"), (12, "第一章"), (20, "丙")])
        restored = outline.apply_chapters(crowded, [{"start": 11, "title": "第一章"}], "cleanup_zh")
        self.assertEqual([(s["start"], s["title"]) for s in restored], [(11, "第一章"), (12, "乙"), (20, "丙")])

    def test_first_words_become_anchors_copied_from_the_source(self):
        units = ["去管理你整个企业员工", "在整个日常经营里边的表现这个是现在我们看到的问题可不可以具体举一个例子比如说一个跨境商家",
                 "他一开始用 Accio Work 是想要解决什么样的问题", "So, what's the plan? Let's start with the data."]
        # Found inside S2 although the model wrote punctuation the source does not have.
        self.assertEqual(outline.locate_words(units, "可不可以，具体举一个例子", 2), (1, units[1].index("可不可以")))
        # The model gave the next unit; the words are looked for nearby.
        self.assertEqual(outline.locate_words(units, "可不可以具体举一个例子", 3), (1, units[1].index("可不可以")))
        # Words running over the end of a unit, an English sentence start, and a fixed-up word.
        self.assertEqual(outline.locate_words(units, "跨境商家他一开始用", 2), (1, units[1].index("跨境商家")))
        self.assertEqual(outline.locate_words(units, "let's start with", 4), (3, units[3].index("Let's")))
        self.assertEqual(outline.locate_words(units, "可不可以具体讲一个例子", 2), (1, units[1].index("可不可以")))
        self.assertIsNone(outline.locate_words(units, "完全不在原文里的话", 2))
        self.assertIsNone(outline.locate_words(units, "问题", 2))
        # As long as asked (an English word weighs two Chinese characters), running into the next
        # unit only when the rest of this one is too short.
        self.assertEqual(outline.anchor_text(units, 1, units[1].index("可不可以"), 11), "可不可以具体举一个例子")
        self.assertEqual(outline.anchor_text(units, 1, units[1].index("跨境商家")), "跨境商家他一开始用 Accio Work")
        self.assertEqual(outline.anchor_text(units, 3, units[3].index("Let's")), "Let's start with the data")

        sections = outline.anchor_sections([
            {"start": 1, "title": "甲", "first_words": "去管理你整个企业员工"},
            {"start": 3, "title": "乙", "first_words": "可不可以具体举一个例子"},
            {"start": 3, "title": "丙", "first_words": "没有这句话"},
            {"start": 4, "title": "丁", "first_words": "可不可以具体举一个例子"},
        ], units)
        # 乙 moves to the unit holding its words; 丙 keeps its unit without an anchor; 丁 would not
        # come after 丙 any more and is dropped.
        self.assertEqual(sections, [{"start": 1, "title": "甲", "anchor": "去管理你整个企业员工"},
                                    {"start": 2, "title": "乙", "anchor": "可不可以具体举一个例子"},
                                    {"start": 3, "title": "丙"}])

    def test_youtube_chapter_times_map_to_the_unit_holding_that_cue(self):
        cue_starts = [(0, 0.0), (10, 5.0), (20, 10.0), (30, 15.0)]
        unit_offsets = [0, 8, 20, 25]
        hints = outline.hints_from_times(
            [{"title": "Intro", "start_time": 0}, {"title": "Main", "start_time": 10.2},
             {"title": "Again", "start_time": 11.0}, {"title": "Late", "start_time": 13.0},
             {"title": "", "start_time": 16}],
            cue_starts, unit_offsets)
        # 10.2s → cue at offset 20 → unit 3; 11s maps there too and is dropped;
        # 13s → nearest cue at 15s, offset 30 → unit 4 (starting at offset 25).
        self.assertEqual(hints, [{"start": 1, "title": "Intro"}, {"start": 3, "title": "Main"},
                                 {"start": 4, "title": "Late"}])
        self.assertEqual(outline.hints_from_times([{"title": "x", "start_time": 0}], cue_starts, [None] * 4), [])

    def test_chapter_hints_are_anchored_at_the_chapter_cue_inside_its_unit(self):
        # Two cues per unit; the second chapter starts at the second cue of unit 2.
        cues = ["大家好欢迎来到节目", "今天聊聊章节", "上一个话题的结尾", "可不可以举个例子", "比如说一个商家", "后来怎么样了"]
        units = [cues[0] + cues[1], cues[2] + " " + cues[3], cues[4] + cues[5]]
        cue_starts, offset = [], 0
        for index, cue in enumerate(cues):
            cue_starts.append((offset, index * 5.0))
            offset += len(cue)
        unit_offsets = [0, len(cues[0] + cues[1]), len(cues[0] + cues[1] + cues[2] + cues[3])]
        hints = outline.hints_from_times(
            [{"title": "开场", "start_time": 0}, {"title": "举例", "start_time": 15.2},
             {"title": "片尾", "start_time": 40.0}],
            cue_starts, unit_offsets, units)
        # Each chapter keeps its unit and begins with its first cue's words, from the middle of
        # unit 2 for the second; the chapter after the last cue has no text and is left out.
        self.assertEqual(hints, [{"start": 1, "title": "开场", "anchor": "大家好欢迎来到节目"},
                                 {"start": 2, "title": "举例", "anchor": "可不可以举个例子"}])

    # --- executor -----------------------------------------------------------

    def test_outline_headings_replace_the_rounds_own_headings(self):
        terms = [{"term": "测验", "variants": ["测试"], "kind": "term"}]
        llm, calls = self._llm(_outline_json(self.SECTIONS, terms))
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir)
            result, _ = WindowExecutorTests._run(self, work_dir, self._config(), llm)
            state = window.load_window_state(str(work_dir))
            merge, merged = self._merged(work_dir, tmpdir)

        self.assertTrue(result["success"], result)
        self.assertEqual(len(calls["outline"]), 1)
        self.assertEqual(result["outline"]["status"], "ok")
        self.assertEqual(result["outline"]["section_count"], 3)
        self.assertEqual(result["outline"]["unplaced_sections"], 0)
        self.assertIn("[S1] " + _sentence(1), calls["outline"][0])
        self.assertIn(f"[S{self.SENTENCE_COUNT}] ", calls["outline"][0])
        first_round = calls["rounds"][0]
        self.assertIn("do not invent headers", first_round)
        self.assertIn("- 开场 (starts in this window; it begins in or right after [S1]", first_round)
        self.assertIn('"first_words"', calls["outline"][0])
        self.assertIn("write 测验", first_round)
        self.assertNotIn("Terminology Guardrails", first_round)
        # Headings come only from the outline, each once, in order, at its section start.
        self.assertNotIn("模型自己的标题", merged)
        self.assertEqual(re.findall(r"^## (.+)$", merged, re.MULTILINE), ["开场", "第二部分", "第三部分"])
        self.assertTrue(merged.startswith("## 开场"))
        for start, title in self.SECTIONS[1:]:
            heading_at = merged.index(f"## {title}")
            self.assertLess(merged.index(_sentence(start - 2)), heading_at)
            self.assertGreater(merged.index(_sentence(start + 2)), heading_at)
        for i in range(1, self.SENTENCE_COUNT + 1):
            self.assertEqual(merged.count(_sentence(i)), 1, i)
        self.assertEqual(merge["sections"], 3)
        self.assertEqual(sum(len(p.get("sections", [])) for p in state["paragraphs"]), 3)

    def test_headings_go_before_the_words_a_section_begins_with_not_at_a_unit_tag(self):
        # Unpunctuated text, like Chinese subtitles: units are fixed-size cuts that start mid-sentence.
        text = "".join(f"第{i}个话题从这里开始然后讲了很多细节" for i in range(1, 61))
        words = "第20个话题从这里开始"
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = chunk_plain_text(text, Path(tmpdir) / "chunks")
            units, _ = window.build_source_sentences(str(work_dir), {})
            holding = next(index for index, unit in enumerate(units) if words in unit)
            self.assertGreater(units[holding].index(words), 0)
            # The model names the unit after the one holding the words; they are found nearby.
            llm, calls = self._llm(json.dumps({"sections": [
                {"start": 1, "first_words": "第1个话题", "title": "开场"},
                {"start": holding + 2, "first_words": words, "title": "第二部分"}]}, ensure_ascii=False),
                writer=fake_llm.window_round)
            result, _ = WindowExecutorTests._run(self, work_dir, self._config(), llm)
            state = window.load_window_state(str(work_dir))
            _, merged = self._merged(work_dir, tmpdir)

        self.assertTrue(result["success"], result)
        self.assertEqual(state["outline"]["sections"][1], {"start": holding + 1, "title": "第二部分", "anchor": words})
        self.assertTrue(any(f"it begins in [S{holding + 1}] at the words “{words}”" in prompt
                            for prompt in calls["rounds"]))
        # The heading sits right before those words, inside what was one unit.
        self.assertIn(f"\n\n## 第二部分\n\n{words}然后", merged)
        self.assertEqual(merged.replace("\n", "").replace("## 开场", "").replace("## 第二部分", ""), text)
        self.assertEqual(result["outline"]["anchored_sections"], 2)
        self.assertEqual(result["outline"]["model_placed_sections"], 2)

    def test_failed_outline_falls_back_to_headings_from_the_rounds(self):
        llm, calls = self._llm("抱歉，我无法生成大纲。")
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir)
            result, _ = WindowExecutorTests._run(self, work_dir, self._config(), llm)
            _, merged = self._merged(work_dir, tmpdir)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["outline"]["status"], "failed")
        self.assertEqual(result["outline"]["error"], "part 1: response has no JSON object")
        self.assertNotIn("Outline (section headings", calls["rounds"][0])
        self.assertIn("Put `##` headers on their own line.", calls["rounds"][0])
        self.assertIn("## 模型自己的标题", merged)

    def test_outline_request_error_falls_back_and_disabled_outline_sends_no_request(self):
        def llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            if prompt.startswith("# Transcript Outline"):
                raise kernel_llm_api.LLMRequestError("provider down", error_type="timeout", retryable=True)
            return _api_result(_marker_writer()[0](prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir)
            result, _ = WindowExecutorTests._run(self, work_dir, self._config(), llm)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["outline"]["status"], "failed")
        self.assertIn("provider down", result["outline"]["error"])

        llm, calls = self._llm(_outline_json(self.SECTIONS))
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir)
            result, _ = WindowExecutorTests._run(self, work_dir, self._config(window_outline=False), llm)
        self.assertEqual(calls["outline"], [])
        self.assertEqual(result["outline"]["status"], "disabled")

    def test_no_outline_when_one_round_covers_the_whole_source(self):
        llm, calls = self._llm(_outline_json(self.SECTIONS))
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir)
            config = self._config(window_read_tokens=6000, window_write_tokens=3000)
            result, _ = WindowExecutorTests._run(self, work_dir, config, llm)
        self.assertTrue(result["success"], result)
        self.assertEqual(calls["outline"], [])
        self.assertEqual(result["outline"]["status"], "skipped")
        self.assertIn("Put `##` headers on their own line.", calls["rounds"][0])

    def test_resumed_run_reuses_the_outline_without_repeating_headings(self):
        writer, _ = _headed_marker_writer()
        failures = {"round": 0}

        def flaky(prompt: str) -> str:
            failures["round"] += 1
            if failures["round"] == 3:
                raise kernel_llm_api.LLMRequestError("provider down", error_type="timeout", retryable=True)
            return writer(prompt)

        first_llm, first_calls = self._llm(_outline_json(self.SECTIONS), writer=flaky)
        second_llm, second_calls = self._llm(_outline_json([(1, "不该出现")]), writer=writer)
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir)
            first, _ = WindowExecutorTests._run(self, work_dir, self._config(), first_llm)
            second, _ = WindowExecutorTests._run(self, work_dir, self._config(), second_llm)
            _, merged = self._merged(work_dir, tmpdir)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(len(first_calls["outline"]), 1)
        self.assertTrue(second["success"], second)
        self.assertTrue(second["resumed"])
        self.assertEqual(second_calls["outline"], [])
        self.assertEqual(re.findall(r"^## (.+)$", merged, re.MULTILINE), ["开场", "第二部分", "第三部分"])

    def test_long_source_is_outlined_in_parts_and_merged(self):
        count = 120

        def outline_by_part(prompt: str) -> str:
            numbers = [n for n, _ in _window_lines(prompt.split("## Transcript", 1)[1])]
            first, last = numbers[0], numbers[-1]
            middle = (first + last) // 2
            # Each part repeats the section the previous one ended with, then adds one.
            title = "开场" if first == 1 else "延续"
            return _outline_json([(first, title), (middle, f"话题{middle}")])

        prompts = []

        def llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            if prompt.startswith("# Transcript Outline"):
                prompts.append(prompt)
                return _api_result(outline_by_part(prompt))
            return _api_result(_marker_writer()[0](prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = WindowExecutorTests._make_work_dir(self, tmpdir, count=count)
            result, _ = WindowExecutorTests._run(self, work_dir, self._config(window_outline_read_tokens=1000), llm)
            state = window.load_window_state(str(work_dir))

        self.assertTrue(result["success"], result)
        self.assertGreater(len(prompts), 1)
        self.assertIn(f"This is part 1 of {len(prompts)}", prompts[0])
        self.assertIn("from the previous part continues", prompts[1])
        starts = [s["start"] for s in state["outline"]["sections"]]
        self.assertEqual(starts, sorted(set(starts)))
        self.assertEqual(state["outline"]["parts"], len(prompts))
        self.assertEqual(result["outline"]["status"], "ok")

    def test_video_chapters_become_outline_hints_and_are_not_inserted_again_at_merge(self):
        llm, calls = self._llm(_outline_json([(1, "开场"), (60, "第二章细化")]))
        segments = [{"id": i - 1, "text": _sentence(i), "start_time": (i - 1) * 3.0, "end_time": (i - 1) * 3.0 + 2.5}
                    for i in range(1, 121)]
        with tempfile.TemporaryDirectory() as tmpdir:
            chapters = Path(tmpdir) / "chapters.json"
            chapters.write_text(json.dumps({"chapters": [{"title": "Opening", "start_time": 0.0},
                                                         {"title": "Chapter Two", "start_time": 180.0}]}),
                                encoding="utf-8")
            path = Path(tmpdir) / "normalized_document.json"
            path.write_text(json.dumps({"content": {"text": "".join(s["text"] for s in segments),
                                                    "preferred_chunk_source": "text"},
                                        "segments": segments}, ensure_ascii=False), encoding="utf-8")
            work_dir = Path(tmpdir) / "chunks"
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                chunking.chunk_document(str(path), str(work_dir), "cleanup_zh", kernel_config.default_config_values(),
                                        chapters_path=str(chapters))
            result, _ = WindowExecutorTests._run(self, work_dir, self._config(), llm)
            _, merged = self._merged(work_dir, tmpdir)

        self.assertTrue(result["success"], result)
        self.assertIn("## Video Chapters", calls["outline"][0])
        # 180s is the start of cue 60, which holds sentence 61.
        self.assertIn("- [S61] Chapter Two", calls["outline"][0])
        self.assertEqual(result["outline"]["chapter_hints"], 2)
        self.assertEqual(len(re.findall(r"^## (.+)$", merged, re.MULTILINE)), 2, merged[:300])
        self.assertNotIn("Opening", merged)

    def test_chapters_file_from_chunk_document_maps_to_units_by_cue_time(self):
        segments = [{"id": i, "text": f"第{i}条字幕说完了。", "start_time": i * 3.0, "end_time": i * 3.0 + 2.5}
                    for i in range(60)]
        with tempfile.TemporaryDirectory() as tmpdir:
            chapters = Path(tmpdir) / "chapters.json"
            chapters.write_text(json.dumps({"has_chapters": True, "chapters": [
                {"title": "Intro", "start_time": 0.0}, {"title": "Middle", "start_time": 90.0}]}),
                encoding="utf-8")
            document = {"source": {"type": "youtube"}, "workflow": {"mode": "chinese"},
                        "content": {"text": "".join(s["text"] for s in segments), "preferred_chunk_source": "text"},
                        "segments": segments}
            path = Path(tmpdir) / "normalized_document.json"
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            work_dir = Path(tmpdir) / "chunks"
            chunking.chunk_document(str(path), str(work_dir), "cleanup_zh", kernel_config.default_config_values(),
                                    chapters_path=str(chapters))
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            offsets: list = []
            units, _ = window.build_source_sentences(str(work_dir), {}, unit_offsets=offsets)
            hints = window.load_chapter_hints(str(work_dir), offsets)

        self.assertEqual(manifest["chapters_path"], str(chapters.absolute()))
        self.assertEqual(len(offsets), len(units))
        # 90s is the start of cue 30, which is unit 31.
        self.assertEqual(hints, [{"start": 1, "title": "Intro"}, {"start": 31, "title": "Middle"}])
        self.assertEqual(units[30], "第30条字幕说完了。")

    def test_bilingual_outline_titles_and_terms_carry_into_the_translation(self):
        def llm(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            if prompt.startswith("# Transcript Outline"):
                self.assertIn('"title_zh"', prompt)
                return _api_result(json.dumps({
                    "sections": [{"start": 1, "title": "Opening", "title_zh": "开场"},
                                 {"start": 16, "title": "Details", "title_zh": "细节"}],
                    "terms": [{"term": "sentence", "variants": [], "kind": "term", "zh": "句子"}],
                }))
            if "Translation-Only" in prompt:
                self.assertIn("- sentence => 句子", prompt)
                body = prompt.split("## Input Text", 1)[1]
                out = []
                for number, block in re.findall(r"^\[S(\d+)\] (.*?)(?=\n\n|\Z)", body, re.MULTILINE | re.DOTALL)[:2]:
                    out.append(f"[S{number}] {block.strip()}")
                    if not block.startswith("#"):
                        out.append("【译】" + block.strip())
                out.append("<!--END-->")
                return _api_result("\n\n".join(out))
            return _api_result(WindowBilingualTests._structure_writer(prompt))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = chunk_plain_text(" ".join(f"This is sentence number {i}." for i in range(1, 31)),
                                        Path(tmpdir) / "chunks", "structure_only")
            config = self._config()
            structure, _ = WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="structure_only")
            translate, _ = WindowExecutorTests._run(self, work_dir, config, llm, prompt_name="translate_only",
                                                    input_key="processed_path")
            _, merged = self._merged(work_dir, tmpdir)

        self.assertTrue(structure["success"], structure)
        self.assertTrue(translate["success"], translate)
        self.assertEqual(re.findall(r"^## (.+)$", merged, re.MULTILINE), ["Opening", "开场", "Details", "细节"])
        for i in range(1, 31):
            self.assertEqual(merged.count(f"This is sentence number {i}."), 2, i)
