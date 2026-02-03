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
├── SKILL.md                 # Claude Skill workflow guide
├── yt_transcript_utils.py   # Utility scripts (VTT parsing, Deepgram processing, etc.)
├── config.yaml              # Local config (not uploaded)
├── config.example.yaml      # Config template
├── LICENSE                  # MIT License
└── README.md                # This document
```

### 🏗️ Architecture

#### Design Principles

| Dimension | Scripted (Fixed) | LLM (Flexible) |
|-----------|------------------|----------------|
| **Determinism** | Predictable input→output | Requires context understanding |
| **Rule-based** | Fixed algorithms | Needs judgment, inference |
| **Complexity** | Complex code prone to errors | Simple rules or flexibility needed |
| **Dependencies** | Only input parameters | Relies on global context/history |

#### Hybrid Architecture

**Script Processing (yt_transcript_utils.py)**:
- `parse-vtt`: VTT subtitle parsing - pure format conversion, deterministic
- `process-deepgram`: Deepgram result processing - complex regex, needs precision
- `sanitize-filename`: Filename cleaning - filesystem rules are fixed
- `split-audio`: Smart audio splitting - uses FFmpeg silence detection to split large audio files (>10MB) at natural pauses

**LLM Processing**:
- Language detection: Combines title, description, channel name
- AI text optimization: Punctuation, paragraphing, error correction
- Bilingual translation: Requires language capabilities
- Formatting decisions: Speaker labels, section titles

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
├── SKILL.md                 # Claude Skill 工作流程指南
├── yt_transcript_utils.py   # 工具脚本（VTT解析、Deepgram处理等）
├── config.yaml              # 本地配置（不上传）
├── config.example.yaml      # 配置模板
├── LICENSE                  # MIT 许可证
└── README.md                # 本文档
```

### 🏗️ 架构设计

#### 设计原则

| 维度 | 适合固化为脚本 | 适合 LLM 灵活处理 |
|------|----------------|-------------------|
| **确定性** | 输入→输出完全可预测 | 需要理解上下文、语义 |
| **规则性** | 基于固定规则/算法 | 需要判断、推理 |
| **复杂度** | 代码复杂易出错 | 规则简单或需要灵活性 |
| **依赖** | 仅依赖输入参数 | 依赖全局上下文/对话历史 |

#### 混合架构

**脚本处理（yt_transcript_utils.py）**：
- `parse-vtt`：VTT 字幕解析 - 纯格式转换，规则确定
- `process-deepgram`：Deepgram 结果处理 - 正则复杂，需精确执行
- `sanitize-filename`：文件名清理 - 文件系统规则固定
- `split-audio`：智能音频分割 - 使用 FFmpeg 静音检测在自然停顿处分割大音频文件（>10MB）

**LLM 处理**：
- 语言判断：综合标题、描述、频道名判断
- AI 文本优化：添加标点、分段分章节、纠错
- 双语翻译：需要语言能力
- 格式化决策：说话者标识、章节标题

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
