#!/usr/bin/env python3
"""Live regression runner for yt-transcript.

Runs the scripted pipeline on a fixed set of real YouTube videos and writes a
report so behaviour can be compared before and after a change.

Stages (pick with --stage):
    source    metadata, subtitle selection (or Deepgram), raw text + segments
    plan      source + validate-state + plan-optimization (no paid API calls
              unless a video needs Deepgram)
    full      plan + chunked LLM optimization + merge + verify-quality (default)

The full stage always runs the chunked path (chunk-document/segments/text,
process-chunks, merge-content), even when plan-optimization says
video_path=short. The short path is in-agent prompting, which a script cannot
reproduce; the chunked path is its scripted equivalent and uses the same prompts.

Usage:
    python3 scripts/run_regression.py
    python3 scripts/run_regression.py --stage plan --only H_peyIigkkc
    python3 scripts/run_regression.py --compare /tmp/yt_regression/<old>/report.json

Each run writes <out-dir>/report.json, <out-dir>/report.md and one folder per
video with every intermediate file and command output.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
UTILS = ROOT_DIR / "yt_transcript_utils.py"
DOWNLOAD_SH = ROOT_DIR / "scripts" / "download.sh"
DEFAULT_VIDEOS = ROOT_DIR / "tests" / "live_regression" / "videos.json"
STAGES = ("source", "plan", "full")

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


class StepError(RuntimeError):
    """A pipeline step failed; the message is shown in the report."""


def run_cmd(args: list[str], log_path: Path, timeout: int) -> str:
    """Run a command, save stdout/stderr to log_path, return stdout."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            args, cwd=ROOT_DIR, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"$ {' '.join(args)}\nTIMEOUT after {timeout}s\n{exc.stderr or ''}", encoding="utf-8")
        raise StepError(f"{Path(args[1]).name if len(args) > 1 else args[0]} timed out after {timeout}s")
    elapsed = time.monotonic() - started
    log_path.write_text(
        f"$ {' '.join(args)}\n# exit={proc.returncode} elapsed={elapsed:.1f}s\n"
        f"## stdout\n{proc.stdout}\n## stderr\n{proc.stderr}",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise StepError(f"{log_path.stem} exited {proc.returncode}: {' | '.join(tail)}")
    return proc.stdout


def run_json(args: list[str], log_path: Path, timeout: int) -> dict:
    """Run a command whose stdout ends with a JSON object."""
    out = run_cmd(args, log_path, timeout).strip()
    start = out.find("{")
    if start < 0:
        raise StepError(f"{log_path.stem} printed no JSON")
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        # Some helpers print progress lines before the JSON payload.
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise StepError(f"{log_path.stem} printed invalid JSON")


def utils(*args: str) -> list[str]:
    return [sys.executable, str(UTILS), *args]


def download(url: str, mode: str) -> list[str]:
    return ["bash", str(DOWNLOAD_SH), url, mode]


def write_state(path: Path, fields: dict) -> None:
    lines = ["# State"]
    lines += [f"{key}: {value}" for key, value in fields.items()]
    lines += ["", "# Progress", "step: 3", "last_action: regression source ready"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_video(video: dict, out_dir: Path, stage: str, allow_deepgram: bool, timeout: int) -> dict:
    vid = video["id"]
    url = video.get("url") or f"https://www.youtube.com/watch?v={vid}"
    vdir = out_dir / vid
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    logs = vdir / "logs"
    logs.mkdir()

    result: dict = {
        "id": vid,
        "label": video.get("label", ""),
        "expected_case": video.get("expected_case", ""),
        "status": "running",
        "failed_step": "",
        "error": "",
        "timings_sec": {},
    }

    def step(name: str):
        result["_step"] = name
        result["_t0"] = time.monotonic()

    def done():
        name = result.pop("_step")
        result["timings_sec"][name] = round(time.monotonic() - result.pop("_t0"), 1)

    try:
        step("metadata")
        meta = run_json(download(url, "metadata"), logs / "metadata.log", timeout)
        result.update(title=meta.get("title", ""), duration=meta.get("duration", 0))
        done()

        step("subtitle_info")
        sub_info = run_json(download(url, "subtitle-info"), logs / "subtitle_info.log", timeout)
        result["subtitle_info"] = {
            k: sub_info.get(k)
            for k in ("has_any", "has_manual", "has_auto", "chinese_available",
                      "english_available", "preferred_source_language", "mode")
        }
        done()

        raw_text = vdir / f"{vid}_raw_text.txt"
        segments = vdir / f"{vid}_segments.json"

        if sub_info.get("has_any"):
            step("subtitles")
            subs = run_json(download(url, "subtitles"), logs / "subtitles.log", timeout)
            vtt = subs.get("selected_source_vtt") or ""
            if not vtt:
                raise StepError("subtitles: no selected_source_vtt")
            language = subs.get("selected_source_language") or ""
            mode = subs.get("resolved_mode") or sub_info.get("mode") or ""
            result["source"] = {
                "src": "youtube",
                "language": language,
                "kind": subs.get("selected_source_kind", ""),
                "mode": mode,
                "fallback_used": subs.get("fallback_used"),
                "blocked_candidates": subs.get("blocked_candidates", []),
            }
            done()

            step("parse_vtt")
            raw_text.write_text(run_cmd(utils("parse-vtt", vtt), logs / "parse_vtt.log", timeout), encoding="utf-8")
            seg_json = run_json(utils("parse-vtt-segments", vtt, "--language", language),
                                logs / "parse_vtt_segments.log", timeout)
            segments.write_text(json.dumps(seg_json, ensure_ascii=False), encoding="utf-8")
            done()
            subtitle_source = "YouTube Subtitles"
            src = "youtube"
        else:
            if not allow_deepgram:
                result["status"] = "skipped"
                result["error"] = "no usable subtitles and --no-deepgram set"
                return result
            language = video.get("audio_language") or "en"
            mode = "chinese" if language.startswith("zh") else "bilingual"

            step("audio")
            audio = run_json(download(url, "audio"), logs / "audio.log", timeout)
            done()

            step("deepgram")
            dg = run_json(
                utils("transcribe-deepgram", audio["audio_file"], "--language", language,
                      "--output-json", str(vdir / f"{vid}_deepgram.json"),
                      "--output-text", str(raw_text),
                      "--output-segments", str(segments)),
                logs / "deepgram.log", max(timeout, 900),
            )
            result["source"] = {
                "src": "deepgram",
                "language": language,
                "kind": "audio",
                "mode": mode,
                "speaker_count": dg.get("speaker_count"),
                "chunk_count": dg.get("chunk_count"),
            }
            done()
            subtitle_source = "Deepgram Transcription"
            src = "deepgram"

        result["raw_chars"] = len(raw_text.read_text(encoding="utf-8")) if raw_text.exists() else 0
        if stage == "source":
            result["status"] = "ok"
            return result

        state_path = vdir / f"{vid}_state.md"
        chunks_dir = vdir / "chunks"
        write_state(state_path, {
            "vid": vid,
            "url": url,
            "title": str(meta.get("title", "")).replace("\n", " "),
            "channel": str(meta.get("channel", "")).replace("\n", " "),
            "upload_date": meta.get("upload_date", ""),
            "duration": meta.get("duration", 0),
            "output_dir": str(vdir),
            "mode": mode,
            "src": src,
            "source_language": language,
            "subtitle_source": subtitle_source,
            "language_mode": mode,
            "work_dir": str(chunks_dir),
            "raw_text": str(raw_text),
            "segments_path": str(segments),
        })

        step("plan")
        validation = run_json(utils("validate-state", str(state_path), "--stage", "post-source"),
                              logs / "validate_state.log", timeout)
        if validation.get("hard_failures"):
            raise StepError(f"validate-state: {validation['hard_failures']}")
        plan = run_json(utils("plan-optimization", str(state_path)), logs / "plan.log", timeout)
        (vdir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if plan.get("hard_failures"):
            raise StepError(f"plan-optimization: {plan['hard_failures']}")
        result["plan"] = {
            k: plan.get(k)
            for k in ("video_path", "routing_reason", "subtitle_quality_score",
                      "subtitle_quality_band", "reroute_recommended", "reroute_target")
        }
        result["plan"]["prompts"] = [op.get("prompt") for op in plan.get("operations", [])]
        result["plan"]["warnings"] = plan.get("warnings", [])
        done()

        if stage == "plan":
            result["status"] = "ok"
            return result

        operations = plan.get("operations", [])
        if not operations:
            raise StepError("plan-optimization returned no operations")
        first_prompt = operations[0]["prompt"]

        step("chunk")
        normalization = plan.get("normalization", {}) or {}
        doc_path = normalization.get("normalized_document_path", "")
        if normalization.get("materialized") and doc_path and Path(doc_path).exists():
            chunk_args = utils("chunk-document", doc_path, str(chunks_dir), "--prompt", first_prompt)
        elif segments.exists():
            chunk_args = utils("chunk-segments", str(segments), str(chunks_dir), "--prompt", first_prompt)
        else:
            chunk_args = utils("chunk-text", str(raw_text), str(chunks_dir), "--prompt", first_prompt)
        run_cmd(chunk_args, logs / "chunk.log", timeout)
        done()

        step("llm")
        llm_runs = []
        for index, op in enumerate(operations):
            input_key = op.get("input_key") or ("raw_path" if index == 0 else "processed_path")
            args = utils("process-chunks", str(chunks_dir), "--prompt", op["prompt"], "--input-key", input_key)
            if op.get("extra_instruction"):
                args += ["--extra-instruction", op["extra_instruction"]]
            if input_key == "raw_path":
                args.append("--auto-replan")
            pc = run_json(args, logs / f"process_chunks_{index}_{op['prompt']}.log", max(timeout, 3600))
            llm_runs.append({"prompt": op["prompt"], **{k: pc.get(k) for k in (
                "success", "total_chunks", "processed_count", "failed_count", "replan_required")}})
            if not pc.get("success") or pc.get("replan_required"):
                raise StepError(f"process-chunks {op['prompt']}: failed_count={pc.get('failed_count')} "
                                f"replan_required={pc.get('replan_required')}")
        result["llm_runs"] = llm_runs
        done()

        step("merge")
        optimized = vdir / f"{vid}_optimized.txt"
        run_cmd(utils("merge-content", str(chunks_dir), str(optimized)), logs / "merge.log", timeout)
        done()

        step("verify")
        vq_args = utils("verify-quality", str(optimized), "--raw-text", str(raw_text), "--work-dir", str(chunks_dir))
        if mode == "bilingual":
            vq_args.append("--bilingual")
        vq = run_json(vq_args, logs / "verify_quality.log", timeout)
        (vdir / "verify_quality.json").write_text(json.dumps(vq, ensure_ascii=False, indent=2), encoding="utf-8")
        checks = vq.get("checks", {})
        result["verify"] = {
            "passed": vq.get("passed"),
            "hard_failures": vq.get("hard_failures", []),
            "warnings": vq.get("warnings", []),
            "checks": {k: checks[k] for k in TRACKED_CHECKS if k in checks},
        }
        done()
        result["status"] = "ok" if vq.get("passed") else "quality_failed"
    except StepError as exc:
        result["status"] = "error"
        result["failed_step"] = result.get("_step", "")
        result["error"] = str(exc)
    finally:
        result.pop("_step", None)
        result.pop("_t0", None)
    return result


def observed_case(item: dict) -> str:
    source = item.get("source") or {}
    if source.get("src") == "deepgram":
        return "deepgram"
    lang = str(source.get("language", ""))
    kind = str(source.get("kind", ""))
    if not lang:
        return ""
    family = "zh" if lang.startswith("zh") else ("en" if lang.startswith("en") else lang)
    return f"{family}_{kind}" if kind else family


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
            diffs.append(f"source {observed_case(old)!r} -> {observed_case(item)!r}")
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
        "| Video | Expected | Observed | Mode | Path | Sub quality | Verify | HF | Warn | Size ratio | Status |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
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
            f"{plan.get('subtitle_quality_band', '')} {plan.get('subtitle_quality_score', '') if plan else ''} | "
            f"{'' if not verify else ('pass' if verify.get('passed') else 'fail')} | "
            f"{len(verify.get('hard_failures', [])) if verify else ''} | {len(verify.get('warnings', [])) if verify else ''} | "
            f"{f'{ratio:.2f}' if isinstance(ratio, (int, float)) else ''} | {status} |"
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
    parser.add_argument("--timeout", type=int, default=300, help="Per-command timeout in seconds")
    args = parser.parse_args()

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
