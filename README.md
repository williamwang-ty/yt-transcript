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

[Read separate System Design Document](SYSTEM_DESIGN.md)

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

[阅读详细系统设计文档](SYSTEM_DESIGN.md)

### 📄 许可证

MIT License

### 🔗 相关链接

- [Deepgram API 文档](https://developers.deepgram.com/)
- [yt-dlp 项目](https://github.com/yt-dlp/yt-dlp)
