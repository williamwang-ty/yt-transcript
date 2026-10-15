"""Regression tests for spoken-language detection and subtitle track selection."""

from tests._support import *

from kernel import subtitle_language


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
            "DQGrPstFRVI": ("", None, False, "", "", ""),
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

    def test_list_subs_fallback_drops_translations(self):
        """Test the --list-subs fallback keeps only the -orig language when translations are listed."""
        rows = "\n".join(
            f"{code:<8} Name   vtt, srt"
            for code in ["ab", "af", "zh-Hans", "zh-Hant", "en-orig", "en", "eo", "fr", "de", "ja", "ko"]
        )
        listing = (
            "[youtube] vid: Downloading webpage\n"
            "[info] Available automatic captions for vid:\n"
            "Language Name                  Formats\n"
            f"{rows}\n"
            "vid has no subtitles\n"
        )
        payload = subtitle_language.metadata_from_list_subs(listing, video_id="vid")
        info = subtitle_language.build_subtitle_info(payload, video_id="vid")
        self.assertEqual(info["source_language"], "en")
        self.assertEqual(info["listed_candidates"], [{"language": "en", "mode": "bilingual", "source_kind": "auto"}])
        self.assertEqual(info["ignored_translated_count"], 9)

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
                utils._call_deepgram_api_once(str(audio_path), api_key="key", language="auto")
                self.assertIn("detect_language=true", captured["url"])
                self.assertNotIn("language=auto", captured["url"])
                utils._call_deepgram_api_once(str(audio_path), api_key="key", language="zh")
                self.assertIn("language=zh", captured["url"])
                self.assertNotIn("detect_language", captured["url"])

    def test_transcribe_deepgram_reports_detected_language(self):
        """Test transcribe-deepgram returns the language Deepgram detected across chunks."""
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

            with mock.patch.object(utils, "split_audio", return_value={
                "chunks": [str(audio_path)] * 3,
                "split_points": [10.0, 20.0],
            }), mock.patch.object(utils, "_call_deepgram_api", side_effect=payloads):
                result = utils.transcribe_deepgram(
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
        settings = utils._resolve_deepgram_request_settings({}, language="en")
        self.assertNotIn("detect_language", settings)
