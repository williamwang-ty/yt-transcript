# yt-transcript

[English](#english) | [中文](#中文)

---

## English

An agent skill that turns a YouTube video into one formatted Markdown article: a Chinese article for a Chinese video, or the English text with a Chinese translation for an English video. It uses the video's own subtitles, or Deepgram speech-to-text when there are none worth trusting, and an LLM to restore punctuation, paragraphs and headings.

[SKILL.md](SKILL.md) is what the host agent reads. [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) explains the design and why it is built this way. This README covers installing, configuring and running it.

### How it works

One command does the whole job for one video:

```bash
python3 yt_transcript_utils.py run "<youtube-url>"
```

`run` goes through fixed stages: `metadata` → `source` → `normalize` → `plan` → `text` → `merge` → `verify` → `assemble` → `cleanup`. It records its progress in one `state.json` per video, prints progress on stderr and exactly one JSON result on stdout, and exits with a code that matches the result. Rerunning the same command continues where it stopped. The host agent (Claude Code, Codex and so on) only starts `run`, acts on the result and reports to the user; the LLM is used only to rewrite text.

How the source and the output language are chosen:

- The spoken language comes from the video's original audio track, its metadata, or its speech-recognition (ASR) track.
- Human Chinese subtitles win for any video. Otherwise a Chinese video gets a Chinese article from its Chinese subtitles, and an English video gets English text with a Chinese translation (`bilingual`) from its English subtitles.
- YouTube's machine-translated subtitle tracks are never used.
- A video without trusted subtitles in its spoken language is transcribed with Deepgram (`nova-3`). So is a Chinese article whose subtitles score as critically poor.
- Videos spoken in other languages stop as `unsupported`.

### Requirements

| Tool | Needed for |
|---|---|
| `python3` (3.10 or newer) | Everything; only the standard library is used |
| `bash` | `scripts/download.sh` and `scripts/update_yt_dlp.sh` |
| `yt-dlp` | Metadata, subtitles and audio. `run` updates it (via `brew` or `pip`) at most once an hour, because YouTube keeps breaking old releases |
| `ffmpeg` and `ffprobe` | Only for Deepgram audio over 10 MB, which is split at silences |
| `curl` | Optional: the yt-dlp update check. Without it the check is skipped |
| A [Deepgram](https://console.deepgram.com/) API key | Only for videos without usable subtitles |
| An LLM backend | The text of every video: the host agent's own CLI, an OpenAI-compatible or Anthropic API, or, as a last resort, the host agent itself (see below) |

```bash
# macOS
brew install yt-dlp ffmpeg
```

### Install

Clone the repository into your agent's skills directory and create `config.yaml`:

```bash
git clone https://github.com/williamwang-ty/yt-transcript.git ~/.claude/skills/yt-transcript
cd ~/.claude/skills/yt-transcript
cp config.example.yaml config.yaml
```

Then edit `config.yaml`: set `output_dir`, and add the Deepgram key and LLM API settings you want to use. `config.yaml` is found next to `yt_transcript_utils.py`, so the skill works from any directory; `--config-path` points `run` at another file. `config.yaml` is gitignored because it holds API keys.

### Configuration

`config.example.yaml` documents every key. It is a flat list of `key: value` lines; nested YAML and multi-line values are not supported. A key that is left out takes its default.

| Keys | What they control |
|---|---|
| `output_dir` | Where articles are written. The only setting every run needs |
| `deepgram_api_key`, `deepgram_model`, `deepgram_enable_utterances`, `deepgram_prefer_structured_output` | Deepgram transcription. `nova-2` models are upgraded to `nova-3` |
| `yt_dlp_socket_timeout_sec`, `yt_dlp_retries`, `yt_dlp_extractor_retries` | yt-dlp timeouts and retries |
| `yt_dlp_cookies_file`, `yt_dlp_cookies_from_browser` | YouTube cookies (see below) |
| `llm_backend`, `llm_agent_cli`, `llm_cli_timeout_sec`, `llm_cli_max_retries`, `llm_cli_probe_timeout_sec` | Which LLM backend processes the text, and the agent CLI's limits |
| `llm_api_format`, `llm_api_key`, `llm_base_url`, `llm_model`, `llm_timeout_sec`, `llm_max_retries`, `llm_backoff_sec`, `llm_stream` | The LLM API |
| `window_*` | The sliding-window text processing (sizes, retries, the global outline) |
| `chunk_*`, `output_ratio_*`, `max_output_tokens_*` | Per-task sizes: chunk targets, expected output/source ratio, and the `max_tokens` ceiling |

Environment variables:

| Variable | Effect |
|---|---|
| `YT_TRANSCRIPT_OUTPUT_DIR` | Replaces `output_dir` |
| `YT_TRANSCRIPT_WORK_ROOT` | Work root instead of `/tmp/yt-transcript` |
| `YT_TRANSCRIPT_AGENT_CLI` | The agent CLI for the text; wins over `llm_agent_cli` |
| `YT_TRANSCRIPT_YT_DLP_UPDATE=0` | Skips the yt-dlp update check |
| `YT_DLP_SOCKET_TIMEOUT_SEC`, `YT_DLP_RETRIES`, `YT_DLP_EXTRACTOR_RETRIES`, `YT_DLP_COOKIES_FILE`, `YT_DLP_COOKIES_FROM_BROWSER` | Win over the matching `yt_dlp_*` keys |

#### LLM backend

With `llm_backend: auto` (the default) the text goes to an agent CLI chosen in this order: `YT_TRANSCRIPT_AGENT_CLI`, then `llm_agent_cli`, then the CLI this video's run already used, then the agent that is running `run` (Claude Code, Codex, OpenCode, pi, Gemini CLI or Cursor, detected from its environment variables). `llm_agent_cli` takes one of those names or a full non-interactive command (`{prompt}` marks where the prompt goes; otherwise it is sent on stdin). The CLI runs in an empty temporary directory and must answer a short probe first.

If no CLI is found or the probe fails, the LLM API is used when `llm_api_key`, `llm_base_url` and `llm_model` are all set. A CLI that fails in the middle of a run also switches to the API when it is set. `llm_backend: api` skips the CLI. When no backend is reachable, `run` hands each text request to the host agent (`needs_agent_text`), which writes the answer itself and reruns.

#### YouTube cookies and bot checks

`run` starts without cookies. When YouTube asks to confirm you are not a bot, rate-limits the request, or asks for a sign-in, `download.sh` retries up to three times with `--cookies-from-browser chrome`. If that does not help, `run` stops with `needs_user` (`cookies_required`, `rate_limited` or `login_required`). The portable fix is a Netscape-format `cookies.txt` exported from a browser that is signed in to YouTube:

```yaml
yt_dlp_cookies_file: "~/.config/yt-transcript/youtube_cookies.txt"
```

`yt_dlp_cookies_from_browser: "chrome"` (or `firefox`, `safari`, `edge`, as yt-dlp supports) uses a browser profile from the start. In a container or on a remote machine, `cookies.txt` is usually the one that works.

### Usage

#### As a skill

Give the agent a YouTube link, for example "Please transcribe this video: https://www.youtube.com/watch?v=xxxxx". Several links are processed one at a time, and the agent ends with a table of titles, statuses and output files. Ask for what you want in plain words; the agent turns it into a flag:

| You ask for | Flag |
|---|---|
| Deepgram instead of subtitles / the subtitles even if they look poor | `--source deepgram` / `--source subtitles` |
| A Chinese article only / English with a Chinese translation | `--mode chinese` / `--mode bilingual` |
| Doing a video again from scratch | `--restart` |
| Only checking which source and mode would be used, without spending anything | `--stage plan` |
| Redoing the text after a quality failure | `--from text` |

`--source` and `--mode` are remembered for the video until `--restart`.

#### By hand

```bash
# Transcribe one video; rerun the same command to continue
python3 yt_transcript_utils.py run "$URL"

# Stop cleanly at the next safe point after about 300 seconds (for shells with a time limit)
python3 yt_transcript_utils.py run "$URL" --max-seconds 300

# Free routing check: source, mode and text plan, no LLM or Deepgram
python3 yt_transcript_utils.py run "$URL" --stage plan

# Overrides
python3 yt_transcript_utils.py run "$URL" --source deepgram   # or subtitles
python3 yt_transcript_utils.py run "$URL" --mode chinese      # or bilingual
python3 yt_transcript_utils.py run "$URL" --from text         # redo this stage and every later one
python3 yt_transcript_utils.py run "$URL" --restart           # discard the work directory and start over

# Unfinished runs
python3 yt_transcript_utils.py status                         # every run under the work root
python3 yt_transcript_utils.py status "$VIDEO_ID"             # one video's state.json
python3 yt_transcript_utils.py cleanup "$VIDEO_ID"            # remove a finished run's work directory
python3 yt_transcript_utils.py cleanup "$VIDEO_ID" --force    # discard an unfinished one
```

The URL can also be an 11-character video id.

#### Results

| `status` | Exit code | Meaning and main fields |
|---|---|---|
| `done` | 0 | The article was written: `output_file`, `title`, `route` (source, language, track, mode), `warnings` (advisory), `llm` (backend, requests, estimated tokens). A `--stage plan` call also ends with `done` |
| `failed` | 1 | A stage failed: `stage`, `reason`, `message`, `retryable`. `locked` means another run of the same video is active |
| `needs_user` | 3 | Only the user can fix it: `cookies_required`, `login_required`, `rate_limited`, `deepgram_key_missing`, `tool_missing` or `output_dir_missing` |
| `unsupported` | 4 | The spoken language is neither Chinese nor English |
| `needs_agent_text` | 5 | No LLM backend was reachable: `task.prompt_file` is the request, the answer goes to `task.output_file` |
| `in_progress` | 6 | The `--max-seconds` budget ran out at a safe point; run `next_command` to continue |

Every result has `video_id`, `work_dir` and `seconds`. Results that can be continued (all but a finished article and `unsupported`) also have `next_command`, the exact command to run next. Exit code 2 means the command line was wrong.

When YouTube refuses the preferred subtitle track (rate limit or bot check) and only another track downloads, `run` does not switch on its own: it stops with `needs_user` (`rate_limited` or `cookies_required`) and adds `preferred_track`, `fallback_track` and `accept_command` (the same command with `--mode` for the other track), so the user decides between retrying later and accepting the other track.

### Work directory and resume

Each video has one work directory, `/tmp/yt-transcript/<video_id>/`, holding `state.json` (stage records, route, overrides, warnings, LLM usage, last result) and the files of each stage. Rerunning `run` skips finished stages and continues an interrupted text stage paragraph by paragraph. A second `run` of a video that is still running gets `failed` with reason `locked`. Once the article is written, the `cleanup` stage removes the intermediate files; `state.json` stays, so a rerun returns `done` at once with the existing article.

Known limitation: macOS (and many Linux systems) empties `/tmp` on restart, so an interrupted run starts over after a reboot unless `YT_TRANSCRIPT_WORK_ROOT` points somewhere persistent. An unfinished Deepgram run also keeps its audio in the work directory (it can take hundreds of MB) until it finishes or is discarded with `cleanup <video_id> --force`.

### Output

The article is written to `<output_dir>/<upload date>. <title>.md` (for example `2026-10-22. Some Title.md`). It starts with YAML frontmatter (`title`, `source`, `url`, `channel`, `author`, `date`, `created`, `type`, `bilingual`, `duration`, `transcript_source`, `tags`, `via`), then a header with the title, a channel link, the language mode and the duration, then the text with `##` section headings, and ends with a one-line footer. In bilingual articles each English paragraph is followed by its Chinese translation, and each heading is an English `##` line followed by a Chinese one. The frontmatter suits an Obsidian vault.

### Development

```bash
# All unit and regression tests, including the offline end-to-end run
python3 -m unittest tests.test_regressions

# Offline end-to-end run over recorded fixtures (fake yt-dlp, Deepgram and LLM)
python3 scripts/run_regression.py --fixtures tests/fixtures/e2e
# After an intended output change, refresh its baseline
UPDATE_E2E_BASELINE=1 python3 -m unittest tests.regression_e2e

# Live regression on real videos (network, an LLM backend, and Deepgram unless skipped)
python3 scripts/run_regression.py --stage plan --no-deepgram   # free routing check
python3 scripts/run_regression.py --only <video_id> --compare <earlier report.json>
```

`run_regression.py` also takes `--videos`, `--out-dir`, `--timeout` and `--config-path`; see [tests/live_regression/README.md](tests/live_regression/README.md).

Debug commands (the host agent does not use them):

```bash
# Spoken language and subtitle candidates from yt-dlp metadata
yt-dlp -J "$URL" | python3 yt_transcript_utils.py subtitle-info

# The quality check on any text file
python3 yt_transcript_utils.py verify-quality "$FILE" --raw-text "$RAW_TEXT" [--bilingual] [--work-dir <dir with glossary.json>]
```

### Project structure

```text
yt-transcript/
├── SKILL.md                 # What the host agent reads
├── SYSTEM_DESIGN.md         # The design document
├── config.example.yaml      # Config template; copy to config.yaml (gitignored)
├── yt_transcript_utils.py   # Entry script
├── kernel/
│   ├── cli.py               # Commands: run, status, cleanup, subtitle-info, verify-quality
│   ├── pipeline.py          # `run`: stages, state.json, lock, results, agent handoff
│   ├── config.py            # config.yaml loading and every default
│   ├── files.py             # Small file helpers
│   ├── llm/                 # Agent CLI and LLM API calls
│   ├── sources/             # Subtitle language and track choice; Deepgram
│   ├── text/                # Subtitle cleanup, normalization, plan, chunking, window executor,
│   │                        # outline, glossary, prompts, merge, article assembly
│   └── quality/             # The quality check
├── prompts/                 # cleanup_zh, structure_only, translate_only
├── scripts/
│   ├── download.sh          # yt-dlp: metadata, subtitles, audio, cookie retries
│   ├── update_yt_dlp.sh     # Hourly yt-dlp update check
│   └── run_regression.py    # Live and offline regression runner
└── tests/                   # Unit tests, offline end-to-end fixtures, live regression video set
```

### License

MIT License

### Links

- [Deepgram API docs](https://developers.deepgram.com/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)

---

## 中文

一个 agent skill：把 YouTube 视频变成一篇排好版的 Markdown 文章。中文视频得到一篇中文文章，英文视频得到英文原文加中文翻译。它使用视频自带的字幕，没有可信字幕时用 Deepgram 语音转文字，再用 LLM 恢复标点、分段和小标题。

[SKILL.md](SKILL.md) 是宿主 agent 读的说明，[SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) 讲设计以及为什么这样设计。本文讲安装、配置和使用。

### 工作方式

一条命令完成一个视频的全部工作：

```bash
python3 yt_transcript_utils.py run "<youtube-url>"
```

`run` 依次经过固定的阶段：`metadata` → `source` → `normalize` → `plan` → `text` → `merge` → `verify` → `assemble` → `cleanup`。每个视频的进度记在一个 `state.json` 里；运行过程打印在 stderr，stdout 只输出一个 JSON 结果，退出码和结果对应。重跑同一条命令就从停下的地方继续。宿主 agent（Claude Code、Codex 等）只负责启动 `run`、处理它返回的结果、向用户汇报；LLM 只用来改写文字。

来源和输出语言怎么定：

- 视频的口语语言来自原始音轨、视频元数据或语音识别（ASR）字幕轨。
- 任何视频只要有人工中文字幕，就用它。否则中文视频用中文字幕出中文文章，英文视频用英文字幕出英文原文加中文翻译（`bilingual`）。
- 从不使用 YouTube 的机器翻译字幕轨。
- 口语语言没有可信字幕的视频，用 Deepgram（`nova-3`）转写；出中文文章而字幕质量评分为极差的，也改用 Deepgram。
- 其他语言的视频以 `unsupported` 停下。

### 依赖

| 工具 | 用途 |
|---|---|
| `python3`（3.10 或更新） | 全部功能；只用标准库 |
| `bash` | `scripts/download.sh` 和 `scripts/update_yt_dlp.sh` |
| `yt-dlp` | 元数据、字幕和音频。因为 YouTube 经常让旧版失效，`run` 最多每小时检查一次并（通过 `brew` 或 `pip`）更新它 |
| `ffmpeg` 和 `ffprobe` | 只在 Deepgram 的音频超过 10 MB、需要在静音处切开时使用 |
| `curl` | 可选：yt-dlp 更新检查用，没有就跳过检查 |
| [Deepgram](https://console.deepgram.com/) API key | 只在视频没有可用字幕时需要 |
| LLM 后端 | 每个视频的文字处理：宿主 agent 自己的命令行、OpenAI 兼容或 Anthropic API，最后还可以交给宿主 agent 本身（见下文） |

```bash
# macOS
brew install yt-dlp ffmpeg
```

### 安装

把仓库克隆到 agent 的 skills 目录，并创建 `config.yaml`：

```bash
git clone https://github.com/williamwang-ty/yt-transcript.git ~/.claude/skills/yt-transcript
cd ~/.claude/skills/yt-transcript
cp config.example.yaml config.yaml
```

然后编辑 `config.yaml`：设置 `output_dir`，再填上要用的 Deepgram key 和 LLM API。`config.yaml` 从 `yt_transcript_utils.py` 所在目录读取，所以在任何目录下都能用；`--config-path` 可以让 `run` 读另一个文件。`config.yaml` 存有 API key，已被 gitignore。

### 配置

`config.example.yaml` 对每一项都有说明。它是扁平的 `key: value` 行，不支持嵌套 YAML 和多行值。没写的配置项取默认值。

| 配置项 | 控制什么 |
|---|---|
| `output_dir` | 文章写到哪里。唯一每次运行都需要的设置 |
| `deepgram_api_key`、`deepgram_model`、`deepgram_enable_utterances`、`deepgram_prefer_structured_output` | Deepgram 转写。`nova-2` 系列模型会自动换成 `nova-3` |
| `yt_dlp_socket_timeout_sec`、`yt_dlp_retries`、`yt_dlp_extractor_retries` | yt-dlp 的超时和重试 |
| `yt_dlp_cookies_file`、`yt_dlp_cookies_from_browser` | YouTube cookies（见下文） |
| `llm_backend`、`llm_agent_cli`、`llm_cli_timeout_sec`、`llm_cli_max_retries`、`llm_cli_probe_timeout_sec` | 文字交给哪个 LLM 后端，以及 agent 命令行的限制 |
| `llm_api_format`、`llm_api_key`、`llm_base_url`、`llm_model`、`llm_timeout_sec`、`llm_max_retries`、`llm_backoff_sec`、`llm_stream` | LLM API |
| `window_*` | 滑动窗口文字处理（大小、重试、全局大纲） |
| `chunk_*`、`output_ratio_*`、`max_output_tokens_*` | 每个任务的大小：分块目标、预期的输出/原文比例、`max_tokens` 上限 |

环境变量：

| 变量 | 作用 |
|---|---|
| `YT_TRANSCRIPT_OUTPUT_DIR` | 取代 `output_dir` |
| `YT_TRANSCRIPT_WORK_ROOT` | 工作根目录，默认 `/tmp/yt-transcript` |
| `YT_TRANSCRIPT_AGENT_CLI` | 处理文字的 agent 命令行，优先于 `llm_agent_cli` |
| `YT_TRANSCRIPT_YT_DLP_UPDATE=0` | 跳过 yt-dlp 更新检查 |
| `YT_DLP_SOCKET_TIMEOUT_SEC`、`YT_DLP_RETRIES`、`YT_DLP_EXTRACTOR_RETRIES`、`YT_DLP_COOKIES_FILE`、`YT_DLP_COOKIES_FROM_BROWSER` | 优先于对应的 `yt_dlp_*` 配置项 |

#### LLM 后端

`llm_backend: auto`（默认）时，文字交给一个 agent 命令行，按这个顺序选：`YT_TRANSCRIPT_AGENT_CLI`，然后 `llm_agent_cli`，然后这个视频的运行已经用过的命令行，最后是正在运行 `run` 的 agent（Claude Code、Codex、OpenCode、pi、Gemini CLI 或 Cursor，从它的环境变量识别）。`llm_agent_cli` 可以写上面的名字，也可以写一条完整的非交互命令（`{prompt}` 标出 prompt 的位置，否则 prompt 从 stdin 传入）。命令行在一个空的临时目录里运行，并且要先通过一次简短的探测。

找不到命令行或探测失败时，如果 `llm_api_key`、`llm_base_url` 和 `llm_model` 都设置了，就用 LLM API。命令行在运行中途失败，也会在设置了 API 时改用 API。`llm_backend: api` 不用命令行。没有任何可用后端时，`run` 把每个文字请求交给宿主 agent（`needs_agent_text`），由它自己写出答案再重跑。

#### YouTube cookies 和机器人验证

`run` 一开始不带 cookies。YouTube 要求确认不是机器人、限流或要求登录时，`download.sh` 会带上 `--cookies-from-browser chrome` 最多重试三次。仍然不行，`run` 就以 `needs_user`（`cookies_required`、`rate_limited` 或 `login_required`）停下。最通用的办法是从已登录 YouTube 的浏览器导出 Netscape 格式的 `cookies.txt`：

```yaml
yt_dlp_cookies_file: "~/.config/yt-transcript/youtube_cookies.txt"
```

`yt_dlp_cookies_from_browser: "chrome"`（或 yt-dlp 支持的 `firefox`、`safari`、`edge`）从一开始就使用浏览器的登录信息。在容器或远程机器上，通常只有 `cookies.txt` 能用。

### 使用

#### 作为 skill

把 YouTube 链接交给 agent，例如"请转录这个视频：https://www.youtube.com/watch?v=xxxxx"。多个链接会逐个处理，最后给出一张表，列出标题、状态和输出文件。想要什么直接说，agent 会转成参数：

| 你要的 | 参数 |
|---|---|
| 用 Deepgram 而不是字幕 / 字幕质量看起来差也要用字幕 | `--source deepgram` / `--source subtitles` |
| 只要中文文章 / 英文原文加中文翻译 | `--mode chinese` / `--mode bilingual` |
| 把一个视频从头重做 | `--restart` |
| 只看会用哪个来源和模式，不花钱 | `--stage plan` |
| 质量检查失败后重做文字 | `--from text` |

`--source` 和 `--mode` 会对这个视频一直记住，直到 `--restart`。

#### 手动运行

```bash
# 转录一个视频；重跑同一条命令就是续跑
python3 yt_transcript_utils.py run "$URL"

# 约 300 秒后在下一个安全点干净地停下（给有时间限制的 shell 用）
python3 yt_transcript_utils.py run "$URL" --max-seconds 300

# 免费的路由检查：来源、模式和文字处理计划，不调用 LLM 和 Deepgram
python3 yt_transcript_utils.py run "$URL" --stage plan

# 覆盖参数
python3 yt_transcript_utils.py run "$URL" --source deepgram   # 或 subtitles
python3 yt_transcript_utils.py run "$URL" --mode chinese      # 或 bilingual
python3 yt_transcript_utils.py run "$URL" --from text         # 重做这个阶段及之后的所有阶段
python3 yt_transcript_utils.py run "$URL" --restart           # 删掉工作目录，从头开始

# 未完成的运行
python3 yt_transcript_utils.py status                         # 工作根目录下的所有运行
python3 yt_transcript_utils.py status "$VIDEO_ID"             # 一个视频的 state.json
python3 yt_transcript_utils.py cleanup "$VIDEO_ID"            # 删除已完成运行的工作目录
python3 yt_transcript_utils.py cleanup "$VIDEO_ID" --force    # 放弃一个未完成的运行
```

URL 也可以是 11 位的视频 id。

#### 结果

| `status` | 退出码 | 含义和主要字段 |
|---|---|---|
| `done` | 0 | 文章已写出：`output_file`、`title`、`route`（来源、语言、字幕轨、模式）、`warnings`（仅供参考）、`llm`（后端、请求数、估算的 token 数）。`--stage plan` 调用也以 `done` 结束 |
| `failed` | 1 | 某个阶段失败：`stage`、`reason`、`message`、`retryable`。`locked` 表示这个视频的另一次运行还在进行 |
| `needs_user` | 3 | 只有用户能解决：`cookies_required`、`login_required`、`rate_limited`、`deepgram_key_missing`、`tool_missing` 或 `output_dir_missing` |
| `unsupported` | 4 | 口语语言既不是中文也不是英文 |
| `needs_agent_text` | 5 | 没有可用的 LLM 后端：`task.prompt_file` 是请求，答案写到 `task.output_file` |
| `in_progress` | 6 | `--max-seconds` 的时间用完，停在了安全点；运行 `next_command` 继续 |

每个结果都有 `video_id`、`work_dir` 和 `seconds`。可以继续的结果（除了已写出文章的 `done` 和 `unsupported`）还有 `next_command`，即下一步要运行的准确命令。退出码 2 表示命令行写错了。

首选字幕轨被 YouTube 拒绝（限流或机器人验证）、只有另一条字幕轨能下载时，`run` 不会自己换：它以 `needs_user`（`rate_limited` 或 `cookies_required`）停下，并给出 `preferred_track`、`fallback_track` 和 `accept_command`（同一条命令加上另一条字幕轨的 `--mode`），由用户决定是稍后重试还是接受另一条字幕轨。

### 工作目录和续跑

每个视频有一个工作目录 `/tmp/yt-transcript/<video_id>/`，里面是 `state.json`（各阶段记录、路由、覆盖参数、警告、LLM 用量、上一次结果）和各阶段的文件。重跑 `run` 会跳过已完成的阶段，中断的文字阶段按段落继续。同一个视频还在运行时再启动一次 `run`，会得到原因为 `locked` 的 `failed`。文章写出后，`cleanup` 阶段删掉中间文件，只留下 `state.json`，所以重跑会立刻返回 `done` 和已有的文章。

已知限制：macOS（以及很多 Linux 系统）重启时会清空 `/tmp`，所以除非 `YT_TRANSCRIPT_WORK_ROOT` 指向一个持久的位置，中断的运行在重启后要从头开始。未完成的 Deepgram 运行也会把音频留在工作目录里（可能有几百 MB），直到运行完成或用 `cleanup <video_id> --force` 放弃。

### 输出

文章写到 `<output_dir>/<上传日期>. <标题>.md`（例如 `2026-10-22. 某个标题.md`）。开头是 YAML frontmatter（`title`、`source`、`url`、`channel`、`author`、`date`、`created`、`type`、`bilingual`、`duration`、`transcript_source`、`tags`、`via`），然后是标题、频道链接、语言模式和时长组成的头部，然后是带 `##` 小标题的正文，最后是一行页脚。双语文章里每个英文段落后面紧跟它的中文译文，每个小标题是一行英文 `##` 加一行中文 `##`。frontmatter 适合放进 Obsidian 库。

### 开发

```bash
# 全部单元测试和回归测试，包括离线端到端运行
python3 -m unittest tests.test_regressions

# 基于录好的 fixtures 的离线端到端运行（假的 yt-dlp、Deepgram 和 LLM）
python3 scripts/run_regression.py --fixtures tests/fixtures/e2e
# 有意改变输出之后，更新它的基线
UPDATE_E2E_BASELINE=1 python3 -m unittest tests.regression_e2e

# 用真实视频做回归（需要网络、LLM 后端，不跳过时还需要 Deepgram）
python3 scripts/run_regression.py --stage plan --no-deepgram   # 免费的路由检查
python3 scripts/run_regression.py --only <video_id> --compare <之前的 report.json>
```

`run_regression.py` 还接受 `--videos`、`--out-dir`、`--timeout` 和 `--config-path`；详见 [tests/live_regression/README.md](tests/live_regression/README.md)。

调试命令（宿主 agent 不使用）：

```bash
# 从 yt-dlp 元数据判断口语语言和候选字幕轨
yt-dlp -J "$URL" | python3 yt_transcript_utils.py subtitle-info

# 对任意文本文件做质量检查
python3 yt_transcript_utils.py verify-quality "$FILE" --raw-text "$RAW_TEXT" [--bilingual] [--work-dir <含 glossary.json 的目录>]
```

### 项目结构

```text
yt-transcript/
├── SKILL.md                 # 宿主 agent 读的说明
├── SYSTEM_DESIGN.md         # 设计文档
├── config.example.yaml      # 配置模板；复制为 config.yaml（已 gitignore）
├── yt_transcript_utils.py   # 入口脚本
├── kernel/
│   ├── cli.py               # 命令：run、status、cleanup、subtitle-info、verify-quality
│   ├── pipeline.py          # `run`：阶段、state.json、运行锁、结果、交给 agent
│   ├── config.py            # 读取 config.yaml，以及所有默认值
│   ├── files.py             # 小的文件工具函数
│   ├── llm/                 # agent 命令行和 LLM API 调用
│   ├── sources/             # 字幕语言和字幕轨选择；Deepgram
│   ├── text/                # 字幕清理、规范化、计划、分块、窗口执行器、
│   │                        # 大纲、术语表、prompt、合并、组装文章
│   └── quality/             # 质量检查
├── prompts/                 # cleanup_zh、structure_only、translate_only
├── scripts/
│   ├── download.sh          # yt-dlp：元数据、字幕、音频、cookies 重试
│   ├── update_yt_dlp.sh     # 每小时一次的 yt-dlp 更新检查
│   └── run_regression.py    # 真实视频和离线回归的运行脚本
└── tests/                   # 单元测试、离线端到端 fixtures、真实视频回归的视频列表
```

### 许可证

MIT License

### 相关链接

- [Deepgram API 文档](https://developers.deepgram.com/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
