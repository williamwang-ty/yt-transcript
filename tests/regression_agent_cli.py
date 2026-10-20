"""Regression tests for the llm_backend=auto agent-CLI backend."""

from tests._support import *
from kernel.long_text import agent_cli
from kernel.long_text import window


def _which_all(name):
    """Pretend every binary is installed."""
    return f"/usr/bin/{name}"


def _completed(stdout="", returncode=0, stderr=""):
    """Build a fake subprocess result."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class AgentCliRegressionTests(unittest.TestCase):
    """Agent detection, backend selection, and the CLI call wrapper."""

    def test_detects_known_agents_from_marker_variables(self):
        """Each host's marker variable maps to its agent name."""
        cases = {
            "CLAUDECODE": "claude-code",
            "CODEX_THREAD_ID": "codex",
            "OPENCODE": "opencode",
            "PI_CODING_AGENT": "pi",
            "GEMINI_CLI": "gemini",
            "CURSOR_AGENT": "cursor",
        }
        for var, expected in cases.items():
            self.assertEqual(agent_cli.detect_host_agent({var: "1"}), expected, var)
        self.assertEqual(agent_cli.detect_host_agent({}), "")

    def test_detects_agent_from_generic_label_with_version_suffix(self):
        """AGENT / AI_AGENT labels match with suffixes but not on partial words."""
        self.assertEqual(agent_cli.detect_host_agent({"AI_AGENT": "claude-code_2-1-300_agent"}), "claude-code")
        self.assertEqual(agent_cli.detect_host_agent({"AGENT": "cursor-cli"}), "cursor")
        self.assertEqual(agent_cli.detect_host_agent({"AGENT": "pixel"}), "")

    def test_api_mode_never_looks_for_a_cli(self):
        """llm_backend=api uses the API even inside an agent."""
        config = {"llm_backend": "api", "llm_api_key": "k", "llm_base_url": "https://x", "llm_model": "m"}
        decision = agent_cli.resolve_llm_backend(config, env={"CLAUDECODE": "1"}, which=_which_all)
        self.assertEqual(decision["backend"], "api")
        self.assertEqual(decision["command"], [])

    def test_auto_uses_detected_agent_cli_after_successful_probe(self):
        """A detected agent whose CLI answers the probe becomes the backend."""
        runner = mock.Mock(return_value=_completed("OK"))
        decision = agent_cli.resolve_llm_backend(
            {"llm_backend": "auto"}, env={"CODEX_THREAD_ID": "t"}, which=_which_all, runner=runner,
        )
        self.assertEqual(decision["backend"], "cli")
        self.assertEqual(decision["agent"], "codex")
        self.assertEqual(decision["agent_source"], "detected")
        self.assertEqual(decision["command"], ["codex", "exec", "--skip-git-repo-check", "-"])
        self.assertTrue(decision["probe"]["ok"])
        self.assertEqual(runner.call_args.kwargs["input"], agent_cli.CLI_PROBE_PROMPT)

    def test_auto_falls_back_to_api_when_probe_fails(self):
        """A CLI that errors on the probe leaves the API as the backend."""
        config = {"llm_backend": "auto", "llm_api_key": "k", "llm_base_url": "https://x", "llm_model": "m"}
        runner = mock.Mock(return_value=_completed("", returncode=1, stderr="not logged in"))
        decision = agent_cli.resolve_llm_backend(config, env={"GEMINI_CLI": "1"}, which=_which_all, runner=runner)
        self.assertEqual(decision["backend"], "api")
        self.assertIn("not logged in", decision["fallback_reason"])

    def test_auto_without_agent_or_api_has_no_backend(self):
        """No detected agent and no API configured resolves to none."""
        decision = agent_cli.resolve_llm_backend({"llm_backend": "auto"}, env={}, which=_which_all)
        self.assertEqual(decision["backend"], "none")
        self.assertIn("llm_agent_cli", decision["fallback_reason"])

    def test_missing_cli_binary_falls_back(self):
        """A detected agent whose CLI is not installed is skipped."""
        decision = agent_cli.resolve_llm_backend(
            {"llm_backend": "auto"}, env={"CURSOR_AGENT": "1"}, which=lambda name: None,
        )
        self.assertEqual(decision["backend"], "none")
        self.assertIn("cursor-agent", decision["fallback_reason"])

    def test_explicit_command_beats_detection_and_previous_run_is_reused(self):
        """An explicit command (via env) wins over detection; a resumed run keeps its CLI."""
        runner = mock.Mock(return_value=_completed("OK"))
        decision = agent_cli.resolve_llm_backend(
            {"llm_backend": "auto"},
            env={"CLAUDECODE": "1", agent_cli.AGENT_CLI_ENV_VAR: "mytool --ask {prompt}"},
            which=_which_all,
            runner=runner,
        )
        self.assertEqual(decision["agent"], "custom")
        self.assertEqual(decision["agent_source"], "env")
        self.assertEqual(runner.call_args.args[0], ["mytool", "--ask", agent_cli.CLI_PROBE_PROMPT])

        resumed = agent_cli.resolve_llm_backend(
            {"llm_backend": "auto"},
            env={"CLAUDECODE": "1"},
            previous={"backend": "cli", "command": ["mytool", "--ask", "{prompt}"]},
            which=_which_all,
            runner=runner,
        )
        self.assertEqual(resumed["agent_source"], "previous_run")
        self.assertEqual(resumed["command"], ["mytool", "--ask", "{prompt}"])

    def test_call_cli_llm_strips_nesting_variables_and_retries(self):
        """The child loses host markers; a failed attempt is retried once."""
        runner = mock.Mock(side_effect=[_completed("", returncode=2, stderr="boom"), _completed("清洗后的文本")])
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "KEEP_ME": "1"}):
            result = agent_cli.call_cli_llm(
                ["claude", "-p"], "prompt", max_retries=1, strip_env=["CLAUDECODE"], agent="claude-code", runner=runner,
            )
        self.assertEqual(result["text"], "清洗后的文本")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["request_url"], "cli:claude-code")
        child_env = runner.call_args.kwargs["env"]
        self.assertNotIn("CLAUDECODE", child_env)
        self.assertIn("KEEP_ME", child_env)

    def test_call_cli_llm_raises_after_retries(self):
        """Empty output on every attempt raises a retryable LLMRequestError."""
        runner = mock.Mock(return_value=_completed(""))
        with self.assertRaises(utils.LLMRequestError) as caught:
            agent_cli.call_cli_llm(["pi", "-p"], "prompt", max_retries=1, runner=runner)
        self.assertEqual(caught.exception.error_type, "cli_empty_output")
        self.assertEqual(len(caught.exception.attempt_history), 2)

    def _make_work_dir(self, tmpdir):
        """Chunk a small transcript into a work dir (one window round covers it)."""
        return chunk_plain_text("第一句话讲人工智能。" * 30, Path(tmpdir) / "text")

    def _config(self, **overrides):
        """Default config with the API configured."""
        config = utils._default_config_values()
        config.update({
            "llm_api_key": "key",
            "llm_base_url": "https://api.deepseek.com",
            "llm_model": "deepseek-v4-pro",
        })
        config.update(overrides)
        return config

    def _cli_result(self, prompt, **kwargs):
        return {"text": tag_echo(prompt), "latency_ms": 5, "request_url": "cli:claude-code",
                "streaming_used": False, "finish_reason": "", "usage": {}, "attempts": 1}

    def test_window_rounds_go_to_the_agent_cli(self):
        """With a CLI backend, requests never reach the API and the choice is recorded."""
        cli_decision = {
            "mode": "auto", "backend": "cli", "agent": "claude-code", "agent_source": "detected",
            "command": ["claude", "-p"], "strip_env": [], "api_configured": True,
            "probe": {"attempted": True, "ok": True}, "fallback_reason": "",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            with mock.patch.object(utils, "load_config", return_value=self._config()), \
                mock.patch.object(agent_cli, "resolve_llm_backend", return_value=cli_decision), \
                mock.patch.object(agent_cli, "call_cli_llm", side_effect=lambda command, prompt, **kw:
                                  self._cli_result(prompt)) as cli_call, \
                mock.patch.object(utils, "_call_llm_api") as api_call, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
                result = window.process_window(str(work_dir), "cleanup_zh")
            state = window.load_window_state(str(work_dir))

        self.assertTrue(result["success"], result)
        self.assertEqual(cli_call.call_count, 1)
        api_call.assert_not_called()
        self.assertEqual(result["request_url"], "cli:claude-code")
        self.assertEqual(state["llm_backend"]["backend"], "cli")

    def test_window_switches_to_api_when_cli_fails_mid_run(self):
        """A CLI failure after its retries moves the round and the rest to the API."""
        cli_decision = {
            "mode": "auto", "backend": "cli", "agent": "codex", "agent_source": "detected",
            "command": ["codex", "exec", "-"], "strip_env": [], "api_configured": True,
            "probe": {"attempted": True, "ok": True}, "fallback_reason": "",
        }
        cli_error = utils.LLMRequestError("codex exited", error_type="cli_exit_error", retryable=True)

        def api(**kwargs):
            return {"text": tag_echo(kwargs["messages"][0]["content"]), "latency_ms": 5,
                    "request_url": "https://api.deepseek.com/v1/chat/completions", "streaming_used": False,
                    "finish_reason": "stop", "usage": {}, "attempts": 1}

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            with mock.patch.object(utils, "load_config", return_value=self._config()), \
                mock.patch.object(agent_cli, "resolve_llm_backend", return_value=cli_decision), \
                mock.patch.object(agent_cli, "call_cli_llm", side_effect=cli_error), \
                mock.patch.object(utils, "_call_llm_api", side_effect=api) as api_call, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
                result = window.process_window(str(work_dir), "cleanup_zh")
            state = window.load_window_state(str(work_dir))

        self.assertTrue(result["success"], result)
        self.assertEqual(api_call.call_count, 1)
        self.assertEqual(state["llm_backend"]["backend"], "api")
        self.assertIn("Agent CLI failed mid-run", state["llm_backend"]["fallback_reason"])
        self.assertTrue(any("switching the remaining window rounds" in w for w in result["warnings"]))

    def test_window_without_any_backend_exits(self):
        """No CLI, no API and no agent handoff stops before the first round."""
        none_decision = {
            "mode": "auto", "backend": "none", "agent": "", "agent_source": "", "command": [],
            "strip_env": [], "api_configured": False, "probe": {"attempted": False},
            "fallback_reason": "No host agent detected",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = self._make_work_dir(tmpdir)
            with mock.patch.object(utils, "load_config", return_value=self._config(llm_api_key="")), \
                mock.patch.object(agent_cli, "resolve_llm_backend", return_value=none_decision), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit):
                    window.process_window(str(work_dir), "cleanup_zh")
        self.assertIn("no LLM backend available", stderr.getvalue())

    def test_load_config_reads_backend_settings(self):
        """llm_backend and llm_agent_cli survive config loading; bad modes default to auto."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f'output_dir: "{tmpdir}"\nllm_backend: "api"\nllm_agent_cli: "gemini"\nllm_cli_timeout_sec: 900\n',
                encoding="utf-8",
            )
            config = utils.load_config(str(config_path))
            self.assertEqual(config["llm_backend"], "api")
            self.assertEqual(config["llm_agent_cli"], "gemini")
            self.assertEqual(config["llm_cli_timeout_sec"], 900)
            config_path.write_text(f'output_dir: "{tmpdir}"\nllm_backend: "weird"\n', encoding="utf-8")
            self.assertEqual(utils.load_config(str(config_path))["llm_backend"], "auto")
