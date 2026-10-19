"""Regression tests for core utility and text-processing behavior."""

from tests._support import *


class CoreRegressionTests(unittest.TestCase):
    """Regression coverage for core utility and chunk-processing behavior."""
    def test_build_api_url_accepts_root_and_v1(self):
        """Test build api url accepts root and v1."""
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
        """Test load config preserves hash inside quotes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                'output_dir: "~/Downloads #1"\n'
                'deepgram_api_key: "dg#key"\n'
                'llm_base_url: "https://api.openai.com/v1"\n',
                encoding="utf-8",
            )
            with mock.patch("sys.stderr", new=io.StringIO()):
                config = utils.load_config(str(config_path))
            self.assertTrue(config["output_dir"].endswith("Downloads #1"))
            self.assertEqual(config["deepgram_api_key"], "dg#key")
            self.assertEqual(config["llm_base_url"], "https://api.openai.com/v1")

    def test_load_config_parses_deepgram_structured_output_flags(self):
        """Test load config parses Deepgram structured-output flags."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'deepgram_model: "nova-3-medical"\n'
                'deepgram_enable_utterances: true\n'
                'deepgram_prefer_structured_output: yes\n',
                encoding="utf-8",
            )

            with mock.patch("sys.stderr", new=io.StringIO()):
                config = utils.load_config(str(config_path))

            self.assertEqual(config["deepgram_model"], "nova-3-medical")
            self.assertTrue(config["deepgram_enable_utterances"])
            self.assertTrue(config["deepgram_prefer_structured_output"])

    def test_load_config_upgrades_legacy_nova2_models_to_nova3(self):
        """Test load config upgrades legacy Nova 2 model names to the Nova 3 default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'deepgram_model: "nova-2-meeting"\n',
                encoding="utf-8",
            )

            with mock.patch("sys.stderr", new=io.StringIO()):
                config = utils.load_config(str(config_path))

            self.assertEqual(utils.DEFAULT_DEEPGRAM_MODEL, "nova-3")
            self.assertEqual(config["deepgram_model"], "nova-3")
            self.assertTrue(any("nova-2-meeting" in warning for warning in config["config_warnings"]))

    def test_resolve_deepgram_request_settings_defaults_to_structured_output(self):
        """Test Deepgram request settings now default to structured output."""
        settings = utils._resolve_deepgram_request_settings({}, language="en")
        legacy_settings = utils._resolve_deepgram_request_settings(
            {
                "deepgram_enable_utterances": False,
                "deepgram_prefer_structured_output": False,
            },
            language="en",
        )

        self.assertEqual(settings["model"], utils.DEFAULT_DEEPGRAM_MODEL)
        self.assertEqual(settings["language"], "en")
        self.assertTrue(settings["utterances"])
        self.assertTrue(settings["prefer_structured_output"])
        self.assertFalse(legacy_settings["utterances"])
        self.assertFalse(legacy_settings["prefer_structured_output"])

    def test_resolve_deepgram_request_settings_blocks_nova2_override(self):
        """Test direct Deepgram Nova 2 overrides are upgraded before requests are built."""
        settings = utils._resolve_deepgram_request_settings(
            {"deepgram_model": "nova-3"},
            language="en",
            model="nova-2-general",
        )

        self.assertEqual(settings["model"], "nova-3")
        self.assertIn("nova-2-general", settings["model_warning"])

    def test_default_config_uses_deepseek_pro_model(self):
        """Test default LLM model favors the more stable DeepSeek pro route."""
        config = utils._default_config_values()

        self.assertEqual(config["deepgram_model"], "nova-3")
        self.assertEqual(config["llm_base_url"], utils.DEFAULT_LLM_BASE_URL)
        self.assertEqual(config["llm_base_url"], "https://api.deepseek.com")
        self.assertEqual(config["llm_model"], utils.DEFAULT_LLM_MODEL)
        self.assertEqual(config["llm_model"], "deepseek-v4-pro")

    def test_split_sentences_handles_chinese_quotes(self):
        """Test split sentences handles chinese quotes."""
        sentences = utils._split_sentences('他说："没问题。"然后离开了。下一句。')
        self.assertEqual(sentences, ['他说："没问题。"', '然后离开了。', '下一句。'])

    def test_split_sentences_handles_mixed_language_text(self):
        """Test split sentences handles mixed language text."""
        sentences = utils._split_sentences("First sentence. 第二句。Third sentence! 最后一句？")
        self.assertEqual(
            sentences,
            ["First sentence.", "第二句。", "Third sentence!", "最后一句？"],
        )

    def test_split_sentences_preserves_decimals(self):
        """Test split sentences preserves decimals."""
        sentences = utils._split_sentences("Version 2.0 is live. Then we ship.")
        self.assertEqual(sentences, ["Version 2.0 is live.", "Then we ship."])

    def test_split_sentences_preserves_acronyms(self):
        """Test split sentences preserves acronyms."""
        sentences = utils._split_sentences("U.S.A. is big. Next sentence.")
        self.assertEqual(sentences, ["U.S.A. is big.", "Next sentence."])

    def test_split_sentences_preserves_honorifics(self):
        """Test split sentences preserves honorifics."""
        sentences = utils._split_sentences("Mr. Smith arrived. He spoke.")
        self.assertEqual(sentences, ["Mr. Smith arrived.", "He spoke."])

    def test_estimate_tokens_heuristic_for_common_text_shapes(self):
        """Test estimate tokens heuristic for common text shapes."""
        self.assertAlmostEqual(utils._estimate_tokens("hello world"), 3, delta=1)
        self.assertAlmostEqual(utils._estimate_tokens("你好世界"), 4, delta=1)
        self.assertAlmostEqual(utils._estimate_tokens("Hello 世界"), 4, delta=1)

    def test_assemble_final_escapes_metadata(self):
        """Test assemble final escapes metadata."""
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
                date="20260307",
                created="2026-03-07",
                transcript_source="YouTube Subtitles",
            )

            content = output.read_text(encoding="utf-8")
            self.assertIn('title: "He said \\"hi\\""', content)
            self.assertIn('url: "https://example.com/watch?v=1"', content)
            self.assertIn('channel: "A \\"B\\""', content)
            self.assertIn('author: "A \\"B\\""', content)
            self.assertIn('date: "2026-03-07"', content)
            self.assertIn('tags: [youtube, video-transcript]', content)
            self.assertIn('via: "youtube"', content)
            self.assertIn("# He said \"hi\"", content)

    def test_process_deepgram_payload_normalizes_chinese_spacing(self):
        """Test process deepgram payload normalizes chinese spacing."""
        payload = {"results": {"channels": [{"alternatives": [{"transcript": "你 好 ！"}]}]}}
        result = utils.process_deepgram_payload(payload)
        self.assertEqual(result["transcript"], "你好！")
        self.assertEqual(result["transcript_source"], "alternative.transcript")
        self.assertFalse(result["has_paragraphs"])
        self.assertFalse(result["has_words"])

        repeated_payload = {"results": {"channels": [{"alternatives": [{"transcript": "哈哈哈哈哈哈"}]}]}}
        repeated_result = utils.process_deepgram_payload(repeated_payload)
        self.assertEqual(repeated_result["transcript"], "哈哈哈")

    def test_process_deepgram_payload_reports_structured_observability_fields(self):
        """Test process deepgram payload reports structured observability fields."""
        payload = json.loads((FIXTURES_DIR / "deepgram_en_structured.json").read_text(encoding="utf-8"))

        result = utils.process_deepgram_payload(payload)

        self.assertEqual(result["transcript"], "Hello world.\n\nWe are testing Deepgram observability.")
        self.assertEqual(result["speaker_count"], 2)
        self.assertEqual(result["transcript_source"], "paragraphs.sentences[].text")
        self.assertEqual(result["paragraph_count"], 2)
        self.assertEqual(result["sentence_count"], 2)
        self.assertEqual(result["sentence_text_count"], 2)
        self.assertEqual(result["timed_sentence_count"], 2)
        self.assertEqual(result["word_count"], 7)
        self.assertEqual(result["utterance_count"], 0)
        self.assertTrue(result["has_paragraphs"])
        self.assertTrue(result["has_words"])
        self.assertFalse(result["has_utterances"])
        self.assertTrue(result["prefer_structured_output"])
        self.assertEqual(
            result["warnings"],
            ["transcript fell back to paragraphs.sentences[].text because no utterances were available"],
        )

    def test_process_deepgram_payload_defaults_to_utterances_but_can_opt_out(self):
        """Test process deepgram payload now defaults to utterances but can still opt out."""
        payload = json.loads((FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"))

        default_result = utils.process_deepgram_payload(payload)
        legacy_result = utils.process_deepgram_payload(payload, prefer_structured_output=False)

        self.assertEqual(default_result["transcript"], "First speaker sentence.\n\nSecond speaker answer.")
        self.assertEqual(default_result["transcript_source"], "utterances")
        self.assertEqual(default_result["utterance_count"], 2)
        self.assertTrue(default_result["has_utterances"])
        self.assertTrue(default_result["prefer_structured_output"])
        self.assertEqual(default_result["warnings"], [])
        self.assertEqual(legacy_result["transcript"], "flat transcript without clear speaker turns")
        self.assertEqual(legacy_result["transcript_source"], "alternative.transcript")
        self.assertFalse(legacy_result["prefer_structured_output"])

    def test_extract_deepgram_segments_preserves_sentence_text_when_words_missing(self):
        """Test extract deepgram segments preserves sentence text when words are missing."""
        payload = json.loads((FIXTURES_DIR / "deepgram_en_sentence_text_only.json").read_text(encoding="utf-8"))

        segments, _ = utils._extract_deepgram_segments_with_report(payload)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "Fallback sentence text should stay punctuated.")
        self.assertEqual(segments[0]["speaker"], 2)

    def test_extract_deepgram_segments_falls_back_to_transcript_without_paragraphs(self):
        """Test extract deepgram segments falls back to transcript without paragraphs."""
        payload = json.loads((FIXTURES_DIR / "deepgram_zh_transcript_only.json").read_text(encoding="utf-8"))

        segments, _ = utils._extract_deepgram_segments_with_report(payload)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "你好！这是一个没有段落数据的例子。")

    def test_extract_deepgram_segments_default_to_utterances_but_can_opt_out(self):
        """Test extract deepgram segments now default to utterances but can still opt out."""
        payload = json.loads((FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"))

        segments, _ = utils._extract_deepgram_segments_with_report(payload)
        legacy_segments, _ = utils._extract_deepgram_segments_with_report(payload, prefer_structured_output=False)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["text"], "First speaker sentence.")
        self.assertEqual(segments[1]["text"], "Second speaker answer.")
        self.assertEqual(segments[0]["speaker"], 0)
        self.assertEqual(segments[1]["speaker"], 1)
        self.assertEqual(len(legacy_segments), 2)
        self.assertEqual(legacy_segments[0]["text"], "First speaker sentence.")
        self.assertEqual(legacy_segments[1]["text"], "Second speaker answer.")

    def test_transcribe_deepgram_merges_chunk_outputs_and_writes_artifacts(self):
        """Test transcribe deepgram merges chunk outputs and writes artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "source.mp3"
            chunk_a = Path(tmpdir) / "chunk_a.mp3"
            chunk_b = Path(tmpdir) / "chunk_b.mp3"
            output_json = Path(tmpdir) / "deepgram.json"
            output_text = Path(tmpdir) / "raw.txt"
            output_segments = Path(tmpdir) / "segments.json"
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
                    output_segments=str(output_segments),
                )

            self.assertEqual(result["transcript"], "Alpha\n\nBeta")
            self.assertEqual(result["speaker_count"], 2)
            self.assertEqual(result["transcript_source"], "alternative.transcript")
            self.assertEqual(output_text.read_text(encoding="utf-8"), "Alpha\n\nBeta")
            self.assertEqual(result["chunk_count"], 2)
            self.assertTrue(result["used_split_mode"])
            self.assertEqual(len(result["json_outputs"]), 2)
            self.assertEqual(result["deepgram_request"]["model"], utils.DEFAULT_DEEPGRAM_MODEL)
            self.assertEqual(result["deepgram_request"]["language"], "en")
            self.assertTrue(result["deepgram_request"]["utterances"])
            self.assertTrue(result["deepgram_request"]["prefer_structured_output"])
            self.assertEqual(result["paragraph_count"], 0)
            self.assertEqual(result["sentence_count"], 0)
            self.assertEqual(result["word_count"], 0)
            self.assertEqual(result["utterance_count"], 0)
            self.assertEqual(len(result["chunk_reports"]), 2)
            self.assertEqual(result["chunk_reports"][0]["transcript_source"], "alternative.transcript")
            self.assertEqual(result["chunk_reports"][0]["segment_source"], "alternative.transcript")
            self.assertEqual(len(result["warnings"]), 2)
            self.assertTrue(output_text.exists())
            self.assertTrue(output_json.exists())
            aggregate = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(aggregate["chunk_count"], 2)
            self.assertEqual(len(aggregate["chunks"]), 2)
            self.assertEqual(aggregate["split_points"], [12.5])
            self.assertEqual(aggregate["deepgram_request"]["model"], utils.DEFAULT_DEEPGRAM_MODEL)
            self.assertEqual(len(aggregate["chunk_reports"]), 2)
            self.assertEqual(len(aggregate["warnings"]), 2)
            self.assertTrue(all(Path(item).exists() for item in result["json_outputs"]))
            self.assertTrue(output_segments.exists())
            segments_doc = json.loads(output_segments.read_text(encoding="utf-8"))
            self.assertEqual(segments_doc["source"], "deepgram")
            self.assertEqual(len(segments_doc["chunk_reports"]), 2)
            self.assertEqual(len(segments_doc["warnings"]), 2)
            self.assertEqual(len(segments_doc["segments"]), 2)
            self.assertEqual(result["segment_count"], 2)
            self.assertEqual(result["segments_output"], str(output_segments))

    def test_transcribe_deepgram_can_resume_from_existing_chunk_jsons(self):
        """Test transcribe deepgram reuses existing chunk payloads when resuming."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "source.mp3"
            chunk_a = Path(tmpdir) / "chunk_a.mp3"
            chunk_b = Path(tmpdir) / "chunk_b.mp3"
            chunk_c = Path(tmpdir) / "chunk_c.mp3"
            output_json = Path(tmpdir) / "deepgram.json"
            output_text = Path(tmpdir) / "raw.txt"
            audio_path.write_bytes(b"src")
            chunk_a.write_bytes(b"a")
            chunk_b.write_bytes(b"b")
            chunk_c.write_bytes(b"c")

            existing_payloads = [
                {"results": {"channels": [{"alternatives": [{"transcript": "first"}]}]}},
                {"results": {"channels": [{"alternatives": [{"transcript": "second"}]}]}},
            ]
            request_settings = utils._resolve_deepgram_request_settings({}, language="en")
            for idx, payload in enumerate(existing_payloads):
                chunk_json = output_json.with_name(f"{output_json.stem}_chunk_{idx:03d}.json")
                chunk_json.write_text(
                    json.dumps(
                        utils._build_deepgram_chunk_cache_envelope(
                            payload,
                            cache_metadata=utils._build_deepgram_chunk_cache_metadata(
                                [chunk_a, chunk_b, chunk_c][idx],
                                chunk_count=3,
                                chunk_index=idx,
                                request_settings=request_settings,
                            ),
                        ),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            payload_c = {"results": {"channels": [{"alternatives": [{"transcript": "third"}]}]}}

            def fake_process(payload, prefer_structured_output=True):
                transcript = payload["results"]["channels"][0]["alternatives"][0]["transcript"]
                return {
                    "transcript": transcript.upper(),
                    "speaker_count": 1,
                    "transcript_source": "alternative.transcript",
                    "prefer_structured_output": prefer_structured_output,
                }

            with mock.patch.object(utils, "split_audio", return_value={
                "chunks": [str(chunk_a), str(chunk_b), str(chunk_c)],
                "split_points": [10.0, 20.0],
            }), mock.patch.object(utils, "_call_deepgram_api", return_value=payload_c) as deepgram_call, mock.patch.object(
                utils,
                "process_deepgram_payload",
                side_effect=fake_process,
            ):
                result = utils.transcribe_deepgram(
                    str(audio_path),
                    "en",
                    api_key="key",
                    output_json=str(output_json),
                    output_text=str(output_text),
                    resume_existing_chunks=True,
                )

            self.assertEqual(deepgram_call.call_count, 1)
            self.assertEqual(result["transcript"], "FIRST\n\nSECOND\n\nTHIRD")
            self.assertEqual([report["reused_existing_payload"] for report in result["chunk_reports"]], [True, True, False])
            self.assertTrue(output_json.exists())
            self.assertEqual(len(result["json_outputs"]), 3)
            aggregate = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(aggregate["chunk_count"], 3)
            self.assertEqual(
                [chunk["payload"]["results"]["channels"][0]["alternatives"][0]["transcript"] for chunk in aggregate["chunks"]],
                ["first", "second", "third"],
            )
            self.assertEqual(output_text.read_text(encoding="utf-8"), "FIRST\n\nSECOND\n\nTHIRD")

    def test_transcribe_deepgram_does_not_reuse_mismatched_chunk_cache(self):
        """Test transcribe deepgram rejects stale chunk-cache envelopes whose metadata no longer matches."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "source.mp3"
            chunk_a = Path(tmpdir) / "chunk_a.mp3"
            output_json = Path(tmpdir) / "deepgram.json"
            audio_path.write_bytes(b"src")
            chunk_a.write_bytes(b"a")

            chunk_json = output_json
            chunk_json.write_text(
                json.dumps(
                    utils._build_deepgram_chunk_cache_envelope(
                        {"results": {"channels": [{"alternatives": [{"transcript": "stale"}]}]}},
                        cache_metadata=utils._build_deepgram_chunk_cache_metadata(
                            chunk_a,
                            chunk_count=1,
                            chunk_index=0,
                            request_settings=utils._resolve_deepgram_request_settings({}, language="zh"),
                        ),
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            fresh_payload = {"results": {"channels": [{"alternatives": [{"transcript": "fresh"}]}]}}

            with mock.patch.object(utils, "split_audio", return_value={
                "chunks": [str(chunk_a)],
                "split_points": [],
            }), mock.patch.object(utils, "_call_deepgram_api", return_value=fresh_payload) as deepgram_call, mock.patch.object(
                utils,
                "process_deepgram_payload",
                return_value={
                    "transcript": "FRESH",
                    "speaker_count": 1,
                    "transcript_source": "alternative.transcript",
                    "prefer_structured_output": True,
                },
            ):
                result = utils.transcribe_deepgram(
                    str(audio_path),
                    "en",
                    api_key="key",
                    output_json=str(output_json),
                    resume_existing_chunks=True,
                )

            self.assertEqual(deepgram_call.call_count, 1)
            self.assertEqual(result["transcript"], "FRESH")
            self.assertEqual([report["reused_existing_payload"] for report in result["chunk_reports"]], [False])

    def test_transcribe_deepgram_defaults_to_structured_output(self):
        """Test transcribe deepgram now defaults to utterances and structured output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "source.mp3"
            output_segments = Path(tmpdir) / "segments.json"
            audio_path.write_bytes(b"src")
            payload = json.loads((FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"))

            with mock.patch.object(utils, "split_audio", return_value={
                "chunks": [str(audio_path)],
                "split_points": [],
            }), mock.patch.object(utils, "_call_deepgram_api", return_value=payload):
                result = utils.transcribe_deepgram(
                    str(audio_path),
                    "en",
                    api_key="key",
                    output_segments=str(output_segments),
                )

            self.assertEqual(result["transcript"], "First speaker sentence.\n\nSecond speaker answer.")
            self.assertEqual(result["transcript_source"], "utterances")
            self.assertTrue(result["deepgram_request"]["utterances"])
            self.assertTrue(result["deepgram_request"]["prefer_structured_output"])
            self.assertEqual(result["utterance_count"], 2)
            self.assertEqual(result["chunk_reports"][0]["transcript_source"], "utterances")
            self.assertEqual(result["chunk_reports"][0]["segment_source"], "utterances")
            self.assertEqual(result["chunk_reports"][0]["utterance_count"], 2)
            self.assertEqual(result["chunk_reports"][0]["utterance_segment_count"], 2)
            segments_doc = json.loads(output_segments.read_text(encoding="utf-8"))
            self.assertTrue(segments_doc["deepgram_request"]["utterances"])
            self.assertEqual(len(segments_doc["segments"]), 2)

    def test_transcribe_deepgram_can_opt_out_of_structured_output_defaults(self):
        """Test transcribe deepgram can still opt out of structured defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "source.mp3"
            output_segments = Path(tmpdir) / "segments.json"
            audio_path.write_bytes(b"src")
            payload = json.loads((FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"))

            with mock.patch.object(utils, "split_audio", return_value={
                "chunks": [str(audio_path)],
                "split_points": [],
            }), mock.patch.object(utils, "_call_deepgram_api", return_value=payload):
                result = utils.transcribe_deepgram(
                    str(audio_path),
                    "en",
                    api_key="key",
                    output_segments=str(output_segments),
                    enable_utterances=False,
                    prefer_structured_output=False,
                )

            self.assertEqual(result["transcript"], "flat transcript without clear speaker turns")
            self.assertEqual(result["transcript_source"], "alternative.transcript")
            self.assertFalse(result["deepgram_request"]["utterances"])
            self.assertFalse(result["deepgram_request"]["prefer_structured_output"])
            self.assertEqual(result["chunk_reports"][0]["transcript_source"], "alternative.transcript")
            segments_doc = json.loads(output_segments.read_text(encoding="utf-8"))
            self.assertFalse(segments_doc["deepgram_request"]["utterances"])

    def test_parse_vtt_segments_extracts_timing_and_dedupes(self):
        """Test parse vtt segments extracts timing and dedupes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vtt_path = Path(tmpdir) / "sub.vtt"
            vtt_path.write_text(
                "WEBVTT\n"
                "Kind: captions\n"
                "Language: en\n"
                "\n"
                "00:00:00.000 --> 00:00:02.000\n"
                "Hello world.\n"
                "\n"
                "00:00:02.000 --> 00:00:04.000\n"
                "Hello world.\n"
                "\n"
                "00:00:04.000 --> 00:00:06.500\n"
                "<c>Second</c> line\n"
                "Third line\n",
                encoding="utf-8",
            )

            result = utils.parse_vtt_segments(str(vtt_path), language="en")

            self.assertEqual(result["source"], "vtt")
            self.assertEqual(result["language"], "en")
            self.assertEqual(result["segment_count"], 2)

            segments = result["segments"]
            self.assertEqual(segments[0]["text"], "Hello world.")
            self.assertEqual(segments[0]["start_time"], 0.0)
            self.assertEqual(segments[0]["end_time"], 4.0)
            self.assertEqual(segments[1]["text"], "Second line Third line")
            self.assertEqual(segments[1]["start_time"], 4.0)
            self.assertEqual(segments[1]["end_time"], 6.5)

    def test_parse_vtt_preserves_cjk_joining_without_invented_spaces(self):
        """Test parse_vtt and parse_vtt_segments keep CJK subtitle fragments tightly joined."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vtt_path = Path(tmpdir) / "sub_zh.vtt"
            vtt_path.write_text(
                "WEBVTT\n"
                "Language: zh-Hans\n"
                "\n"
                "00:00:00.000 --> 00:00:02.000\n"
                "<c>你</c>\n"
                "好，\n"
                "\n"
                "00:00:02.000 --> 00:00:04.000\n"
                "世\n"
                "界！\n",
                encoding="utf-8",
            )

            text_result = utils.parse_vtt(str(vtt_path))
            segments_result = utils.parse_vtt_segments(str(vtt_path), language="zh-Hans")

            self.assertEqual(text_result, "你好，\n世界！")
            self.assertEqual(segments_result["language"], "zh-Hans")
            self.assertEqual(
                [segment["text"] for segment in segments_result["segments"]],
                ["你好，", "世界！"],
            )

    def test_parse_vtt_segments_trims_overlap_and_reports_diagnostics(self):
        """Test adjacent subtitle overlap is trimmed deterministically and reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vtt_path = Path(tmpdir) / "sub_overlap.vtt"
            vtt_path.write_text(
                "WEBVTT\n"
                "Language: zh-Hans\n"
                "\n"
                "00:00:00.000 --> 00:00:02.000\n"
                "大家好今天\n"
                "\n"
                "00:00:02.000 --> 00:00:04.000\n"
                "好今天我们来聊测试\n",
                encoding="utf-8",
            )

            result = utils.parse_vtt_segments(str(vtt_path), language="zh-Hans")

            self.assertEqual(
                [segment["text"] for segment in result["segments"]],
                ["大家好今天", "我们来聊测试"],
            )
            self.assertEqual(result["diagnostics"]["cue_count"], 2)
            self.assertEqual(result["diagnostics"]["overlap_trim_count"], 1)
            self.assertEqual(result["diagnostics"]["overlap_trimmed_chars"], 3)

    def test_assemble_final_escapes_markdown_header_text(self):
        """Test assemble final escapes markdown header text."""
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

    def test_load_normalized_document_recovers_subtitle_text_cleanup(self):
        """Test _load_normalized_document keeps subtitle-derived CJK text normalized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_document = Path(tmpdir) / "normalized_document.json"
            normalized_document.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "format": utils.NORMALIZED_DOCUMENT_FORMAT,
                        "document_id": "vid001",
                        "source_adapter": "segments_json",
                        "source": {
                            "type": "youtube",
                            "subtitle_source": "YouTube Subtitles",
                            "source_language": "zh-Hans",
                        },
                        "content": {
                            "text": "你 好，\n第 二 行。",
                            "preferred_chunk_source": "text",
                        },
                        "segments": [
                            {"id": 0, "text": "你 好，", "start_time": 0.0, "end_time": 1.0},
                            {"id": 1, "text": "第 二 行。", "start_time": 1.0, "end_time": 2.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload = utils._load_normalized_document(str(normalized_document))

            self.assertEqual(payload["content"]["text"], "你好，\n第二行。")
            self.assertEqual(
                [segment["text"] for segment in payload["segments"]],
                ["你好，", "第二行。"],
            )

    def test_each_task_reads_only_its_own_budget_keys(self):
        """structure_only settings no longer stand in for cleanup_zh ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_tokens_structure_only: "777"\n'
                'output_ratio_structure_only: "1.23"\n'
                'max_output_tokens_structure_only: "1999"\n'
                'chunk_tokens_cleanup_zh: "888"\n',
                encoding="utf-8",
            )

            config = utils.load_config(str(config_path))

        self.assertEqual(utils._get_task_chunk_target("structure_only", config), 777)
        self.assertEqual(utils._get_task_output_ratio("structure_only", config), 1.23)
        self.assertEqual(utils._get_task_max_output_tokens("structure_only", config), 1999)
        self.assertEqual(utils._get_task_chunk_target("cleanup_zh", config), 888)
        self.assertEqual(utils._get_task_output_ratio("cleanup_zh", config),
                         utils.TASK_OUTPUT_RATIO_DEFAULTS["cleanup_zh"])
        self.assertEqual(utils._get_task_max_output_tokens("cleanup_zh", config),
                         utils.TASK_MAX_OUTPUT_TOKEN_DEFAULTS["cleanup_zh"])

    def test_load_config_parses_llm_tuning_fields(self):
        """Test load config parses llm tuning fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'llm_timeout_sec: "180"\n'
                'llm_max_retries: "5"\n'
                'llm_backoff_sec: "2.5"\n'
                'llm_stream: "false"\n',
                encoding="utf-8",
            )
            config = utils.load_config(str(config_path))
            self.assertEqual(config["llm_timeout_sec"], 180)
            self.assertEqual(config["llm_max_retries"], 5)
            self.assertEqual(config["llm_backoff_sec"], 2.5)
            self.assertEqual(config["llm_stream"], "false")

    def test_load_config_parses_chunk_tuning_fields(self):
        """Test load config parses chunk tuning fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_size_override: "777"\n'
                'chunk_tokens_structure_only: "1111"\n'
                'chunk_tokens_cleanup_zh: "1001"\n'
                'chunk_tokens_translate_only: "999"\n'
                'chunk_hard_cap_multiplier: "1.5"\n'
                'chunk_glossary_max_prompt_terms: "5"\n'
                'output_ratio_structure_only: "1.2"\n'
                'output_ratio_cleanup_zh: "1.05"\n'
                'output_ratio_translate_only: "1.12"\n'
                'max_output_tokens_structure_only: "1900"\n'
                'max_output_tokens_cleanup_zh: "3200"\n'
                'max_output_tokens_translate_only: "1550"\n',
                encoding="utf-8",
            )

            config = utils.load_config(str(config_path))

            self.assertEqual(config["chunk_size_override"], 777)
            self.assertEqual(config["chunk_tokens_structure_only"], 1111)
            self.assertEqual(config["chunk_tokens_cleanup_zh"], 1001)
            self.assertEqual(config["chunk_tokens_translate_only"], 999)
            self.assertEqual(config["chunk_hard_cap_multiplier"], 1.5)
            self.assertEqual(config["chunk_glossary_max_prompt_terms"], 5)
            self.assertEqual(config["output_ratio_structure_only"], 1.2)
            self.assertEqual(config["output_ratio_cleanup_zh"], 1.05)
            self.assertEqual(config["output_ratio_translate_only"], 1.12)
            self.assertEqual(config["max_output_tokens_structure_only"], 1900)
            self.assertEqual(config["max_output_tokens_cleanup_zh"], 3200)
            self.assertEqual(config["max_output_tokens_translate_only"], 1550)

    def test_load_config_invalid_chunk_values_fall_back(self):
        """Test load config invalid chunk values fall back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_size_override: "oops"\n'
                'chunk_tokens_structure_only: "bad"\n'
                'chunk_hard_cap_multiplier: "10.0"\n'
                'output_ratio_translate_only: "bad"\n'
                'max_output_tokens_translate_only: "0"\n',
                encoding="utf-8",
            )

            with mock.patch('sys.stderr', new_callable=io.StringIO) as fake_stderr:
                config = utils.load_config(str(config_path))

            self.assertEqual(config["chunk_size_override"], 0)
            self.assertEqual(config["chunk_tokens_structure_only"], 1200)
            self.assertEqual(config["chunk_hard_cap_multiplier"], 1.33)
            self.assertEqual(config["output_ratio_translate_only"], 1.10)
            self.assertEqual(config["max_output_tokens_translate_only"], utils.DEFAULT_MAX_OUTPUT_TOKENS)
            self.assertGreaterEqual(len(config["config_warnings"]), 4)
            warning_output = fake_stderr.getvalue()
            self.assertIn("Warning: Invalid numeric config values", warning_output)
            self.assertIn("chunk_size_override='oops'", warning_output)
            self.assertIn("chunk_hard_cap_multiplier='10.0'", warning_output)
            self.assertIn("max_output_tokens_translate_only='0'", warning_output)

    def test_inject_context_block_appends_when_no_anchor(self):
        """Test a context block is appended when the prompt has no input anchor."""
        result = utils._inject_context_block("Prompt header", "## Continuity Context\ncontext")
        self.assertEqual(result, "Prompt header\n\n## Continuity Context\ncontext\n")

    def test_truncate_tail_text_to_tokens_handles_boundary_cases(self):
        """Test truncate tail text to tokens handles boundary cases."""
        self.assertEqual(utils._truncate_tail_text_to_tokens("", 10), "")
        self.assertEqual(utils._truncate_tail_text_to_tokens(" 甲乙 ", 0), "甲乙")
        self.assertEqual(utils._truncate_tail_text_to_tokens("甲乙丙丁", 2), "丙丁")

    def test_call_llm_api_retries_timeout_then_succeeds(self):
        """Test call llm api retries timeout then succeeds."""
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

    def test_execute_llm_request_marks_remote_disconnect_retryable(self):
        """Test execute llm request marks remote disconnect retryable."""
        remote_error = urllib.error.URLError(
            http.client.RemoteDisconnected("Remote end closed connection without response")
        )

        with mock.patch("urllib.request.urlopen", side_effect=remote_error):
            with self.assertRaises(utils.LLMRequestError) as ctx:
                utils._execute_llm_request(
                    api_key="key",
                    base_url="https://api.example.com",
                    model="demo",
                    messages=[{"role": "user", "content": "hello"}],
                )

        self.assertEqual(ctx.exception.error_type, "remote_disconnect")
        self.assertTrue(ctx.exception.retryable)

    def test_call_llm_api_fails_fast_on_http_400(self):
        """Test call llm api fails fast on http 400."""
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

    def test_load_config_coerces_unsafe_yt_dlp_values_to_sane_defaults(self):
        """Test load config coerces unsafe yt dlp values to sane defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'yt_dlp_socket_timeout_sec: "-1"\n'
                'yt_dlp_retries: "-1"\n'
                'yt_dlp_extractor_retries: "-1"\n',
                encoding="utf-8",
            )

            config = utils.load_config(str(config_path))

            self.assertEqual(config["yt_dlp_socket_timeout_sec"], utils.DEFAULT_YT_DLP_SOCKET_TIMEOUT_SEC)
            self.assertEqual(config["yt_dlp_retries"], utils.DEFAULT_YT_DLP_RETRIES)
            self.assertEqual(config["yt_dlp_extractor_retries"], utils.DEFAULT_YT_DLP_EXTRACTOR_RETRIES)
            self.assertTrue(any("yt_dlp_socket_timeout_sec" in warning for warning in config["config_warnings"]))

    def test_call_deepgram_api_retries_transient_timeout_then_succeeds(self):
        """Test call deepgram api retries transient timeout then succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "sample.mp3"
            audio_path.write_bytes(b"audio")
            payload = {"results": {"channels": [{"alternatives": [{"transcript": "ok"}]}]}}

            with mock.patch.object(
                utils,
                "_call_deepgram_api_once",
                side_effect=[TimeoutError("timed out"), payload],
            ) as mocked_call, mock.patch("time.sleep", return_value=None):
                result = utils._call_deepgram_api(
                    str(audio_path),
                    api_key="key",
                    language="en",
                    timeout=3,
                    request_retries=2,
                    retry_backoff_sec=0.01,
                )

            self.assertEqual(result, payload)
            self.assertEqual(mocked_call.call_count, 2)

    def test_call_deepgram_api_once_upgrades_nova2_url_to_nova3(self):
        """Test Deepgram requests never send disabled Nova 2 model names."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "sample.mp3"
            audio_path.write_bytes(b"audio")
            captured = {}

            class FakeResponse:
                """Minimal urlopen context manager."""
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def read(self):
                    return b'{"ok": true}'

            def fake_urlopen(req, timeout=0):
                captured["url"] = req.full_url
                return FakeResponse()

            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch(
                "sys.stderr",
                new=io.StringIO(),
            ):
                result = utils._call_deepgram_api_once(
                    str(audio_path),
                    api_key="key",
                    language="en",
                    model="nova-2-meeting",
                )

            self.assertEqual(result, {"ok": True})
            self.assertIn("model=nova-3", captured["url"])
            self.assertNotIn("nova-2", captured["url"])

    def test_normalize_transcript_text_repairs_conservative_confusable_ascii_tokens(self):
        """Test normalize transcript text repairs conservative confusable ascii tokens."""
        self.assertEqual(utils._normalize_transcript_text("c0ntext 5tack"), "context stack")
        self.assertEqual(utils._normalize_transcript_text("gpt-4o"), "gpt-4o")


if __name__ == "__main__":
    unittest.main()
