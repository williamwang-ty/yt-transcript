# System Design (v4.0)

[English](#english) | [中文](#中文)

---

## English

> **Design Goal**: Enable highly reliable execution on **Weak Models** (e.g., 8B parameters) while maintaining advanced capabilities for SOTA models.

### Part 1: Design Philosophy

#### 1.1 The "Weak Model" Challenge

We identified three primary failure modes when running complex agentic skills on smaller models (Llama-3-8B, Gemini Flash, etc.):

1.  **Context Overflow**: Loading a 800+ line `SKILL.md` plus conversation history dilutes attention.
2.  **Instruction Interference**: When a prompt contains >3 distinct objectives (e.g., "Translate AND Format AND Fix Grammar"), weak models tend to ignore the secondary constraints.
3.  **State Amnesia**: During multi-step workflows, weak models often lose track of variable state (`VIDEO_ID`, `LANGUAGE`) after context switching.

#### 1.2 Core Design Patterns

**1.2.1 Modular Context Loading (The "Swap" Pattern)**

Instead of a monolithic instruction file, we split the skill into a lightweight router and specialized modules.

*   **Router (`SKILL.md`)**: < 400 lines. Contains only high-level decision trees (Binary choices: Yes/No).
*   **Modules (`workflows/*.md`)**: Loaded *on-demand*. The model never sees the "Subtitle Download" instructions while doing "Text Optimization".

*Impact: Reduces active context by ~40-50%.*

**1.2.2 Single-Task Prompts**

We enforce a hard rule: **One Prompt = One Primary Objective**.

*   `structure_only.md`: Only adds newlines and headers. Explicit instruction to NOT translate.
*   `translate_only.md`: Only translates. Explicit instruction to preserve structure.
*   `quick_cleanup.md`: Only adds punctuation.

*Impact: Drastically reduces "hallucination" and instruction skipping.*

**1.2.3 The "Context Sync" Handshake**

Every workflow file begins with a **Context Sync Section** that forces the model to read the State File (`/tmp/${VIDEO_ID}_state.md`) and extract variable values. This replaces memory-based recall with reliable disk-based verification.

**1.2.4 Workflow State Persistence**

To survive context loss or session interruptions, we maintain a lightweight **State File** (`/tmp/${VIDEO_ID}_state.md`).

*   **Mechanism**: The LLM reads this state at the start of every cognitive turn (~180 tokens overhead) and updates it only after irreversible actions (checkpoints).
*   **Checkpointing**:
    1.  **CREATE**: After fetching metadata.
    2.  **READ**: Before every decision/action.
    3.  **WRITE**: After key milestones (Download complete, Chunk processed, File saved).
    4.  **DELETE**: Upon successful cleanup.

*Impact: Ensures the Agent never "forgets" where it is or what rules to follow, even if the chat context is cleared.*

**1.2.5 Fail-Fast & "Safety Nets"**

Weak models tend to loop indefinitely when errors occur.

*   **Fail-Fast**: Instructions explicitly say "If step X fails, STOP. Do not retry."
*   **Safety Net**: In `quick_cleanup.md`, we added a trigger: "If text has ZERO punctuation, ignore minimal-change rules and fully punctuate."

---

### Part 2: Implementation Strategies

#### 2.1 Audio Splitting Strategy

To bypass API limits (25MB) and improve reliability, large audio files are split intelligently:

1. **Rough Split**: Calculate theoretical split points at 10MB intervals.
2. **Silence Detection**: Use FFmpeg to find silence intervals near rough split points.
3. **Smart Decision**: Choose the nearest silence point within 60s deviation.
4. **Fallback**: If no silence is found, force split at the rough point.

#### 2.2 Long-Text Processing Strategy

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

#### 2.3 Serial Processing for Multiple Links

When processing multiple YouTube links, this skill uses **serial processing** (one video at a time) instead of parallel:

| Approach | Feasibility | Reason |
|----------|-------------|--------|
| Parallel with Subagents | Not supported | Current Claude/Gemini Code architecture does not support spawning independent subagents with isolated context for general tasks |
| Parallel in single session | Not feasible | AI optimization step requires direct LLM involvement; cannot split into multiple parallel cognitive threads |
| Serial processing | ✅ Adopted | Process one video completely, clear context, then proceed to next |

---

### Part 3: Technical Reference

#### 3.1 Directory Structure

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

#### 3.2 Minimum Requirements

*   **Context**: 4k tokens active window
*   **Reasoning**: Elementary (Binary classification)
*   **Instruction Following**: Medium (Single-constraint following)
*   **Target Model Tier**: Llama-3-8B (Instruct) / GPT-3.5 Turbo level.

---

## 中文

> **设计目标**: 使 Skill 能够在 **弱模型**（如 8B 参数）上高度可靠地运行，同时为 SOTA 模型保留高级能力。

### 第一部分：设计哲学

#### 1.1 "弱模型"的挑战

我们在较小模型（Llama-3-8B, Gemini Flash 等）上运行复杂的 Agent Skill 时，识别出三种主要故障模式：

1.  **上下文溢出 (Context Overflow)**: 加载 800+ 行的 `SKILL.md` 加上对话历史会稀释模型的注意力。
2.  **指令干扰 (Instruction Interference)**: 当一个 Prompt 包含 >3 个不同的目标（例如"翻译"且"格式化"且"修复语法"）时，弱模型倾向于忽略次要约束。
3.  **状态失忆 (State Amnesia)**: 在多步骤工作流中，弱模型在切换上下文后经常丢失变量状态（如 `VIDEO_ID`, `LANGUAGE`）。

#### 1.2 核心设计模式

**1.2.1 模块化上下文加载 ("Swap" Pattern)**

我们将 Skill 拆分为一个轻量级的路由（Router）和专门的模块（Modules），而不是使用单体指令文件。

*   **Router (`SKILL.md`)**: < 400 行。仅包含高级决策树（二元选择：是/否）。
*   **Modules (`workflows/*.md`)**: *按需*加载。模型在执行"文本优化"时永远不会看到"字幕下载"的指令。

*影响：减少约 40-50% 的活跃上下文。*

**1.2.2 单任务 Prompts**

我们强制执行一条硬性规则：**一个 Prompt = 一个主要目标**。

*   `structure_only.md`: 仅添加换行和标题。显式指令**不**翻译。
*   `translate_only.md`: 仅翻译。显式指令保留结构。
*   `quick_cleanup.md`: 仅添加标点。

*影响：大幅减少"幻觉"和指令跳过。*

**1.2.3 "Context Sync" 握手**

每个 Workflow 文件都以 **Context Sync 部分** 开头，强制模型读取状态文件（`/tmp/${VIDEO_ID}_state.md`）并提取变量值。这用可靠的基于磁盘的验证取代了基于记忆的回想。

**1.2.4 工作流状态持久化**

为了在上下文丢失或会话中断后存活，我们维护一个轻量级的 **状态文件** (`/tmp/${VIDEO_ID}_state.md`)。

*   **机制**: LLM 在每个认知回合开始时读取此状态（约 180 tokens 开销），并仅在不可逆操作（检查点）后更新它。
*   **检查点设计**:
    1.  **CREATE**: 获取 Metadata 后创建。
    2.  **READ**: 每次决策/行动前读取。
    3.  **WRITE**: 关键里程碑后写入（下载完成、分块处理完、文件保存）。
    4.  **DELETE**: 清理完成后删除。

*价值: 确保 Agent 即使在聊天上下文被清空的情况下，也永远不会"忘记"当前进度或应遵循的规则。*

**1.2.5 Fail-Fast & "安全网"**

弱模型在出错时倾向于无限循环。

*   **Fail-Fast**: 指令显式说明 "如果步骤 X 失败，停止 (STOP)。不要重试。"
*   **Safety Net**: 在 `quick_cleanup.md` 中，我们添加了一个触发器："如果文本包含零标点，忽略最小修改规则并完全添加标点。"

---

### 第二部分：实现策略

#### 2.1 音频分割策略

为规避 API 限制（25MB）并提高稳定性，对大音频文件进行智能分割：

1. **粗略分割**：按 10MB 间隔计算理论分割点。
2. **静音检测**：使用 FFmpeg 检测粗略点附近的静音区间。
3. **智能决策**：选择 60秒偏差范围内最近的静音点作为实际分割位置。
4. **兜底机制**：若范围内无静音，则在粗略点强制分割。

#### 2.2 长文本处理策略

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

#### 2.3 多链接串行处理

处理多个 YouTube 链接时，本工具采用**串行处理**（逐个处理）而非并行：

| 方案 | 可行性 | 原因 |
|------|--------|------|
| 并行 + Subagent | 不支持 | 当前 Claude/Gemini Code 架构不支持为通用任务创建具有独立上下文的子智能体 |
| 单会话内并行 | 不可行 | AI 优化步骤需要 LLM 直接参与，无法"分身"成多个并行认知线程 |
| 串行处理 | ✅ 采用 | 完整处理一个视频后清理上下文，再处理下一个 |

---

### 第三部分：技术参考

#### 3.1 目录结构

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

#### 3.2 最低要求

*   **上下文**: 4k tokens 活跃窗口
*   **推理**: 初级 (二元分类)
*   **指令遵循**: 中等 (单一约束遵循)
*   **目标模型层级**: Llama-3-8B (Instruct) / GPT-3.5 Turbo 级别。
