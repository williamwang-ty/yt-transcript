"""Regression tests for workflow-level state and planning behavior."""

from tests._support import *


class WorkflowRegressionTests(unittest.TestCase):
    """Regression coverage for workflow state, normalization, and planning paths."""
    def test_cleanup_script_removes_state_by_default(self):
        """Test cleanup script removes state by default."""
        video_id = "cleanup_state_test"
        state_file = Path(f"/tmp/{video_id}_state.md")
        machine_state_file = Path(f"/tmp/{video_id}_machine_state.json")
        try:
            state_file.write_text("state", encoding="utf-8")
            machine_state_file.write_text("{}", encoding="utf-8")
            subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/cleanup.sh"), video_id],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse(state_file.exists())
            self.assertFalse(machine_state_file.exists())
        finally:
            state_file.unlink(missing_ok=True)
            machine_state_file.unlink(missing_ok=True)

    def test_cleanup_script_can_keep_state(self):
        """Test cleanup script can keep state."""
        video_id = "cleanup_keep_state_test"
        state_file = Path(f"/tmp/{video_id}_state.md")
        machine_state_file = Path(f"/tmp/{video_id}_machine_state.json")
        try:
            state_file.write_text("state", encoding="utf-8")
            machine_state_file.write_text("{}", encoding="utf-8")
            subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/cleanup.sh"), video_id, "--keep-state"],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(state_file.exists())
            self.assertTrue(machine_state_file.exists())
        finally:
            state_file.unlink(missing_ok=True)
            machine_state_file.unlink(missing_ok=True)

    def test_cleanup_script_rejects_unsafe_video_id(self):
        """Test cleanup script rejects unsafe video id."""
        result = subprocess.run(
            ["bash", str(PROJECT_ROOT / "scripts/cleanup.sh"), "../bad-id"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe characters", result.stderr)

    def test_download_metadata_returns_valid_json(self):
        """Test download metadata returns valid json."""
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
        """Test download metadata fails when video id is missing."""
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

    def test_download_metadata_prefers_single_json_fetch_when_available(self):
        """Test download metadata prefers a single -J fetch when available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "yt-dlp.log"
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                f"""#!/usr/bin/env bash
printf '%s\n' "$*" >> '{log_path}'
for arg in "$@"; do
  if [ "$arg" = "-J" ]; then
    cat <<'EOF'
{{"id":"json123","title":"JSON title","duration":42,"upload_date":"20260324","channel":"JSON channel"}}
EOF
    exit 0
  fi
done
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
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            log_text = log_path.read_text(encoding="utf-8")
            self.assertEqual(payload["video_id"], "json123")
            self.assertEqual(payload["title"], "JSON title")
            self.assertEqual(payload["channel"], "JSON channel")
            self.assertIn("-J", log_text)
            self.assertIn("https://example.com/video", log_text)
            self.assertNotIn("%(title)s", log_text)

    def test_download_metadata_retries_with_chrome_after_not_a_bot(self):
        """Test download metadata retries with chrome after not a bot."""
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
            self.assertEqual(payload["yt_dlp_runtime"]["auth_strategy"], "chrome_retry_session")
            self.assertEqual(payload["yt_dlp_runtime"]["session_browser_cookies"], "chrome")
            self.assertIn("retrying with Chrome cookies", result.stderr)
            self.assertIn("attempt 1/3", result.stderr)

    def test_download_metadata_guides_cookie_file_when_chrome_retry_fails(self):
        """Test download metadata guides cookie file when chrome retry fails."""
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

    def test_subtitle_info_prefers_english_for_bilingual(self):
        """Test subtitle info prefers english for bilingual."""
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
        """Test subtitle info stops routing unsupported languages as chinese."""
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
        """Test subtitle info propagates yt dlp failure."""
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
        """Test subtitle info reads structured metadata when json available."""
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
        """Test subtitles selects manual english source file."""
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
        """Test subtitles selects manual english source file from isolated dir."""
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
        """Test subtitles downloads supported english variant."""
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

    def test_subtitles_continue_when_optional_bilingual_track_fails(self):
        """Test subtitles keep the required source track when optional bilingual debug track fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vidopt","subtitles":{"en":[{"ext":"vtt"}],"zh-Hans":[{"ext":"vtt"}]}}
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
  if [ "$langs" = "en" ]; then
    : > "${out}.en.vtt"
    exit 0
  fi
  if [ "$langs" = "zh-Hans" ]; then
    echo "ERROR: Unable to download video subtitles for 'zh-Hans': HTTP Error 429: Too Many Requests" >&2
    exit 1
  fi
fi
exit 1
""",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            try:
                result = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "subtitles"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["selected_source_vtt"], "/tmp/vidopt_downloads/subtitles/vidopt.en.vtt")
                self.assertEqual(payload["selected_source_language"], "en")
                self.assertEqual(payload["selected_source_kind"], "manual")
                self.assertEqual(payload["chinese_files"], [])
                self.assertEqual(len(payload["warnings"]), 1)
                self.assertIn("continuing with required source track only", payload["warnings"][0])
                self.assertIn("zh-Hans", result.stderr)
            finally:
                shutil.rmtree("/tmp/vidopt_downloads", ignore_errors=True)

    def test_subtitles_rejects_unsupported_languages_before_download(self):
        """Test subtitles rejects unsupported languages before download."""
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
        """Test preflight treats update check as best effort."""
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
        """Test preflight supports gnu stat for cache age."""
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
        """Test audio download uses isolated dir."""
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

    def test_audio_download_can_resolve_video_id_from_metadata_json_only(self):
        """Test audio download can resolve video id from metadata json without --print."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
if [ "$1" = "-J" ]; then
  cat <<'EOF'
{"id":"vidjsononly","formats":[{"format_id":"251","language":"zh","vcodec":"none","acodec":"opus"}]}
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

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            try:
                result = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "audio"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["video_id"], "vidjsononly")
                self.assertEqual(payload["audio_format"], "251")
                self.assertEqual(payload["audio_file"], "/tmp/vidjsononly_downloads/audio/vidjsononly.mp3")
            finally:
                shutil.rmtree("/tmp/vidjsononly_downloads", ignore_errors=True)


    def test_download_metadata_applies_safe_ytdlp_defaults_and_reports_runtime(self):
        """Test download metadata applies safe ytdlp defaults and reports runtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "yt-dlp.log"
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                f"""#!/usr/bin/env bash
printf '%s
' "$*" >> '{log_path}'
print_field=''
while [ $# -gt 0 ]; do
  if [ "$1" = "--print" ] && [ $# -ge 2 ]; then
    print_field="$2"
    shift 2
    continue
  fi
  shift
done
case "$print_field" in
  '%(id)s') echo 'safe123' ;;
  '%(title)s') echo 'Safe title' ;;
  '%(duration)s') echo '42' ;;
  '%(upload_date)s') echo '20260316' ;;
  '%(channel)s') echo 'Safe channel' ;;
  *) exit 1 ;;
esac
""",
                encoding="utf-8",
            )
            fake_bin.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env['PATH']}"
            env["YT_DLP_SOCKET_TIMEOUT_SEC"] = "-1"
            env["YT_DLP_RETRIES"] = "-1"
            env["YT_DLP_EXTRACTOR_RETRIES"] = "-1"
            result = subprocess.run(
                ["bash", str(PROJECT_ROOT / "scripts/download.sh"), "https://example.com/video", "metadata"],
                cwd=PROJECT_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            log_text = log_path.read_text(encoding="utf-8")
            self.assertEqual(payload["yt_dlp_runtime"]["socket_timeout_sec"], 15)
            self.assertEqual(payload["yt_dlp_runtime"]["retries"], 1)
            self.assertEqual(payload["yt_dlp_runtime"]["extractor_retries"], 1)
            self.assertIn("--socket-timeout 15", log_text)
            self.assertIn("--retries 1", log_text)
            self.assertIn("--extractor-retries 1", log_text)

    def test_download_metadata_filters_impersonation_warning(self):
        """Test download metadata filters impersonation warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "yt-dlp"
            fake_bin.write_text(
                """#!/usr/bin/env bash
print_field=''
while [ $# -gt 0 ]; do
  if [ "$1" = "--print" ] && [ $# -ge 2 ]; then
    print_field="$2"
    shift 2
    continue
  fi
  shift
done
echo 'WARNING: [youtube] The extractor specified to use impersonation for this download, but no impersonate target is available' >&2
case "$print_field" in
  '%(id)s') echo 'imp123' ;;
  '%(title)s') echo 'Impersonation title' ;;
  '%(duration)s') echo '12' ;;
  '%(upload_date)s') echo '20260316' ;;
  '%(channel)s') echo 'Channel' ;;
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

            self.assertNotIn("no impersonate target is available", result.stderr)
            self.assertIn("impersonation is unavailable in this build", result.stderr)
