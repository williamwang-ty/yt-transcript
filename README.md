# yt-transcript

[English](#english) | [中文](#中文)

---

## English

Transcribe YouTube videos into formatted Markdown articles. Supports subtitle download or Deepgram speech-to-text (with multi-speaker recognition).

Start here:
- If you want the canonical terminology, see `Canonical Terms` below.
- If you want to see how a video is processed, see `The run Command` and `Minimum Commands`.

### ✨ Features

- 🎯 **Smart Subtitle Fetching**: Prioritizes YouTube official/auto-generated subtitles
- 🎙️ **Speech-to-Text**: Auto-transcribes via Deepgram Nova-3 when no subtitles available
- 👥 **Multi-speaker Recognition**: Automatically distinguishes different speakers
- 🌐 **Bilingual Support**: Auto-translate and side-by-side formatting
- 🤖 **AI Enhancement**: Auto punctuation, paragraph splitting, error correction
- 📝 **Markdown Output**: Formatted articles with metadata

### 📋 Prerequisites

- `yt-dlp`: Download YouTube videos/audio/subtitles
- `ffmpeg`: Audio processing (splitting, silence detection)
- `python3`: Text processing
- `curl`: Call Deepgram API
- [Deepgram Account](https://console.deepgram.com/): For speech transcription

#### Installation

```bash
# macOS
brew install yt-dlp python3 ffmpeg

# or via pip
pip install yt-dlp
```

### ⚙️ Configuration

1. Copy the config template:
   ```bash
   cp config.example.yaml config.yaml
   ```

2. Edit `config.yaml` with your settings:
   ```yaml
   deepgram_api_key: "your_api_key_here"
   deepgram_model: "nova-3"
   deepgram_enable_utterances: true
   deepgram_prefer_structured_output: true
   output_dir: "~/Downloads"

   # LLM API for long video chunk processing (optional)
   # Format: "openai" or "anthropic"
   llm_api_format: "openai"
   llm_api_key: "your_llm_api_key"
   llm_base_url: "https://api.deepseek.com"
   llm_model: "deepseek-v4-pro"
   llm_timeout_sec: 180
   llm_max_retries: 3
   llm_backoff_sec: 1.5
   llm_stream: "auto"
   llm_reasoning_probe_enabled: true
   llm_chunk_recovery_attempts: 1
   llm_chunk_recovery_backoff_sec: 1.0

   # yt-dlp network hardening (safe defaults)
   yt_dlp_socket_timeout_sec: 15
   yt_dlp_retries: 1
   yt_dlp_extractor_retries: 1
   ```

   #### YouTube "Sign in to confirm you're not a bot"

   `yt-transcript` now uses this policy by default:

   1. Start anonymously (do **not** read browser cookies up front)
   2. If `yt-dlp` returns `Sign in to confirm you're not a bot`, automatically retry (up to 3 attempts) with:
      - `--cookies-from-browser chrome`
   3. If that Chrome retry also fails, surface a clear error and tell you how to provide a `cookies.txt` file

   This means local desktop setups may recover automatically, while remote/container setups remain explicit and safe.
   The JSON returned by `scripts/download.sh` now also includes a `yt_dlp_runtime` object so callers can see which timeout, retry, and auth strategy actually ran.

   If the automatic Chrome retry fails, the most portable fix is an exported Netscape-format `cookies.txt`:

   ```yaml
   yt_dlp_cookies_file: "~/.config/yt-transcript/youtube_cookies.txt"
   ```

   Or for a one-off run:

   ```bash
   YT_DLP_COOKIES_FILE=~/.config/yt-transcript/youtube_cookies.txt \
     bash scripts/download.sh "$URL" metadata
   ```

   You can still force browser-cookie mode explicitly:

   ```yaml
   yt_dlp_cookies_from_browser: "chrome"
   ```

   Recommended `cookies.txt` import flow:

   1. Open `youtube.com` in a logged-in browser on your local machine
   2. Export cookies for YouTube in Netscape `cookies.txt` format
   3. Copy that file to the machine/container running this skill
   4. Set `yt_dlp_cookies_file` in `config.yaml` or `YT_DLP_COOKIES_FILE` in the environment

   In remote or container environments, `yt_dlp_cookies_file` is usually more reliable than `--cookies-from-browser chrome`.

   > **Note**:
   > - `deepgram_api_key` is only required when the video has no usable subtitles and audio transcription is needed.
   > - Deepgram transcription defaults to `nova-3`. Legacy `nova-2` / `nova-2-*` settings are automatically upgraded to `nova-3` before any request is sent.
   > - `deepgram_enable_utterances` and `deepgram_prefer_structured_output` now default to `true`; set either to `false` only when you need to compare against the legacy flat-transcript path.
   > - Deepgram requests now use bounded automatic retries for transient timeout/network failures before surfacing an error.
   > - The text of every video goes through an LLM backend. By default (`llm_backend: auto`) that is the calling agent's own CLI, so the LLM API settings are a fallback; when neither works, `run` hands each text request to the calling agent (`needs_agent_text`).
   > - `llm_base_url` can be either a provider root URL or a `/v1` URL. The tool normalizes both.
   > - `llm_stream: "auto"` prefers SSE streaming when the provider supports it.
   > - With `llm_reasoning_probe_enabled: true`, unknown OpenAI-compatible models get one non-streaming `OK` probe; if the response exposes reasoning metadata, chunk calls switch to non-streaming for recovery telemetry.
   > - `bash scripts/preflight.sh --require-llm` now performs a real low-cost LLM probe and reports latency.
   > - `llm_backend: "auto"` (default) sends the text requests to the calling agent's own CLI (Claude Code, Codex, OpenCode, pi, Gemini CLI, Cursor), detected from the environment or named with `llm_agent_cli`. The CLI must answer a short probe; if it can't, or fails mid-run, the LLM API above is used. `llm_backend: "api"` always uses the API. `python3 yt_transcript_utils.py resolve-llm-backend` shows which one a run would use.

### 🚀 Usage

#### As a Claude Skill

1. Place this directory in any Claude skills directory (or another agent's skills directory)
2. Provide a YouTube link in your conversation
3. The agent runs `python3 yt_transcript_utils.py run <url>`, which does the whole job for the video, and reports the result (see `The run Command` below)

The scripts resolve `config.yaml` relative to the skill directory, so the skill is no longer tied to `~/.claude/skills/yt-transcript`.

#### Single Video Example

```
Please transcribe this video: https://www.youtube.com/watch?v=xxxxx
```

#### Multiple Videos (Batch Processing)

You can provide multiple links at once. They will be processed **serially** (one `run` per link):

```
Please transcribe these videos:
- https://www.youtube.com/watch?v=xxxxx
- https://www.youtube.com/watch?v=yyyyy
- https://www.youtube.com/watch?v=zzzzz
```

After completion, a summary table will be provided with status and output paths for each video.


### Routing Notes

- `run` makes every routing decision itself and records it in the video's `state.json` (`route`: subtitles or Deepgram, the track, the mode, and the reason).
- Short and long videos go through the same stages; there is no separate Quick Mode. A short text takes one window round and skips the outline; YouTube chapters become outline hints when the plan says `long`.
- `run <url> --stage plan` shows the route and the text plan without spending anything (no LLM, no Deepgram).
- `run` builds the output filename (`yyyy-mm-dd. <title>.md`) and the frontmatter from the metadata.

### 📁 Project Structure

```
yt-transcript/
├── SKILL.md                 # What the host agent reads: run `run`, act on its result (main entry point)
├── SYSTEM_DESIGN.md         # Single authoritative system design document
├── prompts/                 # Single-task prompt templates
├── scripts/                 # Helper shell scripts
├── yt_transcript_utils.py   # Main Python entry (CLI, including `run`, `status`, `cleanup`)
├── kernel/                 # Kernel package
│   ├── pipeline.py         # The `run` command: stages, state.json, results and exit codes
│   ├── task_runtime/       # Generic task runtime layer
│   │   ├── runtime.py      # Ownership, command envelopes, telemetry append
│   │   ├── api.py          # Stable runtime-facing API for create/inspect/advance/control/finalize
│   │   ├── contracts.py    # Runtime contracts for task/run/action/artifact envelopes
│   │   ├── lifecycle.py    # Lifecycle shell and transition summaries for runtime stages
│   │   ├── policy.py       # Allowed-action derivation and budget-pressure policy checks
│   │   ├── evaluator.py    # Quality-gated evaluator reports and action recommendations
│   │   ├── decision.py     # Rule-first action selection and decision records
│   │   ├── ledger.py       # Runtime budget and action accounting summaries
│   │   ├── recovery.py     # Resume-safe recovery summaries and processing sub-states
│   │   ├── artifacts.py    # Artifact-graph helpers for persisted runtime outputs
│   │   ├── state.py        # Manifest/runtime persistence and control files
│   │   ├── controller.py   # Owned mutation and bounded control-loop helpers
│   │   └── telemetry.py    # Telemetry query and summary helpers
│   └── long_text/          # Long-text transformation layer
│       ├── glossary.py     # Glossary extraction and terminology checks
│       ├── semantic.py     # Semantic anchor checks for verify-quality
│       ├── contracts.py    # Control contracts and policy state
│       ├── lifecycle.py    # Manifest lifecycle and resume-state helpers
│       ├── prompting.py    # Prompt assembly and chunking-context helpers
│       ├── llm.py          # LLM request loop and retry helpers
│       ├── window.py       # Sliding-window executor (default long-text mode)
│       ├── processing.py   # Fixed-chunk processing loop (legacy `chunks` mode)
│       ├── chunking.py     # Chunking command surfaces
│       ├── merge.py        # Merge and chapter-plan command surfaces
│       └── execution.py    # Execution, executor routing, and resume command surfaces
├── tests/                   # Regression test suite
├── config.yaml              # Local config (gitignored)
├── config.example.yaml      # Config template
└── README.md                # This document
```

### 🏗️ Architecture & Design Overview

`README.md` is the operator-facing quickstart and command guide. `SYSTEM_DESIGN.md` is the single authoritative design document for the system architecture.

At a high level, `yt-transcript` is a local-first system in which one deterministic command, `run <url>`, turns a YouTube URL into a Markdown article through a fixed list of stages:

- `metadata`: one yt-dlp fetch of the video's info
- `source`: spoken-language detection, the trusted subtitle track or Deepgram transcription, and the subtitle-quality reroute
- `normalize`: the normalized document (sentences with their timed cues)
- `plan`: the text tasks for the mode and their size
- `text`: the long-text transformation subsystem (sliding window, outline and glossary) for every task
- `merge`, `verify` (quality gate), `assemble` (filename, frontmatter, bilingual layout) and `cleanup`

The host agent only starts `run`, acts on the result it returns, and reports to the user; `SKILL.md` tells it how.

The codebase mirrors that design through a two-layer kernel split: `kernel/task_runtime/*` owns generic long-running job control, while `kernel/long_text/*` owns long-text transformation behavior. `yt_transcript_utils.py` remains the main CLI and workflow façade, but it now delegates into those two layers directly.

Phase 1 of the runtime-upgrade path also introduces `kernel/task_runtime/contracts.py`, which normalizes task, run-state, action, artifact, and quality-report envelopes without changing the nominal workflow behavior.
Phase 2 adds `kernel/task_runtime/lifecycle.py`, which wraps runtime-sensitive commands in an explicit lifecycle shell so state transitions become observable before richer policy logic is introduced.
Phase 3 adds `kernel/task_runtime/policy.py`, `decision.py`, and `ledger.py` so command envelopes can expose allowed actions, budget-pressure summaries, and rule-first decision records in a uniform way.
Phase 4 adds `kernel/task_runtime/recovery.py` and `artifacts.py` so long-text runs expose processing sub-states, recovery recommendations, and artifact-graph views without changing the core chunk-processing algorithms.
Phase 5 adds `kernel/task_runtime/evaluator.py` plus constrained llm-assisted ranking hooks so quality-gated recommendations and optional model-assisted action selection can coexist without bypassing the allowed-action contract.
Phase 6 adds `kernel/task_runtime/api.py`, making `create-run`, `inspect-run`, `advance-run`, `apply-control`, `resume-run`, and `finalize-run` the preferred outer runtime contract while preserving the older control commands as compatibility helpers.

Nothing in the `run` flow uses that outer runtime contract; stage 4 of [#23](https://github.com/williamwang-ty/yt-transcript/issues/23) removes it.

The hardest internal subsystem is long-text transformation. Every video's text goes through it (a short text in one window round): windowed processing, consistency protection (outline + glossary), verification, repair, and deterministic merge.

[Read separate System Design Document](SYSTEM_DESIGN.md)

### 🔁 The run Command

`python3 yt_transcript_utils.py run <url>` is the entry point for one video. Each video has one work directory, `/tmp/yt-transcript/<video_id>/` (or `$YT_TRANSCRIPT_WORK_ROOT/<video_id>/`), with one `state.json`. Rerunning the same command continues at the first unfinished stage; a finished video returns `done` with the existing article at once.

Every call prints progress on stderr and exactly one JSON result on stdout, with a fixed exit code:

| `status` | Exit | Meaning |
|---|---|---|
| `done` | 0 | Article written (`output_file`, `route`, `warnings`, `llm`), or `--stage plan` finished |
| `failed` | 1 | A stage failed: `stage`, `reason`, `message`, `retryable`, `next_command` |
| `needs_user` | 3 | Only the user can fix it: `cookies_required`, `login_required`, `rate_limited`, `deepgram_key_missing`, `tool_missing`, `output_dir_missing` |
| `unsupported` | 4 | The spoken language is not supported |
| `needs_agent_text` | 5 | No LLM backend is reachable; one text request is handed to the agent (`task.prompt_file`, `task.output_file`) |
| `in_progress` | 6 | The `--max-seconds` budget ran out at a safe point; rerun to continue |

Exit code 2 is a command-line usage error. Flags: `--source subtitles|deepgram` and `--mode chinese|bilingual` (remembered for the video until `--restart`), `--from <stage>`, `--stage plan`, `--max-seconds <n>`, `--restart`, `--config-path`. `status [<video_id>]` lists unfinished runs or shows one video's state; `cleanup <video_id> [--force]` removes a work directory. `$YT_TRANSCRIPT_OUTPUT_DIR` replaces the configured `output_dir`.

### 🔧 Preflight Modes

`run` checks what each stage needs right before it (yt-dlp, the Deepgram key only when Deepgram is chosen, an LLM backend at the text stage), so preflight is no longer a required step. The checks remain available for diagnosis:

```bash
# Base checks: yt-dlp, python3, config
bash scripts/preflight.sh

# Also check the Deepgram key
bash scripts/preflight.sh --require-deepgram

# Also probe the LLM API
bash scripts/preflight.sh --require-llm
```

### 🧩 Structured Script Outputs

These are the steps `run` calls internally; the agent no longer runs them one by one. They stay available for debugging until stage 4 of [#23](https://github.com/williamwang-ty/yt-transcript/issues/23) turns most of them into internal functions. They emit machine-readable JSON on stdout so routing, validation, and execution decisions stay in code:

- `scripts/download.sh "$URL" metadata`
- `scripts/download.sh "$URL" subtitle-info`
- `scripts/download.sh "$URL" subtitles`
- `scripts/download.sh "$URL" audio`
- `python3 yt_transcript_utils.py get-chapters "$URL"`
- `python3 yt_transcript_utils.py chunk-segments /tmp/${VIDEO_ID}_segments.json /tmp/${VIDEO_ID}_chunks --prompt <RAW_STAGE_PROMPT>`
- `python3 yt_transcript_utils.py chunk-document /tmp/${VIDEO_ID}_normalized_document.json /tmp/${VIDEO_ID}_chunks --prompt <RAW_STAGE_PROMPT>`
- `python3 yt_transcript_utils.py prepare-resume /tmp/${VIDEO_ID}_chunks --prompt <RAW_STAGE_PROMPT>`
- `python3 yt_transcript_utils.py build-chapter-plan /tmp/${VIDEO_ID}_chapters.json /tmp/${VIDEO_ID}_chunks /tmp/${VIDEO_ID}_chunks/chapter_plan.json`
- `python3 yt_transcript_utils.py build-glossary /tmp/${VIDEO_ID}_chunks --mode transcript`
- `python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage <stage>`
- `python3 yt_transcript_utils.py normalize-document /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt`

Here `<RAW_STAGE_PROMPT>` comes from `plan-optimization`:

- `cleanup_zh` for Chinese monolingual runs
- `structure_only` for bilingual English-source runs

This keeps workflow logic in scripts instead of ad-hoc shell parsing inside the prompt instructions.

`plan-optimization` also emits the canonical chunk execution contract.

At the whole-project level, it is the routing boundary between source acquisition and text transformation. At the long-text subsystem level, it defines the execution contract that downstream chunk processing must follow:

- `operations[*].kind` (`chunk` or `prompt`), `prompt`, `input_key` or `input` / `output`
- `operations[*].execution.mode` (`chunked` or `single_pass`)

`normalize-document` materializes `/tmp/${VIDEO_ID}_normalized_document.json` from either raw text or timed `segments.json`, and `plan-optimization` auto-materializes it when source artifacts already exist. When the source segments came from subtitle cleanup, the normalized document now also preserves lightweight cleanup diagnostics under `diagnostics.subtitle_cleanup` and an explainable subtitle-path quality summary under `diagnostics.subtitle_quality`.

`plan-optimization` now separates duration routing from source routing: `routing_reason` still explains short-vs-long execution, while `source_route_reason`, `subtitle_quality_score`, `reroute_recommended`, and `reroute_target` explain whether the current subtitle source should be kept, manually reviewed, or replaced with Deepgram.

For long-video chunking, `plan-optimization` now also emits a canonical `chunking` block; when normalization exists, `chunk-document` is the preferred driver and it keeps the chunk boundaries explicit in `manifest.json` (the window executor reads its source from those chunk files).

The current design also has explicit resume semantics: `prepare-resume` repairs stale manifest state manually, while `process-chunks` runs the same repair step automatically before execution continues.

Unfinished runs keep their work directory: `run`'s own cleanup stage only runs once the article exists, and `python3 yt_transcript_utils.py cleanup <video_id>` refuses an unfinished run unless `--force` is given (to discard it on purpose). `python3 yt_transcript_utils.py status` lists interrupted runs with their next stage, last result and the command that continues them.

Known limitation: all state and work files live under `/tmp`, which macOS empties on restart (many Linux systems do too). Resume therefore only works within the same boot; after a reboot an interrupted run starts over. Interrupted runs also keep their downloaded audio in `/tmp` until they finish or are discarded with `cleanup <video_id> --force`, which can take hundreds of MB. Moving the work directory to a persistent location (for example `~/.cache/yt-transcript`) is deferred for now.

Current policy is intentional and explicit:

- long-text stages run `process-chunks`; with the default `long_text_mode: window` it drives the sliding-window executor, and `long_text_mode: chunks` keeps the legacy fixed-chunk executor
- a work dir that already has finished fixed chunks keeps resuming in `chunks` mode

### 🧭 Intentional Design Decisions

- `bilingual` means English source text plus Chinese translation, not subtitle file merging
- `subtitle-info` decides the spoken language first (original audio track, metadata `language`, ASR track, then manual tracks) and only trusts human subtitles and the ASR track in that language; YouTube's auto-translated tracks are never used
- Human Chinese subtitles take precedence as the single subtitle source track for any video; otherwise Chinese videos use Chinese subtitles and English videos use English subtitles
- Videos spoken in other languages are reported as unsupported (`source_language_supported=false`) instead of being transcribed; Deepgram runs use the detected language, or `--language auto` when the metadata has no language signal
- Chinese-source monolingual optimization now uses a dedicated `cleanup_zh` prompt that preserves meaning while repairing punctuation, paragraphing, duplicate subtitle fragments, and obvious spacing artifacts
- `config.yaml` is intentionally limited to flat top-level key/value entries; nested or multi-line YAML is not supported
- YAML frontmatter values are always quoted on purpose to favor safe parsing over prettier formatting
- Markdown header text is escaped and link destinations are encoded so edge-case titles/channels do not break output structure
- `chunk-document` is now the canonical long-video chunking entrypoint when `normalized_document.json` exists; it follows `preferred_chunk_source` instead of blindly preferring timed segments, so Chinese YouTube-subtitle long paths now chunk from cleaned `text` while still retaining `segments` for timing metadata
- `chunk-text` force-splits very long unpunctuated passages to stay within downstream LLM chunk budgets
- `download.sh metadata` now prefers a single `yt-dlp -J` fetch when available, and subtitle/audio modes reuse metadata-derived video IDs before falling back to extra probes
- `transcribe-deepgram` defaults to Deepgram `nova-3`; legacy `nova-2` / `nova-2-*` model settings are upgraded to `nova-3` at runtime
- `transcribe-deepgram --output-segments` can emit time-aligned segments for downstream timed chunking + YouTube chapter mapping
- `transcribe-deepgram` now defaults to utterance-first transcript assembly; `--disable-utterances --legacy-flat-output` remains available for compatibility/debugging checks
- `transcribe-deepgram` now also reports lightweight observability fields such as paragraph/sentence/word counts, per-chunk transcript metadata, and fallback warnings in its result JSON
- `chunk-segments` produces timed chunk manifests, and `build-chapter-plan` maps YouTube chapters onto chunk boundaries for `merge-content`
- `parse-vtt` / `parse-vtt-segments` now use subtitle-aware cleanup so CJK subtitle fragments are not re-joined with stray ASCII spaces
- `parse-vtt-segments` now also emits lightweight cleanup diagnostics such as duplicate/overlap trimming counters, and `normalize-document` carries those subtitle-cleanup signals plus a deterministic `subtitle_quality_score` into `normalized_document.json`
- `plan-optimization` now exposes explainable source-route fields such as `source_route_reason`, `reroute_recommended`, `reroute_target`, and `reroute_reasons`; critically poor Chinese subtitle paths can recommend Deepgram fallback without silently changing the current workflow shell
- `merge-content` now runs a deterministic post-merge cleanup pass on the merged body only: it repairs chunk seams, conservatively rejoins continuation-like split fragments, preserves any explicit prefixed header/frontmatter verbatim, and drops immediately duplicated heading/body seams without asking the LLM to rewrite the document
- `chunk-segments --chapters` can force chunk boundaries at YouTube chapter starts to reduce heading drift
- `chunk-text` now defaults to token-aware planning when `--prompt` is provided, while an explicit `--chunk-size` without `--prompt` keeps legacy character sizing for workflow compatibility
- prompt names are validated eagerly for chunk planning, so typos fail fast instead of silently falling back to generic budgets
- `process-chunks` sends `max_tokens` as a stop line (default 8192 per task, only bounded by the context window) instead of carving it out of the input budget, so reasoning tokens no longer squeeze the answer
- a chunk response cut off at `max_tokens` (`finish_reason` = `length` / `max_tokens` / `max_output_tokens`, any provider) is retried with a larger ceiling (`llm_output_retry_*`, formerly `llm_reasoning_retry_*`); one still cut off after that is flagged `truncated_output`
- `long_text_mode: window` (the default; `chunks` is kept as a legacy fallback) runs `cleanup_zh`, `quick_cleanup` and the bilingual `structure_only` → `translate_only` pair as a sliding window: numbered source units (an over-long unpunctuated sentence is cut at subtitle cue ends, preferring the longest pauses, when the timed cues in `normalized_document.json` match the text, also where the chunker had cut inside a cue; otherwise every ~120 tokens), a read window from a cursor, a soft write target, and the model copying each unit's `[S<n>]` tag in front of its text and ending with `<!--END-->`; the program works out where each paragraph ends. Complete paragraphs are saved under `<work_dir>/window/` (`window/processed/` for the translation stage), a response without the end line loses only its last paragraph, and the cursor moves past what was kept, so truncation only means less progress. Sizes adapt per round: the write target starts at `window_write_tokens` and shrinks after truncated or slow rounds, the read window follows the write target and the measured output/source ratio (capped at `window_read_tokens`) so little is read twice, and the last stretch goes in one round when it fits. A round whose output is far shorter or longer than its source is rejected like a response with bad tags. It works the same for the API and agent-CLI backends, resumes paragraph by paragraph, falls back to one fixed chunk after repeated bad rounds, and reports coverage and how much was re-read. Work dirs that already have finished fixed chunks keep resuming with fixed chunks, and stages the window does not cover (such as `summarize`) use fixed chunks
- In window mode, `cleanup_zh` and `structure_only` first get a global outline (`window_outline: true`): the whole numbered source goes to the model once (in parts above `window_outline_read_tokens`, 16000 by default) and comes back as a short JSON list of sections (title + first unit, checked to be in range and increasing) and of names and terms with their correct spellings and seen misspellings. Each round gets the outline lines near its window and the terms found in it; headings come only from the outline, written by the model where the topic actually begins near the section start or else inserted by the program, so section structure is decided once for the whole text. YouTube chapters (`chunk-document --chapters`, or `chapter_plan.json`) are mapped to units and given as hints. The bilingual pair gets English + Chinese titles and the terms' Chinese renderings carry into `translate_only`. The outline is kept in the window state and reused on resume; if it fails, rounds place their own headings as before
- `manifest.json` records an explicit `plan.chunk_contract` so a resumed fixed-chunk run keeps its original chunk sizes
- chunk execution now also has explicit resume semantics: stale `running` / missing-output checkpoints are repaired deterministically into `done` or `interrupted` before work resumes
- `process-chunks` now treats transient gateway disconnects such as `Remote end closed connection without response` as retryable transport failures, and can auto-rerun suspiciously short / malformed chunk outputs before keeping a warning
- `process-chunks --dry-run` validates prompts, manifests, and chunk budgets without requiring live LLM credentials or probing a backend; actual execution needs a working backend: the calling agent's CLI (`llm_backend: auto`) or `llm_api_key`, `llm_base_url` and `llm_model`
- `download.sh` now writes subtitle and audio artifacts into per-video isolated temp directories under `/tmp/${VIDEO_ID}_downloads/...` and exposes `download_dir` in JSON for deterministic selection and cleanup
- `download.sh subtitles` now requests the exact selected subtitle language codes, so regional variants such as `en-GB` / `zh-TW` work instead of being dropped by a hard-coded whitelist
- `download.sh subtitles` now tries one source-family candidate at a time: Chinese first, then English only as a fallback when no usable Chinese track can be downloaded
- `download.sh subtitles` now distinguishes detection vs downloadability more explicitly: `listed_candidates` shows tracks exposed by YouTube/yt-dlp, while `attempted_candidates`, `blocked_candidates`, and `fallback_used` show what the current runtime could actually fetch
- when a preferred subtitle candidate fails with an auth-like error such as `HTTP 429`, `download.sh subtitles` now retries the same candidate with Chrome cookies before it gives up and falls back to the next candidate
- subtitle-driven workflows still support Chinese-source monolingual mode and English-source bilingual mode; Chinese-source runs now prefer `cleanup_zh`, while English-source runs still use `structure_only -> translate_only`; when neither usable Chinese nor English subtitles can be downloaded, the workflow should stop and fall back to audio transcription
- `plan-optimization` is the canonical short/long router with `< 1800s = short` and `>= 1800s = long`; both go through the same `run` stages (there is no Quick Mode any more), and a short text takes one window round
- `manifest.json` now separates immutable `plan` metadata from `runtime` state, and `process-chunks` records attempt-level telemetry (`attempt_logs`) in addition to chunk-level fields
- fixed chunks are sized by each task's chunk target (`chunk_tokens_*`) with a hard cap of `chunk_hard_cap_multiplier` × target; the old continuity context, autotune and `replan-remaining` patches were removed with the switch to window mode, and a fixed-chunk run stops after repeated consecutive timeouts instead
- `build-glossary --mode transcript` now mines glossary terms from raw chunks plus any available normalized-document title/channel, chapter titles, and optional metadata/description context; `cleanup_zh` auto-builds that glossary when the work_dir does not have one yet
- glossary selection and verification now use boundary-aware matching for simple ASCII terms, so acronyms like `API` do not get falsely selected from unrelated words such as `rapid` or `capital`
- `run_kernel_command(...)` is the stable Python envelope API for kernel commands, and `python3 yt_transcript_utils.py --api-envelope ...` emits the same `yt_transcript.command_result/v1` envelope on the CLI without breaking legacy flat JSON output
- envelope-producing kernel commands append local `yt_transcript.telemetry_event/v1` records to `telemetry.jsonl` when a stable nearby sink path can be inferred
- `runtime.status` now distinguishes `completed`, `completed_with_errors`, and `aborted`
- runtime token estimation remains heuristic by default; `test-token-count` / `preflight.sh --require-llm` probe provider-side token counting and clearly fall back to local estimates when unavailable
- `chunk_hard_cap_multiplier` is constrained to a conservative `1.0-2.0` range so misconfiguration cannot silently blow up chunk envelopes
- `preflight.sh` is staged so subtitle-only workflows do not require Deepgram or LLM credentials up front, while `--require-llm` now performs both reachability and token-count capability probes
- `transcribe-deepgram` is the only supported Deepgram entry point; split / merge behavior is owned by the Python utility
- `verify-quality` is a hard gate only when `hard_failures` is non-empty; `warnings` are advisory review signals, and `checks` now expose explainable readability metrics such as `chunk_seam_warning_count`, `cjk_space_ratio`, `duplicate_ngram_ratio`, `short_paragraph_ratio`, `header_density`, `punctuation_density`, `glossary_drift_count`, and `glossary_preservation_ratio`
- bilingual `verify-quality` now detects adjacent English-to-Chinese paragraph pairs from the full body stream, so short intro paragraphs before paired EN/ZH blocks no longer trigger a false hard failure

### 📘 Canonical Terms

- `bilingual`: English source text plus Chinese translation. It is not subtitle-file merging.
- `run`: `python3 yt_transcript_utils.py run <url>`, the one entry point for a video; it returns one JSON result whose `status` (`done`, `failed`, `needs_user`, `unsupported`, `needs_agent_text`, `in_progress`) has a fixed exit code.
- `Deepgram unified entry`: `python3 yt_transcript_utils.py transcribe-deepgram ...`
- `Deepgram result observability`: `/tmp/${VIDEO_ID}_deepgram_result.json` now exposes per-chunk `chunk_reports`, aggregate structure counts, and `warnings` when segment extraction falls back from richer structures
- `quality gate`: `verify-quality` JSON where `hard_failures` means STOP, `warnings` means review before proceeding, and `checks` carries the readability metrics behind those warnings. Pass `--work-dir` or `--glossary-path` when available to enable glossary drift checks.
- `source routing`: the planning-layer decision about whether the current subtitle/deepgram source should continue as-is. `routing_reason` covers duration/size routing; `source_route_reason` covers source-quality routing.
- `runtime reroute action`: when `plan-optimization` / `verify-quality` reports `reroute_recommended=true` with `reroute_target=deepgram`, runtime contracts normalize that into `recommended_action=fallback_to_deepgram` so policy, evaluator, and decision outputs stay aligned

### 🧪 Validation Matrix

| Scenario | What `run` does | Result |
|----------|-----------------|--------|
| Video with trusted subtitles, short or long | `metadata` → `source` (subtitle track) → `normalize` → `plan` → `text` → `merge` → `verify` → `assemble` → `cleanup` | `done` with `output_file`; `warnings` are advisory |
| No trusted subtitles, or very poor Chinese subtitles | `source` transcribes the audio with Deepgram | `done`; `needs_user` (`deepgram_key_missing`) without a key |
| Video in another spoken language | stops in `source` | `unsupported` |
| Long video on a host with a shell time limit | stops at the first stage or window-round boundary after `--max-seconds` | `in_progress`; rerunning continues |
| No LLM backend reachable | hands one text request at a time to the agent | `needs_agent_text`; rerun after writing the answer |
| Hard quality failure | `verify` stops before any article is written | `failed` (`quality_hard_failure`); `--from text` redoes the text |

Offline end-to-end: `python3 scripts/run_regression.py --fixtures tests/fixtures/e2e` replays recorded videos with a fake yt-dlp and a fake LLM (also run by the test suite).

Live regression: `python3 scripts/run_regression.py` runs the pipeline on a fixed set of real videos and reports `verify-quality` results; see [tests/live_regression/README.md](tests/live_regression/README.md).

### 🛠️ Minimum Commands

```bash
# Transcribe one video; rerun the same command to resume
python3 yt_transcript_utils.py run "$URL" --max-seconds 300

# Free routing check: source, mode and text plan, no LLM or Deepgram
python3 yt_transcript_utils.py run "$URL" --stage plan

# Overrides (--source and --mode are remembered for the video until --restart)
python3 yt_transcript_utils.py run "$URL" --source deepgram   # or subtitles
python3 yt_transcript_utils.py run "$URL" --mode chinese      # or bilingual
python3 yt_transcript_utils.py run "$URL" --from text         # redo this stage and every later one
python3 yt_transcript_utils.py run "$URL" --restart           # start over

# Unfinished runs
python3 yt_transcript_utils.py status                         # every run under /tmp/yt-transcript/
python3 yt_transcript_utils.py status "$VIDEO_ID"
python3 yt_transcript_utils.py cleanup "$VIDEO_ID" --force    # discard an unfinished run

# Debug: rerun the quality check on any file
python3 yt_transcript_utils.py verify-quality "$FILE" --raw-text "$RAW_TEXT"
```

### 📄 License

MIT License

### 🔗 Links

- [Deepgram API Docs](https://developers.deepgram.com/)
- [yt-dlp Project](https://github.com/yt-dlp/yt-dlp)

---

## 中文

将 YouTube 视频转录为格式化的 Markdown 文章。支持字幕下载或 Deepgram 语音转录（包含多角色识别）。

建议先看：
- 想确认统一术语口径，直接看下方 `术语口径`
- 想了解一个视频是怎么处理的，直接看 `run 命令` 和 `最小命令集`

### ✨ 功能特点

- 🎯 **智能字幕获取**：优先使用 YouTube 官方/自动字幕
- 🎙️ **语音转录**：无字幕时自动使用 Deepgram Nova-3 转录
- 👥 **多说话者识别**：自动区分不同讲者
- 🌐 **中英双语支持**：自动翻译并对照排版
- 🤖 **AI 智能优化**：自动添加标点、分段、纠错
- 📝 **Markdown 输出**：带元数据的格式化文章

### 📋 前置依赖

- `yt-dlp`：下载 YouTube 视频/音频/字幕
- `ffmpeg`：音频处理（分割、静音检测）
- `python3`：处理文本格式化
- `curl`：调用 Deepgram API
- [Deepgram 账号](https://console.deepgram.com/)：用于语音转录

#### 安装依赖

```bash
# macOS
brew install yt-dlp python3 ffmpeg

# 或使用 pip
pip install yt-dlp
```

### ⚙️ 配置

1. 复制配置模板：
   ```bash
   cp config.example.yaml config.yaml
   ```

2. 编辑 `config.yaml`，填入你的配置：
   ```yaml
   deepgram_api_key: "your_api_key_here"
   deepgram_model: "nova-3"
   deepgram_enable_utterances: true
   deepgram_prefer_structured_output: true
   output_dir: "~/Downloads"

   # 长视频 chunk 处理的 LLM API 配置（可选）
   # 格式: "openai" 或 "anthropic"
   llm_api_format: "openai"
   llm_api_key: "your_llm_api_key"
   llm_base_url: "https://api.deepseek.com"
   llm_model: "deepseek-v4-pro"
   llm_timeout_sec: 180
   llm_max_retries: 3
   llm_backoff_sec: 1.5
   llm_stream: "auto"
   llm_reasoning_probe_enabled: true
   llm_chunk_recovery_attempts: 1
   llm_chunk_recovery_backoff_sec: 1.0
   ```

   > **注意**：
   > - `deepgram_api_key` 仅在没有可用字幕、需要音频转录时才必需。
   > - Deepgram 转录默认使用 `nova-3`。旧版 `nova-2` / `nova-2-*` 配置会在发起请求前自动提升为 `nova-3`。
   > - `deepgram_enable_utterances` 和 `deepgram_prefer_structured_output` 现在默认都是 `true`；只有在需要和旧的 flat transcript 路径做对照排查时，才建议改成 `false`。
   > - 每个视频的文字都经过 LLM 后端处理。默认（`llm_backend: auto`）用正在调用 skill 的 agent 自己的 CLI，所以 LLM API 配置只是备用；两者都不可用时，`run` 把每个文字请求交给调用它的 agent 处理（`needs_agent_text`）。
   > - `llm_base_url` 可以填写服务根地址或带 `/v1` 的地址，工具会自动归一化。
   > - `llm_stream: "auto"` 会在 provider 支持时优先走流式响应。
   > - `llm_reasoning_probe_enabled: true` 时，未知 OpenAI-compatible 模型会先走一次非流式 `OK` 探测；如果响应里有 reasoning 元数据，后续 chunk 调用会切到非流式以保留恢复所需的 usage 信息。
   > - `bash scripts/preflight.sh --require-llm` 现在会执行一次低成本真实探活并输出延迟。
   > - `llm_backend: "auto"`（默认）会把文字请求交给正在调用 skill 的 agent 自己的 CLI（Claude Code、Codex、OpenCode、pi、Gemini CLI、Cursor）处理，宿主从环境变量自动识别，也可以用 `llm_agent_cli` 指定。CLI 要先通过一次简短试调；试调失败或中途失败时改用上面的 LLM API。`llm_backend: "api"` 始终走 API。`python3 yt_transcript_utils.py resolve-llm-backend` 可以查看本次会用哪一个。

### 🚀 使用方法

#### 作为 Claude Skill 使用

1. 将此目录放入任意 Claude skills 目录（或其他 agent 的 skills 目录）
2. 在对话中提供 YouTube 链接
3. agent 执行 `python3 yt_transcript_utils.py run <url>`，由它完成这个视频的全部处理，再汇报结果（见下方 `run 命令`）

脚本会相对于 skill 目录查找 `config.yaml`，不再强绑定 `~/.claude/skills/yt-transcript`。

#### 单个视频示例

```
请帮我转录这个视频：https://www.youtube.com/watch?v=xxxxx
```

#### 多个视频（批量处理）

可以一次提供多个链接，将**串行处理**（每个链接执行一次 `run`）：

```
请帮我转录这些视频：
- https://www.youtube.com/watch?v=xxxxx
- https://www.youtube.com/watch?v=yyyyy
- https://www.youtube.com/watch?v=zzzzz
```

处理完成后会提供汇总表格，显示每个视频的状态和输出路径。

### 路由说明

- 所有路由决定都由 `run` 自己做出，并记录在该视频的 `state.json`（`route`：字幕还是 Deepgram、哪条字幕轨、哪种模式，以及原因）。
- 短视频和长视频走同样的阶段，不再有单独的 Quick Mode。短文本只跑一轮窗口，也不生成大纲；计划判定为 `long` 时，YouTube 章节作为大纲的提示。
- `run <url> --stage plan` 只查看路由和文字处理计划，不花钱（不调用 LLM，也不调用 Deepgram）。
- 输出文件名（`yyyy-mm-dd. <标题>.md`）和 frontmatter 由 `run` 根据 metadata 生成。

### 📁 项目结构

```
yt-transcript/
├── SKILL.md                 # 宿主 agent 读的说明：执行 `run`，按结果处理（主入口）
├── SYSTEM_DESIGN.md         # 系统设计唯一权威文档
├── prompts/                 # 单任务 Prompt 模板
├── scripts/                 # Shell 辅助脚本
├── yt_transcript_utils.py   # 主 Python 入口（命令行，包括 `run`、`status`、`cleanup`）
├── kernel/                 # kernel 包
│   ├── pipeline.py         # `run` 命令：阶段、state.json、结果和退出码
│   ├── task_runtime/       # 通用任务运行时层
│   │   ├── runtime.py      # ownership、command envelope、telemetry append
│   │   ├── api.py          # 对外稳定 runtime API：create/inspect/advance/control/finalize
│   │   ├── contracts.py    # runtime contracts：task/run/action/artifact envelope
│   │   ├── lifecycle.py    # runtime lifecycle shell 与 transition summary
│   │   ├── policy.py       # allowed-action derivation 与 budget-pressure policy
│   │   ├── evaluator.py    # quality-gated evaluator report 与建议动作
│   │   ├── decision.py     # rule-first action selection 与 decision record
│   │   ├── ledger.py       # runtime budget 与 action accounting 摘要
│   │   ├── recovery.py     # processing substate 与 recovery summary
│   │   ├── artifacts.py    # artifact graph 与持久化产物引用
│   │   ├── state.py        # manifest/runtime 持久化与控制文件
│   │   ├── controller.py   # owned mutation 与 bounded control-loop 辅助
│   │   └── telemetry.py    # telemetry 查询与汇总辅助
│   └── long_text/          # 长文本变换层
│       ├── glossary.py     # glossary 提取与术语检查
│       ├── semantic.py     # verify-quality 用的 semantic anchor 检查
│       ├── contracts.py    # control contract 与 policy state
│       ├── lifecycle.py    # manifest 生命周期与 resume state 辅助
│       ├── prompting.py    # prompt 组装与 chunking context 辅助
│       ├── llm.py          # LLM 请求循环与重试辅助
│       ├── window.py       # 滑动窗口执行器（默认长文本模式）
│       ├── processing.py   # 固定分块处理循环（旧的 `chunks` 模式）
│       ├── chunking.py     # 分块命令表面
│       ├── merge.py        # merge 与 chapter-plan 命令表面
│       └── execution.py    # 执行、执行器选择与 resume 命令表面
├── tests/                   # 回归测试集
├── config.yaml              # 本地配置（已 gitignore）
├── config.example.yaml      # 配置模板
└── README.md                # 本文档
```

### 🏗️ 架构设计总览

`README.md` 是面向操作者的快速上手与命令指南，`SYSTEM_DESIGN.md` 是系统架构唯一的权威设计文档。

从整体上看，`yt-transcript` 是一个 local-first 的系统：一条确定性的命令 `run <url>` 按固定的阶段列表把 YouTube URL 转换成 Markdown 文章：

- `metadata`：用 yt-dlp 取一次视频信息
- `source`：判断说话语言，选可信的字幕轨或用 Deepgram 转写，字幕质量太差时改道
- `normalize`：生成标准化文档（句子和对应的带时间字幕条）
- `plan`：按模式确定文字处理任务并估算规模
- `text`：每个任务都经过长文本变换子系统（滑动窗口、大纲和术语表）
- `merge`、`verify`（质量门禁）、`assemble`（文件名、frontmatter、双语排版）和 `cleanup`

宿主 agent 只负责启动 `run`、按它返回的结果处理、向用户汇报；`SKILL.md` 告诉它怎么做。

代码结构也按照这套设计拆成两层 kernel：`kernel/task_runtime/*` 负责通用长程任务控制，`kernel/long_text/*` 负责长文本变换行为。`yt_transcript_utils.py` 仍然是主 CLI 和 workflow façade，但现在会直接把职责委托给这两层。

Phase 6 进一步加入了 `kernel/task_runtime/api.py`，把 `create-run`、`inspect-run`、`advance-run`、`apply-control`、`resume-run`、`finalize-run` 收敛为首选外部运行时接口；原来的 `runtime-status`、`process-chunks`、`pause-run`、`cancel-run` 等命令仍然保留，但定位为兼容辅助入口。

`run` 的流程不使用这套外部运行时接口；[#23](https://github.com/williamwang-ty/yt-transcript/issues/23) 的第 4 阶段会删除它。

其中最难的内部子系统是长文本变换。每个视频的文字都经过它（短文本只跑一轮窗口），负责窗口处理、一致性保护（大纲 + 术语表）、verification、repair 与确定性 merge。

[阅读详细系统设计文档](SYSTEM_DESIGN.md)

### 🔁 run 命令

`python3 yt_transcript_utils.py run <url>` 是处理一个视频的入口。每个视频有一个工作目录 `/tmp/yt-transcript/<video_id>/`（或 `$YT_TRANSCRIPT_WORK_ROOT/<video_id>/`），里面只有一个 `state.json`。重跑同一条命令就从第一个未完成的阶段继续；已经完成的视频会立即返回 `done` 和已有的文章。

每次调用在 stderr 输出进度，在 stdout 输出且只输出一个 JSON 结果，退出码固定：

| `status` | 退出码 | 含义 |
|---|---|---|
| `done` | 0 | 文章已写好（`output_file`、`route`、`warnings`、`llm`），或 `--stage plan` 已完成 |
| `failed` | 1 | 某个阶段失败：`stage`、`reason`、`message`、`retryable`、`next_command` |
| `needs_user` | 3 | 只有用户能解决：`cookies_required`、`login_required`、`rate_limited`、`deepgram_key_missing`、`tool_missing`、`output_dir_missing` |
| `unsupported` | 4 | 不支持这个说话语言 |
| `needs_agent_text` | 5 | 没有可用的 LLM 后端，把一个文字请求交给 agent（`task.prompt_file`、`task.output_file`） |
| `in_progress` | 6 | `--max-seconds` 的时间用完，停在安全点；重跑即继续 |

退出码 2 表示命令行用法错误。参数：`--source subtitles|deepgram` 和 `--mode chinese|bilingual`（对这个视频一直有效，直到 `--restart`）、`--from <阶段>`、`--stage plan`、`--max-seconds <n>`、`--restart`、`--config-path`。`status [<video_id>]` 列出未完成的运行或显示一个视频的状态；`cleanup <video_id> [--force]` 删除工作目录。`$YT_TRANSCRIPT_OUTPUT_DIR` 可以替换配置里的 `output_dir`。

### 🔧 预检模式

`run` 在每个阶段开始前检查它需要的东西（yt-dlp；只在选中 Deepgram 时检查 Deepgram key；到文字处理阶段才检查 LLM 后端），所以预检不再是必需的步骤。这些检查仍可用于排查问题：

```bash
# 基础检查：yt-dlp、python3、配置
bash scripts/preflight.sh

# 同时检查 Deepgram key
bash scripts/preflight.sh --require-deepgram

# 同时探测 LLM API
bash scripts/preflight.sh --require-llm
```

### 🧩 结构化脚本输出

下面这些是 `run` 在内部调用的步骤，agent 不再逐条执行它们。在 [#23](https://github.com/williamwang-ty/yt-transcript/issues/23) 的第 4 阶段把其中大部分改为内部函数之前，它们仍可用于调试。它们会在 stdout 输出可解析 JSON，让路由、校验与执行决策留在代码里：

- `scripts/download.sh "$URL" metadata`
- `scripts/download.sh "$URL" subtitle-info`
- `scripts/download.sh "$URL" subtitles`
- `scripts/download.sh "$URL" audio`
- `python3 yt_transcript_utils.py get-chapters "$URL"`
- `python3 yt_transcript_utils.py chunk-segments /tmp/${VIDEO_ID}_segments.json /tmp/${VIDEO_ID}_chunks --prompt <RAW_STAGE_PROMPT>`
- `python3 yt_transcript_utils.py chunk-document /tmp/${VIDEO_ID}_normalized_document.json /tmp/${VIDEO_ID}_chunks --prompt <RAW_STAGE_PROMPT>`
- `python3 yt_transcript_utils.py prepare-resume /tmp/${VIDEO_ID}_chunks --prompt <RAW_STAGE_PROMPT>`
- `python3 yt_transcript_utils.py build-chapter-plan /tmp/${VIDEO_ID}_chapters.json /tmp/${VIDEO_ID}_chunks /tmp/${VIDEO_ID}_chunks/chapter_plan.json`
- `python3 yt_transcript_utils.py build-glossary /tmp/${VIDEO_ID}_chunks --mode transcript`
- `python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage <stage>`
- `python3 yt_transcript_utils.py normalize-document /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt`

这里的 `<RAW_STAGE_PROMPT>` 由 `plan-optimization` 决定：

- 中文单语输出使用 `cleanup_zh`
- 英文源双语输出使用 `structure_only`

这样具体判断逻辑都留在脚本里，而不是写在 prompt 说明中。

`plan-optimization` 现在还会输出标准化的 chunk 执行契约。

在整个项目层面，它是 source acquisition 和 text transformation 之间的路由边界；在长文本子系统层面，它定义了后续 chunk 执行必须遵循的 execution contract：

- `operations[*].kind`（`chunk` 或 `prompt`）、`prompt`、`input_key` 或 `input` / `output`
- `operations[*].execution.mode`（`chunked` 或 `single_pass`）

`normalize-document` 会基于 raw text 或带时间戳的 `segments.json` 物化 `/tmp/${VIDEO_ID}_normalized_document.json`；当源 artifact 已存在时，`plan-optimization` 也会自动完成这一步。如果这些 segments 来自字幕清洗阶段，标准化文档还会把轻量清洗诊断透传到 `diagnostics.subtitle_cleanup`，并在 `diagnostics.subtitle_quality` 下写入可解释的字幕源质量摘要。

`plan-optimization` 现在把“时长/体积路由”和“源路径质量路由”分开表达：`routing_reason` 仍只解释 short/long 执行路径，而 `source_route_reason`、`subtitle_quality_score`、`reroute_recommended`、`reroute_target` 用来解释当前字幕源是继续沿用、只做人工复核，还是建议切换到 Deepgram。

对于长视频分块，`plan-optimization` 现在还会输出显式的 `chunking` 契约；一旦 normalization 已存在，优先使用 `chunk-document`，并把 chunk 边界显式记录到 `manifest.json`（滑动窗口执行器也从这些 chunk 文件读原文）。

当前设计还包含显式的 resume 语义：`prepare-resume` 用于手动修复 stale manifest，而 `process-chunks` 在继续执行前会自动做同样的修复。

未完成的运行会保留工作目录：`run` 自己的 cleanup 阶段只在文章写好后才执行；`python3 yt_transcript_utils.py cleanup <video_id>` 遇到未完成的运行会拒绝，除非加 `--force`（明确放弃）。`python3 yt_transcript_utils.py status` 会列出中断的运行、下一个阶段、上次的结果，以及继续它的命令。

已知限制：所有 state 和中间文件都放在 `/tmp` 下，macOS 重启时会清空 `/tmp`（不少 Linux 系统也是如此）。因此续跑只在同一次开机内有效，重启后中断的任务需要从头开始。中断的任务还会把已下载的音频一直留在 `/tmp`，直到任务完成或用 `cleanup <video_id> --force` 放弃，可能占用几百 MB。把工作目录迁到持久位置（例如 `~/.cache/yt-transcript`）暂时搁置。

当前约定是明确固定的：

- 长文本各阶段都运行 `process-chunks`；默认 `long_text_mode: window` 走滑动窗口执行器，`long_text_mode: chunks` 保留旧的固定分块执行器
- 已经有固定分块完成进度的工作目录，续跑时继续用 `chunks` 模式

### 🧭 设计上的刻意取舍

- `bilingual` 表示“英文源文本 + 中文翻译”，不是直接合并双字幕文件
- `subtitle-info` 先判断视频说的是什么语言（依次看原声音轨、metadata 的 `language`、自动识别字幕轨、人工字幕轨），只信任人工字幕和该语言的自动识别字幕；YouTube 的自动翻译字幕一律不用
- 任何视频只要有人工中文字幕，就优先用它作为唯一源文本；否则中文视频用中文字幕，英文视频用英文字幕
- 其他语言的视频会报告为暂不支持（`source_language_supported=false`），不会继续转录；Deepgram 路径使用判定出的语言，metadata 没有语言信息时用 `--language auto` 让 Deepgram 自己识别
- 中文单语优化现在使用专门的 `cleanup_zh` prompt，在不改变原意的前提下修复标点、分段、重复字幕碎片和明显的中文空格问题
- `config.yaml` 被刻意限制为扁平的顶层键值配置，不支持嵌套结构或多行 YAML
- YAML frontmatter 的值会统一加引号，优先保证解析安全，而不是追求最简洁的展示
- Markdown 头部里的标题/频道文本会做转义，链接目标会做编码，避免边界字符破坏结构
- `chunk-document` 现在是 `normalized_document.json` 已存在时的规范长视频分块入口；它会遵循 `preferred_chunk_source`，而不是在有 timed segments 时一律偏向 `segments`；因此中文字幕长路径现在会优先使用清洗后的 `text` 做正文分块，同时保留 `segments` 供时间轴 / 章节映射使用
- `chunk-text` 会对超长且缺少标点的段落做强制切分，并在提供 `--prompt` 时默认启用 token-aware 规划
- `download.sh metadata` 现在会优先走单次 `yt-dlp -J` 获取；字幕/音频模式也会先复用 metadata 里的 video id，再回退到额外探测
- `transcribe-deepgram` 默认使用 Deepgram `nova-3`；旧版 `nova-2` / `nova-2-*` 模型配置会在运行时提升为 `nova-3`
- `transcribe-deepgram --output-segments` 可选输出带时间戳的对齐 segments，用于后续 timed chunk 与 YouTube 章节映射
- `transcribe-deepgram` 现在默认就是 utterance-first 组装；仍保留 `--disable-utterances --legacy-flat-output` 作为兼容/排障回退
- `transcribe-deepgram` 现在还会在结果 JSON 中输出轻量可观测字段，例如 paragraph/sentence/word 计数、逐 chunk 的 transcript 元数据，以及 structured-output 回退 warning
- `download.sh` 会把字幕和音频文件写到按视频隔离的临时目录 `/tmp/${VIDEO_ID}_downloads/...`，并在 JSON 里返回 `download_dir`，便于确定性地选取和清理
- `download.sh subtitles` 会请求所选字幕的精确语言代码，因此 `en-GB` / `zh-TW` 这类地区变体也能下载，不会被写死的白名单丢掉
- `download.sh subtitles` 现在会按“中文优先、英文回退”的顺序一次只尝试一个源字幕轨；当可见的中文字幕下载失败时，才会继续尝试英文字幕
- 字幕驱动的 workflow 仍支持“中文字幕单语输出”和“英文字幕双语输出”两条路径；其中中文单语现在优先走 `cleanup_zh`，英文字幕仍走 `structure_only -> translate_only`
- `download.sh subtitles` 现在会显式区分“平台列出了哪些轨”和“当前运行环境实际下载到了哪条轨”：`listed_candidates` 描述可见候选，`attempted_candidates` / `blocked_candidates` / `fallback_used` 描述实际下载结果
- 当首选字幕轨因为 `HTTP 429` 这类鉴权/限流问题失败时，`download.sh subtitles` 现在会先用 Chrome cookies 对同一条轨重试，再决定是否回退到下一条候选
- `chunk-segments` 基于 segments 生成带时间轴的 timed manifest；`build-chapter-plan` 可将 YouTube chapters 映射到 chunk 边界，供 `merge-content` 注入标题
- `parse-vtt` / `parse-vtt-segments` 现在都会做 subtitle-aware cleanup，避免 CJK 字幕碎片在重新拼接时被错误插入 ASCII 空格
- `parse-vtt-segments` 现在还会输出轻量 cleanup diagnostics，例如重复 cue / overlap 裁剪计数；`normalize-document` 会把这些字幕清洗信号以及确定性的 `subtitle_quality_score` 一并透传进 `normalized_document.json`
- `plan-optimization` 现在会显式输出 `source_route_reason`、`reroute_recommended`、`reroute_target`、`reroute_reasons` 等源路径解释字段；当中文字幕路径质量极差时，它会建议切到 Deepgram，但不会静默改写当前 workflow shell
- `merge-content` 现在会只对合并后的正文 body 执行 deterministic post-merge cleanup：它会修复 chunk seam 重复、以更保守的续写片段规则重新拼合被拆开的正文、原样保留显式传入的 header/frontmatter，并去掉紧邻重复的标题/正文接缝，而不是把这些机械问题继续留给 LLM
- `chunk-segments --chapters` 可选在 YouTube 章节起点强制切 chunk，减少章节标题漂移
- 如果只传显式 `--chunk-size` 而不传 `--prompt`，`chunk-text` 会继续按 legacy 字符大小解释，避免现有 workflow 被静默改变
- 分块阶段会提前校验 prompt 名称，避免因为 prompt 拼写错误而静默回退到通用预算
- `process-chunks` 把 `max_tokens` 当作停止线（每个任务默认 8192，只受上下文窗口约束），不再从输入预算里切一块给输出，思考 token 不会再挤掉正文
- 分块响应如果停在 `max_tokens`（任何厂商的 `finish_reason` = `length` / `max_tokens` / `max_output_tokens`），会加大上限重试（`llm_output_retry_*`，旧名 `llm_reasoning_retry_*`）；重试后仍被截断的块会标记为 `truncated_output`
- `long_text_mode: window`（默认；`chunks` 作为旧方案保留兜底）用滑动窗口处理 `cleanup_zh`、`quick_cleanup` 和双语的 `structure_only` → `translate_only` 两步：原文按单元编号（没有标点的长句在字幕条结束处切开，优先选停顿最长的地方，前提是 `normalized_document.json` 里的带时间字幕能和原文对上，切块时切在字幕条中间的地方也一并重切；对不上就每约 120 token 硬切），从游标读一段，按软目标写，模型把每个单元的 `[S<n>]` 编号原样写在对应文字前面，最后输出 `<!--END-->`，每段到哪里结束由程序推算。完整段落存到 `<work_dir>/window/`（翻译阶段在 `window/processed/`），没有结束行时只丢掉最后一段，游标移到保留内容之后，截断只是少推进一点。窗口每轮自适应：写目标从 `window_write_tokens` 开始，被截断或太慢就调小；读窗口跟着写目标和实测的输出/原文比例走（上限 `window_read_tokens`），尽量不重复读；剩下的原文一轮能写完就一次发完。输出比原文短太多或长太多的一轮按无效处理。API 和命令行后端用法一样，断点续跑精确到段落，连续失败就把这一段按固定分块处理，并报告覆盖率和重复阅读比例。已经有固定分块完成进度的工作目录继续用固定分块续跑，窗口不覆盖的阶段（如 `summarize`）也用固定分块
- 窗口模式下，`cleanup_zh` 和 `structure_only` 会先生成全局大纲（`window_outline: true`）：把带编号的全文交给模型一次（超过 `window_outline_read_tokens`，默认 16000，就分段生成再合并），返回一份短 JSON：章节列表（标题 + 起始单元编号，校验在范围内且递增）和人名、术语表（正确写法和原文里出现的错写）。每轮请求带上窗口附近的大纲和窗口里出现的术语；标题只来自大纲，由模型在章节起始句附近话题真正开始的地方写出，没写就由程序插入，全文的章节划分一次定好。YouTube 章节（`chunk-document --chapters` 或 `chapter_plan.json`）会映射到单元编号作为提示。双语流程的标题是英文 + 中文，术语的中文译法会带到 `translate_only`。大纲存在窗口运行状态里，断点续跑直接复用；生成失败时退回到每轮自己插标题
- `manifest.json` 会显式记录 `plan.chunk_contract`，固定分块任务续跑时沿用原来的分块大小
- chunk 执行现在也有显式 resume 语义：在继续执行前，stale 的 `running` / 缺失输出 checkpoint 会被确定性修复为 `done` 或 `interrupted`
- `process-chunks` 现在会把 `Remote end closed connection without response` 这类瞬时网关断连视为可重试传输错误，并可在产出异常短/结构异常的 chunk 时自动重跑一轮，再决定是否保留 warning
- `process-chunks --dry-run` 只校验 prompt、manifest 和分块预算，不需要真实的 LLM 凭据，也不探测后端；真正执行时需要一个可用的后端：调用方 agent 的 CLI（`llm_backend: auto`），或者 `llm_api_key`、`llm_base_url` 和 `llm_model`
- `plan-optimization` 是规范的长短路由：`< 1800s = short`，`>= 1800s = long`；两者走同样的 `run` 阶段（已经没有 Quick Mode），短文本只跑一轮窗口
- `manifest.json` 现在会把不可变 `plan` 和可变 `runtime` 状态分开，同时为每个 chunk 记录 `attempt_logs` 级别的请求观测数据
- 固定分块按每个任务的分块目标（`chunk_tokens_*`）切，硬上限是目标的 `chunk_hard_cap_multiplier` 倍；原来为小固定分块加的 continuity 上下文、autotune 和 `replan-remaining` 补丁已随窗口模式转正删除，固定分块连续超时多次后直接停止
- `build-glossary --mode transcript` 现在会从 raw chunk、可用的 normalized document 标题/频道、chapter 标题，以及可选 metadata/description 上下文中抽取术语；当 `cleanup_zh` 的 work_dir 里还没有 glossary 时，`process-chunks` 会自动生成这一份 glossary
- glossary 的筛选与校验现在会对简单 ASCII 术语做边界感知匹配，因此像 `API` 这样的缩写不会再因为 `rapid`、`capital` 之类的无关单词而误命中
- `run_kernel_command(...)` 是 kernel 命令稳定的 Python envelope API；`python3 yt_transcript_utils.py --api-envelope ...` 在 CLI 上输出同样的 `yt_transcript.command_result/v1` envelope，不影响原有的扁平 JSON 输出
- 产出 envelope 的 kernel 命令在能推断出稳定的就近路径时，会把 `yt_transcript.telemetry_event/v1` 记录追加到本地 `telemetry.jsonl`
- `runtime.status` 现在会区分 `completed` / `completed_with_errors` / `aborted`
- 运行时 token 估算默认仍是本地启发式 fallback；`test-token-count` / `preflight.sh --require-llm` 会探测 provider 级 token count，并在不可用时明确回退到 local estimate
- `chunk_hard_cap_multiplier` 会被限制在保守的 `1.0-2.0` 区间，避免配置失误把 chunk 包络静默放大
- `preflight.sh` 采用分层校验，确保只走字幕路径时不必预先配置 Deepgram 或 LLM 凭据；进入 `--require-llm` 时会同时做连通性和 token count 能力探测
- `transcribe-deepgram` 是唯一支持的 Deepgram 统一入口，分片与合并逻辑由 Python 工具统一负责
- `verify-quality` 只有在 `hard_failures` 非空时才阻断流程；`warnings` 仅用于人工复核提示，而 `checks` 现在会额外暴露 `chunk_seam_warning_count`、`cjk_space_ratio`、`duplicate_ngram_ratio`、`short_paragraph_ratio`、`header_density`、`punctuation_density`、`glossary_drift_count`、`glossary_preservation_ratio` 等可解释指标
- 双语 `verify-quality` 现在会在完整正文流里识别相邻的“英文段 -> 中文段”配对，因此对照正文前面即使有简短导语，也不会再被误判为完全缺少双语段对

### 📘 术语口径

- `bilingual`：英文源文本 + 中文翻译，不是双字幕文件合并。
- `run`：`python3 yt_transcript_utils.py run <url>`，处理一个视频的唯一入口；它返回一个 JSON 结果，`status`（`done`、`failed`、`needs_user`、`unsupported`、`needs_agent_text`、`in_progress`）对应固定的退出码。
- `Deepgram 统一入口`：`python3 yt_transcript_utils.py transcribe-deepgram ...`
- `Deepgram 结果可观测性`：`/tmp/${VIDEO_ID}_deepgram_result.json` 现在会暴露逐 chunk 的 `chunk_reports`、聚合结构计数，以及 structured-output 回退时的 `warnings`
- `质量门禁`：读取 `verify-quality` 的 JSON；`hard_failures` 表示必须 STOP，`warnings` 表示需要人工复核，`checks` 则给出这些告警背后的可读性指标。有 work_dir 或现成 glossary 时，建议额外传 `--work-dir` 或 `--glossary-path` 打开 glossary drift 检查。
- `源路径路由`：规划层决定当前 subtitle / Deepgram 源是否继续沿用。`routing_reason` 负责解释时长/输入体积路由，`source_route_reason` 负责解释源质量路由。
- `运行时 reroute 动作`：当 `plan-optimization` / `verify-quality` 返回 `reroute_recommended=true` 且 `reroute_target=deepgram` 时，runtime contract 会把它标准化为 `recommended_action=fallback_to_deepgram`，这样 policy、evaluator、decision 三层动作口径保持一致
- `输出文件命名`：最终 Markdown 文件名里的日期片段必须使用 `yyyy-mm-dd`，不要把 metadata 的原始 `upload_date`（`yyyymmdd`）直接放进文件名。

### 🧪 验证矩阵

| 场景 | `run` 做什么 | 结果 |
|------|-------------|------|
| 有可信字幕的视频，长短都一样 | `metadata` → `source`（字幕轨）→ `normalize` → `plan` → `text` → `merge` → `verify` → `assemble` → `cleanup` | `done` 和 `output_file`；`warnings` 只作提示 |
| 没有可信字幕，或中文字幕质量极差 | `source` 用 Deepgram 转写音频 | `done`；没有 key 时 `needs_user`（`deepgram_key_missing`） |
| 说其他语言的视频 | 在 `source` 停下 | `unsupported` |
| 长视频，宿主的 shell 有时间上限 | 超过 `--max-seconds` 后在第一个阶段或窗口轮次边界停下 | `in_progress`；重跑即继续 |
| 没有可用的 LLM 后端 | 每次把一个文字请求交给 agent | `needs_agent_text`；写好答案后重跑 |
| 质量检查硬失败 | `verify` 停下，不写文章 | `failed`（`quality_hard_failure`）；`--from text` 重做文字处理 |

离线端到端测试：`python3 scripts/run_regression.py --fixtures tests/fixtures/e2e` 用假的 yt-dlp 和假的 LLM 回放录好的视频（测试集也会跑）。

真实视频回归：`python3 scripts/run_regression.py` 会在一组固定的真实视频上跑完整流程并汇总 `verify-quality` 结果，详见 [tests/live_regression/README.md](tests/live_regression/README.md)。

### 🛠️ 最小命令集

```bash
# 转录一个视频；重跑同一条命令就是续跑
python3 yt_transcript_utils.py run "$URL" --max-seconds 300

# 免费的路由检查：来源、模式和文字处理计划，不调用 LLM 和 Deepgram
python3 yt_transcript_utils.py run "$URL" --stage plan

# 覆盖参数（--source 和 --mode 会记住，对这个视频一直有效，直到 --restart）
python3 yt_transcript_utils.py run "$URL" --source deepgram   # 或 subtitles
python3 yt_transcript_utils.py run "$URL" --mode chinese      # 或 bilingual
python3 yt_transcript_utils.py run "$URL" --from text         # 重做这个阶段及之后的所有阶段
python3 yt_transcript_utils.py run "$URL" --restart           # 从头开始

# 未完成的运行
python3 yt_transcript_utils.py status                         # /tmp/yt-transcript/ 下的所有运行
python3 yt_transcript_utils.py status "$VIDEO_ID"
python3 yt_transcript_utils.py cleanup "$VIDEO_ID" --force    # 放弃一个未完成的运行

# 调试：对任意文件重跑质量检查
python3 yt_transcript_utils.py verify-quality "$FILE" --raw-text "$RAW_TEXT"
```

### 📄 许可证

MIT License

### 🔗 相关链接

- [Deepgram API 文档](https://developers.deepgram.com/)
- [yt-dlp 项目](https://github.com/yt-dlp/yt-dlp)
