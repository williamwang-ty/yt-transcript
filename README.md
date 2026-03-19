# yt-transcript

[English](#english) | [中文](#中文)

---

## English

Transcribe YouTube videos into formatted Markdown articles. Supports subtitle download or Deepgram speech-to-text (with multi-speaker recognition).

Start here:
- If you want the canonical terminology, see `Canonical Terms` below.
- If you want the reusable execution order, see `Validation Matrix` and `Minimum Commands`.

### ✨ Features

- 🎯 **Smart Subtitle Fetching**: Prioritizes YouTube official/auto-generated subtitles
- 🎙️ **Speech-to-Text**: Auto-transcribes via Deepgram Nova-2 when no subtitles available
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
   output_dir: "~/Downloads"

   # LLM API for long video chunk processing (optional)
   # Format: "openai" or "anthropic"
   llm_api_format: "openai"
   llm_api_key: "your_llm_api_key"
   llm_base_url: "https://api.openai.com"
   llm_model: "gpt-4o-mini"
   llm_timeout_sec: 180
   llm_max_retries: 3
   llm_backoff_sec: 1.5
   llm_stream: "auto"
   llm_chunk_recovery_attempts: 1
   llm_chunk_recovery_backoff_sec: 1.0
   ```

   #### YouTube "Sign in to confirm you're not a bot"

   `yt-transcript` now uses this policy by default:

   1. Start anonymously (do **not** read browser cookies up front)
   2. If `yt-dlp` returns `Sign in to confirm you're not a bot`, automatically retry (up to 3 attempts) with:
      - `--cookies-from-browser chrome`
   3. If that Chrome retry also fails, surface a clear error and tell you how to provide a `cookies.txt` file

   This means local desktop setups may recover automatically, while remote/container setups remain explicit and safe.

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
   > - LLM API config is only needed for long video chunk processing, or when bilingual translation is required.
   > - `llm_base_url` can be either a provider root URL or a `/v1` URL. The tool normalizes both.
   > - `llm_stream: "auto"` prefers SSE streaming when the provider supports it.
   > - `bash scripts/preflight.sh --require-llm` now performs a real low-cost LLM probe and reports latency.

### 🚀 Usage

#### As a Claude Skill

1. Place this directory in any Claude skills directory
2. Provide a YouTube link in your Claude conversation
3. Claude will automatically execute the transcription workflow

The scripts resolve `config.yaml` relative to the skill directory, so the skill is no longer tied to `~/.claude/skills/yt-transcript`.

#### Single Video Example

```
Please transcribe this video: https://www.youtube.com/watch?v=xxxxx
```

#### Multiple Videos (Batch Processing)

You can provide multiple links at once. They will be processed **serially** (one at a time) to ensure quality and context isolation:

```
Please transcribe these videos:
- https://www.youtube.com/watch?v=xxxxx
- https://www.youtube.com/watch?v=yyyyy
- https://www.youtube.com/watch?v=zzzzz
```

After completion, a summary table will be provided with status and output paths for each video.

### 📁 Project Structure

```
yt-transcript/
├── SKILL.md                 # Claude Skill workflow guide (main entry point)
├── SYSTEM_DESIGN.md         # Single authoritative system design document
├── workflows/               # Modular workflow files
├── prompts/                 # Single-task prompt templates
├── scripts/                 # Helper shell scripts
├── yt_transcript_utils.py   # Main Python entry; imports the two kernel layers directly
├── kernel/                 # Two-layer kernel package
│   ├── task_runtime/       # Generic task runtime layer
│   │   ├── runtime.py      # Ownership, command envelopes, telemetry append
│   │   ├── contracts.py    # Runtime contracts for task/run/action/artifact envelopes
│   │   ├── state.py        # Manifest/runtime persistence and control files
│   │   ├── controller.py   # Owned mutation and bounded control-loop helpers
│   │   └── telemetry.py    # Telemetry query and summary helpers
│   └── long_text/          # Long-text transformation layer
│       ├── glossary.py     # Glossary extraction and terminology checks
│       ├── semantic.py     # Semantic anchor extraction and checks
│       ├── contracts.py    # Control contracts and policy state
│       ├── autotune.py     # Chunk autotune and token-source summarization
│       ├── lifecycle.py    # Manifest lifecycle and resume-state helpers
│       ├── prompting.py    # Prompt assembly and chunking-context helpers
│       ├── llm.py          # LLM request loop and retry helpers
│       ├── processing.py   # Chunk-processing and replan execution loops
│       ├── chunking.py     # Chunking command surfaces
│       ├── merge.py        # Merge and chapter-plan command surfaces
│       └── execution.py    # Execution, resume, and replan command surfaces
├── tests/                   # Regression test suite
├── config.yaml              # Local config (gitignored)
├── config.example.yaml      # Config template
└── README.md                # This document
```

### 🏗️ Architecture & Design Overview

`README.md` is the operator-facing quickstart and command guide. `SYSTEM_DESIGN.md` is the single authoritative design document for the system architecture.

At a high level, `yt-transcript` is a local-first, script-first system that turns a YouTube URL into a Markdown article through:

- preflight and configuration checks
- metadata and subtitle availability detection
- subtitle download or Deepgram fallback transcription
- state synchronization and normalized document creation
- optimization planning
- short-path direct transformation or the long-text transformation subsystem
- final assembly and quality gates

The codebase mirrors that design through a two-layer kernel split: `kernel/task_runtime/*` owns generic long-running job control, while `kernel/long_text/*` owns long-text transformation behavior. `yt_transcript_utils.py` remains the main CLI and workflow façade, but it now delegates into those two layers directly.

Phase 1 of the runtime-upgrade path also introduces `kernel/task_runtime/contracts.py`, which normalizes task, run-state, action, artifact, and quality-report envelopes without changing the nominal workflow behavior.

The hardest internal subsystem is long-text transformation. It activates only when the planning layer determines that the input is long enough to require chunking, continuity control, consistency protection, verification, repair / replan, and deterministic merge.

[Read separate System Design Document](SYSTEM_DESIGN.md)

### 🔧 Preflight Modes

```bash
# Base checks only: subtitles / metadata workflows
bash scripts/preflight.sh

# Require Deepgram before audio transcription
bash scripts/preflight.sh --require-deepgram

# Require LLM config before long-video chunk processing
bash scripts/preflight.sh --require-llm
```

### 🧩 Structured Script Outputs

This project is script-first: helper commands emit machine-readable JSON on stdout so routing, validation, and execution decisions stay in code instead of drifting inside prompt prose:

- `scripts/download.sh "$URL" metadata`
- `scripts/download.sh "$URL" subtitle-info`
- `scripts/download.sh "$URL" subtitles`
- `scripts/download.sh "$URL" audio`
- `python3 yt_transcript_utils.py get-chapters "$URL"`
- `python3 yt_transcript_utils.py chunk-segments /tmp/${VIDEO_ID}_segments.json /tmp/${VIDEO_ID}_chunks --prompt structure_only`
- `python3 yt_transcript_utils.py chunk-document /tmp/${VIDEO_ID}_normalized_document.json /tmp/${VIDEO_ID}_chunks --prompt structure_only`
- `python3 yt_transcript_utils.py prepare-resume /tmp/${VIDEO_ID}_chunks --prompt structure_only`
- `python3 yt_transcript_utils.py build-chapter-plan /tmp/${VIDEO_ID}_chapters.json /tmp/${VIDEO_ID}_chunks /tmp/${VIDEO_ID}_chunks/chapter_plan.json`
- `python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage <stage>`
- `python3 yt_transcript_utils.py normalize-document /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt`

This keeps workflow logic in scripts instead of ad-hoc shell parsing inside the prompt instructions.

`plan-optimization` also emits the canonical chunk execution contract.

At the whole-project level, it is the routing boundary between source acquisition and text transformation. At the long-text subsystem level, it defines the execution contract that downstream chunk processing must follow:

- `operations[*].execution.supports_auto_replan`
- `operations[*].execution.recommended_cli_flags`
- `operations[*].execution.on_replan_required`

`normalize-document` materializes `/tmp/${VIDEO_ID}_normalized_document.json` from either raw text or timed `segments.json`, and `plan-optimization` auto-materializes it when source artifacts already exist.

For long-video chunking, `plan-optimization` now also emits a canonical `chunking` block; when normalization exists, `chunk-document` is the preferred driver and it keeps chunk boundary / continuity assumptions explicit in `manifest.json`.

The current design also has explicit resume semantics: `prepare-resume` repairs stale manifest state manually, while `process-chunks` runs the same repair step automatically before execution continues.

Current policy is intentional and explicit:

- `raw_path` chunk stages use `process-chunks --auto-replan`
- `processed_path` chunk stages do **not** auto-replan; if `replan_required=true`, stop and review manually

### 🧭 Intentional Design Decisions

- `bilingual` means English source text plus Chinese translation, not subtitle file merging
- If both English and Chinese subtitles exist, English remains the only source text for content generation
- `config.yaml` is intentionally limited to flat top-level key/value entries; nested or multi-line YAML is not supported
- YAML frontmatter values are always quoted on purpose to favor safe parsing over prettier formatting
- Markdown header text is escaped and link destinations are encoded so edge-case titles/channels do not break output structure
- `chunk-document` is now the canonical long-video chunking entrypoint when `normalized_document.json` exists; it auto-selects `segments` vs `text` but keeps `chunk-text` / `chunk-segments` available as compatible lower-level drivers
- `chunk-text` force-splits very long unpunctuated passages to stay within downstream LLM chunk budgets
- `transcribe-deepgram --output-segments` can emit time-aligned segments for downstream timed chunking + YouTube chapter mapping
- `chunk-segments` produces timed chunk manifests, and `build-chapter-plan` maps YouTube chapters onto chunk boundaries for `merge-content`
- `parse-vtt-segments` emits the same time-aligned segments format from subtitle VTT files
- `chunk-segments --chapters` can force chunk boundaries at YouTube chapter starts to reduce heading drift
- `chunk-text` now defaults to token-aware planning when `--prompt` is provided, while an explicit `--chunk-size` without `--prompt` keeps legacy character sizing for workflow compatibility
- prompt names are validated eagerly for chunk planning, so typos fail fast instead of silently falling back to generic budgets
- `process-chunks` now assigns prompt-specific `max_output_tokens` from the same planning budget instead of using one large shared default
- `manifest.json` now records explicit `plan.chunk_contract` and `plan.continuity`; `process-chunks` follows that plan-owned continuity policy instead of silently drifting with later config changes
- chunk execution now also has explicit resume semantics: stale `running` / missing-output checkpoints are repaired deterministically into `done` or `interrupted` before work resumes
- `process-chunks` also injects a short continuity context from the previous chunk (tail sentence + optional section title) without enabling body overlap, and chunk budgeting now reserves a small token allowance for that carry-over context
- `process-chunks` now treats transient gateway disconnects such as `Remote end closed connection without response` as retryable transport failures, and can auto-rerun suspiciously short / malformed chunk outputs before keeping a warning
- `process-chunks --dry-run` validates prompts, manifests, and chunk budgets without requiring live LLM credentials; actual execution still requires `llm_api_key`, `llm_base_url`, and `llm_model`
- `download.sh` now writes subtitle and audio artifacts into per-video isolated temp directories under `/tmp/${VIDEO_ID}_downloads/...` and exposes `download_dir` in JSON for deterministic selection and cleanup
- `download.sh subtitles` now requests the exact selected subtitle language codes, so regional variants such as `en-GB` / `zh-TW` work instead of being dropped by a hard-coded whitelist
- subtitle-driven workflows intentionally support only English-source bilingual mode and Chinese-source monolingual mode; when only other subtitle languages exist, the workflow should stop and fall back to audio transcription
- `plan-optimization` is the canonical short/long router with `< 1800s = short` and `>= 1800s = long`; the Quick Mode shortcut from `SKILL.md` is a narrower `< 900s` subset for subtitle-friendly videos
- `manifest.json` now separates immutable `plan` metadata from `runtime` state, and `process-chunks` records attempt-level telemetry (`attempt_logs`) in addition to chunk-level fields
- `process-chunks` no longer rewrites the current batch budget on the fly; when canary chunks or retry history show the plan is unhealthy, it aborts with `replan_required=true` so `replan-remaining` can generate a new plan for unfinished raw chunks
- `process-chunks --auto-replan` preserves that architecture boundary while automating the orchestration loop (`process -> replan-remaining -> resume`) for raw-path plans
- `run_kernel_command(...)` is the stable Python envelope API for kernel commands, and `python3 yt_transcript_utils.py --api-envelope ...` emits the same `yt_transcript.command_result/v1` envelope on the CLI without breaking legacy flat JSON output
- envelope-producing kernel commands append local `yt_transcript.telemetry_event/v1` records to `telemetry.jsonl` when a stable nearby sink path can be inferred
- `runtime.status` now distinguishes `completed`, `completed_with_errors`, and `aborted`, and raw-path replans remap existing `chapter_plan.json` chunk starts so merged chapter headers still land on valid chunk boundaries
- runtime token estimation remains heuristic by default; `test-token-count` / `preflight.sh --require-llm` probe provider-side token counting and clearly fall back to local estimates when unavailable
- `chunk_hard_cap_multiplier` is constrained to a conservative `1.0-2.0` range so misconfiguration cannot silently blow up chunk envelopes
- `preflight.sh` is staged so subtitle-only workflows do not require Deepgram or LLM credentials up front, while `--require-llm` now performs both reachability and token-count capability probes
- `transcribe-deepgram` is the only supported Deepgram entry point; split / merge behavior is owned by the Python utility
- `verify-quality` is a hard gate only when `hard_failures` is non-empty; `warnings` are advisory review signals

### 📘 Canonical Terms

- `bilingual`: English source text plus Chinese translation. It is not subtitle-file merging.
- `preflight base`: `bash scripts/preflight.sh` for metadata, subtitle inspection, and subtitle-driven paths.
- `preflight deepgram`: `bash scripts/preflight.sh --require-deepgram` immediately before audio transcription.
- `preflight llm`: `bash scripts/preflight.sh --require-llm` only when `plan-optimization` says long-video chunk processing requires it.
- `Deepgram unified entry`: `python3 yt_transcript_utils.py transcribe-deepgram ...`
- `quality gate`: `verify-quality` JSON where `hard_failures` means STOP and `warnings` means review before proceeding.

### 🧪 Validation Matrix

| Scenario | Minimum command sequence | Stop/go rule |
|----------|--------------------------|--------------|
| Short video with subtitles | `preflight.sh` → `download.sh metadata` → create state → `validate-state --stage metadata` → `download.sh subtitle-info` → `download.sh subtitles` → `validate-state --stage post-source` → optimize → `verify-quality` | Stop only if `validate-state` or `verify-quality` returns non-empty `hard_failures` |
| Video without usable subtitles | `preflight.sh` → `download.sh metadata` → `download.sh subtitle-info` → `preflight.sh --require-deepgram` → `transcribe-deepgram` → `validate-state --stage post-source` → optimize → `verify-quality` | Stop on any command failure or non-empty `hard_failures` |
| Long video | `validate-state --stage post-source` → `plan-optimization` → if `requires_llm_preflight=true`, run `preflight.sh --require-llm` → chunk → raw-path `process-chunks --auto-replan` → optional processed-path translation → merge → `verify-quality` → `validate-state --stage pre-assemble` | `warnings` alone do not block; `hard_failures` block |

### 🛠️ Minimum Commands

```bash
# 1. Base checks
bash scripts/preflight.sh

# 2. Metadata + subtitle availability
bash scripts/download.sh "$URL" metadata
bash scripts/download.sh "$URL" subtitle-info

# 3. State validation
python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage metadata
python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage post-source

# 4. Optimization planning
python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md

# 4b. Long-video raw chunk stages follow the plan contract
#     use process-chunks --auto-replan for raw_path,
#     but stop-and-review for processed_path replan_required

# 5. Audio fallback when needed
bash scripts/preflight.sh --require-deepgram
python3 yt_transcript_utils.py transcribe-deepgram "$AUDIO_FILE" --language "$LANGUAGE" --output-text "/tmp/${VIDEO_ID}_raw_text.txt"

# 6. Final quality gate
python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt
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
- 想复用执行顺序，直接看 `验证矩阵` 和 `最小命令集`

### ✨ 功能特点

- 🎯 **智能字幕获取**：优先使用 YouTube 官方/自动字幕
- 🎙️ **语音转录**：无字幕时自动使用 Deepgram Nova-2 转录
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
   output_dir: "~/Downloads"

   # 长视频 chunk 处理的 LLM API 配置（可选）
   # 格式: "openai" 或 "anthropic"
   llm_api_format: "openai"
   llm_api_key: "your_llm_api_key"
   llm_base_url: "https://api.openai.com"
   llm_model: "gpt-4o-mini"
   llm_timeout_sec: 180
   llm_max_retries: 3
   llm_backoff_sec: 1.5
   llm_stream: "auto"
   llm_chunk_recovery_attempts: 1
   llm_chunk_recovery_backoff_sec: 1.0
   ```

   > **注意**：
   > - `deepgram_api_key` 仅在没有可用字幕、需要音频转录时才必需。
   > - LLM API 配置仅用于长视频 chunk 处理，或需要双语翻译时。
   > - `llm_base_url` 可以填写服务根地址或带 `/v1` 的地址，工具会自动归一化。
   > - `llm_stream: "auto"` 会在 provider 支持时优先走流式响应。
   > - `bash scripts/preflight.sh --require-llm` 现在会执行一次低成本真实探活并输出延迟。

### 🚀 使用方法

#### 作为 Claude Skill 使用

1. 将此目录放入任意 Claude skills 目录
2. 在 Claude 对话中提供 YouTube 链接
3. Claude 将自动执行转录流程

脚本会相对于 skill 目录查找 `config.yaml`，不再强绑定 `~/.claude/skills/yt-transcript`。

#### 单个视频示例

```
请帮我转录这个视频：https://www.youtube.com/watch?v=xxxxx
```

#### 多个视频（批量处理）

可以一次提供多个链接，将**串行处理**（逐个处理）以确保质量和上下文隔离：

```
请帮我转录这些视频：
- https://www.youtube.com/watch?v=xxxxx
- https://www.youtube.com/watch?v=yyyyy
- https://www.youtube.com/watch?v=zzzzz
```

处理完成后会提供汇总表格，显示每个视频的状态和输出路径。

### 📁 项目结构

```
yt-transcript/
├── SKILL.md                 # Claude Skill 工作流程指南（主入口）
├── SYSTEM_DESIGN.md         # 系统设计唯一权威文档
├── workflows/               # 模块化工作流文件
├── prompts/                 # 单任务 Prompt 模板
├── scripts/                 # Shell 辅助脚本
├── yt_transcript_utils.py   # 主 Python 入口；现直接依赖两层 kernel 子包
├── kernel/                 # 两层 kernel 包
│   ├── task_runtime/       # 通用任务运行时层
│   │   ├── runtime.py      # ownership、command envelope、telemetry append
│   │   ├── state.py        # manifest/runtime 持久化与控制文件
│   │   ├── controller.py   # owned mutation 与 bounded control-loop 辅助
│   │   └── telemetry.py    # telemetry 查询与汇总辅助
│   └── long_text/          # 长文本变换层
│       ├── glossary.py     # glossary 提取与术语检查
│       ├── semantic.py     # semantic anchor 提取与检查
│       ├── contracts.py    # control contract 与 policy state
│       ├── autotune.py     # chunk autotune 与 token source 汇总
│       ├── lifecycle.py    # manifest 生命周期与 resume state 辅助
│       ├── prompting.py    # prompt 组装与 chunking context 辅助
│       ├── llm.py          # LLM 请求循环与重试辅助
│       ├── processing.py   # chunk 处理与 replan 执行循环
│       ├── chunking.py     # 分块命令表面
│       ├── merge.py        # merge 与 chapter-plan 命令表面
│       └── execution.py    # 执行、resume 与 replan 命令表面
├── tests/                   # 回归测试集
├── config.yaml              # 本地配置（已 gitignore）
├── config.example.yaml      # 配置模板
└── README.md                # 本文档
```

### 🏗️ 架构设计总览

`README.md` 是面向操作者的快速上手与命令指南，`SYSTEM_DESIGN.md` 是系统架构唯一的权威设计文档。

从整体上看，`yt-transcript` 是一个 local-first、script-first 的系统：它把 YouTube URL 通过以下阶段转换成 Markdown 文章：

- preflight 与配置检查
- metadata 与字幕可用性探测
- 字幕下载或 Deepgram 兜底转录
- 状态同步与标准化文档生成
- 优化计划制定
- 短路径直接变换或进入长文本变换子系统
- 最终装配与质量门禁

代码结构也按照这套设计拆成两层 kernel：`kernel/task_runtime/*` 负责通用长程任务控制，`kernel/long_text/*` 负责长文本变换行为。`yt_transcript_utils.py` 仍然是主 CLI 和 workflow façade，但现在会直接把职责委托给这两层。

其中最难的内部子系统是长文本变换。它只会在 planning 层判断输入足够长、必须进入 chunk 处理时激活，并负责 chunking、continuity、一致性保护、verification、repair / replan 与确定性 merge。

[阅读详细系统设计文档](SYSTEM_DESIGN.md)

### 🔧 预检模式

```bash
# 仅检查基础依赖：字幕 / metadata 工作流
bash scripts/preflight.sh

# 在音频转录前要求 Deepgram 可用
bash scripts/preflight.sh --require-deepgram

# 在长视频 chunk 处理前要求 LLM 配置完整
bash scripts/preflight.sh --require-llm
```

### 🧩 结构化脚本输出

这个项目是 script-first 的：辅助命令会在 stdout 输出可解析 JSON，让路由、校验与执行决策尽量留在代码里，而不是漂移到 prompt 文案中：

- `scripts/download.sh "$URL" metadata`
- `scripts/download.sh "$URL" subtitle-info`
- `scripts/download.sh "$URL" subtitles`
- `scripts/download.sh "$URL" audio`
- `python3 yt_transcript_utils.py get-chapters "$URL"`
- `python3 yt_transcript_utils.py chunk-segments /tmp/${VIDEO_ID}_segments.json /tmp/${VIDEO_ID}_chunks --prompt structure_only`
- `python3 yt_transcript_utils.py chunk-document /tmp/${VIDEO_ID}_normalized_document.json /tmp/${VIDEO_ID}_chunks --prompt structure_only`
- `python3 yt_transcript_utils.py prepare-resume /tmp/${VIDEO_ID}_chunks --prompt structure_only`
- `python3 yt_transcript_utils.py build-chapter-plan /tmp/${VIDEO_ID}_chapters.json /tmp/${VIDEO_ID}_chunks /tmp/${VIDEO_ID}_chunks/chapter_plan.json`
- `python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage <stage>`
- `python3 yt_transcript_utils.py normalize-document /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt`

这样 workflow 文档只保留调用顺序，具体判断逻辑下沉到脚本中。

`plan-optimization` 现在还会输出标准化的 chunk 执行契约。

在整个项目层面，它是 source acquisition 和 text transformation 之间的路由边界；在长文本子系统层面，它定义了后续 chunk 执行必须遵循的 execution contract：

- `operations[*].execution.supports_auto_replan`
- `operations[*].execution.recommended_cli_flags`
- `operations[*].execution.on_replan_required`

`normalize-document` 会基于 raw text 或带时间戳的 `segments.json` 物化 `/tmp/${VIDEO_ID}_normalized_document.json`；当源 artifact 已存在时，`plan-optimization` 也会自动完成这一步。

对于长视频分块，`plan-optimization` 现在还会输出显式的 `chunking` 契约；一旦 normalization 已存在，优先使用 `chunk-document`，并把 chunk 边界 / continuity 假设显式记录到 `manifest.json`。

当前设计还包含显式的 resume 语义：`prepare-resume` 用于手动修复 stale manifest，而 `process-chunks` 在继续执行前会自动做同样的修复。

当前约定是明确固定的：

- `raw_path` 阶段统一使用 `process-chunks --auto-replan`
- `processed_path` 阶段不做自动 replan；若返回 `replan_required=true`，必须先停下人工检查

### 🧭 设计上的刻意取舍

- `bilingual` 表示“英文源文本 + 中文翻译”，不是直接合并双字幕文件
- 当中英字幕同时存在时，内容生成仍只使用英文字幕作为源文本
- `config.yaml` 被刻意限制为扁平的顶层键值配置，不支持嵌套结构或多行 YAML
- YAML frontmatter 的值会统一加引号，优先保证解析安全，而不是追求最简洁的展示
- Markdown 头部里的标题/频道文本会做转义，链接目标会做编码，避免边界字符破坏结构
- `chunk-document` 现在是 `normalized_document.json` 已存在时的规范长视频分块入口；它会自动选择 `segments` 或 `text`，但仍保留 `chunk-text` / `chunk-segments` 作为兼容的低层驱动
- `chunk-text` 会对超长且缺少标点的段落做强制切分，并在提供 `--prompt` 时默认启用 token-aware 规划
- `transcribe-deepgram --output-segments` 可选输出带时间戳的对齐 segments，用于后续 timed chunk 与 YouTube 章节映射
- `chunk-segments` 基于 segments 生成带时间轴的 timed manifest；`build-chapter-plan` 可将 YouTube chapters 映射到 chunk 边界，供 `merge-content` 注入标题
- `parse-vtt-segments` 可从字幕 VTT 生成同格式的带时间戳 segments，用于 timed chunk 与章节映射
- `chunk-segments --chapters` 可选在 YouTube 章节起点强制切 chunk，减少章节标题漂移
- 如果只传显式 `--chunk-size` 而不传 `--prompt`，`chunk-text` 会继续按 legacy 字符大小解释，避免现有 workflow 被静默改变
- 分块阶段会提前校验 prompt 名称，避免因为 prompt 拼写错误而静默回退到通用预算
- `process-chunks` 现在按 prompt 预算单独设置 `max_output_tokens`，不再复用单一的大默认值
- `manifest.json` 现在会显式记录 `plan.chunk_contract` 与 `plan.continuity`；`process-chunks` 会遵循 plan-own 的 continuity 策略，而不是被后续 config 漂移静默改变
- chunk 执行现在也有显式 resume 语义：在继续执行前，stale 的 `running` / 缺失输出 checkpoint 会被确定性修复为 `done` 或 `interrupted`
- `process-chunks` 还会注入上一块的轻量 continuity context（尾句 + 可选 section title），但不会启用正文 overlap；同时分块预算也会为这段 carry-over context 预留一小段 token 成本
- `process-chunks` 现在会把 `Remote end closed connection without response` 这类瞬时网关断连视为可重试传输错误，并可在产出异常短/结构异常的 chunk 时自动重跑一轮，再决定是否保留 warning
- `manifest.json` 现在会把不可变 `plan` 和可变 `runtime` 状态分开，同时为每个 chunk 记录 `attempt_logs` 级别的请求观测数据
- `process-chunks` 不再在当前 batch 内偷偷改预算；如果 canary 或重试历史表明当前 plan 不健康，会以 `replan_required=true` 终止，并通过 `replan-remaining` 为剩余原始 chunk 生成新计划
- `process-chunks --auto-replan` 会在不破坏上述边界的前提下，自动编排 `process -> replan-remaining -> resume` 这一恢复链路（仅适用于 `raw_path` 计划）
- `runtime.status` 现在会区分 `completed` / `completed_with_errors` / `aborted`，而 raw replan 也会同步重映射已有 `chapter_plan.json` 的 chunk 起点，保证 merge 阶段的章节标题仍落在有效 chunk 边界上
- 运行时 token 估算默认仍是本地启发式 fallback；`test-token-count` / `preflight.sh --require-llm` 会探测 provider 级 token count，并在不可用时明确回退到 local estimate
- `chunk_hard_cap_multiplier` 会被限制在保守的 `1.0-2.0` 区间，避免配置失误把 chunk 包络静默放大
- `preflight.sh` 采用分层校验，确保只走字幕路径时不必预先配置 Deepgram 或 LLM 凭据；进入 `--require-llm` 时会同时做连通性和 token count 能力探测
- `transcribe-deepgram` 是唯一支持的 Deepgram 统一入口，分片与合并逻辑由 Python 工具统一负责
- `verify-quality` 只有在 `hard_failures` 非空时才阻断流程；`warnings` 仅用于人工复核提示

### 📘 术语口径

- `bilingual`：英文源文本 + 中文翻译，不是双字幕文件合并。
- `基础 preflight`：`bash scripts/preflight.sh`，用于 metadata、字幕探测和字幕路径。
- `Deepgram preflight`：`bash scripts/preflight.sh --require-deepgram`，仅在音频转录前执行。
- `LLM preflight`：只有当 `plan-optimization` 返回 long-video chunk 处理需要时，才执行 `bash scripts/preflight.sh --require-llm`。
- `Deepgram 统一入口`：`python3 yt_transcript_utils.py transcribe-deepgram ...`
- `质量门禁`：读取 `verify-quality` 的 JSON；`hard_failures` 表示必须 STOP，`warnings` 表示需要人工复核。

### 🧪 验证矩阵

| 场景 | 最小命令序列 | Stop/go 规则 |
|------|--------------|--------------|
| 有字幕短视频 | `preflight.sh` → `download.sh metadata` → 创建 state → `validate-state --stage metadata` → `download.sh subtitle-info` → `download.sh subtitles` → `validate-state --stage post-source` → 优化 → `verify-quality` | 只有 `validate-state` 或 `verify-quality` 返回非空 `hard_failures` 才停止 |
| 无可用字幕视频 | `preflight.sh` → `download.sh metadata` → `download.sh subtitle-info` → `preflight.sh --require-deepgram` → `transcribe-deepgram` → `validate-state --stage post-source` → 优化 → `verify-quality` | 任一命令失败或 `hard_failures` 非空都必须停止 |
| 长视频 | `validate-state --stage post-source` → `plan-optimization` → 若 `requires_llm_preflight=true` 则执行 `preflight.sh --require-llm` → 分块 → `raw_path` 阶段使用 `process-chunks --auto-replan` → 需要时执行 `processed_path` 翻译阶段 → 合并 → `verify-quality` → `validate-state --stage pre-assemble` | `warnings` 不自动阻断，`hard_failures` 阻断 |

### 🛠️ 最小命令集

```bash
# 1. 基础检查
bash scripts/preflight.sh

# 2. Metadata 与字幕可用性
bash scripts/download.sh "$URL" metadata
bash scripts/download.sh "$URL" subtitle-info

# 3. State 校验
python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage metadata
python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage post-source

# 4. 优化计划
python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md

# 4b. 长视频 raw chunk 阶段遵循 plan contract
#     raw_path 用 process-chunks --auto-replan,
#     processed_path 若出现 replan_required 则停下人工检查

# 5. 需要时走音频兜底
bash scripts/preflight.sh --require-deepgram
python3 yt_transcript_utils.py transcribe-deepgram "$AUDIO_FILE" --language "$LANGUAGE" --output-text "/tmp/${VIDEO_ID}_raw_text.txt"

# 6. 最终质量门禁
python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt
```

### 📄 许可证

MIT License

### 🔗 相关链接

- [Deepgram API 文档](https://developers.deepgram.com/)
- [yt-dlp 项目](https://github.com/yt-dlp/yt-dlp)
