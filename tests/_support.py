"""Shared support helpers for regression tests."""

import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

import yt_transcript_utils as utils
from kernel.long_text import agent_cli as _agent_cli

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
    "utils",
    "PROJECT_ROOT",
    "FIXTURES_DIR",
]
