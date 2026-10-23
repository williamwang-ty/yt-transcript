#!/usr/bin/env python3
"""Stand-in for `yt-dlp` that serves recorded fixtures instead of YouTube.

scripts/download.sh runs unchanged with tests/e2e/bin first on PATH. Each
video's fixtures live in $YT_TRANSCRIPT_E2E_FIXTURES/<video_id>/:

    info.json        trimmed `yt-dlp -J` output (see record_fixtures.py)
    subs/<lang>.vtt  subtitle tracks that downloaded when the fixture was recorded

A call with a URL stands for a page extraction; a call with --load-info-json
reads the given file instead, as yt-dlp does. A subtitle language without a
recorded track fails like a blocked download, so download.sh moves on to its
next candidate as it would on YouTube. Audio downloads write a small
placeholder file; tests/e2e/fake_deepgram.py answers the transcription from
the recorded Deepgram response.

Switches for the tests (environment variables):

    FAKE_YT_DLP_RATE_LIMITED   comma-separated subtitle languages that fail with HTTP 429
    FAKE_YT_DLP_SESSION_LIMITED=1  every page extraction fails with yt-dlp's rate-limit message
    FAKE_YT_DLP_BOT_CHECK=1    every page extraction without cookies fails with the bot check
                               (=always: with cookies too)
    FAKE_YT_DLP_NO_PRESET=1    behave like a yt-dlp without `-t` (before mid-2025)
    FAKE_YT_DLP_LOG=<file>     append one JSON line per call; calls that ran while another
                               one was running are marked "overlap"
    FAKE_YT_DLP_DELAY=<sec>    take this long per call (widens the window for overlaps)
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

FIXTURES_ENV = "YT_TRANSCRIPT_E2E_FIXTURES"
RATE_LIMITED_ENV = "FAKE_YT_DLP_RATE_LIMITED"
SESSION_LIMITED_ENV = "FAKE_YT_DLP_SESSION_LIMITED"
BOT_CHECK_ENV = "FAKE_YT_DLP_BOT_CHECK"
NO_PRESET_ENV = "FAKE_YT_DLP_NO_PRESET"
LOG_ENV = "FAKE_YT_DLP_LOG"
DELAY_ENV = "FAKE_YT_DLP_DELAY"
# Options that take a value; everything else is a flag.
VALUE_OPTIONS = {
    "--sub-lang", "--sub-langs", "--sub-format", "-o", "--output", "-f", "--format", "--print",
    "--socket-timeout", "--retries", "--extractor-retries", "--cookies", "--cookies-from-browser",
    "-t", "--preset-alias", "--load-info-json",
}
# yt-dlp 2026.10.21's words, as kernel/sources/youtube.py has to recognise them.
SESSION_LIMIT_MESSAGE = (
    "This content isn't available, try again later. The current session has been rate-limited by YouTube for "
    "up to an hour. It is recommended to use `-t sleep` to add a delay between video requests to avoid "
    "exceeding the rate limit. For more information, refer to  "
    "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#this-content-isnt-available-try-again-later"
)
BOT_CHECK_MESSAGE = (
    "Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication. "
    "See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass "
    "cookies"
)


def fail(message: str, code: int = 1) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def video_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("youtu.be"):
        return parsed.path.strip("/")
    return (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]


def parse_args(argv: list[str]) -> tuple[dict, set, str]:
    options: dict = {}
    flags: set = set()
    positional = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in VALUE_OPTIONS and index + 1 < len(argv):
            options[arg] = argv[index + 1]
            index += 2
            continue
        if arg.startswith("-"):
            flags.add(arg)
        else:
            positional.append(arg)
        index += 1
    return options, flags, positional[-1] if positional else ""


def render_template(template: str, info: dict) -> str:
    out = template
    for key in ("id", "title", "duration", "upload_date", "channel", "ext"):
        out = out.replace(f"%({key})s", str(info.get(key, "NA")))
    return out


class CallLog:
    """One JSON line per call in $FAKE_YT_DLP_LOG; a lock file shows calls that overlapped."""

    def __init__(self, argv: list[str], kind: str, vid: str):
        self.path = os.environ.get(LOG_ENV, "")
        self.entry = {"pid": os.getpid(), "kind": kind, "video_id": vid, "args": argv, "start": time.time(),
                      "overlap": False}
        self.busy = None
        if self.path and kind != "probe":
            self.busy = open(f"{self.path}.busy", "a")
            try:
                fcntl.flock(self.busy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.entry["overlap"] = True
        delay = float(os.environ.get(DELAY_ENV) or 0)
        if delay and kind != "probe":
            time.sleep(delay)

    def close(self, code: int) -> int:
        if self.path:
            self.entry.update(end=time.time(), exit=code)
            with open(self.path, "a", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(json.dumps(self.entry) + "\n")
        if self.busy:
            self.busy.close()
        return code


def main(argv: list[str]) -> int:
    if os.environ.get(NO_PRESET_ENV) and ("-t" in argv or "--preset-alias" in argv):
        print("Usage: yt-dlp [OPTIONS] URL [URL...]\n\nyt-dlp: error: no such option: -t", file=sys.stderr)
        return 2
    if "--version" in argv or "--list-extractors" in argv:
        CallLog(argv, "probe", "").close(0)
        print("2026.08.19-fixture" if "--version" in argv else "youtube")
        return 0
    root = os.environ.get(FIXTURES_ENV, "")
    if not root:
        return fail(f"{FIXTURES_ENV} is not set")
    options, flags, url = parse_args(argv)
    if "--load-info-json" in options:
        try:
            loaded = json.loads(Path(options["--load-info-json"]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return fail(f"Cannot load info JSON: {error}")
        vid, kind = str(loaded.get("id") or ""), "load_info"
    else:
        vid, kind = video_id(url), "extract"
    log = CallLog(argv, kind, vid)
    return log.close(serve(vid, kind, options, flags, url))


def serve(vid: str, kind: str, options: dict, flags: set, url: str) -> int:
    vdir = Path(os.environ[FIXTURES_ENV]) / vid
    info_path = vdir / "info.json"
    if not vid or not info_path.exists():
        return fail(f"[youtube] {vid or url}: no fixture recorded for this video")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if kind == "extract":
        if os.environ.get(SESSION_LIMITED_ENV):
            return fail(f"[youtube] {vid}: {SESSION_LIMIT_MESSAGE}")
        bot_check = os.environ.get(BOT_CHECK_ENV, "")
        if bot_check == "always" or (bot_check and "--cookies-from-browser" not in options
                                     and "--cookies" not in options):
            return fail(f"[youtube] {vid}: {BOT_CHECK_MESSAGE}")

    if "-J" in flags or "--dump-json" in flags or "--dump-single-json" in flags:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    if "--print" in options:
        print(render_template(options["--print"], info))
        return 0

    output = options.get("-o") or options.get("--output") or ""
    if "--write-sub" in flags or "--write-auto-sub" in flags:
        lang = options.get("--sub-lang") or options.get("--sub-langs") or ""
        if lang in os.environ.get(RATE_LIMITED_ENV, "").split(","):
            return fail(f"Unable to download video subtitles for '{lang}': HTTP Error 429: Too Many Requests")
        track = vdir / "subs" / f"{lang}.vtt"
        if not track.exists():
            return fail(f"Unable to download video subtitles for '{lang}': HTTP Error 404: Not Found")
        target = Path(f"{output}.{lang}.vtt")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(track, target)
        print(f"[info] Writing video subtitles to: {target}")
        return 0
    if "-f" in options or "--format" in options:
        target = Path(render_template(output, {**info, "ext": "m4a"}))
        target.parent.mkdir(parents=True, exist_ok=True)
        # Placeholder bytes: fake_deepgram.py never reads the audio.
        target.write_bytes(b"fixture audio for " + vid.encode("ascii", "replace"))
        print(f"[download] Destination: {target}")
        return 0
    return fail(f"unsupported fake yt-dlp call: {' '.join(sys.argv[1:])}", 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
