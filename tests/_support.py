"""Shared support helpers for regression tests."""

import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

from kernel import config as kernel_config
from kernel.llm import agent_cli as _agent_cli
from kernel.llm import api as kernel_llm_api
from kernel.quality import verify as kernel_verify
from kernel.sources import deepgram as kernel_deepgram
from kernel.text import assemble as kernel_assemble
from kernel.text import chunking as kernel_chunking
from kernel.text import measure as kernel_measure
from kernel.text import normalize as kernel_normalize
from kernel.text import plan as kernel_plan
from kernel.text import prompting as kernel_prompting
from kernel.text.cleanup import subtitle as kernel_subtitle_cleanup

# Tests must not hand chunks to whatever agent CLI runs the suite: drop the
# host-agent markers so llm_backend=auto resolves to the mocked API.
for _agent in _agent_cli.KNOWN_AGENTS:
    for _var in _agent["env"]:
        os.environ.pop(_var, None)
for _var in ("AGENT", "AI_AGENT", _agent_cli.AGENT_CLI_ENV_VAR):
    os.environ.pop(_var, None)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

__all__ = [
    "http",
    "io",
    "json",
    "os",
    "shutil",
    "subprocess",
    "sys",
    "tempfile",
    "urllib",
    "unittest",
    "Path",
    "mock",
    "kernel_assemble",
    "kernel_config",
    "kernel_deepgram",
    "kernel_llm_api",
    "kernel_measure",
    "kernel_normalize",
    "kernel_plan",
    "kernel_prompting",
    "kernel_subtitle_cleanup",
    "kernel_verify",
    "PROJECT_ROOT",
    "FIXTURES_DIR",
    "chunk_plain_text",
    "tag_echo",
]


def chunk_plain_text(text: str, work_dir: Path, prompt_name: str = "cleanup_zh", **config) -> Path:
    """Chunk plain text into a text work dir the way the text stage of `run` does."""
    work_dir = Path(work_dir)
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    normalized = work_dir.parent / f"{work_dir.name}_normalized_document.json"
    normalized.write_text(json.dumps({"content": {"text": text}, "segments": []}, ensure_ascii=False),
                          encoding="utf-8")
    kernel_chunking.chunk_document(str(normalized), str(work_dir), prompt_name,
                                   {**kernel_config.default_config_values(), **config})
    return work_dir


def tag_echo(prompt: str) -> str:
    """Fake model answer for a window round: every numbered unit unchanged in one paragraph, then the end line."""
    units = re.findall(r"^(\[S\d+\] .*)$", prompt, re.MULTILINE)
    return "".join(units) + "\n\n<!--END-->"
