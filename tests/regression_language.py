"""Regression tests for spoken-language detection and subtitle track selection."""

from tests._support import *

from kernel.sources import subtitle_language


REAL_METADATA = json.loads(
    (FIXTURES_DIR / "subtitle_language_metadata.json").read_text(encoding="utf-8")
)["videos"]


def _caption(vid, **query):
    """Build a caption entry whose URL carries the given timedtext query fields."""
    url = f"https://www.youtube.com/api/timedtext?v={vid}"
    for key, value in query.items():
        url += f"&{key}={value}"
    return [{"ext": "vtt", "url": url + "&fmt=vtt"}]


class LanguageRegressionTests(unittest.TestCase):
    """Regression coverage for picking subtitles in the language the video is spoken in."""

    def test_regression_videos_pick_the_expected_source(self):
        """Test the real regression-set metadata routes every video to its expected source."""
        expected = {
            # video id: (source_language, supported, has_any, language, kind, mode)
            "H_peyIigkkc": ("zh", True, True, "zh-Hans", "manual", "chinese"),
            "wu0zS1AuI_E": ("zh", True, True, "zh", "manual", "chinese"),
            "JkMWjp5AXhM": ("zh", True, False, "", "", ""),
            "CIatbV3_Rjg": ("en", True, True, "en", "manual", "bilingual"),
            "ijFSEb2W6_8": ("en", True, True, "en", "auto", "bilingual"),
            "nmgFG7PUHfo": ("en", True, True, "en", "manual", "bilingual"),
            "tGVSOK3PNUI": ("ja", False, False, "", "", ""),
            "DQGrPstFRVI": ("zh", True, False, "", "", ""),
            "Fhx_qNS2P8g": ("zh", True, True, "zh-Hans", "manual", "chinese"),
        }
        self.assertEqual(set(expected), set(REAL_METADATA))
        for vid, (language, supported, has_any, preferred, kind, mode) in expected.items():
            with self.subTest(vid=vid):
                info = subtitle_language.build_subtitle_info(REAL_METADATA[vid])
                self.assertEqual(info["source_language"], language)
                self.assertEqual(info["source_language_supported"], supported)
                self.assertEqual(info["has_any"], has_any)
                self.assertEqual(info["preferred_source_language"], preferred)
                self.assertEqual(info["preferred_source_kind"], kind)
                self.assertEqual(info["mode"], mode)

    def test_auto_translated_tracks_are_never_candidates(self):
        """Test machine-translated zh tracks are ignored for English and Japanese videos."""
        for vid in ("ijFSEb2W6_8", "nmgFG7PUHfo", "tGVSOK3PNUI", "JkMWjp5AXhM"):
            with self.subTest(vid=vid):
                info = subtitle_language.build_subtitle_info(REAL_METADATA[vid])
                languages = [candidate["language"] for candidate in info["listed_candidates"]]
                self.assertNotIn("zh-Hans", languages)
                self.assertNotIn("zh-Hant", languages)
                self.assertGreater(info["ignored_translated_count"], 0)
                self.assertFalse(info["chinese_available"])

    def test_original_audio_track_beats_mislabelled_asr_track(self):
        """Test a Chinese video whose only ASR track is English is not routed to English."""
        info = subtitle_language.build_subtitle_info(REAL_METADATA["JkMWjp5AXhM"])
        self.assertEqual(info["source_language_signal"], "original_audio_track")
        self.assertEqual(info["listed_candidates"], [])
        self.assertEqual(info["unsupported_reason"], "")

    def test_video_text_guesses_the_language_only_when_nothing_else_names_it(self):
        """Test the title/description script guess, on the real regression-set text."""
        # DQGrPstFRVI has no tracks and no language field: its Chinese title and description decide.
        info = subtitle_language.build_subtitle_info(REAL_METADATA["DQGrPstFRVI"])
        self.assertEqual(info["source_language_signal"], "video_text")
        self.assertEqual(info["listed_candidates"], [])
        self.assertEqual(info["unsupported_reason"], "")
        # Every other video keeps its stronger signal.
        for vid, metadata in REAL_METADATA.items():
            if vid != "DQGrPstFRVI":
                with self.subTest(vid=vid):
                    signal = subtitle_language.build_subtitle_info(metadata)["source_language_signal"]
                    self.assertNotIn(signal, subtitle_language.GUESSED_SIGNALS)
        # With the stronger signals removed, the text alone names each video's spoken language,
        # except English, which Latin script cannot tell apart from other languages.
        expected = {"H_peyIigkkc": "zh", "wu0zS1AuI_E": "zh", "JkMWjp5AXhM": "zh", "DQGrPstFRVI": "zh",
                    "Fhx_qNS2P8g": "zh", "tGVSOK3PNUI": "ja", "CIatbV3_Rjg": "", "ijFSEb2W6_8": "",
                    "nmgFG7PUHfo": ""}
        for vid, language in expected.items():
            with self.subTest(vid=vid):
                text_only = {key: REAL_METADATA[vid][key] for key in ("title", "description", "tags", "chapters")}
                detected = subtitle_language.detect_source_language(text_only)
                self.assertEqual(detected["language"], language)
                self.assertEqual(detected["signal"], "video_text" if language else "unknown")

    def test_video_text_reads_the_script_not_the_words(self):
        """Test which text gives a language guess."""
        links = "".join(f"https://www.youtube.com/watch?v=id{n}\n" for n in range(20))
        cases = {
            "오늘의 뉴스 요약": "ko",
            "東京の桜が満開になりました": "ja",
            "小明の日常 第一集": "zh",            # one stylistic kana does not make it Japanese
            "Berita terbaru hari ini": "",       # Latin script: English, Indonesian, ... look alike
            "Elon Musk interview 中文字幕": "",   # too few CJK characters
            "冰河期": "",
        }
        for title, language in cases.items():
            with self.subTest(title=title):
                self.assertEqual(subtitle_language.detect_source_language({"title": title})["language"], language)
        # Links in the description are not words.
        payload = {"title": "2030 冰河期 | 老高與小茉 Mr & Mrs Gao", "description": links}
        self.assertEqual(subtitle_language.detect_source_language(payload)["language"], "zh")
        # Mostly English text with a little Chinese gives no guess.
        payload = {"title": "AI 中文学习", "description": "An English description of this English talk. " * 3}
        self.assertEqual(subtitle_language.detect_source_language(payload)["signal"], "unknown")

    def test_video_text_never_beats_a_track_signal(self):
        """Test a Chinese title does not override the spoken language the ASR track names."""
        payload = {
            "id": "vid",
            "title": "马斯克最新访谈：人工智能将改变一切",
            "automatic_captions": {"en": _caption("vid", lang="en", kind="asr")},
        }
        info = subtitle_language.build_subtitle_info(payload)
        self.assertEqual((info["source_language"], info["source_language_signal"]), ("en", "asr_track"))
        self.assertEqual(info["mode"], "bilingual")

    def test_unsupported_guess_says_it_comes_from_the_title(self):
        """Test a Japanese title without tracks stops as unsupported and says the language was guessed."""
        info = subtitle_language.build_subtitle_info({"id": "vid", "title": "東京の桜が満開になりました"})
        self.assertEqual((info["source_language"], info["source_language_supported"]), ("ja", False))
        self.assertIn("guessed from the video's title and description", info["unsupported_reason"])

    def test_unsupported_language_explains_why(self):
        """Test a Japanese video reports the unsupported spoken language."""
        info = subtitle_language.build_subtitle_info(REAL_METADATA["tGVSOK3PNUI"])
        self.assertIn("'ja' is not supported yet", info["unsupported_reason"])

    def test_manual_chinese_subtitles_win_for_any_spoken_language(self):
        """Test human Chinese subtitles are still preferred on English and Japanese videos."""
        for language in ("en", "ja"):
            with self.subTest(language=language):
                payload = {
                    "id": "vid",
                    "language": language,
                    "subtitles": {"zh-Hans": _caption("vid", lang="zh-Hans")},
                    "automatic_captions": {
                        "zh-Hans": _caption("vid"),
                        language: _caption("vid", lang=language, kind="asr"),
                        f"{language}-orig": _caption("vid", lang=language, kind="asr"),
                    },
                }
                info = subtitle_language.build_subtitle_info(payload)
                self.assertEqual(info["preferred_source_language"], "zh-Hans")
                self.assertEqual(info["preferred_source_kind"], "manual")
                self.assertEqual(info["mode"], "chinese")
                expected_fallbacks = [language] if language == "en" else []
                self.assertEqual(
                    [candidate["language"] for candidate in info["listed_candidates"][1:]],
                    expected_fallbacks,
                )

    def test_chinese_asr_track_in_another_script_counts_as_original(self):
        """Test a zh ASR track served through a zh-Hans tlang is kept as the original track."""
        payload = {
            "id": "vid",
            "language": "zh",
            "automatic_captions": {
                "zh-Hans": _caption("vid", lang="zh", kind="asr", tlang="zh-Hans"),
                "en": _caption("vid", lang="zh", kind="asr", tlang="en"),
            },
        }
        info = subtitle_language.build_subtitle_info(payload)
        self.assertEqual([c["language"] for c in info["listed_candidates"]], ["zh-Hans"])
        self.assertEqual(info["mode"], "chinese")

    def test_unknown_language_falls_back_to_trusted_zh_then_en_tracks(self):
        """Test videos without a language signal still use their manual zh/en tracks."""
        payload = {
            "id": "vid",
            "subtitles": {"en": _caption("vid", lang="en"), "fr": _caption("vid", lang="fr")},
        }
        info = subtitle_language.build_subtitle_info(payload)
        self.assertEqual(info["source_language"], "")
        self.assertIsNone(info["source_language_supported"])
        self.assertEqual(info["preferred_source_language"], "en")
        self.assertEqual(info["mode"], "bilingual")

    def test_subtitle_info_cli_reads_metadata_from_stdin(self):
        """Test the subtitle-info command used by download.sh."""
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "yt_transcript_utils.py"), "subtitle-info",
             "--yt-dlp-runtime-json", '{"auth_strategy": "default"}'],
            input=json.dumps(REAL_METADATA["ijFSEb2W6_8"]),
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["video_id"], "ijFSEb2W6_8")
        self.assertEqual(payload["mode"], "bilingual")
        self.assertEqual(payload["yt_dlp_runtime"], {"auth_strategy": "default"})

    def test_download_subtitle_info_routes_auto_translated_video_to_english(self):
        """Test download.sh subtitle-info no longer picks a machine-translated zh-Hans track."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_path = Path(tmpdir) / "metadata.json"
            metadata_path.write_text(json.dumps(REAL_METADATA["nmgFG7PUHfo"]), encoding="utf-8")
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = \"-J\" ]; then\n"
                f"  cat '{metadata_path}'\n"
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
        self.assertEqual(payload["source_language"], "en")
        self.assertEqual(payload["preferred_source_language"], "en")
        self.assertEqual(payload["preferred_source_kind"], "manual")
        self.assertEqual(payload["mode"], "bilingual")

    def test_deepgram_auto_language_requests_detection(self):
        """Test --language auto asks Deepgram to detect the language instead of forcing one."""
        captured = {}

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            return _Response(b'{"results": {"channels": []}}')

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "audio.mp3"
            audio_path.write_bytes(b"audio")
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                kernel_deepgram._call_deepgram_api_once(str(audio_path), api_key="key", language="auto")
                self.assertIn("detect_language=true", captured["url"])
                self.assertNotIn("language=auto", captured["url"])
                kernel_deepgram._call_deepgram_api_once(str(audio_path), api_key="key", language="zh")
                self.assertIn("language=zh", captured["url"])
                self.assertNotIn("detect_language", captured["url"])

    def test_transcribe_deepgram_reports_detected_language(self):
        """Test the Deepgram transcription returns the language Deepgram detected across chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "source.mp3"
            output_segments = Path(tmpdir) / "segments.json"
            audio_path.write_bytes(b"src")
            base = json.loads((FIXTURES_DIR / "deepgram_en_utterances.json").read_text(encoding="utf-8"))
            payloads = []
            for detected in ("zh", "zh", "en"):
                payload = json.loads(json.dumps(base))
                payload["results"]["channels"][0]["detected_language"] = detected
                payloads.append(payload)

            with mock.patch.object(kernel_deepgram, "split_audio", return_value={
                "chunks": [str(audio_path)] * 3,
                "split_points": [10.0, 20.0],
            }), mock.patch.object(kernel_deepgram, "_call_deepgram_api", side_effect=payloads):
                result = kernel_deepgram.transcribe_deepgram(
                    str(audio_path),
                    "auto",
                    api_key="key",
                    output_segments=str(output_segments),
                )

            self.assertEqual(result["detected_language"], "zh")
            self.assertEqual(result["language"], "zh")
            self.assertTrue(result["deepgram_request"]["detect_language"])
            segments_doc = json.loads(output_segments.read_text(encoding="utf-8"))
            self.assertEqual(segments_doc["language"], "zh")

    def test_transcribe_deepgram_keeps_explicit_language(self):
        """Test an explicit Deepgram language is reported unchanged and does not request detection."""
        settings = kernel_deepgram._resolve_deepgram_request_settings({}, language="en")
        self.assertNotIn("detect_language", settings)
