# yt-transcript 架构设计 Review：脚本与 LLM 的边界

[中文版](#中文版) | [English Version](#english-version)

---

# 中文版

## 1. 背景

这个项目已经不是一个简单的转写脚本，而是一条带有编排、恢复、质量门和回退策略的处理链路。

当前讨论的重点，也已经从"能不能跑通"转向"到底应该把多少处理交给确定性脚本，多少交给 LLM"。

这次 review 想回答的，不只是"脚本和 LLM 怎么分工"，而是一个更具体的问题：

> 怎样既保留 deterministic 底座的稳定性，又避免因为确定性逻辑扩张过度，错过 LLM 持续进化带来的能力红利？

围绕这个问题，这份文档主要记录四件事：

1. 这次 review 是因为什么被触发的
2. 我最开始担心的是什么
3. 分析之后，我得出的结论和需要澄清的地方是什么
4. 基于这些结论，后续应该怎么优化

---

## 2. 这次 review 的触发原因

### 2.1 我开始怀疑当前系统里有些职责边界没有收紧

随着系统逐步长出 workflow、runtime、quality gate、fallback 和 reroute，架构已经不再只是"脚本调用模型"这么简单。

我开始担心，当前设计里真正需要重新审视的，不是抽象的"脚本 vs LLM"对立，而是少数关键入口和关键链路里，仍然存在职责混杂的问题。

我尤其担心以下几类边界：

- 文本处理入口是否承担了过多混合职责
- 单一入口文件是否承载了过多不同性质的逻辑
- 评估层、控制层、机械清洗层、语言层之间的边界是否已经足够硬

### 2.2 我也担心系统里确定性逻辑过多，会限制对 LLM 进化红利的吸收

这次 review 的另一个重要触发点，是我开始怀疑：当前系统是否把太多本来可以交给 LLM 的文本任务，仍然固化在确定性逻辑里。

过去这样做有充分理由，因为模型能力、稳定性和可控性都不够强，所以需要依赖更多脚本和规则来兜底。

但如果 LLM 在以下能力上持续变强：

- 句子边界修复
- 段落整理
- 局部文本润色
- 术语上下文自然化
- chunk 级别的局部重写与修补

那么系统就需要重新判断：

> 哪些能力应该继续固化在脚本里，哪些能力应该从"规则优先"转向"受约束的 LLM 优先"。

如果这个问题不重新审视，系统就可能虽然越来越稳，却越来越难从 LLM 的能力进化中获益。

### 2.3 runtime 的复杂度值得警惕，但它不是这次 review 的唯一主线

我也确实担心 runtime 和状态控制的复杂度会继续增长。

不过，这次 review 的重点并不是先去讨论 runtime 会不会过重，而是先确认：

- 当前最主要的问题到底是不是 runtime
- 还是说，更核心的热点其实是职责混杂，以及确定性逻辑是否已经扩张到了不该扩张的语言层

换句话说，runtime 是需要纳入 review 的对象，但不是唯一主问题，更不是最先需要下结论的部分。

---

## 3. 我最开始担心的具体问题

### 3.1 少数文本处理入口的职责仍然过宽

我最明显的担心，是少数文本处理入口仍然承担了太多混合职责。

最典型的例子是 `cleanup_zh` 一类 prompt：它同时处理

- 句子修复
- 空格修复
- 重复片段修复
- 分段
- 标题生成
- 轻度术语归一化

这种聚合型职责会让系统显得像是在依赖一个大 prompt 兜底，而不是依赖清晰分层后的协作。

### 3.2 核心入口文件仍然过于集中

我也担心当前的核心逻辑仍然过多堆在单一入口文件里，导致维护耦合偏高。

尤其是 `yt_transcript_utils.py` 同时承载了：

- normalization
- planning
- quality
- routing
- compatibility
- 部分 runtime 入口

这不一定说明方向错了，但会让架构边界在阅读和维护体验上变得模糊。

### 3.3 评估层和控制层的边界必须更硬

我特别担心文档或实现里把评估层写模糊。

`字幕质量评分 / chunk seam / reroute` 这些信号已经进入 deterministic 的 planning / policy / evaluator 主路径，不能让 LLM 进入在线裁决链路。否则原本可解释的 quality gate 很容易重新变成半黑盒。

### 3.4 runtime 需要收敛，但不该被误读为主问题

我仍然担心 runtime 和状态控制的复杂度会继续增长，但它不是当前最紧急的结构性热点。

如果优先级排错，就容易先去打磨一个已经相对 bounded 的控制层，而把真正的耦合问题留在后面。

---

## 4. 为什么这些担心值得认真审视

### 4.1 文本质感问题本来就很难完全规则化

最终交付物的问题，很多都不是"有没有写错"，而是"读起来顺不顺"。这类问题往往没有特别清晰的通用规则。

例如：

- 中文字符之间的空格有时是错，有时又不能简单删掉
- 句子边界不是纯语法问题，还受上下文和语气影响
- 段落划分有时是结构问题，有时是表达问题
- 术语是否要统一，取决于上下文和用户预期

这意味着，如果把过多文本细节都压给规则系统，就很容易陷入"补一个 corner case，再补一个 corner case"的状态。

### 4.2 LLM 在语言任务上天然更强

LLM 对自然语言的优势，恰好就在这些难以规则化的地方：

- 更容易处理边缘表达
- 更容易根据上下文做局部修正
- 更容易把碎片化文本整理成自然语言
- 更适合做"看起来顺"的优化

所以，既然目标最终是文本质量，就必须重新审视：现在究竟有哪些文本工作还被不必要地留在脚本层。

### 4.3 当前系统已经积累了较重的控制结构

当前系统里，已经有比较完整的 runtime、恢复、决策、质量门和 fallback。它们的价值在于：

- 可恢复
- 可审计
- 可定位问题
- 可控制重试和回退

这些能力仍然重要。

但正因为控制结构已经不轻，才更需要确认：复杂度是不是放在了最值得放的地方。否则系统可能一边继续加控制，一边又没有把真正应该交给 LLM 的语言层工作释放出来。

### 4.4 因此，问题不是"脚本太多"，而是"边界和迁移判断不够清楚"

经过梳理之后，我更准确的担心是：

- prompt 职责没有完全拆开
- 机械清洗和语言修复的边界还不够锋利
- 单一入口文件仍承担了过多不同性质的工作
- 某些本可从 LLM 进化中获益的文本任务，仍然被过多确定性逻辑锁在脚本层

相比笼统地说"整体过度脚本化"，这种表述更接近当前问题本身。

---

## 5. review 后的结论与澄清

### 5.1 这次 review 确认了职责边界确实需要继续收紧

我接受这次 review 的一个核心判断：当前系统确实还存在职责边界不够清楚的问题。

但这个问题更准确的表述不是"脚本太多"，而是：

- prompt 职责没有和机械清洗层完全剥离干净
- 语言修复、结构整理和控制逻辑之间仍然有交叉
- 部分核心入口承载了过多不同性质的职责

也就是说，真正需要收敛的是边界，而不是简单地做一次方向翻转。

### 5.2 我也确认了一个合理担心：过多确定性文本逻辑会削弱系统吸收 LLM 进化红利的能力

这次 review 也确认了另一点：

> 如果系统把越来越多文本层工作固化进脚本和规则，就会越来越难利用 LLM 在语言理解、文本修复和自然化表达上的持续进步。

这个担心是成立的。

但它并不意味着"确定性逻辑没有价值"，而是意味着：

- 确定性逻辑不能无限扩张到语言层
- 不应该因为历史上模型不够强，就长期保留本来应该迁移出去的文本规则负担
- 系统需要主动区分：哪些是控制问题，哪些是语言问题

### 5.3 但结论不应该走向"全 LLM"

我赞同前面的反思，但我的结论不是"把脚本换成 LLM"。

更合理的方向是：

> 脚本负责底座和边界，LLM 负责语言质感和局部修复。

原因很简单：

- 状态控制、恢复、质量门、路由，必须可解释、可回放、可回归
- 文本修复、断句、分段、术语自然化，LLM 更擅长
- 如果让 LLM 接管底层控制，会损失稳定性和可审计性

### 5.4 确定性脚本仍然有不可替代的价值

我认为这些部分长期都应该保留为脚本：

- 下载、解析、切块、合并
- 中文空格、标点、重叠片段、重复片段清理
- 路由、fallback、恢复、重跑
- 质量门和输出验证

这些不是"生成文本"的问题，而是"控制系统"的问题。LLM 可以参与，但不适合做主控。

### 5.5 LLM 应该被用在它真正擅长的层

更合理的 LLM 使用方式是：

- 对 chunk 进行受约束的文本优化
- 对中文表达进行轻度修复
- 对术语和上下文做自然化处理
- 对局部失败片段做补写或重写
- 对标题、段落、句子边界做自然度优化

也就是说：

> LLM 更像"受控的语言修复器"，不是"自由编排器"。

### 5.6 评估层必须保持 deterministic 主裁决

以下内容可以由 LLM 辅助解释或生成修复候选，但不能进入在线主裁决链路：

- 字幕质量评分的原因分析
- glossary drift 的语义解释
- chunk seam 质量问题的离线诊断
- reroute 建议的离线辅助说明
- 长文本质量门阈值的离线校准分析

这里最重要的是：

> LLM 可以参与说明"为什么"，但不能参与在线主裁决"是否通过"。

### 5.7 runtime 不会消失，但应该收敛和瘦身

我不认为 runtime 和状态机未来会消失。它们仍然必要，因为系统总要知道：

- 现在走到哪一步了
- 为什么走这条路
- 失败后怎么恢复
- 质量是否通过
- 是否需要重跑或回退

但我也同意一点：如果 runtime 变得太复杂，而它带来的收益只是"历史兼容"或"过度保护"，那就应该瘦身，而不是继续加控制逻辑。

### 5.8 对这次 review 的最终收口

所以，这次 review 最终落下来的判断是：

- **不把系统改成 LLM-first**
- **继续保持 script-first 的主控架构**
- **重点做职责边界收敛，而不是方向翻转**
- **同时减少那些会阻碍系统吸收 LLM 进化红利的过度确定性文本负担**

这意味着，这次讨论的落点不是"脚本 vs LLM 谁赢"，而是：

> **保留脚本做底座，让 LLM 接管文本质感，同时把 runtime、prompt 和 evaluator 的职责边界收紧。**

---

## 6. 基于上述结论的后续优化方向

### 6.1 建议的目标分层

可以把当前系统理解成一条从"控制"到"表达"的链路：

```text
输入 / URL / 任务
  ↓
工作流编排层
  - SKILL.md
  - workflows/*.md
  - 选择 subtitle / deepgram / text optimization 路径
  ↓
运行时控制层
  - task_runtime/*
  - state / policy / decision / recovery / evaluator
  - 决定是否继续、恢复、回退、重跑
  ↓
机械处理层
  - text_cleanup/*
  - chunking / merge / autotune
  - 负责解析、清洗、切块、拼接、基础修复
  ↓
LLM 语言层
  - cleanup_zh / translate_only / chunk repair
  - 负责断句、分段、润色、术语自然化
  ↓
评估层
  - verify_quality
  - glossary / semantic / seam / spacing / duplication checks
  - 决定是否通过、是否 reroute、是否人工介入
  ↓
最终交付物
```

这个分层的核心，不是把所有东西都拆开，而是让每一层只做它最擅长的事：

- **控制层**管"该不该做、什么时候做、失败怎么办"
- **机械层**管"把脏数据清掉、把结构整理好"
- **语言层**管"怎么写得更自然、更像人"
- **评估层**管"这样写到底行不行"

### 6.2 建议长期保留为确定性代码的部分

这些部分建议长期保持脚本化：

- **路由与状态控制**：决定走哪条路径、是否 fallback、是否恢复
- **字幕解析与机械清洗**：包括中文空格、标点、重复、overlap、seam cleanup
- **chunk / merge / chapter 结构控制**：切块边界、章节映射、合并顺序
- **质量门与校验**：是否通过、是否重跑、是否人工介入
- **运行时契约与落盘状态**：可恢复、可审计、可回放

这部分的核心原则是：

> 规则优先，行为稳定，结果可验证。

### 6.3 建议更多转给 LLM 的部分

这些部分建议让 LLM 承担主要工作：

- `cleanup_zh` 类任务
- `translate_only` 类任务
- 句子边界修复
- 分段和段落整理
- 术语上下文判断
- 局部文本润色
- chunk 失败后的局部重写

这部分的核心原则是：

> 让模型处理"怎么写得更自然"，而不是"该不该做、什么时候做"。

### 6.4 LLM 只能做离线解释，不进入在线裁决

以下内容可以由 LLM 辅助解释或生成修复候选，但不能进入在线主裁决链路：

- 字幕质量评分的原因分析
- glossary drift 的语义解释
- chunk seam 质量问题的离线诊断
- reroute 建议的离线辅助说明
- 长文本质量门阈值的离线校准分析

这里最重要的是：

> LLM 可以参与说明"为什么"，但不能参与在线主裁决"是否通过"。

### 6.5 "保留脚本 / 转 LLM / 增强评估"三列表格

| 模块或能力 | 保留脚本 | 转 LLM | 增强评估 |
|---|---:|---:|---:|
| 路由、状态、恢复、重跑 | 是 | 否 | 是 |
| 字幕解析、中文空格、overlap、seam cleanup | 是 | 否 | 是 |
| chunk 边界、chapter mapping、merge 顺序 | 是 | 否 | 是 |
| `cleanup_zh`、`translate_only` | 否 | 是 | 是 |
| 句子边界修复、分段、局部润色 | 否 | 是 | 是 |
| 术语上下文自然化 | 否 | 是 | 是 |
| 字幕质量评分 | 是 | 否 | 是 |
| glossary drift / semantic anchors | 是 | 否 | 是 |
| quality gate 是否通过 | 是 | 否 | 是 |
| reroute 决策 | 是 | 否 | 是 |
| reroute 解释 / 修复候选生成 | 否 | 是 | 是 |
| chunk repair 候选文本 | 否 | 是 | 是 |

### 6.6 建议的职责切分方式

建议把系统分成四类职责，而不是简单分成三类：

#### A. 控制层
- runtime
- state
- policy
- recovery
- decision

#### B. 机械处理层
- subtitle cleanup
- cjk normalization
- overlap trimming
- post-merge cleanup
- chunking / merge

#### C. 语言层
- cleanup prompts
- translation prompts
- chunk repair
- text polish
- terminology naturalization

#### D. 评估层
- verify_quality
- quality report
- glossary / semantic / seam / spacing / duplication checks
- reroute / failure 解释

这样分之后，系统会更清晰：

- 控制层管"流程"
- 机械层管"格式"
- 语言层管"质感"
- 评估层管"是否合格"

### 6.7 建议的优化顺序

如果要重构，我建议优先级如下：

1. **先拆大文件**
   - 继续把 `yt_transcript_utils.py` 里的规划、校验、质量信号、路由辅助逻辑下沉到更清晰的 kernel 模块
   - 这一步先解决真正的耦合热点

2. **再收敛 prompt 职责**
   - 不要让 `cleanup_zh` 同时承担太多清洗和润色职责
   - 把结构整理、轻度修复、术语自然化拆得更清楚

3. **继续下沉机械清洗模块**
   - 保持 `text_cleanup` 作为明确的确定性底座
   - 让它与 prompt 层彻底分离

4. **把真正属于语言层的工作更多转给受约束的 LLM**
   - 逐步减少那些只是因为历史上模型不够强而保留下来的文本规则负担
   - 让系统在保持可控边界的前提下，持续吸收 LLM 的能力进步

5. **最后再做 runtime 语义收敛**
   - 清理历史兼容逻辑
   - 删除已经不再带来收益的状态分支
   - 保留真正有价值的可恢复机制

### 6.8 最终目标

最终应该形成一条比较清楚的链路：

> **deterministic cleanup → constrained LLM transformation → post-merge cleanup → stronger evaluator**

这条链路的好处是：

- 脚本解决脏数据和硬约束
- LLM 解决语言质感和局部修复
- evaluator 解决质量判断
- runtime 负责恢复与可审计性

---

## 7. 总结

这次 review 最终确认了两件事：

- 当前架构方向本身没有错，但职责边界确实还可以继续收紧
- 当前系统也需要警惕：不要因为确定性逻辑扩张过度，而错过 LLM 持续进化带来的能力红利

因此，正确的路线不是抛弃脚本，也不是把系统改成 LLM-first，而是：

> **继续保留 script-first 的主控架构，让脚本负责底座、控制和验证，让 LLM 更多承担文本质感和局部修复，同时进一步收紧 runtime、prompt、机械清洗层和 evaluator 之间的职责边界。**

这会比继续堆规则，或者反过来完全交给模型，都更适合这个项目。

---

# English Version

## 1. Background

This project has outgrown being a simple transcription script. It is now a processing pipeline with orchestration, recovery, quality gates, and fallback strategies.

The discussion has shifted from "can it run end to end?" to "how much of the processing should live in deterministic scripts, and how much should be handled by LLMs?"

This review is trying to answer a more specific question than simply "how should scripts and LLMs split the work?"

> How do we preserve the stability of a deterministic foundation without letting deterministic logic grow so far that we miss out on the compounding gains from ongoing LLM improvements?

This document covers four things:

1. What triggered this review
2. What I was initially worried about
3. What I concluded after the analysis, and what needed clarification
4. How to move forward based on those conclusions

---

## 2. What Triggered This Review

### 2.1 I began to suspect that some responsibility boundaries in the current system were still too loose

As the system grew workflows, runtime, quality gates, fallbacks, and reroutes, the architecture stopped being just "scripts calling a model."

What started to concern me was no longer the abstract framing of "scripts vs. LLMs," but something more concrete: a small number of key entry points and critical paths still had mixed responsibilities.

I was particularly concerned about these boundaries:

- Whether text processing entry points were doing too many different things at once
- Whether a single entry file was carrying logic of fundamentally different natures
- Whether the boundaries between the evaluation layer, control layer, mechanical cleaning layer, and language layer were hard enough

### 2.2 I was also concerned that too much deterministic logic would limit how much we could benefit from ongoing LLM improvements

Another key trigger was the growing suspicion that the system was still hard-coding too many text tasks into deterministic logic even though they could reasonably be handed off to LLMs.

Historically, that made sense. Models were not yet strong enough in capability, stability, or controllability, so scripts and rules had to serve as the safety net.

But if LLMs keep getting better at:

- Fixing sentence boundaries
- Reorganizing paragraphs
- Polishing text locally
- Naturalizing terminology in context
- Rewriting or patching at the chunk level

then the system needs to reassess one thing:

> Which capabilities should remain in scripts, and which should move from a "rules first" approach to a "constrained LLM first" approach?

Without revisiting that question, the system could become more and more stable while becoming less and less able to benefit from improvements in LLM capability.

### 2.3 Runtime complexity is worth watching, but it is not the only main focus of this review

I am genuinely concerned that runtime and state-control complexity may keep growing.

But the point of this review is not to start by debating whether runtime has become too heavy. It is to first establish:

- Is runtime really the biggest problem right now?
- Or is the more important hotspot responsibility mixing—and the question of whether deterministic logic has already expanded into parts of the language layer where it does not belong?

In other words, runtime is part of this review, but it is not the only main issue, and it is not the first place where we should force a conclusion.

---

## 3. My Initial Specific Concerns

### 3.1 A few text processing entry points still had overly broad scope

The clearest problem was that a few text-processing entry points were still doing too many different jobs at once.

The most typical example is a `cleanup_zh`-style prompt that simultaneously handles:

- Sentence repair
- Whitespace repair
- Duplicate segment removal
- Segmentation
- Title generation
- Light terminology normalization

That kind of catch-all responsibility makes the system look as though it is relying on one large prompt as a safety net, instead of on clearly separated layers working together.

### 3.2 Core entry files were still too concentrated

I was also concerned that too much core logic was still concentrated in a single entry file, creating unnecessary maintenance coupling.

`yt_transcript_utils.py`, in particular, was carrying:

- normalization
- planning
- quality
- routing
- compatibility
- partial runtime entry points

That does not necessarily mean the direction itself was wrong, but it does make the architectural boundaries feel blurrier when reading or maintaining the code.

### 3.3 The boundary between the evaluation layer and the control layer must be stricter

I was particularly concerned that the evaluation layer might be described too vaguely in the docs or implementation.

Signals such as subtitle quality scores, chunk seam issues, and reroute triggers have already entered the main deterministic planning / policy / evaluator path. LLMs must not be allowed into the online decision chain. Otherwise, a quality gate that was originally explainable could easily turn back into a half-black-box.

### 3.4 Runtime needs to converge, but shouldn't be misread as the main problem

I still worry about runtime and state control complexity growing, but it's not the most urgent structural issue right now.

If we get the priorities wrong, we risk polishing a control layer that is already relatively bounded while leaving the real coupling problems for later.

---

## 4. Why These Concerns Deserve Serious Attention

### 4.1 Text quality issues are inherently hard to fully formalize

Many problems in the final deliverable are not about whether something is technically wrong, but about whether it reads naturally. Those kinds of issues rarely come with clean, universal rules.

For example:

- Spaces between Chinese characters are sometimes wrong, but you can't just strip them all
- Sentence boundaries aren't purely grammatical—they depend on context and tone
- Paragraph breaks are sometimes structural, sometimes expressive
- Whether to unify terminology depends on context and user expectations

If too many of those text-level details are forced into a rule system, you end up in an endless cycle of patching one corner case after another.

### 4.2 LLMs are naturally stronger at language tasks

LLMs are strongest precisely in the areas that resist formalization:

- Handling edge-case expressions
- Making local corrections based on context
- Turning fragmented text into natural language
- Improving whether the text simply reads smoothly

Since the end goal is text quality, we need to revisit which parts of the text work are still being left in the script layer unnecessarily.

### 4.3 The system has already accumulated significant control structure

The system already has fairly complete runtime, recovery, decision-making, quality gates, and fallbacks. Their value lies in:

- Recoverability
- Auditability
- Debuggability
- Controlled retries and rollbacks

These capabilities still matter.

But precisely because the control structure is already non-trivial, we need to confirm that the complexity is sitting where it should be. Otherwise, the system keeps adding controls without freeing up the language-layer work that should go to LLMs.

### 4.4 So the problem is not "too many scripts," but rather unclear boundaries and unclear migration judgment

After thinking it through, my more precise concern is:

- Prompt responsibilities aren't fully separated
- The boundary between mechanical cleaning and language repair isn't sharp enough
- Single entry files still carry work of fundamentally different natures
- Some text tasks that could benefit from LLM evolution are still locked behind excessive deterministic logic

This is a more accurate description than the blanket claim of "over-scripting."

---

## 5. Conclusions and Clarifications

### 5.1 This review confirmed that responsibility boundaries do need to be tightened further

I agree with the core judgment from this review: the system still does have unclear responsibility boundaries.

But the more accurate framing is not "too many scripts." It is this:

- Prompt responsibilities are not yet cleanly separated from the mechanical cleaning layer
- Language repair, structural organization, and control logic still overlap
- Some core entry points still carry too many fundamentally different responsibilities

In other words, what needs to converge is the boundaries, not the overall direction.

### 5.2 I also confirmed a valid concern: excessive deterministic text logic does weaken the system's ability to absorb the benefits of ongoing LLM evolution

This review also confirmed the following:

> If the system keeps hard-coding more and more text-layer work into scripts and rules, it will become increasingly difficult to benefit from ongoing improvements in LLMs' language understanding, text repair, and natural expression.

That concern is valid.

But it does not mean that deterministic logic has no value. It means:

- Deterministic logic can't expand indefinitely into the language layer
- We shouldn't keep text rule debt around just because models used to be weaker
- The system needs to actively distinguish control problems from language problems

### 5.3 But the conclusion shouldn't swing to "all LLM"

I agree with the reflection above, but my conclusion isn't "replace scripts with LLMs."

A more reasonable direction is:

> Scripts should own the foundation and the boundaries, while LLMs should own text quality and local repair.

The reasoning is straightforward:

- State control, recovery, quality gates, and routing must remain explainable, replayable, and regression-testable
- Text repair, sentence splitting, segmentation, and terminology naturalization are where LLMs shine
- Handing low-level control to LLMs would sacrifice stability and auditability

### 5.4 Deterministic scripts still have irreplaceable value

I believe these parts should stay as scripts for the long haul:

- Download, parsing, chunking, merging
- Chinese whitespace, punctuation, overlap, and duplicate cleanup
- Routing, fallback, recovery, reruns
- Quality gates and output validation

These are not really "generate text" problems; they are "control the system" problems. LLMs can assist, but they should not be the primary controller.

### 5.5 LLMs should be used where they actually excel

A more reasonable way to use LLMs is:

- Constrained text optimization on chunks
- Light repairs to Chinese expression
- Naturalizing terminology and context
- Rewriting or patching locally failed segments
- Optimizing naturalness of titles, paragraphs, and sentence boundaries

In other words:

> LLMs act as a "controlled language repairer," not a "free-form orchestrator."

### 5.6 The evaluation layer must retain deterministic final authority

For the following items, LLMs can help explain what is happening or generate repair candidates, but they cannot enter the online decision chain:

- Root cause analysis for subtitle quality scores
- Semantic interpretation of glossary drift
- Offline diagnosis of chunk seam quality issues
- Offline supporting explanations for reroute suggestions
- Offline calibration analysis for long-text quality gate thresholds

The key point:

> LLMs can explain "why," but they cannot make the online call on "whether it passes."

### 5.7 Runtime won't disappear, but it should converge and slim down

I don't expect runtime and state machines to go away. They're still necessary because the system always needs to know:

- Where it is in the process
- Why it chose this path
- How to recover from failure
- Whether quality passed
- Whether a rerun or rollback is needed

But I agree with this: if runtime has grown complex and its only remaining value is "historical compatibility" or "over-defensiveness," it should slim down rather than keep accumulating control logic.

### 5.8 Final takeaway from this review

So the final judgments from this review are:

- **Don't flip the system to LLM-first**
- **Keep the script-first control architecture**
- **Focus on converging responsibility boundaries, not flipping directions**
- **Reduce the excessive deterministic text burden that blocks the system from absorbing LLM evolution benefits**

The takeaway isn't "scripts vs. LLMs—who wins." It's:

> **Keep scripts as the foundation, hand text quality to LLMs, and tighten the responsibility boundaries between runtime, prompts, and evaluators.**

---

## 6. Optimization Directions

### 6.1 Target layering

Think of the current system as a chain from "control" to "expression":

```text
Input / URL / Task
  ↓
Workflow Orchestration Layer
  - SKILL.md
  - workflows/*.md
  - Selects subtitle / deepgram / text optimization path
  ↓
Runtime Control Layer
  - task_runtime/*
  - state / policy / decision / recovery / evaluator
  - Decides whether to continue, recover, fallback, or rerun
  ↓
Mechanical Processing Layer
  - text_cleanup/*
  - chunking / merge / autotune
  - Handles parsing, cleaning, chunking, merging, basic repair
  ↓
LLM Language Layer
  - cleanup_zh / translate_only / chunk repair
  - Handles sentence splitting, segmentation, polishing, terminology naturalization
  ↓
Evaluation Layer
  - verify_quality
  - glossary / semantic / seam / spacing / duplication checks
  - Decides whether to pass, reroute, or escalate to human review
  ↓
Final Deliverable
```

The point of this layering isn't to split everything apart—it's to make sure each layer only does what it's best at:

- **Control layer**: "should we do this, when, and what if it fails?"
- **Mechanical layer**: "clean the dirty data, get the structure right"
- **Language layer**: "make it read naturally, like a human wrote it"
- **Evaluation layer**: "is this actually good enough?"

### 6.2 Parts that should remain deterministic in the long term

These should remain as scripts:

- **Routing and state control**: path selection, fallback decisions, recovery triggers
- **Subtitle parsing and mechanical cleaning**: Chinese whitespace, punctuation, duplicates, overlap, seam cleanup
- **Chunk / merge / chapter structure control**: chunk boundaries, chapter mapping, merge ordering
- **Quality gates and validation**: pass/fail, rerun triggers, human escalation
- **Runtime contracts and persisted state**: recoverability, auditability, replayability

Core principle:

> Rules first, stable behavior, verifiable results.

### 6.3 Parts that should increasingly be shifted to LLMs

These should have LLMs as the primary worker:

- `cleanup_zh`-type tasks
- `translate_only`-type tasks
- Sentence boundary repair
- Segmentation and paragraph reorganization
- Terminology contextual judgment
- Local text polishing
- Local rewriting after chunk failures

Core principle:

> Let the model handle "how to write it more naturally," not "whether to do it and when."

### 6.4 LLMs can only explain offline—they don't make online decisions

LLMs can assist with explanation or generate repair candidates for the following, but they cannot enter the online decision chain:

- Root cause analysis for subtitle quality scores
- Semantic interpretation of glossary drift
- Offline diagnosis of chunk seam quality issues
- Offline supporting explanations for reroute suggestions
- Offline calibration analysis for long-text quality gate thresholds

The key point:

> LLMs can explain "why," but they cannot make the online call on "whether it passes."

### 6.5 "Keep Script / Shift to LLM / Enhance Evaluation" matrix

| Module or Capability | Keep Script | Shift to LLM | Enhance Evaluation |
|---|:---:|:---:|:---:|
| Routing, state, recovery, rerun | Yes | No | Yes |
| Subtitle parsing, Chinese whitespace, overlap, seam cleanup | Yes | No | Yes |
| Chunk boundaries, chapter mapping, merge ordering | Yes | No | Yes |
| `cleanup_zh`, `translate_only` | No | Yes | Yes |
| Sentence boundary repair, segmentation, local polishing | No | Yes | Yes |
| Terminology contextual naturalization | No | Yes | Yes |
| Subtitle quality scoring | Yes | No | Yes |
| Glossary drift / semantic anchors | Yes | No | Yes |
| Quality gate pass/fail | Yes | No | Yes |
| Reroute decision | Yes | No | Yes |
| Reroute explanation / repair candidate generation | No | Yes | Yes |
| Chunk repair candidate text | No | Yes | Yes |

### 6.6 Proposed responsibility split

Split the system into four responsibility categories, not three:

#### A. Control Layer
- runtime
- state
- policy
- recovery
- decision

#### B. Mechanical Processing Layer
- subtitle cleanup
- cjk normalization
- overlap trimming
- post-merge cleanup
- chunking / merge

#### C. Language Layer
- cleanup prompts
- translation prompts
- chunk repair
- text polish
- terminology naturalization

#### D. Evaluation Layer
- verify_quality
- quality report
- glossary / semantic / seam / spacing / duplication checks
- reroute / failure explanation

After this split, the system becomes clearer:

- Control layer owns "flow"
- Mechanical layer owns "format"
- Language layer owns "texture"
- Evaluation layer owns "quality gate"

### 6.7 Suggested optimization order

If restructuring, I'd suggest this priority order:

1. **Split the large files first**
   - Continue pushing planning, validation, quality signals, and routing helpers out of `yt_transcript_utils.py` into cleaner kernel modules
   - This addresses the real coupling hotspot first

2. **Then converge prompt responsibilities**
   - Stop letting `cleanup_zh` carry both cleaning and polishing duties
   - Separate structural reorganization, light repair, and terminology naturalization more clearly

3. **Continue pushing mechanical cleaning into its own modules**
   - Keep `text_cleanup` as a clearly deterministic foundation
   - Fully decouple it from the prompt layer

4. **Shift genuinely language-layer work to constrained LLMs**
   - Gradually shed text rule debt that exists only because models used to be weaker
   - Let the system keep absorbing LLM improvements while maintaining controllable boundaries

5. **Finally, converge runtime semantics**
   - Clean up historical compatibility logic
   - Delete state branches that no longer deliver value
   - Keep only the recovery mechanisms that actually matter

### 6.8 End goal

The end state should be a clear pipeline:

> **deterministic cleanup → constrained LLM transformation → post-merge cleanup → stronger evaluator**

The benefits:

- Scripts handle dirty data and hard constraints
- LLMs handle text quality and local repair
- Evaluators handle quality judgment
- Runtime handles recovery and auditability

---

## 7. Summary

This review confirmed two things:

- The current architectural direction is sound, but responsibility boundaries can still be tightened further
- The system should stay alert: don't let deterministic logic expand so far that it blocks the compounding benefits of LLM evolution

The right path isn't to abandon scripts, nor to flip the system to LLM-first. It's:

> **Keep the script-first control architecture. Let scripts own the foundation, control, and validation. Let LLMs take on more text quality and local repair. And tighten the responsibility boundaries between runtime, prompts, mechanical cleaning, and evaluators.**

That's a better fit for this project than piling on more rules—or handing everything over to the model.