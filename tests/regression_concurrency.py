"""Concurrent calls of `run`: the video lock, the machine-wide YouTube lock and the refusal rule.

kernel/locks.py is checked with real processes racing for one lock file. The
pipeline tests run `run` through the CLI (several processes at once) or in
process, against the fake yt-dlp, which logs every call and marks calls that
overlapped another one (tests/e2e/fake_yt_dlp.py).
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from kernel import locks
from kernel import pipeline
from kernel.sources import youtube as kernel_youtube
from tests.regression_run import DEEPGRAM, ENGLISH, LONG, PROJECT_ROOT, RunFixture

# argv: lock path, start time (epoch), seconds to hold, "inherit" to pass the lock
# to a child that outlives this process. Prints "held", then the span it held.
HOLDER = """
import json, os, subprocess, sys, time
sys.path.insert(0, %r)
from kernel import locks
path, start_at, hold = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
time.sleep(max(0.0, start_at - time.time()))
lock = locks.FileLock(path)
lock.acquire(video_id="holder")
began = time.time()
if sys.argv[4:] == ["inherit"]:
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%%s)" %% hold], pass_fds=(lock.fd,))
    print("held", flush=True)
    os._exit(0)
print("held", flush=True)
time.sleep(hold)
ended = time.time()
lock.release()
print(json.dumps([began, ended]), flush=True)
""" % str(PROJECT_ROOT)


def holder(path: Path, hold: float, start_at: float = 0.0, inherit: bool = False) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(path), str(start_at), str(hold),
                             *(["inherit"] if inherit else [])], stdout=subprocess.PIPE, text=True)


def wait_until(condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


class FileLockTests(unittest.TestCase):
    """The flock primitive: exclusive, bounded waits, freed by the kernel."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yt-locks-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "x.lock"

    def test_eight_processes_never_hold_one_lock_together(self):
        for _ in range(5):
            start_at = time.time() + 0.5
            procs = [holder(self.path, 0.05, start_at) for _ in range(8)]
            spans = sorted(json.loads(proc.communicate(timeout=30)[0].splitlines()[-1]) for proc in procs)
            for earlier, later in zip(spans, spans[1:]):
                self.assertGreaterEqual(later[0], earlier[1])

    def test_a_killed_holder_frees_the_lock_at_once(self):
        proc = holder(self.path, 60)
        self.addCleanup(proc.stdout.close)
        self.assertEqual("held", proc.stdout.readline().strip())
        self.assertTrue(locks.is_held(self.path))
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        started = time.monotonic()
        lock = locks.FileLock(self.path)
        lock.acquire(timeout=5)
        self.addCleanup(lock.release)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_bounded_wait_ends_with_the_holder(self):
        proc = holder(self.path, 60)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual("held", proc.stdout.readline().strip())
        waited = []
        started = time.monotonic()
        with self.assertRaises(locks.Busy) as caught:
            locks.FileLock(self.path).acquire(timeout=0.3, on_wait=waited.append)
        self.assertLess(0.25, time.monotonic() - started)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual((proc.pid, "holder"), (caught.exception.holder["pid"], caught.exception.holder["video_id"]))
        self.assertEqual(proc.pid, waited[0]["pid"])
        self.assertIn(f"pid {proc.pid} (video_id holder", locks.describe(caught.exception.holder))
        with self.assertRaises(locks.Busy):
            locks.FileLock(self.path).acquire(timeout=0)

    def test_a_child_that_inherited_the_lock_keeps_it_after_its_parent_died(self):
        proc = holder(self.path, 1.0, inherit=True)
        self.addCleanup(proc.stdout.close)
        self.assertEqual("held", proc.stdout.readline().strip())
        proc.wait(timeout=10)
        self.assertTrue(locks.is_held(self.path))
        wait_until(lambda: not locks.is_held(self.path))

    def test_lock_files_stay_after_release(self):
        lock = locks.FileLock(self.path)
        lock.acquire(video_id="abc")
        self.assertTrue(lock.held and locks.is_held(self.path))
        self.assertEqual("abc", locks.read_holder(self.path)["video_id"])
        lock.release()
        self.assertFalse(lock.held or locks.is_held(self.path))
        self.assertTrue(self.path.is_file())


class YouTubeProblemTests(unittest.TestCase):
    """The one reader of yt-dlp's error text."""

    def test_classify_names_what_yt_dlp_reported(self):
        cases = {
            "ERROR: [youtube] x: This content isn't available, try again later. The current session has been "
            "rate-limited by YouTube for up to an hour.": "rate_limited",
            "ERROR: [youtube] x: This content isn’t available, try again later.": "rate_limited",
            "ERROR: Unable to download video subtitles for 'zh-Hans': HTTP Error 429: Too Many Requests":
                "rate_limited",
            "ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies-from-browser": "bot_check",
            "ERROR: [youtube] x: Sign in to confirm you're not a bot": "bot_check",
            "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate": "login_required",
            "ERROR: [youtube] x: Private video. Sign in if you've been granted access": "login_required",
            "ERROR: [youtube] x: Join this channel to get access to members-only content": "login_required",
            "ERROR: Unable to download video subtitles for 'en': HTTP Error 404: Not Found": "",
            "": "",
        }
        for text, problem in cases.items():
            with self.subTest(text=text):
                self.assertEqual(problem, kernel_youtube.classify(text))

    def test_said_quotes_the_line_that_names_the_problem(self):
        text = ("[youtube] x: Downloading webpage\n"
                "ERROR: [youtube] x: This content isn't available, try again later.\n"
                "ℹ️ something after\n")
        self.assertEqual("ERROR: [youtube] x: This content isn't available, try again later.",
                         kernel_youtube.said(text))
        self.assertEqual("ERROR: other", kernel_youtube.said("ERROR: other\nlast line"))
        self.assertEqual("last line", kernel_youtube.said("first\nlast line\n"))


class ConcurrentRunTests(RunFixture):
    """`run` calls that overlap: the same video, other videos, and a YouTube refusal."""

    def setUp(self):
        super().setUp()
        self.yt_dlp_log = self.root / "yt-dlp.jsonl"
        os.environ["FAKE_YT_DLP_LOG"] = str(self.yt_dlp_log)

    def yt_dlp_calls(self, video: str = "") -> list:
        """The fake yt-dlp's calls, without the `-t sleep` probes."""
        if not self.yt_dlp_log.exists():
            return []
        calls = [json.loads(line) for line in self.yt_dlp_log.read_text(encoding="utf-8").splitlines()]
        return [call for call in calls if call["kind"] != "probe" and (not video or call["video_id"] == video)]

    def hold(self, path: Path) -> locks.FileLock:
        lock = locks.FileLock(path)
        lock.acquire(video_id="test")
        self.addCleanup(lock.release)
        return lock

    # -- one video --------------------------------------------------------------

    def test_eight_calls_for_one_video_at_once_do_the_work_once(self):
        procs = [subprocess.Popen(self.cli(ENGLISH), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(8)]
        outputs = [proc.communicate(timeout=300) for proc in procs]
        files = set()
        for proc, (out, err) in zip(procs, outputs):
            self.assertEqual(0, proc.returncode, err[-2000:])
            lines = out.strip().splitlines()
            self.assertEqual(1, len(lines), out)
            result = json.loads(lines[0])
            self.assertEqual("done", result["status"])
            files.add(result["output_file"])
        self.assertEqual(1, len(files))
        self.assertEqual(["window", "translate"], self.llm_calls())
        self.assertTrue(any("Another run of this video is active" in err for _, err in outputs))
        self.assertEqual(1, len([call for call in self.yt_dlp_calls(ENGLISH) if call["kind"] == "extract"]))

    def test_waiting_for_the_video_lock_is_bounded_by_max_seconds(self):
        lock = self.hold(pipeline.video_lock_path(ENGLISH))
        started = time.monotonic()
        result = self.run_video(ENGLISH, max_seconds=0.5, mode="bilingual")
        self.assertGreaterEqual(time.monotonic() - started, 0.45)
        self.assertEqual(("in_progress", ""), (result["status"], result["stage"]))
        self.assertIn(f"pid {os.getpid()}", result["message"])
        # The call did nothing, so its next command repeats its flags; the state was not touched.
        self.assertIn("--mode bilingual --max-seconds 0.5", result["next_command"])
        self.assertFalse((self.work_dir(ENGLISH) / pipeline.STATE_FILE).exists())
        lock.release()
        self.assert_finished(ENGLISH, self.run_video(ENGLISH, max_seconds=300, mode="bilingual"))

    def test_cleanup_never_deletes_a_video_that_is_running(self):
        self.run_video(ENGLISH, stage="plan")
        lock = self.hold(pipeline.video_lock_path(ENGLISH))
        self.assertTrue(pipeline.status(ENGLISH)[0]["active"])
        refused, code = pipeline.cleanup(ENGLISH, force=True)
        self.assertEqual((1, "locked"), (code, refused["reason"]))
        self.assertTrue(self.work_dir(ENGLISH).is_dir())
        lock.release()
        self.assertFalse(pipeline.status(ENGLISH)[0]["active"])
        removed, code = pipeline.cleanup(ENGLISH, force=True)
        self.assertEqual((0, True), (code, removed["removed"]))
        self.assertTrue(pipeline.video_lock_path(ENGLISH).is_file())

    # -- YouTube ----------------------------------------------------------------

    def test_one_page_fetch_per_video_and_yt_dlp_paces_itself(self):
        self.assert_finished(ENGLISH, self.run_video(ENGLISH))
        metadata, subtitles = self.yt_dlp_calls(ENGLISH)
        self.assertEqual(("extract", "load_info"), (metadata["kind"], subtitles["kind"]))
        self.assertIn("-J", metadata["args"])
        for call in (metadata, subtitles):
            self.assertIn("-t sleep", " ".join(call["args"]))

        os.environ["FAKE_YT_DLP_NO_PRESET"] = "1"
        self.assert_finished(DEEPGRAM, self.run_video(DEEPGRAM))
        calls = self.yt_dlp_calls(DEEPGRAM)
        self.assertEqual(["extract", "extract"], [call["kind"] for call in calls])  # metadata, then audio by URL
        self.assertFalse(any("-t" in call["args"] for call in calls))

    def test_a_waiting_video_waits_for_youtube_within_max_seconds(self):
        self.hold(kernel_youtube.lock_path())
        result = self.run_video(ENGLISH, max_seconds=0.5)
        self.assertEqual(("in_progress", "metadata"), (result["status"], result["stage"]))
        self.assertIn(f"YouTube was still in use by pid {os.getpid()}", result["message"])
        self.assertEqual([], self.yt_dlp_calls())

    def test_a_session_rate_limit_stops_runs_already_going_and_new_runs_try_once(self):
        limited = dict(os.environ, FAKE_YT_DLP_SESSION_LIMITED="1", FAKE_YT_DLP_DELAY="1.5")
        first = subprocess.Popen(self.cli(LONG), env=limited, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        wait_until(lambda: locks.is_held(kernel_youtube.lock_path()))
        # Begins before the refusal, then waits for YouTube.
        queued = self.run_video(ENGLISH, max_seconds=60)
        out, err = first.communicate(timeout=60)
        hit = json.loads(out)
        self.assertEqual(("needs_user", "metadata", "rate_limited"), (hit["status"], hit["stage"], hit["reason"]))
        self.assertIn("rate-limited by YouTube for up to an hour", hit["message"])
        self.assertEqual(("needs_user", "metadata", "rate_limited"),
                         (queued["status"], queued["stage"], queued["reason"]))
        self.assertIn("runs that began before then do not call it again", queued["message"])
        self.assertIn("rate-limited by YouTube", queued["message"])
        self.assertEqual([], self.yt_dlp_calls(ENGLISH))
        refusal = kernel_youtube.last_refusal()
        self.assertEqual(("rate_limited", LONG), (refusal["problem"], refusal["video_id"]))

        # A run that begins later tries YouTube once, and renews the record when it is refused again.
        os.environ["FAKE_YT_DLP_SESSION_LIMITED"] = "1"
        again = self.run_video(ENGLISH)
        self.assertEqual("rate_limited", again["reason"])
        self.assertEqual(1, len(self.yt_dlp_calls(ENGLISH)))
        self.assertGreater(kernel_youtube.last_refusal()["time"], refusal["time"])
        del os.environ["FAKE_YT_DLP_SESSION_LIMITED"]
        self.assert_finished(ENGLISH, self.run_video(ENGLISH))

    def test_a_bot_check_gets_one_retry_with_chrome_cookies(self):
        os.environ["FAKE_YT_DLP_BOT_CHECK"] = "1"
        self.assert_finished(ENGLISH, self.run_video(ENGLISH))
        extracts = [call for call in self.yt_dlp_calls(ENGLISH) if call["kind"] == "extract"]
        self.assertEqual(2, len(extracts))
        self.assertNotIn("--cookies-from-browser", extracts[0]["args"])
        self.assertIn("--cookies-from-browser chrome", " ".join(extracts[1]["args"]))
        self.assertEqual({}, kernel_youtube.last_refusal())

        os.environ["FAKE_YT_DLP_BOT_CHECK"] = "always"
        result = self.run_video(DEEPGRAM)
        self.assertEqual(("needs_user", "metadata", "cookies_required"),
                         (result["status"], result["stage"], result["reason"]))
        self.assertIn("confirm you’re not a bot", result["message"])
        self.assertEqual(2, len(self.yt_dlp_calls(DEEPGRAM)))
        self.assertEqual(("bot_check", DEEPGRAM), (kernel_youtube.last_refusal()["problem"],
                                                   kernel_youtube.last_refusal()["video_id"]))

    def test_configured_cookies_get_no_extra_retry(self):
        os.environ["FAKE_YT_DLP_BOT_CHECK"] = "always"
        config = self.write_config("cookies.yaml", yt_dlp_cookies_from_browser="firefox")
        result = self.run_video(ENGLISH, config=config)
        self.assertEqual("cookies_required", result["reason"])
        self.assertEqual(1, len(self.yt_dlp_calls(ENGLISH)))


if __name__ == "__main__":
    unittest.main()
