#!/usr/bin/env python3
"""Stand-in for `yt-dlp` that serves recorded fixtures instead of YouTube.

scripts/download.sh runs unchanged with tests/e2e/bin first on PATH. Each
video's fixtures live in $YT_TRANSCRIPT_E2E_FIXTURES/<video_id>/:

    info.json        trimmed `yt-dlp -J` output (see record_fixtures.py)
    subs/<lang>.vtt  subtitle tracks that downloaded when the fixture was recorded

A subtitle language without a recorded track fails like a blocked download,
so download.sh moves on to its next candidate as it would on YouTube. Audio
downloads write a small placeholder file; tests/e2e/fake_deepgram.py answers
the transcription from the recorded Deepgram response.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.parse
from pathlib import Path

FIXTURES_ENV = "YT_TRANSCRIPT_E2E_FIXTURES"
# Options that take a value; everything else is a flag.
VALUE_OPTIONS = {
    "--sub-lang", "--sub-langs", "--sub-format", "-o", "--output", "-f", "--format", "--print",
    "--socket-timeout", "--retries", "--extractor-retries", "--cookies", "--cookies-from-browser",
}


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


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print("2026.08.19-fixture")
        return 0
    root = os.environ.get(FIXTURES_ENV, "")
    if not root:
        return fail(f"{FIXTURES_ENV} is not set")
    options, flags, url = parse_args(argv)
    vid = video_id(url)
    vdir = Path(root) / vid
    info_path = vdir / "info.json"
    if not vid or not info_path.exists():
        return fail(f"[youtube] {vid or url}: no fixture recorded for this video")
    info = json.loads(info_path.read_text(encoding="utf-8"))

    if "-J" in flags or "--dump-json" in flags or "--dump-single-json" in flags:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    if "--print" in options:
        print(render_template(options["--print"], info))
        return 0
    if "--list-subs" in flags:
        return fail("--list-subs is not recorded; download.sh should use -J metadata")

    output = options.get("-o") or options.get("--output") or ""
    if "--write-sub" in flags or "--write-auto-sub" in flags:
        lang = options.get("--sub-lang") or options.get("--sub-langs") or ""
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
    return fail(f"unsupported fake yt-dlp call: {' '.join(argv)}", 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
