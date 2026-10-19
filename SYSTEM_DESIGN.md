# yt-transcript System Design / yt-transcript 系统设计

## English Version

> [!IMPORTANT]
> **This document describes the target architecture.** It is being implemented step by step under tracker [#23](https://github.com/williamwang-ty/yt-transcript/issues/23). Until the later stages land, the repository still runs the old flow, in which the agent follows `SKILL.md` and `workflows/*.md` command by command. Section 9 maps today's code to the target, names the stage that moves each piece, and lists the behavior changes.

## 1. Document Positioning

This document explains the **system design of the whole `yt-transcript` project**: what it builds, how the work is divided between code and the model, and why.

`README.md` is the operator-facing quickstart. `SKILL.md` is the short contract the host agent reads. This document is neither; it explains structure, boundaries, and trade-offs. It is not a coding spec: field names, file names, and reason codes below show the shape of the design and may still change while it is implemented.

It works at two levels:

- **Project level** (sections 2 to 6): how one command turns a YouTube URL into a Markdown article, and what the agent around it is responsible for.
- **Subsystem level** (section 7): how the hardest stage, long-text transformation, works inside that command.

`SYSTEM_DESIGN.md` is the single authoritative design document for this repository.

---

## 2. What the Project Is Actually Building

`yt-transcript` is a **local transcript production system** packaged as an agent skill. Given a YouTube URL it produces one formatted Markdown article in the user's notes folder.

It has two parts with a hard line between them:

- **`run <url>`**, a deterministic command that owns the whole path: metadata, source selection, subtitle download or Deepgram transcription, normalization, planning, LLM text processing, merge, quality check, assembly, and cleanup. It records everything in one state file per video and can always be rerun to continue.
- **The host agent** (Claude Code, Codex, and so on), which starts `run`, handles the few states `run` hands back (the user must act, the language is unsupported, a step failed, or no LLM backend is reachable), turns one-off user requests into override flags, runs several URLs one after another, and reports the result.

The LLM is used only to transform text. It never decides which step comes next.

---

## 3. Project-Level Problems the System Must Solve

### 3.1 Source availability is uncertain

A YouTube video may have human subtitles, only YouTube's speech-recognition (ASR) track, machine-translated tracks that look like real subtitles, subtitles of very poor quality, or nothing usable at all. YouTube may also refuse requests (bot checks, login walls, rate limits). The system cannot assume one acquisition path always works.

### 3.2 Routing decisions must be reliable

The system must decide, every time and the same way:

- which language the video is spoken in, and whether it is supported
- which subtitle track to trust, or whether to transcribe the audio
- whether the subtitles are good enough or must be replaced by Deepgram
- whether the output is Chinese only or English plus Chinese

These decisions should come from code reading structured data, not from a model interpreting prose.

### 3.3 Text optimization is model-sensitive

Turning transcript text into a readable article (restoring punctuation and paragraphs, fixing recognition errors, adding headings, translating) is where LLMs add the most value, and also where weak models and overloaded prompts fail most easily.

### 3.4 Long videos break naive workflows

Past a certain length, the text no longer fits one reliable call, interruptions become likely, headings and terminology drift between pieces, and seams between pieces cut sentences. Long-text transformation is the hardest subsystem (section 7).

### 3.5 The flow itself must not depend on the model following instructions

In the old design the agent executed the flow by reading about 370 lines of `SKILL.md` plus three workflow documents, running some twenty commands, and hand-writing a `state.md` file. That had predictable costs:

- weaker hosts skipped or reordered steps, and the documented Quick Mode skipped planning and the quality check outright
- the flow could not be tested end to end, because it existed only as prose
- every observed failure was answered with another compensating layer (state validation, a runtime contract layer, policy and decision records), and none of them could be removed safely because nothing proved they were unused

So the flow has to be code that can be tested, while the model keeps the work that actually needs judgment.

### 3.6 The project must remain operable

The user and the agent need to see what path was chosen and why, what has been done, what it cost, and how to continue after an interruption, without reading code or logs.

---

## 4. Project-Level Design Goals

> Code runs every path that is known in advance; the model handles what needs judgment, the exceptions, and the conversation with the user.

Concretely:

- **One entry point**: `run <url>` goes from URL to final Markdown; the agent does not chain internal commands.
- **Deterministic routing**: language, source, reroute, and mode decisions are computed from structured data and recorded with their reasons.
- **Resumable by rerunning**: every stage is idempotent and recorded; rerunning the same command continues from the first unfinished stage.
- **Explicit handback**: `run` stops only at clean breakpoints and returns a small, fixed set of states, each with a reason and a next step.
- **Model-tolerant**: prompts stay narrow and the program owns boundaries, so a weak model produces a worse article, not a broken run.
- **Provider-agnostic**: text processing works with any host agent CLI or any OpenAI-compatible API, with no provider-specific request fields.
- **Host-agnostic**: nothing assumes a particular agent's tools; long runs fit hosts whose shell tool has a time limit.
- **Testable end to end**: the whole flow runs offline against recorded fixtures and a fake LLM.
- **Small surface**: few public commands and few config switches; replaced code is deleted, not kept behind flags.

---

## 5. Overall System Design

```text
User ──> Host agent (reads SKILL.md, a few dozen lines)
            │  run <url> [overrides]
            v
        ┌──────────────────────── run ─────────────────────────┐
        │ metadata -> source -> normalize -> plan -> text       │
        │   -> merge -> verify -> assemble -> cleanup           │
        │                                                       │
        │ /tmp/yt-transcript/<video_id>/state.json  (+ lock)    │
        │ text stage -> LLM backend: host CLI | API | agent     │
        └───────────────────────────┬───────────────────────────┘
                                    │ one JSON result + exit code
                                    v
   done | in_progress | needs_user | unsupported | failed | needs_agent_text
                                    │
                                    v
   Host agent: report, rerun, ask the user, or do one text task and rerun
```

The key idea:

> The middle of the flow (parse, route, number the text, process it in windows, merge, check, assemble) is deterministic, so it lives in code where it can be tested and where fixes accumulate. Surprises happen at the edges (YouTube, yt-dlp, external CLIs, the user's intent), so `run` stops there with an explicit state and lets the model decide.

---

## 6. Detailed Design

### 6.1 Entry point and responsibilities

| Concern | Owned by `run` (code) | Owned by the host agent (model) |
|---|---|---|
| Flow order, skipping, resuming | yes | no |
| Language, source, reroute, mode decisions | yes, with recorded reasons | only through override flags the user asked for |
| Text transformation | through an LLM backend | only when handed a `needs_agent_text` task |
| Filenames, dates, frontmatter, title cleanup | yes | no |
| Cookies, login, rate limits, missing keys or tools | detects them and returns `needs_user` | tells the user what to do |
| Failures | returns `failed` with a reason and the rerun command | decides whether to retry, change an override, or report |
| Several URLs | one video per call | calls `run` once per URL, in order, and tabulates results |
| Reporting | returns the output path, route, warnings, and usage | tells the user |

The agent never edits the state file and never runs internal stages by hand. If it needs a different outcome, it reruns `run` with an override flag.

### 6.2 Stages

`run` executes a fixed list of stages. Each stage reads what earlier stages recorded, writes its outputs atomically into the video's work directory, and only then marks itself `done` in `state.json`. A stage that is `done` and whose recorded outputs still exist is skipped on a rerun.

| Stage | What it does | Main outputs | Can stop with |
|---|---|---|---|
| `metadata` | Fetch the video's yt-dlp info once (title, channel, date, duration, chapters, track list, audio-track languages); later stages read this file instead of calling yt-dlp again | `metadata.json` | `needs_user` (bot check or login after the automatic browser-cookie retries, rate limit, yt-dlp missing), `failed` |
| `source` | Detect the spoken language and pick the trusted track (6.3); download it and parse it into raw text and timed segments; score the subtitle quality and switch to Deepgram when the scoring rule calls for it; or transcribe with Deepgram directly | `source/`, `raw_text.txt`, `segments.json`, the route decision | `unsupported`, `needs_user` (preferred track rate-limited, Deepgram key missing), `failed` |
| `normalize` | Build the normalized document (sentences with their timed cues) from the segments | `normalized_document.json` | `failed` |
| `plan` | Choose the text tasks for the mode (Chinese: `cleanup_zh`; bilingual: `structure_only`, then `translate_only`) and estimate their size (input tokens, expected rounds) | `plan.json` | `failed` |
| `text` | Run each planned task through the window executor (section 7); each task is recorded separately | `text/<task>/` | `needs_agent_text`, `in_progress`, `failed` |
| `merge` | Concatenate the final task's paragraphs in order | `optimized.md` | `failed` |
| `verify` | Run the quality check against the raw text | `quality.json` | `failed` (hard failures only; warnings are recorded) |
| `assemble` | Build the final file: frontmatter, cleaned title, `yyyy-mm-dd. <title>.md` filename, bilingual layout | the article in `output_dir` | `failed` |
| `cleanup` | Delete the intermediate files once the article exists; keep `state.json` | — | — |

Notes:

- **Idempotent stages.** Rerunning an unfinished stage starts that stage over, except that expensive sub-steps keep their results inside the stage (downloaded audio, Deepgram chunk results, the outline, finished window rounds), so an interrupted stage never pays twice.
- **No separate preflight.** Each stage checks what it needs right before it needs it: yt-dlp before `metadata`, the Deepgram key only when `source` chooses Deepgram, an LLM backend only at `text`. Cheap paths stay cheap.
- **The source is final before any LLM call.** The subtitle quality reroute moves from planning into `source`, so `normalize` and everything after it work on accepted text.
- **One path for every length.** Short and long videos go through the same stages. A short video's text fits one window round, and the outline pass is skipped when the whole text fits one round. YouTube chapters are passed to the outline as hints when the plan says `long`; a short text has no outline to use them.
- **Free routing check.** `--stage plan` runs `metadata` through `plan` without spending money. If the route needs Deepgram, it records that decision and stops after the decision in `source`, without transcribing.

### 6.3 Source selection

These rules exist today and only move into the `source` stage:

1. Detect the spoken language: the audio track YouTube marks as original first, then metadata `language`, then an `xx-orig` ASR track, then a non-translated ASR track.
2. Trust only human subtitles and the ASR track in the spoken language. YouTube's machine-translated tracks are dropped.
3. Chinese video: human Chinese subtitles, else the Chinese ASR track; mode `chinese`.
4. English video: human Chinese subtitles if present (mode `chinese`), else English subtitles (mode `bilingual`: English source plus Chinese translation).
5. Other spoken languages: `unsupported`, unless human Chinese subtitles exist.
6. No trusted track, or a track whose quality score triggers the reroute rule (today: critical quality on the `chinese` route): transcribe with Deepgram in the detected language. Deepgram is automatic; there is no confirmation step. A Deepgram result in an unsupported language also returns `unsupported`.

One rule changes. When YouTube refuses the preferred track (rate limiting) after the automatic retries, the old flow silently fell back to the next track, for example English subtitles instead of human Chinese ones, which turned a Chinese article into a bilingual one and made results differ between runs. `source` now returns `needs_user` with reason `rate_limited` and names the fallback track. The agent can rerun later, or accept the fallback explicitly with an override (`--mode bilingual` in that example).

`yt-dlp` stays the acquisition dependency. Direct YouTube InnerTube fetching was evaluated: it is lighter for subtitle-only runs but is an unofficial, interface-sensitive surface, does not remove the need for the Deepgram fallback, and would move extraction maintenance into this project. It remains a possible future fast path, not the default.

### 6.4 Work directory and state

Each video has one work directory:

```text
/tmp/yt-transcript/<video_id>/
  state.json                 the only state file
  .runtime_owner.json        the lock, held while a run is active
  metadata.json  chapters.json
  source/                    downloaded track or audio, subtitle quality check, Deepgram JSON and chunk cache
  raw_text.txt  segments.json  normalized_document.json
  plan.json
  text/                      window executor state for every task (cursor, outline, sizes, paragraphs)
  agent_task/                present only while a needs_agent_text task is open
  optimized.md  quality.json
```

`YT_TRANSCRIPT_WORK_ROOT` replaces `/tmp/yt-transcript` and `YT_TRANSCRIPT_OUTPUT_DIR` replaces the configured `output_dir`; the regression runner uses both so it never touches real runs or the vault.

`state.json` is written atomically after every stage and every window round. Its shape:

```json
{
  "schema": 1,
  "video_id": "abc123",
  "url": "https://www.youtube.com/watch?v=abc123",
  "status": "running",
  "overrides": {"source": null, "mode": null},
  "route": {
    "source_language": "en",
    "language_signal": "original_audio_track",
    "source": "subtitles",
    "track": "en",
    "track_kind": "manual",
    "mode": "bilingual",
    "subtitle_quality": {"score": 0.82, "band": "good"},
    "blocked_tracks": [],
    "rerouted_from": null,
    "reason": "English video with human English subtitles"
  },
  "stages": {
    "metadata": {"status": "done", "seconds": 2.1, "outputs": ["metadata.json"]},
    "text": {"status": "running", "tasks": {
      "structure_only": {"status": "done", "rounds": 4},
      "translate_only": {"status": "running", "rounds": 2}
    }}
  },
  "llm": {"backend": "claude-cli", "requests": 9, "input_tokens": 41200, "output_tokens": 18700},
  "warnings": [],
  "last_result": {"status": "failed", "stage": "text", "reason": "llm_timeout"},
  "output_file": null
}
```

Rules:

- **Resume** means rerunning the same command: `run` reads `state.json` and starts at the first stage that is not `done`. Inside `text`, the window executor resumes at its cursor.
- **Finished runs stay finished.** `cleanup` keeps `state.json`, so rerunning `run` on a finished video returns `done` with the existing article at once, as long as that file still exists (if it was deleted, the run starts over). Rerunning a whole batch therefore only does the unfinished videos. `--restart` produces the article again.
- **Overrides are sticky.** `--source` and `--mode` are saved in `state.json` and stay in effect on reruns of that video until `--restart`, so a resume never silently flips the route.
- **Config is read on every run**, but finished stages are not redone when it changes. `--from <stage>` redoes them. After `cleanup` the intermediate files are gone, so `--from` on a finished run redoes every stage; it is meant for unfinished runs (for example `--from text` after a quality failure).
- **The lock** keeps two processes from writing one work directory. A second `run` on a locked video returns `failed` with reason `locked`; a lock left by a dead process is taken over. Different videos can run in parallel because they share no files.
- **What it replaces**: the hand-written `state.md`, `machine_state.json`, the chunk `manifest.json`, `telemetry.jsonl`, and the scattered `/tmp/<video_id>_*` files. Downloads and the normalized document move under the video's own directory, so runs never collide on shared paths.
- **Hygiene**: `video_id` must match `[A-Za-z0-9_-]+` before it becomes a path. `state.json` never holds API keys; it records the backend's name, not its credentials.
- **Lifetime**: the directory lives under `/tmp`, so a reboot loses unfinished runs. This is an accepted limitation.

### 6.5 Results handed back to the agent

Every `run` call prints exactly one JSON object on stdout and exits with a fixed code. Progress lines (stage, round n of m) go to stderr.

| `status` | Exit | Meaning | What the agent does |
|---|---|---|---|
| `done` | 0 | Article written, or `--stage plan` finished | Report `output_file`, route, warnings, and LLM usage |
| `failed` | 1 | A stage failed: `stage`, `reason`, `message`, `retryable`, `next_command` | Retry once if `retryable`, otherwise report; may suggest an override |
| `needs_user` | 3 | Only the user can fix it: `cookies_required`, `login_required`, `rate_limited`, `deepgram_key_missing`, `tool_missing`, `output_dir_missing` | Tell the user exactly what to do; rerun after they do it |
| `unsupported` | 4 | The spoken language is not supported | Report; do not retry |
| `needs_agent_text` | 5 | No LLM backend is reachable; one text request is handed over | Do the task, write the output file, rerun the same command |
| `in_progress` | 6 | The `--max-seconds` budget ran out at a clean boundary: `stage`, progress | Rerun the same command; optionally tell the user the progress |

Exit code 2 stays reserved for command-line usage errors. Typical `failed` reasons: `download_error`, `deepgram_error`, `llm_error`, `llm_timeout`, `quality_hard_failure`, `locked`, `override_not_applicable`.

A `done` result:

```json
{
  "status": "done",
  "video_id": "abc123",
  "output_file": "/Users/me/notes/01.input/01.articles/2026-10-01. Some Talk.md",
  "route": {"source": "subtitles", "track": "en", "track_kind": "manual", "mode": "bilingual"},
  "warnings": ["glossary term 'Kubernetes' appears in 2 spellings"],
  "llm": {"backend": "claude-cli", "requests": 9, "input_tokens": 41200, "output_tokens": 18700},
  "seconds": 412
}
```

A `needs_agent_text` result names a prompt file (the whole request, numbered units included) and an output file:

```json
{
  "status": "needs_agent_text",
  "video_id": "abc123",
  "stage": "text",
  "task": {
    "prompt_file": "/tmp/yt-transcript/abc123/agent_task/prompt.md",
    "output_file": "/tmp/yt-transcript/abc123/agent_task/output.txt"
  },
  "next_command": "run https://www.youtube.com/watch?v=abc123"
}
```

The task is exactly one LLM request: an outline pass or one window round, with the same prompt, numbered units, and end marker an LLM backend would receive. On the rerun, `run` validates the output file the same way it validates any response (section 7) and moves on. A short video needs one handoff per text task; a long video needs several. The agent is therefore simply the slowest LLM backend, with no separate text path to maintain. It is a fallback: normally `auto` reaches the host CLI or the API.

### 6.6 Override flags

| Flag | Kept for reruns | Effect |
|---|---|---|
| `--source subtitles\|deepgram` | yes | Force the source. `deepgram` skips subtitles; `subtitles` keeps the chosen track even when its quality score would trigger the reroute. Redoes `source` and every later stage. |
| `--mode chinese\|bilingual` | yes | Force the output form. The mode follows the source text (Chinese text gives `chinese`, English text gives `bilingual`), so this picks the track: on an English video, `bilingual` uses the English subtitles even when human Chinese subtitles exist, and `chinese` uses the human Chinese subtitles. Redoes `source` and every later stage. |
| `--from <stage>` | no | Mark that stage and every later stage not done, then run. For recovery and debugging. |
| `--stage plan` | no | Stop after `plan`: the free routing check. |
| `--max-seconds <n>` | no | Start no new stage or window round after n seconds, once the call has finished at least one; return `in_progress`. |
| `--restart` | no | Delete the work directory, including saved overrides, and start over. |

These are the agent's escape hatches. A user request like "use Deepgram for this one" or "just give me Chinese" maps to a flag, not to hand-run steps. A combination that cannot be honored (`--mode bilingual` on a Chinese video, `--mode chinese` on an English video without human Chinese subtitles, `--source subtitles` when no trusted track exists) returns `failed` with reason `override_not_applicable`; nothing is guessed.

### 6.7 Long runs and host time limits

A long video can take a quarter of an hour or more, while many hosts kill shell commands after a few minutes. Two properties make that safe on any host:

- `run` can be killed at any moment. Every finished stage and window round is already on disk, the lock of a dead process is taken over, and rerunning loses at most the request that was in flight.
- `--max-seconds <n>` lets the agent avoid the kill: `run` stops at the first stage or round boundary after n seconds and returns `in_progress` with the progress so far. SKILL.md tells the agent to pass about half of its shell tool's time limit when it has one, and to rerun until the status changes.

This needs nothing host-specific such as background jobs.

### 6.8 LLM backend

All text processing goes through one backend interface with three implementations:

- **host CLI**: the host agent's own command line (a built-in table covers Claude Code, Codex, opencode, pi, Gemini, and Cursor; `llm_agent_cli` names any other), run without tools in an empty temporary directory
- **API**: any OpenAI-compatible endpoint from config
- **agent handoff**: the `needs_agent_text` result above

With `llm_backend: auto` (the default) `run` probes the host CLI once, then the API, and hands the request to the agent only if neither works. `llm_backend: api` uses only the API. The chosen backend is recorded in `state.json`. Nothing depends on a provider's special request fields: truncation is detected through the window protocol's markers, which also works for CLI output that has no `finish_reason`.

### 6.9 Quality check and assembly

- `verify` reports the length ratio, structure, glossary, semantic anchors, and bilingual pairing. Only hard failures stop the run (`failed`, stage `verify`, with the reasons), and then no article is written. Warnings are saved in `state.json` and returned with `done` for the agent to mention.
- `assemble` computes everything that used to be built by hand from state: the cleaned title, `yyyy-mm-dd` dates (never the raw `yyyymmdd` from metadata), the filename, frontmatter, and the transcript-source label.
- `cleanup` runs only after the article exists. A failed or interrupted run keeps its whole directory so it can be resumed or inspected.

### 6.10 Public commands

| Command | Purpose |
|---|---|
| `run <url> [flags]` | The whole flow (sections 6.2 to 6.9) |
| `status [<video_id>]` | Without an id: unfinished runs under `/tmp/yt-transcript/` with their stage and last result. With an id: that video's state, finished or not. Replaces `find-unfinished`. |
| `cleanup <video_id> [--force]` | Remove a work directory; refuses an unfinished run without `--force` |
| `subtitle-info <url>` | Debug: language detection and track candidates, without downloading |
| `verify-quality <file> --raw-text <file>` | Debug: rerun the quality check on any file |

Everything else becomes internal functions. Pause, cancel, telemetry queries, and the external runtime API are removed: interrupting the process stops a run, rerunning resumes it, and `state.json` holds the timings and usage.

### 6.11 Module structure

```text
cli          argument parsing, the five commands, JSON results and exit codes
  └─ pipeline    stage list, state.json, lock, resume, overrides, results
       ├─ sources    yt-dlp (metadata, tracks, audio), language selection,
       │             subtitle quality, Deepgram
       ├─ text       VTT parsing, normalization, units, window executor,
       │             outline, glossary, merge, CJK and overlap cleanup
       ├─ quality    verify-quality, semantic anchors
       └─ llm        backends (host CLI, API, agent handoff), retries, truncation
config       loading, defaults, validation (used by every module)
```

Dependencies point downward only. Kernel modules stop copying `yt_transcript_utils`' namespace into their globals (`_bind_utils_globals`); every module imports what it uses, and the 8,000-line `yt_transcript_utils.py` is split along these lines. `prompts/*.md` stay files. `download.sh` keeps working as a subprocess while `run` is introduced, and its logic moves into `sources` when utils is split. During stages 2 to 4 the command is `python3 <skill-root>/yt_transcript_utils.py run <url>`; stage 5 moves the code into one package and leaves a thin entry script at the skill root, so the command that SKILL.md shows changes once.

### 6.12 Testing

- **Unit tests** per module. Tests of deleted code are deleted with it, not kept alive.
- **End-to-end tests** run the whole flow offline: recorded fixtures replace YouTube and Deepgram at the `sources` boundary (the yt-dlp and Deepgram calls), and a fake LLM CLI, plugged in through `llm_agent_cli`, implements the window protocol and the outline JSON. Cases: Chinese human subtitles, English subtitles (bilingual), no subtitles (Deepgram), and an unsupported language; plus resume after an interruption inside `text`, sticky overrides, and every result status.
- **Real-video regression** on the Mac (`scripts/run_regression.py`, which calls `run`) before merging any stage that touches the main path.

### 6.13 What SKILL.md becomes

A few dozen lines: run `run <url>` (with `--max-seconds` if the shell tool has a time limit); act on each result status as in the table in 6.5; map user requests to override flags; for several URLs, call `run` once per URL and summarize in a table; use `status` to find interrupted runs. No state schema, no step list, no Quick Mode.

---

## 7. Core Subsystem Design: Long-Text Transformation

The previous sections describe the whole project. This section zooms in on the `text` stage. Its algorithms (units, outline and glossary, window rounds, validation, fallback) stay as they are; the refactor changes where its state lives and who calls it.

### 7.1 Why long-text transformation is the core difficulty

The hardest problem in `yt-transcript` is not downloading subtitles or calling Deepgram. It is this:

> How do we transform a transcript that is larger than the model's reliable working window into a coherent, article-like result without losing structure, consistency, or recoverability?

Once a video is long enough, the quality of the whole project is decided more by this stage than by acquisition.

### 7.2 Problems the long-text subsystem must solve

- **Context limits**: the full document cannot be processed safely in one call
- **Probabilistic behavior**: LLM output may drift, summarize, omit, or rewrite too aggressively
- **Interruptibility**: long runs may stop because of timeouts, retry exhaustion, or a killed process
- **Cross-round consistency**: headings, terms, numbers, dates, URLs, and style must stay coherent across the document
- **Merge risk**: if boundaries are unclear, final assembly becomes unreliable
- **Control-boundary confusion**: if the model decides where its output starts and stops, the subsystem becomes a black box

### 7.3 Long-text subsystem design goals

- **partitionable**: process a large document in tractable rounds
- **resumable**: continue after interruption without redoing finished paragraphs
- **verifiable**: check output through deterministic rules first
- **mergeable**: reassemble round results deterministically
- **consistency-aware**: decide section structure and terminology once for the whole document
- **uniform**: a short video is the one-round case of the same executor

### 7.4 Long-text subsystem vocabulary and information flow

#### One executor

The sliding-window executor is the only executor. It covers `cleanup_zh` and the bilingual `structure_only` → `translate_only` pair. The old fixed-chunk executor, the `long_text_mode` switch, `summarize`, `quick_cleanup`, and the chapter plan are removed in stage 4 of #23. Fixed chunks survive only as the window's own fallback for one stretch (below).

The window executor was introduced because small fixed chunks needed a stack of patches (continuity context, autotune, replanning) to hide their seams; those patches were removed when window mode became the default.

#### Long-text vocabulary by layer

##### Layer 1: source shaping

- **normalized document**
  - the canonical input built by the `normalize` stage from the timed segments; its cues tell the executor where speech pauses are
- **unit / `[S<n>]`**
  - one numbered source sentence (in the `translate_only` task, one paragraph of the `structure_only` output, split only above ~600 tokens); an over-long unpunctuated sentence is cut at subtitle cue ends (longest pause first), otherwise every ~120 tokens

##### Layer 2: document-level guidance

- **global outline**
  - one upfront pass over the numbered full text (`window_outline`, for `cleanup_zh` and `structure_only`) that returns section titles with their first unit; YouTube chapters are mapped to units, and a section starting at a chapter keeps the chapter title (translated into Chinese for `cleanup_zh` when the title has none)
- **glossary**
  - names and terms with their correct spellings and seen mis-recognitions; the outline pass returns one, and a locally built transcript glossary (no LLM) is used by rounds when there is no outline and by the quality check

##### Layer 3: round execution and control

- **cursor**
  - the first unit not yet written; everything before it is saved as complete paragraphs
- **read window / write target**
  - how much source a round reads from the cursor and how much output it should aim for; both adapt per round (`window_read_tokens`, `window_write_tokens`)
- **round**
  - one model call: read window + tail of written output + nearby outline lines + matching terms; the model copies each unit's `[S<n>]` tag and ends with `<!--END-->`
- **fallback**
  - after repeated invalid rounds, the stretch is processed once as one fixed chunk so the run still advances
- **ownership**
  - the `run` lock on the video's work directory; the executor has no lock of its own

##### Layer 4: verification, merge, and final outputs

- **round validation**
  - tags present, in range, and increasing; output/source length ratio within bounds
- **coverage / efficiency**
  - which units were written and how much source was read twice; recorded in `state.json`
- **merge**
  - deterministic ordered concatenation of the final task's paragraphs
- **quality gate**
  - the `verify` stage; semantic anchors (dates, numbers, versions, URLs) are checked there as warnings

#### Small long-text information flow

```text
normalized_document.json
        |
        v
numbered units [S1..Sn]
        |
        v
Global outline + glossary (once, cached in text/<task>/)
        |
        v
Round: read window from cursor + written tail + outline + terms
        |
        v
LLM backend (host CLI, API, or agent handoff)
        |
        v
Validate tags / END line / length ratio
        |
        +-------------------------+
        | valid                   | invalid (repeated)
        v                         v
Keep complete paragraphs     Process this stretch as one fixed chunk
Advance cursor                    |
        |<------------------------+
        v
More units? -> next round
        |
        v
merge stage -> verify stage -> assemble stage
```

A response cut off before `<!--END-->` only loses its last paragraph: the cursor moves past what was kept, so truncation means less progress, not a broken document.

### 7.5 Long-text subsystem architecture

#### Implementation view (target modules)

- **`text`**: unit numbering, outline pass, round loop, adaptive sizes, validation, fallback, coverage, merge
- **`llm`**: one request interface over the three backends; retries, timeouts, and the `max_tokens` stop line (config values are honored as given; a window round keeps the complete paragraphs of a cut-off response and shrinks the write target)
- **`quality`**: the semantic-anchor and glossary checks used by `verify`
- **`pipeline`**: calls the executor once per planned task, owns the lock, and records progress and usage

#### A. The program owns boundaries, not the model

The model never decides where its output starts or stops in the source. It copies the unit tags it was given, and the program derives paragraph ranges, checks that they are in order, and decides what to keep. This makes merge a plain concatenation.

#### B. Structure decided once

When the run has an outline, headings come only from it. A round may write a heading where the topic actually begins near the outline's start unit; otherwise the program inserts it. Without an outline (the whole source fits in one round, `window_outline: false`, or the outline failed), rounds place their own headings.

#### C. Sizes as soft targets

`max_tokens` is only a loose stop line. The write target shrinks after truncated or slow rounds; the read window follows the write target and the measured output/source ratio, so little source is read twice. The last stretch goes in one round when it fits.

#### D. Deterministic-first verification

Each round is validated by rules (tags in order and in range, length ratio) before it is kept; a missing end line only drops the last paragraph. The merged text then goes through the `verify` stage, where only hard failures block.

#### E. Fallback instead of replanning

When rounds keep failing on one stretch, the executor processes that stretch as one fixed chunk and moves on. There is no plan-level replanning step.

#### F. Persisted state

Each task's window state (cursor, outline, adaptive sizes, saved paragraphs) lives under `text/<task>/` in the video's work directory, so a rerun of `run` resumes at the cursor. Request counts, token usage, and timings go into `state.json`. There is no separate telemetry file and no pause or cancel signal: stopping the process and rerunning is the control, and `--max-seconds` stops cleanly between rounds.

### 7.6 Why this subsystem design fits the whole project

The long-text subsystem is the `text` stage, not a separate product. It consumes the normalized document from `normalize`, follows the tasks chosen by `plan`, uses the same lock and state file as every other stage, and hands its paragraphs to `merge`. Short videos use it too, as the one-round case, so there is one text path to test and improve.

---

## 8. Key Trade-Offs and Boundaries

### 8.1 Why the project is not "just one big prompt"

Because the project must solve more than generation: source discovery, routing, state, recovery, quality gates, and output packaging. Collapsing all of that into one prompt would make the system fragile and opaque.

### 8.2 Known paths go to code; judgment and exceptions go to the model

A model acting as the orchestrator can, in principle, handle situations nobody planned for. In practice this project's unplanned situations happen at the edges: YouTube refusing a request, yt-dlp changing behavior, an external CLI failing, the user wanting something unusual. The middle of the flow is the same every time. When a model improvises in the middle, it hides bugs (a skipped check looks like success), and its fixes do not accumulate (the next run improvises again).

So `run` owns the middle and stops at the edges with an explicit state and a reason. The rate-limit rule in 6.3 is an example: instead of quietly producing a different kind of article, `run` hands the situation to the model, which can explain it to the user and either wait or accept the fallback with a flag. The model stays in charge outside `run`: it decides what to tell the user, whether to retry, which override to use, and how to batch work. The escape hatches (`--source`, `--mode`, `--from`, `--restart`, and the debug commands) keep that power without letting it leak back into the flow.

### 8.3 Why the model no longer orchestrates step by step

Earlier versions put the flow in `SKILL.md` prose. Section 3.5 lists the result: steps skipped by weaker hosts, a hand-written state file that could disagree with reality, no end-to-end test, and layer after layer added to compensate. Each layer made the instructions longer, which made skipping more likely. Moving the flow into code breaks that loop: a missing step becomes a failing test instead of a silent difference in output.

### 8.4 Why the runtime contract layer is removed

The March runtime layer (policy, evaluator, decision records, recovery hints, ledger, artifact graph, and an external create/inspect/advance API) was meant to give weak models and harnesses a structured contract: the current state, the allowed next actions, and a recommended decision. The intent was sound, but the contract never reached the model: it was printed only with `--api-envelope`, which `SKILL.md` never passed, and nothing else read it. With `run` owning the flow, the model no longer chooses next actions, so the contract has no reader. What the model still needs is the result of a whole run, which section 6.5 gives in a few fields. The parts that did work (the ownership lock, resumable state, usage records) move into `pipeline` and `state.json`; the rest is deleted.

### 8.5 Why long-text transformation is a subsystem, not the whole project

Because the project still has to find the source text, decide how to acquire it, normalize it, and assemble the article. Long-text transformation is central, but it is one stage.

### 8.6 Product-scope decisions the system intentionally makes

- `bilingual` means English source text plus Chinese translation
- human Chinese subtitles, when present, are the single source track for any video
- only human subtitles and the spoken-language ASR track are trusted; machine-translated tracks are never used
- videos spoken in languages other than Chinese and English are reported as unsupported
- subtitles are preferred when good enough; Deepgram runs automatically when they are missing or critically poor
- short and long videos share one path
- work files live under `/tmp`, one directory per video, and do not survive a reboot
- the design is local-first, not multi-tenant

### 8.7 What this design does not promise

- a generic workflow platform or multi-agent runtime
- a multi-tenant cloud transcription service
- languages other than Chinese and English
- resuming runs across reboots

---

## 9. Implementation Mapping and Migration (Appendix)

This section relates today's code to the target. Stage numbers refer to tracker #23.

| Today | Target | Stage |
|---|---|---|
| `SKILL.md` (≈370 lines) and `workflows/*.md` drive the flow | `run <url>` drives it; `SKILL.md` shrinks to a few dozen lines; `workflows/` is deleted | 2, 3 |
| Quick Mode for videos under 15 minutes; short videos' text processed by the host model in the conversation | removed; short videos go through `run`, one window round, through the LLM backend | 3 |
| `scripts/run_regression.py` holds the only scripted copy of the flow | its flow becomes `pipeline`; the regression script calls `run` | 2 |
| No end-to-end test | fixtures from 4 real videos and a fake LLM CLI, run offline | 1 |
| `/tmp/<video_id>_state.md`, `machine_state.json`, chunk `manifest.json`, `telemetry.jsonl`, `/tmp/<video_id>_*` files | `/tmp/yt-transcript/<video_id>/` with one `state.json` | 2, 4 |
| `validate-state` stage checks | internal assertions in `pipeline` | 4 |
| `find-unfinished` | `status` | 2 |
| `scripts/preflight.sh` | per-stage checks inside `run` | 4 |
| `scripts/cleanup.sh` | the `cleanup` stage and command | 2, 4 |
| `kernel/task_runtime/*` (policy, decision, evaluator, recovery, ledger, artifacts, api, lifecycle, contracts), `--api-envelope`, 11 outer-orchestration commands, pause and cancel | deleted; the lock and state move into `pipeline` | 4 |
| Fixed-chunk executor (`kernel/long_text/processing.py`, its manifest and contracts in `lifecycle.py` and `contracts.py`, the router `execution.py`), `long_text_mode`, `summarize`, `chapter_plan` / `build-chapter-plan`, `prompts/summarize.md`, `prompts/quick_cleanup.md` | deleted; window executor only | 4 |
| `chunk-document`, `chunk-segments`, `chunk-text` (`kernel/long_text/chunking.py`) | one internal step that builds units from the normalized document | 4 |
| Legacy switches (`chunk_mode: chars`, old config names, `--legacy-flat-output`, old manifest compatibility) | deleted | 4 |
| `yt_transcript_utils.py` (≈8,000 lines) and `_bind_utils_globals` in kernel modules | `cli`, `pipeline`, `sources`, `text`, `llm`, `quality`, `config` with explicit imports | 5 |
| `kernel/subtitle_language.py` | `sources` | 5 |
| `kernel/long_text/window.py`, `outline.py`, `glossary.py`, `merge.py`, `prompting.py`, `kernel/text_cleanup/*` | `text` | 5 |
| `kernel/long_text/llm.py`, `agent_cli.py` | `llm` | 5 |
| `kernel/long_text/semantic.py`, the quality checks in utils | `quality` | 5 |
| `scripts/download.sh` | called by `sources` as a subprocess, then folded into `sources` | 2, 5 |
| `system_design_notes/`, the "target architecture" banner above | deleted; README rewritten for the new entry point | 6 |

Unchanged throughout: subtitle language selection, Deepgram transcription, the window executor's algorithms, the outline and glossary, and the quality checks. They move; they do not change behavior.

Behavior changes the user will notice:

1. Short videos' text goes through the LLM backend (normally the host CLI) instead of being written by the host model in the conversation.
2. A rate-limited preferred subtitle track stops the run with `needs_user` instead of silently falling back to another track (6.3).
3. Rerunning a finished video returns the existing article instead of starting over; `--restart` redoes it.
4. The subtitle quality reroute happens inside `source`, before normalization; the outcome is the same.
5. New: `--max-seconds` and the `in_progress` result for hosts with shell time limits.

---

## 10. One-Sentence Summary

`yt-transcript` is a local transcript production system in which one deterministic command, `run`, owns the known path from YouTube URL to Markdown article, the LLM only transforms text inside it, and the host agent handles the exceptions and the user.

---

## 中文版

> [!IMPORTANT]
> **本文描述的是目标架构**，按 tracker [#23](https://github.com/williamwang-ty/yt-transcript/issues/23) 逐步实现。在后续阶段完成之前，仓库仍按旧流程运行：agent 读 `SKILL.md` 和 `workflows/*.md`，一条命令一条命令地执行。第 9 节列出现有代码和目标的对应关系、每一部分在哪个阶段迁移，以及用户能感觉到的行为变化。

## 1. 文档定位

本文说明 **整个 `yt-transcript` 项目的系统设计**：它构建什么，代码和模型怎样分工，以及为什么这样分。

`README.md` 是给使用者的快速上手文档，`SKILL.md` 是宿主 agent 读的简短约定。本文两者都不是，它解释结构、边界和取舍。它也不是编码规范：下面出现的字段名、文件名和原因代码用来说明设计的形状，实现过程中还可能调整。

本文分两个层次：

- **项目层**（第 2 到 6 节）：一条命令怎样把 YouTube 链接变成一篇 Markdown 文章，以及外面的 agent 负责什么。
- **子系统层**（第 7 节）：这条命令里最难的阶段，也就是长文本变换，是怎样工作的。

`SYSTEM_DESIGN.md` 是本仓库唯一权威的设计文档。

---

## 2. 这个项目到底在构建什么

`yt-transcript` 是一个以 agent skill 形式交付的 **本地转录生产系统**。给它一个 YouTube 链接，它在用户的笔记目录里生成一篇排好版的 Markdown 文章。

它由两部分组成，中间有一条硬边界：

- **`run <url>`**：一条确定性的命令，负责全流程：元数据、选择来源、下载字幕或用 Deepgram 转写、规范化、规划、LLM 文字处理、合并、质量检查、组装和清理。它把一切记在每个视频一个的状态文件里，任何时候重跑都能接着做。
- **宿主 agent**（Claude Code、Codex 等）：启动 `run`；处理 `run` 交回来的少数几种状态（需要用户操作、语言不支持、某一步失败、没有可用的 LLM 后端）；把用户的临时要求换成覆盖参数；多个链接时逐个调用；最后向用户汇报。

LLM 只用来处理文字，从不决定下一步做什么。

---

## 3. 项目级要解决的问题

### 3.1 来源可用性并不确定

一个 YouTube 视频可能有人工字幕，可能只有 YouTube 语音识别（ASR）字幕，可能有看起来像真字幕的机器翻译轨，可能字幕质量很差，也可能什么能用的都没有。YouTube 还可能拒绝请求（机器人验证、要求登录、限流）。系统不能假设某一条获取路径总是可行。

### 3.2 路由决策必须可靠

系统每次都必须用同样的方式决定：

- 视频说的是什么语言，是否支持
- 信任哪一条字幕轨，还是转写音频
- 字幕质量够不够，是否要改用 Deepgram
- 输出只有中文，还是英文加中文

这些决定应该由代码读取结构化数据得出，而不是靠模型理解一段说明文字。

### 3.3 文本优化对模型能力很敏感

把转录文字变成可读的文章（恢复标点和分段、修正识别错误、加标题、翻译），是 LLM 最有价值的地方，也是弱模型和过载 prompt 最容易出错的地方。

### 3.4 长视频会击穿朴素工作流

文字长到一定程度后，一次调用放不下，中途中断变得常见，各段之间的标题和术语会漂移，段与段的接缝会把句子切断。长文本变换是最难的子系统（第 7 节）。

### 3.5 流程本身不能依赖模型遵守说明

旧设计里，agent 要读约 370 行的 `SKILL.md` 和三份 workflow 文档，执行二十来条命令，还要手写一个 `state.md` 文件，以此来执行流程。代价是可以预见的：

- 弱一些的宿主会跳步或打乱顺序，文档里的 Quick Mode 干脆跳过了规划和质量检查
- 流程只存在于文字说明里，没法做端到端测试
- 每发现一种失败，就加一层补偿（状态校验、runtime 契约层、policy 和 decision 记录），而这些层都没法安全删除，因为没有任何东西能证明它们没用

所以流程必须是可以测试的代码，模型只保留真正需要判断的工作。

### 3.6 项目必须可运营

用户和 agent 需要知道选了哪条路径、为什么，已经做完什么，花了多少，中断后怎样继续，而不用去读代码或日志。

---

## 4. 项目级设计目标

> 事先已知的路径都由代码执行；需要判断的事、例外情况和与用户的沟通交给模型。

具体来说：

- **唯一入口**：`run <url>` 从链接一直做到最终 Markdown；agent 不串联内部命令。
- **确定性路由**：语言、来源、改道和模式的决定都由结构化数据计算得出，并连同原因一起记录。
- **重跑即续跑**：每个阶段都是幂等的并且记账；重跑同一条命令就从第一个未完成的阶段继续。
- **明确交回**：`run` 只在干净的断点停下，返回一小组固定的状态，每个状态都带原因和下一步。
- **容忍弱模型**：prompt 保持窄，边界由程序掌握，所以弱模型只会让文章差一些，不会让流程坏掉。
- **不绑定模型提供方**：文字处理可以用任何宿主 agent 的命令行或任何 OpenAI 兼容的 API，不使用某家提供方特有的请求字段。
- **不绑定宿主**：不假设某个 agent 特有的工具；长任务也适用于 shell 工具有时间上限的宿主。
- **可端到端测试**：整个流程可以离线跑在录好的 fixtures 和假 LLM 上。
- **接口小**：对外命令少，配置开关少；被替换的代码直接删除，不留在开关后面。

---

## 5. 总体系统设计

```text
用户 ──> 宿主 agent（读 SKILL.md，几十行）
            │  run <url> [覆盖参数]
            v
        ┌──────────────────────── run ─────────────────────────┐
        │ metadata -> source -> normalize -> plan -> text       │
        │   -> merge -> verify -> assemble -> cleanup           │
        │                                                       │
        │ /tmp/yt-transcript/<video_id>/state.json（+ 运行锁）  │
        │ text 阶段 -> LLM 后端：宿主 CLI | API | agent         │
        └───────────────────────────┬───────────────────────────┘
                                    │ 一个 JSON 结果 + 退出码
                                    v
   done | in_progress | needs_user | unsupported | failed | needs_agent_text
                                    │
                                    v
   宿主 agent：汇报、重跑、询问用户，或完成一段文字任务后重跑
```

核心思路：

> 流程的中间部分（解析、路由、给文字编号、按窗口处理、合并、检查、组装）是确定性的，所以放在代码里，可以测试，修复也能积累。意外发生在边缘（YouTube、yt-dlp、外部命令行工具、用户的意图），所以 `run` 在那里停下，给出明确的状态，交给模型决定。

---

## 6. 详细设计

### 6.1 入口与分工

| 事项 | 由 `run`（代码）负责 | 由宿主 agent（模型）负责 |
|---|---|---|
| 流程顺序、跳过、续跑 | 是 | 否 |
| 语言、来源、改道、模式的决定 | 是，并记录原因 | 只通过用户要求的覆盖参数 |
| 文字处理 | 通过 LLM 后端 | 只在收到 `needs_agent_text` 任务时 |
| 文件名、日期、frontmatter、标题清理 | 是 | 否 |
| cookies、登录、限流、缺少密钥或工具 | 检测出来并返回 `needs_user` | 告诉用户该怎么做 |
| 失败 | 返回 `failed`，附原因和重跑命令 | 决定重试、换覆盖参数，还是汇报 |
| 多个链接 | 每次调用处理一个视频 | 按顺序每个链接调用一次 `run`，结果列成表 |
| 汇报 | 返回输出路径、路由、警告和用量 | 告诉用户 |

agent 从不编辑状态文件，也从不手动执行内部阶段。需要不同的结果时，就带上覆盖参数重跑 `run`。

### 6.2 阶段

`run` 按固定的阶段列表执行。每个阶段读取前面阶段记下的内容，把产物原子地写进该视频的工作目录，然后才在 `state.json` 里把自己标为 `done`。重跑时，已经 `done` 且记录的产物仍然存在的阶段会被跳过。

| 阶段 | 做什么 | 主要产物 | 可能的停止状态 |
|---|---|---|---|
| `metadata` | 用 yt-dlp 取一次视频信息（标题、频道、日期、时长、章节、字幕轨列表、音轨语言）；后面的阶段读这个文件，不再调用 yt-dlp 取信息 | `metadata.json` | `needs_user`（自动用浏览器 cookies 重试后仍遇到机器人验证或要求登录、限流、没有 yt-dlp）、`failed` |
| `source` | 判断口语语言并选出可信的字幕轨（6.3）；下载并解析成纯文本和带时间的片段；给字幕质量打分，评分规则要求时改用 Deepgram；或者直接用 Deepgram 转写 | `source/`、`raw_text.txt`、`segments.json`、路由决定 | `unsupported`、`needs_user`（首选字幕轨被限流、缺少 Deepgram 密钥）、`failed` |
| `normalize` | 从片段生成规范化文档（句子及其对应的字幕时间） | `normalized_document.json` | `failed` |
| `plan` | 按模式选择文字任务（中文：`cleanup_zh`；双语：先 `structure_only` 再 `translate_only`），并估算规模（输入 token、预计轮数） | `plan.json` | `failed` |
| `text` | 用窗口执行器（第 7 节）逐个执行规划好的任务；每个任务单独记账 | `text/<task>/` | `needs_agent_text`、`in_progress`、`failed` |
| `merge` | 按顺序拼接最后一个任务的段落 | `optimized.md` | `failed` |
| `verify` | 对照原始文字做质量检查 | `quality.json` | `failed`（只有硬失败才停；警告只记录） |
| `assemble` | 生成最终文件：frontmatter、清理后的标题、`yyyy-mm-dd. <标题>.md` 文件名、双语排版 | `output_dir` 里的文章 | `failed` |
| `cleanup` | 文章写出后删除中间文件，保留 `state.json` | — | — |

说明：

- **阶段幂等。** 重跑一个未完成的阶段，会把这个阶段从头做一遍；但昂贵的子步骤在阶段内部保留结果（下载好的音频、Deepgram 分段结果、大纲、已完成的窗口轮次），所以中断的阶段不会重复花钱。
- **没有单独的 preflight。** 每个阶段在真正需要之前才检查它需要的东西：`metadata` 之前检查 yt-dlp，`source` 选了 Deepgram 才检查 Deepgram 密钥，到 `text` 才检查 LLM 后端。便宜的路径保持便宜。
- **第一次调用 LLM 之前来源已经定下来。** 字幕质量改道从规划阶段移到 `source`，所以 `normalize` 及之后的阶段处理的都是已经认可的文字。
- **长短视频一条路径。** 短视频和长视频走同样的阶段。短视频的文字一轮窗口就能处理完，全文一轮能写完时也会跳过大纲调用。规划结果为 `long` 时，YouTube 章节作为提示交给大纲；短文本没有大纲，也就用不上章节。
- **免费的路由检查。** `--stage plan` 执行 `metadata` 到 `plan`，不花钱。如果路由需要 Deepgram，就记下这个决定，在 `source` 做完决定后停下，不做转写。

### 6.3 来源选择

这些规则现在已经存在，只是挪进 `source` 阶段：

1. 判断口语语言：先看 YouTube 标为原始的音轨，再看元数据的 `language`，再看 `xx-orig` ASR 轨，最后看非翻译的 ASR 轨。
2. 只信任人工字幕和口语语言的 ASR 轨，丢弃 YouTube 的机器翻译轨。
3. 中文视频：人工中文字幕，没有就用中文 ASR 轨；模式 `chinese`。
4. 英文视频：有人工中文字幕就用它（模式 `chinese`），否则用英文字幕（模式 `bilingual`：英文原文加中文翻译）。
5. 其他口语语言：`unsupported`，除非有人工中文字幕。
6. 没有可信的字幕轨，或者字幕质量分触发改道规则（目前是 `chinese` 路线上质量为 critical）：按检测出的语言用 Deepgram 转写。Deepgram 自动执行，没有确认步骤。Deepgram 识别出的语言不受支持时，同样返回 `unsupported`。

有一条规则改变了。首选字幕轨在自动重试后仍被 YouTube 限流时，旧流程会悄悄换用下一条字幕轨，比如用英文字幕代替人工中文字幕，结果中文文章变成了双语文章，而且每次运行结果可能不一样。现在 `source` 返回 `needs_user`，原因是 `rate_limited`，并说明备用的字幕轨是哪条。agent 可以稍后重跑，也可以用覆盖参数明确接受备用字幕轨（上面的例子里是 `--mode bilingual`）。

`yt-dlp` 仍然是获取来源的依赖。直接调用 YouTube InnerTube 获取字幕评估过：对只需要字幕的场景更轻，但它是非官方、接口敏感的入口，不能取代 Deepgram 兜底，还会把维护提取逻辑的负担转到本项目。它可以作为将来的快速路径，但不是默认方案。

### 6.4 工作目录与状态

每个视频一个工作目录：

```text
/tmp/yt-transcript/<video_id>/
  state.json                 唯一的状态文件
  .runtime_owner.json        锁，运行期间持有
  metadata.json  chapters.json
  source/                    下载的字幕或音频、字幕质量检查、Deepgram JSON 及分段缓存
  raw_text.txt  segments.json  normalized_document.json
  plan.json
  text/                      所有任务的窗口执行器状态（游标、大纲、大小、段落）
  agent_task/                只在有未完成的 needs_agent_text 任务时存在
  optimized.md  quality.json
```

`YT_TRANSCRIPT_WORK_ROOT` 可以代替 `/tmp/yt-transcript`，`YT_TRANSCRIPT_OUTPUT_DIR` 可以代替配置里的 `output_dir`；回归脚本两个都用，所以不会碰到真实的运行目录和笔记库。

每个阶段、每轮窗口之后都原子地写一次 `state.json`。它的形状：

```json
{
  "schema": 1,
  "video_id": "abc123",
  "url": "https://www.youtube.com/watch?v=abc123",
  "status": "running",
  "overrides": {"source": null, "mode": null},
  "route": {
    "source_language": "en",
    "language_signal": "original_audio_track",
    "source": "subtitles",
    "track": "en",
    "track_kind": "manual",
    "mode": "bilingual",
    "subtitle_quality": {"score": 0.82, "band": "good"},
    "blocked_tracks": [],
    "rerouted_from": null,
    "reason": "English video with human English subtitles"
  },
  "stages": {
    "metadata": {"status": "done", "seconds": 2.1, "outputs": ["metadata.json"]},
    "text": {"status": "running", "tasks": {
      "structure_only": {"status": "done", "rounds": 4},
      "translate_only": {"status": "running", "rounds": 2}
    }}
  },
  "llm": {"backend": "claude-cli", "requests": 9, "input_tokens": 41200, "output_tokens": 18700},
  "warnings": [],
  "last_result": {"status": "failed", "stage": "text", "reason": "llm_timeout"},
  "output_file": null
}
```

规则：

- **续跑**就是重跑同一条命令：`run` 读 `state.json`，从第一个不是 `done` 的阶段开始。在 `text` 阶段内部，窗口执行器从游标处继续。
- **完成的就保持完成。** `cleanup` 保留 `state.json`，所以对已完成的视频重跑 `run`，会立即返回 `done` 和已有的文章，前提是这篇文章还在（被删掉了就从头再做）。因此整批重跑时只会处理没完成的视频。要重新生成文章就用 `--restart`。
- **覆盖参数会保留。** `--source` 和 `--mode` 存进 `state.json`，在这个视频之后的重跑里一直有效，直到 `--restart`，所以续跑不会悄悄改变路由。
- **每次运行都会读配置**，但配置改了，已完成的阶段也不会重做；要重做就用 `--from <阶段>`。`cleanup` 之后中间文件已经删掉，所以对已完成的运行用 `--from` 会重做所有阶段；它是给未完成的运行用的（比如质量检查失败后 `--from text`）。
- **运行锁**防止两个进程同时写一个工作目录。对一个被锁住的视频再执行 `run`，返回 `failed`，原因是 `locked`；已退出进程留下的锁会被接管。不同视频之间不共享文件，可以并行运行。
- **取代的东西**：手写的 `state.md`、`machine_state.json`、分块的 `manifest.json`、`telemetry.jsonl`，以及散落的 `/tmp/<video_id>_*` 文件。下载文件和规范化文档都移到该视频自己的目录下，不同运行之间不会在共享路径上撞车。
- **安全**：`video_id` 必须匹配 `[A-Za-z0-9_-]+` 才能用作路径。`state.json` 不存 API 密钥，只记后端名字，不记凭据。
- **寿命**：目录在 `/tmp` 下，重启后未完成的任务会丢失。这是接受的限制。

### 6.5 交回给 agent 的结果

每次 `run` 调用都在 stdout 打印且只打印一个 JSON 对象，并以固定的退出码结束。进度信息（阶段、第 n 轮共 m 轮）输出到 stderr。

| `status` | 退出码 | 含义 | agent 怎么做 |
|---|---|---|---|
| `done` | 0 | 文章已写出，或 `--stage plan` 已完成 | 汇报 `output_file`、路由、警告和 LLM 用量 |
| `failed` | 1 | 某个阶段失败：`stage`、`reason`、`message`、`retryable`、`next_command` | `retryable` 时重试一次，否则汇报；可以建议某个覆盖参数 |
| `needs_user` | 3 | 只有用户能解决：`cookies_required`、`login_required`、`rate_limited`、`deepgram_key_missing`、`tool_missing`、`output_dir_missing` | 准确告诉用户要做什么；用户做完后重跑 |
| `unsupported` | 4 | 口语语言不受支持 | 汇报；不重试 |
| `needs_agent_text` | 5 | 没有可用的 LLM 后端，交出一次文字请求 | 完成任务、写出输出文件、重跑同一条命令 |
| `in_progress` | 6 | `--max-seconds` 时间用完，在干净的边界停下：`stage`、进度 | 重跑同一条命令；可以顺便告诉用户进度 |

退出码 2 留给命令行用法错误。`failed` 的常见原因：`download_error`、`deepgram_error`、`llm_error`、`llm_timeout`、`quality_hard_failure`、`locked`、`override_not_applicable`。

一个 `done` 结果：

```json
{
  "status": "done",
  "video_id": "abc123",
  "output_file": "/Users/me/notes/01.input/01.articles/2026-10-01. Some Talk.md",
  "route": {"source": "subtitles", "track": "en", "track_kind": "manual", "mode": "bilingual"},
  "warnings": ["glossary term 'Kubernetes' appears in 2 spellings"],
  "llm": {"backend": "claude-cli", "requests": 9, "input_tokens": 41200, "output_tokens": 18700},
  "seconds": 412
}
```

`needs_agent_text` 结果给出一个 prompt 文件（完整的请求，包括编号单元）和一个输出文件：

```json
{
  "status": "needs_agent_text",
  "video_id": "abc123",
  "stage": "text",
  "task": {
    "prompt_file": "/tmp/yt-transcript/abc123/agent_task/prompt.md",
    "output_file": "/tmp/yt-transcript/abc123/agent_task/output.txt"
  },
  "next_command": "run https://www.youtube.com/watch?v=abc123"
}
```

这个任务恰好是一次 LLM 请求：一次大纲调用或一轮窗口，prompt、编号单元和结束标记都和 LLM 后端收到的一样。重跑时，`run` 用校验任何响应的同一套规则（第 7 节）校验输出文件，然后继续。短视频每个文字任务交一次；长视频要交好几次。所以 agent 就是最慢的一种 LLM 后端，不需要单独维护一条文字处理路径。它只是兜底：正常情况下 `auto` 会用上宿主 CLI 或 API。

### 6.6 覆盖参数

| 参数 | 重跑时保留 | 作用 |
|---|---|---|
| `--source subtitles\|deepgram` | 是 | 强制来源。`deepgram` 跳过字幕；`subtitles` 即使质量分会触发改道也坚持用选中的字幕轨。从 `source` 起重做后面所有阶段。 |
| `--mode chinese\|bilingual` | 是 | 强制输出形式。模式跟着来源文字走（中文文字对应 `chinese`，英文文字对应 `bilingual`），所以这个参数实际上决定用哪条字幕轨：英文视频上，`bilingual` 即使有人工中文字幕也用英文字幕，`chinese` 用人工中文字幕。从 `source` 起重做后面所有阶段。 |
| `--from <阶段>` | 否 | 把该阶段及之后的阶段标为未完成，然后运行。用于恢复和调试。 |
| `--stage plan` | 否 | 做完 `plan` 就停：免费的路由检查。 |
| `--max-seconds <n>` | 否 | n 秒后、且本次调用至少完成了一个阶段或一轮窗口时，不再开始新的阶段或新一轮窗口，返回 `in_progress`。 |
| `--restart` | 否 | 删除工作目录（包括保存的覆盖参数），从头开始。 |

这些是 agent 的逃生口。用户说"这个用 Deepgram"或"只要中文"，对应的是一个参数，而不是手动执行几步。无法满足的组合（中文视频上用 `--mode bilingual`，没有人工中文字幕的英文视频上用 `--mode chinese`，没有可信字幕轨时用 `--source subtitles`）返回 `failed`，原因是 `override_not_applicable`；不做任何猜测。

### 6.7 长任务和宿主的时间上限

长视频可能要跑一刻钟甚至更久，而很多宿主几分钟后就会结束 shell 命令。下面两点让任何宿主都能安全运行：

- `run` 随时可以被杀掉。每个完成的阶段和每轮窗口都已经落盘，已退出进程的锁会被接管，重跑最多损失正在进行的那一次请求。
- `--max-seconds <n>` 让 agent 不必被动挨杀：`run` 在 n 秒后的第一个阶段或轮次边界停下，返回 `in_progress` 和目前的进度。SKILL.md 告诉 agent：如果自己的 shell 工具有时间上限，就传大约一半的上限值，然后一直重跑到状态改变。

这不需要后台任务之类宿主特有的能力。

### 6.8 LLM 后端

所有文字处理都走同一个后端接口，有三种实现：

- **宿主 CLI**：宿主 agent 自己的命令行（内置表覆盖 Claude Code、Codex、opencode、pi、Gemini 和 Cursor；其他的用 `llm_agent_cli` 指定），在空的临时目录里、不带工具运行
- **API**：配置里的任意 OpenAI 兼容接口
- **交给 agent**：上面的 `needs_agent_text` 结果

`llm_backend: auto`（默认）时，`run` 先探测一次宿主 CLI，再试 API，两者都不行才把请求交给 agent。`llm_backend: api` 只用 API。选中的后端记在 `state.json` 里。不依赖任何提供方的特殊请求字段：截断通过窗口协议的标记检测，没有 `finish_reason` 的命令行输出也适用。

### 6.9 质量检查与组装

- `verify` 报告长度比、结构、术语、semantic anchors 和双语配对。只有硬失败会让运行停下（`failed`，阶段 `verify`，附原因），这时不写文章。警告存进 `state.json`，随 `done` 一起返回，由 agent 提及。
- `assemble` 计算以前要从状态里手工拼出来的所有东西：清理后的标题、`yyyy-mm-dd` 格式的日期（从不使用元数据里原始的 `yyyymmdd`）、文件名、frontmatter 和转录来源标签。
- `cleanup` 只在文章存在之后运行。失败或中断的运行保留整个目录，便于续跑或检查。

### 6.10 对外命令

| 命令 | 用途 |
|---|---|
| `run <url> [参数]` | 整个流程（6.2 到 6.9 节） |
| `status [<video_id>]` | 不带 id：列出 `/tmp/yt-transcript/` 下未完成的运行、所在阶段和上次结果。带 id：显示该视频的状态，不论是否完成。取代 `find-unfinished`。 |
| `cleanup <video_id> [--force]` | 删除一个工作目录；未完成的运行不带 `--force` 时拒绝 |
| `subtitle-info <url>` | 调试用：语言判断和候选字幕轨，不下载 |
| `verify-quality <file> --raw-text <file>` | 调试用：对任意文件重跑质量检查 |

其余都变成内部函数。暂停、取消、telemetry 查询和对外 runtime API 都删除：中断进程就停下，重跑就续跑，耗时和用量记在 `state.json` 里。

### 6.11 模块结构

```text
cli          参数解析、五个命令、JSON 结果和退出码
  └─ pipeline    阶段列表、state.json、运行锁、续跑、覆盖参数、结果
       ├─ sources    yt-dlp（元数据、字幕轨、音频）、语言选择、
       │             字幕质量、Deepgram
       ├─ text       VTT 解析、规范化、单元、窗口执行器、
       │             大纲、术语表、合并、CJK 和重叠清理
       ├─ quality    verify-quality、semantic anchors
       └─ llm        后端（宿主 CLI、API、交给 agent）、重试、截断
config       加载、默认值、校验（所有模块都用）
```

依赖只向下。kernel 模块不再把 `yt_transcript_utils` 的命名空间复制进自己的全局变量（`_bind_utils_globals`）；每个模块显式导入自己用到的东西，8000 行的 `yt_transcript_utils.py` 按上面的结构拆开。`prompts/*.md` 仍然是文件。引入 `run` 期间 `download.sh` 继续作为子进程使用，拆分 utils 时它的逻辑并入 `sources`。第 2 到 4 阶段期间命令是 `python3 <skill-root>/yt_transcript_utils.py run <url>`；第 5 阶段把代码移进一个包，在 skill 根目录留一个很薄的入口脚本，所以 SKILL.md 里的命令只改一次。

### 6.12 测试

- **单元测试**按模块组织。被删除代码的测试随代码一起删除，不保留。
- **端到端测试**离线跑整个流程：在 `sources` 的边界（yt-dlp 和 Deepgram 调用处）用录好的 fixtures 代替 YouTube 和 Deepgram，通过 `llm_agent_cli` 接入一个假 LLM 命令行，它实现窗口协议和大纲 JSON。用例：中文人工字幕、英文字幕（双语）、无字幕（Deepgram）、不支持的语言；另外覆盖在 `text` 阶段中断后续跑、覆盖参数的保留，以及每一种结果状态。
- **真实视频回归**：任何动到主路径的阶段合并前，在 Mac 上跑 `scripts/run_regression.py`（它调用 `run`）。

### 6.13 SKILL.md 会变成什么样

几十行：执行 `run <url>`（shell 工具有时间上限时带上 `--max-seconds`）；按 6.5 的表处理每种结果状态；把用户的要求换成覆盖参数；多个链接时每个链接调用一次 `run`，最后用表格汇总；用 `status` 找中断的运行。没有状态文件格式，没有步骤清单，没有 Quick Mode。

---

## 7. 核心子系统设计：长文本变换

前面几节讲的是整个项目；这一节放大看 `text` 阶段。它的算法（单元、大纲和术语表、窗口轮次、校验、兜底）保持不变；重构改变的是它的状态放在哪里、由谁调用。

### 7.1 为什么长文本变换是核心难点

`yt-transcript` 里最难的问题，不是下载字幕，也不是调用 Deepgram，而是：

> 当转录文字超过模型可靠工作窗口时，怎样把它变成一篇连贯的、文章化的结果，同时不丢结构、不失一致性、可以恢复？

视频足够长以后，整个项目的质量更多取决于这个阶段，而不是来源获取。

### 7.2 长文本子系统要解决的问题

- **上下文限制**：全文不能安全地一次处理
- **概率性行为**：LLM 可能漂移、摘要化、遗漏或过度改写
- **可中断性**：长任务可能因为超时、重试耗尽或进程被杀而停下
- **跨轮一致性**：标题、术语、数字、日期、URL 和风格要在整篇文档里保持一致
- **合并风险**：边界不清楚，最终组装就不可靠
- **控制边界混乱**：如果由模型决定自己的输出从哪里开始、到哪里结束，子系统就成了黑盒

### 7.3 长文本子系统的设计目标

- **可切分**：把大文档分成若干轮处理
- **可恢复**：中断后继续，已完成的段落不重做
- **可验证**：优先通过确定性规则检查结果
- **可合并**：每轮结果能确定性地重组为整体
- **有一致性感知**：章节结构和术语对整篇文档只决定一次
- **统一**：短视频就是同一个执行器只跑一轮的情况

### 7.4 长文本子系统的词汇表与信息流

#### 一个执行器

滑动窗口执行器是唯一的执行器，覆盖 `cleanup_zh` 以及双语的 `structure_only` → `translate_only` 两步。旧的固定分块执行器、`long_text_mode` 开关、`summarize`、`quick_cleanup` 和 chapter plan 在 #23 的第 4 阶段删除。固定分块只作为窗口对某一段的兜底保留下来（见下文）。

引入窗口执行器，是因为小的固定分块需要一堆补丁（continuity 上下文、autotune、replan）来掩盖接缝；窗口模式转为默认后，这些补丁已经删除。

#### 分层理解长文本子系统词汇表

##### 第 1 层：源文本整形

- **规范化文档（normalized document）**
  - `normalize` 阶段从带时间的片段生成的标准输入；其中的字幕时间告诉执行器哪里有停顿
- **单元 / `[S<n>]`**
  - 一个编号的原文句子（`translate_only` 任务里是 `structure_only` 输出的一个段落，超过约 600 token 才拆开）；没有标点的长句在字幕条结束处切开（优先最长停顿），对不上字幕时每约 120 token 切一次

##### 第 2 层：文档级指导

- **全局大纲**
  - 对带编号全文的一次前置调用（`window_outline`，用于 `cleanup_zh` 和 `structure_only`），返回章节标题和起始单元；YouTube 章节会映射到单元，从章节处开始的小节沿用章节标题（`cleanup_zh` 下标题没有中文时用译文）
- **术语表（glossary）**
  - 人名、术语的正确写法和见到的识别错误；大纲调用会返回一份，另有一份本地生成的转录术语表（不用 LLM），在没有大纲时供各轮使用，并用于质量检查

##### 第 3 层：轮次执行与控制

- **游标（cursor）**
  - 第一个还没写出的单元；它之前的内容都已存为完整段落
- **读窗口 / 写目标**
  - 一轮从游标读多少原文、输出大约写多少；两者每轮自适应（`window_read_tokens`、`window_write_tokens`）
- **轮次（round）**
  - 一次模型调用：读窗口 + 已写内容的结尾 + 附近的大纲行 + 匹配的术语；模型照抄每个单元的 `[S<n>]` 编号，最后输出 `<!--END-->`
- **兜底（fallback）**
  - 连续多轮无效后，把这一段按一个固定分块处理一次，保证任务继续推进
- **占有（ownership）**
  - 就是 `run` 对视频工作目录的运行锁；执行器自己不再有锁

##### 第 4 层：校验、合并与最终输出

- **轮次校验**
  - 编号齐全、在范围内且递增；输出/原文长度比在合理范围
- **覆盖率 / 效率**
  - 哪些单元已写出，多少原文被重复阅读；记在 `state.json` 里
- **合并（merge）**
  - 把最后一个任务的段落按顺序确定性拼接
- **质量门禁**
  - 即 `verify` 阶段；semantic anchors（日期、数字、版本号、URL）在那里作为警告检查

#### 精简版长文本信息流

```text
normalized_document.json
        |
        v
编号单元 [S1..Sn]
        |
        v
全局大纲 + 术语表（只做一次，缓存在 text/<task>/）
        |
        v
一轮：从游标读窗口 + 已写结尾 + 大纲 + 术语
        |
        v
LLM 后端（宿主 CLI、API 或交给 agent）
        |
        v
校验编号 / 结束行 / 长度比
        |
        +-------------------------+
        | 有效                    | 连续无效
        v                         v
保留完整段落                把这一段按一个固定分块处理
推进游标                          |
        |<------------------------+
        v
还有单元？-> 下一轮
        |
        v
merge 阶段 -> verify 阶段 -> assemble 阶段
```

响应在 `<!--END-->` 之前被截断时，只丢掉最后一段：游标移到已保留内容之后，截断只是少推进一点，不会让文档出错。

### 7.5 长文本子系统的架构设计

#### 实现视图（目标模块）

- **`text`**：单元编号、大纲调用、轮次循环、自适应大小、校验、兜底、覆盖率、合并
- **`llm`**：三种后端之上的统一请求接口；重试、超时和 `max_tokens` 停止线（配置值按原样使用；窗口轮次遇到被截断的响应时保留完整段落并调小写目标）
- **`quality`**：`verify` 使用的 semantic anchor 和术语检查
- **`pipeline`**：每个规划好的任务调用一次执行器，持有运行锁，记录进度和用量

#### A. 边界由程序决定，而不是模型

模型从不决定自己的输出从原文哪里开始、到哪里结束。它只照抄给它的单元编号，程序据此推算每段覆盖的范围、检查顺序，并决定保留什么。因此合并只是简单拼接。

#### B. 结构只决定一次

有大纲时，标题只来自大纲。话题在大纲起始单元附近真正开始的地方，模型可以写出标题；否则由程序插入。没有大纲时（全文一轮就能写完、`window_outline: false` 或大纲失败），各轮自己放标题。

#### C. 大小是软目标

`max_tokens` 只是宽松的停止线。被截断或太慢的轮次之后写目标会调小；读窗口跟着写目标和实测的输出/原文比例走，尽量少重复读原文。剩下的原文一轮能写完时就一次发完。

#### D. 优先确定性校验

每轮在保留之前先用规则校验（编号顺序和范围、长度比）；缺少结束行只会丢掉最后一段。合并后的文字再经过 `verify` 阶段，只有硬失败会阻断。

#### E. 用兜底代替 replan

某一段连续多轮失败时，执行器把这一段按一个固定分块处理，然后继续。没有计划级的 replan 步骤。

#### F. 持久化状态

每个任务的窗口状态（游标、大纲、自适应大小、已保存段落）放在视频工作目录的 `text/<task>/` 下，重跑 `run` 会从游标续跑。请求数、token 用量和耗时记进 `state.json`。没有单独的 telemetry 文件，也没有暂停或取消信号：停掉进程再重跑就是控制手段，`--max-seconds` 则在两轮之间干净地停下。

### 7.6 为什么这个子系统设计适合整个项目

长文本子系统就是 `text` 阶段，不是另一个独立产品。它消费 `normalize` 生成的规范化文档，按 `plan` 选定的任务执行，和其他阶段共用同一个运行锁和状态文件，把段落交给 `merge`。短视频也用它，是只跑一轮的情况，所以只有一条文字处理路径需要测试和改进。

---

## 8. 关键取舍与系统边界

### 8.1 为什么项目不是"一个大 prompt"

因为项目要解决的不只是生成：还有来源探测、路由、状态、恢复、质量门禁和输出打包。把这些都塞进一个 prompt，系统会既脆弱又不透明。

### 8.2 已知路径交给代码，判断和例外交给模型

模型当总指挥，理论上能处理没人事先规划过的情况。但在这个项目里，没规划到的情况实际上都发生在边缘：YouTube 拒绝请求、yt-dlp 行为变化、外部命令行工具出错、用户提出不寻常的要求。流程的中间部分每次都一样。模型在中间部分临场发挥，会把 bug 藏起来（跳过的检查看起来像成功了），而且它的修正不会积累（下次运行又重新发挥一遍）。

所以 `run` 负责中间部分，在边缘停下，给出明确的状态和原因。6.3 里限流的规则就是一个例子：`run` 不再悄悄产出另一种文章，而是把情况交给模型，模型可以向用户解释，然后等待，或者用参数接受备用字幕轨。在 `run` 之外仍然由模型做主：告诉用户什么、是否重试、用哪个覆盖参数、怎样批量处理。逃生口（`--source`、`--mode`、`--from`、`--restart` 和调试命令）保留了这种能力，又不让它渗回流程内部。

### 8.3 为什么不再让模型逐步编排

以前的版本把流程写在 `SKILL.md` 的文字里。3.5 节列出了结果：弱宿主跳步，手写的状态文件可能和实际不符，没有端到端测试，于是一层又一层地加补偿。每加一层，说明就更长，跳步也就更容易。把流程移进代码打破了这个循环：漏掉一步会变成一个失败的测试，而不是输出里一个无声的差异。

### 8.4 为什么删掉 runtime 契约层

三月加入的 runtime 层（policy、evaluator、decision 记录、recovery 提示、ledger、artifact graph，以及对外的 create/inspect/advance API）本意是给弱模型和 harness 一个结构化契约：当前状态、允许的下一步动作和推荐的决定。用意是对的，但这个契约从没送到模型面前：只有带 `--api-envelope` 才会输出，而 `SKILL.md` 从不传这个参数，也没有别的东西读它。`run` 接管流程以后，模型不再选择下一步动作，这个契约就没有读者了。模型仍然需要的是整次运行的结果，6.5 节用几个字段给出。真正起作用的部分（占有锁、可续跑的状态、用量记录）移进 `pipeline` 和 `state.json`，其余删除。

### 8.5 为什么长文本变换是子系统，而不是整个项目

因为项目还要找到来源文字、决定怎样获取、做规范化、组装文章。长文本变换是核心，但它只是其中一个阶段。

### 8.6 系统有意做出的产品范围决定

- `bilingual` 指英文原文加中文翻译
- 只要有人工中文字幕，任何视频都只用它作为来源字幕轨
- 只信任人工字幕和口语语言的 ASR 轨，从不使用机器翻译轨
- 口语不是中文或英文的视频报告为不支持
- 字幕够好时优先用字幕；没有字幕或质量严重不足时自动用 Deepgram
- 短视频和长视频走同一条路径
- 工作文件放在 `/tmp` 下，每个视频一个目录，重启后不保留
- 设计以本地为先，不是多租户平台

### 8.7 本设计不承诺的东西

- 通用工作流平台或多 agent 运行时
- 多租户的云端转录服务
- 中文和英文以外的语言
- 跨重启续跑

---

## 9. 实现映射与迁移（附录）

本节把现有代码和目标对应起来。阶段编号指 tracker #23 里的阶段。

| 现在 | 目标 | 阶段 |
|---|---|---|
| `SKILL.md`（约 370 行）和 `workflows/*.md` 驱动流程 | `run <url>` 驱动流程；`SKILL.md` 缩到几十行；删除 `workflows/` | 2、3 |
| 15 分钟以下视频走 Quick Mode；短视频的文字由宿主模型在对话里处理 | 删除；短视频走 `run`，一轮窗口，通过 LLM 后端处理 | 3 |
| `scripts/run_regression.py` 里有流程唯一的脚本化副本 | 它的流程变成 `pipeline`；回归脚本改为调用 `run` | 2 |
| 没有端到端测试 | 4 个真实视频的 fixtures 加假 LLM 命令行，离线运行 | 1 |
| `/tmp/<video_id>_state.md`、`machine_state.json`、分块 `manifest.json`、`telemetry.jsonl`、`/tmp/<video_id>_*` 文件 | `/tmp/yt-transcript/<video_id>/`，只有一个 `state.json` | 2、4 |
| `validate-state` 的阶段检查 | `pipeline` 内部断言 | 4 |
| `find-unfinished` | `status` | 2 |
| `scripts/preflight.sh` | `run` 内部各阶段的检查 | 4 |
| `scripts/cleanup.sh` | `cleanup` 阶段和命令 | 2、4 |
| `kernel/task_runtime/*`（policy、decision、evaluator、recovery、ledger、artifacts、api、lifecycle、contracts）、`--api-envelope`、11 个外层编排命令、暂停和取消 | 删除；运行锁和状态移进 `pipeline` | 4 |
| 固定分块执行器（`kernel/long_text/processing.py`，它在 `lifecycle.py`、`contracts.py` 里的 manifest 和契约，执行器路由 `execution.py`）、`long_text_mode`、`summarize`、`chapter_plan` / `build-chapter-plan`、`prompts/summarize.md`、`prompts/quick_cleanup.md` | 删除；只保留窗口执行器 | 4 |
| `chunk-document`、`chunk-segments`、`chunk-text`（`kernel/long_text/chunking.py`） | 一个从规范化文档生成单元的内部步骤 | 4 |
| legacy 开关（`chunk_mode: chars`、旧配置名、`--legacy-flat-output`、旧 manifest 兼容） | 删除 | 4 |
| `yt_transcript_utils.py`（约 8000 行）和 kernel 模块里的 `_bind_utils_globals` | `cli`、`pipeline`、`sources`、`text`、`llm`、`quality`、`config`，显式导入 | 5 |
| `kernel/subtitle_language.py` | `sources` | 5 |
| `kernel/long_text/window.py`、`outline.py`、`glossary.py`、`merge.py`、`prompting.py`、`kernel/text_cleanup/*` | `text` | 5 |
| `kernel/long_text/llm.py`、`agent_cli.py` | `llm` | 5 |
| `kernel/long_text/semantic.py`、utils 里的质量检查 | `quality` | 5 |
| `scripts/download.sh` | 先由 `sources` 作为子进程调用，后并入 `sources` | 2、5 |
| `system_design_notes/`、本文开头的"目标架构"标注 | 删除；README 按新入口重写 | 6 |

全程不变的：字幕语言选择、Deepgram 转写、窗口执行器的算法、大纲和术语表、质量检查。它们只搬位置，不改行为。

用户能感觉到的行为变化：

1. 短视频的文字改由 LLM 后端（通常是宿主 CLI）处理，不再由宿主模型在对话里直接写。
2. 首选字幕轨被限流时，运行以 `needs_user` 停下，不再悄悄换用另一条字幕轨（6.3）。
3. 对已完成的视频重跑会返回已有的文章，而不是从头再做；要重做用 `--restart`。
4. 字幕质量改道在 `source` 内部、规范化之前完成；结果不变。
5. 新增：`--max-seconds` 和 `in_progress` 结果，用于 shell 有时间上限的宿主。

---

## 10. 一句话总结

`yt-transcript` 是一个本地转录生产系统：一条确定性的命令 `run` 负责从 YouTube 链接到 Markdown 文章的已知路径，LLM 只在其中处理文字，宿主 agent 负责例外情况和与用户的沟通。
