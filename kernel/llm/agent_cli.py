"""Host-agent CLI backend for chunk LLM calls.

With `llm_backend: auto`, chunk requests go to the command-line tool of the
agent that is running the skill (Claude Code, Codex, ...), so long videos use
the same model family and subscription as the calling agent. When that CLI
cannot be used and the paid `llm_*` API is configured, `llm_api_fallback`
decides: ask the user (the default), use the API, or never use it.
`llm_backend: api` always uses the API.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from kernel.config import (
    DEFAULT_CLI_MAX_RETRIES,
    DEFAULT_CLI_PROBE_TIMEOUT_SEC,
    DEFAULT_CLI_TIMEOUT_SEC,
    normalize_backend_mode,
)
from kernel.llm.api import LLMRequestError, backoff_delay

AGENT_CLI_ENV_VAR = "YT_TRANSCRIPT_AGENT_CLI"
PROMPT_PLACEHOLDER = "{prompt}"
CLI_PROBE_PROMPT = "Reply with OK only."


class ApiFallbackNeeded(Exception):
    """The agent CLI cannot be used, the API is configured, and the user has to say whether to use it.

    `case` is "cli_unavailable" (no CLI found, or its probe failed) or
    "cli_failed" (a request still failed after its retries).
    """

    def __init__(self, case: str, detail: str, agent: str = ""):
        super().__init__(detail)
        self.case = case
        self.detail = detail
        self.agent = agent


# Ordered: the first agent whose marker variable is set wins. Markers are the
# variables each host sets on the shell commands it runs. Commands either read
# the prompt from stdin or take it as the `{prompt}` argument.
KNOWN_AGENTS = (
    {
        "name": "claude-code",
        "aliases": ("claude", "claude-code", "claudecode"),
        "env": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
        # --tools "": a text-rewrite request needs no tools; without this the
        # agent sometimes explores the working directory before answering.
        "commands": (["claude", "-p", "--output-format", "text", "--tools", ""],),
        # A nested `claude -p` that inherits these can hang; drop them for the child.
        "strip_env": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    },
    {
        "name": "codex",
        "aliases": ("codex",),
        "env": ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_CI"),
        "commands": (["codex", "exec", "--skip-git-repo-check", "-"],),
    },
    {
        "name": "opencode",
        "aliases": ("opencode",),
        "env": ("OPENCODE", "OPENCODE_PID", "OPENCODE_BIN_PATH"),
        "commands": (["opencode", "run", PROMPT_PLACEHOLDER],),
    },
    {
        "name": "pi",
        "aliases": ("pi", "pi-coding-agent"),
        "env": ("PI_CODING_AGENT",),
        "commands": (["pi", "-p", "--no-tools", "--no-session"],),
    },
    {
        "name": "gemini",
        "aliases": ("gemini", "gemini-cli"),
        "env": ("GEMINI_CLI",),
        "commands": (["gemini", "-p", PROMPT_PLACEHOLDER],),
    },
    {
        "name": "cursor",
        "aliases": ("cursor", "cursor-agent", "cursor-cli"),
        "env": ("CURSOR_AGENT", "CURSOR_SANDBOX"),
        # --trust: headless runs refuse untrusted directories otherwise. Only the
        # `cursor-agent` name is used: a bare `agent` can belong to other tools.
        "commands": (["cursor-agent", "-p", "--trust", "--output-format", "text", PROMPT_PLACEHOLDER],),
    },
)


def find_agent(name: str) -> dict | None:
    """Return the known agent whose name or alias matches."""
    key = str(name or "").strip().lower()
    if not key:
        return None
    for agent in KNOWN_AGENTS:
        if key == agent["name"] or key in agent["aliases"]:
            return agent
    return None


def detect_host_agent(env: dict | None = None) -> str:
    """Name the agent running this process from its marker variables, or ''."""
    env = os.environ if env is None else env
    for agent in KNOWN_AGENTS:
        if any(str(env.get(var, "") or "").strip() for var in agent["env"]):
            return agent["name"]
    # Generic convention some hosts follow: AGENT / AI_AGENT name the agent,
    # sometimes with a version suffix (e.g. "claude-code_2-1-300_agent").
    for var in ("AGENT", "AI_AGENT"):
        agent = _match_agent_label(str(env.get(var, "") or ""))
        if agent:
            return agent["name"]
    return ""


def _match_agent_label(label: str) -> dict | None:
    """Match an AGENT-style label, ignoring a trailing version/suffix."""
    label = label.strip().lower()
    if not label:
        return None
    best = None
    for agent in KNOWN_AGENTS:
        for alias in (agent["name"], *agent["aliases"]):
            if label == alias or (label.startswith(alias) and label[len(alias)] in "_@-/ ."):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), agent)
    return best[1] if best else None


def resolve_agent_command(selector: str, which=shutil.which) -> dict:
    """Turn an agent name or a command string into a runnable command template."""
    selector = str(selector or "").strip()
    agent = find_agent(selector)
    if agent:
        for command in agent["commands"]:
            if which(command[0]):
                return {
                    "agent": agent["name"],
                    "command": list(command),
                    "strip_env": list(agent.get("strip_env", ())),
                    "error": "",
                }
        return {
            "agent": agent["name"],
            "command": [],
            "strip_env": [],
            "error": f"{agent['name']} CLI not found on PATH (tried: {', '.join(c[0] for c in agent['commands'])})",
        }
    try:
        command = shlex.split(selector)
    except ValueError as error:
        return {"agent": "custom", "command": [], "strip_env": [], "error": f"Cannot parse agent CLI command: {error}"}
    if not command:
        return {"agent": "", "command": [], "strip_env": [], "error": "No agent CLI command given"}
    if not which(command[0]):
        return {"agent": "custom", "command": [], "strip_env": [], "error": f"{command[0]} not found on PATH"}
    known = find_agent(os.path.basename(command[0]))
    return {
        "agent": known["name"] if known else "custom",
        "command": command,
        "strip_env": list(known.get("strip_env", ())) if known else [],
        "error": "",
    }


def build_cli_invocation(command: list, prompt: str) -> tuple[list, str | None]:
    """Fill `{prompt}` into the argv, or send the prompt on stdin when absent."""
    if any(PROMPT_PLACEHOLDER in part for part in command):
        return [part.replace(PROMPT_PLACEHOLDER, prompt) for part in command], None
    return list(command), prompt


def call_cli_llm(command: list, prompt: str, *, timeout_sec: int = DEFAULT_CLI_TIMEOUT_SEC,
                 max_retries: int = DEFAULT_CLI_MAX_RETRIES, backoff_sec: float = 1.5, strip_env=(),
                 agent: str = "", runner=subprocess.run, sleep=time.sleep) -> dict:
    """Run one prompt through an agent CLI; result mirrors `call_llm_api`.

    A retryable failure is retried up to `max_retries` times, after the same
    exponential backoff the API path uses.
    """
    request_url = f"cli:{agent or (command[0] if command else 'unknown')}"
    argv, stdin_text = build_cli_invocation(command, prompt)
    child_env = {key: value for key, value in os.environ.items() if key not in set(strip_env or ())}
    attempt_history = []
    last_error = None

    max_retries = max(0, int(max_retries))
    for attempt in range(1, max_retries + 2):
        started = time.monotonic()
        try:
            # Run in an empty directory so the agent has no project files to
            # read or act on; the prompt carries everything it needs.
            with tempfile.TemporaryDirectory(prefix="yt-transcript-cli-") as scratch_dir:
                completed = runner(
                    argv,
                    input=stdin_text if stdin_text is not None else "",
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    env=child_env,
                    cwd=scratch_dir,
                )
        except subprocess.TimeoutExpired:
            last_error = LLMRequestError(
                f"{request_url} timed out after {timeout_sec}s",
                error_type="cli_timeout",
                retryable=True,
                request_url=request_url,
            )
        except OSError as error:
            last_error = LLMRequestError(
                f"{request_url} could not start: {error}",
                error_type="cli_not_found",
                retryable=False,
                request_url=request_url,
            )
        else:
            latency_ms = int((time.monotonic() - started) * 1000)
            text = (completed.stdout or "").strip()
            if completed.returncode == 0 and text:
                attempt_history.append({
                    "attempt_index": attempt,
                    "result": "success",
                    "error_type": "",
                    "status_code": None,
                    "latency_ms": latency_ms,
                    "request_url": request_url,
                    "streaming_used": False,
                    "retryable": False,
                    "finish_reason": "",
                    "usage": {},
                })
                return {
                    "text": text,
                    "latency_ms": latency_ms,
                    "request_url": request_url,
                    "streaming_used": False,
                    "finish_reason": "",
                    "usage": {},
                    "attempts": attempt,
                    "attempt_history": attempt_history,
                    "backend": "cli",
                }
            stderr_tail = (completed.stderr or "").strip()[-500:]
            if completed.returncode != 0:
                message = f"{request_url} exited with code {completed.returncode}: {stderr_tail}"
                error_type = "cli_exit_error"
            else:
                message = f"{request_url} returned no output: {stderr_tail}"
                error_type = "cli_empty_output"
            last_error = LLMRequestError(
                message,
                error_type=error_type,
                retryable=True,
                request_url=request_url,
                response_body=stderr_tail,
            )

        attempt_history.append({
            "attempt_index": attempt,
            "result": "failure",
            "error_type": last_error.error_type,
            "status_code": None,
            "latency_ms": None,
            "request_url": request_url,
            "streaming_used": False,
            "retryable": last_error.retryable,
        })
        if not last_error.retryable or attempt > max_retries:
            break
        delay = backoff_delay(attempt, backoff_sec)
        print(f"Retrying {request_url} after {last_error.error_type} in {delay:.1f}s "
              f"(attempt {attempt}/{max_retries})", file=sys.stderr)
        sleep(delay)

    last_error.attempts = len(attempt_history)
    last_error.attempt_history = attempt_history
    raise last_error


def api_is_configured(config: dict) -> bool:
    """Return whether the `llm_*` API settings are complete."""
    config = config or {}
    return all(str(config.get(key, "") or "").strip() for key in ("llm_api_key", "llm_base_url", "llm_model"))


def resolve_llm_backend(config: dict, *, env: dict | None = None, probe: bool = True,
                        previous: dict | None = None, which=shutil.which, runner=subprocess.run) -> dict:
    """Pick the text backend: the host agent's CLI in auto mode, the API in api mode.

    Selection order in auto mode: an explicit command (`YT_TRANSCRIPT_AGENT_CLI`
    or `llm_agent_cli`), a backend recorded by an earlier run of the same work
    dir, then the detected host agent. A CLI is used only after a probe call
    returns text; otherwise there is no backend ("none"). This never picks the
    API in auto mode, because the API costs money: whether a run without a CLI
    may use it is `llm_api_fallback`'s call, made by the caller.
    """
    config = config or {}
    env = os.environ if env is None else env
    mode = normalize_backend_mode(config.get("llm_backend"))
    api_ready = api_is_configured(config)
    decision = {
        "mode": mode,
        "backend": "api" if mode == "api" and api_ready else "none",
        "agent": "",
        "agent_source": "",
        "command": [],
        "strip_env": [],
        "api_configured": api_ready,
        "probe": {"attempted": False},
        "fallback_reason": "",
    }
    if mode == "api":
        if not api_ready:
            decision["fallback_reason"] = "llm_backend is api but llm_api_key, llm_base_url or llm_model is missing"
        return decision

    selector = ""
    source = ""
    for candidate, candidate_source in (
        (env.get(AGENT_CLI_ENV_VAR, ""), "env"),
        (config.get("llm_agent_cli", ""), "config"),
    ):
        if str(candidate or "").strip():
            selector, source = str(candidate).strip(), candidate_source
            break
    previous = previous if isinstance(previous, dict) else {}
    if not selector and previous.get("backend") == "cli" and previous.get("command"):
        # Keep a resumed run on the backend it started with.
        selector, source = shlex.join(previous["command"]), "previous_run"
    if not selector:
        detected = detect_host_agent(env)
        if detected:
            selector, source = detected, "detected"

    if not selector:
        decision["fallback_reason"] = (
            f"No host agent detected; set llm_agent_cli in config.yaml or {AGENT_CLI_ENV_VAR} to use the agent's own CLI"
        )
        return decision

    resolved = resolve_agent_command(selector, which=which)
    decision["agent"] = resolved["agent"]
    decision["agent_source"] = source
    if resolved["error"]:
        decision["fallback_reason"] = resolved["error"]
        return decision
    decision["command"] = resolved["command"]
    decision["strip_env"] = resolved["strip_env"]

    if not probe:
        decision["backend"] = "cli"
        return decision

    probe_timeout = max(1, int(config.get("llm_cli_probe_timeout_sec") or DEFAULT_CLI_PROBE_TIMEOUT_SEC))
    probe_record = {"attempted": True, "ok": False, "latency_ms": None, "error": ""}
    try:
        result = call_cli_llm(
            resolved["command"],
            CLI_PROBE_PROMPT,
            timeout_sec=probe_timeout,
            max_retries=0,
            strip_env=resolved["strip_env"],
            agent=resolved["agent"],
            runner=runner,
        )
        probe_record["ok"] = True
        probe_record["latency_ms"] = result.get("latency_ms")
    except Exception as error:  # noqa: BLE001 - any failure means "no CLI"
        probe_record["error"] = str(error)
    decision["probe"] = probe_record
    if probe_record["ok"]:
        decision["backend"] = "cli"
    else:
        decision["fallback_reason"] = f"Agent CLI probe failed: {probe_record['error']}"
    return decision
