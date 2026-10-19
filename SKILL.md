---
name: yt-transcript
description: Transcribe YouTube videos into formatted Markdown articles (a Chinese article, or English text with a Chinese translation). Uses the video's subtitles, or Deepgram speech-to-text when there are none. Use this skill when a user provides a YouTube link and wants a text version.
---

# YouTube Video Transcription

One command does the whole job for one video: it picks the subtitle track or Deepgram, processes the text with an LLM, checks the quality, and writes the article into the configured `output_dir`. Your part is to start it, act on the result it returns, and report to the user.

```bash
python3 <skill-root>/yt_transcript_utils.py run "<youtube-url>" --max-seconds 300
```

- `<skill-root>` is the directory that contains this SKILL.md.
- Give this shell command a 10-minute timeout if your shell tool lets you set one (for example `timeout: 600000`). If your shell limit is shorter, pass about half of it as `--max-seconds`.
- `run` prints progress on stderr and exactly one JSON object on stdout; the exit code matches its `status`.

## Act on the result

| `status` (exit code) | What to do |
|---|---|
| `done` (0) | Report it (see Reporting below). |
| `in_progress` (6) | Not an error: the time budget ran out at a safe point. Run `next_command` again at once, and keep going until the status changes. |
| `needs_agent_text` (5) | No LLM backend was reachable, so one text request is handed to you. Read `task.prompt_file` and do exactly what it asks (keep every `[S<n>]` tag, end with the `<!--END-->` line it describes). Write only your answer to `task.output_file`, then run `next_command`. A video can need several such requests; repeat until the status changes. |
| `needs_user` (3) | Only the user can fix this. Tell them what `message` says and stop. For reason `rate_limited` with an `accept_command`: ask whether to wait and retry later, or accept the `fallback_track` now; run `accept_command` only if they accept. |
| `unsupported` (4) | Tell the user the video's spoken language is not supported (only Chinese and English are). Do not retry or force another source. |
| `failed` (1) | If `retryable` is true, run `next_command` once more. Otherwise, or if it fails again, report `stage`, `reason` and `message`. `locked`: another run of this video is still active; do not start a second one. `override_not_applicable`: the flag the user asked for cannot apply to this video; say why. |

Exit code 2 means the command line was wrong (for example, not a YouTube URL).

Rules:

- Do not run the internal steps yourself (`download.sh`, `process-chunks`, `assemble-final` and so on), do not edit anything in the work directory except `task.output_file`, and do not write or rewrite the article yourself. To change the outcome, rerun `run` with a flag below.
- Rerunning the same command resumes an interrupted or failed run where it stopped, including one your shell killed before it printed a result. A video that is already done returns `done` at once with the existing article.
- The article is final. Do not summarize it, rewrite it, or pass it through another digest step unless the user asks, and do not read it into the conversation unless they ask (it can be long).

## User requests and flags

| The user asks for | Add to `run` |
|---|---|
| speech-to-text (Deepgram) instead of subtitles | `--source deepgram` |
| the subtitles, even if their quality looks poor | `--source subtitles` |
| a Chinese article only / English text with a Chinese translation | `--mode chinese` / `--mode bilingual` |
| doing a video again from scratch | `--restart` |
| only checking which source and mode would be used, without spending anything | `--stage plan` |
| redoing the text after a quality failure | `--from text` |

Add a flag only when the user asked for that outcome. `--source` and `--mode` are remembered for that video, so later reruns keep them until `--restart`.

## Several videos

Process the URLs one at a time, in order, each with its own `run`, and finish handling one video's result before starting the next. A `needs_user` result with reason `cookies_required`, `rate_limited`, `tool_missing` or `output_dir_missing` would hit the remaining videos too: stop there and tell the user. Every other result concerns only that video (for example `deepgram_key_missing` matters only for videos without usable subtitles): note it and go on with the next URL. End with a table: title (or the URL when the result has none), status, and the output file or the reason.

## Unfinished runs

`python3 <skill-root>/yt_transcript_utils.py status` lists interrupted runs, each with its `next_command`; `status <video_id>` shows one video. Run `next_command` to continue one. Discard a run only when the user asks: `python3 <skill-root>/yt_transcript_utils.py cleanup <video_id> --force`. Work files live under `/tmp/yt-transcript/`, so a reboot loses unfinished runs.

## Reporting

Reply in the user's language. For `done`, give the title, `output_file`, the route, and any `warnings` (advisory only; the article was written):

```text
✅ Transcription complete
   Title: <title>
   Source: <route.source: YouTube subtitles or Deepgram>
   Mode: <route.mode: chinese = Chinese article, bilingual = English text with Chinese translation>
   Output file: <output_file>
```

If you answered `needs_agent_text` requests, say so: setting up an LLM backend (`llm_*` in `config.yaml`) lets later runs finish without you.

Product decisions you may need to explain:

- Human Chinese subtitles win for any video. Otherwise a Chinese video gives a Chinese article and an English video gives English text with a Chinese translation.
- YouTube's machine-translated subtitle tracks are never used. A video without trusted subtitles in its spoken language, or whose Chinese subtitles are very poor, is transcribed with Deepgram automatically.
- Videos spoken in other languages are not supported yet.
