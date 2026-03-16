import io
import json
import os
import shutil
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

    def test_build_token_count_url_accepts_root_v1_and_messages(self):
        self.assertEqual(
            utils._build_token_count_url("https://api.anthropic.com", "anthropic"),
            "https://api.anthropic.com/v1/messages/count_tokens",
        )
        self.assertEqual(
            utils._build_token_count_url("https://api.anthropic.com/v1", "anthropic"),
            "https://api.anthropic.com/v1/messages/count_tokens",
        )
        self.assertEqual(
            utils._build_token_count_url("https://api.anthropic.com/v1/messages", "anthropic"),
            "https://api.anthropic.com/v1/messages/count_tokens",
        )
        self.assertEqual(
            utils._build_token_count_url("https://api.example.com", "openai"),
            "",
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

    def test_estimate_tokens_heuristic_for_common_text_shapes(self):
        self.assertAlmostEqual(utils._estimate_tokens("hello world"), 3, delta=1)
        self.assertAlmostEqual(utils._estimate_tokens("你好世界"), 4, delta=1)
        self.assertAlmostEqual(utils._estimate_tokens("Hello 世界"), 4, delta=1)
        self.assertEqual(utils._estimate_tokens("abc", mode="chars"), 3)

    def test_chunk_text_hard_splits_overlong_sentence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"
            source.write_text("甲" * 25, encoding="utf-8")
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )

            result = utils.chunk_text(str(source), str(out_dir), chunk_size=10, config_path=str(config_path))

            self.assertEqual(result["total_chunks"], 3)
            self.assertTrue(any("split into 3 fixed-width segment" in warning for warning in result["warnings"]))
            chunks = [path.read_text(encoding="utf-8") for path in sorted(out_dir.glob("chunk_*.txt"))]
            self.assertEqual([len(chunk) for chunk in chunks], [10, 10, 5])
            self.assertEqual("".join(chunks), "甲" * 25)

    def test_chunk_text_preserves_content_for_deepgram_style_unpunctuated_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"
            text = "这是一段没有标点的中文转录文本" * 50
            source.write_text(text, encoding="utf-8")
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )

            result = utils.chunk_text(str(source), str(out_dir), chunk_size=80, config_path=str(config_path))

            self.assertGreater(result["total_chunks"], 1)
            chunks = [path.read_text(encoding="utf-8") for path in sorted(out_dir.glob("chunk_*.txt"))]
            self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
            self.assertEqual("".join(chunk.replace("\n\n", "") for chunk in chunks), text)

    def test_chunk_text_with_realistic_chinese_transcript_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"
            text = (FIXTURES_DIR / "chinese_transcript_sample.txt").read_text(encoding="utf-8").strip()
            source.write_text(text, encoding="utf-8")
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )

            result = utils.chunk_text(str(source), str(out_dir), chunk_size=70, config_path=str(config_path))

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

    def test_transcribe_deepgram_merges_chunk_outputs_and_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "source.mp3"
            chunk_a = Path(tmpdir) / "chunk_a.mp3"
            chunk_b = Path(tmpdir) / "chunk_b.mp3"
            output_json = Path(tmpdir) / "deepgram.json"
            output_text = Path(tmpdir) / "raw.txt"
            audio_path.write_bytes(b"src")
            chunk_a.write_bytes(b"a")
            chunk_b.write_bytes(b"b")

            payloads = [
                {"results": {"channels": [{"alternatives": [{"transcript": "first"}]}]}},
                {"results": {"channels": [{"alternatives": [{"transcript": "second"}]}]}},
            ]
            processed = [
                {"transcript": "Alpha", "speaker_count": 1},
                {"transcript": "Beta", "speaker_count": 2},
            ]

            with mock.patch.object(utils, "split_audio", return_value={
                "chunks": [str(chunk_a), str(chunk_b)],
                "split_points": [12.5],
            }), mock.patch.object(utils, "_call_deepgram_api", side_effect=payloads), mock.patch.object(
                utils,
                "process_deepgram_payload",
                side_effect=processed,
            ):
                result = utils.transcribe_deepgram(
                    str(audio_path),
                    "en",
                    api_key="key",
                    output_json=str(output_json),
                    output_text=str(output_text),
                )

            self.assertEqual(result["transcript"], "Alpha\n\nBeta")
            self.assertEqual(result["speaker_count"], 2)
            self.assertEqual(output_text.read_text(encoding="utf-8"), "Alpha\n\nBeta")
            self.assertEqual(result["chunk_count"], 2)
            self.assertTrue(result["used_split_mode"])
            self.assertEqual(len(result["json_outputs"]), 2)
            self.assertTrue(output_text.exists())
            self.assertTrue(all(Path(item).exists() for item in result["json_outputs"]))

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
            self.assertTrue(all(op["execution"]["mode"] == "single_pass" for op in result["operations"]))
            self.assertTrue(all(not op["execution"]["supports_auto_replan"] for op in result["operations"]))

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
            self.assertTrue(result["operations"][0]["execution"]["supports_auto_replan"])
            self.assertEqual(result["operations"][0]["execution"]["recommended_cli_flags"], ["--auto-replan"])
            self.assertEqual(result["operations"][0]["execution"]["on_replan_required"], "auto_replan_remaining")
            self.assertTrue(result["replan_contract"]["raw_path"]["supports_auto_replan"])

    def test_plan_optimization_marks_processed_path_chunk_stage_for_manual_review(self):
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
                "mode: bilingual\n"
                "src: youtube\n"
                "source_language: en\n"
                "subtitle_source: YouTube Subtitles\n"
                "work_dir: /tmp/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual([op["prompt"] for op in result["operations"]], ["structure_only", "translate_only"])
            self.assertTrue(result["operations"][0]["execution"]["supports_auto_replan"])
            self.assertFalse(result["operations"][1]["execution"]["supports_auto_replan"])
            self.assertEqual(result["operations"][1]["execution"]["on_replan_required"], "stop_and_review")
            self.assertEqual(result["replan_contract"]["processed_path"]["on_replan_required"], "stop_and_review")

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

    def test_cleanup_script_rejects_unsafe_video_id(self):
        result = subprocess.run(
            ["bash", str(PROJECT_ROOT / "scripts/cleanup.sh"), "../bad-id"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe characters", result.stderr)

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

    def test_download_metadata_fails_when_video_id_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = \"--print\" ] && [ \"$2\" = '%(id)s' ]; then\n"
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
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not resolve a video ID", result.stderr)

    def test_download_metadata_retries_with_chrome_after_not_a_bot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
has_chrome=false
print_field=''
while [ $# -gt 0 ]; do
  if [ "$1" = "--cookies-from-browser" ] && [ $# -ge 2 ] && [ "$2" = "chrome" ]; then
    has_chrome=true
    shift 2
    continue
  fi
  if [ "$1" = "--print" ] && [ $# -ge 2 ]; then
    print_field="$2"
    shift 2
    continue
  fi
  shift
done

if [ "$has_chrome" != true ]; then
  echo "ERROR: [youtube] abc123: Sign in to confirm you're not a bot" >&2
  exit 1
fi

case "$print_field" in
  '%(id)s') echo 'abc123' ;;
  '%(title)s') echo 'Recovered title' ;;
  '%(duration)s') echo '42' ;;
  '%(upload_date)s') echo '20260316' ;;
  '%(channel)s') echo 'Recovered channel' ;;
  *) exit 1 ;;
esac
""",
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
            self.assertEqual(payload["title"], "Recovered title")
            self.assertEqual(payload["channel"], "Recovered channel")
            self.assertIn("retrying with Chrome cookies", result.stderr)
            self.assertIn("attempt 1/3", result.stderr)

    def test_download_metadata_guides_cookie_file_when_chrome_retry_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
has_chrome=false
while [ $# -gt 0 ]; do
  if [ "$1" = "--cookies-from-browser" ] && [ $# -ge 2 ] && [ "$2" = "chrome" ]; then
    has_chrome=true
    shift 2
    continue
  fi
  shift
done

if [ "$has_chrome" = true ]; then
  echo "ERROR: could not find chrome cookies database" >&2
  exit 1
fi

echo "ERROR: [youtube] abc123: Sign in to confirm you're not a bot" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            result = subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "metadata"],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("retrying with Chrome cookies", result.stderr)
            self.assertIn("attempt 1/3", result.stderr)
            self.assertIn("Automatic Chrome cookies retry failed", result.stderr)
            self.assertIn("yt_dlp_cookies_file", result.stderr)
            self.assertIn("Netscape-format cookies.txt", result.stderr)

    def test_process_chunks_dry_run_does_not_require_llm_credentials(self):
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

    def test_subtitle_info_stops_routing_unsupported_languages_as_chinese(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--print" ] && [ "$2" = '%(id)s' ]; then
  echo 'vides'
  exit 0
fi
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vides","subtitles":{"es":[{"ext":"vtt"}]}}
EOF
  exit 0
fi
exit 1
""",
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
            self.assertTrue(payload["has_any"])
            self.assertFalse(payload["english_available"])
            self.assertFalse(payload["chinese_available"])
            self.assertEqual(payload["preferred_source_language"], "")
            self.assertEqual(payload["preferred_source_kind"], "")
            self.assertEqual(payload["mode"], "")

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

    def test_subtitle_info_reads_structured_metadata_when_json_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--print" ] && [ "$2" = '%(id)s' ]; then
  echo 'vidjson'
  exit 0
fi
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vidjson","subtitles":{"en":[{"ext":"vtt"}],"zh-Hans":[{"ext":"vtt"}]},"automatic_captions":{"en-US":[{"ext":"vtt"}]}}
EOF
  exit 0
fi
if [ "$1" = "--list-subs" ]; then
  echo 'should not be used' >&2
  exit 99
fi
exit 1
""",
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
            self.assertEqual(payload["video_id"], "vidjson")
            self.assertTrue(payload["english_available"])
            self.assertTrue(payload["chinese_available"])
            self.assertEqual(payload["preferred_source_language"], "en")
            self.assertEqual(payload["preferred_source_kind"], "manual")
            self.assertEqual(payload["mode"], "bilingual")
    def test_subtitles_selects_manual_english_source_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--print" ] && [ "$2" = '%(id)s' ]; then
  echo 'vid001'
  exit 0
fi
if [ "$1" = "--list-subs" ]; then
  cat <<'EOF'
[info] Available subtitles for vid001:
Language Name                  Formats
zh-Hans  Chinese (Simplified)  vtt, ttml
en       English              vtt, ttml
[info] Available automatic captions for vid001:
Language Name                  Formats
en-US    English (US)         vtt, ttml
EOF
  exit 0
fi
if [ "$1" = "--write-sub" ]; then
  out=''
  while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then
      out="$2"
      shift 2
      continue
    fi
    shift
  done
  mkdir -p "$(dirname "$out")"
  : > "${out}.en.vtt"
  : > "${out}.en-US.vtt"
  : > "${out}.zh-Hans.vtt"
  exit 0
fi
exit 1
""",
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
            self.assertEqual(payload["selected_source_vtt"], "/tmp/vid001_downloads/subtitles/vid001.en.vtt")
            self.assertEqual(payload["selected_source_language"], "en")
            self.assertEqual(payload["selected_source_kind"], "manual")
            shutil.rmtree("/tmp/vid001_downloads", ignore_errors=True)
    def test_subtitles_selects_manual_english_source_file_from_isolated_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--print" ] && [ "$2" = '%(id)s' ]; then
  echo 'vid001'
  exit 0
fi
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vid001","subtitles":{"en":[{"ext":"vtt"}],"zh-Hans":[{"ext":"vtt"}]},"automatic_captions":{"en-US":[{"ext":"vtt"}]}}
EOF
  exit 0
fi
if [ "$1" = "--write-sub" ]; then
  out=''
  while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then
      out="$2"
      shift 2
      continue
    fi
    shift
  done
  mkdir -p "$(dirname "$out")"
  : > "${out}.en.vtt"
  : > "${out}.en-US.vtt"
  : > "${out}.zh-Hans.vtt"
  exit 0
fi
exit 1
""",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            stale = Path("/tmp/vid001.en.vtt")
            stale.write_text("stale", encoding="utf-8")
            try:
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
                self.assertEqual(payload["selected_source_vtt"], "/tmp/vid001_downloads/subtitles/vid001.en.vtt")
                self.assertNotEqual(payload["selected_source_vtt"], str(stale))
                self.assertTrue(Path(payload["selected_source_vtt"]).exists())
            finally:
                stale.unlink(missing_ok=True)
                shutil.rmtree("/tmp/vid001_downloads", ignore_errors=True)

    def test_subtitles_downloads_supported_english_variant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--print" ] && [ "$2" = '%(id)s' ]; then
  echo 'vidgb'
  exit 0
fi
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vidgb","subtitles":{"en-GB":[{"ext":"vtt"}]}}
EOF
  exit 0
fi
if [ "$1" = "--write-sub" ]; then
  out=''
  langs=''
  while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then
      out="$2"
      shift 2
      continue
    fi
    if [ "$1" = "--sub-lang" ]; then
      langs="$2"
      shift 2
      continue
    fi
    shift
  done
  mkdir -p "$(dirname "$out")"
  case ",$langs," in
    *,en-GB,*) : > "${out}.en-GB.vtt" ;;
  esac
  exit 0
fi
exit 1
""",
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
            self.assertEqual(payload["selected_source_vtt"], "/tmp/vidgb_downloads/subtitles/vidgb.en-GB.vtt")
            self.assertEqual(payload["selected_source_language"], "en-GB")
            self.assertEqual(payload["selected_source_kind"], "manual")
            shutil.rmtree("/tmp/vidgb_downloads", ignore_errors=True)

    def test_subtitles_rejects_unsupported_languages_before_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--print" ] && [ "$2" = '%(id)s' ]; then
  echo 'vides'
  exit 0
fi
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vides","subtitles":{"es":[{"ext":"vtt"}]}}
EOF
  exit 0
fi
if [ "$1" = "--write-sub" ]; then
  echo 'unexpected subtitle download' >&2
  exit 99
fi
exit 1
""",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            result = subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "subtitles"],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("supports English or Chinese subtitle tracks only", result.stderr)
            self.assertNotIn("unexpected subtitle download", result.stderr)

    def test_preflight_treats_update_check_as_best_effort(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_yt_dlp = Path(tmpdir) / "yt-dlp"
            fake_yt_dlp.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then
  echo '2025.01.01'
  exit 0
fi
exit 0
""",
                encoding="utf-8",
            )
            fake_yt_dlp.chmod(0o755)

            fake_ffmpeg = Path(tmpdir) / "ffmpeg"
            fake_ffmpeg.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_ffmpeg.chmod(0o755)

            fake_curl = Path(tmpdir) / "curl"
            fake_curl.write_text("#!/usr/bin/env bash\nexit 127\n", encoding="utf-8")
            fake_curl.chmod(0o755)

            config_path = PROJECT_ROOT / "config.yaml"
            backup_path = None
            if config_path.exists():
                backup_path = Path(tmpdir) / "config.yaml.backup"
                shutil.copy2(config_path, backup_path)

            config_path.write_text('output_dir: "/tmp/yt-transcript-preflight-test"\n', encoding="utf-8")
            try:
                env = os.environ.copy()
                env["PATH"] = f"{tmpdir}:{env['PATH']}"
                result = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "scripts/preflight.sh")],
                    cwd=PROJECT_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                )
            finally:
                if backup_path is not None and backup_path.exists():
                    shutil.copy2(backup_path, config_path)
                else:
                    config_path.unlink(missing_ok=True)
                shutil.rmtree("/tmp/yt-transcript-preflight-test", ignore_errors=True)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Could not check for updates", result.stdout)
            self.assertIn("All pre-flight checks passed", result.stdout)

    def test_preflight_supports_gnu_stat_for_cache_age(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_yt_dlp = Path(tmpdir) / "yt-dlp"
            fake_yt_dlp.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then
  echo '2025.01.01'
  exit 0
fi
exit 0
""",
                encoding="utf-8",
            )
            fake_yt_dlp.chmod(0o755)

            fake_ffmpeg = Path(tmpdir) / "ffmpeg"
            fake_ffmpeg.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_ffmpeg.chmod(0o755)

            fake_stat = Path(tmpdir) / "stat"
            fake_stat.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "-f" ]; then
  exit 1
fi
if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then
  date +%s
  exit 0
fi
exit 1
""",
                encoding="utf-8",
            )
            fake_stat.chmod(0o755)

            version_cache = Path("/tmp/yt-dlp-version-cache")
            previous_cache = None
            if version_cache.exists():
                previous_cache = version_cache.read_text(encoding="utf-8")
            version_cache.write_text("2025.01.01", encoding="utf-8")

            config_path = PROJECT_ROOT / "config.yaml"
            backup_path = None
            if config_path.exists():
                backup_path = Path(tmpdir) / "config.yaml.backup"
                shutil.copy2(config_path, backup_path)

            config_path.write_text('output_dir: "/tmp/yt-transcript-preflight-test"\n', encoding="utf-8")
            try:
                env = os.environ.copy()
                env["PATH"] = f"{tmpdir}:{env['PATH']}"
                result = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "scripts/preflight.sh")],
                    cwd=PROJECT_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                )
            finally:
                if backup_path is not None and backup_path.exists():
                    shutil.copy2(backup_path, config_path)
                else:
                    config_path.unlink(missing_ok=True)
                shutil.rmtree("/tmp/yt-transcript-preflight-test", ignore_errors=True)
                if previous_cache is None:
                    version_cache.unlink(missing_ok=True)
                else:
                    version_cache.write_text(previous_cache, encoding="utf-8")

            self.assertEqual(result.returncode, 0)
            self.assertIn("yt-dlp is up to date (cached)", result.stdout)
            self.assertIn("All pre-flight checks passed", result.stdout)
    def test_audio_download_uses_isolated_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "--print" ] && [ "$2" = '%(id)s' ]; then
  echo 'vidaudio'
  exit 0
fi
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vidaudio","formats":[{"format_id":"251","language":"zh","vcodec":"none","acodec":"opus"},{"format_id":"140","language":"en","vcodec":"none","acodec":"mp4a.40.2"}]}
EOF
  exit 0
fi
if [ "$1" = "-f" ]; then
  out=''
  while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then
      out="$2"
      shift 2
      continue
    fi
    shift
  done
  mkdir -p "$(dirname "$out")"
  : > "${out//%(ext)s/mp3}"
  exit 0
fi
exit 1
""",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            stale = Path("/tmp/vidaudio.mp3")
            stale.write_text("stale", encoding="utf-8")
            try:
                env = os.environ.copy()
                env["PATH"] = f"{tmpdir}:{env['PATH']}"
                result = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "audio"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["audio_file"], "/tmp/vidaudio_downloads/audio/vidaudio.mp3")
                self.assertNotEqual(payload["audio_file"], str(stale))
                self.assertEqual(payload["audio_format"], "251")
                self.assertTrue(Path(payload["audio_file"]).exists())
            finally:
                stale.unlink(missing_ok=True)
                shutil.rmtree("/tmp/vidaudio_downloads", ignore_errors=True)
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

    def test_load_config_parses_chunk_tuning_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n'
                'chunk_size_override: "777"\n'
                'chunk_tokens_structure_only: "1111"\n'
                'chunk_tokens_quick_cleanup: "888"\n'
                'chunk_tokens_translate_only: "999"\n'
                'chunk_tokens_summarize: "2222"\n'
                'chunk_hard_cap_multiplier: "1.5"\n'
                'chunk_safety_buffer_tokens: "321"\n'
                'chunk_overlap_sentences: "0"\n'
                'chunk_context_tail_sentences: "2"\n'
                'chunk_context_summary_tokens: "70"\n'
                'output_ratio_structure_only: "1.2"\n'
                'output_ratio_quick_cleanup: "1.08"\n'
                'output_ratio_translate_only: "1.12"\n'
                'output_ratio_summarize: "0.2"\n'
                'max_output_tokens_structure_only: "1900"\n'
                'max_output_tokens_quick_cleanup: "1450"\n'
                'max_output_tokens_translate_only: "1550"\n'
                'max_output_tokens_summarize: "512"\n'
                'enable_token_count_probe: "false"\n'
                'enable_chunk_autotune: "true"\n'
                'autotune_reduce_percent: "0.3"\n'
                'autotune_increase_percent: "0.15"\n'
                'autotune_success_window: "9"\n'
                'autotune_p95_latency_threshold_ms: "12345"\n'
                'autotune_canary_chunks: "4"\n',
                encoding="utf-8",
            )

            config = utils.load_config(str(config_path))

            self.assertEqual(config["chunk_mode"], "chars")
            self.assertEqual(config["chunk_size_override"], 777)
            self.assertEqual(config["chunk_tokens_structure_only"], 1111)
            self.assertEqual(config["chunk_tokens_quick_cleanup"], 888)
            self.assertEqual(config["chunk_tokens_translate_only"], 999)
            self.assertEqual(config["chunk_tokens_summarize"], 2222)
            self.assertEqual(config["chunk_hard_cap_multiplier"], 1.5)
            self.assertEqual(config["chunk_safety_buffer_tokens"], 321)
            self.assertEqual(config["chunk_context_tail_sentences"], 2)
            self.assertEqual(config["chunk_context_summary_tokens"], 70)
            self.assertEqual(config["output_ratio_structure_only"], 1.2)
            self.assertEqual(config["output_ratio_quick_cleanup"], 1.08)
            self.assertEqual(config["output_ratio_translate_only"], 1.12)
            self.assertEqual(config["output_ratio_summarize"], 0.2)
            self.assertEqual(config["max_output_tokens_structure_only"], 1900)
            self.assertEqual(config["max_output_tokens_quick_cleanup"], 1450)
            self.assertEqual(config["max_output_tokens_translate_only"], 1550)
            self.assertEqual(config["max_output_tokens_summarize"], 512)
            self.assertFalse(config["enable_token_count_probe"])
            self.assertTrue(config["enable_chunk_autotune"])
            self.assertEqual(config["autotune_reduce_percent"], 0.3)
            self.assertEqual(config["autotune_increase_percent"], 0.15)
            self.assertEqual(config["autotune_success_window"], 9)
            self.assertEqual(config["autotune_p95_latency_threshold_ms"], 12345)
            self.assertEqual(config["autotune_canary_chunks"], 4)

    def test_load_config_invalid_chunk_values_fall_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "weird"\n'
                'chunk_size_override: "oops"\n'
                'chunk_tokens_structure_only: "bad"\n'
                'chunk_hard_cap_multiplier: "10.0"\n'
                'chunk_safety_buffer_tokens: "-1"\n'
                'chunk_context_tail_sentences: "-1"\n'
                'chunk_context_summary_tokens: "-1"\n'
                'output_ratio_summarize: "bad"\n'
                'max_output_tokens_summarize: "0"\n'
                'enable_token_count_probe: "banana"\n'
                'enable_chunk_autotune: "banana"\n'
                'autotune_reduce_percent: "1.2"\n'
                'autotune_increase_percent: "0"\n'
                'autotune_success_window: "0"\n'
                'autotune_p95_latency_threshold_ms: "-1"\n'
                'autotune_canary_chunks: "0"\n',
                encoding="utf-8",
            )

            with mock.patch('sys.stderr', new_callable=io.StringIO) as fake_stderr:
                config = utils.load_config(str(config_path))

            self.assertEqual(config["chunk_mode"], "tokens")
            self.assertEqual(config["chunk_size_override"], 0)
            self.assertEqual(config["chunk_tokens_structure_only"], 1200)
            self.assertEqual(config["chunk_hard_cap_multiplier"], 1.33)
            self.assertEqual(config["chunk_safety_buffer_tokens"], 400)
            self.assertEqual(config["chunk_context_tail_sentences"], 1)
            self.assertEqual(config["chunk_context_summary_tokens"], 60)
            self.assertEqual(config["output_ratio_summarize"], 0.15)
            self.assertEqual(config["max_output_tokens_summarize"], 384)
            self.assertTrue(config["enable_token_count_probe"])
            self.assertFalse(config["enable_chunk_autotune"])
            self.assertEqual(config["autotune_reduce_percent"], 0.25)
            self.assertEqual(config["autotune_increase_percent"], 0.10)
            self.assertEqual(config["autotune_success_window"], 20)
            self.assertEqual(config["autotune_p95_latency_threshold_ms"], 45000)
            self.assertEqual(config["autotune_canary_chunks"], 3)
            self.assertGreaterEqual(len(config["config_warnings"]), 10)
            warning_output = fake_stderr.getvalue()
            self.assertIn("Warning: Invalid numeric config values", warning_output)
            self.assertIn("chunk_size_override='oops'", warning_output)
            self.assertIn("chunk_hard_cap_multiplier='10.0'", warning_output)
            self.assertIn("max_output_tokens_summarize='0'", warning_output)
            self.assertIn("autotune_reduce_percent='1.2'", warning_output)

    def test_chunk_text_uses_token_aware_default_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("甲" * 9000, encoding="utf-8")

            result = utils.chunk_text(str(source), str(out_dir), 0, "structure_only")
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(result["chunk_mode"], "tokens")
            self.assertEqual(manifest["chunk_mode"], "tokens")
            self.assertEqual(result["target_tokens"], manifest["target_tokens"])
            self.assertLess(manifest["planned_max_output_tokens"], 1800)
            self.assertGreater(result["total_chunks"], 1)

    def test_chunk_text_chars_mode_preserves_legacy_prompt_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"
            source.write_text("甲" * 9000, encoding="utf-8")
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )

            result = utils.chunk_text(str(source), str(out_dir), 0, "structure_only", str(config_path))
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(result["chunk_mode"], "chars")
            self.assertEqual(result["chunk_size"], 4000)
            self.assertEqual(manifest["recommended_chunk_size"], 4000)

    def test_chunk_text_varies_by_prompt_in_token_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            structure_dir = Path(tmpdir) / "structure"
            summary_dir = Path(tmpdir) / "summary"
            source.write_text(("这是一个用于测试分块预算的句子。" * 800), encoding="utf-8")

            structure_result = utils.chunk_text(str(source), str(structure_dir), 0, "structure_only")
            summary_result = utils.chunk_text(str(source), str(summary_dir), 0, "summarize")

            self.assertEqual(structure_result["chunk_mode"], "tokens")
            self.assertEqual(summary_result["chunk_mode"], "tokens")
            self.assertLess(structure_result["chunk_size"], summary_result["chunk_size"])
            self.assertGreater(structure_result["total_chunks"], summary_result["total_chunks"])

    def test_chunk_text_explicit_size_without_prompt_uses_legacy_char_sizing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("甲" * 25, encoding="utf-8")

            result = utils.chunk_text(str(source), str(out_dir), 10)
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            chunks = [path.read_text(encoding="utf-8") for path in sorted(out_dir.glob("chunk_*.txt"))]

            self.assertEqual(result["chunk_mode"], "chars")
            self.assertEqual(manifest["chunk_mode"], "chars")
            self.assertEqual(result["total_chunks"], 3)
            self.assertEqual([len(chunk) for chunk in chunks], [10, 10, 5])

    def test_chunk_text_missing_explicit_config_path_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")

            with self.assertRaises(SystemExit):
                utils.chunk_text(str(source), str(out_dir), 0, "structure_only", "/no/such/config.yaml")

    def test_chunk_text_unknown_prompt_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")

            with self.assertRaises(SystemExit):
                utils.chunk_text(str(source), str(out_dir), 0, "not_a_prompt")

    def test_chunk_text_rejects_prompt_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")

            with self.assertRaises(SystemExit):
                utils.chunk_text(str(source), str(out_dir), 0, "../CODE_REVIEW")

    def test_calculate_chunk_budget_varies_by_prompt_and_override(self):
        default_config = utils._default_config_values()
        structure_prompt = (PROJECT_ROOT / "prompts" / "structure_only.md").read_text(encoding="utf-8")
        summary_prompt = (PROJECT_ROOT / "prompts" / "summarize.md").read_text(encoding="utf-8")

        structure_budget = utils._calculate_chunk_budget("structure_only", structure_prompt, default_config)
        summary_budget = utils._calculate_chunk_budget("summarize", summary_prompt, default_config)

        self.assertLess(structure_budget["target_tokens"], summary_budget["target_tokens"])
        self.assertLess(structure_budget["planned_max_output_tokens"], 1800)
        self.assertEqual(summary_budget["planned_max_output_tokens"], 384)

        structure_total = (
            structure_budget["prompt_tokens"]
            + structure_budget["target_tokens"]
            + structure_budget["planned_max_output_tokens"]
            + structure_budget["safety_buffer_tokens"]
        )
        self.assertLessEqual(structure_total, structure_budget["effective_budget_tokens"])

        override_config = utils._default_config_values()
        override_config["chunk_size_override"] = 777
        self.assertEqual(utils._get_task_chunk_target("structure_only", override_config), 777)

    def test_calculate_chunk_budget_reserves_continuity_context(self):
        structure_prompt = (PROJECT_ROOT / "prompts" / "structure_only.md").read_text(encoding="utf-8")
        with_continuity = utils._default_config_values()
        without_continuity = utils._default_config_values()
        without_continuity["chunk_context_tail_sentences"] = 0

        budget_with = utils._calculate_chunk_budget("structure_only", structure_prompt, with_continuity)
        budget_without = utils._calculate_chunk_budget("structure_only", structure_prompt, without_continuity)

        self.assertGreater(budget_with["continuity_reserve_tokens"], 0)
        self.assertEqual(budget_without["continuity_reserve_tokens"], 0)
        self.assertGreater(budget_with["prompt_tokens"], budget_without["prompt_tokens"])
        self.assertLess(budget_with["target_tokens"], budget_without["target_tokens"])

    def test_inject_continuity_context_appends_when_no_anchor(self):
        result = utils._inject_continuity_context("Prompt header", "## Continuity Context\ncontext")
        self.assertEqual(result, "Prompt header\n\n## Continuity Context\ncontext\n")

    def test_truncate_tail_text_to_tokens_handles_boundary_cases(self):
        self.assertEqual(utils._truncate_tail_text_to_tokens("", 10), "")
        self.assertEqual(utils._truncate_tail_text_to_tokens(" 甲乙 ", 0), "甲乙")
        self.assertEqual(utils._truncate_tail_text_to_tokens("甲乙丙丁", 2), "丙丁")

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
            self.assertEqual(len(result["attempt_history"]), 2)
            self.assertEqual(result["attempt_history"][0]["error_type"], "timeout")
            self.assertEqual(result["attempt_history"][1]["result"], "success")
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

    def test_process_chunks_uses_prompt_specific_max_output_tokens(self):
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

    def test_process_chunks_reuses_manifest_token_estimates_without_remote_probe(self):
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
                "[EN1]\n\n[ZH1]",
                "[EN2]\n\n[ZH2]",
                "[EN3]\n\n[ZH3]",
                "[EN4]\n\n[ZH4]",
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

    def test_replan_remaining_supersedes_pending_chunks_and_appends_new_plan(self):
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

    def test_process_chunks_with_replans_stops_when_replan_step_fails(self):
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
            result = utils.process_chunks_with_replans("/tmp/demo", "structure_only", max_replans=1)

        self.assertFalse(result["success"])
        self.assertTrue(result["aborted"])
        self.assertTrue(result["replan_required"])
        self.assertIn("failed to generate replacement plan", result["aborted_reason"])
        self.assertEqual(result["warning_count"], 1)

    def test_merge_content_keeps_chapter_headers_after_replan_remaining(self):
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

    def test_test_llm_api_can_run_from_explicit_overrides_without_config(self):
        with mock.patch.object(utils, "load_config", side_effect=AssertionError("explicit overrides should not require load_config")), mock.patch.object(
            utils,
            "_load_optional_config",
            return_value=utils._default_config_values(),
        ), mock.patch.object(utils, "_call_llm_api", return_value={
            "text": "OK",
            "latency_ms": 42,
            "request_url": "https://api.example.com/v1/chat/completions",
            "streaming_used": False,
            "attempts": 1,
        }):
            result = utils.test_llm_api(
                api_key="key",
                base_url="https://api.example.com",
                model="demo",
                api_format="openai",
            )

        self.assertTrue(result["valid"])
        self.assertEqual(result["request_url"], "https://api.example.com/v1/chat/completions")

    def test_count_tokens_via_provider_uses_anthropic_endpoint(self):
        config = utils._default_config_values()
        config.update({
            "enable_token_count_probe": True,
            "llm_api_key": "key",
            "llm_base_url": "https://api.anthropic.com",
            "llm_model": "claude-demo",
            "llm_api_format": "anthropic",
            "llm_probe_timeout_sec": 5,
        })
        requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"input_tokens": 7}'

        def fake_urlopen(req, timeout=0):
            requests.append((req.full_url, timeout, req.data.decode("utf-8")))
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = utils._count_tokens_via_provider("Hello world", config=config)

        self.assertTrue(result["valid"])
        self.assertTrue(result["provider_supported"])
        self.assertEqual(result["token_count_source"], "provider")
        self.assertEqual(result["token_count"], 7)
        self.assertEqual(requests[0][0], "https://api.anthropic.com/v1/messages/count_tokens")
        self.assertIn('"model": "claude-demo"', requests[0][2])

    def test_count_tokens_via_provider_http_error_falls_back_cleanly(self):
        import urllib.error

        config = utils._default_config_values()
        config.update({
            "enable_token_count_probe": True,
            "llm_api_key": "key",
            "llm_base_url": "https://api.anthropic.com",
            "llm_model": "claude-demo",
            "llm_api_format": "anthropic",
            "llm_probe_timeout_sec": 5,
        })
        http_error = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages/count_tokens",
            429,
            "rate limited",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"rate limited"}'),
        )

        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            result = utils._count_tokens_via_provider("Hello world", config=config)

        self.assertFalse(result["valid"])
        self.assertEqual(result["error_type"], "http_429")
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["token_count_source"], "local_estimate")
        self.assertGreater(result["token_count"], 0)

    def test_count_tokens_via_provider_network_error_falls_back_cleanly(self):
        import urllib.error

        config = utils._default_config_values()
        config.update({
            "enable_token_count_probe": True,
            "llm_api_key": "key",
            "llm_base_url": "https://api.anthropic.com",
            "llm_model": "claude-demo",
            "llm_api_format": "anthropic",
            "llm_probe_timeout_sec": 5,
        })

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns failed")):
            result = utils._count_tokens_via_provider("Hello world", config=config)

        self.assertFalse(result["valid"])
        self.assertEqual(result["error_type"], "network")
        self.assertEqual(result["token_count_source"], "local_estimate")
        self.assertIn("dns failed", result["error"])

    def test_count_tokens_via_provider_timeout_falls_back_cleanly(self):
        import socket

        config = utils._default_config_values()
        config.update({
            "enable_token_count_probe": True,
            "llm_api_key": "key",
            "llm_base_url": "https://api.anthropic.com",
            "llm_model": "claude-demo",
            "llm_api_format": "anthropic",
            "llm_probe_timeout_sec": 5,
        })

        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("probe timed out")):
            result = utils._count_tokens_via_provider("Hello world", config=config)

        self.assertFalse(result["valid"])
        self.assertEqual(result["error_type"], "timeout")
        self.assertEqual(result["token_count_source"], "local_estimate")
        self.assertIn("probe timed out", result["error"])

    def test_test_token_count_falls_back_to_local_estimate(self):
        with mock.patch.object(utils, "load_config", return_value={
            "enable_token_count_probe": True,
            "llm_api_key": "key",
            "llm_base_url": "https://api.example.com",
            "llm_model": "demo",
            "llm_api_format": "openai",
            "llm_probe_timeout_sec": 5,
        }):
            result = utils.test_token_count(sample_text="Hello 世界")

        self.assertTrue(result["valid"])
        self.assertFalse(result["provider_supported"])
        self.assertEqual(result["token_count_source"], "local_estimate")
        self.assertTrue(result["fallback_used"])
        self.assertGreater(result["token_count"], 0)

    def test_test_token_count_can_run_from_explicit_overrides_without_config(self):
        with mock.patch.object(utils, "load_config", side_effect=AssertionError("explicit overrides should not require load_config")), mock.patch.object(
            utils,
            "_load_optional_config",
            return_value=utils._default_config_values(),
        ), mock.patch.object(utils, "_count_tokens_via_provider", return_value={
            "valid": False,
            "provider_supported": False,
            "token_count": 5,
            "token_count_source": "local_estimate",
            "request_url": "",
            "latency_ms": None,
            "api_format": "openai",
            "error_type": "unsupported_api_format",
            "error": "Provider token counting is not implemented",
        }):
            result = utils.test_token_count(
                api_key="key",
                base_url="https://api.example.com",
                model="demo",
                api_format="openai",
                sample_text="Hello 世界",
            )

        self.assertTrue(result["valid"])
        self.assertFalse(result["provider_supported"])
        self.assertEqual(result["token_count"], 5)


if __name__ == "__main__":
    unittest.main()
