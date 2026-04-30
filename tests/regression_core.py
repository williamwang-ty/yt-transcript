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

    def test_build_token_count_url_accepts_root_v1_and_messages(self):
        """Test build token count url accepts root v1 and messages."""
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

    def test_extract_openai_metadata_includes_reasoning_usage(self):
        """Test OpenAI-compatible metadata extraction includes reasoning usage."""
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "",
                        "reasoning_content": "hidden chain",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 1223,
                "total_tokens": 1343,
                "completion_tokens_details": {
                    "reasoning_tokens": 1223,
                },
            },
        }

        self.assertEqual(utils._extract_llm_text(payload, "openai"), "")
        metadata = utils._build_llm_result_metadata(payload, "openai")

        self.assertEqual(metadata["finish_reason"], "stop")
        self.assertEqual(metadata["reasoning_text"], "hidden chain")
        self.assertEqual(metadata["usage"]["reasoning_tokens"], 1223)
        self.assertEqual(metadata["usage"]["content_tokens"], 0)
        self.assertTrue(utils._has_reasoning_metadata(metadata))
        self.assertEqual(utils._reasoning_metadata_reason(metadata), "reasoning_tokens")
        self.assertTrue(
            utils._is_reasoning_budget_exhaustion(
                {
                    "text": "",
                    "finish_reason": "length",
                    "usage": metadata["usage"],
                },
                "",
                1223,
            )
        )
        self.assertFalse(
            utils._is_reasoning_budget_exhaustion(
                {
                    "text": "",
                    "finish_reason": "content_filter",
                    "usage": metadata["usage"],
                },
                "",
                1223,
            )
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
            config = utils.load_config(str(config_path))
            self.assertTrue(config["output_dir"].endswith("Downloads #1"))
            self.assertEqual(config["deepgram_api_key"], "dg#key")
            self.assertEqual(config["llm_base_url"], "https://api.openai.com/v1")

    def test_load_config_parses_deepgram_structured_output_flags(self):
        """Test load config parses Deepgram structured-output flags."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                'output_dir: "~/Downloads"\n'
                'deepgram_model: "nova-2-meeting"\n'
                'deepgram_enable_utterances: true\n'
                'deepgram_prefer_structured_output: yes\n',
                encoding="utf-8",
            )

            config = utils.load_config(str(config_path))

            self.assertEqual(config["deepgram_model"], "nova-2-meeting")
            self.assertTrue(config["deepgram_enable_utterances"])
            self.assertTrue(config["deepgram_prefer_structured_output"])

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

    def test_default_config_uses_deepseek_pro_model(self):
        """Test default LLM model favors the more stable DeepSeek pro route."""
        config = utils._default_config_values()

        self.assertEqual(config["llm_base_url"], utils.DEFAULT_LLM_BASE_URL)
        self.assertEqual(config["llm_base_url"], "https://api.deepseek.com")
        self.assertEqual(config["llm_model"], utils.DEFAULT_LLM_MODEL)
        self.assertEqual(config["llm_model"], "deepseek-v4-pro")

    def test_load_config_defaults_reasoning_probe_enabled(self):
        """Test real config parsing enables unknown-model reasoning probes by default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(f'output_dir: "{tmpdir}"\n', encoding="utf-8")

            config = utils.load_config(str(config_path))

        self.assertTrue(config["llm_reasoning_probe_enabled"])

    def test_reasoning_model_detection_handles_provider_prefixed_names(self):
        """Test provider-prefixed DeepSeek reasoning model aliases disable chunk streaming."""
        reasoning_models = [
            "deepseek-v4-pro",
            "deepseek/deepseek-reasoner",
            "openrouter/deepseek/deepseek-r1",
            "DeepSeek_R1_Distill",
        ]

        for model in reasoning_models:
            with self.subTest(model=model):
                self.assertTrue(utils._is_reasoning_llm_model(model))
                self.assertEqual(utils._resolve_chunk_stream_mode_for_model(model, "auto"), "false")

        self.assertFalse(utils._is_reasoning_llm_model("deepseek-v3"))

    def test_reasoning_metadata_detection_uses_response_features_not_model_name(self):
        """Test structured response features can identify unknown reasoning models."""
        result = {
            "text": "OK",
            "finish_reason": "stop",
            "reasoning_text": "hidden thinking",
            "usage": {
                "completion_tokens": 3,
                "reasoning_tokens": 0,
                "content_tokens": 3,
            },
        }

        self.assertFalse(utils._is_reasoning_llm_model("acme/unknown-model"))
        self.assertTrue(utils._has_reasoning_metadata(result))
        self.assertEqual(utils._reasoning_metadata_reason(result), "reasoning_text")

    def test_chunk_text_splits_chinese_without_spaces(self):
        """Test chunk text splits chinese without spaces."""
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
        self.assertEqual(utils._estimate_tokens("abc", mode="chars"), 3)

    def test_chunk_text_hard_splits_overlong_sentence(self):
        """Test chunk text hard splits overlong sentence."""
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
        """Test chunk text preserves content for deepgram style unpunctuated text."""
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
        """Test chunk text with realistic chinese transcript fixture."""
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

    def test_process_deepgram_can_opt_out_of_structured_defaults(self):
        """Test process_deepgram helper can still force the legacy flat path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "deepgram.json"
            payload_path.write_text(
                (FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            default_result = utils.process_deepgram(str(payload_path))
            legacy_result = utils.process_deepgram(str(payload_path), prefer_structured_output=False)

            self.assertEqual(default_result["transcript_source"], "utterances")
            self.assertEqual(default_result["transcript"], "First speaker sentence.\n\nSecond speaker answer.")
            self.assertEqual(legacy_result["transcript_source"], "alternative.transcript")
            self.assertEqual(legacy_result["transcript"], "flat transcript without clear speaker turns")
            self.assertFalse(legacy_result["prefer_structured_output"])

    def test_process_deepgram_accepts_chunk_cache_envelope(self):
        """Test process_deepgram can unwrap a persisted Deepgram chunk-cache envelope."""
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "deepgram.json"
            raw_payload = json.loads((FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"))
            payload_path.write_text(
                json.dumps(
                    utils._build_deepgram_chunk_cache_envelope(
                        raw_payload,
                        cache_metadata={"chunk_index": 0, "chunk_count": 1},
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = utils.process_deepgram(str(payload_path))

            self.assertEqual(result["transcript_source"], "utterances")
            self.assertEqual(result["transcript"], "First speaker sentence.\n\nSecond speaker answer.")

    def test_extract_deepgram_segments_preserves_sentence_text_when_words_missing(self):
        """Test extract deepgram segments preserves sentence text when words are missing."""
        payload = json.loads((FIXTURES_DIR / "deepgram_en_sentence_text_only.json").read_text(encoding="utf-8"))

        segments = utils.extract_deepgram_segments(payload)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "Fallback sentence text should stay punctuated.")
        self.assertEqual(segments[0]["speaker"], 2)

    def test_extract_deepgram_segments_falls_back_to_transcript_without_paragraphs(self):
        """Test extract deepgram segments falls back to transcript without paragraphs."""
        payload = json.loads((FIXTURES_DIR / "deepgram_zh_transcript_only.json").read_text(encoding="utf-8"))

        segments = utils.extract_deepgram_segments(payload)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "你好！这是一个没有段落数据的例子。")

    def test_extract_deepgram_segments_default_to_utterances_but_can_opt_out(self):
        """Test extract deepgram segments now default to utterances but can still opt out."""
        payload = json.loads((FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"))

        segments = utils.extract_deepgram_segments(payload)
        legacy_segments = utils.extract_deepgram_segments(payload, prefer_structured_output=False)

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

    def test_chunk_segments_writes_timed_manifest(self):
        """Test chunk segments writes timed manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            segments_path = Path(tmpdir) / "segments.json"
            work_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"

            segments_path.write_text(
                json.dumps(
                    {
                        "source": "deepgram",
                        "segments": [
                            {"id": 0, "text": "A" * 30, "start_time": 0.0, "end_time": 10.0},
                            {"id": 1, "text": "B" * 30, "start_time": 10.0, "end_time": 20.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )

            result = utils.chunk_segments(
                str(segments_path),
                str(work_dir),
                chunk_size=35,
                prompt_name="",
                config_path=str(config_path),
            )

            self.assertEqual(result["total_chunks"], 2)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_chunks"], 2)
            self.assertEqual(manifest["source_segments_count"], 2)
            self.assertEqual(manifest["chunks"][0]["start_time"], 0.0)
            self.assertEqual(manifest["chunks"][0]["end_time"], 10.0)
            self.assertEqual(manifest["chunks"][0]["source_segment_start"], 0)
            self.assertEqual(manifest["chunks"][1]["start_time"], 10.0)
            self.assertEqual(manifest["chunks"][1]["end_time"], 20.0)
            self.assertEqual(manifest["chunks"][1]["source_segment_start"], 1)

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

    def test_cli_parse_vtt_segments_command_is_registered(self):
        """Test cli parse vtt segments command is registered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vtt_path = Path(tmpdir) / "sub.vtt"
            vtt_path.write_text(
                "WEBVTT\n"
                "Language: en\n"
                "\n"
                "00:00:00.000 --> 00:00:02.000\n"
                "Hello world.\n"
                "\n"
                "00:00:02.000 --> 00:00:04.000\n"
                "Hello world.\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "yt_transcript_utils.py"),
                    "parse-vtt-segments",
                    str(vtt_path),
                    "--language",
                    "en",
                ],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["source"], "vtt")
            self.assertEqual(payload["language"], "en")
            self.assertEqual(payload["segment_count"], 1)
            self.assertIn("diagnostics", payload)
            self.assertEqual(payload["diagnostics"]["exact_duplicate_cue_count"], 1)
            self.assertEqual(payload["segments"][0]["text"], "Hello world.")
            self.assertEqual(payload["segments"][0]["start_time"], 0.0)
            self.assertEqual(payload["segments"][0]["end_time"], 4.0)

    def test_chunk_segments_can_force_chapter_boundaries(self):
        """Test chunk segments can force chapter boundaries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            segments_path = Path(tmpdir) / "segments.json"
            chapters_path = Path(tmpdir) / "chapters.json"
            work_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"

            segments_path.write_text(
                json.dumps(
                    {
                        "source": "vtt",
                        "segments": [
                            {"id": 0, "text": "A" * 10, "start_time": 0.0, "end_time": 10.0},
                            {"id": 1, "text": "B" * 10, "start_time": 10.0, "end_time": 20.0},
                            {"id": 2, "text": "C" * 10, "start_time": 20.0, "end_time": 30.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            chapters_path.write_text(
                json.dumps(
                    {
                        "chapters": [
                            {"title": "Intro", "start_time": 0.0, "end_time": 20.0},
                            {"title": "Part", "start_time": 20.0, "end_time": 30.0},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )

            result = utils.chunk_segments(
                str(segments_path),
                str(work_dir),
                chunk_size=1000,
                prompt_name="",
                config_path=str(config_path),
                chapters_path=str(chapters_path),
            )

            self.assertEqual(result["total_chunks"], 2)
            chunk0 = (work_dir / "chunk_000.txt").read_text(encoding="utf-8")
            chunk1 = (work_dir / "chunk_001.txt").read_text(encoding="utf-8")
            self.assertIn("A" * 10, chunk0)
            self.assertIn("B" * 10, chunk0)
            self.assertIn("C" * 10, chunk1)

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_chunks"], 2)
            self.assertEqual(manifest["chunks"][0]["start_time"], 0.0)
            self.assertEqual(manifest["chunks"][0]["end_time"], 20.0)
            self.assertEqual(manifest["chunks"][1]["start_time"], 20.0)
            self.assertEqual(manifest["chunks"][1]["end_time"], 30.0)

    def test_chunk_document_prefers_segments_and_writes_formal_contract(self):
        """Test chunk document prefers segments and writes formal contract."""
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_document = Path(tmpdir) / "normalized_document.json"
            work_dir = Path(tmpdir) / "chunks"
            normalized_document.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "format": utils.NORMALIZED_DOCUMENT_FORMAT,
                        "document_id": "vid001",
                        "source_adapter": "segments_json",
                        "artifacts": {
                            "segments_path": "/tmp/vid001_segments.json",
                            "normalized_document": str(normalized_document),
                        },
                        "content": {
                            "text": "First line\nSecond line",
                            "preferred_chunk_source": "segments",
                        },
                        "segments": [
                            {"id": 0, "text": "First line", "start_time": 0.0, "end_time": 1.0},
                            {"id": 1, "text": "Second line", "start_time": 1.0, "end_time": 2.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = utils.chunk_document(str(normalized_document), str(work_dir), chunk_size=1000)

            self.assertEqual(result["driver"], "chunk-document")
            self.assertEqual(result["source_kind"], "segments")
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["plan"]["chunk_contract"]["driver"], "chunk-document")
            self.assertEqual(manifest["plan"]["chunk_contract"]["source_kind"], "segments")
            self.assertEqual(manifest["plan"]["chunk_contract"]["boundary_mode"], "strict")
            self.assertEqual(manifest["plan"]["continuity"]["mode"], "reference_only")
            self.assertEqual(
                manifest["plan"]["chunk_contract"]["normalized_document_path"],
                str(normalized_document.absolute()),
            )
            self.assertEqual(manifest["chunks"][0]["source_kind"], "segments")
            self.assertEqual(manifest["chunks"][0]["boundary_mode"], "strict")
            self.assertEqual(manifest["chunks"][0]["start_time"], 0.0)

    def test_chunk_document_can_force_text_mode(self):
        """Test chunk document can force text mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_document = Path(tmpdir) / "normalized_document.json"
            work_dir = Path(tmpdir) / "chunks"
            normalized_document.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "format": utils.NORMALIZED_DOCUMENT_FORMAT,
                        "document_id": "vid001",
                        "source_adapter": "segments_json",
                        "content": {
                            "text": "Alpha. Beta. Gamma.",
                            "preferred_chunk_source": "segments",
                        },
                        "segments": [
                            {"id": 0, "text": "Alpha", "start_time": 0.0, "end_time": 1.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = utils.chunk_document(str(normalized_document), str(work_dir), chunk_size=1000, prefer="text")

            self.assertEqual(result["source_kind"], "text")
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["plan"]["chunk_contract"]["source_kind"], "text")
            self.assertNotIn("start_time", manifest["chunks"][0])

    def test_chunk_text_manifest_initializes_control_state(self):
        """Test chunk text manifest initializes control state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。第三句。第四句。", encoding="utf-8")

            utils.chunk_text(str(source), str(work_dir), 4, "structure_only")

            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 5)
            self.assertEqual(manifest["runtime"]["control"]["repair_attempted_count"], 0)
            self.assertEqual(manifest["runtime"]["operation_control"], {})
            self.assertEqual(manifest["chunks"][0]["control"]["verification_status"], "pending")
            self.assertFalse(manifest["chunks"][0]["control"]["repair_exhausted"])

    def test_build_chapter_plan_maps_chapters_to_timed_chunks(self):
        """Test build chapter plan maps chapters to timed chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            segments_path = Path(tmpdir) / "segments.json"
            work_dir = Path(tmpdir) / "chunks"
            chapters_path = Path(tmpdir) / "chapters.json"
            output_path = Path(tmpdir) / "chapter_plan.json"
            config_path = Path(tmpdir) / "config.yaml"

            segments_path.write_text(
                json.dumps(
                    {
                        "source": "deepgram",
                        "segments": [
                            {"id": 0, "text": "A" * 30, "start_time": 0.0, "end_time": 10.0},
                            {"id": 1, "text": "B" * 30, "start_time": 10.0, "end_time": 20.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )
            utils.chunk_segments(
                str(segments_path),
                str(work_dir),
                chunk_size=35,
                prompt_name="",
                config_path=str(config_path),
            )

            chapters_path.write_text(
                json.dumps(
                    {
                        "chapters": [
                            {"title": "Intro", "start_time": 0.0, "end_time": 10.0},
                            {"title": "Topic", "start_time": 10.0, "end_time": 20.0},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = utils.build_chapter_plan(str(chapters_path), str(work_dir), str(output_path))
            self.assertTrue(result["success"])
            self.assertTrue(output_path.exists())

            plan = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(plan[0]["start_chunk"], 0)
            self.assertEqual(plan[1]["start_chunk"], 1)
            self.assertEqual(plan[1]["anchor_segment_id"], 1)

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

    def test_validate_state_is_stage_aware(self):
        """Test validate state is stage aware."""
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
        """Test validate state accepts final stage when output file present."""
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

    def test_validate_state_rejects_compact_date_in_output_filename(self):
        """Test final output filenames require yyyy-mm-dd date fragments."""
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
                "output_file: /tmp/out/20260308. sample.md\n",
                encoding="utf-8",
            )

            result = utils.validate_state(str(state), stage="final")

            self.assertFalse(result["passed"])
            self.assertEqual(result["checks"]["output_filename_date_failures"], [
                "Output filename date must use yyyy-mm-dd, not yyyymmdd (20260308 -> 2026-03-08)."
            ])
            self.assertTrue(any("yyyy-mm-dd" in failure for failure in result["hard_failures"]))

    def test_validate_state_materializes_machine_state_from_legacy_markdown(self):
        """Test validate state materializes machine state from legacy markdown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
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

            result = utils.validate_state(str(state), stage="post-source")

            self.assertTrue(result["passed"], result)
            machine_state = Path(result["machine_state_path"])
            self.assertTrue(machine_state.exists())
            payload = json.loads(machine_state.read_text(encoding="utf-8"))
            self.assertEqual(payload["document_id"], "vid001")
            self.assertEqual(payload["compat_projection"]["fields"]["title"], "Sample")

    def test_plan_optimization_accepts_machine_state_json_input(self):
        """Test plan optimization accepts machine state json input."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
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
            sync_result = utils.sync_machine_state(str(state))

            result = utils.plan_optimization(sync_result["machine_state_path"])

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["video_path"], "long")
            self.assertEqual(result["machine_state_path"], sync_result["machine_state_path"])

    def test_sync_machine_state_can_write_legacy_projection_from_json(self):
        """Test sync machine state can write legacy projection from json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 120\n"
                "output_dir: /tmp/out\n",
                encoding="utf-8",
            )
            sync_result = utils.sync_machine_state(str(state))
            state.unlink()

            rewrite_result = utils.sync_machine_state(sync_result["machine_state_path"], write_legacy=True)

            self.assertTrue(Path(rewrite_result["legacy_state_path"]).exists())
            content = Path(rewrite_result["legacy_state_path"]).read_text(encoding="utf-8")
            self.assertIn("vid: vid001", content)

    def test_normalize_document_materializes_from_raw_text_artifact(self):
        """Test normalize document materializes from raw text artifact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
            raw_text = Path(tmpdir) / "vid001_raw.txt"
            raw_text.write_text("Line one.\r\n\r\nLine two.  \n", encoding="utf-8")
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 120\n"
                "output_dir: /tmp/out\n"
                f"raw_text: {raw_text}\n",
                encoding="utf-8",
            )

            result = utils.normalize_document(str(state))

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["materialized"])
            payload = json.loads(Path(result["normalized_document_path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["source_adapter"], "raw_text_file")
            self.assertEqual(payload["content"]["text"], "Line one.\n\nLine two.")

    def test_normalize_document_prefers_segments_artifact_when_available(self):
        """Test normalize document prefers segments artifact when available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
            raw_text = Path(tmpdir) / "vid001_raw.txt"
            segments = Path(tmpdir) / "vid001_segments.json"
            raw_text.write_text("fallback raw text", encoding="utf-8")
            segments.write_text(
                json.dumps({
                    "source": "vtt",
                    "language": "en",
                    "segments": [
                        {"id": 0, "text": " First line ", "start_time": 0.0, "end_time": 1.0},
                        {"id": 1, "text": "Second line", "start_time": 1.0, "end_time": 2.0},
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 120\n"
                "output_dir: /tmp/out\n"
                f"raw_text: {raw_text}\n"
                f"segments_path: {segments}\n",
                encoding="utf-8",
            )

            result = utils.normalize_document(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["source_adapter"], "segments_json")
            self.assertEqual(result["segment_count"], 2)
            payload = json.loads(Path(result["normalized_document_path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["content"]["preferred_chunk_source"], "segments")
            self.assertEqual(payload["content"]["text"], "First line\nSecond line")
            machine_payload = json.loads(Path(result["machine_state_path"]).read_text(encoding="utf-8"))
            self.assertEqual(machine_payload["normalization"]["source_adapter"], "segments_json")

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

    def test_normalize_document_prefers_text_for_chinese_youtube_subtitles(self):
        """Test chinese YouTube subtitle documents keep segments but prefer text chunking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
            raw_text = Path(tmpdir) / "vid001_raw.txt"
            segments = Path(tmpdir) / "vid001_segments.json"
            work_dir = Path(tmpdir) / "chunks"
            raw_text.write_text("你 好，\n\n第 二 行。", encoding="utf-8")
            segments.write_text(
                json.dumps(
                    {
                        "source": "vtt",
                        "language": "zh-Hans",
                        "diagnostics": {
                            "cue_count": 2,
                            "overlap_trim_count": 1,
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
                "src: youtube\n"
                "source_language: zh-Hans\n"
                "subtitle_source: YouTube Subtitles\n"
                f"raw_text: {raw_text}\n"
                f"segments_path: {segments}\n"
                f"work_dir: {work_dir}\n",
                encoding="utf-8",
            )

            result = utils.normalize_document(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["source_adapter"], "segments_json")
            self.assertEqual(result["preferred_chunk_source"], "text")
            self.assertEqual(result["cleanup_diagnostics"]["overlap_trim_count"], 1)
            payload = json.loads(Path(result["normalized_document_path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["content"]["preferred_chunk_source"], "text")
            self.assertEqual(payload["content"]["text"], "你好，\n\n第二行。")
            self.assertEqual(payload["diagnostics"]["subtitle_cleanup"]["overlap_trim_count"], 1)
            self.assertIn("subtitle_quality", payload["diagnostics"])
            self.assertTrue(payload["diagnostics"]["subtitle_quality"]["applicable"])
            self.assertGreaterEqual(payload["diagnostics"]["subtitle_quality"]["subtitle_quality_score"], 0.75)
            self.assertEqual(
                [segment["text"] for segment in payload["segments"]],
                ["你好，", "第二行。"],
            )

            chunk_result = utils.chunk_document(str(result["normalized_document_path"]), str(work_dir), chunk_size=1000)

            self.assertEqual(chunk_result["source_kind"], "text")
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["plan"]["chunk_contract"]["source_kind"], "text")
            self.assertNotIn("start_time", manifest["chunks"][0])

    def test_plan_optimization_materializes_normalized_document_when_artifact_exists(self):
        """Test plan optimization materializes normalized document when artifact exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
            raw_text = Path(tmpdir) / "vid001_raw.txt"
            raw_text.write_text("Hello world", encoding="utf-8")
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
                f"raw_text: {raw_text}\n"
                "work_dir: /tmp/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["normalization"]["materialized"])
            self.assertTrue(Path(result["normalization"]["normalized_document_path"]).exists())
            self.assertEqual(result["outputs"]["normalized_document"], result["normalization"]["normalized_document_path"])

    def test_plan_optimization_reports_chunk_document_contract(self):
        """Test plan optimization reports chunk document contract."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "vid001_state.md"
            raw_text = Path(tmpdir) / "vid001_raw.txt"
            raw_text.write_text("Hello world", encoding="utf-8")
            state.write_text(
                f"""# State
vid: vid001
url: https://example.com/watch?v=1
title: Sample
channel: Channel
upload_date: 20260308
duration: 3600
output_dir: /tmp/out
mode: bilingual
src: youtube
source_language: en
subtitle_source: YouTube Subtitles
raw_text: {raw_text}
work_dir: /tmp/vid001_chunks
""",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["chunking"]["driver"], "chunk-document")
            self.assertEqual(result["chunking"]["preferred_source_kind"], "text")
            self.assertEqual(result["chunking"]["boundary_mode"], "strict")
            self.assertEqual(result["chunking"]["continuity_mode"], "reference_only")

    def test_plan_optimization_returns_short_bilingual_operations(self):
        """Test plan optimization returns short bilingual operations."""
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

    def test_plan_optimization_routes_short_chinese_youtube_to_cleanup_prompt(self):
        """Test plan optimization routes Chinese subtitle text through cleanup_zh."""
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
                "mode: chinese\n"
                "src: youtube\n"
                "source_language: zh-Hans\n"
                "subtitle_source: YouTube Subtitles\n"
                "work_dir: /tmp/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["video_path"], "short")
            self.assertEqual([op["prompt"] for op in result["operations"]], ["cleanup_zh"])
            self.assertEqual(result["operations"][0]["execution"]["mode"], "single_pass")
            self.assertEqual(result["operations"][0]["extra_instruction"], "")
            self.assertEqual(result["source_route_reason"], "subtitle_quality_pending")
            self.assertFalse(result["reroute_recommended"])

    def test_plan_optimization_recommends_deepgram_for_critical_chinese_subtitles(self):
        """Test plan optimization recommends Deepgram when subtitle quality is critically poor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state.md"
            raw_text = Path(tmpdir) / "vid001_raw.txt"
            segments = Path(tmpdir) / "vid001_segments.json"
            raw_text.write_text(
                "\n".join([
                    "我 们",
                    "先 看",
                    "这 里",
                    "再 说",
                    "下 一",
                    "步 呢",
                    "你 看",
                    "对 吧",
                    "现 在",
                    "继 续",
                    "往 下",
                    "讲 吧",
                ]),
                encoding="utf-8",
            )
            segments.write_text(
                json.dumps(
                    {
                        "source": "vtt",
                        "language": "zh-Hans",
                        "diagnostics": {
                            "cue_count": 20,
                            "empty_cue_count": 3,
                            "exact_duplicate_cue_count": 5,
                            "overlap_trim_count": 5,
                            "overlap_collapsed_cue_count": 2,
                            "markup_tag_count": 8,
                            "nbsp_entity_count": 4,
                            "collapsed_cjk_spacing_count": 12,
                            "tightened_punctuation_spacing_count": 6,
                            "tightened_bracket_spacing_count": 2,
                        },
                        "segments": [
                            {"id": idx, "text": f"片段 {idx}", "start_time": float(idx), "end_time": float(idx + 1)}
                            for idx in range(20)
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 120\n"
                "output_dir: /tmp/out\n"
                "mode: chinese\n"
                "src: youtube\n"
                "source_language: zh-Hans\n"
                "subtitle_source: YouTube Subtitles\n"
                f"raw_text: {raw_text}\n"
                f"segments_path: {segments}\n"
                "work_dir: /tmp/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["reroute_recommended"])
            self.assertEqual(result["reroute_target"], "deepgram")
            self.assertEqual(result["source_route_reason"], "subtitle_quality_critical_deepgram_recommended")
            self.assertEqual(result["checks"]["source_route_reason"], "subtitle_quality_critical_deepgram_recommended")
            self.assertEqual(result["subtitle_quality_band"], "critical")
            self.assertLess(result["subtitle_quality_score"], 0.35)
            self.assertGreater(result["checks"]["overlap_reduction_count"], 0)
            self.assertEqual(result["normalization"]["subtitle_quality"]["subtitle_quality_band"], "critical")
            self.assertTrue(any("Deepgram before text optimization is recommended" in warning for warning in result["warnings"]))

    def test_plan_optimization_keeps_bilingual_subtitles_on_manual_review_for_critical_quality(self):
        """Test poor bilingual subtitle quality warns without auto-rerouting to Deepgram."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state.md"
            raw_text = Path(tmpdir) / "vid001_raw.txt"
            segments = Path(tmpdir) / "vid001_segments.json"
            raw_text.write_text(
                "\n".join([f"segment {idx}" for idx in range(12)]),
                encoding="utf-8",
            )
            segments.write_text(
                json.dumps(
                    {
                        "source": "vtt",
                        "language": "en",
                        "diagnostics": {
                            "cue_count": 20,
                            "empty_cue_count": 3,
                            "exact_duplicate_cue_count": 5,
                            "overlap_trim_count": 5,
                            "overlap_collapsed_cue_count": 2,
                            "markup_tag_count": 8,
                            "nbsp_entity_count": 4,
                            "collapsed_cjk_spacing_count": 12,
                            "tightened_punctuation_spacing_count": 6,
                            "tightened_bracket_spacing_count": 2,
                        },
                        "segments": [
                            {"id": idx, "text": f"segment {idx}", "start_time": float(idx), "end_time": float(idx + 1)}
                            for idx in range(20)
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
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
                f"raw_text: {raw_text}\n"
                f"segments_path: {segments}\n"
                "work_dir: /tmp/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertFalse(result["reroute_recommended"])
            self.assertEqual(result["reroute_target"], "")
            self.assertEqual(result["source_route_reason"], "subtitle_quality_critical_manual_review_only")
            self.assertEqual(result["subtitle_quality_band"], "critical")
            self.assertLess(result["subtitle_quality_score"], 0.35)
            self.assertTrue(any("continue only with manual review" in warning for warning in result["warnings"]))

    def test_plan_optimization_returns_long_deepgram_operations(self):
        """Test plan optimization returns long deepgram operations."""
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
            self.assertEqual(result["operations"][0]["prompt"], "cleanup_zh")
            self.assertIn("Deepgram ASR", result["operations"][0]["extra_instruction"])
            self.assertTrue(result["operations"][0]["execution"]["supports_auto_replan"])
            self.assertEqual(result["operations"][0]["execution"]["recommended_cli_flags"], ["--auto-replan"])
            self.assertEqual(result["operations"][0]["execution"]["on_replan_required"], "auto_replan_remaining")
            self.assertTrue(result["replan_contract"]["raw_path"]["supports_auto_replan"])
            self.assertEqual(result["operations"][0]["control"]["repair"]["mode"], "bounded_retry")
            self.assertEqual(result["operations"][0]["control"]["replan"]["on_replan_required"], "auto_replan_remaining")
            self.assertEqual(result["quality_contract"]["stop_rule"], "hard_failures_stop")

    def test_cleanup_zh_reuses_structure_only_chunk_tuning(self):
        """Test cleanup_zh uses dedicated chunk sizing and budgets."""
        override_config = {
            "chunk_size_override": 0,
            "chunk_tokens_structure_only": 777,
            "output_ratio_structure_only": 1.23,
            "max_output_tokens_structure_only": 1999,
            "chunk_tokens_cleanup_zh": 888,
            "output_ratio_cleanup_zh": 1.05,
            "max_output_tokens_cleanup_zh": 3200,
        }

        self.assertEqual(utils._get_task_chunk_target("cleanup_zh", override_config), 888)
        self.assertEqual(utils._get_task_output_ratio("cleanup_zh", override_config), 1.05)
        self.assertEqual(utils._get_task_max_output_tokens("cleanup_zh", override_config), 3200)

    def test_cleanup_zh_falls_back_to_legacy_structure_only_tuning(self):
        """Test older cleanup_zh configs still inherit structure_only tuning."""
        legacy_config = {
            "chunk_size_override": 0,
            "chunk_tokens_structure_only": 777,
            "output_ratio_structure_only": 1.23,
            "max_output_tokens_structure_only": 1999,
        }

        self.assertEqual(utils._get_task_chunk_target("cleanup_zh", legacy_config), 777)
        self.assertEqual(utils._get_task_output_ratio("cleanup_zh", legacy_config), 1.23)
        self.assertEqual(utils._get_task_max_output_tokens("cleanup_zh", legacy_config), 1999)

    def test_load_config_cleanup_zh_falls_back_to_legacy_structure_only_tuning(self):
        """Test real config parsing preserves cleanup_zh legacy tuning fallback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_tokens_structure_only: "777"\n'
                'output_ratio_structure_only: "1.23"\n'
                'max_output_tokens_structure_only: "1999"\n',
                encoding="utf-8",
            )

            config = utils.load_config(str(config_path))

            self.assertEqual(config["chunk_tokens_cleanup_zh"], 777)
            self.assertEqual(config["output_ratio_cleanup_zh"], 1.23)
            self.assertEqual(config["max_output_tokens_cleanup_zh"], 1999)
            self.assertEqual(utils._get_task_chunk_target("cleanup_zh", config), 777)
            self.assertEqual(utils._get_task_output_ratio("cleanup_zh", config), 1.23)
            self.assertEqual(utils._get_task_max_output_tokens("cleanup_zh", config), 1999)

    def test_plan_optimization_marks_processed_path_chunk_stage_for_manual_review(self):
        """Test plan optimization marks processed path chunk stage for manual review."""
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
            self.assertFalse(result["operations"][1]["control"]["replan"]["supports_auto_replan"])
            self.assertEqual(result["operations"][1]["control"]["quality_gate"]["hard_failure_checks"][-1]["id"], "bilingual_pairs")

    def test_quality_gate_contract_exposes_advisory_readability_metrics(self):
        """Test quality gate contract exposes advisory readability metrics."""
        contract = utils._build_quality_gate_contract(bilingual=False)

        warning_ids = [entry["id"] for entry in contract["warning_checks"]]

        self.assertIn("chunk_seam_duplication", warning_ids)
        self.assertIn("header_fragment_balance", warning_ids)
        self.assertIn("cjk_spacing_anomaly", warning_ids)
        self.assertIn("fragment_paragraph_ratio", warning_ids)
        self.assertIn("duplicate_ngram_ratio", warning_ids)
        self.assertIn("punctuation_density", warning_ids)
        self.assertIn("glossary_drift", warning_ids)

    def test_cli_api_envelope_wraps_plan_optimization(self):
        """Test cli api envelope wraps plan optimization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state.md"
            work_dir = Path(tmpdir) / "vid001_chunks"
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
                f"work_dir: {work_dir}\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "python3",
                    str(PROJECT_ROOT / "yt_transcript_utils.py"),
                    "--api-envelope",
                    "plan-optimization",
                    str(state),
                ],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            telemetry_path = Path(payload["telemetry"]["telemetry_path"])
            self.assertEqual(payload["format"], utils.COMMAND_RESULT_FORMAT)
            self.assertEqual(payload["command"], "plan-optimization")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["result"]["passed"])
            self.assertTrue(telemetry_path.exists())
            event = json.loads(telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["command"], "plan-optimization")
            self.assertEqual(event["trace_id"], payload["trace_id"])

    def test_load_config_parses_llm_tuning_fields(self):
        """Test load config parses llm tuning fields."""
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
                'llm_reasoning_probe_enabled: "false"\n'
                'llm_stop_after_consecutive_timeouts: "4"\n'
                'llm_reasoning_retry_attempts: "3"\n'
                'llm_reasoning_retry_multiplier: "1.5"\n'
                'llm_reasoning_retry_max_tokens: "5000"\n',
                encoding="utf-8",
            )
            config = utils.load_config(str(config_path))
            self.assertEqual(config["llm_timeout_sec"], 180)
            self.assertEqual(config["llm_max_retries"], 5)
            self.assertEqual(config["llm_backoff_sec"], 2.5)
            self.assertEqual(config["llm_stream"], "false")
            self.assertEqual(config["llm_probe_timeout_sec"], 9)
            self.assertEqual(config["llm_probe_max_tokens"], 7)
            self.assertFalse(config["llm_reasoning_probe_enabled"])
            self.assertEqual(config["llm_stop_after_consecutive_timeouts"], 4)
            self.assertEqual(config["llm_reasoning_retry_attempts"], 3)
            self.assertEqual(config["llm_reasoning_retry_multiplier"], 1.5)
            self.assertEqual(config["llm_reasoning_retry_max_tokens"], 5000)

    def test_load_config_parses_chunk_tuning_fields(self):
        """Test load config parses chunk tuning fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n'
                'chunk_size_override: "777"\n'
                'chunk_tokens_structure_only: "1111"\n'
                'chunk_tokens_quick_cleanup: "888"\n'
                'chunk_tokens_cleanup_zh: "1001"\n'
                'chunk_tokens_translate_only: "999"\n'
                'chunk_tokens_summarize: "2222"\n'
                'chunk_hard_cap_multiplier: "1.5"\n'
                'chunk_safety_buffer_tokens: "321"\n'
                'chunk_overlap_sentences: "0"\n'
                'chunk_context_tail_sentences: "2"\n'
                'chunk_context_summary_tokens: "70"\n'
                'output_ratio_structure_only: "1.2"\n'
                'output_ratio_quick_cleanup: "1.08"\n'
                'output_ratio_cleanup_zh: "1.05"\n'
                'output_ratio_translate_only: "1.12"\n'
                'output_ratio_summarize: "0.2"\n'
                'max_output_tokens_structure_only: "1900"\n'
                'max_output_tokens_quick_cleanup: "1450"\n'
                'max_output_tokens_cleanup_zh: "3200"\n'
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
            self.assertEqual(config["chunk_tokens_cleanup_zh"], 1001)
            self.assertEqual(config["chunk_tokens_translate_only"], 999)
            self.assertEqual(config["chunk_tokens_summarize"], 2222)
            self.assertEqual(config["chunk_hard_cap_multiplier"], 1.5)
            self.assertEqual(config["chunk_safety_buffer_tokens"], 321)
            self.assertEqual(config["chunk_context_tail_sentences"], 2)
            self.assertEqual(config["chunk_context_summary_tokens"], 70)
            self.assertEqual(config["output_ratio_structure_only"], 1.2)
            self.assertEqual(config["output_ratio_quick_cleanup"], 1.08)
            self.assertEqual(config["output_ratio_cleanup_zh"], 1.05)
            self.assertEqual(config["output_ratio_translate_only"], 1.12)
            self.assertEqual(config["output_ratio_summarize"], 0.2)
            self.assertEqual(config["max_output_tokens_structure_only"], 1900)
            self.assertEqual(config["max_output_tokens_quick_cleanup"], 1450)
            self.assertEqual(config["max_output_tokens_cleanup_zh"], 3200)
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
        """Test load config invalid chunk values fall back."""
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
        """Test chunk text uses token aware default budget."""
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
        """Test chunk text chars mode preserves legacy prompt default."""
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
        """Test chunk text varies by prompt in token mode."""
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
        """Test chunk text explicit size without prompt uses legacy char sizing."""
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
        """Test chunk text missing explicit config path fails fast."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")

            with self.assertRaises(SystemExit):
                utils.chunk_text(str(source), str(out_dir), 0, "structure_only", "/no/such/config.yaml")

    def test_chunk_text_unknown_prompt_fails_fast(self):
        """Test chunk text unknown prompt fails fast."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")

            with self.assertRaises(SystemExit):
                utils.chunk_text(str(source), str(out_dir), 0, "not_a_prompt")

    def test_chunk_text_rejects_prompt_path_traversal(self):
        """Test chunk text rejects prompt path traversal."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            out_dir = Path(tmpdir) / "chunks"
            source.write_text("第一句。第二句。", encoding="utf-8")

            with self.assertRaises(SystemExit):
                utils.chunk_text(str(source), str(out_dir), 0, "../CODE_REVIEW")

    def test_calculate_chunk_budget_varies_by_prompt_and_override(self):
        """Test calculate chunk budget varies by prompt and override."""
        default_config = utils._default_config_values()
        structure_prompt = (PROJECT_ROOT / "prompts" / "structure_only.md").read_text(encoding="utf-8")
        summary_prompt = (PROJECT_ROOT / "prompts" / "summarize.md").read_text(encoding="utf-8")

        structure_budget = utils._calculate_chunk_budget("structure_only", structure_prompt, default_config)
        summary_budget = utils._calculate_chunk_budget("summarize", summary_prompt, default_config)

        self.assertLess(structure_budget["target_tokens"], summary_budget["target_tokens"])
        self.assertLessEqual(structure_budget["planned_max_output_tokens"], 1800)
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
        """Test calculate chunk budget reserves continuity context."""
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
        """Test inject continuity context appends when no anchor."""
        result = utils._inject_continuity_context("Prompt header", "## Continuity Context\ncontext")
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

    def test_merge_content_supports_multiple_chapters_per_chunk(self):
        """Test merge content supports multiple chapters per chunk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text("正文", encoding="utf-8")
            (work_dir / "chapter_plan.json").write_text(
                json.dumps(
                    [
                        {"start_chunk": 0, "title_en": "A", "title_zh": "甲"},
                        {"start_chunk": 0, "title_en": "B", "title_zh": "乙"},
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertEqual(merge_result["chapters_inserted"], 2)
            self.assertIn("## A", merged_text)
            self.assertIn("## 甲", merged_text)
            self.assertIn("## B", merged_text)
            self.assertIn("## 乙", merged_text)

    def test_merge_content_trims_chunk_seam_overlap(self):
        """Test merge content removes repeated seam text after chunk concat."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                            {"id": 1, "processed_path": "processed_001.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text("第一句。\n这是重复句。", encoding="utf-8")
            (work_dir / "processed_001.md").write_text("这是重复句。\n第二句。", encoding="utf-8")

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertTrue(merge_result["post_merge_cleanup_applied"])
            self.assertEqual(merge_result["cleanup_diagnostics"]["seam_overlap_trim_count"], 1)
            self.assertEqual(merged_text.count("这是重复句。"), 1)
            self.assertNotIn("yt-transcript-chunk-seam", merged_text)

    def test_merge_content_merges_short_incomplete_fragments(self):
        """Test merge content rejoins short body fragments split across chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                            {"id": 1, "processed_path": "processed_001.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text("这是一个被拆开的段落", encoding="utf-8")
            (work_dir / "processed_001.md").write_text("后半句，补完。", encoding="utf-8")

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertEqual(merge_result["cleanup_diagnostics"]["short_paragraph_merge_count"], 1)
            self.assertIn("这是一个被拆开的段落\n后半句，补完。", merged_text)
            self.assertNotIn("这是一个被拆开的段落\n\n后半句，补完。", merged_text)

    def test_merge_content_preserves_unrelated_short_chinese_paragraphs(self):
        """Test merge content keeps standalone short Chinese paragraphs separated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                            {"id": 1, "processed_path": "processed_001.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text("短句", encoding="utf-8")
            (work_dir / "processed_001.md").write_text("第二段。", encoding="utf-8")

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertEqual(merge_result["cleanup_diagnostics"]["short_paragraph_merge_count"], 0)
            self.assertIn("短句\n\n第二段。", merged_text)

    def test_merge_content_preserves_short_english_heading_like_paragraphs(self):
        """Test merge content keeps short English heading-like body paragraphs separated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                            {"id": 1, "processed_path": "processed_001.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text("Background and Context", encoding="utf-8")
            (work_dir / "processed_001.md").write_text("Next paragraph.", encoding="utf-8")

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertEqual(merge_result["cleanup_diagnostics"]["short_paragraph_merge_count"], 0)
            self.assertIn("Background and Context\n\nNext paragraph.", merged_text)

    def test_merge_content_preserves_markdown_list_blocks(self):
        """Test merge content does not collapse markdown list blocks into following body text."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                            {"id": 1, "processed_path": "processed_001.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text("- item", encoding="utf-8")
            (work_dir / "processed_001.md").write_text("continued", encoding="utf-8")

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertEqual(merge_result["cleanup_diagnostics"]["short_paragraph_merge_count"], 0)
            self.assertIn("- item\n\ncontinued", merged_text)

    def test_merge_content_preserves_complex_header_prefix_while_cleaning_body(self):
        """Test merge content keeps explicit header prefixes untouched while cleaning the merge body."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                            {"id": 1, "processed_path": "processed_001.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text(
                "```md\nalpha\n---\nbeta\n```\n\n这是重复句。",
                encoding="utf-8",
            )
            (work_dir / "processed_001.md").write_text("这是重复句。\n第二句。", encoding="utf-8")

            header = (
                "---\n"
                "title: \"Sample\"\n"
                "source: \"https://example.com\"\n"
                "---\n\n"
                "# Sample\n\n"
                "> Source: Example\n\n"
                "---"
            )

            merge_result = utils.merge_content(str(work_dir), str(output_file), header_content=header)
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertTrue(merged_text.startswith(header + "\n\n"))
            self.assertIn("```md\nalpha\n---\nbeta\n```", merged_text)
            self.assertEqual(merged_text.count("这是重复句。"), 1)
            self.assertEqual(merge_result["cleanup_diagnostics"]["seam_overlap_trim_count"], 1)

    def test_merge_content_dedupes_chunk_heading_after_chapter_injection(self):
        """Test merge content removes heading lines duplicated by chapter injection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            output_file = Path(tmpdir) / "merged.md"
            work_dir.mkdir(parents=True, exist_ok=True)

            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "chunks": [
                            {"id": 0, "processed_path": "processed_000.md", "status": "done"},
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (work_dir / "processed_000.md").write_text("## Intro\n\n正文", encoding="utf-8")
            (work_dir / "chapter_plan.json").write_text(
                json.dumps(
                    [
                        {"start_chunk": 0, "title_en": "Intro"},
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            merge_result = utils.merge_content(str(work_dir), str(output_file))
            merged_text = output_file.read_text(encoding="utf-8")

            self.assertTrue(merge_result["success"])
            self.assertEqual(merge_result["chapters_inserted"], 1)
            self.assertEqual(merge_result["cleanup_diagnostics"]["heading_line_dedup_count"], 1)
            self.assertEqual(merged_text.count("## Intro"), 1)
            self.assertIn("正文", merged_text)

    def test_test_llm_api_returns_probe_metadata(self):
        """Test test llm api returns probe metadata."""
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
        """Test test llm api can run from explicit overrides without config."""
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
        """Test count tokens via provider uses anthropic endpoint."""
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
            """Minimal fake HTTP response used by regression tests."""
            def __enter__(self):
                """Enter."""
                return self

            def __exit__(self, exc_type, exc, tb):
                """Exit."""
                return False

            def read(self):
                """Read."""
                return b'{"input_tokens": 7}'

        def fake_urlopen(req, timeout=0):
            """Fake urlopen."""
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
        """Test count tokens via provider http error falls back cleanly."""
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
        """Test count tokens via provider network error falls back cleanly."""
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
        """Test count tokens via provider timeout falls back cleanly."""
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
        """Test test token count falls back to local estimate."""
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
        """Test test token count can run from explicit overrides without config."""
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

    def test_chunk_segments_merges_tiny_chapter_boundary_fragment_forward(self):
        """Test chunk segments merges tiny chapter boundary fragment forward."""
        with tempfile.TemporaryDirectory() as tmpdir:
            segments_path = Path(tmpdir) / "segments.json"
            chapters_path = Path(tmpdir) / "chapters.json"
            work_dir = Path(tmpdir) / "chunks"
            config_path = Path(tmpdir) / "config.yaml"

            segments_path.write_text(
                json.dumps(
                    {
                        "source": "vtt",
                        "segments": [
                            {"id": 0, "text": "A" * 10, "start_time": 0.0, "end_time": 10.0},
                            {"id": 1, "text": "B" * 10, "start_time": 10.0, "end_time": 20.0},
                            {"id": 2, "text": "C" * 3, "start_time": 20.0, "end_time": 23.0},
                            {"id": 3, "text": "D" * 24, "start_time": 23.0, "end_time": 40.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            chapters_path.write_text(
                json.dumps(
                    {"chapters": [{"title": "Part Two", "start_time": 20.0, "end_time": 40.0}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )

            result = utils.chunk_segments(
                str(segments_path),
                str(work_dir),
                chunk_size=25,
                prompt_name="",
                config_path=str(config_path),
                chapters_path=str(chapters_path),
            )

            self.assertEqual(result["total_chunks"], 2)
            self.assertTrue(any("Merged 1 undersized chunk fragment" in warning for warning in result["warnings"]))
            chunk1 = (work_dir / "chunk_001.txt").read_text(encoding="utf-8")
            self.assertIn("C" * 3, chunk1)
            self.assertIn("D" * 24, chunk1)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["chunks"][1]["start_time"], 20.0)
            self.assertEqual(manifest["chunks"][1]["source_segment_start"], 2)

    def test_build_chapter_plan_tolerates_near_boundary_start_without_warning(self):
        """Test build chapter plan tolerates near boundary start without warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            segments_path = Path(tmpdir) / "segments.json"
            work_dir = Path(tmpdir) / "chunks"
            chapters_path = Path(tmpdir) / "chapters.json"
            output_path = Path(tmpdir) / "chapter_plan.json"
            config_path = Path(tmpdir) / "config.yaml"

            segments_path.write_text(
                json.dumps(
                    {
                        "source": "deepgram",
                        "segments": [
                            {"id": 0, "text": "A" * 30, "start_time": 0.0, "end_time": 10.0},
                            {"id": 1, "text": "B" * 30, "start_time": 10.0, "end_time": 20.0},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                f'output_dir: "{tmpdir}"\n'
                'chunk_mode: "chars"\n',
                encoding="utf-8",
            )
            utils.chunk_segments(
                str(segments_path),
                str(work_dir),
                chunk_size=35,
                prompt_name="",
                config_path=str(config_path),
            )
            chapters_path.write_text(
                json.dumps(
                    {
                        "chapters": [
                            {"title": "Intro", "start_time": 0.0, "end_time": 9.95},
                            {"title": "Topic", "start_time": 9.95, "end_time": 20.0},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = utils.build_chapter_plan(str(chapters_path), str(work_dir), str(output_path))
            plan = json.loads(output_path.read_text(encoding="utf-8"))

            self.assertTrue(result["success"])
            self.assertFalse(any("fallback strategy" in warning for warning in result["warnings"]))
            self.assertEqual(plan[1]["start_chunk"], 1)
            self.assertEqual(plan[1]["match_strategy"], "near_next_start")
            self.assertIn("boundary_tolerance_sec", plan[1]["mapping_diagnostics"])

    def test_plan_optimization_escalates_oversized_short_input_to_chunked_path(self):
        """Test plan optimization escalates oversized short input to chunked path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path = Path(tmpdir) / "raw.txt"
            state = Path(tmpdir) / "state.md"
            raw_path.write_text(("word " * 5000).strip(), encoding="utf-8")
            state.write_text(
                "# State\n"
                "vid: vid001\n"
                "url: https://example.com/watch?v=1\n"
                "title: Sample\n"
                "channel: Channel\n"
                "upload_date: 20260308\n"
                "duration: 1200\n"
                f"output_dir: {tmpdir}\n"
                "mode: bilingual\n"
                "src: youtube\n"
                "source_language: en\n"
                "subtitle_source: YouTube Subtitles\n"
                f"raw_text: {raw_path}\n"
                f"work_dir: {tmpdir}/vid001_chunks\n",
                encoding="utf-8",
            )

            result = utils.plan_optimization(str(state))

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["duration_bucket"], "short")
            self.assertEqual(result["video_path"], "long")
            self.assertEqual(result["routing_reason"], "oversized_short_input")
            self.assertTrue(result["requires_llm_preflight"])
            self.assertTrue(all(op["kind"] == "chunk" for op in result["operations"]))
            self.assertGreater(result["estimated_input_tokens"], result["single_pass_token_limit"])

    def test_normalize_transcript_text_repairs_conservative_confusable_ascii_tokens(self):
        """Test normalize transcript text repairs conservative confusable ascii tokens."""
        self.assertEqual(utils._normalize_transcript_text("c0ntext 5tack"), "context stack")
        self.assertEqual(utils._normalize_transcript_text("gpt-4o"), "gpt-4o")


if __name__ == "__main__":
    unittest.main()
