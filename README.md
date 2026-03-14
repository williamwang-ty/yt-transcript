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
   ```

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
├── workflows/               # Modular workflow files
├── prompts/                 # Single-task prompt templates
├── scripts/                 # Helper shell scripts
├── yt_transcript_utils.py   # Python utilities
├── config.yaml              # Local config (gitignored)
├── config.example.yaml      # Config template
└── README.md                # This document
```

### 🏗️ Architecture & Design Philosophy (v4.0)

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

The helper scripts now emit machine-readable JSON on stdout:

- `scripts/download.sh "$URL" metadata`
- `scripts/download.sh "$URL" subtitle-info`
- `scripts/download.sh "$URL" subtitles`
- `scripts/download.sh "$URL" audio`
- `python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage <stage>`
- `python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt`

This keeps workflow logic in scripts instead of ad-hoc shell parsing inside the prompt instructions.

### 🧭 Intentional Design Decisions

- `bilingual` means English source text plus Chinese translation, not subtitle file merging
- If both English and Chinese subtitles exist, English remains the only source text for content generation
- `config.yaml` is intentionally limited to flat top-level key/value entries; nested or multi-line YAML is not supported
- YAML frontmatter values are always quoted on purpose to favor safe parsing over prettier formatting
- Markdown header text is escaped and link destinations are encoded so edge-case titles/channels do not break output structure
- `chunk-text` force-splits very long unpunctuated passages to stay within downstream LLM chunk budgets
- `preflight.sh` is staged so subtitle-only workflows do not require Deepgram or LLM credentials up front, while `--require-llm` now performs a real probe
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
| Long video | `validate-state --stage post-source` → `plan-optimization` → if `requires_llm_preflight=true`, run `preflight.sh --require-llm` → chunk / process / merge → `verify-quality` → `validate-state --stage pre-assemble` | `warnings` alone do not block; `hard_failures` block |

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
├── workflows/               # 模块化工作流文件
├── prompts/                 # 单任务 Prompt 模板
├── scripts/                 # Shell 辅助脚本
├── yt_transcript_utils.py   # Python 工具脚本
├── config.yaml              # 本地配置（已 gitignore）
├── config.example.yaml      # 配置模板
└── README.md                # 本文档
```

### 🏗️ 架构设计与设计哲学 (v4.0)

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

辅助脚本现在会在 stdout 输出可解析 JSON：

- `scripts/download.sh "$URL" metadata`
- `scripts/download.sh "$URL" subtitle-info`
- `scripts/download.sh "$URL" subtitles`
- `scripts/download.sh "$URL" audio`
- `python3 yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage <stage>`
- `python3 yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md`
- `python3 yt_transcript_utils.py verify-quality /tmp/${VIDEO_ID}_optimized.txt --raw-text /tmp/${VIDEO_ID}_raw_text.txt`

这样 workflow 文档只保留调用顺序，具体判断逻辑下沉到脚本中。

### 🧭 设计上的刻意取舍

- `bilingual` 表示“英文源文本 + 中文翻译”，不是直接合并双字幕文件
- 当中英字幕同时存在时，内容生成仍只使用英文字幕作为源文本
- `config.yaml` 被刻意限制为扁平的顶层键值配置，不支持嵌套结构或多行 YAML
- YAML frontmatter 的值会统一加引号，优先保证解析安全，而不是追求最简洁的展示
- Markdown 头部里的标题/频道文本会做转义，链接目标会做编码，避免边界字符破坏结构
- `chunk-text` 会对超长且缺少标点的段落做强制切分，并可按 prompt 自动选择更保守的 chunk 大小
- `preflight.sh` 采用分层校验，确保只走字幕路径时不必预先配置 Deepgram 或 LLM 凭据
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
| 长视频 | `validate-state --stage post-source` → `plan-optimization` → 若 `requires_llm_preflight=true` 则执行 `preflight.sh --require-llm` → 分块 / 处理 / 合并 → `verify-quality` → `validate-state --stage pre-assemble` | `warnings` 不自动阻断，`hard_failures` 阻断 |

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
