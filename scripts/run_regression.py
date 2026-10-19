#!/usr/bin/env python3
"""Live regression runner for yt-transcript.

Runs `yt_transcript_utils.py run` on a fixed set of real YouTube videos and
writes a report so behaviour can be compared before and after a change.

Stages (pick with --stage):
    plan      `run --stage plan`: metadata, subtitle selection, subtitle
              quality, normalization and the text plan. No LLM requests and
              no Deepgram; a video that needs audio transcription is reported
              as skipped (no usable subtitles) or rerouted (subtitle quality).
    full      `run` to the finished article (default). With --no-deepgram it
              runs `--stage plan` first and stops there when the route needs
              Deepgram.

Every run uses its own work root (<out-dir>/work) and writes the article to
<out-dir>/<video_id>/output, so it never touches /tmp/yt-transcript or the
configured output_dir.

Usage:
    python3 scripts/run_regression.py
    python3 scripts/run_regression.py --stage plan --only H_peyIigkkc
    python3 scripts/run_regression.py --compare /tmp/yt_regression/<old>/report.json
    python3 scripts/run_regression.py --config-path /tmp/regression_config.yaml
    python3 scripts/run_regression.py --fixtures tests/fixtures/e2e

--fixtures replays recorded videos offline (see tests/e2e/): a fake yt-dlp
serves the recorded metadata and subtitles, Deepgram answers from the recorded
response, and the LLM is tests/fake_llm.py through the agent CLI setting.
Every other step runs the real code. tests/regression_e2e.py runs it in a
copy of the skill with its own config.yaml.

Each run writes <out-dir>/report.json, <out-dir>/report.md and one folder per
video with the run log (run.log), the last result (result.json), the final
state.json and the article.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
UTILS = ROOT_DIR / "yt_transcript_utils.py"
DEFAULT_VIDEOS = ROOT_DIR / "tests" / "live_regression" / "videos.json"
E2E_DIR = ROOT_DIR / "tests" / "e2e"
FAKE_LLM = ROOT_DIR / "tests" / "fake_llm.py"
STAGES = ("plan", "full")
# Same names as kernel/pipeline.py; this script only talks to the CLI.
WORK_ROOT_ENV = "YT_TRANSCRIPT_WORK_ROOT"
OUTPUT_DIR_ENV = "YT_TRANSCRIPT_OUTPUT_DIR"

# verify-quality checks worth tracking across runs.
TRACKED_CHECKS = (
    "size_ratio_vs_raw",
    "section_count",
    "paragraph_count",
    "total_chars",
    "cn_char_ratio",
    "en_char_ratio",
    "bilingual_pairs",
    "header_density",
    "chunk_seam_warning_count",
    "glossary_drift_count",
    "semantic_missing_count",
)

# Environment for every run; fixture mode adds the fakes (see fixture_env).
CMD_ENV: dict | None = None
# Fixture mode: `run` goes through tests/e2e/fake_deepgram.py.
FIXTURE_MODE = False
# --config-path for every run (fixture mode writes its own config).
CONFIG_ARGS: list[str] = []


def write_fixture_config(path: Path) -> None:
    """Config for fixture runs: the LLM is the fake agent CLI, no API; Deepgram is faked."""
    path.write_text(
        "llm_backend: auto\n"
        f"llm_agent_cli: {json.dumps(shlex.join([sys.executable, str(FAKE_LLM)]))}\n"
        "deepgram_api_key: fixture\n",
        encoding="utf-8",
    )


def fixture_env(fixtures_dir: Path) -> dict:
    """Environment that points download.sh and the LLM backend at the fakes."""
    env = dict(os.environ)
    env["PATH"] = f"{E2E_DIR / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["YT_TRANSCRIPT_E2E_FIXTURES"] = str(fixtures_dir.resolve())
    env["YT_TRANSCRIPT_AGENT_CLI"] = shlex.join([sys.executable, str(FAKE_LLM)])
    # The fake yt-dlp must not be "updated" from GitHub.
    env["YT_TRANSCRIPT_YT_DLP_UPDATE"] = "0"
    return env


def run_args(url: str, *extra: str) -> list[str]:
    script = E2E_DIR / "fake_deepgram.py" if FIXTURE_MODE else UTILS
    return [sys.executable, str(script), "run", url, *extra, *CONFIG_ARGS]


def call_run(args: list[str], log_path: Path, env: dict, timeout: int) -> dict:
    """Run one `run` call, append its output to log_path, return its JSON result."""
    started = time.monotonic()
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"$ {shlex.join(args)}\n")
        try:
            proc = subprocess.run(args, cwd=ROOT_DIR, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            log.write(f"TIMEOUT after {timeout}s\n{stderr}\n")
            return {"status": "failed", "reason": "timeout", "message": f"run timed out after {timeout}s"}
        log.write(f"# exit={proc.returncode} elapsed={time.monotonic() - started:.1f}s\n"
                  f"## stdout\n{proc.stdout}\n## stderr\n{proc.stderr}\n")
    lines = proc.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        return {"status": "failed", "reason": "no_result",
                "message": f"run exited {proc.returncode} without a result: {' | '.join(tail)}"}
    return result


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def needs_deepgram(state: dict) -> bool:
    """The route needs audio transcription that has not run (plan-only runs stop there)."""
    return (state.get("route") or {}).get("source") == "deepgram" \
        and (state.get("stages") or {}).get("source", {}).get("status") != "done"


def track_case(language: str, kind: str) -> str:
    family = "zh" if language.startswith("zh") else ("en" if language.startswith("en") else language)
    return f"{family}_{kind}" if kind else family


def report_item(video: dict, result: dict, state: dict, blocked: list[str] | None = None) -> dict:
    """One report row from the last `run` result and the video's state.json.

    `blocked` names preferred tracks YouTube refused before the run accepted
    the fallback track (see run_video).
    """
    route = state.get("route") or {}
    stages = state.get("stages") or {}
    meta = state.get("metadata") or {}
    item: dict = {
        "id": video["id"],
        "label": video.get("label", ""),
        "expected_case": video.get("expected_case", ""),
        "status": "",
        "failed_step": "",
        "error": "",
        "timings_sec": {name: record["seconds"] for name, record in stages.items() if "seconds" in record},
        "title": meta.get("title", ""),
        "duration": meta.get("duration", 0),
        "result": {key: result.get(key) for key in ("status", "stage", "reason") if key in result},
        "route_reason": route.get("reason", ""),
    }
    rerouted = route.get("rerouted_from") or {}
    skipped_audio = needs_deepgram(state)
    if route.get("source") == "subtitles" or (skipped_audio and rerouted):
        # A rerouted video that was not transcribed reports the subtitle track it checked.
        track = rerouted if route.get("source") == "deepgram" else route
        item["source"] = {
            "src": "youtube",
            "language": track.get("track") or "",
            "kind": track.get("track_kind") or "",
            "mode": track.get("mode") or "",
            "fallback_used": route.get("fallback_used"),
            # e.g. "zh-Hans:rate_limited"; a non-empty list means YouTube refused a
            # preferred track this run, so the selected source can differ between runs.
            "blocked": list(blocked or []) + list(route.get("blocked_tracks") or []),
        }
    elif route.get("source") == "deepgram":
        item["source"] = {"src": "deepgram", "language": route.get("language") or "", "kind": "audio",
                          "mode": route.get("mode") or ""}
        item["source"].update(route.get("deepgram") or {})
        if rerouted:
            item["source"]["rerouted_from"] = track_case(str(rerouted.get("track") or ""),
                                                         str(rerouted.get("track_kind") or ""))
    if "raw_chars" in state:
        item["raw_chars"] = state["raw_chars"]

    plan = (stages.get("plan") or {}).get("summary") or {}
    if plan:
        item["plan"] = {key: plan.get(key) for key in (
            "video_path", "routing_reason", "subtitle_quality_score", "subtitle_quality_band",
            "reroute_recommended", "reroute_target")}
        item["plan"]["prompts"] = [task["prompt"] for task in plan.get("tasks", [])]
        item["plan"]["warnings"] = plan.get("warnings", [])
    elif rerouted:
        quality = rerouted.get("subtitle_quality") or {}
        item["plan"] = {"subtitle_quality_score": quality.get("score"), "subtitle_quality_band": quality.get("band"),
                        "reroute_recommended": True, "reroute_target": "deepgram"}

    tasks = (stages.get("text") or {}).get("tasks") or {}
    if tasks:
        item["llm_runs"] = [
            {"prompt": prompt, "success": record.get("status") == "done",
             **{key: record.get(key) for key in ("round_count", "invalid_rounds", "fallback_rounds")}}
            for prompt, record in ((task["prompt"], tasks.get(task["prompt"], {})) for task in plan.get("tasks", []))
        ]
    verify = (stages.get("verify") or {}).get("summary") or {}
    if verify:
        checks = verify.get("checks") or {}
        item["verify"] = {
            "passed": verify.get("passed"),
            "hard_failures": verify.get("hard_failures") or [],
            "warnings": verify.get("warnings") or [],
            "checks": {key: checks[key] for key in TRACKED_CHECKS if key in checks},
        }
    if state.get("output_file"):
        item["output_file"] = Path(state["output_file"]).name
    item["llm"] = state.get("llm") or {}

    status = result.get("status")
    if status == "done" and skipped_audio:
        item["status"] = "rerouted" if rerouted else "skipped"
        item["error"] = ("subtitle quality is critical; the route moves to Deepgram, which this run does not call"
                         if rerouted else "no usable subtitles; the route needs Deepgram, which this run does not call")
    elif status == "done":
        item["status"] = "ok"
    elif status == "failed" and result.get("reason") == "quality_hard_failure":
        item["status"] = "quality_failed"
    elif status == "unsupported":
        item["status"] = "unsupported"
        item["error"] = result.get("message", "")
    else:
        item["status"] = "error"
        item["failed_step"] = result.get("stage", "")
        item["error"] = f"{status} ({result.get('reason') or 'no reason'}): {result.get('message', '')}".rstrip(": ")
    return item


def run_video(video: dict, out_dir: Path, stage: str, allow_deepgram: bool, timeout: int) -> dict:
    vid = video["id"]
    url = video.get("url") or f"https://www.youtube.com/watch?v={vid}"
    vdir = out_dir / vid
    if vdir.exists():
        shutil.rmtree(vdir)
    (vdir / "output").mkdir(parents=True)
    env = dict(CMD_ENV or os.environ)
    env[WORK_ROOT_ENV] = str(out_dir / "work")
    env[OUTPUT_DIR_ENV] = str(vdir / "output")
    log = vdir / "run.log"
    state_path = out_dir / "work" / vid / "state.json"

    plan_first = stage == "plan" or not allow_deepgram
    plan_args = ["--stage", "plan"] if plan_first else []
    result = call_run(run_args(url, "--restart", *plan_args), log, env, timeout)
    blocked = []
    if result.get("status") == "needs_user" and result.get("fallback_track"):
        # YouTube refused the preferred track. Take the fallback as the agent-driven
        # flow used to, so the report stays comparable; Notes lists the blocked track.
        blocked = [f"{(result.get('preferred_track') or {}).get('track', '')}:{result.get('reason', '')}"]
        result = call_run(run_args(url, "--mode", result["fallback_track"]["mode"], *plan_args), log, env, timeout)
    state = read_state(state_path)
    if stage == "full" and plan_first and result.get("status") == "done" and not needs_deepgram(state):
        # The plan run stopped before the text stage; this call continues from there.
        result = call_run(run_args(url), log, env, timeout)
        state = read_state(state_path)

    (vdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if state_path.exists():
        shutil.copyfile(state_path, vdir / "state.json")
    return report_item(video, result, state, blocked)


def observed_case(item: dict) -> str:
    if item.get("status") == "unsupported":
        return "unsupported"
    source = item.get("source") or {}
    if source.get("src") == "deepgram":
        return "deepgram"
    lang = str(source.get("language", ""))
    kind = str(source.get("kind", ""))
    if not lang:
        return ""
    return track_case(lang, kind)


def source_notes(item: dict) -> str:
    """Short explanation of why a source may differ from what was preferred."""
    source = item.get("source") or {}
    notes = []
    if source.get("blocked"):
        notes.append("blocked " + ", ".join(source["blocked"]))
    if source.get("rerouted_from"):
        notes.append(f"rerouted from {source['rerouted_from']}")
    plan = item.get("plan") or {}
    if plan.get("reroute_recommended") and not source.get("rerouted_from"):
        notes.append(f"plan recommends {plan.get('reroute_target') or 'reroute'}")
    return "; ".join(notes)


def source_mismatch(item: dict) -> bool:
    """True when the pipeline picked a different source than the set expects."""
    expected, observed = item.get("expected_case", ""), observed_case(item)
    return bool(expected and observed and expected != observed)


def compare(current: dict, baseline: dict) -> list[str]:
    """Return human-readable differences between two reports."""
    base_by_id = {v["id"]: v for v in baseline.get("videos", [])}
    lines = []
    for item in current.get("videos", []):
        old = base_by_id.get(item["id"])
        if not old:
            lines.append(f"- {item['id']}: new in this run")
            continue
        diffs = []
        for key in ("status", "failed_step"):
            if item.get(key) != old.get(key):
                diffs.append(f"{key} {old.get(key)!r} -> {item.get(key)!r}")
        if observed_case(item) != observed_case(old):
            new_src, old_src = item.get("source") or {}, old.get("source") or {}
            flaky = (new_src.get("blocked") or old_src.get("blocked")) and not (
                new_src.get("rerouted_from") or old_src.get("rerouted_from"))
            diffs.append(f"source {observed_case(old)!r} -> {observed_case(item)!r}"
                         + (" (flaky: a preferred track was blocked by YouTube)" if flaky else ""))
        for key in ("video_path", "subtitle_quality_score", "reroute_recommended"):
            a, b = (old.get("plan") or {}).get(key), (item.get("plan") or {}).get(key)
            if a != b:
                diffs.append(f"plan.{key} {a!r} -> {b!r}")
        ov, nv = old.get("verify") or {}, item.get("verify") or {}
        if len(ov.get("hard_failures", [])) != len(nv.get("hard_failures", [])):
            diffs.append(f"hard_failures {len(ov.get('hard_failures', []))} -> {len(nv.get('hard_failures', []))}")
        if len(ov.get("warnings", [])) != len(nv.get("warnings", [])):
            diffs.append(f"warnings {len(ov.get('warnings', []))} -> {len(nv.get('warnings', []))}")
        for key in TRACKED_CHECKS:
            a, b = (ov.get("checks") or {}).get(key), (nv.get("checks") or {}).get(key)
            if a != b and a is not None and b is not None:
                if isinstance(a, float) or isinstance(b, float):
                    diffs.append(f"{key} {a:.3f} -> {b:.3f}")
                else:
                    diffs.append(f"{key} {a} -> {b}")
        lines.append(f"- {item['id']}: " + ("; ".join(diffs) if diffs else "no change"))
    return lines


def render_markdown(report: dict, diff_lines: list[str] | None) -> str:
    out = [
        f"# yt-transcript regression run {report['started_at']}",
        "",
        f"Commit `{report['git_commit']}` · stage `{report['stage']}` · "
        f"{report['summary']['ok']}/{report['summary']['total']} ok · "
        f"{report['summary']['source_mismatch']} source mismatch (⚠)",
        "",
        "| Video | Expected | Observed | Mode | Path | Sub quality | Verify | HF | Warn | Size ratio | Status | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in report["videos"]:
        plan = item.get("plan") or {}
        verify = item.get("verify") or {}
        checks = verify.get("checks") or {}
        ratio = checks.get("size_ratio_vs_raw")
        status = item["status"] + (f" ({item['failed_step']})" if item.get("failed_step") else "")
        out.append(
            f"| {item['id']} | {item.get('expected_case', '')} | {observed_case(item)}{' ⚠' if source_mismatch(item) else ''} | "
            f"{(item.get('source') or {}).get('mode', '')} | {plan.get('video_path', '')} | "
            f"{plan.get('subtitle_quality_band') or ''} {'' if plan.get('subtitle_quality_score') is None else plan['subtitle_quality_score']} | "
            f"{'' if not verify else ('pass' if verify.get('passed') else 'fail')} | "
            f"{len(verify.get('hard_failures', [])) if verify else ''} | {len(verify.get('warnings', [])) if verify else ''} | "
            f"{f'{ratio:.2f}' if isinstance(ratio, (int, float)) else ''} | {status} | {source_notes(item)} |"
        )
    problems = [i for i in report["videos"] if i.get("error") or (i.get("verify") or {}).get("hard_failures") or (i.get("verify") or {}).get("warnings")]
    if problems:
        out += ["", "## Details"]
        for item in problems:
            out.append(f"\n### {item['id']} {item.get('title', '')}")
            if item.get("error"):
                out.append(f"- error: {item['error']}")
            for hf in (item.get("verify") or {}).get("hard_failures", []):
                out.append(f"- hard failure: {hf}")
            for w in (item.get("verify") or {}).get("warnings", []):
                out.append(f"- warning: {w}")
    if diff_lines is not None:
        out += ["", f"## Compared with {report.get('baseline', '')}", *diff_lines]
    return "\n".join(out) + "\n"


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT_DIR,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT_DIR,
                               capture_output=True, text=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--videos", default=str(DEFAULT_VIDEOS), help="Video set JSON")
    parser.add_argument("--only", action="append", default=[], help="Run only this video id (repeatable)")
    parser.add_argument("--stage", choices=STAGES, default="full")
    parser.add_argument("--out-dir", default="", help="Default: /tmp/yt_regression/<timestamp>")
    parser.add_argument("--compare", default="", help="Earlier report.json to diff against")
    parser.add_argument("--no-deepgram", action="store_true", help="Skip videos that need audio transcription")
    parser.add_argument("--timeout", type=int, default=5400, help="Per-video timeout of each run call, in seconds")
    parser.add_argument("--config-path", default="", help="config.yaml for every run (default: the skill's own)")
    parser.add_argument("--fixtures", default="",
                        help="Replay recorded fixtures offline (default video set: <dir>/videos.json)")
    args = parser.parse_args()

    global CMD_ENV, FIXTURE_MODE
    if args.fixtures:
        CMD_ENV = fixture_env(Path(args.fixtures))
        FIXTURE_MODE = True
        if args.videos == str(DEFAULT_VIDEOS):
            args.videos = str(Path(args.fixtures) / "videos.json")

    video_set = json.loads(Path(args.videos).read_text(encoding="utf-8"))
    videos = video_set["videos"] if isinstance(video_set, dict) else video_set
    if args.only:
        videos = [v for v in videos if v["id"] in args.only]
        if not videos:
            print(f"No videos match --only {args.only}", file=sys.stderr)
            return 2

    started = datetime.now(timezone.utc)
    out_dir = Path(args.out_dir or f"/tmp/yt_regression/{started.strftime('%Y%m%dT%H%M%SZ')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if FIXTURE_MODE:
        write_fixture_config(out_dir / "fixture_config.yaml")
        CONFIG_ARGS[:] = ["--config-path", str(out_dir / "fixture_config.yaml")]
    elif args.config_path:
        CONFIG_ARGS[:] = ["--config-path", str(Path(args.config_path).expanduser().resolve())]

    results = []
    for video in videos:
        print(f"▶ {video['id']} ({video.get('label', '')})", file=sys.stderr)
        item = run_video(video, out_dir, args.stage, not args.no_deepgram, args.timeout)
        print(f"  {item['status']} {item.get('error', '')}".rstrip(), file=sys.stderr)
        results.append(item)

    report = {
        "format": "yt_transcript.regression_report/v1",
        "started_at": started.isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        "stage": args.stage,
        "videos": results,
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "quality_failed": sum(1 for r in results if r["status"] == "quality_failed"),
            "error": sum(1 for r in results if r["status"] == "error"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "rerouted": sum(1 for r in results if r["status"] == "rerouted"),
            "unsupported": sum(1 for r in results if r["status"] == "unsupported"),
            "source_mismatch": sum(1 for r in results if source_mismatch(r)),
        },
    }
    diff_lines = None
    if args.compare:
        report["baseline"] = args.compare
        diff_lines = compare(report, json.loads(Path(args.compare).read_text(encoding="utf-8")))

    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report, diff_lines)
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"Report: {out_dir / 'report.md'}", file=sys.stderr)
    return 0 if report["summary"]["error"] == 0 and report["summary"]["quality_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
