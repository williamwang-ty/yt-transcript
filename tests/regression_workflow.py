"""Regression tests for the shell scripts: download.sh and the yt-dlp update check."""

from tests._support import *

# A yt-dlp stand-in driven by spec.json next to it; every call is logged to calls.jsonl.
FAKE_YT_DLP = r'''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
spec = json.loads((here / "spec.json").read_text(encoding="utf-8"))
args = sys.argv[1:]
with open(here / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")


def value(name):
    return args[args.index(name) + 1] if name in args else ""


if "--list-extractors" in args:
    if not spec.get("preset", True):
        print("yt-dlp: error: no such option: -t", file=sys.stderr)
        sys.exit(2)
    print("youtube")
    sys.exit(0)
if "--version" in args:
    print(spec.get("version", "2026.10.21"))
    sys.exit(0)
for line in spec.get("stderr", []):
    print(line, file=sys.stderr)
if "-J" in args:
    if spec.get("j_error"):
        print(spec["j_error"], file=sys.stderr)
        sys.exit(1)
    print(json.dumps(spec["info"]))
    sys.exit(0)
if "--write-sub" in args:
    lang = value("--sub-lang")
    answer = spec.get("subs", {}).get(lang)
    if answer != "ok":
        print(answer or f"ERROR: Unable to download video subtitles for '{lang}': HTTP Error 404: Not Found",
              file=sys.stderr)
        sys.exit(1)
    target = Path(value("-o") + f".{lang}.vtt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("WEBVTT\n", encoding="utf-8")
    print(f"[info] Writing video subtitles to: {target}")
    sys.exit(0)
if "-f" in args:
    target = Path(value("-o").replace("%(ext)s", "m4a"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio")
    sys.exit(0)
sys.exit(3)
'''

URL = "https://www.youtube.com/watch?v=vid001"


class DownloadScriptTests(unittest.TestCase):
    """download.sh metadata, subtitle-info, subtitles and audio modes, and update_yt_dlp.sh."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yt-download-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        fake = self.bin / "yt-dlp"
        fake.write_text(FAKE_YT_DLP, encoding="utf-8")
        fake.chmod(0o755)
        self.info_file = self.dir / "metadata.json"
        self.env = os.environ.copy()
        self.env.update({
            "PATH": f"{self.bin}{os.pathsep}{self.env['PATH']}",
            "YT_TRANSCRIPT_INFO_JSON": str(self.info_file),
            "YT_TRANSCRIPT_DOWNLOAD_DIR": str(self.dir / "downloads"),
        })
        for var in ("YT_DLP_SOCKET_TIMEOUT_SEC", "YT_DLP_RETRIES", "YT_DLP_EXTRACTOR_RETRIES",
                    "YT_DLP_COOKIES_FROM_BROWSER", "YT_DLP_COOKIES_FILE", "YT_TRANSCRIPT_SUBTITLE_MODE"):
            self.env.pop(var, None)

    def spec(self, **spec) -> None:
        (self.bin / "spec.json").write_text(json.dumps(spec), encoding="utf-8")

    def download(self, mode: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(["bash", str(PROJECT_ROOT / "scripts/download.sh"), URL, mode], cwd=PROJECT_ROOT,
                                env=self.env, capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(0, result.returncode, result.stderr)
        return result

    def calls(self) -> list:
        """yt-dlp calls apart from the `-t sleep` probe."""
        path = self.bin / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        return [args for args in calls if "--list-extractors" not in args and "--version" not in args]

    # -- metadata -------------------------------------------------------------

    def test_metadata_fetches_the_page_once(self):
        self.spec(info={"id": "vid001", "title": 'He said "hi"', "duration": 42.0, "upload_date": "20260307",
                        "channel": 'A "B"'})
        payload = json.loads(self.download("metadata").stdout)
        self.assertEqual(("vid001", 'He said "hi"', 42, "20260307", 'A "B"'),
                         (payload["video_id"], payload["title"], payload["duration"], payload["upload_date"],
                          payload["channel"]))
        self.assertEqual(1, len(self.calls()))
        self.assertIn("-J", self.calls()[0])
        self.assertEqual("vid001", json.loads(self.info_file.read_text(encoding="utf-8"))["id"])

    def test_metadata_failure_passes_yt_dlp_errors_on_without_asking_again(self):
        error = ("ERROR: [youtube] vid001: This content isn't available, try again later. The current session "
                 "has been rate-limited by YouTube for up to an hour.")
        self.spec(j_error=error)
        result = self.download("metadata", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(error, result.stderr)
        # One request: no field-by-field fallbacks and no cookie retries in the script.
        self.assertEqual(1, len(self.calls()))
        self.assertFalse(self.info_file.exists() and self.info_file.read_text(encoding="utf-8"))

    def test_metadata_fails_when_the_video_id_is_missing(self):
        self.spec(info={"title": "no id"})
        result = self.download("metadata", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Could not resolve a video ID", result.stderr)

    def test_saved_metadata_is_used_without_a_request(self):
        self.info_file.write_text(json.dumps({"id": "vid001", "title": "Saved"}), encoding="utf-8")
        self.spec(info={"id": "vid001", "title": "Fetched"})
        self.assertEqual("Saved", json.loads(self.download("metadata").stdout)["title"])
        self.assertEqual([], self.calls())

    def test_metadata_applies_safe_defaults_and_the_sleep_preset(self):
        self.spec(info={"id": "vid001"})
        self.env.update({"YT_DLP_SOCKET_TIMEOUT_SEC": "-1", "YT_DLP_RETRIES": "-1", "YT_DLP_EXTRACTOR_RETRIES": "x"})
        payload = json.loads(self.download("metadata").stdout)
        self.assertEqual({"socket_timeout_sec": 15, "retries": 1, "extractor_retries": 1, "auth_strategy": "anonymous",
                          "cookies_file": "", "cookies_from_browser": "", "sleep_preset": True},
                         payload["yt_dlp_runtime"])
        args = " ".join(self.calls()[0])
        for expected in ("--socket-timeout 15", "--retries 1", "--extractor-retries 1", "-t sleep"):
            self.assertIn(expected, args)

    def test_yt_dlp_without_the_sleep_preset_runs_without_it(self):
        self.spec(info={"id": "vid001"}, preset=False, version="2025.03.31")
        result = self.download("metadata")
        self.assertFalse(json.loads(result.stdout)["yt_dlp_runtime"]["sleep_preset"])
        self.assertNotIn("-t", self.calls()[0])
        self.assertIn("This yt-dlp (2025.03.31) has no '-t sleep' preset", result.stderr)

    def test_configured_cookies_reach_yt_dlp(self):
        self.spec(info={"id": "vid001"})
        self.env["YT_DLP_COOKIES_FROM_BROWSER"] = "firefox"
        payload = json.loads(self.download("metadata").stdout)
        self.assertEqual("browser_cookies", payload["yt_dlp_runtime"]["auth_strategy"])
        self.assertIn("--cookies-from-browser firefox", " ".join(self.calls()[0]))

    def test_impersonation_warning_is_filtered(self):
        self.spec(info={"id": "vid001"}, stderr=[
            "WARNING: [youtube] The extractor specified to use impersonation for this download, but no "
            "impersonate target is available"])
        result = self.download("metadata")
        self.assertNotIn("no impersonate target is available", result.stderr)
        self.assertIn("impersonation is unavailable in this build", result.stderr)

    # -- subtitle-info and subtitles ------------------------------------------

    def test_subtitle_info_prefers_human_chinese(self):
        self.spec(info={"id": "vid001", "subtitles": {"en": [{"ext": "vtt"}], "zh-Hans": [{"ext": "vtt"}]},
                        "automatic_captions": {"en-US": [{"ext": "vtt"}]}})
        payload = json.loads(self.download("subtitle-info").stdout)
        self.assertEqual(("zh-Hans", "manual", "chinese"),
                         (payload["preferred_source_language"], payload["preferred_source_kind"], payload["mode"]))
        self.assertEqual([{"language": "zh-Hans", "mode": "chinese", "source_kind": "manual"},
                          {"language": "en", "mode": "bilingual", "source_kind": "manual"},
                          {"language": "en-US", "mode": "bilingual", "source_kind": "auto"}],
                         payload["listed_candidates"])

    def test_subtitle_info_marks_unsupported_languages_as_no_usable_subtitles(self):
        self.spec(info={"id": "vid001", "subtitles": {"es": [{"ext": "vtt"}]}})
        payload = json.loads(self.download("subtitle-info").stdout)
        self.assertFalse(payload["has_any"])
        self.assertEqual(("", "", ""),
                         (payload["preferred_source_language"], payload["preferred_source_kind"], payload["mode"]))

    def test_subtitles_download_from_the_saved_metadata(self):
        self.spec(info={"id": "vid001", "subtitles": {"en": [{"ext": "vtt"}], "zh-Hans": [{"ext": "vtt"}]}},
                  subs={"zh-Hans": "ok", "en": "ok"})
        payload = json.loads(self.download("subtitles").stdout)
        subtitle_dir = self.dir / "downloads" / "subtitles"
        self.assertEqual(str(subtitle_dir / "vid001.zh-Hans.vtt"), payload["selected_source_vtt"])
        self.assertEqual(("zh-Hans", "manual", "chinese", False),
                         (payload["selected_source_language"], payload["selected_source_kind"],
                          payload["resolved_mode"], payload["fallback_used"]))
        self.assertEqual([{"language": "zh-Hans", "mode": "chinese", "source_kind": "manual", "status": "selected"}],
                         payload["attempted_candidates"])
        # One page fetch (-J); the subtitle comes from the saved metadata, not the URL.
        first, second = self.calls()
        self.assertIn("-J", first)
        self.assertEqual(str(self.info_file), second[second.index("--load-info-json") + 1])
        self.assertNotIn(URL, second)

    def test_a_refused_track_reports_yt_dlp_error_and_the_next_one_is_tried(self):
        error = "ERROR: Unable to download video subtitles for 'zh-Hans': HTTP Error 429: Too Many Requests"
        self.spec(info={"id": "vid001", "subtitles": {"en": [{"ext": "vtt"}], "zh-Hans": [{"ext": "vtt"}]}},
                  subs={"zh-Hans": error, "en": "ok"})
        result = self.download("subtitles")
        payload = json.loads(result.stdout)
        self.assertEqual(("en", "bilingual", True),
                         (payload["selected_source_language"], payload["resolved_mode"], payload["fallback_used"]))
        self.assertEqual([{"language": "zh-Hans", "mode": "chinese", "source_kind": "manual", "status": "blocked",
                           "error": error}], payload["blocked_candidates"])
        self.assertEqual(["zh-Hans", "en"], [c["language"] for c in payload["attempted_candidates"]])
        self.assertEqual(1, len(payload["warnings"]))
        self.assertIn(error, result.stderr)

    def test_subtitles_fail_when_no_track_downloads(self):
        self.spec(info={"id": "vid001", "subtitles": {"en": [{"ext": "vtt"}]}})
        result = self.download("subtitles", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("HTTP Error 404", result.stderr)
        self.assertIn("No usable English or Chinese subtitle track could be downloaded", result.stderr)

    def test_subtitles_download_a_regional_english_variant(self):
        self.spec(info={"id": "vid001", "subtitles": {"en-GB": [{"ext": "vtt"}]}}, subs={"en-GB": "ok"})
        payload = json.loads(self.download("subtitles").stdout)
        self.assertEqual(("en-GB", "manual"), (payload["selected_source_language"], payload["selected_source_kind"]))

    def test_subtitles_reject_unsupported_languages_before_download(self):
        self.spec(info={"id": "vid001", "subtitles": {"es": [{"ext": "vtt"}]}}, subs={"es": "ok"})
        result = self.download("subtitles", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Spoken language 'es' is not supported yet", result.stderr)
        self.assertFalse(any("--write-sub" in args for args in self.calls()))

    def test_subtitle_mode_limits_the_candidates(self):
        self.spec(info={"id": "vid001", "subtitles": {"en": [{"ext": "vtt"}], "zh-Hans": [{"ext": "vtt"}]}},
                  subs={"zh-Hans": "ok", "en": "ok"})
        self.env["YT_TRANSCRIPT_SUBTITLE_MODE"] = "bilingual"
        payload = json.loads(self.download("subtitles").stdout)
        self.assertEqual(("en", "bilingual"), (payload["selected_source_language"], payload["resolved_mode"]))

    # -- audio ----------------------------------------------------------------

    def test_audio_picks_a_chinese_audio_only_format_and_downloads_by_url(self):
        self.spec(info={"id": "vid001", "formats": [
            {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a"},
            {"format_id": "251", "language": "zh", "vcodec": "none", "acodec": "opus"},
            {"format_id": "140", "language": "en", "vcodec": "none", "acodec": "mp4a.40.2"}]})
        payload = json.loads(self.download("audio").stdout)
        self.assertEqual(str(self.dir / "downloads" / "audio" / "vid001.m4a"), payload["audio_file"])
        self.assertEqual("251", payload["audio_format"])
        self.assertTrue(Path(payload["audio_file"]).is_file())
        download = self.calls()[-1]
        self.assertEqual("251", download[download.index("-f") + 1])
        self.assertIn(URL, download)

    def test_audio_falls_back_to_bestaudio(self):
        self.spec(info={"id": "vid001", "formats": []})
        self.assertEqual("bestaudio", json.loads(self.download("audio").stdout)["audio_format"])

    # -- update_yt_dlp.sh -----------------------------------------------------

    def run_update(self, brew_script: str | None) -> subprocess.CompletedProcess:
        if brew_script is not None:
            brew = self.bin / "brew"
            brew.write_text("#!/usr/bin/env bash\n"
                            f"printf '%s\\n' \"$*\" >> '{self.dir / 'brew.log'}'\n" + brew_script, encoding="utf-8")
            brew.chmod(0o755)
        for name in ("pip", "pip3"):
            trap = self.bin / name
            trap.write_text(f"#!/usr/bin/env bash\necho pip >> '{self.dir / 'pip.log'}'\n", encoding="utf-8")
            trap.chmod(0o755)
        env = dict(self.env, YT_TRANSCRIPT_LOCK_DIR=str(self.dir / "locks"))
        env.pop("YT_TRANSCRIPT_YT_DLP_UPDATE", None)
        env["PATH"] = f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
        result = subprocess.run(["bash", str(PROJECT_ROOT / "scripts/update_yt_dlp.sh")], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.dir / "pip.log").exists())
        return result

    def brew_calls(self) -> list:
        log = self.dir / "brew.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def test_update_script_does_nothing_when_disabled(self):
        result = subprocess.run(["bash", str(PROJECT_ROOT / "scripts/update_yt_dlp.sh")], capture_output=True,
                                text=True, env={**self.env, "YT_TRANSCRIPT_YT_DLP_UPDATE": "0"})
        self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))

    def test_update_asks_homebrew_at_most_hourly(self):
        self.spec(info={})
        # Homebrew's prefix is the folder that holds bin/yt-dlp.
        result = self.run_update(f'if [ "$1" = "--prefix" ]; then echo "{self.dir}"; exit 0; fi\nexit 0\n')
        self.assertIn("Asking Homebrew", result.stdout)
        self.assertIn("is the newest Homebrew has", result.stdout)
        self.assertEqual(["--prefix", "upgrade yt-dlp"], self.brew_calls())
        self.assertTrue((self.dir / "locks" / "yt-dlp-update.stamp").is_file())
        # Within the hour nothing runs, whatever the first result was.
        self.assertEqual("", self.run_update(None).stdout)
        self.assertEqual(["--prefix", "upgrade yt-dlp"], self.brew_calls())

    def test_failed_brew_upgrade_is_reported_and_has_no_pip_fallback(self):
        self.spec(info={})
        result = self.run_update(f'if [ "$1" = "--prefix" ]; then echo "{self.dir}"; exit 0; fi\n'
                                 'echo "Error: Another active Homebrew process is already in progress." >&2\nexit 1\n')
        self.assertIn("brew upgrade yt-dlp failed", result.stdout)
        self.assertIn("Another active Homebrew process", result.stdout)

    def test_yt_dlp_outside_homebrew_is_only_reported(self):
        self.spec(info={})
        result = self.run_update('if [ "$1" = "--prefix" ]; then echo /opt/homebrew; exit 0; fi\nexit 0\n')
        self.assertIn("is not managed by Homebrew", result.stdout)
        self.assertEqual(["--prefix"], self.brew_calls())
