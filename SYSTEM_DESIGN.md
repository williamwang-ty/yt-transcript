# yt-transcript System Design / yt-transcript 系统设计

## English Version

## 1. Document Positioning

This document explains the **system design of the entire `yt-transcript` project**.

`README.md` is the operator-facing quickstart and command guide. `SYSTEM_DESIGN.md` explains architectural rationale, boundaries, and system-level trade-offs.

Its purpose is not to describe which function to change next, which field to add, or which module to split. It is not a coding spec and not a stage-by-stage implementation checklist.

This document answers two levels of design questions at the same time:

- **Project level**: How does `yt-transcript` work as a complete end-to-end system that turns a YouTube video into a formatted Markdown article?
- **Subsystem level**: Inside that end-to-end system, how is the hardest part — long-text transformation — designed?

So this document is intentionally structured around **the whole project first**, and then **the long-text transformation subsystem** as one core chapter inside it.

For this repository, `SYSTEM_DESIGN.md` is the single authoritative design document for this capability.

---

## 2. What the Project Is Actually Building

`yt-transcript` is not only a subtitle downloader, and it is not only a long-text LLM pipeline.

It is a **local-first, script-first transcript production system** that takes a YouTube URL and produces a formatted Markdown article.

That full process includes:

- environment and credential checks
- metadata acquisition
- subtitle availability detection
- source-path routing
- subtitle download or Deepgram transcription fallback
- state synchronization and normalization
- text optimization
- long-text chunk processing when needed
- final assembly
- quality gates and runtime inspection

In this full system, **long-text transformation is a core subsystem**, but it is still only one part of the whole project.

---

## 3. Project-Level Problems the System Must Solve

### 3.1 Source availability is uncertain

A YouTube video may have:

- usable English subtitles
- only Chinese subtitles
- auto-generated subtitles of uneven quality
- no usable subtitles at all
- audio that must be sent to Deepgram for transcription

So the system cannot assume that one acquisition path always works.

### 3.2 Routing decisions must be reliable

The system must make operational decisions such as:

- subtitle path or Deepgram path
- short-video optimization path or long-video path
- whether LLM preflight is required
- whether bilingual transformation is needed
- whether text should be processed from plain text or timed segments

These decisions should not depend on fragile prompt interpretation alone.

### 3.3 Text optimization is model-sensitive

The project does not stop at raw transcription. It must turn raw transcript-like text into readable article-like Markdown through tasks such as:

- structure restoration
- cleanup
- translation
- rewriting
- chapter injection

These are the places where LLMs add the most value, but also where weak models and overloaded prompts fail most easily.

### 3.4 Long videos break naive workflows

Once a source transcript becomes long enough, naive workflows break down:

- the text exceeds the model context window
- one-shot prompts become too expensive and unstable
- interruptions become likely
- consistency across chunks becomes difficult to preserve
- the final merge becomes risky if boundaries are unclear

This is why long-text transformation becomes the hardest subsystem in the project.

### 3.5 The project must remain operable, not just runnable

A working demo is not enough. Operators need to know:

- what path the system chose
- what state it is currently in
- what artifacts already exist
- whether a run can be resumed
- whether a failure is local, retryable, or plan-level

So the project must be designed as an operable local system, not just a chain of ad-hoc commands.

---

## 4. Project-Level Design Goals

The project-level design goal can be summarized as:

> Turn YouTube-to-Markdown transcript production into a reliable local system whose routing, state, recovery, and quality gates are controlled by code, while using LLMs only where text transformation actually benefits from them.

More concretely, the project aims for:

- **Reliable end-to-end flow**: the full path from URL to final Markdown should be stable and inspectable
- **Script-first routing**: high-risk branching logic should live in deterministic scripts and structured JSON outputs
- **Local-first control**: state, artifacts, telemetry, and recovery should remain inspectable on disk
- **Model-tolerant execution**: stronger models can improve quality, but weaker models should still survive the workflow because prompts stay narrow and code owns orchestration
- **Layered quality gates**: stop/go decisions should rely on validation rather than optimism
- **Separation of concerns**: source acquisition, planning, text transformation, merge, and verification should not collapse into one opaque step
- **Extensible architecture**: the long-text subsystem can grow more capable without forcing a redesign of the entire project

---

## 5. Overall System Design

At the whole-project level, the design can be summarized as:

```text
YouTube URL
  -> preflight and configuration checks
  -> metadata and subtitle availability detection
  -> source-path routing
  -> subtitle download OR Deepgram transcription
  -> state synchronization and normalized document creation
  -> optimization planning
  -> short-path direct transformation OR long-text transformation subsystem
  -> final assembly
  -> quality verification
  -> final Markdown output + inspectable artifacts
```

The key architectural idea is this:

> The project does not treat transcription, transformation, and assembly as one prompt. It treats them as a staged system in which deterministic scripts own routing and state, and LLMs are inserted only into the text-transformation stages.

---

## 6. Whole-Project Layered Design

### 6.1 Environment and Preflight Layer

#### Problem to solve

The project depends on multiple external capabilities, but not every workflow path requires all of them.

For example:

- subtitle-only flows do not need Deepgram credentials
- long-video chunk processing may require LLM connectivity and token counting
- basic metadata checks should work before expensive or high-friction dependencies are required

#### Solution

The system uses layered preflight modes:

- base preflight for metadata / subtitle workflows
- Deepgram preflight only before audio transcription
- LLM preflight only before long-video chunk execution

#### Design value

- reduces unnecessary setup friction
- keeps cheap paths cheap
- makes dependency checks match the actual execution path

### 6.2 Source Discovery and Routing Layer

#### Problem to solve

The system must decide which source path to take without relying on fuzzy model judgment.

#### Solution

Source discovery is handled through structured script outputs such as:

- metadata detection
- subtitle availability inspection
- source language selection
- chapter discovery when relevant

These machine-readable outputs drive routing decisions instead of prose-only prompt reasoning.

#### Design value

- path selection becomes deterministic and inspectable
- workflows stay declarative
- operators can see why a particular path was chosen

### 6.3 Source Acquisition Layer

#### Problem to solve

The project must support more than one source acquisition mode, while still producing artifacts the later stages can consume uniformly.

#### Solution

The system supports two primary source paths:

- **subtitle path**: download and parse usable subtitles
- **audio fallback path**: transcribe audio through Deepgram when subtitles are unavailable or insufficient

Both paths are designed to produce normalized downstream artifacts such as raw text and, when available, timed segments.

#### Design value

- multiple acquisition strategies remain compatible with one downstream architecture
- later stages do not need to know whether text came from subtitles or audio transcription
- timed information remains available for chapter-aware chunking when possible

### 6.4 Canonical State and Normalization Layer

#### Problem to solve

An end-to-end transcript system becomes fragile if it depends on transient chat memory or loosely remembered shell variables.

#### Solution

The project persists explicit state and normalizes source artifacts into canonical document forms.

At a design level, this means the system maintains:

- operator-visible state
- machine-usable normalized document artifacts
- stable references to source, intermediate, and final outputs

The project therefore treats state and normalized documents as first-class system objects, not incidental files.

#### Design value

- the workflow becomes resumable
- downstream planning does not need to reinterpret raw source formats every time
- both humans and scripts can inspect what the system currently believes to be true

### 6.5 Optimization Planning Layer

#### Problem to solve

The project must decide how text optimization should proceed based on document size, source form, and required operations.

#### Solution

The system introduces an explicit planning stage that decides:

- whether the job is short-path or long-path
- which operations are required
- whether chunking is needed
- whether LLM preflight is required
- which execution contract later stages should follow

This planning stage is the boundary between “we have source text” and “we know how to transform it safely.”

#### Design value

- execution behavior is derived from a plan instead of accidental defaults
- the long-text subsystem only activates when it is actually needed
- the project can maintain different execution paths without turning into a maze of hidden conditions

### 6.6 Text Optimization Layer

#### Problem to solve

Text optimization is where raw transcript-like input is turned into readable Markdown output, but it is also the place where LLM instability matters most.

#### Solution

The project keeps text optimization narrow and path-aware:

- shorter inputs can use simpler direct prompt flows
- longer inputs are delegated to the long-text transformation subsystem
- prompt templates remain task-specific rather than overloaded
- scripts own orchestration around the prompts

#### Design value

- the project gets LLM help where it is useful
- prompt responsibilities stay narrow enough for weaker models to survive
- long-document complexity does not infect every simpler case

### 6.7 Final Assembly and Quality Gate Layer

#### Problem to solve

Even if source acquisition and text transformation succeed, the project still needs a reliable way to produce final output and decide whether that output is acceptable.

#### Solution

The system separates:

- **assembly**: building the final Markdown file and document wrapper
- **quality verification**: checking whether the output meets minimum structural and content expectations

This makes output packaging and output judgment explicit final stages rather than hidden side effects of generation.

#### Design value

- final output structure becomes more stable
- quality gates are auditable
- the user gets both a document and a reasoned stop/go signal

### 6.8 Runtime Control and Observability Layer

#### Problem to solve

A long-running local workflow is hard to operate if its internal state is invisible.

#### Solution

The project keeps runtime behavior inspectable through persisted state, manifests, ownership records, control signals, telemetry, and intermediate artifacts.

This is especially important for long-video chunk execution, but the design principle applies to the whole project.

#### Design value

- failures become diagnosable
- resuming becomes practical
- the project behaves like a local job system instead of a disposable script

---

## 7. Core Subsystem Design: Long-Text Transformation

The previous sections describe the whole project. This section focuses on the project’s hardest subsystem.

### 7.1 Why long-text transformation is the core difficulty

The hardest problem in `yt-transcript` is not “download subtitles” or “call Deepgram.”

The hardest problem is this:

> How do we transform a transcript that is larger than the model’s reliable working window into a coherent, article-like result without losing structure, consistency, recoverability, or operator control?

This subsystem matters because once a video becomes long enough, the overall project quality is determined less by acquisition and more by whether long-form transformation is engineered correctly.

### 7.2 Problems the long-text subsystem must solve

The subsystem must solve several coupled problems at once:

- **Context limits**: the full document cannot be processed safely in one call
- **Probabilistic behavior**: LLM output may drift, summarize, omit, or rewrite too aggressively
- **Interruptibility**: long runs may stop due to timeout, retry exhaustion, process crash, or human control signals
- **Cross-chunk consistency**: headings, terms, numbers, dates, URLs, and style must remain coherent across the whole document
- **Merge risk**: if chunk boundaries are unclear, final assembly becomes unreliable
- **Control-boundary confusion**: if the model is allowed to make orchestration decisions, the subsystem turns into a black box

### 7.3 Long-text subsystem design goals

The long-text subsystem is designed to be:

- **partitionable**: split a large document into tractable units
- **resumable**: continue after interruption
- **verifiable**: check output through deterministic rules first
- **controllable**: support retry, repair, pause, resume, cancel, and replanning semantics
- **mergeable**: reassemble chunk results deterministically
- **consistency-aware**: preserve terminology and high-signal details across chunks

### 7.4 Long-text subsystem architecture

#### A. Plan and chunk contract

The subsystem does not begin with chunk execution. It begins with an explicit contract that defines:

- chunk boundaries
- output scope
- continuity policy
- merge assumptions
- verification expectations

This prevents later stages from guessing what a chunk “probably means.”

#### B. Chunking with strict output boundaries

Chunking is designed around a stable core range per chunk.

The subsystem may provide neighboring context as reference, but the model is required to output only the current chunk’s core transformation. This keeps merge simple and deterministic.

#### C. Continuity as reference, not shared output

To avoid abrupt transitions, the subsystem passes limited continuity context such as:

- previous chunk tail
- previous section title
- boundary rules

But continuity is reference-only. It is not part of the required output range.

#### D. Consistency controls: glossary and semantic anchors

Document-level consistency is protected through deterministic control signals such as:

- glossary terms for names and terminology
- semantic anchors for dates, numbers, percentages, versions, and URLs

These are injected into prompts and then checked again after generation.

#### E. Deterministic-first verification

The subsystem prefers deterministic checks before introducing any probabilistic judge layer.

Typical checks include:

- output length anomalies
- missing structure
- missing translation when translation is expected
- missing glossary terms
- missing semantic anchors

This keeps repair triggers stable and cheap.

#### F. Repair versus replan

Not every failure means the same thing.

The subsystem distinguishes:

- **repairable local failure**: retry or repair a specific chunk under the same plan
- **plan-level failure**: the current plan itself is unhealthy and should be replaced for the remaining work

This distinction is what prevents endless retries and gives the subsystem engineering semantics.

#### G. Deterministic merge and final assembly

Merge is intentionally kept simple.

If the chunk contract is sound, chunk outputs can be reassembled through ordered concatenation plus document-level wrapping. A healthy subsystem should not depend on clever post-hoc alignment to fix unclear boundaries.

#### H. Persisted state, runtime control, and telemetry

Because long-text transformation is a long-lived job, it relies on:

- persisted manifests and chunk state
- ownership and runtime control markers
- pause / resume / cancel semantics
- telemetry and attempt history

These are not add-ons. They are part of the subsystem design itself.

### 7.5 Why this subsystem design fits the whole project

The long-text subsystem is not a separate product hidden inside the repository. It exists to serve the full transcript workflow.

It fits the whole project because it:

- activates only when the planning layer says long-form handling is needed
- consumes the project’s normalized source artifacts
- returns outputs that the project’s assembly and quality-gate layers can use
- shares the same local-first state, recovery, and observability philosophy as the rest of the project

So the project-level system design and the long-text subsystem design are not competing stories. The second is a focused zoom-in on the hardest part of the first.

---

## 8. Key Trade-Offs and Boundaries

### 8.1 Why the project is not “just one big prompt”

Because the project must solve more than generation:

- source discovery
- routing
- state progression
- recovery
- quality gates
- output packaging

Collapsing all of that into one prompt would make the system fragile and opaque.

### 8.2 Why the project is script-first instead of free-form agent-first

Because high-risk decisions such as routing, validation, and recovery are easier to stabilize when scripts emit structured outputs and code owns the control flow.

### 8.3 Why long-text transformation is treated as a subsystem, not the entire project

Because `yt-transcript` still has to solve the larger end-to-end workflow:

- finding the source text
- deciding how to acquire it
- normalizing it
- deciding whether long-form handling is needed
- assembling the final article

Long-text transformation is central, but it is not the whole product.

### 8.4 Product-scope decisions the system intentionally makes

The current project intentionally commits to a few product-level decisions:

- `bilingual` means English source text plus Chinese translation
- if both English and Chinese subtitles exist, English remains the source text for generation
- subtitle-based acquisition is preferred when it is good enough
- Deepgram is a fallback acquisition path, not the default path for every video
- the current design is local-first rather than multi-tenant platform-first

### 8.5 What this design does not currently promise

This document does not claim that the project is:

- a generic distributed workflow platform
- a multi-tenant cloud transcription service
- a general-purpose multi-agent runtime
- a fully provider-agnostic universal document factory

Those may be future directions, but they are not the current system design goal.

---

## 9. Current Implementation Mapping (Appendix)

This section is not a spec. It helps relate design concepts to the current repository surfaces.

The internal implementation is organized into two layers: `task_runtime` for generic long-running task control, and `long_text` for long-text transformation behavior.

- `SKILL.md`
  - top-level human/agent workflow entry
- `workflows/*.md`
  - modular procedural workflow documents for source-path execution
- `prompts/*.md`
  - single-purpose prompt templates for cleanup, structure, summary, and translation
- `scripts/preflight.sh`
  - layered environment checks
- `scripts/download.sh`
  - metadata, subtitle, and audio acquisition surface
- `yt_transcript_utils.py`
  - main Python entry, CLI façade, workflow orchestration, planning, verification, and adapter commands; it imports the two kernel layers directly
- `kernel/task_runtime/runtime.py`
  - task ownership, command envelopes, and telemetry append helpers
- `kernel/task_runtime/state.py`
  - persistent manifest, runtime state, and control-file helpers
- `kernel/task_runtime/controller.py`
  - owned mutation and bounded control-loop behavior
- `kernel/task_runtime/telemetry.py`
  - telemetry inspection and summaries
- `kernel/long_text/glossary.py`
  - terminology extraction and checks for long-text consistency
- `kernel/long_text/semantic.py`
  - high-signal anchor extraction and checks
- `kernel/long_text/contracts.py`
  - control contracts, replan policy state, and chunk/runtime control helpers
- `kernel/long_text/autotune.py`
  - chunk autotune policy and token-source summary helpers
- `kernel/long_text/lifecycle.py`
  - manifest lifecycle, resume repair, and chunk/runtime defaulting
- `kernel/long_text/prompting.py`
  - prompt assembly and chunking-context preparation helpers
- `kernel/long_text/llm.py`
  - LLM request, retry, and streaming fallback helpers
- `kernel/long_text/processing.py`
  - chunk-processing, replan, and auto-replan execution loops
- `kernel/long_text/chunking.py`
  - public chunking command surfaces for long-text processing
- `kernel/long_text/merge.py`
  - chapter-plan mapping and deterministic merge command surfaces
- `kernel/long_text/execution.py`
  - execution, resume, and replan command surfaces for long-text jobs
- `README.md`
  - user-facing overview and operational entrypoints
- `SYSTEM_DESIGN.md`
  - architectural explanation of the whole project and its core long-text subsystem

---

## 10. One-Sentence Summary

`yt-transcript` is not just a subtitle tool and not just a long-text LLM kernel.

It is a local transcript production system whose full workflow is designed around deterministic routing, persisted state, explicit quality gates, and inspectable recovery — with long-text transformation engineered as its most important internal subsystem.

---

## 中文版

## 1. 文档定位

这份文档说明的是 **整个 `yt-transcript` 项目的系统设计**。

`README.md` 是面向操作者的快速上手与命令指南，`SYSTEM_DESIGN.md` 负责解释架构动机、系统边界与核心取舍。

它的目的，不是描述“下一步该改哪个函数、加哪个字段、拆哪个模块”。它不是 coding spec，也不是阶段性施工清单。

这份文档同时回答两层设计问题：

- **项目级问题**：`yt-transcript` 作为一个完整系统，如何把 YouTube 视频变成格式化 Markdown 文章？
- **子系统级问题**：在这个完整系统内部，最难的部分——长文本变换——是怎么设计的？

所以这份文档会刻意按两层展开：**先讲整个项目**，再讲**长文本变换这个核心子系统**。

在当前仓库里，`SYSTEM_DESIGN.md` 是这套能力唯一的权威设计文档。

---

## 2. 这个项目到底在构建什么

`yt-transcript` 不是一个单纯的字幕下载器，也不只是一个长文本 LLM 流水线。

它本质上是一个 **local-first、script-first 的转录产物生产系统**：输入一个 YouTube URL，输出一篇格式化的 Markdown 文章。

这个完整过程包括：

- 环境与凭据检查
- 元数据获取
- 字幕可用性探测
- 源路径路由
- 字幕下载或 Deepgram 转录兜底
- 状态同步与标准化
- 文本优化
- 必要时进入长文本 chunk 处理
- 最终装配
- 质量门禁与运行时检查

在这个完整系统里，**长文本变换是核心子系统之一**，但它仍然只是整个项目的一部分。

---

## 3. 项目级要解决的问题

### 3.1 来源可用性并不确定

一个 YouTube 视频可能出现多种情况：

- 有可用英文字幕
- 只有中文字幕
- 只有质量不稳定的自动字幕
- 根本没有可用字幕
- 必须回退到 Deepgram 做音频转录

所以系统不能假设永远只有一条稳定的获取路径。

### 3.2 路由决策必须可靠

系统需要做很多运行级决策，例如：

- 走字幕路径还是 Deepgram 路径
- 走短视频优化路径还是长视频路径
- 是否需要执行 LLM preflight
- 是否需要双语处理
- 后续处理应该基于 plain text 还是 timed segments

这些决策不能只依赖脆弱的 prompt 理解。

### 3.3 文本优化对模型能力很敏感

项目并不止步于拿到原始转录文本。它还需要把 transcript 风格文本变成更可读的 Markdown 文章，这里面包括：

- 结构恢复
- 清理与整理
- 翻译
- 改写
- 章节注入

这些是 LLM 最有价值的地方，但也是弱模型和过载 prompt 最容易出问题的地方。

### 3.4 长视频会击穿朴素工作流

一旦文本足够长，朴素流程就会失效：

- 文本超过上下文窗口
- 一次性 prompt 成本太高且不稳定
- 中断概率大幅上升
- 跨 chunk 一致性难以保持
- 如果边界不清晰，最终 merge 风险会很高

这就是为什么长文本变换会成为项目里最难的子系统。

### 3.5 项目必须“可运营”，而不只是“能跑通”

一个演示级脚本还不够。操作者还必须知道：

- 系统选了哪条路径
- 当前运行到了什么状态
- 哪些产物已经存在
- 是否可以从中断点继续
- 当前失败是局部可重试，还是计划级失败

所以这个项目必须被设计成一个可运营的本地系统，而不是一串临时命令。

---

## 4. 项目级设计目标

项目级的设计目标可以概括为：

> 把从 YouTube 到 Markdown 的转录产物生产，做成一个由代码控制路由、状态、恢复和质量门禁的可靠本地系统；而 LLM 只在真正适合它的文本变换环节发挥作用。

更具体地说，项目追求：

- **端到端可靠**：从 URL 到最终 Markdown 的整条路径都应稳定且可检查
- **脚本优先路由**：高风险分支逻辑尽量下沉到确定性脚本与结构化 JSON 输出
- **本地优先控制**：状态、产物、telemetry、恢复信息都应在本地可检查
- **对模型能力有韧性**：强模型可以提高质量，但即使是弱模型也应能在窄职责 prompt + 代码编排下走完整体流程
- **分层质量门禁**：stop/go 决策应依赖校验，而不是靠乐观假设
- **关注点分离**：source acquisition、planning、text transformation、merge、verification 不应塌缩成一个黑盒步骤
- **可扩展架构**：长文本子系统可以继续增强，而不需要推翻整个项目的结构

---

## 5. 总体系统设计

在整个项目层面，系统设计可以概括为：

```text
YouTube URL
  -> preflight 与配置检查
  -> metadata 与字幕可用性探测
  -> 源路径路由
  -> 字幕下载 或 Deepgram 转录
  -> 状态同步与标准化文档生成
  -> 优化计划制定
  -> 短路径直接变换 或 长文本变换子系统
  -> 最终装配
  -> 质量校验
  -> Markdown 成品 + 可检查的中间产物
```

整个架构最核心的想法是：

> 项目不会把“转录、变换、装配”当成一个 prompt 来做，而是把它拆成一个分阶段系统：确定性脚本负责路由和状态，LLM 只插入到文本变换阶段。

---

## 6. 整个项目的分层设计

### 6.1 环境与 Preflight 层

#### 要解决的问题

项目依赖多种外部能力，但并不是每一条工作流都需要它们全部存在。

例如：

- 纯字幕路径不需要 Deepgram 凭据
- 长视频 chunk 处理才可能需要 LLM 连通性和 token count 能力
- 基础 metadata 检查应该在引入高成本依赖前就能完成

#### 解决方案

系统采用分层 preflight：

- 基础 preflight：服务于 metadata / 字幕工作流
- Deepgram preflight：只在音频转录前执行
- LLM preflight：只在长视频 chunk 执行前执行

#### 设计价值

- 减少不必要的环境配置摩擦
- 让轻量路径保持轻量
- 让依赖检查和真实执行路径严格对应

### 6.2 来源探测与路由层

#### 要解决的问题

系统必须决定走哪条 source path，而且不能依赖模糊的模型判断。

#### 解决方案

来源探测通过结构化脚本输出完成，例如：

- metadata 获取
- 字幕可用性探测
- 源语言选择
- 在需要时发现 YouTube 章节

这些 machine-readable 输出驱动路径路由，而不是仅靠 prompt 文案推导。

#### 设计价值

- 路径选择更确定、可检查
- workflow 文档能保持声明式
- 操作者能看见系统为什么选这条路

### 6.3 来源获取层

#### 要解决的问题

项目需要支持不止一种 source acquisition 模式，但后续层又必须尽可能统一地消费这些产物。

#### 解决方案

系统支持两条主要来源路径：

- **字幕路径**：下载并解析可用字幕
- **音频兜底路径**：当字幕不可用或不足时，回退到 Deepgram 做转录

这两条路径都会尽量产出统一的下游工件，例如原始文本，以及在可用时带时间轴的 segments。

#### 设计价值

- 多种获取策略仍能汇聚到同一套下游架构
- 后续层不需要关心文本来自字幕还是音频转录
- 如果有时间信息，后续仍可做 chapter-aware chunking

### 6.4 规范状态与标准化层

#### 要解决的问题

如果一个端到端转录系统依赖临时聊天记忆或不稳定的 shell 变量，它很快就会变得脆弱。

#### 解决方案

项目显式持久化状态，并把 source artifacts 统一为规范化文档表示。

在设计层面，这意味着系统维护：

- 人可见的状态
- 机可用的标准化文档工件
- 对 source、中间产物、最终产物的稳定引用

换句话说，项目把 state 和 normalized document 视为一等系统对象，而不是顺手产生的文件。

#### 设计价值

- 整条流程具备可恢复性
- 后续 planning 不必反复重新理解原始 source 形态
- 人和脚本都能检查系统当前“认为是真的是什么”

### 6.5 优化规划层

#### 要解决的问题

项目必须根据文档长度、source 形态和目标操作，决定文本优化应该怎么进行。

#### 解决方案

系统引入显式 planning 阶段，用来决定：

- 当前任务是 short path 还是 long path
- 需要哪些操作
- 是否需要 chunking
- 是否需要 LLM preflight
- 后续执行应该遵循什么 contract

这个 planning 阶段，是“已经拿到 source text”和“知道如何安全变换它”之间的明确边界。

#### 设计价值

- 执行行为来自 plan，而不是来自偶然的默认值
- 长文本子系统只在真正需要时才启用
- 项目可以维持不同执行路径，而不变成隐藏条件的迷宫

### 6.6 文本优化层

#### 要解决的问题

文本优化是把 transcript 风格输入变成可读 Markdown 输出的核心阶段，但它也是 LLM 不稳定性影响最大的地方。

#### 解决方案

项目让文本优化保持窄职责、路径感知：

- 较短输入走更直接的 prompt 流程
- 较长输入委托给长文本变换子系统
- prompt 模板保持单任务，而不是过载
- prompt 周边的编排由脚本负责

#### 设计价值

- 项目在真正需要的地方使用 LLM
- prompt 职责足够窄，弱模型也更容易存活
- 长文档复杂度不会污染所有简单场景

### 6.7 最终装配与质量门禁层

#### 要解决的问题

即使 source acquisition 和 text transformation 都成功了，项目仍然需要一种可靠方式来生成最终输出，并判断这个输出是否可接受。

#### 解决方案

系统把两件事显式拆开：

- **装配**：构建最终 Markdown 文件和文档包装
- **质量校验**：检查输出是否满足最低结构与内容要求

这样，输出包装与输出判断就成为明确的最终阶段，而不是生成过程里隐含的副作用。

#### 设计价值

- 最终输出结构更稳定
- 质量门禁可审计
- 用户拿到的不只是文档，还有一个可解释的 stop/go 判断

### 6.8 运行控制与可观察性层

#### 要解决的问题

一个长时间运行的本地工作流，如果内部状态不可见，就很难被稳定操作。

#### 解决方案

项目通过持久化 state、manifest、ownership、control signals、telemetry 和中间产物，让运行行为保持可检查。

这一点在长视频 chunk 执行里尤其重要，但这套设计原则其实适用于整个项目。

#### 设计价值

- 失败更可诊断
- resume 变得可操作
- 项目行为更像一个本地作业系统，而不是一次性脚本

---

## 7. 核心子系统设计：长文本变换

前面几节讲的是整个项目；这一节聚焦项目里最难的那部分。

### 7.1 为什么长文本变换是核心难点

`yt-transcript` 里最难的问题，不是“下载字幕”，也不是“调用 Deepgram”。

真正最难的问题是：

> 当 transcript 超过模型可靠工作窗口时，如何把它变成一篇连贯、结构稳定、可恢复、可控的文章化结果？

这个子系统重要，是因为一旦视频足够长，项目整体质量的决定因素就不再主要是 acquisition，而是长文档变换是否被正确地工程化。

### 7.2 长文本子系统要解决的问题

这个子系统必须同时解决多个耦合问题：

- **上下文限制**：全文不能安全地一次处理
- **概率性行为**：LLM 可能漂移、摘要化、遗漏、过度改写
- **可中断性**：长作业可能因为 timeout、重试耗尽、进程崩溃或人工控制而停止
- **跨 chunk 一致性**：标题、术语、数字、日期、URL、风格要在整篇文档里保持一致
- **merge 风险**：如果 chunk 边界不清晰，最终装配会变得不可靠
- **控制边界混乱**：如果让模型参与编排决策，整个子系统就会退化成黑盒

### 7.3 长文本子系统的设计目标

长文本子系统被设计成：

- **可切分**：把大文档拆成可处理单元
- **可恢复**：中断后能继续
- **可验证**：优先通过确定性规则检查结果
- **可控制**：支持 retry、repair、pause、resume、cancel、replan
- **可合并**：局部结果能确定性重组为整体
- **有一致性感知**：能保护跨 chunk 的术语和高信号细节

### 7.4 长文本子系统的架构设计

#### A. plan 与 chunk contract

子系统并不是从执行 chunk 开始，而是从一个显式 contract 开始。这个 contract 定义：

- chunk 边界
- 输出范围
- continuity 策略
- merge 假设
- verification 预期

这样后续阶段就不会再去猜“这个 chunk 大概意味着什么”。

#### B. 严格输出边界的 chunking

chunking 围绕“每个 chunk 有稳定 core range”来设计。

系统可以给模型提供前后参考上下文，但模型只被允许输出当前 chunk 核心范围内的变换结果。这样 merge 才能保持简单、确定。

#### C. 把 continuity 当作参考，而不是共享输出

为了避免段落断裂，子系统会传递有限的 continuity context，例如：

- previous chunk tail
- previous section title
- boundary rules

但这些都是参考信息，不属于必须输出的内容范围。

#### D. 一致性控制：glossary 与 semantic anchors

文档级一致性通过一些确定性的控制信号来保护，例如：

- glossary：保护名称、术语、缩写
- semantic anchors：保护日期、数字、百分比、版本号、URL

这些信号会先注入 prompt，再在生成后重新检查。

#### E. 确定性优先的验证

在引入概率性的 judge 层之前，子系统优先使用确定性检查。

典型检查包括：

- 输出长度异常
- 结构缺失
- 需要翻译但没翻译
- glossary 词项缺失
- semantic anchors 缺失

这样 repair trigger 才会稳定且便宜。

#### F. repair 与 replan 的区分

并不是每一种失败都表示同一件事。

子系统会区分：

- **局部可修复失败**：在同一计划下重试或修复单个 chunk
- **计划级失败**：当前计划本身不健康，应该为剩余任务生成新计划

这个区分，是防止无限重试、赋予系统工程语义的关键。

#### G. 确定性的 merge 与最终装配

merge 被刻意设计得尽量简单。

如果 chunk contract 是健康的，那么 chunk 输出就应该能够通过有序拼接 + 文档级包装重新组装回来，而不依赖事后“聪明对齐”来修补边界不清晰的问题。

#### H. 持久化状态、运行控制与 telemetry

因为长文本变换是一个长生命周期作业，它必须依赖：

- 持久化 manifest 与 chunk state
- ownership 与 runtime control markers
- pause / resume / cancel 语义
- telemetry 与 attempt history

这些都不是附加能力，而是子系统设计的一部分。

### 7.5 为什么这个子系统设计适合整个项目

长文本子系统并不是仓库里藏着的另一个独立产品，它是为了服务完整转录流程而存在的。

它适配整个项目，是因为它：

- 只在 planning 层判断“确实需要长文档处理”时才激活
- 消费项目已经标准化好的 source artifacts
- 返回的产物能被项目的 assembly 与 quality-gate 层继续使用
- 和整个项目共享同一套 local-first 的 state、recovery、observability 理念

所以，项目级系统设计和长文本子系统设计不是两套互相竞争的叙事；后者只是前者里最难部分的一次放大。

---

## 8. 关键取舍与系统边界

### 8.1 为什么整个项目不是“一个大 prompt”

因为项目要解决的不只是生成，还包括：

- source discovery
- routing
- state progression
- recovery
- quality gates
- output packaging

把这些都压进一个 prompt，只会让系统变得脆弱且不可观察。

### 8.2 为什么项目是 script-first，而不是 free-form agent-first

因为路由、校验、恢复这类高风险决策，在脚本输出结构化结果、代码拥有控制流时更容易被稳定下来。

### 8.3 为什么长文本变换被当成子系统，而不是整个项目本身

因为 `yt-transcript` 仍然需要解决更大的端到端工作流：

- 找到 source text
- 决定怎么获取它
- 标准化它
- 决定是否需要长文档处理
- 组装最终文章

长文本变换很核心，但它并不是整个产品的全部。

### 8.4 系统刻意做出的产品级约束

当前项目明确坚持几条产品口径：

- `bilingual` 表示英文源文本 + 中文翻译
- 当中英字幕同时存在时，内容生成仍以英文字幕为 source text
- 当字幕质量足够时，优先走字幕路径
- Deepgram 是兜底获取路径，而不是每个视频都默认走的主路径
- 当前设计优先 local-first，而不是 multi-tenant platform-first

### 8.5 当前设计不承诺的内容

这份设计文档并不宣称当前项目已经是：

- 通用分布式工作流平台
- 多租户云端转录服务
- 通用多 agent runtime
- 完全 provider-agnostic 的通用文档工厂

这些可能是未来方向，但都不是当前系统设计的目标。

---

## 9. 当前实现映射（附录）

这一节不是 spec，只是帮助把设计概念映射到当前仓库表面。

当前内部实现分成两层：`task_runtime` 负责通用长程任务控制，`long_text` 负责长文本变换行为。

- `SKILL.md`
  - 顶层的人类/agent 工作流入口
- `workflows/*.md`
  - 面向 source path 的模块化过程文档
- `prompts/*.md`
  - 单任务 prompt 模板，如 cleanup、structure、summary、translation
- `scripts/preflight.sh`
  - 分层环境检查
- `scripts/download.sh`
  - metadata、字幕、音频的获取接口
- `yt_transcript_utils.py`
  - 主 Python 入口、CLI façade、workflow 编排、planning、verification 与适配命令；现直接依赖两层 kernel 子包
- `kernel/task_runtime/runtime.py`
  - 任务 ownership、command envelope 与 telemetry append 辅助
- `kernel/task_runtime/state.py`
  - manifest、runtime state 与控制文件的持久化辅助
- `kernel/task_runtime/controller.py`
  - owned mutation 与 bounded control-loop 行为
- `kernel/task_runtime/telemetry.py`
  - telemetry 查询与汇总
- `kernel/long_text/glossary.py`
  - 长文本一致性的术语提取与检查
- `kernel/long_text/semantic.py`
  - 高信号 anchor 的提取与检查
- `kernel/long_text/contracts.py`
  - control contract、replan policy state 与 chunk/runtime control 辅助
- `kernel/long_text/autotune.py`
  - chunk autotune 策略与 token source 汇总辅助
- `kernel/long_text/lifecycle.py`
  - manifest 生命周期、resume repair 与 chunk/runtime 默认化
- `kernel/long_text/prompting.py`
  - prompt 组装与 chunking-context 准备辅助
- `kernel/long_text/llm.py`
  - LLM 请求、重试与 streaming fallback 辅助
- `kernel/long_text/processing.py`
  - chunk 处理、replan 与 auto-replan 执行循环
- `kernel/long_text/chunking.py`
  - 长文本分块相关的公开命令表面
- `kernel/long_text/merge.py`
  - chapter-plan 映射与确定性 merge 的公开命令表面
- `kernel/long_text/execution.py`
  - 长文本作业的执行、resume 与 replan 命令表面
- `README.md`
  - 面向用户的总览与操作入口
- `SYSTEM_DESIGN.md`
  - 解释整个项目与核心长文本子系统为什么这样设计

---

## 10. 一句话总结

`yt-transcript` 既不只是一个字幕工具，也不只是一个长文本 LLM 内核。

它是一个本地优先的转录产物生产系统：整条流程围绕确定性路由、持久化状态、显式质量门禁和可检查恢复来设计，而长文本变换则是其中最重要的内部子系统。
