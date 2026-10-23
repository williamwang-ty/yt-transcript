# yt-transcript System Design / yt-transcript 系统设计

## English Version

## 1. Document Positioning

This document explains the **system design of the whole `yt-transcript` project**: what it builds, how the work is divided between code and the model, and why.

`README.md` is the operator-facing guide (install, configure, commands). `SKILL.md` is the short contract the host agent reads. This document is neither; it explains structure, boundaries, and trade-offs. It is not a coding spec, but the names it uses (stages, files, state and result fields, reason codes, flags, config keys) are the ones in the code.

It works at three levels:

- **Project level** (sections 2 to 6): how one command turns a YouTube URL into a Markdown article, and what the agent around it is responsible for.
- **Subsystem level** (section 7): how the hardest stage, long-text transformation, works inside that command.
- **Implementation map** (section 9): which module implements each part, where the code differs from the plan it was built from, and how it got to this shape.

**Design history**: [section 9.3](#93-design-history) lists each stage of the refactor that produced this design, with its pull request, and links to the earlier design notes and the previous version of this document. Those were deleted from the repository but stay readable there.

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

In the old design (before stage 3; see [9.3](#93-design-history)) the agent executed the flow by reading about 370 lines of `SKILL.md` plus three workflow documents, running some twenty commands, and hand-writing a `state.md` file. That had predictable costs:

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
        │ /tmp/yt-transcript/<video_id>/state.json  (+ locks)   │
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

`run` executes a fixed list of stages. Each stage reads what earlier stages recorded, writes its outputs into the video's work directory, and only then marks itself `done` in `state.json`. A stage that is `done` and whose recorded outputs still exist is skipped on a rerun; once a stage runs again, every later stage runs again too.

| Stage | What it does | Main outputs | Can stop with |
|---|---|---|---|
| `metadata` | Inside the YouTube lock (6.4): let Homebrew update yt-dlp (at most hourly), then fetch the video's yt-dlp info once with `yt-dlp -J` (title, channel, date, duration, chapters, track list, audio-track languages); later stages read this file instead of asking YouTube again | `metadata.json`, `chapters.json` | `needs_user` (`cookies_required` after one retry with Chrome cookies, `login_required`, `rate_limited`, `tool_missing`), `in_progress` (the wait for YouTube used up `--max-seconds`), `failed` |
| `source` | Detect the spoken language and pick the trusted track (6.3); download it and parse it into raw text and timed segments; score the subtitle quality and switch to Deepgram when the scoring rule calls for it; or transcribe with Deepgram directly | `source/`, `raw_text.txt`, `segments.json`, the route in `state.json` | `unsupported`, `needs_user` (preferred track refused, Deepgram key missing, or a yt-dlp case above), `failed` |
| `normalize` | Build the normalized document: the cleaned text, the timed segments, and whether chunking follows the text or the segments | `normalized_document.json` | `failed` |
| `plan` | Choose the text tasks for the mode (Chinese: `cleanup_zh`; bilingual: `structure_only`, then `translate_only`), mark the text `short` or `long`, and estimate its size (input tokens, expected rounds per task). Chinese Deepgram text gets an extra instruction to repair recognition artifacts | `plan.json` | `failed` |
| `text` | Cut the normalized document into chunks, then run each planned task through the window executor (section 7); each task is recorded separately | `text/` | `needs_agent_text`, `needs_user` (`llm_api_fallback`, 6.8), `in_progress`, `failed` |
| `merge` | Join the final task's paragraphs in order (the first task's with the outline's headings and whole sentences, 7.5 B), then drop headings, paragraphs and paragraph openings repeated at round seams | `optimized.md` | `failed` |
| `verify` | Run the quality check against the raw text | `quality.json` | `failed` (hard failures only; warnings are recorded) |
| `assemble` | Write the article: frontmatter, header, body and footer, named `yyyy-mm-dd. <title>.md` | the article in `output_dir` | `needs_user` (`output_dir_missing`), `failed` |
| `cleanup` | Delete the intermediate files once the article exists; keep `state.json` | — | — |

Notes:

- **Idempotent stages.** Rerunning an unfinished stage starts that stage over, except that expensive sub-steps keep their results inside the stage (the downloaded audio, the Deepgram result of each audio piece, the outline, finished window rounds), so an interrupted stage never pays twice.
- **No separate preflight.** Each stage checks what it needs right before it needs it: yt-dlp at `metadata` (including the Homebrew update, at most hourly, because YouTube keeps breaking older yt-dlp releases), the Deepgram key only when `source` chooses Deepgram, an LLM backend only at `text`, `output_dir` only at `assemble`. Cheap paths stay cheap.
- **The source is final before any LLM call.** The subtitle quality reroute happens inside `source`, so `normalize` and everything after it work on accepted text.
- **One path for every length.** Short and long videos go through the same stages. `plan` marks the text `long` when the video runs 30 minutes or more, or when a shorter video's text is above the single-request limit (about 4,100 to 4,800 estimated tokens, depending on the route). For a `long` text, YouTube chapters become outline hints (on the segment path they also cut the chunks); a `short` text uses neither. The outline pass is skipped whenever one round can process the whole text (7.4), so a short video usually takes one request per task.
- **Free routing check.** `--stage plan` runs `metadata` through `plan` without spending money. If the route needs Deepgram, it records that decision and stops inside `source`, without transcribing.

### 6.3 Source selection

The `source` stage applies these rules (`kernel/sources/subtitle_language.py` picks the candidates):

1. Detect the spoken language from the strongest signal available: the audio track YouTube marks as original, then the metadata `language`, then the original-language ASR track (when exactly one language has one), then the human subtitle tracks (when they all share one language). When none of these exists, the script of the video's own text (title, description, chapter titles, tags) gives a guess (`video_text`): Han characters mean Chinese, kana Japanese, hangul Korean. Latin script gives no guess, because English, Indonesian or Spanish look alike; neither does text with only a few CJK characters or mostly Latin words.
2. Trust only human subtitles and the ASR track in the spoken language. YouTube's machine-translated tracks are dropped.
3. Human Chinese subtitles come first for every video; mode `chinese`.
4. Chinese video: otherwise the Chinese ASR track; mode `chinese`.
5. English video: otherwise English subtitles, human ones first; mode `bilingual` (English source plus Chinese translation).
6. Other spoken languages: `unsupported` (reason `unsupported_language`), unless human Chinese subtitles exist. When no signal names the language, every trusted Chinese or English track is a candidate.
7. No trusted track, or a track whose quality score triggers the reroute rule (critical quality on the `chinese` route): transcribe with Deepgram in the detected language, or with Deepgram's own language detection when nothing, not even the video's text, names it. Deepgram runs automatically; there is no confirmation step. A Deepgram result in an unsupported language also returns `unsupported`.

A language that comes only from the video's text, or from nothing, is not firm: `--mode` names the spoken language instead (`chinese` = Chinese audio, `bilingual` = English audio), Deepgram is asked for that language, and the route records `language_signal: mode_override`. The `unsupported` result for such a guess, and for a language Deepgram detected itself, says so in its message. The text signal exists because a video with no caption track and no metadata language otherwise depends on Deepgram's detection alone, and that detection heard a Mandarin video (DQGrPstFRVI, whose title and description are Chinese) as Indonesian. Deepgram-specific options (restricting detection to Chinese and English, confidence thresholds) were not used.

When YouTube refuses the preferred track (rate limit or bot check) while a later track still downloads, `source` does not quietly take the next track. The old agent-driven flow did, which turned a Chinese article into a bilingual one and made results differ between runs. `source` returns `needs_user` with reason `rate_limited` (`cookies_required` for a bot check), names `preferred_track` and `fallback_track`, and gives an `accept_command`: the same command with `--mode <fallback mode>`. The agent asks the user whether to wait and rerun, or to accept the fallback. Such a refusal concerns this video only. When YouTube refuses every track, or the page itself, it is refusing the machine, and every run on it stops ("Several runs at once" in 6.4).

`yt-dlp` stays the acquisition dependency. Direct YouTube InnerTube fetching was evaluated: it is lighter for subtitle-only runs but is an unofficial, interface-sensitive surface, does not remove the need for the Deepgram fallback, and would move extraction maintenance into this project. It remains a possible future fast path, not the default.

### 6.4 Work directory and state

Each video has one work directory:

```text
/tmp/yt-transcript/<video_id>/
  state.json                 the only state file; kept after cleanup
  metadata.json  chapters.json
  source/                    subtitles/ or audio/ from download.sh (plus audio pieces when split),
                             subtitle_document.json (the subtitle quality check),
                             deepgram.json, deepgram_chunk_NNN.json (Deepgram result per audio piece)
  raw_text.txt  segments.json  normalized_document.json
  plan.json
  text/                      manifest.json and chunk_NNN.txt (the chunks), glossary.json (cleanup_zh),
                             window/ (first task: state.json, out_NNNNN.md per paragraph),
                             window/processed/ (translate_only)
  agent_task/                prompt.md, task.json, output.txt, answers/; only while requests go to the agent
  optimized.md  quality.json
```

Beside the video directories, `/tmp/yt-transcript/.locks/` holds the lock files (`video-<video_id>.lock` per video, `youtube.lock`, `articles.lock`) and two small records: `youtube_rate_limit.json` (YouTube's last refusal of this machine) and `yt-dlp-update.stamp` (the last Homebrew check). None of them is ever deleted.

`YT_TRANSCRIPT_WORK_ROOT` replaces `/tmp/yt-transcript` and `YT_TRANSCRIPT_OUTPUT_DIR` replaces the configured `output_dir`; the regression runner uses both so it never touches real runs or the notes folder. The video locks move with the work root. The YouTube lock and the two records stay in `/tmp/yt-transcript/.locks/` whatever the work root, because they are about the machine; only `YT_TRANSCRIPT_LOCK_DIR` moves them, which the offline tests do.

`state.json` is written atomically whenever a stage starts or ends and before every window round. Its shape, from a finished bilingual run (shortened):

```json
{
  "schema": 1,
  "video_id": "abc123",
  "url": "https://www.youtube.com/watch?v=abc123",
  "status": "done",
  "created_at": "2026-10-22T16:59:52.758+00:00",
  "updated_at": "2026-10-22T16:59:53.611+00:00",
  "overrides": {"source": null, "mode": null},
  "metadata": {"title": "Some Talk", "channel": "Some Channel", "upload_date": "20261001", "duration": 437},
  "route": {
    "source_language": "en",
    "language_signal": "original_audio_track",
    "source": "subtitles",
    "language": "en",
    "track": "en",
    "track_kind": "manual",
    "mode": "bilingual",
    "subtitle_quality": {"score": 1.0, "band": "excellent"},
    "blocked_tracks": [],
    "fallback_used": false,
    "rerouted_from": null,
    "reason": "English video with human English subtitles"
  },
  "stages": {
    "metadata": {"status": "done", "started_at": "…", "finished_at": "…", "seconds": 0.2,
                 "outputs": ["metadata.json", "chapters.json"]},
    "plan": {"status": "done", "summary": {"tasks": ["…"], "video_path": "short", "estimated_input_tokens": 1800}},
    "text": {"status": "done", "tasks": {
      "structure_only": {"status": "done", "cursor": 94, "units": 94, "rounds": 1, "round_count": 1,
                         "invalid_rounds": 0, "fallback_rounds": 0,
                         "outline": {"status": "skipped"}, "coverage": {"…": "…"}, "efficiency": {"…": "…"}},
      "translate_only": {"status": "done", "cursor": 27, "units": 27, "rounds": 1}
    }},
    "verify": {"status": "done", "summary": {"passed": true, "hard_failures": [], "warnings": [], "checks": {"…": "…"}}}
  },
  "llm": {"backend": "claude-code-cli", "requests": 2, "input_tokens": 4794, "output_tokens": 4819,
          "estimated_tokens": true},
  "warnings": [],
  "last_result": {"status": "done", "at": "2026-10-22T16:59:53.611+00:00"},
  "output_file": "/Users/me/notes/01.input/01.articles/2026-10-01. Some Talk.md",
  "raw_chars": 6539
}
```

`status` is `running` while a call is active, `planned` after a `--stage plan` call, and otherwise the status of the last result. A stage record also keeps what later stages need: `normalize` its result, `plan` the task list (`summary`), `source` the path of the downloaded audio. The `llm` token counts are local estimates from the prompts and outputs, because CLI backends report no usage.

Rules:

- **Resume** means rerunning the same command: `run` reads `state.json` and starts at the first stage that is not `done`. Inside `text`, the window executor resumes at its cursor.
- **Finished runs stay finished.** `cleanup` keeps `state.json`, so rerunning `run` on a finished video returns `done` with the existing article at once, as long as that file still exists (if it was deleted, the run starts over). Rerunning a whole batch therefore only does the unfinished videos. `--restart` produces the article again.
- **Overrides are sticky.** `--source` and `--mode` are saved in `state.json` and stay in effect on reruns of that video until `--restart`, so a resume never silently flips the route. An override that cannot be honored (`override_not_applicable`) is not saved, so a plain rerun goes back to the earlier route.
- **Config is read on every call**, but finished stages are not redone when it changes. `--from <stage>` redoes them. After `cleanup` the intermediate files are gone, so `--from` on a finished run redoes every stage; it is meant for unfinished runs (for example `--from text` after a quality failure).
- **The video lock** keeps two processes from writing one work directory: a call holds it from start to end, and a second call on the same video waits for the first (within its `--max-seconds`), then continues from the state the first one left, usually returning `done` at once. Different videos share no files and run in parallel; only their YouTube requests take turns (below).
- **Hygiene**: `video_id` must match `[A-Za-z0-9_-]+` before it becomes a path. `state.json` never holds API keys; it records the backend's name, not its credentials.
- **Lifetime**: the directory lives under `/tmp`, so a reboot loses unfinished runs. This is an accepted limitation.

#### Several runs at once

Several `run` calls can be going at once: a batch started in parallel, or two agents on one machine. Two file locks (`flock`, `kernel/locks.py`) keep them apart. There is no queue, scheduler or extra state; a call that has to wait simply waits.

- **Two locks, one order.** The video lock (`<work root>/.locks/video-<id>.lock`) is held for the whole call. The YouTube lock (`/tmp/yt-transcript/.locks/youtube.lock`) is held around each yt-dlp step: the update check with the metadata fetch, the subtitle download, the audio download. A call always takes its video lock first, so two calls cannot deadlock. Deepgram, the LLM and every other stage run outside the YouTube lock, so one video's text stage overlaps another's download.
- **One yt-dlp at a time.** YouTube meters requests per machine (its IP, or the account in the cookies), not per process; parallel extractions are what got this machine "rate-limited by YouTube for up to an hour". Under the lock, every yt-dlp call also uses yt-dlp's `-t sleep` preset, its own pause between requests, so parallel runs ask YouTube no faster than one run does, with no pacing numbers of ours. A yt-dlp too old for `-t sleep` (before mid-2025) runs without it, with a log line. Each video's page is extracted once by `metadata` (`yt-dlp -J`, stderr kept, no per-field fallbacks); the subtitle download loads that file with `--load-info-json`. The audio download still passes the URL, so the Deepgram route makes a second page request.
- **Children hold the locks too.** The lock descriptors are passed to `download.sh` and the update script, so a killed `run` whose yt-dlp is still going keeps YouTube locked until that yt-dlp ends.
- **No stale locks.** The operating system frees a process's locks when it dies, so nothing has to detect or take over a dead holder. Lock files are never deleted (deleting one while another call waits on it would let two holders in); each records its holder's pid, start time and video id for the messages.
- **Waiting counts against `--max-seconds`.** When the budget runs out before a lock is free, the call returns `in_progress` naming the holder, and its `next_command` keeps waiting. Without `--max-seconds` the call waits as long as it takes. A same-video call therefore never fails with `locked`.
- **One refusal stops the runs already going.** When YouTube refuses the machine as a whole, the call writes the time and yt-dlp's message to `/tmp/yt-transcript/.locks/youtube_rate_limit.json`. Every call checks that record right after taking the YouTube lock: a call that began before the refusal returns `needs_user` (`rate_limited`, or `cookies_required` for a bot check) quoting yt-dlp, without asking YouTube again; a call that begins later tries once. A batch thus stops at the first refusal instead of feeding YouTube more requests, and a rerun after the user's go-ahead tries again. There is nothing to clear and no cooldown of ours: yt-dlp recognises the limit, YouTube decides when it ends.
- **What counts as a refusal of the machine**: yt-dlp's rate-limit message (HTTP 429, "try again later", "rate-limited by YouTube"), and a bot check that Chrome cookies did not pass. A track refused while another track downloads (6.3), and a video that needs a signed-in account (`login_required`), concern that video only and are not recorded.
- **One classifier.** `kernel/sources/youtube.py` is the only code that reads yt-dlp's error text; it names `rate_limited`, `bot_check` or `login_required`, and anything else stays a retryable `download_error`. `download.sh` runs each yt-dlp call once and passes the error on: no retries, no error reading.
- **Cookies only for the bot check.** A bot check with no cookies configured gets one retry with Chrome's cookies. A rate limit gets no retry, because more requests only extend it.
- **The yt-dlp update** runs inside the YouTube lock, so no yt-dlp runs while Homebrew replaces it. Homebrew is asked at most hourly per machine, and the time is recorded whatever the result. Only a yt-dlp that Homebrew manages is upgraded; any other is only reported, and nothing installs through pip.
- **`cleanup`** takes the video lock before it deletes, so it never removes a running video's directory.

### 6.5 Results handed back to the agent

Every `run` call prints exactly one JSON object on stdout and exits with a fixed code. Progress lines (stages, window rounds, yt-dlp messages) go to stderr.

| `status` | Exit | Meaning and fields | What the agent does |
|---|---|---|---|
| `done` | 0 | Article written: `output_file`, `title`, `route`, `warnings`, `llm`. After `--stage plan`: `stage`, `route`, `plan`, and a `message` when the route needs Deepgram | Report the result |
| `failed` | 1 | A stage failed: `stage`, `reason`, `message`, `retryable` (plus `hard_failures` for a quality failure) | Rerun `next_command` once if `retryable`, otherwise report; may suggest an override |
| `needs_user` | 3 | Only the user can fix it. `reason` is `cookies_required`, `login_required`, `rate_limited`, `deepgram_key_missing`, `tool_missing`, `output_dir_missing` or `llm_api_fallback`; `message` says what to do. A refused preferred track adds `preferred_track`, `fallback_track` and `accept_command`. `llm_api_fallback` (6.8) adds `choices`, each with `answer`, `label` and `command` | Tell the user; rerun after they act, run `accept_command` if they accept the fallback, or run the `command` of the choice they pick |
| `unsupported` | 4 | The spoken language is not supported: `reason` `unsupported_language`, `language` | Report; do not retry. When `message` suggests `--mode` (the language was guessed from the title or detected by Deepgram) and the user says the video is Chinese or English, rerun with that `--mode` |
| `needs_agent_text` | 5 | No LLM backend is reachable; one request is handed over: `task.name`, `task.prompt_file`, `task.output_file` | Answer it, write the answer to the output file, rerun `next_command` |
| `in_progress` | 6 | The `--max-seconds` budget ran out at a safe point: `stage`, `progress` (`stages_done`, `text_tasks`). Also when the budget ran out while waiting for another run of the same video or for YouTube (6.4); `message` names the holder | Rerun `next_command` until the status changes |

Every result also has `video_id`, `work_dir` and `seconds`. Every result except `done` and `unsupported`, and every result of a `--stage plan` call, has `next_command`: the command that continues the run. Exit code 2 is reserved for command-line usage errors (`{"status": "usage_error", "message": …}`), such as an argument that is not a YouTube URL.

`failed` reasons: `download_error`, `deepgram_error`, `llm_error` and `llm_timeout` are retryable. `source_error`, `normalize_error`, `plan_error`, `merge_error`, `quality_hard_failure`, `override_not_applicable` and `config_error` are not, and neither is `<stage>_error`, which wraps an unexpected exception in that stage.

A `done` result:

```json
{
  "status": "done",
  "video_id": "abc123",
  "output_file": "/Users/me/notes/01.input/01.articles/2026-10-01. Some Talk.md",
  "title": "Some Talk",
  "route": {"source": "subtitles", "language": "en", "track": "en", "track_kind": "manual", "mode": "bilingual"},
  "warnings": ["Size ratio 4.30x vs raw text is outside expected range (1.2-4.0x for bilingual)"],
  "llm": {"backend": "claude-code-cli", "requests": 9, "input_tokens": 41200, "output_tokens": 18700,
          "estimated_tokens": true},
  "work_dir": "/tmp/yt-transcript/abc123",
  "seconds": 412.3
}
```

A `needs_agent_text` result names a prompt file (the whole request, numbered units included) and an output file:

```json
{
  "status": "needs_agent_text",
  "video_id": "abc123",
  "stage": "text",
  "reason": "no_llm_backend",
  "message": "No LLM backend is reachable (…). Answer the request in task.prompt_file exactly as the model would, write only the answer to task.output_file, then run next_command.",
  "task": {
    "name": "structure_only",
    "prompt_file": "/tmp/yt-transcript/abc123/agent_task/prompt.md",
    "output_file": "/tmp/yt-transcript/abc123/agent_task/output.txt"
  },
  "next_command": "python3 /path/to/yt-transcript/yt_transcript_utils.py run https://www.youtube.com/watch?v=abc123",
  "work_dir": "/tmp/yt-transcript/abc123",
  "seconds": 1.4
}
```

The task is exactly one LLM request: an outline request, a window round, or a fallback chunk, with the same prompt, numbered units, and end marker an LLM backend would receive. On the rerun, `run` moves the answer to `agent_task/answers/<sha256 of the prompt>.txt`, gives it back when the same prompt comes up, and validates it the same way it validates any response (section 7); answers stay until the text stage finishes. A short video needs about one handoff per text task; a long video needs several. The agent is therefore simply the slowest LLM backend, with no separate text path to maintain. It is a fallback: normally `auto` reaches the host CLI, and `llm_backend: api` the API.

### 6.6 Override flags

| Flag | Kept for reruns | Effect |
|---|---|---|
| `--source subtitles\|deepgram` | yes | Force the source. `deepgram` skips subtitles; `subtitles` keeps the chosen track even when its quality score would trigger the reroute. Redoes `source` and every later stage. |
| `--mode chinese\|bilingual` | yes | Force the output form. The mode follows the source text (Chinese text gives `chinese`, English text gives `bilingual`), so this picks the track: on an English video, `bilingual` uses the English subtitles even when human Chinese subtitles exist, and `chinese` uses the human Chinese subtitles. When no track or metadata names the spoken language (6.3), it also names it for Deepgram: `chinese` = Chinese audio, `bilingual` = English audio. Redoes `source` and every later stage. |
| `--from <stage>` | no | Mark that stage and every later stage not done, then run. For recovery and debugging. |
| `--stage plan` | repeated in `next_command` | Stop after `plan`: the free routing check. |
| `--max-seconds <n>` | repeated in `next_command` | Start no new stage or window round after n seconds, once the call has finished at least one; return `in_progress`. |
| `--llm-api once\|always\|no\|never` | yes | Answer the `llm_api_fallback` question (6.8) for this video; `always` and `never` also save the answer in `config.yaml`. Redoes nothing. |
| `--restart` | no | Delete the work directory, including saved overrides, and start over. |
| `--config-path <file>` | repeated in `next_command` | Read another `config.yaml` instead of the one in the skill root. |

These are the agent's escape hatches. A user request like "use Deepgram for this one" or "just give me Chinese" maps to a flag, not to hand-run steps. A combination that cannot be honored (`--mode bilingual` on a Chinese video, `--mode chinese` on an English video without human Chinese subtitles, `--source subtitles` when no trusted track exists) returns `failed` with reason `override_not_applicable`; nothing is guessed.

### 6.7 Long runs and host time limits

A long video can take a quarter of an hour or more, while many hosts kill shell commands after a few minutes. Two properties make that safe on any host:

- `run` can be killed at any moment. Every finished stage and window round is already on disk, the operating system frees a killed process's locks, and rerunning loses at most the request that was in flight.
- `--max-seconds <n>` lets the agent avoid the kill: `run` stops at the first stage or round boundary after n seconds and returns `in_progress` with the progress so far. SKILL.md has the agent pass `--max-seconds 300` with a 10-minute shell timeout (about half of the limit when its shell limit is shorter), and rerun until the status changes.

This needs nothing host-specific such as background jobs.

### 6.8 LLM backend

Every text request of the `text` stage goes through one request function in the window executor, with three backends behind it:

- **host CLI**: the host agent's own command line from a built-in table (Claude Code `claude -p`, Codex `codex exec`, opencode, pi, Gemini CLI, Cursor `cursor-agent`), or any non-interactive command given in `llm_agent_cli` or the `YT_TRANSCRIPT_AGENT_CLI` environment variable. It runs in an empty temporary directory, with tools switched off where the CLI has a flag for it (Claude Code, pi).
- **API**: an OpenAI-compatible or Anthropic Messages endpoint from config (`llm_api_format`, `llm_base_url`, `llm_model`, `llm_api_key`), with retries, backoff and optional streaming.
- **agent handoff**: the `needs_agent_text` result above.

With `llm_backend: auto` (the default), each task picks its backend when its window run starts: an explicit command first (the environment variable, then `llm_agent_cli`), else the CLI an earlier call of the same task used, else the host detected from its environment variables. The CLI is used only after it answers a one-line probe within `llm_cli_probe_timeout_sec`. A failed CLI request is retried `llm_cli_max_retries` times after the same exponential backoff the API uses (`llm_backoff_sec`). `llm_backend: api` uses the API and skips the CLI. `state.json` records the backend as `<agent>-cli`, `api` or `agent-handoff`.

In `auto` mode the CLI can still be unusable: none was found, its probe failed, or a request still failed after its retries. With the API configured, `llm_api_fallback` decides what happens then. The API is billed per request, and several parallel videos would all switch to it at once, so the default asks the user instead of choosing:

| `llm_api_fallback` | No usable CLI | A CLI request still failing |
|---|---|---|
| `ask` (default) | `needs_user`, reason `llm_api_fallback`, with four `choices` (below) | the same, at that round |
| `auto` | the API takes every request | the API takes the rest of the run, with a warning |
| `never` | agent handoff | a retryable `llm_error`; the rerun continues at that round |

Without the API configured, the run behaves as with `never`. The question lists four answers, each with its command (`next_command` plus `--llm-api <answer>`): `once` (the API for this video), `always` (the API, and `llm_api_fallback: auto` saved in `config.yaml`), `no` (not for this video) and `never` (not for this video, and `llm_api_fallback: never` saved). The video keeps its answer in `state.json` until `--restart`, like an override. Saving changes only that one line of `config.yaml` and keeps the file's mode, since the file holds API keys. Asking costs one stop per video at most, and a batch asks once (SKILL.md); the alternative, a silent switch, is how the paid API used to run up without anyone deciding.

Nothing depends on a provider's special request fields. `max_tokens` is sent as a loose ceiling; truncation is detected through the window protocol's end line, which also works for CLI output that has no `finish_reason`. A reported `finish_reason` of `length` (or the provider's equivalent) only makes later rounds write less.

### 6.9 Quality check and assembly

- `verify` checks structure, signs of truncation, the size ratio against the raw text, repeated phrases and seam duplicates, Chinese readability, bilingual pairing, glossary drift, semantic anchors (dates, numbers, versions, URLs), and the window run's coverage. Only hard failures stop the run (`failed`, stage `verify`, reason `quality_hard_failure`), and then no article is written: a missing or empty file, no `##` section heading in more than 1,200 characters, or a bilingual body without a single English-then-Chinese paragraph pair. Everything else is a warning, saved in `state.json` and returned with `done` for the agent to mention. It also records, with no warning, the longest paragraph, how many paragraphs pass the prompts' split point (500 characters of Chinese, or 250 words for the English paragraphs of bilingual output), how many contain a single line break, and how many `##` headings follow a paragraph that stops mid-sentence (a heading that split a sentence: what the placement in 7.5 B let through, such as a paragraph ending on a dash).
- `assemble` computes everything that used to be built by hand from state: the filename `yyyy-mm-dd. <title>.md` (illegal characters replaced, the video id when the title is empty, the date never in the raw `yyyymmdd` form; ` (<video_id>)` is appended when a file of that name holds another video's article, judged by its frontmatter `url`, so a rerun of the same video replaces its own article and nothing else is overwritten), YAML frontmatter with quoted values (`title`, `source`, `url`, `channel`, `author`, `date`, `created`, `type`, `bilingual`, `duration`, `transcript_source`, `tags`, `via`), a header with the title, the channel link, the language mode and the duration, and a one-line footer naming the transcript source. The bilingual layout itself (each English paragraph followed by its Chinese translation) comes from `translate_only`. The name is chosen and the file written under a machine-wide lock (`articles.lock` next to the YouTube lock), so two videos with the same title finishing at once cannot both take the free name; the file goes to a dot-prefixed temp file in the same folder and is then renamed, so a notes app never reads half an article.
- `cleanup` runs only after the article exists. A failed or interrupted run keeps its whole directory so it can be resumed or inspected.

### 6.10 Public commands

Every command runs as `python3 <skill-root>/yt_transcript_utils.py <command>`.

| Command | Purpose |
|---|---|
| `run <url> [flags]` | The whole flow (sections 6.2 to 6.9); `<url>` may also be a bare 11-character video id |
| `status [<video_id>]` | Without an id: every run under the work root, split into `unfinished` (each with `next_stage`, `last_result` and `next_command`) and `finished`. With an id: that video's entry plus its whole `state.json` |
| `cleanup <video_id> [--force]` | Remove a work directory; refuses an active run, and an unfinished run without `--force` |
| `subtitle-info` | Debug: read `yt-dlp -J` metadata on stdin and print the language detection and the track candidates; `download.sh` uses it too |
| `verify-quality <file> [--raw-text <file>] [--bilingual] [--work-dir <dir>]` | Debug: rerun the quality check on any file |

Everything else is internal. Pause, cancel, telemetry queries and the external runtime API were removed: interrupting the process stops a run, rerunning resumes it, and `state.json` holds the timings and usage.

### 6.11 Module structure

```text
yt_transcript_utils.py    entry script at the skill root; calls kernel.cli.main()
kernel/
  cli.py                  argument parsing, the five commands, JSON output and exit codes
  pipeline.py             stage list, state.json, the two locks, resume, overrides, results;
                          download.sh calls; the agent handoff; status and cleanup
  locks.py                the flock primitive: bounded waits, holder info, never-deleted lock files;
                          the machine-wide lock directory
  sources/
    youtube.py            the machine-wide YouTube lock path, the yt-dlp error classifier, the refusal record
    subtitle_language.py  spoken language, trusted tracks, subtitle candidates
    deepgram.py           audio splitting, the Deepgram API with retries and a per-piece cache, parsing
  text/
    cleanup/              VTT parsing and subtitle cleanup, CJK spacing, overlap trimming, post-merge cleanup
    normalize.py          the normalized document
    subtitle_quality.py   subtitle quality score and the reroute signal
    plan.py               text tasks, short or long, size estimate
    chunking.py           the chunks the window run reads
    window.py             the sliding-window executor and the backend choice per task
    outline.py            global outline, chapter hints, terms per round
    structure.py          headings and paragraph ends at sentence boundaries of the written text
    glossary.py           transcript glossary and drift checks
    prompting.py  measure.py  merge.py  assemble.py
  llm/
    api.py                HTTP client for OpenAI- and Anthropic-format APIs
    agent_cli.py          host CLI table, detection, probe, call
  quality/
    verify.py  semantic.py   the checks of the verify stage
  config.py               config.yaml parsing and the default of every setting
  files.py                atomic writes, timestamps
scripts/download.sh       yt-dlp: metadata, subtitle-info, subtitles, audio (run as a subprocess; one call each)
scripts/update_yt_dlp.sh  the hourly `brew upgrade yt-dlp`
prompts/*.md              task prompts: cleanup_zh, structure_only, translate_only
```

Dependencies point one way. `config`, `files` and `locks` import nothing else from the kernel; `llm` uses only `config`, `sources` only `config`, `files` and `locks`; `text` uses `config`, `files` and `llm`; `quality` uses `text`; `pipeline` uses all of them; `cli` uses `pipeline`, plus `sources` and `quality` for the debug commands. Nothing in `kernel/` imports `yt_transcript_utils.py`. The only loop runs across processes: `pipeline` starts `download.sh`, which pipes metadata through the `subtitle-info` command.

The module plan written before the split (stage 0 of the refactor) differs from the result in four places, for these reasons:

- **Subtitle quality lives in `text/subtitle_quality.py`, not in `sources`.** It scores cleaned subtitle text, and `normalize` uses it as well as the `source` stage; in `sources` it would make `text` depend on `sources`. The `source` stage reaches it through `pipeline`.
- **`download.sh` stays a subprocess** instead of moving into `sources`. It is about 420 lines of shell that build the yt-dlp commands (settings, cookies, `-t sleep`, the subtitle candidates in order, the audio format choice), run each once and print JSON; the retry and error policy lives in Python (`sources/youtube.py`, `_Run.download`). Porting the rest would risk behavior for no user-visible gain. `pipeline` passes it paths and yt-dlp settings through environment variables, and the held lock descriptors.
- **The agent handoff stays in `pipeline`** (`AgentHandoff`), not in `llm`. It belongs to the run rather than to a backend: it writes into the work directory and ends the call with a `needs_agent_text` result. The window executor receives it as a callback and treats it like any other backend.
- **`config.py` owns the defaults.** It defines the default of every setting, and `load_config` always returns every key filled in, so the modules that read a setting get a complete value instead of keeping defaults of their own.

### 6.12 Testing

- **Unit tests** per area in `tests/regression_*.py`, all run by `python3 -m unittest tests.test_regressions` (offline, well under a minute). Tests of deleted code were deleted with it.
- **End-to-end tests** (`tests/regression_e2e.py`) run the whole flow offline through `scripts/run_regression.py --fixtures tests/fixtures/e2e`: a fake `yt-dlp` on `PATH` (`tests/e2e/`) serves recorded metadata and subtitles, a fake Deepgram answers from a recorded response, and a fake LLM CLI (`tests/fake_llm.py`, plugged in through `llm_agent_cli`) implements the window protocol and the outline JSON. Cases: Chinese human subtitles (long, several rounds with an outline), English subtitles (bilingual), no subtitles (Deepgram), and an unsupported language. Routes and outputs are compared with `tests/fixtures/e2e/baseline.json`; `UPDATE_E2E_BASELINE=1` rewrites it after an intended change. The four cases run at the same time, and the fake `yt-dlp` logs every call, so the test also checks that yt-dlp never ran twice at once and that each video's page was fetched once (twice on the Deepgram route). `tests/regression_run.py` covers resume inside `text`, sticky and rejected overrides, `--max-seconds`, `--from`, `--restart`, `--stage plan`, the quality reroute, and every result status. `tests/regression_concurrency.py` covers the locks with real processes: eight processes racing for one lock never overlap, a killed holder frees it, a bounded wait returns `in_progress` with the holder, `cleanup` refuses a running video, and a rate limit stops the queued runs without another yt-dlp call while a later run tries once.
- **Real-video regression** on a machine that can reach YouTube (`scripts/run_regression.py`, which calls `run`; see `tests/live_regression/README.md`) before merging any change to the main path. `--stage plan` and `--no-deepgram` keep it free or cheap.

### 6.13 What SKILL.md contains

About 80 lines: run `run <url> --max-seconds 300`; act on each result status as in 6.5; map user requests to override flags; for several URLs, call `run` once per URL, stop the batch only on results that would hit every video (`cookies_required`, `rate_limited`, `tool_missing`, `output_dir_missing`), and summarize in a table; use `status` to find interrupted runs. No state schema, no step list, no Quick Mode.

---

## 7. Core Subsystem Design: Long-Text Transformation

The previous sections describe the whole project. This section zooms in on the `text` stage, which turns the normalized document into the article body.

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

The sliding-window executor (`kernel/text/window.py`) is the only executor. It runs `cleanup_zh`, or the bilingual pair `structure_only` → `translate_only`, where the second task numbers the paragraphs the first one wrote. It replaced a fixed-chunk executor that needed a stack of patches (continuity context, autotune, replanning) to hide its seams. Fixed chunks remain only as the layout of the window run's input and as its fallback for one stretch (below).

Paragraphs are written only by `cleanup_zh` and `structure_only`; `translate_only` keeps the English paragraphs and puts each one's translation under it. Both prompts carry the same Paragraph Rule: one paragraph covers one thing (a point, an example, a step, a speaker's turn); a new speaker or section always starts one, a turn of topic, example or scene usually does, a pause or a filler sentence does not; length is a guide (100-300 Chinese characters, or 2-6 sentences of about 50-150 words), and a paragraph above about 500 characters or 250 words is split at a turn of meaning if it has one. The program never splits or rejects a paragraph by length; `verify` only records the longest one (6.9).

#### Long-text vocabulary by layer

##### Layer 1: source shaping

- **normalized document**
  - built by the `normalize` stage: the cleaned text, the timed segments, and `preferred_chunk_source` (`text` for subtitles on the `chinese` route, `segments` otherwise); the segment times tell the executor where speech pauses are
- **chunk**
  - before the first round, the `text` stage cuts the normalized document into chunks of about `chunk_tokens_<task>` tokens (`kernel/text/chunking.py`, listed in `text/manifest.json`): at sentence ends on the text path, at subtitle cues on the segment path, where it also cuts before every YouTube chapter start when the plan says `long`. A chunk boundary stays a unit boundary. The text path (subtitles on the `chinese` route) has no chapter cuts, so a chapter can start inside a unit.
- **unit / `[S<n>]`**
  - one numbered piece of the source, a sentence where the source has punctuation. A sentence above about 120 tokens (common in unpunctuated ASR text) is cut at subtitle cue ends, longest pause first, or every ~120 tokens when the cues cannot be matched to the text, so in unpunctuated Chinese subtitles a unit often starts and ends in the middle of a sentence. Units are therefore reading positions, not sentences: where a heading or a paragraph end goes is decided on the written text, once the model's punctuation shows where sentences end (7.5 B). In `translate_only` a unit is one paragraph of the `structure_only` output, split only above about 600 tokens.

##### Layer 2: document-level guidance

- **global outline**
  - for `cleanup_zh` and `structure_only` (`window_outline: true`), one pass over the numbered full text before the first round (`kernel/text/outline.py`), in parts when the text is longer than `window_outline_read_tokens`. It returns a term list and the sections, each with its title, its first unit (checked to be in range and increasing) and the first words of the sentence it begins with. Those words are looked up in the source within two units of the given one; when found, the section moves to the unit holding them and keeps them, copied from the source, as its anchor; the heading goes before the written sentence that holds it (7.5 B). A unit number alone is not enough, because units of unpunctuated subtitles start mid-sentence. The outline is skipped when one round can process the whole text, since that round sees all of it. When the plan says `long`, YouTube chapters are mapped to units through the cue times, anchored at the text of the chapter's first cue (a chapter after the last cue is left out), and given as hints; the section nearest a chapter takes the chapter's unit, anchor and title, unless the title is a placeholder or not in the output language (an English chapter title in a `cleanup_zh` run keeps the model's translation). In a bilingual run each heading is an English `##` line followed by a Chinese one.
- **terms (glossary)**
  - names and terms with their correct spellings and seen mis-recognitions. With an outline, each round gets the outline's terms that occur in its window, and `translate_only` gets them with their Chinese renderings. Without an outline, `cleanup_zh` rounds use `text/glossary.json`, a transcript glossary built locally (no LLM) from the chunks, the title, the channel and the chapter titles; `verify` checks the final text against the same file.

##### Layer 3: round execution and control

- **cursor**
  - the first unit not yet written; everything before it is saved as complete paragraphs
- **read window / write target**
  - how much source a round reads from the cursor and how much output it should aim for; both adapt per round, capped by `window_read_tokens` and `window_write_tokens`
- **round**
  - one model call: read window + the tail of the written output (`window_tail_tokens`) + nearby outline lines + matching terms; the model copies each unit's `[S<n>]` tag in front of that unit's text and ends with `<!--END-->`
- **fallback**
  - after more than `window_marker_retries` invalid rounds in a row at the same cursor, the stretch is processed once as one plain fixed chunk of about `chunk_tokens_<task>` tokens, so the run still advances
- **ownership**
  - the video lock that `run` holds for the whole call (6.4); the executor has no lock of its own

##### Layer 4: verification, merge, and final outputs

- **round validation**
  - tags present, inside the window, and never going backwards; the output/source ratio between 0.4 and 2.5 times the expected one (checked once a round covers at least 200 source tokens)
- **coverage / efficiency**
  - which units were written, which stretches came out much shorter than the run's typical ratio, and how much source was read twice; recorded per task in `state.json`
- **merge**
  - ordered concatenation of the final task's paragraphs, followed by the deterministic post-merge cleanup of repeated headings, paragraphs and paragraph openings. The first task's paragraphs are joined through `kernel/text/structure.py`, which adds the outline's headings and mends paragraphs that stop inside a sentence (7.5 B); in a bilingual run this happens before `translate_only` numbers them
- **quality gate**
  - the `verify` stage (6.9)

#### Small long-text information flow

```text
normalized_document.json
        |
        v
chunks (text/manifest.json, chunk_NNN.txt)
        |
        v
numbered units [S1..Sn]
        |
        v
Global outline + terms (once, kept in the window state)
        |
        v
Round: read window from cursor + written tail + outline lines + terms
        |
        v
LLM backend (host CLI, API, or agent handoff)
        |
        v
Validate tags / END line / length ratio
        |
        +-------------------------+
        | valid                   | invalid (repeatedly)
        v                         v
Keep complete paragraphs     Process this stretch as one fixed chunk
Advance cursor                    |
        |<------------------------+
        v
More units? -> next round
        |
        v
Join paragraphs: headings before the sentences holding their anchors, unfinished sentences mended
        |
        v
merge stage -> verify stage -> assemble stage
```

A response cut off before `<!--END-->` only loses its last paragraph: the cursor moves past what was kept, so truncation means less progress, not a broken document.

### 7.5 Long-text subsystem architecture

#### Implementation view

| Concern | Code |
|---|---|
| Chunks | `kernel/text/chunking.py` (`chunk_document`), called once by the `text` stage |
| Units, rounds, sizing, validation, fallback, coverage | `kernel/text/window.py` (`process_window`, `run_window_rounds`, `parse_window_output`, `plan_round`, `update_sizing`) |
| Backend choice per task; what an unusable CLI does (`llm_api_fallback`) | the request function in `process_window`, over `kernel/llm/agent_cli.py` and `kernel/llm/api.py`; the question and its answers in `_Run.llm_api_question` and `_Run.answer_llm_api_question` |
| Outline, chapter hints, terms per round | `kernel/text/outline.py` |
| Headings and paragraph ends at sentence boundaries | `kernel/text/structure.py` (`structure_paragraphs`), called by `raw_stage_text` in `window.py` |
| Transcript glossary and drift check | `kernel/text/glossary.py` |
| Prompt assembly | `kernel/text/prompting.py` and `prompts/*.md` |
| Merge and seam cleanup | `kernel/text/merge.py`, `kernel/text/cleanup/post_merge.py` |
| One executor call per task, the time budget, the agent handoff, progress and usage in `state.json` | `kernel/pipeline.py` (`stage_text`, `AgentHandoff`, `llm_usage`) |

#### A. The program owns boundaries, not the model

The model never decides where its output starts or stops in the source. It copies the unit tags it was given, and the program derives paragraph ranges, checks that they are in order, and decides what to keep. This makes merge a plain concatenation. The one boundary the program cannot read from a source without punctuation is where sentences end; for headings and paragraph ends it waits until the model has punctuated the text (B).

#### B. Structure decided once, placed at sentence boundaries

The order is punctuation first, structure second. In unpunctuated subtitles, sentence ends exist only once a cleanup round has written the punctuation. The paragraph breaks a round makes inside its own output already fall at sentence ends, because the model sees the sentences it writes. Only two boundaries are fixed in units before that: where a round stops (always at the end of a unit) and where a section starts (a unit and the words it begins with). Both are therefore settled after the rounds, on the written text.

When the run has an outline, headings come only from it, and the program adds them; rounds write none. A round is told, for every section starting in its window, the unit and the words the section begins with, and starts a new paragraph at the beginning of the sentence holding those words. Where the heading goes is decided after the rounds: units of unpunctuated subtitles are cuts at cue pauses, so a heading put at a unit, or a model asked to write one there, splits sentences. The program looks for the section's anchor in the paragraphs near the section's unit (letters and digits only; when the whole anchor is missing, its first or its last six characters) and puts the heading before the sentence that holds it, splitting the paragraph there when needed. When the anchor is not found, the heading goes between two paragraphs, before the first one whose middle reaches the section's unit.

In the same pass, a paragraph that stops inside a sentence (usually where one round ended) takes the rest of that sentence from the next paragraph or hands its unfinished sentence to it, whichever piece is shorter, up to about 150 characters; a heading a round wrote right after such a paragraph moves before that sentence. A paragraph ending on a dash or a colon counts as finished, and text with no sentence end is left as it is, since nothing shows where its sentences are. The counts are kept as `structure` in the task's progress. Without an outline (the whole source fits in one round, `window_outline: false`, or the outline failed), rounds place their own headings and only the mending applies.

Asking the model to write each heading before the sentence holding the anchor came first; the model still broke sentences at unit tags now and then, once adding a dash to hide the break, which is why the placement moved to the program.

A separate punctuation pass before the rounds, so that every unit is a whole sentence, was considered and not built. The cleanup has to rewrite the whole text anyway (punctuation, fillers, recognition errors), so a punctuation pass would write it a second time, roughly doubling output tokens and time, and it would only help at the two boundaries above, which the program already puts at sentence ends after the rounds.

#### C. Sizes as soft targets

`max_tokens` (`max_output_tokens_<task>`, 8192 by default) is only a loose stop line. The write target starts at `window_write_tokens`, shrinks by a quarter after a round that was truncated or took more than half the request timeout, and grows by a tenth after two clean rounds, never above `window_write_tokens` or three quarters of `max_tokens`. The read window follows the write target and the measured output/source ratio, plus a quarter of look-ahead, so little source is read twice. The last stretch goes in one round when it fits.

#### D. Deterministic-first verification

Each round is validated by rules (tags in order and in range, length ratio) before it is kept; a missing end line only drops the last paragraph. The merged text then goes through the `verify` stage, where only hard failures block.

#### E. Fallback instead of replanning

When rounds keep failing on one stretch, the executor processes that stretch as one fixed chunk and moves on. There is no plan-level replanning step.

#### F. Persisted state

Each task's window state lives in the video's work directory: `text/window/` for the first task and `text/window/processed/` for `translate_only`, each with a `state.json` (cursor, outline, sizes, a record of every round) and one `out_NNNNN.md` file per saved paragraph. A rerun of `run` resumes at the cursor as long as the units, the prompt and the state format are unchanged; otherwise that task starts over. Request counts, estimated tokens and per-task coverage go into the video's `state.json`. There is no separate telemetry file and no pause or cancel signal: stopping the process and rerunning is the control, and `--max-seconds` stops cleanly between rounds.

### 7.6 Why this subsystem design fits the whole project

The long-text subsystem is the `text` stage, not a separate product. It consumes the normalized document from `normalize`, follows the tasks chosen by `plan`, uses the same video lock and state file as every other stage, and hands its paragraphs to `merge`. Short videos use it too, as the one-round case, so there is one text path to test and improve.

---

## 8. Key Trade-Offs and Boundaries

### 8.1 Why the project is not "just one big prompt"

Because the project must solve more than generation: source discovery, routing, state, recovery, quality gates, and output packaging. Collapsing all of that into one prompt would make the system fragile and opaque.

### 8.2 Known paths go to code; judgment and exceptions go to the model

A model acting as the orchestrator can, in principle, handle situations nobody planned for. In practice this project's unplanned situations happen at the edges: YouTube refusing a request, yt-dlp changing behavior, an external CLI failing, the user wanting something unusual. The middle of the flow is the same every time. When a model improvises in the middle, it hides bugs (a skipped check looks like success), and its fixes do not accumulate (the next run improvises again).

So `run` owns the middle and stops at the edges with an explicit state and a reason. The rate-limit rule in 6.3 is an example: instead of quietly producing a different kind of article, `run` hands the situation to the model, which can explain it to the user and either wait or accept the fallback with a flag. The model stays in charge outside `run`: it decides what to tell the user, whether to retry, which override to use, and how to batch work. The escape hatches (`--source`, `--mode`, `--from`, `--restart`, and the debug commands) keep that power without letting it leak back into the flow.

### 8.3 Why the model no longer orchestrates step by step

Earlier versions put the flow in `SKILL.md` prose. Section 3.5 lists the result: steps skipped by weaker hosts, a hand-written state file that could disagree with reality, no end-to-end test, and layer after layer added to compensate. Each layer made the instructions longer, which made skipping more likely. Moving the flow into code breaks that loop: a missing step becomes a failing test instead of a silent difference in output.

### 8.4 Why the runtime contract layer was removed

The March runtime layer (policy, evaluator, decision records, recovery hints, ledger, artifact graph, and an external create/inspect/advance API) was meant to give weak models and harnesses a structured contract: the current state, the allowed next actions, and a recommended decision. The intent was sound, but the contract never reached the model: it was printed only with `--api-envelope`, which `SKILL.md` never passed, and nothing else read it. Once `run` owned the flow, the model no longer chose next actions, so the contract had no reader. What the model still needs is the result of a whole run, which section 6.5 gives in a few fields. The parts that did work (the ownership lock, resumable state, usage records) moved into `pipeline` and `state.json`; the rest was deleted in stage 4. The layer's original rationale is in the 2026-03-19 research notes linked from [9.3](#93-design-history).

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

## 9. Implementation Map (Appendix)

### 9.1 Where each part of the design lives

| Design | Code |
|---|---|
| Entry script and commands (6.10) | `yt_transcript_utils.py` → `kernel/cli.py` |
| Stage list, `state.json`, resume, overrides, results and exit codes (6.2, 6.4 to 6.6) | `kernel/pipeline.py`: `STAGES`, `EXIT_CODES`, `_Run.execute`, `_Run.run_stage`, `_Run.invalidate`, `run`, `status`, `cleanup` |
| Locks and several runs at once (6.4) | `kernel/locks.py` (`FileLock`); `video_lock_path` and `run` in `kernel/pipeline.py`; `_Run.youtube`; `kernel/sources/youtube.py` (`lock_path`, `refusal_since`, `record_refusal`) |
| `--max-seconds` and `in_progress` (6.7) | `_Run.deadline_passed` and the `stop_check` that `stage_text` passes to the window executor |
| yt-dlp calls, the update check, `needs_user` reasons (6.2) | `_Run.download`, `_Run.update_yt_dlp`, `kernel/sources/youtube.py` (`classify`); `scripts/download.sh`, `scripts/update_yt_dlp.sh` |
| Language detection and track choice (6.3) | `kernel/sources/subtitle_language.py` (`select_subtitle_candidates`), `_Run.stage_source`, `_Run.subtitle_route` |
| Subtitle quality score and reroute (6.3) | `kernel/text/subtitle_quality.py`, applied in `_Run.subtitle_route` |
| Deepgram (6.3) | `kernel/sources/deepgram.py` (`transcribe_deepgram`), `_Run.transcribe` |
| VTT parsing and subtitle cleanup | `kernel/text/cleanup/subtitle.py`, `cjk.py`, `overlap.py` |
| Normalized document | `kernel/text/normalize.py` |
| Text plan | `kernel/text/plan.py`, `_Run.stage_plan` |
| Long-text subsystem (section 7) | see 7.5 |
| LLM backends (6.8) | `kernel/llm/agent_cli.py`, `kernel/llm/api.py`; the choice in `kernel/text/window.py`; the handoff in `kernel/pipeline.py` (`AgentHandoff`) |
| Quality check (6.9) | `kernel/quality/verify.py`, `kernel/quality/semantic.py` |
| Article assembly (6.9) | `kernel/text/assemble.py` (`assemble_final`, `frontmatter_url`), `_Run.stage_assemble`, `article_path` in `kernel/pipeline.py` |
| Config and defaults | `kernel/config.py`, documented key by key in `config.example.yaml` |
| Host contract (6.13) | `SKILL.md` |
| Tests (6.12) | `tests/`, `scripts/run_regression.py` |

### 9.2 Where the code differs from the plan

The plan this design was implemented from differs from the code in a few places, all deliberate:

- the four module-structure points in 6.11: subtitle quality is in `text`, `download.sh` is still a subprocess, the agent handoff is in `pipeline`, and `config.py` owns every default;
- `subtitle-info` reads yt-dlp metadata on stdin instead of taking a URL, because `download.sh` pipes into it and `run` uses the metadata it already fetched;
- the `text` stage still cuts the source into chunks before the window run, because a chunk is both the boundary layout of the units and the size of the fallback.

### 9.3 Design history

The design was implemented in seven stages, one pull request each, under tracker [#23](https://github.com/williamwang-ty/yt-transcript/issues/23):

| Stage | Change |
|---|---|
| 0 ([#31](https://github.com/williamwang-ty/yt-transcript/pull/31)) | This design document |
| 1 ([#32](https://github.com/williamwang-ty/yt-transcript/pull/32)) | Offline end-to-end tests over recorded fixtures, with a fake yt-dlp, Deepgram and LLM |
| 2 ([#33](https://github.com/williamwang-ty/yt-transcript/pull/33)) | The `run` command and one `state.json` per video, built from the regression script's copy of the flow |
| 3 ([#34](https://github.com/williamwang-ty/yt-transcript/pull/34)) | `SKILL.md` reduced to driving `run`; Quick Mode and `workflows/` removed; the yt-dlp update check moved into `run` |
| 4 ([#35](https://github.com/williamwang-ty/yt-transcript/pull/35)) | Redundant layers deleted: the hand-written `state.md` and its checks, the runtime contract layer (policy, decision, evaluator, recovery, ledger, artifacts, the external API, pause and cancel, telemetry), the fixed-chunk executor with `summarize` and the chapter plan, legacy switches, `preflight.sh` and `cleanup.sh`, and most public commands |
| 5 ([#36](https://github.com/williamwang-ty/yt-transcript/pull/36)) | `yt_transcript_utils.py` (about 8,000 lines) split into the `kernel/` packages with explicit imports |
| 6 | Documentation: this document completed, `system_design_notes/` deleted, README rewritten |

`state.json` replaced the hand-written `state.md`, `machine_state.json`, the execution state in the chunk `manifest.json` (the `text/manifest.json` that remains only lists the chunk files), `telemetry.jsonl`, and the scattered `/tmp/<video_id>_*` files.

Behavior changes compared with the agent-driven flow before stage 3:

1. Short videos' text goes through the LLM backend (normally the host CLI) instead of being written by the host model in the conversation.
2. A refused preferred subtitle track stops the run with `needs_user` instead of silently falling back to another track (6.3).
3. Rerunning a finished video returns the existing article instead of starting over; `--restart` redoes it.
4. The subtitle quality reroute happens inside `source`, before normalization; the outcome is the same.
5. New: `--max-seconds` and the `in_progress` result for hosts with shell time limits.

Subtitle language selection, Deepgram transcription, the window executor's algorithms, the outline and glossary, and the quality checks moved without changing behavior, apart from two output fixes made in stage 3: merged window output no longer joins short paragraphs (which had glued bilingual pairs together), and `structure_only` drops `-` speaker marks.

The design notes written before this refactor were deleted in stage 6, together with the old version of this document. They stay readable at the last commit before stage 6 and in the git history (`git log -- SYSTEM_DESIGN.md system_design_notes/`):

- [Research notes, 2026-03-19](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/system_design_notes/20260319_SYSTEM_DESIGN_RESEARCH_NOTES.md): what kind of architecture the project is, and the case for the "adaptive runtime" that became the runtime contract layer (removed in stage 4, see 8.4).
- [Final deliverable quality plan, 2026-03-25](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/system_design_notes/20260325_SYSTEM_DESIGN_FINAL_DELIVERABLE_QUALITY_PLAN.md): where article quality problems come from, and which belong to code and which to the LLM.
- [Architecture review, 2026-03-30](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/system_design_notes/20260330_SYSTEM_DESIGN_ARCHITECTURE_REVIEW.md): the boundary between scripts and the LLM.
- [This document before stage 6](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/SYSTEM_DESIGN.md): section 9 there maps each old file and command to where it went, stage by stage.

Each stage's reasoning and checks are in its pull request and issue linked in the table above.

---

## 10. One-Sentence Summary

`yt-transcript` is a local transcript production system in which one deterministic command, `run`, owns the known path from YouTube URL to Markdown article, the LLM only transforms text inside it, and the host agent handles the exceptions and the user.

---

## 中文版

## 1. 文档定位

本文说明 **整个 `yt-transcript` 项目的系统设计**：它构建什么，代码和模型怎样分工，以及为什么这样分。

`README.md` 是给使用者的说明（安装、配置、命令），`SKILL.md` 是宿主 agent 读的简短约定。本文两者都不是，它解释结构、边界和取舍。它也不是编码规范，但文中用到的名字（阶段、文件、状态和结果字段、原因代码、参数、配置项）都和代码里一致。

本文分三个层次：

- **项目层**（第 2 到 6 节）：一条命令怎样把 YouTube 链接变成一篇 Markdown 文章，以及外面的 agent 负责什么。
- **子系统层**（第 7 节）：这条命令里最难的阶段，也就是长文本变换，是怎样工作的。
- **实现对照**（第 9 节）：每一部分由哪个模块实现，代码和当初的计划有哪些不同，以及代码是怎样走到现在这个样子的。

**设计历史**：[第 9.3 节](#93-设计历史)列出产生这份设计的重构的每个阶段及其 pull request，并链接到早期的设计笔记和本文的旧版本。它们已从仓库中删除，但从那里仍然可以阅读。

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

旧设计里（第 3 阶段之前，见 [9.3](#93-设计历史)），agent 要读约 370 行的 `SKILL.md` 和三份 workflow 文档，执行二十来条命令，还要手写一个 `state.md` 文件，以此来执行流程。代价是可以预见的：

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
        │ /tmp/yt-transcript/<video_id>/state.json（+ 锁）      │
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

`run` 按固定的阶段列表执行。每个阶段读取前面阶段记下的内容，把产物写进该视频的工作目录，然后才在 `state.json` 里把自己标为 `done`。重跑时，已经 `done` 且记录的产物仍然存在的阶段会被跳过；某个阶段一旦重新执行，它之后的所有阶段也都重新执行。

| 阶段 | 做什么 | 主要产物 | 可能的停止状态 |
|---|---|---|---|
| `metadata` | 在 YouTube 锁里（6.4）：先让 Homebrew 更新 yt-dlp（最多每小时一次），再用 `yt-dlp -J` 取一次视频信息（标题、频道、日期、时长、章节、字幕轨列表、音轨语言）；后面的阶段读这个文件，不再向 YouTube 要这些信息 | `metadata.json`、`chapters.json` | `needs_user`（用 Chrome cookies 重试一次后仍要求机器人验证 `cookies_required`、`login_required`、`rate_limited`、`tool_missing`）、`in_progress`（等 YouTube 用完了 `--max-seconds`）、`failed` |
| `source` | 判断口语语言并选出可信的字幕轨（6.3）；下载并解析成纯文本和带时间的片段；给字幕质量打分，评分规则要求时改用 Deepgram；或者直接用 Deepgram 转写 | `source/`、`raw_text.txt`、`segments.json`、`state.json` 里的路由 | `unsupported`、`needs_user`（首选字幕轨被拒、缺少 Deepgram 密钥，或上面的 yt-dlp 情况）、`failed` |
| `normalize` | 生成规范化文档：清理后的文字、带时间的片段，以及分块时按文字切还是按片段切 | `normalized_document.json` | `failed` |
| `plan` | 按模式选择文字任务（中文：`cleanup_zh`；双语：先 `structure_only` 再 `translate_only`），把文字标为 `short` 或 `long`，并估算规模（输入 token、每个任务预计轮数）。中文的 Deepgram 文字会多一条修复识别错误的说明 | `plan.json` | `failed` |
| `text` | 先把规范化文档切成分块，再用窗口执行器（第 7 节）逐个执行规划好的任务；每个任务单独记账 | `text/` | `needs_agent_text`、`needs_user`（`llm_api_fallback`，6.8）、`in_progress`、`failed` |
| `merge` | 按顺序拼接最后一个任务的段落（第一个任务的段落带上大纲的标题、句子完整，见 7.5 B），再去掉在轮次接缝处重复的标题、段落和段首 | `optimized.md` | `failed` |
| `verify` | 对照原始文字做质量检查 | `quality.json` | `failed`（只有硬失败才停；警告只记录） |
| `assemble` | 写出文章：frontmatter、头部、正文和结尾，文件名为 `yyyy-mm-dd. <标题>.md` | `output_dir` 里的文章 | `needs_user`（`output_dir_missing`）、`failed` |
| `cleanup` | 文章写出后删除中间文件，保留 `state.json` | — | — |

说明：

- **阶段幂等。** 重跑一个未完成的阶段，会把这个阶段从头做一遍；但昂贵的子步骤在阶段内部保留结果（下载好的音频、每段音频的 Deepgram 结果、大纲、已完成的窗口轮次），所以中断的阶段不会重复花钱。
- **没有单独的 preflight。** 每个阶段在真正需要之前才检查它需要的东西：到 `metadata` 才检查 yt-dlp（包括 Homebrew 更新，最多每小时一次，因为 YouTube 经常让旧版 yt-dlp 失效），`source` 选了 Deepgram 才检查 Deepgram 密钥，到 `text` 才检查 LLM 后端，到 `assemble` 才检查 `output_dir`。便宜的路径保持便宜。
- **第一次调用 LLM 之前来源已经定下来。** 字幕质量改道在 `source` 内部完成，所以 `normalize` 及之后的阶段处理的都是已经认可的文字。
- **长短视频一条路径。** 短视频和长视频走同样的阶段。视频时长 30 分钟以上，或者较短视频的文字超过单次请求的上限（按路线不同，约 4100 到 4800 个估算 token），`plan` 就把文字标为 `long`。`long` 的文字把 YouTube 章节作为大纲的提示（按片段切时还会在章节开始处切分块）；`short` 的文字两者都不用。只要一轮能处理完全文就跳过大纲调用（7.4），所以短视频通常每个任务只需要一次请求。
- **免费的路由检查。** `--stage plan` 执行 `metadata` 到 `plan`，不花钱。如果路由需要 Deepgram，就记下这个决定，在 `source` 内部停下，不做转写。

### 6.3 来源选择

`source` 阶段按下面的规则选择（候选字幕轨由 `kernel/sources/subtitle_language.py` 给出）：

1. 用能拿到的最强信号判断口语语言：先看 YouTube 标为原始的音轨，再看元数据的 `language`，再看原始语言的 ASR 轨（只有一种语言有 ASR 轨时），再看人工字幕轨（所有人工字幕是同一种语言时）。这些都没有时，用视频自己的文字（标题、简介、章节名、标签）是哪种文字来推测（`video_text`）：汉字为中文，假名为日语，韩文为韩语。拉丁字母不下结论，因为英语、印尼语、西班牙语分不出来；CJK 字符很少或以拉丁单词为主的文字也不下结论。
2. 只信任人工字幕和口语语言的 ASR 轨，丢弃 YouTube 的机器翻译轨。
3. 任何视频，人工中文字幕都排在第一位；模式 `chinese`。
4. 中文视频：否则用中文 ASR 轨；模式 `chinese`。
5. 英文视频：否则用英文字幕，人工优先；模式 `bilingual`（英文原文加中文翻译）。
6. 其他口语语言：`unsupported`（原因 `unsupported_language`），除非有人工中文字幕。没有任何信号说明语言时，所有可信的中文或英文字幕轨都是候选。
7. 没有可信的字幕轨，或者字幕质量分触发改道规则（`chinese` 路线上质量为 critical）：按检测出的语言用 Deepgram 转写，连视频文字也说明不了语言时才让 Deepgram 自己识别。Deepgram 自动执行，没有确认步骤。Deepgram 识别出的语言不受支持时，同样返回 `unsupported`。

只来自视频文字的语言、或者完全没有信号时，语言都不算确定：这时 `--mode` 直接指定口语语言（`chinese` 为中文音频，`bilingual` 为英文音频），Deepgram 按这个语言转写，路由里记为 `language_signal: mode_override`。这种推测出的语言、以及 Deepgram 自己识别出的语言不受支持时，`unsupported` 的 `message` 会说明可以这样重跑。加视频文字这个信号，是因为没有任何字幕轨、元数据也没写语言的视频，原来只能靠 Deepgram 自己识别，而它把一个普通话视频（DQGrPstFRVI，标题和简介都是中文）识别成了印尼语。没有使用 Deepgram 专有的做法（把识别范围限定在中英文、按置信度定阈值）。

首选字幕轨被 YouTube 拒绝（限流或机器人验证）、后面的字幕轨却下载成功时，`source` 不会悄悄换用下一条字幕轨。旧的 agent 驱动流程会这样做，结果中文文章变成了双语文章，而且每次运行结果可能不一样。现在 `source` 返回 `needs_user`，原因是 `rate_limited`（机器人验证时是 `cookies_required`），列出 `preferred_track` 和 `fallback_track`，并给出 `accept_command`：同一条命令加上 `--mode <备用字幕轨的模式>`。agent 问用户是等一会儿再重跑，还是接受备用字幕轨。这种拒绝只关系到这一个视频。YouTube 拒绝所有字幕轨，或者连页面都拒绝时，它拒绝的是整台机器，这台机器上的所有运行都会停下（6.4 的“多个运行同时进行”）。

`yt-dlp` 仍然是获取来源的依赖。直接调用 YouTube InnerTube 获取字幕评估过：对只需要字幕的场景更轻，但它是非官方、接口敏感的入口，不能取代 Deepgram 兜底，还会把维护提取逻辑的负担转到本项目。它可以作为将来的快速路径，但不是默认方案。

### 6.4 工作目录与状态

每个视频一个工作目录：

```text
/tmp/yt-transcript/<video_id>/
  state.json                 唯一的状态文件；cleanup 后保留
  metadata.json  chapters.json
  source/                    download.sh 下载的 subtitles/ 或 audio/（音频被切开时还有各段音频），
                             subtitle_document.json（字幕质量检查），
                             deepgram.json、deepgram_chunk_NNN.json（每段音频的 Deepgram 结果）
  raw_text.txt  segments.json  normalized_document.json
  plan.json
  text/                      manifest.json 和 chunk_NNN.txt（分块）、glossary.json（cleanup_zh），
                             window/（第一个任务：state.json、每段一个 out_NNNNN.md），
                             window/processed/（translate_only）
  agent_task/                prompt.md、task.json、output.txt、answers/；只在把请求交给 agent 期间存在
  optimized.md  quality.json
```

各视频目录旁边，`/tmp/yt-transcript/.locks/` 放锁文件（每个视频一个 `video-<video_id>.lock`，以及 `youtube.lock`、`articles.lock`）和两份小记录：`youtube_rate_limit.json`（YouTube 最近一次拒绝这台机器）和 `yt-dlp-update.stamp`（最近一次问 Homebrew 的时间）。这些文件都从不删除。

`YT_TRANSCRIPT_WORK_ROOT` 可以代替 `/tmp/yt-transcript`，`YT_TRANSCRIPT_OUTPUT_DIR` 可以代替配置里的 `output_dir`；回归脚本两个都用，所以不会碰到真实的运行目录和笔记库。视频锁跟着工作根目录走。YouTube 锁和那两份记录关系到整台机器，所以不管工作根目录在哪里，都留在 `/tmp/yt-transcript/.locks/`；只有 `YT_TRANSCRIPT_LOCK_DIR` 能改它们的位置，离线测试就是这样做的。

每个阶段开始和结束时、每轮窗口之前，都原子地写一次 `state.json`。下面是一次已完成的双语运行的 `state.json`（有删节）：

```json
{
  "schema": 1,
  "video_id": "abc123",
  "url": "https://www.youtube.com/watch?v=abc123",
  "status": "done",
  "created_at": "2026-10-22T16:59:52.758+00:00",
  "updated_at": "2026-10-22T16:59:53.611+00:00",
  "overrides": {"source": null, "mode": null},
  "metadata": {"title": "Some Talk", "channel": "Some Channel", "upload_date": "20261001", "duration": 437},
  "route": {
    "source_language": "en",
    "language_signal": "original_audio_track",
    "source": "subtitles",
    "language": "en",
    "track": "en",
    "track_kind": "manual",
    "mode": "bilingual",
    "subtitle_quality": {"score": 1.0, "band": "excellent"},
    "blocked_tracks": [],
    "fallback_used": false,
    "rerouted_from": null,
    "reason": "English video with human English subtitles"
  },
  "stages": {
    "metadata": {"status": "done", "started_at": "…", "finished_at": "…", "seconds": 0.2,
                 "outputs": ["metadata.json", "chapters.json"]},
    "plan": {"status": "done", "summary": {"tasks": ["…"], "video_path": "short", "estimated_input_tokens": 1800}},
    "text": {"status": "done", "tasks": {
      "structure_only": {"status": "done", "cursor": 94, "units": 94, "rounds": 1, "round_count": 1,
                         "invalid_rounds": 0, "fallback_rounds": 0,
                         "outline": {"status": "skipped"}, "coverage": {"…": "…"}, "efficiency": {"…": "…"}},
      "translate_only": {"status": "done", "cursor": 27, "units": 27, "rounds": 1}
    }},
    "verify": {"status": "done", "summary": {"passed": true, "hard_failures": [], "warnings": [], "checks": {"…": "…"}}}
  },
  "llm": {"backend": "claude-code-cli", "requests": 2, "input_tokens": 4794, "output_tokens": 4819,
          "estimated_tokens": true},
  "warnings": [],
  "last_result": {"status": "done", "at": "2026-10-22T16:59:53.611+00:00"},
  "output_file": "/Users/me/notes/01.input/01.articles/2026-10-01. Some Talk.md",
  "raw_chars": 6539
}
```

`status` 在调用进行中是 `running`，`--stage plan` 调用之后是 `planned`，其余时候是上一次结果的状态。阶段记录里还保存后面阶段要用的东西：`normalize` 的结果、`plan` 的任务列表（`summary`）、`source` 下载的音频路径。`llm` 里的 token 数是根据 prompt 和输出在本地估算的，因为命令行后端不报告用量。

规则：

- **续跑**就是重跑同一条命令：`run` 读 `state.json`，从第一个不是 `done` 的阶段开始。在 `text` 阶段内部，窗口执行器从游标处继续。
- **完成的就保持完成。** `cleanup` 保留 `state.json`，所以对已完成的视频重跑 `run`，会立即返回 `done` 和已有的文章，前提是这篇文章还在（被删掉了就从头再做）。因此整批重跑时只会处理没完成的视频。要重新生成文章就用 `--restart`。
- **覆盖参数会保留。** `--source` 和 `--mode` 存进 `state.json`，在这个视频之后的重跑里一直有效，直到 `--restart`，所以续跑不会悄悄改变路由。无法满足的覆盖参数（`override_not_applicable`）不会保存，所以直接重跑会回到原来的路由。
- **每次调用都会读配置**，但配置改了，已完成的阶段也不会重做；要重做就用 `--from <阶段>`。`cleanup` 之后中间文件已经删掉，所以对已完成的运行用 `--from` 会重做所有阶段；它是给未完成的运行用的（比如质量检查失败后 `--from text`）。
- **视频锁**防止两个进程同时写一个工作目录：一次调用从开始到结束都持有它，同一个视频的第二次调用会等第一次结束（在自己的 `--max-seconds` 之内），然后从第一次留下的状态继续，通常立即返回 `done`。不同视频之间不共享文件，可以并行运行；只有它们对 YouTube 的请求要排队（见下）。
- **安全**：`video_id` 必须匹配 `[A-Za-z0-9_-]+` 才能用作路径。`state.json` 不存 API 密钥，只记后端名字，不记凭据。
- **寿命**：目录在 `/tmp` 下，重启后未完成的任务会丢失。这是接受的限制。

#### 多个运行同时进行

同一台机器上可以同时有几个 `run` 调用：并行启动的一批视频，或者两个 agent 各跑各的。两把文件锁（`flock`，`kernel/locks.py`）把它们隔开。没有队列、调度器或额外的状态；要等的调用就等着。

- **两把锁，固定顺序。** 视频锁（`<工作根目录>/.locks/video-<id>.lock`）在整个调用期间持有。YouTube 锁（`/tmp/yt-transcript/.locks/youtube.lock`）只在每一步 yt-dlp 期间持有：更新检查连同取元数据、下载字幕、下载音频。调用总是先拿视频锁，所以两个调用不会互相死锁。Deepgram、LLM 和其他阶段都在 YouTube 锁之外运行，所以一个视频的文字处理可以和另一个视频的下载同时进行。
- **同一时间只有一个 yt-dlp。** YouTube 按机器（IP，或 cookies 里的账号）计量请求，而不是按进程；这台机器被“rate-limited by YouTube for up to an hour”，就是因为并行提取。在锁里，每次 yt-dlp 调用还都加上 yt-dlp 自带的 `-t sleep` 预设（它自己在请求之间停顿），所以并行的运行向 YouTube 请求的速度不会超过一个运行，我们也不用自己定节流参数。太旧、没有 `-t sleep` 的 yt-dlp（2025 年年中之前）就不加，并记一行日志。每个视频的页面只在 `metadata` 提取一次（`yt-dlp -J`，保留 stderr，不再逐字段兜底）；下载字幕时用 `--load-info-json` 读这个文件。下载音频仍然传链接，所以 Deepgram 路线会第二次请求页面。
- **子进程也持有锁。** 锁的文件描述符会传给 `download.sh` 和更新脚本，所以 `run` 被杀掉而它的 yt-dlp 还在跑时，YouTube 锁会一直保持到那个 yt-dlp 结束。
- **没有残留的锁。** 进程死掉时操作系统会释放它的锁，所以不需要检测或接管死掉的持有者。锁文件从不删除（有调用在等一个锁文件时把它删掉，会让两个持有者同时进去）；每个锁文件里记着持有者的进程号、开始时间和视频 id，用在提示信息里。
- **等锁算进 `--max-seconds`。** 锁还没空出来预算就用完时，调用返回 `in_progress`，说明谁在占用，它的 `next_command` 会接着等。没有 `--max-seconds` 时一直等下去。所以同一视频的调用再也不会以 `locked` 失败。
- **一次拒绝让已经在跑的运行都停下。** YouTube 拒绝整台机器时，这次调用把时间和 yt-dlp 的原话写进 `/tmp/yt-transcript/.locks/youtube_rate_limit.json`。每个调用拿到 YouTube 锁后马上检查这份记录：在拒绝之前就开始的调用返回 `needs_user`（`rate_limited`，机器人验证时是 `cookies_required`），并引用 yt-dlp 的原话，不再请求 YouTube；在拒绝之后才开始的调用会试一次。所以一批视频会在第一次被拒时停下，而不是继续给 YouTube 发请求；用户同意后重跑，就会再试。没有需要清除的东西，也没有我们自己定的冷却时间：限流长什么样由 yt-dlp 识别，什么时候结束由 YouTube 决定。
- **什么算拒绝整台机器**：yt-dlp 的限流消息（HTTP 429、“try again later”、“rate-limited by YouTube”），以及用了 Chrome cookies 仍没通过的机器人验证。另一条字幕轨能下载时某条字幕轨被拒（6.3），以及视频需要登录账号（`login_required`），都只关系到这个视频，不会记录。
- **只有一个分类器。** 只有 `kernel/sources/youtube.py` 读 yt-dlp 的错误文字；它识别出 `rate_limited`、`bot_check` 或 `login_required`，其他情况都是可重试的 `download_error`。`download.sh` 每个 yt-dlp 调用只执行一次，把错误原样交出来：不重试，也不读错误。
- **cookies 只用于机器人验证。** 没配 cookies 时遇到机器人验证，用 Chrome 的 cookies 重试一次。限流不重试，因为再发请求只会让限流更久。
- **yt-dlp 更新**在 YouTube 锁里进行，所以 Homebrew 替换 yt-dlp 时不会有 yt-dlp 在跑。整台机器最多每小时问一次 Homebrew，不管结果如何都记下时间。只升级 Homebrew 管理的 yt-dlp；其他方式装的只报告，也不会用 pip 安装。
- **`cleanup`** 删除前先拿视频锁，所以绝不会删掉正在运行的视频目录。

### 6.5 交回给 agent 的结果

每次 `run` 调用都在 stdout 打印且只打印一个 JSON 对象，并以固定的退出码结束。进度信息（阶段、窗口轮次、yt-dlp 的消息）输出到 stderr。

| `status` | 退出码 | 含义和字段 | agent 怎么做 |
|---|---|---|---|
| `done` | 0 | 文章已写出：`output_file`、`title`、`route`、`warnings`、`llm`。`--stage plan` 之后：`stage`、`route`、`plan`，路由需要 Deepgram 时还有 `message` | 汇报结果 |
| `failed` | 1 | 某个阶段失败：`stage`、`reason`、`message`、`retryable`（质量检查失败时还有 `hard_failures`） | `retryable` 时重跑一次 `next_command`，否则汇报；可以建议某个覆盖参数 |
| `needs_user` | 3 | 只有用户能解决。`reason` 是 `cookies_required`、`login_required`、`rate_limited`、`deepgram_key_missing`、`tool_missing`、`output_dir_missing` 或 `llm_api_fallback`；`message` 说明该怎么做。首选字幕轨被拒时还有 `preferred_track`、`fallback_track` 和 `accept_command`。`llm_api_fallback`（6.8）还有 `choices`，每项带 `answer`、`label` 和 `command` | 告诉用户；用户处理后重跑，用户接受备用字幕轨时执行 `accept_command`，或执行用户所选那一项的 `command` |
| `unsupported` | 4 | 口语语言不受支持：`reason` 为 `unsupported_language`，还有 `language` | 汇报；不重试。`message` 建议了 `--mode`（语言是从标题推测的或由 Deepgram 自己识别的），而用户说视频其实是中文或英文时，用对应的 `--mode` 重跑 |
| `needs_agent_text` | 5 | 没有可用的 LLM 后端，交出一次请求：`task.name`、`task.prompt_file`、`task.output_file` | 完成请求，把答案写进输出文件，重跑 `next_command` |
| `in_progress` | 6 | `--max-seconds` 时间用完，在安全点停下：`stage`、`progress`（`stages_done`、`text_tasks`）。等同一视频的另一个运行或等 YouTube 时预算用完，也返回它（6.4），`message` 说明谁在占用 | 重跑 `next_command`，直到状态改变 |

每个结果还有 `video_id`、`work_dir` 和 `seconds`。除了 `done` 和 `unsupported`，每个结果都有 `next_command`，也就是继续这次运行的命令；`--stage plan` 调用的结果也都有。退出码 2 留给命令行用法错误（`{"status": "usage_error", "message": …}`），比如参数不是 YouTube 链接。

`failed` 的原因：`download_error`、`deepgram_error`、`llm_error` 和 `llm_timeout` 可以重试；`source_error`、`normalize_error`、`plan_error`、`merge_error`、`quality_hard_failure`、`override_not_applicable` 和 `config_error` 不能重试，`<阶段>_error`（某个阶段里出现意外异常）也不能重试。

一个 `done` 结果：

```json
{
  "status": "done",
  "video_id": "abc123",
  "output_file": "/Users/me/notes/01.input/01.articles/2026-10-01. Some Talk.md",
  "title": "Some Talk",
  "route": {"source": "subtitles", "language": "en", "track": "en", "track_kind": "manual", "mode": "bilingual"},
  "warnings": ["Size ratio 4.30x vs raw text is outside expected range (1.2-4.0x for bilingual)"],
  "llm": {"backend": "claude-code-cli", "requests": 9, "input_tokens": 41200, "output_tokens": 18700,
          "estimated_tokens": true},
  "work_dir": "/tmp/yt-transcript/abc123",
  "seconds": 412.3
}
```

`needs_agent_text` 结果给出一个 prompt 文件（完整的请求，包括编号单元）和一个输出文件：

```json
{
  "status": "needs_agent_text",
  "video_id": "abc123",
  "stage": "text",
  "reason": "no_llm_backend",
  "message": "No LLM backend is reachable (…). Answer the request in task.prompt_file exactly as the model would, write only the answer to task.output_file, then run next_command.",
  "task": {
    "name": "structure_only",
    "prompt_file": "/tmp/yt-transcript/abc123/agent_task/prompt.md",
    "output_file": "/tmp/yt-transcript/abc123/agent_task/output.txt"
  },
  "next_command": "python3 /path/to/yt-transcript/yt_transcript_utils.py run https://www.youtube.com/watch?v=abc123",
  "work_dir": "/tmp/yt-transcript/abc123",
  "seconds": 1.4
}
```

这个任务恰好是一次 LLM 请求：一次大纲请求、一轮窗口或一个兜底分块，prompt、编号单元和结束标记都和 LLM 后端收到的一样。重跑时，`run` 把答案移到 `agent_task/answers/<prompt 的 sha256>.txt`，遇到同一个 prompt 时交回这个答案，并用校验任何响应的同一套规则（第 7 节）校验它；答案一直保留到 `text` 阶段结束。短视频大约每个文字任务交一次；长视频要交好几次。所以 agent 就是最慢的一种 LLM 后端，不需要单独维护一条文字处理路径。它只是兜底：正常情况下 `auto` 会用上宿主 CLI，`llm_backend: api` 则用 API。

### 6.6 覆盖参数

| 参数 | 重跑时保留 | 作用 |
|---|---|---|
| `--source subtitles\|deepgram` | 是 | 强制来源。`deepgram` 跳过字幕；`subtitles` 即使质量分会触发改道也坚持用选中的字幕轨。从 `source` 起重做后面所有阶段。 |
| `--mode chinese\|bilingual` | 是 | 强制输出形式。模式跟着来源文字走（中文文字对应 `chinese`，英文文字对应 `bilingual`），所以这个参数实际上决定用哪条字幕轨：英文视频上，`bilingual` 即使有人工中文字幕也用英文字幕，`chinese` 用人工中文字幕。没有字幕轨或元数据说明口语语言时（6.3），它也替 Deepgram 指定语言：`chinese` 为中文音频，`bilingual` 为英文音频。从 `source` 起重做后面所有阶段。 |
| `--from <阶段>` | 否 | 把该阶段及之后的阶段标为未完成，然后运行。用于恢复和调试。 |
| `--stage plan` | 写进 `next_command` | 做完 `plan` 就停：免费的路由检查。 |
| `--max-seconds <n>` | 写进 `next_command` | n 秒后、且本次调用至少完成了一个阶段或一轮窗口时，不再开始新的阶段或新一轮窗口，返回 `in_progress`。 |
| `--llm-api once\|always\|no\|never` | 是 | 回答这个视频的 `llm_api_fallback` 问题（6.8）；`always` 和 `never` 还会把回答记进 `config.yaml`。不重做任何阶段。 |
| `--restart` | 否 | 删除工作目录（包括保存的覆盖参数），从头开始。 |
| `--config-path <文件>` | 写进 `next_command` | 读取另一个 `config.yaml`，而不是 skill 根目录下的那个。 |

这些是 agent 的逃生口。用户说"这个用 Deepgram"或"只要中文"，对应的是一个参数，而不是手动执行几步。无法满足的组合（中文视频上用 `--mode bilingual`，没有人工中文字幕的英文视频上用 `--mode chinese`，没有可信字幕轨时用 `--source subtitles`）返回 `failed`，原因是 `override_not_applicable`；不做任何猜测。

### 6.7 长任务和宿主的时间上限

长视频可能要跑一刻钟甚至更久，而很多宿主几分钟后就会结束 shell 命令。下面两点让任何宿主都能安全运行：

- `run` 随时可以被杀掉。每个完成的阶段和每轮窗口都已经落盘，被杀掉的进程的锁由操作系统释放，重跑最多损失正在进行的那一次请求。
- `--max-seconds <n>` 让 agent 不必被动挨杀：`run` 在 n 秒后的第一个阶段或轮次边界停下，返回 `in_progress` 和目前的进度。SKILL.md 让 agent 带上 `--max-seconds 300`，并把 shell 超时设为 10 分钟（shell 上限更短时传大约一半的上限值），然后一直重跑到状态改变。

这不需要后台任务之类宿主特有的能力。

### 6.8 LLM 后端

`text` 阶段的每个文字请求都经过窗口执行器里的同一个请求函数，背后有三种后端：

- **宿主 CLI**：宿主 agent 自己的命令行，来自内置表（Claude Code `claude -p`、Codex `codex exec`、opencode、pi、Gemini CLI、Cursor `cursor-agent`），也可以是 `llm_agent_cli` 或环境变量 `YT_TRANSCRIPT_AGENT_CLI` 指定的任意非交互命令。它在空的临时目录里运行，命令行有对应参数时关闭工具（Claude Code、pi）。
- **API**：配置里的 OpenAI 兼容接口或 Anthropic Messages 接口（`llm_api_format`、`llm_base_url`、`llm_model`、`llm_api_key`），带重试、退避和可选的流式响应。
- **交给 agent**：上面的 `needs_agent_text` 结果。

`llm_backend: auto`（默认）时，每个任务在它的窗口运行开始时选后端：先看明确指定的命令（先环境变量，再 `llm_agent_cli`），再看同一任务之前的调用用过的 CLI，最后根据环境变量识别宿主。CLI 要在 `llm_cli_probe_timeout_sec` 内答出一次一行的试调才会被使用。CLI 请求失败时，按 API 那条路同样的指数退避（`llm_backoff_sec`）重试 `llm_cli_max_retries` 次。`llm_backend: api` 用 API，并跳过 CLI。`state.json` 里把后端记为 `<agent>-cli`、`api` 或 `agent-handoff`。

`auto` 模式下 CLI 仍可能用不了：没找到、试调失败，或者某个请求重试后仍然失败。配了 API 时，这时怎么办由 `llm_api_fallback` 决定。API 按请求收费，而且并行的几个视频会一起切过去，所以默认是问用户，而不是替用户选：

| `llm_api_fallback` | 没有可用的 CLI | CLI 请求一直失败 |
|---|---|---|
| `ask`（默认） | `needs_user`，原因 `llm_api_fallback`，带四个 `choices`（见下） | 同样，停在那一轮 |
| `auto` | 所有请求交给 API | 剩下的部分交给 API，并给出警告 |
| `never` | 交给 agent | 可重试的 `llm_error`，重跑时从那一轮继续 |

没配 API 时，行为和 `never` 一样。问题列出四个回答，每个都带着自己的命令（`next_command` 加上 `--llm-api <回答>`）：`once`（这个视频用 API）、`always`（用 API，并把 `llm_api_fallback: auto` 记进 `config.yaml`）、`no`（这个视频不用）和 `never`（这个视频不用，并记下 `llm_api_fallback: never`）。这个视频的回答像覆盖参数一样存进 `state.json`，直到 `--restart`。记进 `config.yaml` 时只改这一行，并保留文件权限，因为这个文件里有 API 密钥。问一次最多让每个视频停一次，批量时只问一次（SKILL.md）；另一种做法是悄悄切换，以前付费 API 的费用就是这样在没人决定的情况下涨上去的。

不依赖任何提供方的特殊请求字段。`max_tokens` 只作为宽松的上限发送；截断通过窗口协议的结束行检测，没有 `finish_reason` 的命令行输出也适用。报告了 `finish_reason` 为 `length`（或提供方的同义值）时，只会让后面的轮次少写一些。

### 6.9 质量检查与组装

- `verify` 检查结构、截断迹象、相对原文的长度比、重复短语和接缝重复、中文可读性、双语配对、术语漂移、semantic anchors（日期、数字、版本号、URL）以及窗口运行的覆盖率。只有硬失败会让运行停下（`failed`，阶段 `verify`，原因 `quality_hard_failure`），这时不写文章：文件不存在或为空、超过 1200 个字符却没有一个 `##` 标题、双语正文里没有一组"英文段后接中文段"的配对。其余都是警告，存进 `state.json`，随 `done` 一起返回，由 agent 提及。另外它只记录、不警告：最长段落的长度、超过 prompt 分段点的段落数（中文 500 字；双语输出按英文段落，250 词）、段内含单个换行的段落数，以及有多少个 `##` 标题前面那段没有说完（标题把一句话切开了，也就是 7.5 B 的放置没能处理的情况，比如前一段以破折号结尾）。
- `assemble` 计算以前要从状态里手工拼出来的所有东西：文件名 `yyyy-mm-dd. <标题>.md`（替换非法字符，标题为空时用视频 id，日期从不使用原始的 `yyyymmdd`；同名文件里是别的视频的文章时，按它 frontmatter 的 `url` 判断，在名字后加 ` (<video_id>)`，所以同一视频重跑覆盖自己的旧文章，别的文件都不会被覆盖）、值都加引号的 YAML frontmatter（`title`、`source`、`url`、`channel`、`author`、`date`、`created`、`type`、`bilingual`、`duration`、`transcript_source`、`tags`、`via`）、包含标题、频道链接、语言模式和时长的头部，以及注明转录来源的一行结尾。双语排版本身（每个英文段后面跟它的中文译文）由 `translate_only` 产生。选文件名和写文件在一把整台机器共用的锁（YouTube 锁旁边的 `articles.lock`）里完成，所以两个同名视频同时写完时不会都占用那个空名字；文件先写到同目录下以点开头的临时文件，再改名，笔记软件不会读到半篇文章。
- `cleanup` 只在文章存在之后运行。失败或中断的运行保留整个目录，便于续跑或检查。

### 6.10 对外命令

所有命令的形式都是 `python3 <skill-root>/yt_transcript_utils.py <命令>`。

| 命令 | 用途 |
|---|---|
| `run <url> [参数]` | 整个流程（6.2 到 6.9 节）；`<url>` 也可以是 11 个字符的视频 id |
| `status [<video_id>]` | 不带 id：列出工作根目录下的所有运行，分为 `unfinished`（各带 `next_stage`、`last_result` 和 `next_command`）和 `finished`。带 id：该视频的条目加上完整的 `state.json` |
| `cleanup <video_id> [--force]` | 删除一个工作目录；正在运行的拒绝，未完成的运行不带 `--force` 也拒绝 |
| `subtitle-info` | 调试用：从 stdin 读 `yt-dlp -J` 的元数据，输出语言判断和候选字幕轨；`download.sh` 也用它 |
| `verify-quality <文件> [--raw-text <文件>] [--bilingual] [--work-dir <目录>]` | 调试用：对任意文件重跑质量检查 |

其余都是内部函数。暂停、取消、telemetry 查询和对外 runtime API 已经删除：中断进程就停下，重跑就续跑，耗时和用量记在 `state.json` 里。

### 6.11 模块结构

```text
yt_transcript_utils.py    skill 根目录下的入口脚本；调用 kernel.cli.main()
kernel/
  cli.py                  参数解析、五个命令、JSON 输出和退出码
  pipeline.py             阶段列表、state.json、两把锁、续跑、覆盖参数、结果；
                          调用 download.sh；交给 agent；status 和 cleanup
  locks.py                flock 原语：有上限的等待、持有者信息、从不删除的锁文件；整台机器共用的锁目录
  sources/
    youtube.py            整台机器共用的 YouTube 锁路径、yt-dlp 错误分类器、拒绝记录
    subtitle_language.py  口语语言、可信字幕轨、候选字幕轨
    deepgram.py           切分音频、带重试和分段缓存的 Deepgram API 调用、解析结果
  text/
    cleanup/              VTT 解析和字幕清理、CJK 空格、重叠裁剪、合并后清理
    normalize.py          规范化文档
    subtitle_quality.py   字幕质量分和改道信号
    plan.py               文字任务、short 或 long、规模估算
    chunking.py           窗口运行读取的分块
    window.py             滑动窗口执行器，以及每个任务的后端选择
    outline.py            全局大纲、章节提示、每轮的术语
    structure.py          按写出文字的句子边界放标题、收段落
    glossary.py           转录术语表和漂移检查
    prompting.py  measure.py  merge.py  assemble.py
  llm/
    api.py                OpenAI 和 Anthropic 格式 API 的 HTTP 客户端
    agent_cli.py          宿主 CLI 表、识别、试调、调用
  quality/
    verify.py  semantic.py   verify 阶段的各项检查
  config.py               解析 config.yaml，定义每个配置项的默认值
  files.py                原子写入、时间戳
scripts/download.sh       yt-dlp：metadata、subtitle-info、subtitles、audio（作为子进程运行；每样只调用一次）
scripts/update_yt_dlp.sh  每小时最多一次的 `brew upgrade yt-dlp`
prompts/*.md              任务 prompt：cleanup_zh、structure_only、translate_only
```

依赖只朝一个方向。`config`、`files` 和 `locks` 不引用 kernel 里的其他模块；`llm` 只用 `config`，`sources` 只用 `config`、`files` 和 `locks`；`text` 用 `config`、`files` 和 `llm`；`quality` 用 `text`；`pipeline` 用上面所有模块；`cli` 用 `pipeline`，调试命令另外用 `sources` 和 `quality`。`kernel/` 里没有任何模块引用 `yt_transcript_utils.py`。唯一的回路跨越进程：`pipeline` 启动 `download.sh`，`download.sh` 再把元数据交给 `subtitle-info` 命令。

拆分之前（重构第 0 阶段）写下的模块计划和结果有四处不同，原因如下：

- **字幕质量放在 `text/subtitle_quality.py`，不在 `sources`。** 它给清理后的字幕文字打分，`normalize` 和 `source` 阶段都要用；放在 `sources` 会让 `text` 依赖 `sources`。`source` 阶段通过 `pipeline` 调用它。
- **`download.sh` 仍然作为子进程运行**，没有并入 `sources`。它是约 420 行的 shell，负责拼 yt-dlp 命令（设置、cookies、`-t sleep`、按顺序的候选字幕轨、选择音频格式），每个命令只执行一次，再输出 JSON；重试和错误处理的策略在 Python 里（`sources/youtube.py`、`_Run.download`）。把剩下的部分也移植过来有改变行为的风险，用户却得不到任何好处。`pipeline` 通过环境变量把路径和 yt-dlp 设置传给它，并把持有的锁描述符一起传过去。
- **交给 agent 的逻辑留在 `pipeline`**（`AgentHandoff`），不在 `llm`。它属于这次运行，而不是某个后端：它往工作目录里写文件，并以 `needs_agent_text` 结果结束本次调用。窗口执行器把它作为回调接收，和其他后端一样对待。
- **默认值都归 `config.py`。** 它定义每个配置项的默认值，`load_config` 返回的结果里每个键都已填好，所以读取配置的模块拿到的总是完整的值，不再各自保留默认值。

### 6.12 测试

- **单元测试**按领域放在 `tests/regression_*.py`，用 `python3 -m unittest tests.test_regressions` 全部运行（离线，不到一分钟）。被删除代码的测试已随代码一起删除。
- **端到端测试**（`tests/regression_e2e.py`）通过 `scripts/run_regression.py --fixtures tests/fixtures/e2e` 离线跑整个流程：`PATH` 上的假 `yt-dlp`（`tests/e2e/`）提供录好的元数据和字幕，假 Deepgram 用录好的响应回答，假 LLM 命令行（`tests/fake_llm.py`，通过 `llm_agent_cli` 接入）实现窗口协议和大纲 JSON。用例：中文人工字幕（长视频，带大纲的多轮窗口）、英文字幕（双语）、无字幕（Deepgram）、不支持的语言。路由和产物与 `tests/fixtures/e2e/baseline.json` 对比；有意的改动之后用 `UPDATE_E2E_BASELINE=1` 重新生成。四个用例同时运行，假 `yt-dlp` 记下每次调用，所以这个测试还检查 yt-dlp 从没有两个同时在跑，以及每个视频的页面只取了一次（Deepgram 路线两次）。`tests/regression_run.py` 覆盖 `text` 阶段内的续跑、覆盖参数的保留和拒绝、`--max-seconds`、`--from`、`--restart`、`--stage plan`、质量改道，以及每一种结果状态。`tests/regression_concurrency.py` 用真实的进程测锁：八个进程抢一把锁从不重叠，持有者被杀掉后锁被释放，有上限的等待返回带持有者的 `in_progress`，`cleanup` 拒绝删除正在运行的视频，限流让排队的运行不再调用 yt-dlp，而之后开始的运行会试一次。
- **真实视频回归**：任何动到主路径的改动合并前，在能访问 YouTube 的机器上跑 `scripts/run_regression.py`（它调用 `run`；见 `tests/live_regression/README.md`）。`--stage plan` 和 `--no-deepgram` 可以让它免费或很便宜。

### 6.13 SKILL.md 的内容

约 80 行：执行 `run <url> --max-seconds 300`；按 6.5 的表处理每种结果状态；把用户的要求换成覆盖参数；多个链接时每个链接调用一次 `run`，只在会影响所有视频的结果（`cookies_required`、`rate_limited`、`tool_missing`、`output_dir_missing`）上停下整批，最后用表格汇总；用 `status` 找中断的运行。没有状态文件格式，没有步骤清单，没有 Quick Mode。

---

## 7. 核心子系统设计：长文本变换

前面几节讲的是整个项目；这一节放大看 `text` 阶段，它把规范化文档变成文章正文。

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

滑动窗口执行器（`kernel/text/window.py`）是唯一的执行器。它执行 `cleanup_zh`，或者双语的 `structure_only` → `translate_only` 两步，第二步给第一步写出的段落编号。它取代了旧的固定分块执行器，后者需要一堆补丁（continuity 上下文、autotune、replan）来掩盖接缝。固定分块只剩两个用处：作为窗口运行输入的切分方式，以及窗口对某一段的兜底（见下文）。

段落只由 `cleanup_zh` 和 `structure_only` 写出；`translate_only` 保留英文段落，把每段的译文放在它下面。这两个 prompt 带同一条分段规则：一段只讲一件事（一个观点、一个例子、一个步骤、一个人的一次发言）；换说话人或新的一节一定另起一段，话题、例子或场景转换通常另起一段，停顿和一句过渡话不分段；长度只作参考（中文 100 到 300 字，英文 2 到 6 句、约 50 到 150 词），超过约 500 字或 250 词时，有意思转折就在那里分开。程序不按长度切分或拒收段落，`verify` 只记录最长的段落（6.9）。

#### 分层理解长文本子系统词汇表

##### 第 1 层：源文本整形

- **规范化文档（normalized document）**
  - `normalize` 阶段生成：清理后的文字、带时间的片段，以及 `preferred_chunk_source`（`chinese` 路线上的字幕为 `text`，其余为 `segments`）；片段的时间告诉执行器哪里有停顿
- **分块（chunk）**
  - 第一轮之前，`text` 阶段把规范化文档切成约 `chunk_tokens_<task>` 个 token 的分块（`kernel/text/chunking.py`，列在 `text/manifest.json` 里）：按文字切时在句末切，按片段切时在字幕条之间切，计划判定为 `long` 时还会在每个 YouTube 章节开始处切。分块边界一定也是单元边界。按文字切（`chinese` 路线上的字幕）时不在章节处切，所以章节可能从一个单元的中间开始。
- **单元 / `[S<n>]`**
  - 一段编号的原文，原文有标点时就是一句话。超过约 120 token 的句子（没有标点的 ASR 文字里很常见）在字幕条结束处切开，优先最长的停顿；字幕对不上文字时每约 120 token 切一次。所以在没有标点的中文字幕里，单元常常在句子中间开始和结束。因此单元只是"读到哪儿了"的位置，不是句子：标题和段落结尾落在哪里，要等模型加好标点、句子边界出现以后，在写出的文字上决定（7.5 B）。`translate_only` 里一个单元是 `structure_only` 输出的一个段落，超过约 600 token 才拆开。

##### 第 2 层：文档级指导

- **全局大纲**
  - 用于 `cleanup_zh` 和 `structure_only`（`window_outline: true`）：第一轮之前对带编号的全文做一次调用（`kernel/text/outline.py`），全文超过 `window_outline_read_tokens` 时分几部分做。它返回一份术语表和各节：标题、起始单元（检查是否在范围内且递增），以及这一节开头那句话的头几个字。程序在给出的单元前后两个单元内查找这几个字；找到了，这一节就移到包含它们的单元，并把从原文照抄的这几个字作为锚点；标题放在写出的文字里包含锚点的那句话前面（7.5 B）。只靠单元编号不够，因为没有标点的字幕切出来的单元会从句子中间开始。一轮就能处理完全文时跳过大纲，因为那一轮能看到全部内容。计划判定为 `long` 时，YouTube 章节通过字幕时间映射到单元，以章节第一条字幕的原文为锚点（在最后一条字幕之后才开始的章节不用），作为提示交给模型；离章节最近的那一节沿用章节的单元、锚点和标题，除非标题是占位标题或不是输出语言（`cleanup_zh` 里的英文章节标题保留模型的译文）。双语运行里每个标题是一行英文 `##` 加一行中文 `##`。
- **术语（glossary）**
  - 人名、术语的正确写法和见到的识别错误。有大纲时，每轮拿到大纲术语中在本窗口出现的那些，`translate_only` 还会拿到它们的中文译法。没有大纲时，`cleanup_zh` 的各轮使用 `text/glossary.json`：一份本地生成（不用 LLM）的转录术语表，来源是分块、标题、频道和章节标题；`verify` 也用这份文件检查最终文字。

##### 第 3 层：轮次执行与控制

- **游标（cursor）**
  - 第一个还没写出的单元；它之前的内容都已存为完整段落
- **读窗口 / 写目标**
  - 一轮从游标读多少原文、输出大约写多少；两者每轮自适应，上限分别是 `window_read_tokens` 和 `window_write_tokens`
- **轮次（round）**
  - 一次模型调用：读窗口 + 已写内容的结尾（`window_tail_tokens`）+ 附近的大纲行 + 匹配的术语；模型把每个单元的 `[S<n>]` 编号抄在该单元文字前面，最后输出 `<!--END-->`
- **兜底（fallback）**
  - 同一个游标处连续无效的轮次超过 `window_marker_retries` 次后，把这一段按一个约 `chunk_tokens_<task>` token 的普通固定分块处理一次，保证任务继续推进
- **占有（ownership）**
  - 就是 `run` 在整个调用期间持有的视频锁（6.4）；执行器自己没有锁

##### 第 4 层：校验、合并与最终输出

- **轮次校验**
  - 编号存在、在窗口范围内且不倒退；输出/原文比例在预期比例的 0.4 到 2.5 倍之间（一轮覆盖至少 200 个原文 token 时才检查）
- **覆盖率 / 效率**
  - 哪些单元已写出，哪些段落明显短于本次运行的典型比例，多少原文被重复阅读；按任务记在 `state.json` 里
- **合并（merge）**
  - 把最后一个任务的段落按顺序拼接，然后用确定性的合并后清理去掉重复的标题、段落和段首。第一个任务的段落先经过 `kernel/text/structure.py`：加上大纲的标题，并修好停在句子中间的段落（7.5 B）；双语运行里这一步发生在 `translate_only` 给段落编号之前
- **质量门禁**
  - 即 `verify` 阶段（6.9）

#### 精简版长文本信息流

```text
normalized_document.json
        |
        v
分块（text/manifest.json、chunk_NNN.txt）
        |
        v
编号单元 [S1..Sn]
        |
        v
全局大纲 + 术语（只做一次，保存在窗口状态里）
        |
        v
一轮：从游标读窗口 + 已写结尾 + 大纲行 + 术语
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
拼接段落：标题放在包含锚点的那句话前面，没说完的句子接回去
        |
        v
merge 阶段 -> verify 阶段 -> assemble 阶段
```

响应在 `<!--END-->` 之前被截断时，只丢掉最后一段：游标移到已保留内容之后，截断只是少推进一点，不会让文档出错。

### 7.5 长文本子系统的架构设计

#### 实现视图

| 事项 | 代码 |
|---|---|
| 分块 | `kernel/text/chunking.py`（`chunk_document`），由 `text` 阶段调用一次 |
| 单元、轮次、大小调整、校验、兜底、覆盖率 | `kernel/text/window.py`（`process_window`、`run_window_rounds`、`parse_window_output`、`plan_round`、`update_sizing`） |
| 每个任务的后端选择；CLI 用不了时怎么办（`llm_api_fallback`） | `process_window` 里的请求函数，底层是 `kernel/llm/agent_cli.py` 和 `kernel/llm/api.py`；问题和回答在 `_Run.llm_api_question` 和 `_Run.answer_llm_api_question` |
| 大纲、章节提示、每轮的术语 | `kernel/text/outline.py` |
| 按句子边界放标题、收段落 | `kernel/text/structure.py`（`structure_paragraphs`），由 `window.py` 的 `raw_stage_text` 调用 |
| 转录术语表和漂移检查 | `kernel/text/glossary.py` |
| 组装 prompt | `kernel/text/prompting.py` 和 `prompts/*.md` |
| 合并和接缝清理 | `kernel/text/merge.py`、`kernel/text/cleanup/post_merge.py` |
| 每个任务调用一次执行器、时间预算、交给 agent、把进度和用量记进 `state.json` | `kernel/pipeline.py`（`stage_text`、`AgentHandoff`、`llm_usage`） |

#### A. 边界由程序决定，而不是模型

模型从不决定自己的输出从原文哪里开始、到哪里结束。它只照抄给它的单元编号，程序据此推算每段覆盖的范围、检查顺序，并决定保留什么。因此合并只是简单拼接。原文没有标点时，程序唯一读不出来的边界是句子在哪里结束；标题和段落结尾因此要等模型加好标点以后再定（B）。

#### B. 结构只决定一次，放在句子边界上

顺序是先有标点，再定结构。没有标点的字幕，要等清理的那一轮把标点写出来，才知道句子在哪里结束。一轮在自己的输出里分段，本来就落在句末，因为模型写的时候看得到句子。在这之前按单元定下的边界只有两种：一轮在哪里停（总是某个单元的末尾），一节从哪里开始（一个单元加上开头那几个字）。所以这两种边界都在各轮写完以后，在写出的文字上决定。

有大纲时，标题只来自大纲，由程序加上，各轮不写标题。每一轮会拿到在本窗口开始的每一节的起始单元和开头那几个字，在这几个字所在的那句话开头另起一段。标题放在哪里，要等各轮写完以后再定：没有标点的字幕切出来的单元是按字幕停顿切的，把标题放在单元上，或者让模型在那里写标题，都会把句子切开。程序在这一节起始单元附近的段落里找它的锚点（只比较字母和数字；整段锚点找不到时，再找它的前六个或后六个字符），把标题放在包含锚点的那句话前面，必要时在那里把段落拆开。找不到锚点时，标题放在两段之间，即第一个中点到达起始单元的段落前面。

同一步里，停在句子中间的段落（多半是一轮结束的地方）会从下一段拿回这句话剩下的部分，或者把没说完的这句交给下一段，看哪一块短，最长约 150 个字；紧跟在这种段落后面、由某一轮写出的标题，会挪到这句话前面。以破折号或冒号结尾的段落算作说完了；一个句末标点都没有的文字保持原样，因为看不出句子在哪里。这些计数记在任务进度的 `structure` 里。没有大纲时（全文一轮就能写完、`window_outline: false` 或大纲失败），各轮自己放标题，只做接句子这一步。

最初的做法是让模型在包含锚点的那句话前面写出标题；模型仍会不时在单元编号处把句子切开，有一次还加了破折号掩盖断口，所以改由程序来放。

也考虑过在各轮之前先单独打一遍标点，让每个单元都是完整的句子，但没有这样做。清理本来就要把全文重写一遍（加标点、去口头禅、改识别错误），单独打标点等于再把全文写一遍，输出 token 和耗时差不多翻倍；而它只对上面那两种边界有用，这两种边界现在已由程序在各轮之后放到句末。

#### C. 大小是软目标

`max_tokens`（`max_output_tokens_<task>`，默认 8192）只是宽松的停止线。写目标从 `window_write_tokens` 开始；某轮被截断或用时超过请求超时的一半，就缩小四分之一；连续两轮正常，就增大十分之一，但不超过 `window_write_tokens`，也不超过 `max_tokens` 的四分之三。读窗口跟着写目标和实测的输出/原文比例走，再加四分之一的余量，尽量少重复读原文。剩下的原文一轮能写完时就一次发完。

#### D. 优先确定性校验

每轮在保留之前先用规则校验（编号顺序和范围、长度比）；缺少结束行只会丢掉最后一段。合并后的文字再经过 `verify` 阶段，只有硬失败会阻断。

#### E. 用兜底代替 replan

某一段连续多轮失败时，执行器把这一段按一个固定分块处理，然后继续。没有计划级的 replan 步骤。

#### F. 持久化状态

每个任务的窗口状态放在视频工作目录里：第一个任务在 `text/window/`，`translate_only` 在 `text/window/processed/`，各有一个 `state.json`（游标、大纲、大小、每轮的记录），每个保存的段落一个 `out_NNNNN.md` 文件。只要单元、prompt 和状态格式没变，重跑 `run` 就从游标续跑；否则这个任务从头开始。请求数、估算的 token 数和每个任务的覆盖率记进视频的 `state.json`。没有单独的 telemetry 文件，也没有暂停或取消信号：停掉进程再重跑就是控制手段，`--max-seconds` 则在两轮之间干净地停下。

### 7.6 为什么这个子系统设计适合整个项目

长文本子系统就是 `text` 阶段，不是另一个独立产品。它消费 `normalize` 生成的规范化文档，按 `plan` 选定的任务执行，和其他阶段共用同一个视频锁和状态文件，把段落交给 `merge`。短视频也用它，是只跑一轮的情况，所以只有一条文字处理路径需要测试和改进。

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

三月加入的 runtime 层（policy、evaluator、decision 记录、recovery 提示、ledger、artifact graph，以及对外的 create/inspect/advance API）本意是给弱模型和 harness 一个结构化契约：当前状态、允许的下一步动作和推荐的决定。用意是对的，但这个契约从没送到模型面前：只有带 `--api-envelope` 才会输出，而 `SKILL.md` 从不传这个参数，也没有别的东西读它。`run` 接管流程以后，模型不再选择下一步动作，这个契约就没有读者了。模型仍然需要的是整次运行的结果，6.5 节用几个字段给出。真正起作用的部分（占有锁、可续跑的状态、用量记录）已移进 `pipeline` 和 `state.json`，其余在第 4 阶段删除。这一层当初的设计理由见 [9.3](#93-设计历史) 链接的 2026-03-19 研究笔记。

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

## 9. 实现对照（附录）

### 9.1 设计的每一部分在哪里实现

| 设计 | 代码 |
|---|---|
| 入口脚本和命令（6.10） | `yt_transcript_utils.py` → `kernel/cli.py` |
| 阶段列表、`state.json`、续跑、覆盖参数、结果和退出码（6.2、6.4 到 6.6） | `kernel/pipeline.py`：`STAGES`、`EXIT_CODES`、`_Run.execute`、`_Run.run_stage`、`_Run.invalidate`、`run`、`status`、`cleanup` |
| 锁和多个运行同时进行（6.4） | `kernel/locks.py`（`FileLock`）；`kernel/pipeline.py` 里的 `video_lock_path` 和 `run`；`_Run.youtube`；`kernel/sources/youtube.py`（`lock_path`、`refusal_since`、`record_refusal`） |
| `--max-seconds` 和 `in_progress`（6.7） | `_Run.deadline_passed`，以及 `stage_text` 交给窗口执行器的 `stop_check` |
| 调用 yt-dlp、更新检查、`needs_user` 的原因（6.2） | `_Run.download`、`_Run.update_yt_dlp`、`kernel/sources/youtube.py`（`classify`）；`scripts/download.sh`、`scripts/update_yt_dlp.sh` |
| 语言判断和字幕轨选择（6.3） | `kernel/sources/subtitle_language.py`（`select_subtitle_candidates`）、`_Run.stage_source`、`_Run.subtitle_route` |
| 字幕质量分和改道（6.3） | `kernel/text/subtitle_quality.py`，在 `_Run.subtitle_route` 里使用 |
| Deepgram（6.3） | `kernel/sources/deepgram.py`（`transcribe_deepgram`）、`_Run.transcribe` |
| VTT 解析和字幕清理 | `kernel/text/cleanup/subtitle.py`、`cjk.py`、`overlap.py` |
| 规范化文档 | `kernel/text/normalize.py` |
| 文字处理计划 | `kernel/text/plan.py`、`_Run.stage_plan` |
| 长文本子系统（第 7 节） | 见 7.5 |
| LLM 后端（6.8） | `kernel/llm/agent_cli.py`、`kernel/llm/api.py`；选择在 `kernel/text/window.py`；交给 agent 在 `kernel/pipeline.py`（`AgentHandoff`） |
| 质量检查（6.9） | `kernel/quality/verify.py`、`kernel/quality/semantic.py` |
| 组装文章（6.9） | `kernel/text/assemble.py`（`assemble_final`、`frontmatter_url`）、`_Run.stage_assemble`、`kernel/pipeline.py` 里的 `article_path` |
| 配置和默认值 | `kernel/config.py`，每一项在 `config.example.yaml` 里有说明 |
| 与宿主的约定（6.13） | `SKILL.md` |
| 测试（6.12） | `tests/`、`scripts/run_regression.py` |

### 9.2 代码和计划不同的地方

实现这份设计时依据的计划，和代码有几处不同，都是有意为之：

- 6.11 里模块结构的四点：字幕质量在 `text`，`download.sh` 仍是子进程，交给 agent 的逻辑在 `pipeline`，默认值都归 `config.py`；
- `subtitle-info` 从 stdin 读 yt-dlp 元数据，而不是接收一个链接，因为 `download.sh` 把元数据管道给它，而 `run` 直接用已经取到的元数据；
- `text` 阶段在窗口运行之前仍然先把原文切成分块，因为分块既决定单元边界的切分方式，也是兜底处理的大小。

### 9.3 设计历史

这份设计分七个阶段实现，每个阶段一个 pull request，总 tracker 是 [#23](https://github.com/williamwang-ty/yt-transcript/issues/23)：

| 阶段 | 改动 |
|---|---|
| 0（[#31](https://github.com/williamwang-ty/yt-transcript/pull/31)） | 这份设计文档 |
| 1（[#32](https://github.com/williamwang-ty/yt-transcript/pull/32)） | 基于录好的 fixtures 的离线端到端测试，配有假的 yt-dlp、Deepgram 和 LLM |
| 2（[#33](https://github.com/williamwang-ty/yt-transcript/pull/33)） | `run` 命令和每个视频一个的 `state.json`，从回归脚本里那份流程副本改造而来 |
| 3（[#34](https://github.com/williamwang-ty/yt-transcript/pull/34)） | `SKILL.md` 缩减为驱动 `run`；删除 Quick Mode 和 `workflows/`；yt-dlp 更新检查移进 `run` |
| 4（[#35](https://github.com/williamwang-ty/yt-transcript/pull/35)） | 删除冗余层：手写的 `state.md` 及其检查，runtime 契约层（policy、decision、evaluator、recovery、ledger、artifacts、对外 API、暂停和取消、telemetry），固定分块执行器连同 `summarize` 和 chapter plan，legacy 开关，`preflight.sh` 和 `cleanup.sh`，以及大部分对外命令 |
| 5（[#36](https://github.com/williamwang-ty/yt-transcript/pull/36)） | 把约 8000 行的 `yt_transcript_utils.py` 拆成 `kernel/` 下的各个包，显式导入 |
| 6 | 文档：补全本文，删除 `system_design_notes/`，重写 README |

`state.json` 取代了手写的 `state.md`、`machine_state.json`、分块 `manifest.json` 里的执行状态（现在留下的 `text/manifest.json` 只列出分块文件）、`telemetry.jsonl`，以及散落的 `/tmp/<video_id>_*` 文件。

和第 3 阶段之前由 agent 驱动的流程相比，用户能感觉到的行为变化：

1. 短视频的文字改由 LLM 后端（通常是宿主 CLI）处理，不再由宿主模型在对话里直接写。
2. 首选字幕轨被拒时，运行以 `needs_user` 停下，不再悄悄换用另一条字幕轨（6.3）。
3. 对已完成的视频重跑会返回已有的文章，而不是从头再做；要重做用 `--restart`。
4. 字幕质量改道在 `source` 内部、规范化之前完成；结果不变。
5. 新增：`--max-seconds` 和 `in_progress` 结果，用于 shell 有时间上限的宿主。

字幕语言选择、Deepgram 转写、窗口执行器的算法、大纲和术语表、质量检查都只是搬了位置，行为没变；唯一的例外是第 3 阶段的两处输出修正：窗口输出合并时不再拼接短段落（这曾把双语的中英两段粘在一起），`structure_only` 去掉 `-` 说话人标记。

这次重构之前写的设计笔记和本文的旧版本在第 6 阶段删除，但在第 6 阶段之前的最后一个提交和 git 历史里仍然可以读到（`git log -- SYSTEM_DESIGN.md system_design_notes/`）：

- [研究笔记，2026-03-19](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/system_design_notes/20260319_SYSTEM_DESIGN_RESEARCH_NOTES.md)：项目属于哪种架构，以及"adaptive runtime"的设想，它后来成了 runtime 契约层（第 4 阶段删除，见 8.4）。
- [最终交付物质量规划，2026-03-25](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/system_design_notes/20260325_SYSTEM_DESIGN_FINAL_DELIVERABLE_QUALITY_PLAN.md)：文章质量问题从哪里来，哪些归代码、哪些归 LLM。
- [架构 review，2026-03-30](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/system_design_notes/20260330_SYSTEM_DESIGN_ARCHITECTURE_REVIEW.md)：脚本和 LLM 的边界。
- [第 6 阶段之前的本文](https://github.com/williamwang-ty/yt-transcript/blob/f33bb9f302cc41c4b8fc436db611f2212f6d2efc/SYSTEM_DESIGN.md)：其中第 9 节逐个列出旧文件和旧命令在每个阶段去了哪里。

每个阶段的理由和验证记录在上表链接的 pull request 和 issue 里。

---

## 10. 一句话总结

`yt-transcript` 是一个本地转录生产系统：一条确定性的命令 `run` 负责从 YouTube 链接到 Markdown 文章的已知路径，LLM 只在其中处理文字，宿主 agent 负责例外情况和与用户的沟通。
