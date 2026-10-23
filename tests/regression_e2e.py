"""End-to-end regression over recorded fixtures (no network, no real LLM or Deepgram).

Each case runs `scripts/run_regression.py --fixtures tests/fixtures/e2e`
(which calls `yt_transcript_utils.py run`) in a copy of the skill with an
empty config, so a local config.yaml cannot change routing or the LLM
backend. The fakes are in tests/e2e/ and tests/fake_llm.py; fixtures are
recorded with tests/e2e/record_fixtures.py. All cases run at once and share
one YouTube lock, as parallel calls on one machine would.

The observed behaviour of every case is compared with
tests/fixtures/e2e/baseline.json. After an intended change, regenerate it with

    UPDATE_E2E_BASELINE=1 python3 -m unittest tests.regression_e2e

and review the diff.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kernel.llm import agent_cli as _agent_cli
from kernel.pipeline import STAGES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "e2e"
BASELINE = FIXTURES / "baseline.json"
# What the copied skill needs to run the pipeline and the fakes.
SKILL_FILES = ("yt_transcript_utils.py", "kernel", "prompts", "scripts", "tests/e2e", "tests/fake_llm.py")
UPDATE_ENV = "UPDATE_E2E_BASELINE"
DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\. ")
FRONTMATTER_KEYS = ("title", "source", "channel", "date", "created", "bilingual", "duration", "transcript_source")


def _video_set() -> list[dict]:
    path = FIXTURES / "videos.json"
    return json.loads(path.read_text(encoding="utf-8"))["videos"] if path.exists() else []


def _copy_skill(target: Path) -> None:
    for name in SKILL_FILES:
        source = PROJECT_ROOT / name
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, destination)


def _child_env(llm_log: Path, root: Path) -> dict:
    env = {key: value for key, value in os.environ.items()}
    # Detected host agents must not matter; the fake CLI comes from the run.
    for agent in _agent_cli.KNOWN_AGENTS:
        for var in agent["env"]:
            env.pop(var, None)
    for var in ("AGENT", "AI_AGENT", _agent_cli.AGENT_CLI_ENV_VAR):
        env.pop(var, None)
    env["FAKE_LLM_LOG"] = str(llm_log)
    env["YT_TRANSCRIPT_LOCK_DIR"] = str(root / "locks")
    # Every yt-dlp call is logged and takes a moment, so overlapping calls would show.
    env["FAKE_YT_DLP_LOG"] = str(root / "yt-dlp.jsonl")
    env["FAKE_YT_DLP_DELAY"] = "0.1"
    return env


def _frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fields = {}
    for line in (match.group(1) if match else "").splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _rounded(value):
    return round(value, 3) if isinstance(value, float) else value


def observe(item: dict, vdir: Path, llm_calls: list[dict]) -> dict:
    """The parts of one run that the baseline pins down."""
    source = item.get("source") or {}
    plan = item.get("plan") or {}
    verify = item.get("verify") or {}
    observed = {
        "status": item.get("status"),
        "failed_step": item.get("failed_step", ""),
        "error": item.get("error", ""),
        "source": {k: source.get(k) for k in ("src", "language", "kind", "mode", "fallback_used", "blocked")
                   if k in source},
        "plan": {k: plan.get(k) for k in ("video_path", "routing_reason", "subtitle_quality_band",
                                           "reroute_recommended", "prompts") if k in plan},
        "raw_chars": item.get("raw_chars"),
        "llm_runs": [{k: run.get(k) for k in ("prompt", "round_count", "invalid_rounds", "fallback_rounds")}
                     for run in item.get("llm_runs", [])],
        "llm_calls": [call["kind"] for call in llm_calls if call["kind"] != "probe"],
    }
    if verify:
        observed["verify"] = {
            "passed": verify.get("passed"),
            "hard_failures": verify.get("hard_failures", []),
            "warnings": verify.get("warnings", []),
            "checks": {k: _rounded(v) for k, v in (verify.get("checks") or {}).items()},
        }
    if item.get("output_file"):
        text = (vdir / "output" / item["output_file"]).read_text(encoding="utf-8")
        frontmatter = _frontmatter(text)
        observed["output"] = {
            "file": item["output_file"],
            "frontmatter": {k: frontmatter.get(k) for k in FRONTMATTER_KEYS},
            "headings": [line for line in text.splitlines() if line.startswith("#")],
        }
    return observed


class EndToEndFixtureTests(unittest.TestCase):
    """Full pipeline over the recorded fixture videos."""

    @classmethod
    def setUpClass(cls):
        cls.videos = _video_set()
        if not cls.videos:
            raise unittest.SkipTest("no e2e fixtures recorded (tests/fixtures/e2e/videos.json)")
        cls._tmp = tempfile.TemporaryDirectory(prefix="yt-e2e-")
        root = Path(cls._tmp.name)
        _copy_skill(root / "skill")
        cls.results = {}
        procs = {}
        for video in cls.videos:
            vid = video["id"]
            procs[vid] = subprocess.Popen(
                [sys.executable, str(root / "skill" / "scripts" / "run_regression.py"),
                 "--fixtures", str(FIXTURES), "--only", vid, "--out-dir", str(root / "out" / vid),
                 "--timeout", "300"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=_child_env(root / f"{vid}_llm.jsonl", root),
            )
        log = root / "yt-dlp.jsonl"
        for vid, proc in procs.items():
            out_dir = root / "out" / vid
            llm_log = root / f"{vid}_llm.jsonl"
            _, stderr = proc.communicate(timeout=900)
            report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in llm_log.read_text(encoding="utf-8").splitlines()] \
                if llm_log.exists() else []
            cls.results[vid] = {
                "report": report["videos"][0],
                "observed": observe(report["videos"][0], out_dir / vid, calls),
                "vdir": out_dir / vid,
                "stderr": stderr,
            }
        cls.yt_dlp_calls = [call for call in map(json.loads, log.read_text(encoding="utf-8").splitlines())
                            if call["kind"] != "probe"] if log.exists() else []

    @classmethod
    def tearDownClass(cls):
        tmp = getattr(cls, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def test_routing_matches_expected_case(self):
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        try:
            import run_regression
        finally:
            sys.path.pop(0)
        for video in self.videos:
            with self.subTest(video=video["id"]):
                item = self.results[video["id"]]["report"]
                self.assertEqual(video["expected_case"], run_regression.observed_case(item), item.get("error"))
                self.assertFalse((item.get("source") or {}).get("blocked"))

    def test_supported_videos_finish_with_artifacts(self):
        for video in self.videos:
            if video["expected_case"] == "unsupported":
                continue
            vid = video["id"]
            with self.subTest(video=vid):
                result = self.results[vid]
                item, vdir = result["report"], result["vdir"]
                self.assertEqual("ok", item["status"], f"{item.get('error')}\n{result['stderr'][-2000:]}")
                state = json.loads((vdir / "state.json").read_text(encoding="utf-8"))
                self.assertEqual("done", state["status"])
                self.assertEqual(STAGES, tuple(state["stages"]))
                self.assertTrue(all(stage["status"] == "done" for stage in state["stages"].values()))
                # The cleanup stage leaves only state.json in the video's work dir.
                self.assertEqual(["state.json"], sorted(p.name for p in (vdir.parent / "work" / vid).iterdir()))
                self.assertEqual(vdir / "output" / item["output_file"], Path(state["output_file"]))
                self.assertTrue(item["verify"]["passed"])
                self.assertEqual([], item["verify"]["hard_failures"])
                self.assertNotIn("unexpected", result["observed"]["llm_calls"])

    def test_final_markdown_name_and_frontmatter(self):
        for video in self.videos:
            if video["expected_case"] == "unsupported":
                continue
            with self.subTest(video=video["id"]):
                output = self.results[video["id"]]["observed"]["output"]
                self.assertRegex(output["file"], DATE_PREFIX)
                self.assertTrue(output["file"].endswith(".md"))
                frontmatter = output["frontmatter"]
                self.assertRegex(frontmatter["date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertEqual(frontmatter["date"], output["file"][:10])
                self.assertEqual(f"https://www.youtube.com/watch?v={video['id']}", frontmatter["source"])
                self.assertEqual("true" if video["expected_case"].startswith("en") else "false",
                                 frontmatter["bilingual"])
                self.assertTrue(any(line.startswith("## ") for line in output["headings"]))

    def test_unsupported_language_stops_before_text_work(self):
        for video in self.videos:
            if video["expected_case"] != "unsupported":
                continue
            with self.subTest(video=video["id"]):
                result = self.results[video["id"]]
                self.assertEqual("unsupported", result["report"]["status"])
                self.assertEqual([], result["observed"]["llm_calls"])

    def test_long_video_runs_several_window_rounds_with_outline(self):
        long_cases = [v for v in self.videos if v.get("case", "").endswith("_long")]
        for video in long_cases:
            with self.subTest(video=video["id"]):
                calls = self.results[video["id"]]["observed"]["llm_calls"]
                self.assertIn("outline", calls)
                self.assertGreaterEqual(calls.count("window"), 2)

    def test_yt_dlp_ran_one_at_a_time_with_one_page_fetch_per_video(self):
        self.assertTrue(self.yt_dlp_calls)
        self.assertEqual([], [call for call in self.yt_dlp_calls if call["overlap"]])
        for video in self.videos:
            with self.subTest(video=video["id"]):
                calls = [call for call in self.yt_dlp_calls if call["video_id"] == video["id"]]
                extracts = [call for call in calls if call["kind"] == "extract"]
                # The metadata, plus the audio download for the Deepgram route; subtitles read the saved metadata.
                self.assertEqual(2 if video["expected_case"] == "deepgram" else 1, len(extracts))
                self.assertIn("-J", extracts[0]["args"])
                self.assertTrue(all(call["kind"] == "load_info" for call in calls
                                    if "--write-sub" in call["args"]))

    def test_matches_baseline(self):
        observed = {vid: result["observed"] for vid, result in sorted(self.results.items())}
        if os.environ.get(UPDATE_ENV):
            BASELINE.write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.skipTest(f"baseline written to {BASELINE}")
        self.assertTrue(BASELINE.exists(), f"no baseline yet; run with {UPDATE_ENV}=1")
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(baseline), sorted(observed))
        for vid in observed:
            with self.subTest(video=vid):
                self.assertEqual(baseline[vid], observed[vid])


if __name__ == "__main__":
    unittest.main()
