import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yt_transcript_utils as utils


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


class RegressionTests(unittest.TestCase):
    def test_build_api_url_accepts_root_and_v1(self):
        self.assertEqual(
            utils._build_api_url("https://api.openai.com", "openai"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            utils._build_api_url("https://api.openai.com/", "openai"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            utils._build_api_url("https://api.openai.com/v1", "openai"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            utils._build_api_url("https://api.openai.com/v1/", "openai"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            utils._build_api_url("https://api.anthropic.com", "anthropic"),
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(
            utils._build_api_url("https://api.anthropic.com/", "anthropic"),
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(
            utils._build_api_url("https://api.anthropic.com/v1", "anthropic"),
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(
            utils._build_api_url("https://api.anthropic.com/v1/", "anthropic"),
            "https://api.anthropic.com/v1/messages",
        )

    def test_load_config_preserves_hash_inside_quotes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                'output_dir: "~/Downloads #1"\n'
                'deepgram_api_key: "dg#key"\n'
                'llm_base_url: "https://api.openai.com/v1"\n',
                encoding="utf-8",
            )
            config = utils.load_config(str(config_path))
            self.assertTrue(config["output_dir"].endswith("Downloads #1"))
            self.assertEqual(config["deepgram_api_key"], "dg#key")
            self.assertEqual(config["llm_base_url"], "https://api.openai.com/v1")

    def test_chunk_text_splits_chinese_without_spaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")

            result = utils.chunk_text(str(source), str(out_dir), chunk_size=5)

            self.assertGreater(result["total_chunks"], 1)
            chunks = [path.read_text(encoding="utf-8") for path in sorted(out_dir.glob("chunk_*.txt"))]
            self.assertIn("第一句。", chunks[0])
            self.assertTrue(any("第四句。" in chunk for chunk in chunks))

    def test_split_sentences_handles_chinese_quotes(self):
        sentences = utils._split_sentences('他说："没问题。"然后离开了。下一句。')
        self.assertEqual(sentences, ['他说："没问题。"', '然后离开了。', '下一句。'])

    def test_split_sentences_handles_mixed_language_text(self):
        sentences = utils._split_sentences("First sentence. 第二句。Third sentence! 最后一句？")
        self.assertEqual(
            sentences,
            ["First sentence.", "第二句。", "Third sentence!", "最后一句？"],
        )

    def test_split_sentences_preserves_decimals(self):
        sentences = utils._split_sentences("Version 2.0 is live. Then we ship.")
        self.assertEqual(sentences, ["Version 2.0 is live.", "Then we ship."])

    def test_split_sentences_preserves_acronyms(self):
        sentences = utils._split_sentences("U.S.A. is big. Next sentence.")
        self.assertEqual(sentences, ["U.S.A. is big.", "Next sentence."])

    def test_split_sentences_preserves_honorifics(self):
        sentences = utils._split_sentences("Mr. Smith arrived. He spoke.")
        self.assertEqual(sentences, ["Mr. Smith arrived.", "He spoke."])

    def test_chunk_text_hard_splits_overlong_sentence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("甲" * 25, encoding="utf-8")

            result = utils.chunk_text(str(source), str(out_dir), chunk_size=10)

            self.assertEqual(result["total_chunks"], 3)
            self.assertTrue(any("split into 3 fixed-width segment" in warning for warning in result["warnings"]))
            chunks = [path.read_text(encoding="utf-8") for path in sorted(out_dir.glob("chunk_*.txt"))]
            self.assertEqual([len(chunk) for chunk in chunks], [10, 10, 5])
            self.assertEqual("".join(chunks), "甲" * 25)

    def test_chunk_text_preserves_content_for_deepgram_style_unpunctuated_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            text = "这是一段没有标点的中文转录文本" * 50
            source.write_text(text, encoding="utf-8")

            result = utils.chunk_text(str(source), str(out_dir), chunk_size=80)

            self.assertGreater(result["total_chunks"], 1)
            chunks = [path.read_text(encoding="utf-8") for path in sorted(out_dir.glob("chunk_*.txt"))]
            self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
            self.assertEqual("".join(chunk.replace("\n\n", "") for chunk in chunks), text)

    def test_chunk_text_with_realistic_chinese_transcript_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            text = (FIXTURES_DIR / "chinese_transcript_sample.txt").read_text(encoding="utf-8").strip()
            source.write_text(text, encoding="utf-8")

            result = utils.chunk_text(str(source), str(out_dir), chunk_size=70)

            self.assertGreater(result["total_chunks"], 1)
            chunks = [path.read_text(encoding="utf-8") for path in sorted(out_dir.glob("chunk_*.txt"))]
            self.assertTrue(all(len(chunk) <= 70 for chunk in chunks))
            self.assertEqual("".join(chunk.replace("\n\n", "") for chunk in chunks), text)
            self.assertTrue(any("API 设计" in chunk or "OpenAI SDK" in chunk for chunk in chunks))

    def test_assemble_final_escapes_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.txt"
            output = Path(tmpdir) / "out.md"
            optimized.write_text("正文内容", encoding="utf-8")

            utils.assemble_final(
                str(optimized),
                str(output),
                title='He said "hi"',
                source="https://example.com/watch?v=1",
                channel='A "B"',
                created="2026-03-07",
                transcript_source="YouTube Subtitles",
            )

            content = output.read_text(encoding="utf-8")
            self.assertIn('title: "He said \\"hi\\""', content)
            self.assertIn('channel: "A \\"B\\""', content)
            self.assertIn("# He said \"hi\"", content)

    def test_assemble_final_escapes_markdown_header_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.txt"
            output = Path(tmpdir) / "out.md"
            optimized.write_text("正文内容", encoding="utf-8")

            utils.assemble_final(
                str(optimized),
                str(output),
                title="Edge # [One](Two)\nNext",
                source="https://example.com/a path(1)?q=[x]#frag",
                channel="Chan [A](B) #1",
                created="2026-03-08",
                transcript_source="YouTube Subtitles",
            )

            content = output.read_text(encoding="utf-8")
            self.assertIn(r"# Edge \# \[One\]\(Two\) Next", content)
            self.assertIn(r"[YouTube - Chan \[A\]\(B\) \#1](https://example.com/a%20path%281%29?q=%5Bx%5D#frag)", content)

    def test_verify_quality_rejects_missing_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text("这是一大段没有章节标题的文本。" * 120, encoding="utf-8")
            raw.write_text("这是一大段没有章节标题的文本。" * 100, encoding="utf-8")

            result = utils.verify_quality(str(optimized), str(raw), bilingual=False)

            self.assertFalse(result["passed"])
            self.assertTrue(any("section headers" in failure for failure in result["hard_failures"]))

    def test_verify_quality_checks_bilingual_pairs(self):
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

    def test_validate_state_is_stage_aware(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state.md"
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 120\n"
                "output_dir: /tmp/out\n"
                "mode: bilingual\n"
                "src: youtube\n"
                "source_language: en\n"
                "subtitle_source: YouTube Subtitles\n",
                encoding="utf-8",
            )

            metadata_result = utils.validate_state(str(state), stage="metadata")
            final_result = utils.validate_state(str(state), stage="final")

            self.assertTrue(metadata_result["passed"], metadata_result)
            self.assertFalse(final_result["passed"])
            self.assertTrue(any("output_file" in failure for failure in final_result["hard_failures"]))

    def test_validate_state_accepts_final_stage_when_output_file_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state.md"
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 120\n"
                "output_dir: /tmp/out\n"
                "mode: bilingual\n"
                "src: youtube\n"
                "source_language: en\n"
                "subtitle_source: YouTube Subtitles\n"
                "output_file: /tmp/out/sample.md\n",
                encoding="utf-8",
            )

            result = utils.validate_state(str(state), stage="final")

            self.assertTrue(result["passed"], result)

    def test_plan_optimization_returns_short_bilingual_operations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state.md"
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 120\n"
                "output_dir: /tmp/out\n"
                "mode: bilingual\n"
                "src: youtube\n"
                "source_language: en\n"
                "subtitle_source: YouTube Subtitles\n"
                "work_dir: /tmp/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["video_path"], "short")
            self.assertEqual([op["prompt"] for op in result["operations"]], ["structure_only", "translate_only"])
            self.assertFalse(result["requires_llm_preflight"])

    def test_plan_optimization_returns_long_deepgram_operations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state.md"
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 3600\n"
                "output_dir: /tmp/out\n"
                "mode: chinese\n"
                "src: deepgram\n"
                "source_language: zh\n"
                "subtitle_source: Deepgram Transcription\n"
                "work_dir: /tmp/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["video_path"], "long")
            self.assertTrue(result["requires_llm_preflight"])
            self.assertEqual(result["operations"][0]["prompt"], "structure_only")
            self.assertIn("Chinese character spacing", result["operations"][0]["extra_instruction"])

    def test_cleanup_script_removes_state_by_default(self):
        video_id = "cleanup_state_test"
        state_file = Path(f"/tmp/{video_id}_state.md")
        try:
            state_file.write_text("state", encoding="utf-8")
            subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/cleanup.sh"), video_id],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse(state_file.exists())
        finally:
            state_file.unlink(missing_ok=True)

    def test_cleanup_script_can_keep_state(self):
        video_id = "cleanup_keep_state_test"
        state_file = Path(f"/tmp/{video_id}_state.md")
        try:
            state_file.write_text("state", encoding="utf-8")
            subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/cleanup.sh"), video_id, "--keep-state"],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(state_file.exists())
        finally:
            state_file.unlink(missing_ok=True)

    def test_download_metadata_returns_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = \"--print\" ]; then\n"
                "  case \"$2\" in\n"
                "    '%(id)s') echo 'abc123' ;;\n"
                "    '%(title)s') echo 'He said \"hi\"' ;;\n"
                "    '%(duration)s') echo '42' ;;\n"
                "    '%(upload_date)s') echo '20260307' ;;\n"
                "    '%(channel)s') echo 'A \"B\"' ;;\n"
                "  esac\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            result = subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "metadata"],
                cwd=PROJECT_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["video_id"], "abc123")
            self.assertEqual(payload["title"], 'He said "hi"')
            self.assertEqual(payload["channel"], 'A "B"')

    def test_subtitle_info_prefers_english_for_bilingual(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = \"--print\" ] && [ \"$2\" = '%(id)s' ]; then\n"
                "  echo 'vid001'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = \"--list-subs\" ]; then\n"
                "  cat <<'EOF'\n"
                "[info] Available subtitles for vid001:\n"
                "Language Name                  Formats\n"
                "zh-Hans  Chinese (Simplified)  vtt, ttml\n"
                "en       English              vtt, ttml\n"
                "[info] Available automatic captions for vid001:\n"
                "Language Name                  Formats\n"
                "en-US    English (US)         vtt, ttml\n"
                "EOF\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            result = subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "subtitle-info"],
                cwd=PROJECT_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["has_manual"])
            self.assertTrue(payload["english_available"])
            self.assertTrue(payload["chinese_available"])
            self.assertEqual(payload["preferred_source_language"], "en")
            self.assertEqual(payload["preferred_source_kind"], "manual")
            self.assertEqual(payload["mode"], "bilingual")

    def test_subtitle_info_propagates_yt_dlp_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = \"--print\" ] && [ \"$2\" = '%(id)s' ]; then\n"
                "  echo 'vid001'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = \"--list-subs\" ]; then\n"
                "  echo 'ERROR: network failed' >&2\n"
                "  exit 1\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            result = subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "subtitle-info"],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("network failed", result.stderr)

    def test_subtitles_selects_manual_english_source_file(self):
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                fake_bin = Path(tmpdir) / "yt-dlp"
                fake_bin.write_text(
                    "#!/usr/bin/env bash\n"
                    "if [ \"$1\" = \"--print\" ] && [ \"$2\" = '%(id)s' ]; then\n"
                    "  echo 'vid001'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [ \"$1\" = \"--list-subs\" ]; then\n"
                    "  cat <<'EOF'\n"
                    "[info] Available subtitles for vid001:\n"
                    "Language Name                  Formats\n"
                    "zh-Hans  Chinese (Simplified)  vtt, ttml\n"
                    "en       English              vtt, ttml\n"
                    "[info] Available automatic captions for vid001:\n"
                    "Language Name                  Formats\n"
                    "en-US    English (US)         vtt, ttml\n"
                    "EOF\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [ \"$1\" = \"--write-sub\" ]; then\n"
                    "  out=''\n"
                    "  while [ $# -gt 0 ]; do\n"
                    "    if [ \"$1\" = \"-o\" ]; then\n"
                    "      out=\"$2\"\n"
                    "      shift 2\n"
                    "      continue\n"
                    "    fi\n"
                    "    shift\n"
                    "  done\n"
                    "  : > \"${out}.en.vtt\"\n"
                    "  : > \"${out}.en-US.vtt\"\n"
                    "  : > \"${out}.zh-Hans.vtt\"\n"
                    "  exit 0\n"
                    "fi\n"
                    "exit 1\n",
                    encoding="utf-8",
                )
                fake_bin.chmod(0o755)

                env = os.environ.copy()
                env["PATH"] = f"{tmpdir}:{env['PATH']}"
                result = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "subtitles"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["selected_source_vtt"], "/tmp/vid001.en.vtt")
                self.assertEqual(payload["selected_source_language"], "en")
                self.assertEqual(payload["selected_source_kind"], "manual")
        finally:
            Path("/tmp/vid001.en.vtt").unlink(missing_ok=True)
            Path("/tmp/vid001.en-US.vtt").unlink(missing_ok=True)
            Path("/tmp/vid001.zh-Hans.vtt").unlink(missing_ok=True)

    def test_load_config_parses_llm_tuning_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'llm_timeout_sec: "180"\n'
                'llm_max_retries: "5"\n'
                'llm_backoff_sec: "2.5"\n'
                'llm_stream: "false"\n'
                'llm_probe_timeout_sec: "9"\n'
                'llm_probe_max_tokens: "7"\n'
                'llm_stop_after_consecutive_timeouts: "4"\n',
                encoding="utf-8",
            )
            config = utils.load_config(str(config_path))
            self.assertEqual(config["llm_timeout_sec"], 180)
            self.assertEqual(config["llm_max_retries"], 5)
            self.assertEqual(config["llm_backoff_sec"], 2.5)
            self.assertEqual(config["llm_stream"], "false")
            self.assertEqual(config["llm_probe_timeout_sec"], 9)
            self.assertEqual(config["llm_probe_max_tokens"], 7)
            self.assertEqual(config["llm_stop_after_consecutive_timeouts"], 4)

    def test_chunk_text_uses_prompt_aware_default_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("甲" * 9000, encoding="utf-8")

            result = utils.chunk_text(str(source), str(out_dir), 0, "structure_only")
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(result["chunk_size"], 4000)
            self.assertEqual(manifest["recommended_chunk_size"], 4000)
            self.assertGreater(result["total_chunks"], 1)

    def test_call_llm_api_retries_timeout_then_succeeds(self):
        with mock.patch.object(utils, "_execute_llm_request") as mocked_request, mock.patch("time.sleep"):
            mocked_request.side_effect = [
                utils.LLMRequestError("timeout", error_type="timeout", retryable=True, request_url="https://api.example.com/v1/chat/completions"),
                {"text": "ok", "latency_ms": 12, "request_url": "https://api.example.com/v1/chat/completions", "streaming_used": True},
            ]

            result = utils._call_llm_api(
                api_key="key",
                base_url="https://api.example.com",
                model="demo",
                messages=[{"role": "user", "content": "hello"}],
            )

            self.assertEqual(result["text"], "ok")
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(mocked_request.call_count, 2)

    def test_call_llm_api_fails_fast_on_http_400(self):
        with mock.patch.object(utils, "_execute_llm_request") as mocked_request, mock.patch("time.sleep"):
            mocked_request.side_effect = utils.LLMRequestError(
                "bad request",
                error_type="http_400",
                status_code=400,
                retryable=False,
                request_url="https://api.example.com/v1/chat/completions",
            )

            with self.assertRaises(utils.LLMRequestError):
                utils._call_llm_api(
                    api_key="key",
                    base_url="https://api.example.com",
                    model="demo",
                    messages=[{"role": "user", "content": "hello"}],
                )

            self.assertEqual(mocked_request.call_count, 1)

    def test_process_chunks_skips_done_chunks_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")
            utils.chunk_text(str(source), str(work_dir), 50, "structure_only")

            manifest_path = work_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["chunks"][0]["status"] = "done"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            (work_dir / "processed_000.md").write_text("## 已完成\n\n内容", encoding="utf-8")

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

    def test_process_chunks_aborts_after_consecutive_timeouts(self):
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
            self.assertEqual(result["failed_count"], 2)
            self.assertEqual(manifest["chunks"][0]["status"], "failed")
            self.assertEqual(manifest["chunks"][1]["status"], "failed")

    def test_test_llm_api_returns_probe_metadata(self):
        with mock.patch.object(utils, "load_config", return_value={
            "llm_api_key": "key",
            "llm_base_url": "https://api.example.com",
            "llm_model": "demo",
            "llm_api_format": "openai",
            "llm_probe_timeout_sec": 10,
            "llm_probe_max_tokens": 8,
            "llm_backoff_sec": 0.5,
            "llm_stream": "auto",
        }), mock.patch.object(utils, "_call_llm_api", return_value={
            "text": "OK",
            "latency_ms": 42,
            "request_url": "https://api.example.com/v1/chat/completions",
            "streaming_used": True,
            "attempts": 1,
        }):
            result = utils.test_llm_api()

        self.assertTrue(result["valid"])
        self.assertEqual(result["latency_ms"], 42)
        self.assertTrue(result["streaming_used"])


if __name__ == "__main__":
    unittest.main()
