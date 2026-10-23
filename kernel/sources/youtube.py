"""What every `run` on this machine shares when it talks to YouTube.

YouTube meters requests per machine (its IP, or the account in the cookies),
not per process, so these controls are machine-wide and live outside the work
root, in the machine lock directory (`kernel.locks.machine_dir`):

- `youtube.lock`: every yt-dlp call, and the yt-dlp update, runs while holding
  it. One yt-dlp at a time means yt-dlp's own pacing (`-t sleep`) paces the
  whole machine, with no numbers of ours.
- `youtube_rate_limit.json`: when YouTube last refused this machine as a whole,
  and yt-dlp's words for it. A run that began before that moment does not call
  YouTube again; a run that begins later tries once. Nothing to clear and no
  cooldown of ours: yt-dlp knows what the limit looks like, YouTube knows when
  it ends.

`classify` is the one place that reads yt-dlp's error text.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from kernel.files import atomic_write_text
from kernel.locks import LOCK_DIR_ENV, machine_dir as lock_dir  # noqa: F401 - LOCK_DIR_ENV for callers

LOCK_FILE = "youtube.lock"
REFUSAL_FILE = "youtube_rate_limit.json"

# What a failed yt-dlp call means, from its output; checked in this order.
PROBLEMS = (
    # yt-dlp 2026: "This content isn't available, try again later. The current
    # session has been rate-limited by YouTube for up to an hour."
    ("rate_limited", ("available, try again later", "rate-limited by youtube", "http error 429",
                      "too many requests")),
    ("bot_check", ("confirm you're not a bot", "confirm you’re not a bot")),
    ("login_required", ("login_required", "sign in to confirm your age", "private video", "members-only",
                        "join this channel")),
)
# Problems with this machine's standing at YouTube rather than with one video.
SESSION_PROBLEMS = ("rate_limited", "bot_check")


def lock_path() -> Path:
    return lock_dir() / LOCK_FILE


def classify(text: str) -> str:
    """Name the problem in yt-dlp's output (a PROBLEMS key), or '' when it is none of them."""
    lowered = str(text or "").lower()
    for problem, needles in PROBLEMS:
        if any(needle in lowered for needle in needles):
            return problem
    return ""


def said(text: str) -> str:
    """yt-dlp's own line about the problem: the line naming it, else the last ERROR line, else the last line."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    problem = classify(text)
    for line in lines:
        if problem and classify(line) == problem:
            return line
    errors = [line for line in lines if line.startswith("ERROR")]
    return (errors or lines or [""])[-1]


def last_refusal() -> dict:
    """The machine-wide refusal on record, or {}."""
    try:
        record = json.loads((lock_dir() / REFUSAL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) and isinstance(record.get("time"), (int, float)) else {}


def refusal_since(started: float) -> dict:
    """The refusal a run that began at `started` (epoch seconds) must respect, or {}."""
    record = last_refusal()
    return record if record and record["time"] >= started else {}


def record_refusal(problem: str, message: str, video_id: str) -> dict:
    """Note that YouTube refused this machine now; call it while holding the YouTube lock."""
    record = {"problem": problem, "message": message, "video_id": video_id, "time": time.time(),
              "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    atomic_write_text(lock_dir() / REFUSAL_FILE, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return record
