"""The `run` command: one deterministic pipeline from a YouTube URL to the article.

`run <url>` executes a fixed list of stages (metadata, source, normalize,
plan, text, merge, verify, assemble, cleanup). Each stage writes its outputs
into the video's work directory and is marked done in `state.json` only after
that, so rerunning the same command continues at the first unfinished stage.
Every call returns one result (`done`, `failed`, `needs_user`, `unsupported`,
`needs_agent_text`, `in_progress`) with a fixed exit code; the host agent acts
on it. SYSTEM_DESIGN.md section 6 describes the design.

Work directory: `/tmp/yt-transcript/<video_id>/`, or `$YT_TRANSCRIPT_WORK_ROOT/<video_id>/`.
`$YT_TRANSCRIPT_OUTPUT_DIR` replaces the configured `output_dir` (regression runs).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from kernel import subtitle_language as kernel_subtitle_language
from kernel.long_text import execution as kernel_execution
from kernel.long_text import window as kernel_window
from kernel.task_runtime import runtime as kernel_runtime
from kernel.task_runtime import state as kernel_state

STAGES = ("metadata", "source", "normalize", "plan", "text", "merge", "verify", "assemble", "cleanup")
PLAN_STAGE = "plan"
STATE_SCHEMA = 1

WORK_ROOT_ENV = "YT_TRANSCRIPT_WORK_ROOT"
OUTPUT_DIR_ENV = "YT_TRANSCRIPT_OUTPUT_DIR"
DEFAULT_WORK_ROOT = "/tmp/yt-transcript"

EXIT_CODES = {
    "done": 0,
    "failed": 1,
    "needs_user": 3,
    "unsupported": 4,
    "needs_agent_text": 5,
    "in_progress": 6,
}
USAGE_EXIT_CODE = 2

STATE_FILE = "state.json"
METADATA_FILE = "metadata.json"
CHAPTERS_FILE = "chapters.json"
SOURCE_DIR = "source"
SUBTITLE_CHECK_FILE = "source/subtitle_document.json"
DEEPGRAM_FILE = "source/deepgram.json"
RAW_TEXT_FILE = "raw_text.txt"
SEGMENTS_FILE = "segments.json"
NORMALIZED_FILE = "normalized_document.json"
PLAN_FILE = "plan.json"
TEXT_DIR = "text"
AGENT_TASK_DIR = "agent_task"
OPTIMIZED_FILE = "optimized.md"
QUALITY_FILE = "quality.json"
# Everything the cleanup stage removes once the article exists; state.json stays.
INTERMEDIATE_PATHS = (METADATA_FILE, CHAPTERS_FILE, SOURCE_DIR, RAW_TEXT_FILE, SEGMENTS_FILE,
                      NORMALIZED_FILE, PLAN_FILE, TEXT_DIR, AGENT_TASK_DIR, OPTIMIZED_FILE, QUALITY_FILE)
# Files a stage owns: removed when the stage has to start over because an
# earlier stage was redone. source/ keeps its downloads and Deepgram caches.
STAGE_OWNED_PATHS = {
    "metadata": (METADATA_FILE, CHAPTERS_FILE),
    "source": (RAW_TEXT_FILE, SEGMENTS_FILE, SUBTITLE_CHECK_FILE),
    "normalize": (NORMALIZED_FILE,),
    "plan": (PLAN_FILE,),
    "text": (TEXT_DIR, AGENT_TASK_DIR),
    "merge": (OPTIMIZED_FILE,),
    "verify": (QUALITY_FILE,),
}

SOURCE_LABELS = {"subtitles": "YouTube Subtitles", "deepgram": "Deepgram Transcription"}
LANGUAGE_NAMES = {"zh": "Chinese", "en": "English"}
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
DOWNLOAD_TIMEOUT_SEC = 1800
# scripts/update_yt_dlp.sh skips itself when this is "0" (the offline tests set it).
YT_DLP_UPDATE_ENV = "YT_TRANSCRIPT_YT_DLP_UPDATE"
YT_DLP_UPDATE_TIMEOUT_SEC = 300
DEEPGRAM_KEY_PLACEHOLDERS = {"", "your_api_key_here", "your_deepgram_api_key_here"}
# yt-dlp messages that only the user can resolve, checked in this order.
NEEDS_USER_PATTERNS = (
    ("cookies_required", ("confirm you're not a bot", "confirm you’re not a bot")),
    ("rate_limited", ("http error 429", "too many requests")),
    ("login_required", ("login_required", "sign in to confirm your age", "private video",
                        "members-only", "join this channel")),
)
NEEDS_USER_MESSAGES = {
    "cookies_required": (
        "YouTube asked to sign in to confirm this is not a bot, and the automatic Chrome-cookies retry did not "
        "help. Export youtube.com cookies (Netscape cookies.txt) from a logged-in browser, set "
        "yt_dlp_cookies_file in config.yaml, then rerun."
    ),
    "login_required": (
        "This video needs a signed-in YouTube account (private, members-only or age-restricted). Set "
        "yt_dlp_cookies_file or yt_dlp_cookies_from_browser in config.yaml for an account that can watch it, "
        "then rerun."
    ),
    "rate_limited": "YouTube is rate-limiting requests from this machine. Wait a while, then rerun.",
    "tool_missing": "yt-dlp is not installed or not on PATH. Install it (pip install -U yt-dlp), then rerun.",
    "deepgram_key_missing": (
        "This video needs audio transcription (no usable subtitles), but no Deepgram API key is configured. "
        "Set deepgram_api_key in config.yaml, then rerun."
    ),
    "output_dir_missing": "output_dir is not set in config.yaml. Set it to the folder for the articles, then rerun.",
}


class Stop(Exception):
    """Ends the current call with a result other than `done`."""

    def __init__(self, status: str, reason: str = "", message: str = "", **fields):
        super().__init__(message or reason or status)
        self.status = status
        self.reason = reason
        self.message = message
        self.fields = fields


class AgentTextNeeded(Exception):
    """No LLM backend is reachable; the agent has to answer one request."""

    def __init__(self, task: dict):
        super().__init__("agent text needed")
        self.task = task


class _StderrTail:
    """Pass stderr through and keep its last lines for failure messages."""

    def __init__(self, stream, keep: int = 12):
        self.stream = stream
        self.keep = keep
        self.lines: list[str] = []
        self._partial = ""

    def write(self, text):
        self.stream.write(text)
        self._partial += str(text)
        *complete, self._partial = self._partial.split("\n")
        self.lines.extend(line for line in complete if line.strip())
        del self.lines[:-self.keep]
        return len(text)

    def flush(self):
        self.stream.flush()

    def last(self, count: int = 3) -> str:
        lines = self.lines + ([self._partial] if self._partial.strip() else [])
        return " | ".join(line.strip() for line in lines[-count:])

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _utils():
    import yt_transcript_utils as utils
    return utils


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def work_root() -> Path:
    return Path(os.environ.get(WORK_ROOT_ENV) or DEFAULT_WORK_ROOT).expanduser()


def parse_video_id(url: str) -> str:
    """Return the video id of a YouTube URL (or a bare 11-character id), else ''."""
    text = str(url or "").strip()
    if len(text) == 11 and VIDEO_ID_PATTERN.fullmatch(text):
        return text
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    candidate = ""
    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parts[0] if parts else ""
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        candidate = (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]
        if not candidate and len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v", "e"}:
            candidate = parts[1]
    return candidate if VIDEO_ID_PATTERN.fullmatch(candidate or "") else ""


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def language_family(code: str | None) -> str:
    return kernel_subtitle_language.language_family(code)


def iso_date(upload_date: str) -> str:
    """yyyymmdd -> yyyy-mm-dd, the date format for output filenames."""
    text = str(upload_date or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def classify_download_error(text: str) -> str:
    """Name the needs_user reason a yt-dlp failure calls for, or ''."""
    lowered = str(text or "").lower()
    for reason, needles in NEEDS_USER_PATTERNS:
        if any(needle in lowered for needle in needles):
            return reason
    return ""


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink()


def read_state(work_dir: Path) -> dict:
    try:
        payload = json.loads((work_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("schema") == STATE_SCHEMA else {}


def _new_state(video_id: str, url: str) -> dict:
    return {
        "schema": STATE_SCHEMA,
        "video_id": video_id,
        "url": url,
        "status": "running",
        "created_at": _now(),
        "updated_at": _now(),
        "overrides": {"source": None, "mode": None},
        "metadata": {},
        "route": {},
        "stages": {},
        "llm": {},
        "warnings": [],
        "last_result": {},
        "output_file": None,
    }


def run_finished(state: dict) -> bool:
    """The article was written, cleanup ran, and the article still exists."""
    output_file = str(state.get("output_file") or "")
    return state.get("stages", {}).get("cleanup", {}).get("status") == "done" and bool(output_file) \
        and Path(output_file).is_file()


def _skill_command() -> str:
    return f"python3 {shlex.quote(str(_utils()._skill_root() / 'yt_transcript_utils.py'))}"


class _Run:
    """One call of `run` for one video, holding the work dir lock."""

    def __init__(self, *, video_id: str, url: str, work_dir: Path, source: str | None, mode: str | None,
                 from_stage: str | None, stop_after: str | None, max_seconds: float | None,
                 restart: bool, config_path: str | None, stderr_tail: _StderrTail | None):
        self.video_id = video_id
        self.url = url
        self.work_dir = work_dir
        self.requested_overrides = {"source": source, "mode": mode}
        self.from_stage = from_stage
        self.stop_after = stop_after
        self.max_seconds = max_seconds
        self.restart = restart
        self.config_path = config_path
        self.stderr_tail = stderr_tail
        self.started = time.monotonic()
        self.progress_made = False
        self.state: dict = {}
        self.config: dict = {}

    # -- paths, state, results ------------------------------------------------

    def path(self, name: str) -> Path:
        return self.work_dir / name

    @property
    def overrides(self) -> dict:
        return self.state["overrides"]

    @property
    def plan_only(self) -> bool:
        return self.stop_after == PLAN_STAGE

    def save(self) -> None:
        self.state["updated_at"] = _now()
        kernel_state.atomic_write_text(self.path(STATE_FILE),
                                       json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def next_command(self) -> str:
        parts = [_skill_command(), "run", shlex.quote(self.url)]
        if self.plan_only:
            parts += ["--stage", PLAN_STAGE]
        if self.max_seconds:
            parts += ["--max-seconds", f"{self.max_seconds:g}"]
        if self.config_path:
            parts += ["--config-path", shlex.quote(str(self.config_path))]
        return " ".join(parts)

    def deadline_passed(self) -> bool:
        return bool(self.max_seconds) and self.progress_made and \
            time.monotonic() - self.started >= float(self.max_seconds)

    def finish(self, status: str, **fields) -> dict:
        result = {"status": status, "video_id": self.video_id, **fields}
        if status not in ("done", "unsupported") or self.plan_only:
            result.setdefault("next_command", self.next_command())
        result["work_dir"] = str(self.work_dir)
        result["seconds"] = round(time.monotonic() - self.started, 1)
        self.state["status"] = "planned" if (status == "done" and self.plan_only and not run_finished(self.state)) \
            else status
        self.state["last_result"] = {key: result[key] for key in ("status", "stage", "reason", "message")
                                     if key in result}
        self.state["last_result"]["at"] = _now()
        self.save()
        return result

    def done_result(self) -> dict:
        route = self.state.get("route", {})
        return self.finish(
            "done",
            output_file=self.state.get("output_file"),
            title=self.state.get("metadata", {}).get("title", ""),
            route={key: route.get(key) for key in ("source", "language", "track", "track_kind", "mode")},
            warnings=list(self.state.get("warnings", [])),
            llm=self.state.get("llm", {}),
        )

    # -- main loop ------------------------------------------------------------

    def execute(self) -> dict:
        if self.restart:
            for child in self.work_dir.iterdir():
                if child.name != kernel_runtime.RUNTIME_OWNER_FILENAME:
                    _remove(child)
        self.state = read_state(self.work_dir) or _new_state(self.video_id, self.url)
        self.state["url"] = self.url
        try:
            self.config = _utils().load_config(self.config_path, allow_missing=self.config_path is None)
        except SystemExit:
            return self.finish("failed", stage="", reason="config_error", retryable=False,
                               message=(self.stderr_tail.last(1) if self.stderr_tail else "")
                               or f"Cannot read the config file {self.config_path}")

        changed = [key for key, value in self.requested_overrides.items()
                   if value is not None and value != self.overrides.get(key)]
        previous_overrides = dict(self.overrides)
        for key in changed:
            self.overrides[key] = self.requested_overrides[key]
        from_stage = self.from_stage
        if changed:
            _log(f"ℹ️ Override changed ({', '.join(changed)}); redoing the source and every later stage.")
            from_stage = "source" if not from_stage or STAGES.index(from_stage) > STAGES.index("source") \
                else from_stage

        if not from_stage and run_finished(self.state):
            return self.done_result()
        if self.state.get("output_file") and not run_finished(self.state) \
                and self.state.get("stages", {}).get("cleanup", {}).get("status") == "done":
            _log("ℹ️ The article from the earlier run is gone; starting over.")
            from_stage = STAGES[0]
        if from_stage:
            for name in STAGES[STAGES.index(from_stage):]:
                self.invalidate(name)
        self.save()

        upstream_ran = False
        for name in STAGES:
            if self.plan_only and STAGES.index(name) > STAGES.index(PLAN_STAGE):
                return self.plan_result()
            record = self.state["stages"].get(name, {})
            if record.get("status") == "done" and not upstream_ran and self.outputs_exist(record):
                continue
            if upstream_ran or record.get("status") == "done":
                self.invalidate(name)
            if self.deadline_passed():
                return self.finish("in_progress", stage=name, progress=self.progress())
            try:
                self.run_stage(name)
            except Stop as stop:
                if stop.status == "plan_stop":
                    return self.plan_result()
                if stop.reason == "override_not_applicable":
                    # Remember only overrides that apply, or a plain rerun would keep failing.
                    self.state["overrides"] = previous_overrides
                return self.finish(stop.status, stage=name, reason=stop.reason, message=stop.message,
                                   **stop.fields)
            upstream_ran = True
            self.progress_made = True
        return self.done_result()

    def outputs_exist(self, record: dict) -> bool:
        return all(Path(output).exists() if os.path.isabs(output) else self.path(output).exists()
                   for output in record.get("outputs", []))

    def invalidate(self, name: str) -> None:
        """Forget a stage's result and remove the files it owns."""
        for relative in STAGE_OWNED_PATHS.get(name, ()):
            _remove(self.path(relative))
        self.state["stages"].pop(name, None)
        if name == "source":
            self.state["route"] = {}
        if name == "text":
            self.state["llm"] = {}
        if name == "assemble":
            # The article itself stays; the next assemble overwrites it.
            self.state["output_file"] = None

    def run_stage(self, name: str) -> None:
        record = self.state["stages"].setdefault(name, {})
        record.update({"status": "running", "started_at": _now()})
        self.save()
        _log(f"▶ {name}")
        started = time.monotonic()
        try:
            getattr(self, f"stage_{name}")(record)
        except Stop as stop:
            record["status"] = "pending" if stop.status == "plan_stop" else stop.status
            record["seconds"] = round(time.monotonic() - started, 1)
            self.save()
            raise
        except AgentTextNeeded:
            raise
        except (Exception, SystemExit) as error:  # noqa: BLE001 - every failure becomes a `failed` result
            if isinstance(error, SystemExit):
                message = (self.stderr_tail.last() if self.stderr_tail else "") or f"exited {error.code}"
            else:
                message = f"{type(error).__name__}: {error}"
            record["status"] = "failed"
            record["seconds"] = round(time.monotonic() - started, 1)
            self.save()
            raise Stop("failed", f"{name}_error", message, retryable=False) from error
        record["status"] = "done"
        record["seconds"] = round(time.monotonic() - started, 1)
        record["finished_at"] = _now()
        self.save()

    def plan_result(self) -> dict:
        plan = self.state.get("stages", {}).get("plan", {}).get("summary", {})
        route = self.state.get("route", {})
        fields = {"stage": PLAN_STAGE, "route": route, "warnings": list(self.state.get("warnings", []))}
        if plan:
            fields["plan"] = plan
        if route.get("source") == "deepgram" and self.state["stages"].get("source", {}).get("status") != "done":
            fields["message"] = "The route needs Deepgram transcription, which --stage plan does not run."
        return self.finish("done", **fields)

    def progress(self) -> dict:
        text = self.state.get("stages", {}).get("text", {})
        return {"stages_done": [name for name in STAGES
                                if self.state.get("stages", {}).get(name, {}).get("status") == "done"],
                "text_tasks": text.get("tasks", {})}

    # -- downloads --------------------------------------------------------------

    def download(self, mode: str, *, subtitle_mode: str = "") -> dict:
        """Run `download.sh <url> <mode>` and return its JSON; Stop on failure."""
        if not shutil.which("yt-dlp"):
            raise Stop("needs_user", "tool_missing", NEEDS_USER_MESSAGES["tool_missing"])
        env = dict(os.environ)
        env.update({
            "YT_TRANSCRIPT_INFO_JSON": str(self.path(METADATA_FILE)),
            "YT_TRANSCRIPT_DOWNLOAD_DIR": str(self.path(SOURCE_DIR)),
            "YT_TRANSCRIPT_SUBTITLE_MODE": subtitle_mode,
        })
        self.path(SOURCE_DIR).mkdir(parents=True, exist_ok=True)
        script = _utils()._skill_root() / "scripts" / "download.sh"
        try:
            proc = subprocess.run(["bash", str(script), self.url, mode], capture_output=True, text=True,
                                  timeout=DOWNLOAD_TIMEOUT_SEC, env=env)
        except subprocess.TimeoutExpired:
            raise Stop("failed", "download_error", f"download.sh {mode} timed out after {DOWNLOAD_TIMEOUT_SEC}s",
                       retryable=True)
        if proc.stderr:
            sys.stderr.write(proc.stderr if proc.stderr.endswith("\n") else proc.stderr + "\n")
        if proc.returncode != 0:
            reason = classify_download_error(proc.stderr)
            if reason:
                raise Stop("needs_user", reason, NEEDS_USER_MESSAGES[reason])
            tail = [line.strip() for line in (proc.stderr or proc.stdout).splitlines() if line.strip()][-3:]
            raise Stop("failed", "download_error", f"download.sh {mode} exited {proc.returncode}: "
                       + " | ".join(tail), retryable=True)
        out = proc.stdout.strip()
        try:
            return json.loads(out[out.index("{"):])
        except ValueError:
            raise Stop("failed", "download_error", f"download.sh {mode} printed no JSON", retryable=True)

    def update_yt_dlp(self) -> None:
        """Update yt-dlp when a newer release exists; YouTube keeps breaking old ones.

        Best effort (the script checks GitHub at most hourly and never fails),
        run once per video before its metadata is fetched.
        """
        if os.environ.get(YT_DLP_UPDATE_ENV) == "0" or not shutil.which("yt-dlp"):
            return
        script = _utils()._skill_root() / "scripts" / "update_yt_dlp.sh"
        try:
            proc = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                                  timeout=YT_DLP_UPDATE_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired):
            _log("⚠️ The yt-dlp update check did not finish; continuing with the installed version.")
            return
        for line in (proc.stdout + proc.stderr).splitlines():
            if line.strip():
                _log(line)

    def load_info(self) -> dict:
        try:
            payload = json.loads(self.path(METADATA_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # -- stages -----------------------------------------------------------------

    def stage_metadata(self, record: dict) -> None:
        _remove(self.path(METADATA_FILE))
        self.update_yt_dlp()
        summary = self.download("metadata")
        if summary.get("video_id") and summary["video_id"] != self.video_id:
            raise Stop("failed", "download_error",
                       f"yt-dlp resolved video id {summary['video_id']!r}, expected {self.video_id!r}")
        self.state["metadata"] = {
            "title": str(summary.get("title") or ""),
            "channel": str(summary.get("channel") or ""),
            "upload_date": str(summary.get("upload_date") or ""),
            "duration": int(summary.get("duration") or 0),
        }
        outputs = []
        info = self.load_info()
        if info:
            outputs.append(METADATA_FILE)
            chapters = [c for c in (info.get("chapters") or []) if isinstance(c, dict)]
            self.path(CHAPTERS_FILE).write_text(
                json.dumps({"has_chapters": bool(chapters), "chapters": chapters}, ensure_ascii=False),
                encoding="utf-8")
            outputs.append(CHAPTERS_FILE)
        record["outputs"] = outputs

    def subtitle_info(self) -> dict:
        info = self.load_info()
        if info:
            return kernel_subtitle_language.build_subtitle_info(info, video_id=self.video_id)
        return self.download("subtitle-info")

    def source_payload(self, source: str, language: str) -> dict:
        """What the normalizer needs to know about the source (the old state.md fields)."""
        meta = self.state["metadata"]
        return {
            "type": "youtube" if source == "subtitles" else "deepgram",
            "url": self.url,
            "title": meta.get("title", "").replace("\n", " "),
            "channel": meta.get("channel", "").replace("\n", " "),
            "upload_date": meta.get("upload_date", ""),
            "duration": str(meta.get("duration", 0)),
            "source_language": language,
            "subtitle_source": SOURCE_LABELS[source],
        }

    def stage_source(self, record: dict) -> None:
        sub_info = self.subtitle_info()
        source_language = str(sub_info.get("source_language") or "")
        route = {
            "source_language": source_language,
            "language_signal": sub_info.get("source_language_signal", ""),
            "source": "",
            "language": "",
            "track": None,
            "track_kind": None,
            "mode": "",
            "subtitle_quality": None,
            "blocked_tracks": [],
            "fallback_used": False,
            "rerouted_from": None,
            "reason": "",
        }
        self.state["route"] = route
        candidates = [c for c in sub_info.get("listed_candidates", []) if isinstance(c, dict)]
        if not candidates and sub_info.get("source_language_supported") is False:
            raise Stop("unsupported", "unsupported_language",
                       sub_info.get("unsupported_reason") or f"Spoken language {source_language!r} is not supported",
                       language=source_language)

        want_source, want_mode = self.overrides.get("source"), self.overrides.get("mode")
        usable = [c for c in candidates if not want_mode or c.get("mode") == want_mode]
        spoken_mode = kernel_subtitle_language.MODE_BY_FAMILY.get(source_language)
        if want_mode and not usable and (want_source == "subtitles" or (spoken_mode and spoken_mode != want_mode)):
            raise Stop("failed", "override_not_applicable",
                       f"--mode {want_mode} cannot be honored: no trusted subtitle track gives a {want_mode} "
                       f"article for this {LANGUAGE_NAMES.get(source_language, source_language or 'unknown-language')} video",
                       retryable=False)
        if want_source == "subtitles" and not usable:
            raise Stop("failed", "override_not_applicable",
                       "--source subtitles cannot be honored: this video has no trusted Chinese or English "
                       "subtitle track", retryable=False)

        if usable and want_source != "deepgram":
            if self.subtitle_route(route, sub_info, want_mode):
                return self.record_source(record)
        elif want_source == "deepgram":
            route["reason"] = "--source deepgram"
        else:
            route["reason"] = "no trusted subtitle track in the spoken language"
        self.transcribe(route, source_language or "auto")
        self.record_source(record)

    def subtitle_route(self, route: dict, sub_info: dict, want_mode: str | None) -> bool:
        """Take the subtitle track; return False when its quality calls for Deepgram instead."""
        subs = self.download("subtitles", subtitle_mode=want_mode or "")
        refused = [c for c in subs.get("blocked_candidates", []) or []
                   if c.get("failure_reason") in {"rate_limited", "bot_verification"}]
        route["blocked_tracks"] = [f"{c.get('language', '')}:{c.get('failure_reason', 'blocked')}"
                                   for c in subs.get("blocked_candidates", []) or []]
        language = str(subs.get("selected_source_language") or "")
        kind = str(subs.get("selected_source_kind") or "")
        mode = str(subs.get("resolved_mode") or sub_info.get("mode") or "")
        if refused and not want_mode:
            first = refused[0]
            reason = "rate_limited" if first.get("failure_reason") == "rate_limited" else "cookies_required"
            raise Stop(
                "needs_user", reason,
                f"YouTube refused the preferred subtitle track {first.get('language')} "
                f"({first.get('source_kind')}, {first.get('failure_reason')}); only {language} ({kind}) could be "
                f"downloaded, which gives a {mode} article. Rerun later for {first.get('language')}, or accept "
                f"{language} by rerunning with --mode {mode}.",
                preferred_track={"track": first.get("language"), "track_kind": first.get("source_kind"),
                                 "mode": first.get("mode")},
                fallback_track={"track": language, "track_kind": kind, "mode": mode},
                accept_command=f"{self.next_command()} --mode {mode}",
            )
        vtt = Path(str(subs.get("selected_source_vtt") or ""))
        if not vtt.is_file():
            raise Stop("failed", "download_error", "download.sh subtitles returned no subtitle file", retryable=True)
        utils = _utils()
        self.path(RAW_TEXT_FILE).write_text(utils.parse_vtt(str(vtt)) + "\n", encoding="utf-8")
        self.path(SEGMENTS_FILE).write_text(
            json.dumps(utils.parse_vtt_segments(str(vtt), language=language), ensure_ascii=False),
            encoding="utf-8")
        route.update({
            "source": "subtitles",
            "language": language,
            "track": language,
            "track_kind": kind,
            "mode": mode,
            "fallback_used": bool(subs.get("fallback_used")),
        })

        check = utils.build_normalized_document(
            document_id=self.video_id,
            raw_text_path=str(self.path(RAW_TEXT_FILE)),
            segments_path=str(self.path(SEGMENTS_FILE)),
            normalized_document_path=str(self.path(SUBTITLE_CHECK_FILE)),
            source_payload=self.source_payload("subtitles", language),
            workflow_payload={"mode": mode},
        )
        if not check.get("passed"):
            raise Stop("failed", "source_error", "; ".join(check.get("hard_failures", [])), retryable=False)
        routing = utils._build_source_routing_signal("youtube", mode, check.get("subtitle_quality", {}))
        score, band = routing["subtitle_quality_score"], routing["subtitle_quality_band"]
        route["subtitle_quality"] = {"score": score, "band": band}
        family = LANGUAGE_NAMES.get(language_family(language), language)
        spoken = LANGUAGE_NAMES.get(route["source_language"], route["source_language"] or "unknown-language")
        route["reason"] = f"{spoken} video with {'human' if kind == 'manual' else 'ASR'} {family} subtitles" \
            + (f" (preferred track blocked: {', '.join(route['blocked_tracks'])})" if route["blocked_tracks"] else "")
        if routing["reroute_recommended"] and routing["reroute_target"] == "deepgram" \
                and self.overrides.get("source") != "subtitles":
            route["rerouted_from"] = {"track": language, "track_kind": kind, "mode": mode,
                                      "subtitle_quality": route["subtitle_quality"]}
            route["reason"] = f"subtitle quality {band} ({score}); rerouted to Deepgram"
            _log(f"ℹ️ Subtitle quality is {band} ({score}); transcribing the audio with Deepgram instead.")
            for name in (RAW_TEXT_FILE, SEGMENTS_FILE):
                _remove(self.path(name))
            return False
        return True

    def transcribe(self, route: dict, language: str) -> None:
        route.update({"source": "deepgram", "track": None, "track_kind": None, "subtitle_quality": None,
                      "language": language_family(language) if language != "auto" else "",
                      "mode": kernel_subtitle_language.MODE_BY_FAMILY.get(language_family(language), "")})
        if self.plan_only:
            raise Stop("plan_stop")
        api_key = str(self.config.get("deepgram_api_key") or "").strip()
        if api_key in DEEPGRAM_KEY_PLACEHOLDERS:
            raise Stop("needs_user", "deepgram_key_missing", NEEDS_USER_MESSAGES["deepgram_key_missing"])
        record = self.state["stages"]["source"]
        audio_file = Path(str(record.get("audio_file") or ""))
        if not record.get("audio_file") or not audio_file.is_file():
            audio = self.download("audio")
            audio_file = Path(str(audio.get("audio_file") or ""))
            if not audio_file.is_file():
                raise Stop("failed", "download_error", "download.sh audio returned no audio file", retryable=True)
            record["audio_file"] = str(audio_file)
            self.save()
        utils = _utils()
        try:
            result = utils.transcribe_deepgram(
                str(audio_file),
                language=language,
                config_path=self.config_path,
                api_key=api_key,
                output_json=str(self.path(DEEPGRAM_FILE)),
                output_text=str(self.path(RAW_TEXT_FILE)),
                output_segments=str(self.path(SEGMENTS_FILE)),
                resume_existing_chunks=True,
            )
        except (Exception, SystemExit) as error:  # noqa: BLE001
            message = (self.stderr_tail.last() if self.stderr_tail and isinstance(error, SystemExit) else "") \
                or f"{type(error).__name__}: {error}"
            raise Stop("failed", "deepgram_error", message, retryable=True) from error
        detected = str(result.get("language") or language)
        family = language_family(detected)
        if family not in kernel_subtitle_language.MODE_BY_FAMILY:
            raise Stop("unsupported", "unsupported_language",
                       f"Deepgram detected the spoken language {detected!r}, which is not supported yet; "
                       "only Chinese and English videos can be transcribed", language=detected)
        mode = kernel_subtitle_language.MODE_BY_FAMILY[family]
        want_mode = self.overrides.get("mode")
        if want_mode and want_mode != mode:
            raise Stop("failed", "override_not_applicable",
                       f"--mode {want_mode} cannot be honored: the audio is {LANGUAGE_NAMES[family]}, "
                       f"which gives a {mode} article", retryable=False)
        route.update({"language": detected, "mode": mode,
                      "deepgram": {"speaker_count": result.get("speaker_count"),
                                   "chunk_count": result.get("chunk_count")}})

    def record_source(self, record: dict) -> None:
        raw = self.path(RAW_TEXT_FILE)
        if not raw.is_file() or not raw.read_text(encoding="utf-8").strip():
            raise Stop("failed", "source_error", "the source produced no text", retryable=False)
        self.state["raw_chars"] = len(raw.read_text(encoding="utf-8"))
        record["outputs"] = [RAW_TEXT_FILE, SEGMENTS_FILE] if self.path(SEGMENTS_FILE).is_file() else [RAW_TEXT_FILE]

    def stage_normalize(self, record: dict) -> None:
        route = self.state["route"]
        result = _utils().build_normalized_document(
            document_id=self.video_id,
            raw_text_path=str(self.path(RAW_TEXT_FILE)),
            segments_path=str(self.path(SEGMENTS_FILE)),
            normalized_document_path=str(self.path(NORMALIZED_FILE)),
            source_payload=self.source_payload(route["source"], route["language"]),
            workflow_payload={"mode": route["mode"]},
        )
        if not result.get("passed"):
            raise Stop("failed", "normalize_error", "; ".join(result.get("hard_failures", [])), retryable=False)
        result.pop("cleanup_diagnostics", None)
        record["result"] = result
        record["outputs"] = [NORMALIZED_FILE]

    def stage_plan(self, record: dict) -> None:
        route = self.state["route"]
        utils = _utils()
        plan = utils.plan_text_operations(
            video_id=self.video_id,
            duration=int(self.state["metadata"].get("duration") or 0),
            mode=route["mode"],
            source="youtube" if route["source"] == "subtitles" else "deepgram",
            normalization=self.state["stages"]["normalize"]["result"],
            raw_text_path=str(self.path(RAW_TEXT_FILE)),
            segments_path=str(self.path(SEGMENTS_FILE)),
            work_dir=str(self.path(TEXT_DIR)),
            structured_text=str(self.path(TEXT_DIR) / "structured.md"),
            optimized_text=str(self.path(OPTIMIZED_FILE)),
            config=self.config,
        )
        self.path(PLAN_FILE).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        tasks = []
        for index, operation in enumerate(plan.get("operations", [])):
            prompt = operation["prompt"]
            ratio = utils._get_task_output_ratio(prompt, self.config)
            write_tokens = max(1, int(self.config.get("window_write_tokens")
                                      or kernel_window.DEFAULT_WINDOW_WRITE_TOKENS))
            tasks.append({
                "prompt": prompt,
                "input_key": operation.get("input_key") or ("raw_path" if index == 0 else "processed_path"),
                "extra_instruction": operation.get("extra_instruction", ""),
                "estimated_rounds": max(1, math.ceil(plan.get("estimated_input_tokens", 0) * ratio / write_tokens)),
            })
        if not tasks:
            raise Stop("failed", "plan_error", "the plan has no text tasks", retryable=False)
        record["summary"] = {
            "tasks": tasks,
            "video_path": plan.get("video_path"),
            "routing_reason": plan.get("routing_reason"),
            "estimated_input_tokens": plan.get("estimated_input_tokens"),
            "subtitle_quality_score": plan.get("subtitle_quality_score"),
            "subtitle_quality_band": plan.get("subtitle_quality_band"),
            "reroute_recommended": plan.get("reroute_recommended"),
            "reroute_target": plan.get("reroute_target"),
            "warnings": plan.get("warnings", []),
        }
        self.add_warnings(plan.get("warnings", []))
        record["outputs"] = [PLAN_FILE]

    def stage_text(self, record: dict) -> None:
        text_dir = self.path(TEXT_DIR)
        tasks = self.state["stages"]["plan"]["summary"]["tasks"]
        utils = _utils()
        if not (text_dir / "manifest.json").is_file():
            # YouTube chapters become outline hints on the long path, as in the
            # agent-driven long path. A short text has no outline, and chapter
            # cuts would only split its sentences at the chapter times.
            chapters = self.path(CHAPTERS_FILE)
            has_chapters = False
            if chapters.is_file() and self.state["stages"]["plan"]["summary"].get("video_path") == "long":
                try:
                    has_chapters = bool(json.loads(chapters.read_text(encoding="utf-8")).get("has_chapters"))
                except (OSError, json.JSONDecodeError, AttributeError):
                    has_chapters = False
            utils.chunk_document(str(self.path(NORMALIZED_FILE)), str(text_dir), prompt_name=tasks[0]["prompt"],
                                 config_path=self.config_path,
                                 chapters_path=str(chapters) if has_chapters else "")
        handoff = AgentHandoff(self.path(AGENT_TASK_DIR))
        handoff.collect()
        task_records = record.setdefault("tasks", {})
        for task in tasks:
            prompt = task["prompt"]
            task_record = task_records.setdefault(prompt, {"status": "pending"})
            if task_record.get("status") == "done":
                continue
            if self.deadline_passed():
                raise Stop("in_progress", "", "", progress=self.progress())
            task_record["status"] = "running"
            self.save()
            rounds_seen = {"count": 0}

            def stop_check(prompt=prompt, task_record=task_record, rounds_seen=rounds_seen) -> str:
                # Called before every window round: refresh progress, and stop
                # once the time budget is spent and this call has done something.
                rounds_seen["count"] += 1
                if rounds_seen["count"] > 1:
                    self.progress_made = True
                self.update_task_progress(task_record, task["input_key"])
                self.update_llm_usage()
                self.save()
                return "paused" if self.deadline_passed() else ""

            try:
                result = kernel_execution.process_chunks(
                    str(text_dir),
                    prompt,
                    extra_instruction=task.get("extra_instruction", ""),
                    config_path=self.config_path,
                    input_key=task["input_key"],
                    stop_check=stop_check,
                    agent_handoff=handoff.request,
                )
            except AgentTextNeeded as needed:
                task_record["status"] = "needs_agent_text"
                self.update_task_progress(task_record, task["input_key"])
                self.update_llm_usage()
                raise Stop(
                    "needs_agent_text", "no_llm_backend",
                    "No LLM backend is reachable (no host agent CLI answered and no API is configured). Answer "
                    "the request in task.prompt_file exactly as the model would, write only the answer to "
                    "task.output_file, then run next_command.",
                    task={**needed.task, "name": prompt},
                ) from None
            task_record.update({key: result.get(key) for key in (
                "long_text_mode", "total_chunks", "processed_count", "failed_count", "round_count",
                "fallback_rounds", "invalid_rounds", "outline") if key in result})
            self.update_task_progress(task_record, task["input_key"])
            self.update_llm_usage()
            self.add_warnings(result.get("warnings", []))
            if result.get("success"):
                task_record["status"] = "done"
                self.progress_made = True
                self.save()
                continue
            status = str(result.get("status") or "")
            if status == "paused":
                task_record["status"] = "running"
                raise Stop("in_progress", "", "", progress=self.progress())
            task_record["status"] = "failed"
            error_type = str(result.get("error") or "")
            message = str(result.get("message") or result.get("aborted_reason") or "text processing failed")
            reason = "locked" if "ownership" in message.lower() else \
                ("llm_timeout" if "timeout" in error_type.lower() else "llm_error")
            raise Stop("failed", reason, message, retryable=True)
        handoff.clear()
        record["outputs"] = [TEXT_DIR]

    def update_task_progress(self, task_record: dict, input_key: str) -> None:
        window = kernel_window.load_window_state(str(self.path(TEXT_DIR)), input_key)
        if window:
            task_record["cursor"] = window.get("cursor", 0)
            task_record["units"] = window.get("sentence_count", 0)
            task_record["rounds"] = len(window.get("rounds", []))

    def update_llm_usage(self) -> None:
        self.state["llm"] = llm_usage(self.path(TEXT_DIR))

    def stage_merge(self, record: dict) -> None:
        result = _utils().merge_content(str(self.path(TEXT_DIR)), str(self.path(OPTIMIZED_FILE)))
        if not result.get("success", False):
            raise Stop("failed", "merge_error", str(result.get("message") or result.get("error") or
                                                    "merge-content did not succeed"), retryable=False)
        record["outputs"] = [OPTIMIZED_FILE]

    def stage_verify(self, record: dict) -> None:
        result = _utils().verify_quality(
            str(self.path(OPTIMIZED_FILE)),
            raw_text_path=str(self.path(RAW_TEXT_FILE)),
            bilingual=self.state["route"]["mode"] == "bilingual",
            work_dir=str(self.path(TEXT_DIR)),
        )
        self.path(QUALITY_FILE).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        record["summary"] = {key: result.get(key) for key in ("passed", "hard_failures", "warnings", "checks")}
        record["outputs"] = [QUALITY_FILE]
        if not result.get("passed"):
            raise Stop("failed", "quality_hard_failure", "; ".join(result.get("hard_failures", [])),
                       retryable=False, hard_failures=result.get("hard_failures", []))
        self.add_warnings(result.get("warnings", []))

    def stage_assemble(self, record: dict) -> None:
        output_dir = os.environ.get(OUTPUT_DIR_ENV) or str(self.config.get("output_dir") or "")
        if not output_dir.strip():
            raise Stop("needs_user", "output_dir_missing", NEEDS_USER_MESSAGES["output_dir_missing"])
        meta = self.state["metadata"]
        utils = _utils()
        date = iso_date(meta.get("upload_date", ""))
        title = utils.sanitize_filename(str(meta.get("title") or "")).strip() or self.video_id
        output_file = Path(output_dir).expanduser() / (f"{date}. {title}.md" if date else f"{title}.md")
        mode = self.state["route"]["mode"]
        utils.assemble_final(
            str(self.path(OPTIMIZED_FILE)),
            str(output_file),
            title=str(meta.get("title") or ""),
            source=self.url,
            channel=str(meta.get("channel") or ""),
            date=str(meta.get("upload_date") or ""),
            created=date,
            duration=int(meta.get("duration") or 0),
            transcript_source=SOURCE_LABELS[self.state["route"]["source"]],
            bilingual=mode == "bilingual",
        )
        self.state["output_file"] = str(output_file.absolute())
        record["outputs"] = [self.state["output_file"]]

    def stage_cleanup(self, record: dict) -> None:
        for relative in INTERMEDIATE_PATHS:
            _remove(self.path(relative))
        record["outputs"] = []

    def add_warnings(self, warnings) -> None:
        for warning in warnings or []:
            if warning and warning not in self.state["warnings"]:
                self.state["warnings"].append(str(warning))


class AgentHandoff:
    """Hand LLM requests to the host agent when no backend is reachable.

    A request is written to `prompt.md`; the agent writes its answer to
    `output.txt` and reruns `run`. On the rerun the answer moves into
    `answers/<sha256 of the prompt>.txt`, and the same prompt gets it back
    (once per process, so a retry of an invalid answer is handed over again).
    Answers stay until the text stage finishes, which lets a multi-part
    outline collect one part per rerun.
    """

    def __init__(self, task_dir: Path):
        self.dir = task_dir
        self.answers = task_dir / "answers"
        self.used: set[str] = set()

    def collect(self) -> None:
        try:
            task = json.loads((self.dir / "task.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        output = self.dir / "output.txt"
        if not output.is_file() or not output.read_text(encoding="utf-8").strip():
            return
        self.answers.mkdir(parents=True, exist_ok=True)
        os.replace(output, self.answers / f"{task['sha256']}.txt")
        for name in ("task.json", "prompt.md"):
            _remove(self.dir / name)

    def request(self, prompt_text: str, max_output_tokens: int) -> dict:
        digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        answer = self.answers / f"{digest}.txt"
        if digest not in self.used and answer.is_file():
            self.used.add(digest)
            return {"text": answer.read_text(encoding="utf-8"), "finish_reason": "", "latency_ms": 0,
                    "request_url": "agent:handoff"}
        self.dir.mkdir(parents=True, exist_ok=True)
        _remove(self.dir / "output.txt")
        (self.dir / "prompt.md").write_text(prompt_text, encoding="utf-8")
        (self.dir / "task.json").write_text(json.dumps({"sha256": digest, "created_at": _now(),
                                                        "max_output_tokens": max_output_tokens}),
                                            encoding="utf-8")
        raise AgentTextNeeded({"prompt_file": str(self.dir / "prompt.md"),
                               "output_file": str(self.dir / "output.txt")})

    def clear(self) -> None:
        _remove(self.dir)


def llm_usage(text_dir: Path) -> dict:
    """Requests and estimated tokens of the window runs in a text work dir."""
    usage = {"backend": "", "requests": 0, "input_tokens": 0, "output_tokens": 0, "estimated_tokens": True}
    for input_key in (kernel_window.RAW_STAGE, kernel_window.PROCESSED_STAGE):
        window = kernel_window.load_window_state(str(text_dir), input_key)
        if not window:
            continue
        backend = window.get("llm_backend") or {}
        if backend.get("backend") == "cli":
            usage["backend"] = f"{backend.get('agent') or 'agent'}-cli"
        elif backend.get("backend") == "api":
            usage["backend"] = "api"
        elif backend.get("backend") == "none":
            usage["backend"] = "agent-handoff"
        for request in (window.get("outline") or {}).get("requests", []):
            usage["requests"] += 1
            usage["input_tokens"] += int(request.get("prompt_tokens") or 0)
        for round_record in window.get("rounds", []):
            usage["requests"] += 1
            usage["input_tokens"] += int(round_record.get("prompt_tokens") or 0)
            usage["output_tokens"] += int(round_record.get("output_tokens") or 0)
    return usage


def run(url: str, *, source: str | None = None, mode: str | None = None, from_stage: str | None = None,
        stage: str | None = None, max_seconds: float | None = None, restart: bool = False,
        config_path: str | None = None, stderr_tail: _StderrTail | None = None) -> tuple[dict, int]:
    """Run (or continue) the pipeline for one video; return (result, exit code)."""
    video_id = parse_video_id(url)
    if not video_id:
        return {"status": "usage_error", "message": f"Not a YouTube video URL: {url!r}"}, USAGE_EXIT_CODE
    if max_seconds is not None and max_seconds <= 0:
        return {"status": "usage_error", "message": "--max-seconds must be positive"}, USAGE_EXIT_CODE
    work_dir = work_root() / video_id
    work_dir.mkdir(parents=True, exist_ok=True)
    ownership = kernel_runtime.acquire_runtime_ownership(str(work_dir), "run")
    if not ownership.get("success"):
        active = ownership.get("active_owner") or {}
        result = {
            "status": "failed",
            "video_id": video_id,
            "stage": "",
            "reason": "locked",
            "message": f"Another run of this video is active (pid {active.get('pid', '?')}); wait for it to finish.",
            "retryable": True,
            "work_dir": str(work_dir),
        }
        return result, EXIT_CODES["failed"]
    try:
        job = _Run(video_id=video_id, url=canonical_url(video_id), work_dir=work_dir, source=source, mode=mode,
                   from_stage=from_stage, stop_after=stage, max_seconds=max_seconds, restart=restart,
                   config_path=config_path, stderr_tail=stderr_tail)
        result = job.execute()
    finally:
        kernel_runtime.release_runtime_ownership(str(work_dir), ownership.get("owner_id", ""))
    return result, EXIT_CODES.get(result["status"], EXIT_CODES["failed"])


def _status_entry(work_dir: Path, state: dict) -> dict:
    stages = state.get("stages", {})
    pending = next((name for name in STAGES if stages.get(name, {}).get("status") != "done"), "")
    return {
        "video_id": state.get("video_id", work_dir.name),
        "url": state.get("url", ""),
        "title": state.get("metadata", {}).get("title", ""),
        "status": state.get("status", ""),
        "finished": run_finished(state),
        "next_stage": "" if run_finished(state) else pending,
        "last_result": state.get("last_result", {}),
        "output_file": state.get("output_file"),
        "updated_at": state.get("updated_at", ""),
        "work_dir": str(work_dir),
        "active": kernel_runtime.read_runtime_ownership(str(work_dir)).get("held", False),
    }


def status(video_id: str = "") -> tuple[dict, int]:
    """Without an id: the runs under the work root. With an id: that video's state."""
    root = work_root()
    if video_id:
        if not VIDEO_ID_PATTERN.fullmatch(video_id):
            return {"status": "usage_error", "message": f"Not a video id: {video_id!r}"}, USAGE_EXIT_CODE
        work_dir = root / video_id
        state = read_state(work_dir)
        if not state:
            return {"video_id": video_id, "found": False, "work_dir": str(work_dir)}, 0
        entry = _status_entry(work_dir, state)
        entry["found"] = True
        entry["state"] = state
        if not entry["finished"]:
            entry["next_command"] = f"{_skill_command()} run {shlex.quote(state.get('url', ''))}"
        return entry, 0
    unfinished, finished = [], []
    for state_path in sorted(root.glob(f"*/{STATE_FILE}")) if root.is_dir() else []:
        state = read_state(state_path.parent)
        if not state:
            continue
        entry = _status_entry(state_path.parent, state)
        if entry["finished"]:
            finished.append(entry)
        else:
            entry["next_command"] = f"{_skill_command()} run {shlex.quote(state.get('url', ''))}"
            unfinished.append(entry)
    return {"work_root": str(root), "has_unfinished": bool(unfinished), "unfinished": unfinished,
            "finished": finished}, 0


def cleanup(video_id: str, force: bool = False) -> tuple[dict, int]:
    """Remove a video's work directory; an unfinished run needs `force`."""
    if not VIDEO_ID_PATTERN.fullmatch(video_id or ""):
        return {"status": "usage_error", "message": f"Not a video id: {video_id!r}"}, USAGE_EXIT_CODE
    work_dir = work_root() / video_id
    if not work_dir.exists():
        return {"success": True, "removed": False, "video_id": video_id, "message": "no work directory"}, 0
    if kernel_runtime.read_runtime_ownership(str(work_dir)).get("held"):
        return {"success": False, "removed": False, "video_id": video_id, "reason": "locked",
                "message": "A run of this video is active; stop it first."}, 1
    if not run_finished(read_state(work_dir)) and not force:
        return {"success": False, "removed": False, "video_id": video_id, "reason": "unfinished",
                "message": "The run is unfinished; rerun it to continue, or pass --force to discard it."}, 1
    shutil.rmtree(work_dir)
    return {"success": True, "removed": True, "video_id": video_id, "work_dir": str(work_dir)}, 0
