"""The `run` command (kernel/pipeline.py) over the recorded e2e fixtures.

Each test gets its own work root and output dir. download.sh runs against the
fake yt-dlp in tests/e2e/bin, Deepgram answers from the recorded response, and
the LLM is tests/fake_llm.py through `llm_agent_cli`; everything else is the
real pipeline. tests/regression_e2e.py pins the articles themselves; these
tests cover resuming, the result statuses and the overrides.
"""

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yt_transcript_utils as utils
from kernel import pipeline
from kernel.long_text import agent_cli as _agent_cli
from kernel.task_runtime import runtime as kernel_runtime
from tests.e2e import fake_deepgram

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "e2e"
FAKE_LLM = PROJECT_ROOT / "tests" / "fake_llm.py"
E2E_BIN = PROJECT_ROOT / "tests" / "e2e" / "bin"

ENGLISH = "CIatbV3_Rjg"   # short English video: structure_only + translate_only, one round each
LONG = "Fhx_qNS2P8g"      # long Chinese video: outline + three window rounds
DEEPGRAM = "E2Edeepgram"  # no subtitles: Deepgram
JAPANESE = "tGVSOK3PNUI"  # unsupported language
EARLY_STAGES = ("metadata", "source", "normalize", "plan")


class RunCommandTests(unittest.TestCase):
    """`run` statuses, resume points and overrides, in process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yt-run-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.llm_log = self.root / "llm.jsonl"
        patcher = mock.patch.dict(os.environ, {
            "PATH": f"{E2E_BIN}{os.pathsep}{os.environ.get('PATH', '')}",
            fake_deepgram.FIXTURES_ENV: str(FIXTURES),
            pipeline.WORK_ROOT_ENV: str(self.root / "work"),
            pipeline.OUTPUT_DIR_ENV: str(self.root / "out"),
            "FAKE_LLM_LOG": str(self.llm_log),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # A detected host agent must not answer instead of the fake CLI.
        for agent in _agent_cli.KNOWN_AGENTS:
            for var in agent["env"]:
                os.environ.pop(var, None)
        for var in ("AGENT", "AI_AGENT", _agent_cli.AGENT_CLI_ENV_VAR, "FAKE_YT_DLP_RATE_LIMITED",
                    "FAKE_LLM_FAIL_FROM_UNIT"):
            os.environ.pop(var, None)
        deepgram = mock.patch.object(utils, "_call_deepgram_api", fake_deepgram.recorded_response)
        deepgram.start()
        self.addCleanup(deepgram.stop)
        self.config = self.write_config("config.yaml")

    # -- helpers ------------------------------------------------------------

    def write_config(self, name: str, **fields) -> str:
        values = {
            "llm_backend": "auto",
            "llm_agent_cli": json.dumps(shlex.join([sys.executable, str(FAKE_LLM)])),
            "llm_cli_max_retries": "0",
            "deepgram_api_key": "fixture",
        }
        values.update(fields)
        path = self.root / name
        path.write_text("".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
                        encoding="utf-8")
        return str(path)

    def run_video(self, video: str, config: str | None = None, **options) -> dict:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result, code = pipeline.run(video, config_path=config or self.config, **options)
        self.assertEqual(pipeline.EXIT_CODES.get(result["status"], pipeline.USAGE_EXIT_CODE), code)
        return result

    def work_dir(self, video: str) -> Path:
        return self.root / "work" / video

    def state(self, video: str) -> dict:
        return json.loads((self.work_dir(video) / pipeline.STATE_FILE).read_text(encoding="utf-8"))

    def llm_calls(self) -> list[str]:
        if not self.llm_log.exists():
            return []
        calls = [json.loads(line)["kind"] for line in self.llm_log.read_text(encoding="utf-8").splitlines()]
        return [kind for kind in calls if kind != "probe"]

    def stage_starts(self, video: str) -> dict:
        return {name: record.get("started_at") for name, record in self.state(video)["stages"].items()}

    def reference_article(self, video: str) -> str:
        """The article from one uninterrupted run in a separate work root and output dir."""
        with mock.patch.dict(os.environ, {pipeline.WORK_ROOT_ENV: str(self.root / "ref_work"),
                                          pipeline.OUTPUT_DIR_ENV: str(self.root / "ref_out"),
                                          "FAKE_LLM_LOG": str(self.root / "ref_llm.jsonl")}):
            result = self.run_video(video)
        self.assertEqual("done", result["status"], result)
        return Path(result["output_file"]).read_text(encoding="utf-8")

    def assert_finished(self, video: str, result: dict) -> None:
        self.assertEqual("done", result["status"], result)
        self.assertTrue(Path(result["output_file"]).is_file())
        state = self.state(video)
        self.assertEqual("done", state["status"])
        self.assertTrue(all(state["stages"][name]["status"] == "done" for name in pipeline.STAGES))
        self.assertEqual([pipeline.STATE_FILE], sorted(p.name for p in self.work_dir(video).iterdir()))

    # -- inputs and the CLI ---------------------------------------------------

    def test_parse_video_id_accepts_youtube_url_forms(self):
        for url in ("https://www.youtube.com/watch?v=CIatbV3_Rjg&t=42s", "https://youtu.be/CIatbV3_Rjg?si=x",
                    "youtube.com/watch?v=CIatbV3_Rjg", "https://m.youtube.com/watch?v=CIatbV3_Rjg",
                    "https://www.youtube.com/shorts/CIatbV3_Rjg", "https://www.youtube.com/live/CIatbV3_Rjg",
                    "CIatbV3_Rjg"):
            with self.subTest(url=url):
                self.assertEqual(ENGLISH, pipeline.parse_video_id(url))
        for url in ("https://example.com/watch?v=CIatbV3_Rjg", "https://www.youtube.com/@channel", "", "not a url"):
            with self.subTest(url=url):
                self.assertEqual("", pipeline.parse_video_id(url))

    def test_invalid_url_is_a_usage_error(self):
        result = self.run_video("https://example.com/video")
        self.assertEqual("usage_error", result["status"])
        self.assertFalse((self.root / "work").exists())

    def test_cli_prints_one_json_result_with_the_exit_code(self):
        env = dict(os.environ)
        for args, code, status in (([ENGLISH], 0, "done"), (["https://example.com/x"], 2, "usage_error"),
                                   ([JAPANESE], 4, "unsupported")):
            with self.subTest(args=args):
                proc = subprocess.run(
                    [sys.executable, str(PROJECT_ROOT / "tests" / "e2e" / "fake_deepgram.py"), "run", *args,
                     "--config-path", self.config],
                    capture_output=True, text=True, env=env, timeout=300,
                )
                self.assertEqual(code, proc.returncode, proc.stderr[-2000:])
                lines = proc.stdout.strip().splitlines()
                self.assertEqual(1, len(lines), proc.stdout)
                self.assertEqual(status, json.loads(lines[0])["status"])

    # -- done, resume, in_progress --------------------------------------------

    def test_finished_run_returns_done_without_redoing_work(self):
        first = self.run_video(ENGLISH)
        self.assert_finished(ENGLISH, first)
        self.assertEqual("subtitles", first["route"]["source"])
        self.assertEqual("bilingual", first["route"]["mode"])
        self.assertEqual(["window", "translate"], self.llm_calls())
        self.assertRegex(Path(first["output_file"]).name, r"^\d{4}-\d{2}-\d{2}\. .+\.md$")

        second = self.run_video(f"https://youtu.be/{ENGLISH}")
        self.assertEqual("done", second["status"])
        self.assertEqual(first["output_file"], second["output_file"])
        self.assertEqual(["window", "translate"], self.llm_calls())

        # A deleted article means the run starts over.
        Path(first["output_file"]).unlink()
        third = self.run_video(ENGLISH)
        self.assert_finished(ENGLISH, third)
        self.assertEqual(["window", "translate"] * 2, self.llm_calls())

    def test_rerun_after_llm_failure_resumes_at_the_text_cursor(self):
        expected = self.reference_article(LONG)
        os.environ["FAKE_LLM_FAIL_FROM_UNIT"] = "2"
        failed = self.run_video(LONG)
        self.assertEqual("failed", failed["status"])
        self.assertEqual("text", failed["stage"])
        self.assertEqual("llm_error", failed["reason"])
        self.assertTrue(failed["retryable"])
        state = self.state(LONG)
        cursor = state["stages"]["text"]["tasks"]["cleanup_zh"]["cursor"]
        self.assertGreater(cursor, 1)
        starts = self.stage_starts(LONG)
        calls_before = self.llm_calls()
        self.assertEqual(["outline", "window"], calls_before[:2])

        del os.environ["FAKE_LLM_FAIL_FROM_UNIT"]
        resumed = self.run_video(LONG)
        self.assert_finished(LONG, resumed)
        # Done stages kept their first run; the text stage went on from the cursor.
        for name in EARLY_STAGES:
            self.assertEqual(starts[name], self.stage_starts(LONG)[name], name)
        new_calls = self.llm_calls()[len(calls_before):]
        self.assertNotIn("outline", new_calls)
        self.assertEqual(2, new_calls.count("window"))
        self.assertEqual(expected, Path(resumed["output_file"]).read_text(encoding="utf-8"))

    def test_max_seconds_returns_in_progress_at_safe_points(self):
        expected = self.reference_article(LONG)
        results, cursors = [], []
        for _ in range(30):
            result = self.run_video(LONG, max_seconds=0.001)
            results.append(result)
            if result["status"] != "in_progress":
                break
            self.assertIn("--max-seconds 0.001", result["next_command"])
            task = self.state(LONG)["stages"].get("text", {}).get("tasks", {}).get("cleanup_zh", {})
            if task.get("status") == "running":
                cursors.append(task["cursor"])
        self.assert_finished(LONG, results[-1])
        self.assertGreater(len(results), len(pipeline.STAGES))
        # Each in_progress inside the text stage stopped after one more round.
        self.assertGreaterEqual(len(cursors), 2)
        self.assertEqual(sorted(set(cursors)), cursors)
        self.assertEqual(["outline", "window", "window", "window"], self.llm_calls())
        self.assertEqual(expected, Path(results[-1]["output_file"]).read_text(encoding="utf-8"))

    # -- other statuses -------------------------------------------------------

    def test_unsupported_language_stops_in_the_source_stage(self):
        result = self.run_video(JAPANESE)
        self.assertEqual("unsupported", result["status"])
        self.assertEqual(("source", "unsupported_language", "ja"),
                         (result["stage"], result["reason"], result["language"]))
        self.assertEqual([], self.llm_calls())

    def test_missing_deepgram_key_needs_user_then_resumes(self):
        result = self.run_video(DEEPGRAM, config=self.write_config("nokey.yaml", deepgram_api_key=None))
        self.assertEqual(("needs_user", "source", "deepgram_key_missing"),
                         (result["status"], result["stage"], result["reason"]))
        self.assertEqual("deepgram", self.state(DEEPGRAM)["route"]["source"])
        starts = self.stage_starts(DEEPGRAM)

        resumed = self.run_video(DEEPGRAM)
        self.assert_finished(DEEPGRAM, resumed)
        self.assertEqual({"source": "deepgram", "language": "zh", "track": None, "track_kind": None,
                          "mode": "chinese"}, resumed["route"])
        self.assertEqual(starts["metadata"], self.stage_starts(DEEPGRAM)["metadata"])

    def test_missing_output_dir_needs_user_then_assembles(self):
        del os.environ[pipeline.OUTPUT_DIR_ENV]
        result = self.run_video(ENGLISH)
        self.assertEqual(("needs_user", "assemble", "output_dir_missing"),
                         (result["status"], result["stage"], result["reason"]))
        config = self.write_config("with_output.yaml", output_dir=str(self.root / "vault"))
        resumed = self.run_video(ENGLISH, config=config)
        self.assert_finished(ENGLISH, resumed)
        self.assertEqual(self.root / "vault", Path(resumed["output_file"]).parent)
        self.assertEqual(["window", "translate"], self.llm_calls())

    def test_rate_limited_preferred_track_needs_user_until_mode_is_given(self):
        # The English video with a manual Chinese track (preferred) that YouTube rate-limits.
        fixtures = self.root / "fixtures"
        (fixtures / ENGLISH).mkdir(parents=True)
        info = json.loads((FIXTURES / ENGLISH / "info.json").read_text(encoding="utf-8"))
        info["subtitles"]["zh-Hans"] = [{"ext": "vtt", "name": "Chinese (Simplified)", "url": ""}]
        (fixtures / ENGLISH / "info.json").write_text(json.dumps(info), encoding="utf-8")
        (fixtures / ENGLISH / "subs").symlink_to(FIXTURES / ENGLISH / "subs")
        os.environ[fake_deepgram.FIXTURES_ENV] = str(fixtures)
        os.environ["FAKE_YT_DLP_RATE_LIMITED"] = "zh-Hans"

        result = self.run_video(ENGLISH)
        self.assertEqual(("needs_user", "source", "rate_limited"),
                         (result["status"], result["stage"], result["reason"]))
        self.assertEqual({"track": "zh-Hans", "track_kind": "manual", "mode": "chinese"},
                         result["preferred_track"])
        self.assertEqual({"track": "en", "track_kind": "manual", "mode": "bilingual"}, result["fallback_track"])
        self.assertTrue(result["accept_command"].endswith("--mode bilingual"))
        self.assertEqual([], self.llm_calls())

        # Accepting the English track; the choice sticks for later reruns.
        accepted = self.run_video(ENGLISH, mode="bilingual")
        self.assert_finished(ENGLISH, accepted)
        self.assertEqual(("en", "bilingual"), (accepted["route"]["track"], accepted["route"]["mode"]))
        self.assertEqual("bilingual", self.state(ENGLISH)["overrides"]["mode"])
        self.assertEqual("done", self.run_video(ENGLISH)["status"])

    def test_missing_yt_dlp_needs_user(self):
        with mock.patch("kernel.pipeline.shutil.which", return_value=None):
            result = self.run_video(ENGLISH)
        self.assertEqual(("needs_user", "metadata", "tool_missing"),
                         (result["status"], result["stage"], result["reason"]))

    def test_no_llm_backend_hands_each_request_to_the_agent(self):
        expected = self.reference_article(LONG)
        config = self.write_config("handoff.yaml", llm_agent_cli=None)
        tasks = []
        for _ in range(20):
            result = self.run_video(LONG, config=config)
            if result["status"] != "needs_agent_text":
                break
            self.assertEqual(("text", "no_llm_backend"), (result["stage"], result["reason"]))
            task = result["task"]
            tasks.append(task["name"])
            prompt = Path(task["prompt_file"]).read_text(encoding="utf-8")
            answer = subprocess.run([sys.executable, str(FAKE_LLM)], input=prompt, capture_output=True,
                                    text=True, check=True, timeout=60).stdout
            Path(task["output_file"]).write_text(answer, encoding="utf-8")
        self.assert_finished(LONG, result)
        # The outline and the three window rounds, one request per call.
        self.assertEqual(["cleanup_zh"] * 4, tasks)
        self.assertEqual("agent-handoff", result["llm"]["backend"])
        self.assertEqual(expected, Path(result["output_file"]).read_text(encoding="utf-8"))

    def test_active_lock_fails_with_locked(self):
        work_dir = self.work_dir(ENGLISH)
        work_dir.mkdir(parents=True)
        owner = kernel_runtime.acquire_runtime_ownership(str(work_dir), "run")
        self.assertTrue(owner["success"])
        try:
            result = self.run_video(ENGLISH)
        finally:
            kernel_runtime.release_runtime_ownership(str(work_dir), owner["owner_id"])
        self.assertEqual(("failed", "locked"), (result["status"], result["reason"]))
        self.assertTrue(result["retryable"])
        self.assertEqual("done", self.run_video(ENGLISH)["status"])

    def test_override_that_cannot_be_honored_fails(self):
        for video, options in ((ENGLISH, {"mode": "chinese"}), (DEEPGRAM, {"source": "subtitles"})):
            with self.subTest(video=video):
                result = self.run_video(video, **options)
                self.assertEqual(("failed", "source", "override_not_applicable"),
                                 (result["status"], result["stage"], result["reason"]))
                self.assertFalse(result["retryable"])

    def test_quality_failure_stops_in_verify_and_from_text_redoes_the_text(self):
        failing = {"passed": False, "hard_failures": ["size ratio too low"], "warnings": [], "checks": {}}
        with mock.patch.object(utils, "verify_quality", return_value=failing):
            result = self.run_video(ENGLISH)
        self.assertEqual(("failed", "verify", "quality_hard_failure"),
                         (result["status"], result["stage"], result["reason"]))
        self.assertEqual(["size ratio too low"], result["hard_failures"])
        starts = self.stage_starts(ENGLISH)

        # A plain rerun repeats only the verify stage.
        rerun = self.run_video(ENGLISH)
        self.assert_finished(ENGLISH, rerun)
        self.assertEqual(["window", "translate"], self.llm_calls())

        # --from text on a finished run: cleanup removed the inputs, so every stage reruns.
        redone = self.run_video(ENGLISH, from_stage="text")
        self.assert_finished(ENGLISH, redone)
        self.assertEqual(["window", "translate"] * 2, self.llm_calls())
        self.assertNotEqual(starts["metadata"], self.stage_starts(ENGLISH)["metadata"])

    def test_from_stage_before_cleanup_keeps_earlier_stages(self):
        with mock.patch.object(utils, "verify_quality",
                               return_value={"passed": False, "hard_failures": ["x"], "warnings": [], "checks": {}}):
            self.run_video(ENGLISH)
        starts = self.stage_starts(ENGLISH)
        result = self.run_video(ENGLISH, from_stage="text")
        self.assert_finished(ENGLISH, result)
        for name in EARLY_STAGES:
            self.assertEqual(starts[name], self.stage_starts(ENGLISH)[name], name)
        self.assertNotEqual(starts["text"], self.stage_starts(ENGLISH)["text"])
        self.assertEqual(["window", "translate"] * 2, self.llm_calls())

    def test_changed_override_redoes_the_source_and_sticks(self):
        self.run_video(DEEPGRAM, stage="plan")
        starts = self.stage_starts(DEEPGRAM)
        result = self.run_video(DEEPGRAM, source="deepgram")
        self.assert_finished(DEEPGRAM, result)
        self.assertEqual(starts["metadata"], self.stage_starts(DEEPGRAM)["metadata"])
        self.assertEqual({"source": "deepgram", "mode": None}, self.state(DEEPGRAM)["overrides"])
        self.assertEqual("--source deepgram", self.state(DEEPGRAM)["route"]["reason"])
        # Without the flag the remembered override still holds.
        self.assertEqual("done", self.run_video(DEEPGRAM)["status"])

    def test_restart_discards_the_work_dir(self):
        self.run_video(ENGLISH)
        starts = self.stage_starts(ENGLISH)
        result = self.run_video(ENGLISH, restart=True)
        self.assert_finished(ENGLISH, result)
        self.assertNotEqual(starts["metadata"], self.stage_starts(ENGLISH)["metadata"])
        self.assertEqual(["window", "translate"] * 2, self.llm_calls())

    def test_stage_plan_checks_routing_without_llm_or_deepgram(self):
        dg = self.run_video(DEEPGRAM, stage="plan")
        self.assertEqual(("done", "plan"), (dg["status"], dg["stage"]))
        self.assertEqual("deepgram", dg["route"]["source"])
        self.assertIn("Deepgram", dg["message"])
        self.assertNotIn("plan", dg)
        self.assertEqual("pending", self.state(DEEPGRAM)["stages"]["source"]["status"])
        self.assertFalse((self.work_dir(DEEPGRAM) / pipeline.RAW_TEXT_FILE).exists())

        en = self.run_video(ENGLISH, stage="plan")
        self.assertEqual("done", en["status"])
        self.assertEqual("planned", self.state(ENGLISH)["status"])
        self.assertEqual(["structure_only", "translate_only"], [task["prompt"] for task in en["plan"]["tasks"]])
        self.assertEqual("short", en["plan"]["video_path"])
        self.assertEqual([], self.llm_calls())
        starts = self.stage_starts(ENGLISH)
        full = self.run_video(ENGLISH)
        self.assert_finished(ENGLISH, full)
        for name in EARLY_STAGES:
            self.assertEqual(starts[name], self.stage_starts(ENGLISH)[name], name)

    def test_critical_subtitles_reroute_to_deepgram_in_the_source_stage(self):
        real_signal = utils._build_source_routing_signal

        def critical(*args, **kwargs):
            return {**real_signal(*args, **kwargs), "subtitle_quality_score": 0.2,
                    "subtitle_quality_band": "critical", "reroute_recommended": True, "reroute_target": "deepgram"}

        recorded = lambda *args, **kwargs: fake_deepgram.recorded_response(f"/x/{DEEPGRAM}.m4a")  # noqa: E731
        with mock.patch.object(utils, "_build_source_routing_signal", critical), \
                mock.patch.object(utils, "_call_deepgram_api", recorded):
            rerouted = self.run_video(LONG, stage="plan")
            self.assertEqual("deepgram", rerouted["route"]["source"])
            self.assertEqual({"track": "zh-Hans", "track_kind": "manual", "mode": "chinese",
                              "subtitle_quality": {"score": 0.2, "band": "critical"}},
                             rerouted["route"]["rerouted_from"])
            kept = self.run_video(LONG, source="subtitles", stage="plan")
            self.assertEqual("subtitles", kept["route"]["source"])
            self.assertIsNone(kept["route"]["rerouted_from"])

    def test_status_and_cleanup_commands(self):
        self.run_video(ENGLISH)
        self.run_video(DEEPGRAM, stage="plan")
        listing, code = pipeline.status()
        self.assertEqual(0, code)
        self.assertEqual([ENGLISH], [entry["video_id"] for entry in listing["finished"]])
        self.assertEqual([DEEPGRAM], [entry["video_id"] for entry in listing["unfinished"]])
        self.assertTrue(listing["has_unfinished"])
        self.assertEqual("source", listing["unfinished"][0]["next_stage"])
        one, _ = pipeline.status(ENGLISH)
        self.assertTrue(one["found"] and one["finished"])

        refused, code = pipeline.cleanup(DEEPGRAM)
        self.assertEqual((1, "unfinished"), (code, refused["reason"]))
        self.assertTrue(self.work_dir(DEEPGRAM).exists())
        removed, code = pipeline.cleanup(DEEPGRAM, force=True)
        self.assertEqual((0, True), (code, removed["removed"]))
        removed, code = pipeline.cleanup(ENGLISH)
        self.assertEqual((0, True), (code, removed["removed"]))
        self.assertEqual({"finished": [], "unfinished": []},
                         {key: pipeline.status()[0][key] for key in ("finished", "unfinished")})


if __name__ == "__main__":
    unittest.main()
