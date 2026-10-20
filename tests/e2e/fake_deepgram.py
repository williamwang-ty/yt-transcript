#!/usr/bin/env python3
"""Run `yt_transcript_utils.py` with Deepgram answered from a recorded response.

Usage: fake_deepgram.py run <url> [run args]

Everything runs as usual except the Deepgram HTTP request:
`_call_deepgram_api` returns $YT_TRANSCRIPT_E2E_FIXTURES/<video_id>/deepgram.json,
where <video_id> is the audio file's stem (download.sh names it so).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from kernel import cli as kernel_cli  # noqa: E402
from kernel.sources import deepgram as kernel_deepgram  # noqa: E402

FIXTURES_ENV = "YT_TRANSCRIPT_E2E_FIXTURES"


def recorded_response(audio_path: str, *args, **kwargs) -> dict:
    vid = Path(audio_path).name.split(".", 1)[0]
    path = Path(os.environ[FIXTURES_ENV]) / vid / "deepgram.json"
    if not path.exists():
        raise SystemExit(f"fake_deepgram: no recorded Deepgram response at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    kernel_deepgram._call_deepgram_api = recorded_response
    sys.argv = [str(ROOT_DIR / "yt_transcript_utils.py"), *sys.argv[1:]]
    result = kernel_cli.main()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
