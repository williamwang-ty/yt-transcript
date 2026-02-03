# yt-transcript

[English](#english) | [中文](#中文)

---

## English

Transcribe YouTube videos into formatted Markdown articles. Supports subtitle download or Deepgram speech-to-text (with multi-speaker recognition).

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
   ```

### 🚀 Usage

#### As a Claude Skill

1. Place this directory in `~/.claude/skills/` or your Claude skills directory
2. Provide a YouTube link in your Claude conversation
3. Claude will automatically execute the transcription workflow

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

> **Design Goal**: Enable highly reliable execution on **Weak Models** (e.g., 8B parameters) while maintaining advanced capabilities for SOTA models.

#### 1. The "Weak Model" Challenge

We identified three primary failure modes when running complex agentic skills on smaller models (Llama-3-8B, Gemini Flash, etc.):

1.  **Context Overflow**: Loading a 800+ line `SKILL.md` plus conversation history dilutes attention.
2.  **Instruction Interference**: When a prompt contains >3 distinct objectives (e.g., "Translate AND Format AND Fix Grammar"), weak models tend to ignore the secondary constraints.
3.  **State Amnesia**: During multi-step workflows, weak models often lose track of variable state (`VIDEO_ID`, `LANGUAGE`) after context switching.

#### 2. Core Design Patterns

To address these, we implemented the following patterns:

**2.1 Modular Context Loading (The "Swap" Pattern)**
Instead of a monolithic instruction file, we split the skill into a lightweight router and specialized modules.

*   **Router (`SKILL.md`)**: < 400 lines. Contains only high-level decision trees (Binary choices: Yes/No).
*   **Modules (`workflows/*.md`)**: Loaded *on-demand*. The model never sees the "Subtitle Download" instructions while doing "Text Optimization".

*Impact: Reduces active context by ~40-50%.*

**2.2 Single-Task Prompts**
We enforce a hard rule: **One Prompt = One Primary Objective**.

*   `structure_only.md`: Only adds newlines and headers. explicit instruction to NOT translate.
*   `translate_only.md`: Only translates. Explicit instruction to preserve structure.
*   `quick_cleanup.md`: Only adds punctuation.

*Impact: Drastically reduces "hallucination" and instruction skipping.*

**2.3 The "Context Recap" Handshake**
Every workflow file begins with a **Variable Confirmation Section** that forces the model to "ground" itself before executing new instructions, combating state amnesia.

**2.4 Fail-Fast & "Safety Nets"**
Weak models tend to loop indefinitely when errors occur.
*   **Fail-Fast**: Instructions explicitly say "If step X fails, STOP. Do not retry."
*   **Safety Net**: In `quick_cleanup.md`, we added a trigger: "If text has ZERO punctuation, ignore minimal-change rules and fully punctuate."

#### 3. Directory Structure Role

```
yt-transcript/
├── SKILL.md                # The Brain (Router)
├── workflows/              # The Limbs (Procedural Knowledge)
│   ├── subtitle_download.md
│   ├── deepgram_transcribe.md
│   └── text_optimization.md
├── prompts/                # The Voice (Generation Templates)
│   ├── structure_only.md
│   ├── translate_only.md
│   └── quick_cleanup.md
├── scripts/                # The Hands (Tool Execution)
│   ├── preflight.sh
│   ├── download.sh
│   └── cleanup.sh
└── yt_transcript_utils.py  # Python Utilities
```

#### 4. Minimum Requirements

*   **Context**: 4k tokens active window
*   **Reasoning**: Elementary (Binary classification)
*   **Instruction Following**: Medium (Single-constraint following)
*   **Target Model Tier**: Llama-3-8B (Instruct) / GPT-3.5 Turbo level.

#### Audio Splitting Strategy

To bypass API limits (25MB) and improve reliability, large audio files are split intelligently:
1. **Rough Split**: Calculate theoretical split points at 10MB intervals.
2. **Silence Detection**: Use FFmpeg to find silence intervals near rough split points.
3. **Smart Decision**: Choose the nearest silence point within 60s deviation.
4. **Fallback**: If no silence is found, force split at the rough point.

#### Long-Text Processing Strategy (New!)

To handle arbitrarily long videos (e.g., >2 hours) without hitting LLM context limits, we use a **Map-Reduce inspired hybrid pipeline**:

1.  **Structural Chunking (Script)**:
    - The `chunk-text` command splits raw text into semantic chunks (~8000 chars) based on sentence boundaries.
    - Uses an idempotent `manifest.json` to track processing status, allowing resumability.

2.  **Two-Stage Chapter Planning**:
    - **Priority 1**: Use YouTube Chapters if available (via `get-chapters`).
    - **Priority 2**: If no chapters, the LLM first generates summaries for each chunk, then plans a global chapter structure based on summaries.

3.  **Stateless Translation**:
    - Each chunk is translated independently by the LLM without needing global context.
    - Script (`merge-content`) handles the re-assembly and injection of chapter headers.

#### Why Serial Processing for Multiple Links?

When processing multiple YouTube links, this skill uses **serial processing** (one video at a time) instead of parallel:

| Approach | Feasibility | Reason |
|----------|-------------|--------|
| Parallel with Subagents | Not supported | Current Claude/Gemini Code architecture does not support spawning independent subagents with isolated context for general tasks |
| Parallel in single session | Not feasible | AI optimization step requires direct LLM involvement; cannot split into multiple parallel cognitive threads |
| Serial processing | Adopted | Process one video completely, clear context, then proceed to next |

### 📄 License

MIT License

### 🔗 Links

- [Deepgram API Docs](https://developers.deepgram.com/)
- [yt-dlp Project](https://github.com/yt-dlp/yt-dlp)

---

## 中文

将 YouTube 视频转录为格式化的 Markdown 文章。支持字幕下载或 Deepgram 语音转录（包含多角色识别）。

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
   ```

### 🚀 使用方法

#### 作为 Claude Skill 使用

1. 将此目录放入 `~/.claude/skills/` 或你的 Claude skills 目录
2. 在 Claude 对话中提供 YouTube 链接
3. Claude 将自动执行转录流程

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

> **设计目标**: 使 Skill 能够在 **弱模型**（如 8B 参数）上高度可靠地运行，同时为 SOTA 模型保留高级能力。

#### 1. "弱模型"的挑战

我们在较小模型（Llama-3-8B, Gemini Flash 等）上运行复杂的 Agent Skill 时，识别出三种主要故障模式：

1.  **上下文溢出 (Context Overflow)**: 加载 800+ 行的 `SKILL.md` 加上对话历史会稀释模型的注意力。
2.  **指令干扰 (Instruction Interference)**: 当一个 Prompt 包含 >3 个不同的目标（例如“翻译”且“格式化”且“修复语法”）时，弱模型倾向于忽略次要约束。
3.  **状态失忆 (State Amnesia)**: 在多步骤工作流中，弱模型在切换上下文后经常丢失变量状态（如 `VIDEO_ID`, `LANGUAGE`）。

#### 2. 核心设计模式

为了解决这些问题，我们实施了以下模式：

**2.1 模块化上下文加载 ("Swap" Pattern)**
我们将 Skill 拆分为一个轻量级的路由（Router）和专门的模块（Modules），而不是使用单体现成文件。

*   **Router (`SKILL.md`)**: < 400 行。仅包含高级决策树（二元选择：是/否）。
*   **Modules (`workflows/*.md`)**: *按需*加载。模型在执行“文本优化”时永远不会看到“字幕下载”的指令。

*影响：减少约 40-50% 的活跃上下文。*

**2.2 单任务 Prompts**
我们强制执行一条硬性规则：**一个 Prompt = 一个主要目标**。

*   `structure_only.md`: 仅添加换行和标题。显式指令**不**翻译。
*   `translate_only.md`: 仅翻译。显式指令保留结构。
*   `quick_cleanup.md`: 仅添加标点。

*影响：大幅减少“幻觉”和指令跳过。*

**2.3 "Context Recap" 握手**
每个 Workflow 文件都以 **变量确认部分** 开头，强制模型在执行新指令前先“落地”自身状态，以对抗状态失忆。

**2.4 Fail-Fast & "安全网"**
弱模型在出错时倾向于无限循环。
*   **Fail-Fast**: 指令显式说明 "如果步骤 X 失败，停止 (STOP)。不要重试。"
*   **Safety Net**: 在 `quick_cleanup.md` 中，我们添加了一个触发器："如果文本包含零标点，忽略最小修改规则并完全添加标点。"

#### 3. 目录结构角色

```
yt-transcript/
├── SKILL.md                # 大脑 (路由)
├── workflows/              # 四肢 (过程知识)
│   ├── subtitle_download.md
│   ├── deepgram_transcribe.md
│   └── text_optimization.md
├── prompts/                # 声音 (生成模板)
│   ├── structure_only.md
│   ├── translate_only.md
│   └── quick_cleanup.md
├── scripts/                # 双手 (工具执行)
│   ├── preflight.sh
│   ├── download.sh
│   └── cleanup.sh
└── yt_transcript_utils.py  # Python 工具脚本
```

#### 4. 最低要求

*   **上下文**: 4k tokens 活跃窗口
*   **推理**: 初级 (二元分类)
*   **指令遵循**: 中等 (单一约束遵循)
*   **目标模型层级**: Llama-3-8B (Instruct) / GPT-3.5 Turbo 级别。

#### 音频分割策略

为规避 API 限制（25MB）并提高稳定性，对大音频文件进行智能分割：
1. **粗略分割**：按 10MB 间隔计算理论分割点。
2. **静音检测**：使用 FFmpeg 检测粗略点附近的静音区间。
3. **智能决策**：选择 60秒偏差范围内最近的静音点作为实际分割位置。
4. **兜底机制**：若范围内无静音，则在粗略点强制分割。

#### 长文本处理策略（新增！）

为了在不突破 LLM 上下文限制的情况下处理超长视频（如 >2小时），我们采用了 **Map-Reduce 思想的混合流水线**：

1.  **结构化分块（脚本）**：
    - `chunk-text` 命令按句子边界将原始文本切分为语义块（~8000字符）。
    - 使用幂等的 `manifest.json` 追踪状态，支持断点续传。

2.  **两阶段章节规划**：
    - **优先级 1**：如果有 YouTube 章节（通过 `get-chapters` 获取），直接使用。
    - **优先级 2**：无章节时，LLM 先对每个块生成摘要，再基于摘要规划全局章节结构。

3.  **无状态翻译**：
    - 每个文本块由 LLM 独立翻译，不需要全局上下文。
    - 最终由脚本（`merge-content`）负责按顺序组装并插入章节标题。

#### 为什么多链接采用串行处理？

处理多个 YouTube 链接时，本工具采用**串行处理**（逐个处理）而非并行：

| 方案 | 可行性 | 原因 |
|------|--------|------|
| 并行 + Subagent | 不支持 | 当前 Claude/Gemini Code 架构不支持为通用任务创建具有独立上下文的子智能体 |
| 单会话内并行 | 不可行 | AI 优化步骤需要 LLM 直接参与，无法"分身"成多个并行认知线程 |
| 串行处理 | 采用 | 完整处理一个视频后清理上下文，再处理下一个 |

### 📄 许可证

MIT License

### 🔗 相关链接

- [Deepgram API 文档](https://developers.deepgram.com/)
- [yt-dlp 项目](https://github.com/yt-dlp/yt-dlp)
