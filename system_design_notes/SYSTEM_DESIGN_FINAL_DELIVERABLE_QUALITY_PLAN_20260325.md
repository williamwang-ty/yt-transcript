# yt-transcript 最终交付物质量与可读性提升规划

[中文版](#zh-cn) | [English Version](#en)

<a id="zh-cn"></a>

## 中文版

## 1. 文档定位

这份文档聚焦一个更具体的问题：

> 如何系统性提升 `yt-transcript` 最终交付 Markdown 的文本质量、可读性、稳定性与一致性。

它不是单点 bug 修复记录，也不是某一个 prompt 的微调备忘录。

它的目标是把“URL 到最终文章”的文本生产链条拆开，明确：

1. 当前质量问题真正来自哪里
2. 哪些问题应该由确定性代码解决，哪些问题应该交给 LLM
3. 怎样建立更可靠的质量评估与回退机制
4. 如何分阶段落地，而不是一次性重写整条链路

这份规划是对 [SYSTEM_DESIGN_RESEARCH_NOTES_20260319_230057.md](./SYSTEM_DESIGN_RESEARCH_NOTES_20260319_230057.md) 的补充。
前一份文档讨论的是系统形态与 runtime 设计，这一份只聚焦“最终交付物质量”。

---

## 2. 当前问题判断

### 2.1 现象层问题

当前最终交付物仍经常出现以下问题：

- 中文字符之间存在不自然空格
- 断句不稳，句子边界偏碎
- 段落划分生硬，短段过多
- 自动字幕半句重叠导致重复片段残留
- chunk 边界附近出现衔接不顺或重复
- 专有名词、产品名、频道名、术语不稳定
- “看起来通过了 verify-quality”，但读起来仍然不够自然

### 2.2 根因判断

当前系统的核心问题不是“没有 prompt”，而是：

> 字幕路径的清洗仍然过度依赖 LLM，而确定性规则层太弱，甚至在前处理阶段主动制造了部分噪声。

更具体地说：

- `parse_vtt()` 与 `parse_vtt_segments()` 会把 cue 文本用空格拼接，这对中文不友好
- 字幕去重只处理“完全相同的连续重复”，无法处理常见的 overlap 重叠
- `_normalize_text_body()` 只做非常轻的换行清理，不做中文文本规范化
- 长视频默认偏向 `segments` 作为 chunk 来源，容易把字幕粒度噪声带入后续优化
- `merge_content()` 只负责拼接，不做 chunk seam 清洗
- `verify-quality` 更偏结构正确性，不足以评估中文可读性

### 2.3 当前路径清洗强度不一致

当前不同 source path 的清洗强度并不一致：

- YouTube 中文字幕路径：
  主要依赖 `cleanup_zh` prompt 做修复
- 英文字幕路径：
  主要是 `structure_only -> translate_only`
- Deepgram 中文路径：
  除了 prompt 外，还有更强的规则清洗
- Deepgram 英文路径：
  以结构整理与翻译为主

这会导致用户感知上出现不一致：

- “明明有中文字幕，结果成品反而没有 Deepgram 那么干净”

---

## 3. 设计目标

### 3.1 一级目标

最终交付物应满足四个目标：

1. 保真：
   不改动原意，不偷偷总结，不添加不存在的信息
2. 可读：
   读起来像自然中文文章，而不是被空格污染的字幕拼接物
3. 稳定：
   相同类型输入，应输出相近质量，而不是完全靠模型状态波动
4. 可验证：
   质量判断不能只靠“看起来还行”，而要有可自动检查的指标

### 3.2 二级目标

- 中文字幕路径与 Deepgram 中文路径的体验差距要明显缩小
- 长视频路径不能为了章节映射而牺牲正文质量
- 质量提升应优先通过确定性代码完成，再让 LLM 做语义层结构化整理
- 质量问题应具备可观测性，便于回归与定位

### 3.3 非目标

以下事项不作为本轮规划的主目标：

- 把 transcript 改写成“文章创作稿”
- 引入大规模风格润色或总结式重写
- 在 skill 内再嵌一层完整 agent
- 把所有问题都交给更贵的模型解决

---

## 4. 设计原则

### 4.1 规则优先，模型补足

优先用确定性代码解决：

- 中文空格
- 标点前空白
- 明显重复片段
- chunk 接缝重复
- VTT cue 重叠残留

只有这些基础噪声被消掉后，再让 LLM 负责：

- 断句
- 分段
- 标题层次
- 术语上下文判断

### 4.2 中文优先，不套英语文本假设

当前很多逻辑默认用空格连接文本，这更接近英文文本假设。
新的清洗层需要显式支持：

- CJK-aware join
- 中文标点规则
- 中英混排规则
- 专有名词与中文句法共存

### 4.3 交付物质量要独立评估

`process` 阶段不能“自己生产、自己宣布合格”。
必须引入更细粒度的 quality evaluator，用于判断：

- 是否还有中文空格污染
- 是否还有明显重复
- 是否段落过碎
- 是否 chunk 边界断裂
- 是否术语漂移严重

### 4.4 不牺牲可恢复性

所有新增质量步骤都要遵循现有 runtime 哲学：

- 中间结果可持久化
- 失败可定位
- 阶段可恢复
- stop/go 有明确依据

---

## 5. 目标架构

建议把最终交付质量链路拆成七层。

### 5.1 Source Acquisition Layer

职责：

- 获取 metadata / subtitles / audio
- 区分“存在字幕”与“字幕可下载”
- 记录 source family、下载失败原因、fallback 行为

这层已经相对成熟，不是本轮主改造重点。
但下一步需要补“字幕质量信号”而不仅是“是否存在字幕”。

新增建议：

- 为字幕源记录基础质量信号
- 例如：
  - punctuation_density
  - repeated_line_ratio
  - avg_cue_length
  - cjk_space_ratio
  - overlap_suspect_ratio

### 5.2 Deterministic Subtitle Cleanup Layer

这是本轮最关键的新层。

目标：

> 在进入 LLM 之前，把字幕文本清到“至少不会主动伤害后续处理”。

建议新增一个独立的 subtitle cleanup 模块，而不是继续把逻辑散落在 `yt_transcript_utils.py` 中。

建议模块形态：

- `kernel/text_cleanup/subtitle.py`
- `kernel/text_cleanup/cjk.py`
- `kernel/text_cleanup/overlap.py`

建议提供的核心能力：

1. CJK-aware join
2. 中文字符间空格清理
3. 标点前后空白修正
4. cue overlap 去重
5. 连续重复短语压缩
6. 轻量术语归一化钩子
7. 输出诊断信息，供 quality report 使用

### 5.3 Source Quality Routing Layer

当前 source routing 基本只看“是否有字幕”。
下一步需要改成：

> 既看 availability，也看 usability 与 quality。

建议增加新的质量路由决策：

- 中文字幕存在且质量可接受：
  走 subtitle path
- 中文字幕存在但质量明显过低：
  允许切 Deepgram，或者至少要求更强清洗路径
- 中文字幕不存在，英文字幕可接受：
  走 bilingual path
- 全部字幕不可用或质量太差：
  走 Deepgram

这里的关键不是“让系统更爱切 Deepgram”，而是：

> 让系统能基于质量，而不是基于存在性，做更合理的路径选择。

### 5.4 Chunking Strategy Layer

当前长视频路径默认偏向 `normalized_document -> segments`。
这对章节映射有帮助，但不一定对正文质量最优。

建议重新定义中文单语正文的 chunking 优先级：

- 正文优化优先使用 `raw_text` / normalized text
- `segments` 优先用于：
  - 时间映射
  - chapter plan
  - diagnostics

推荐规则：

- Chinese monolingual:
  `text-first`
- English bilingual:
  保留当前结构路径
- Timed chapter mapping:
  独立依赖 `segments`

这样可以降低字幕时间片碎片对正文质量的污染。

### 5.5 LLM Transformation Layer

LLM 不应再承担“基础清洗 + 结构整理 + 术语猜测 + 接缝修复”四种职责。

建议收敛为两类 prompt：

1. `cleanup_zh`
   只负责：
   - 断句
   - 分段
   - 标题结构
   - 保守的明显术语修正

2. `translate_only`
   继续只负责双语翻译

建议避免让 `cleanup_zh` 负责：

- 大规模去重
- 大量空格修复
- chunk seam 修复

这些应由规则层先完成。

### 5.6 Post-Merge Cleanup Layer

这是当前明显缺失的一层。

`merge_content()` 现在只是拼接 chunk。
后续应在 merge 后增加一个 deterministic polish 步骤，专门修：

- chunk 接缝重复句
- 相邻短段碎片
- 多余空行
- 头尾残句拼接问题
- 连续相同标题或异常标题密度

建议形态：

- `post_merge_cleanup(text, mode, diagnostics)`

注意：

- 这里不是“润色文风”
- 而是“修复分块处理带来的机械性瑕疵”

### 5.7 Quality Evaluation Layer

当前 `verify-quality` 应继续保留，但要扩展出更强的中文质量指标。

建议新增检查：

1. `cjk_spacing_anomaly`
   - 中文字符间不自然空格比例
2. `fragment_paragraph_ratio`
   - 过短段落占比
3. `duplicate_ngram_ratio`
   - 2-8 token 重复片段占比
4. `chunk_seam_duplication`
   - 接缝处重复句检测
5. `punctuation_density`
   - 标点密度是否过低
6. `header_fragment_balance`
   - 标题数量与正文长度是否失衡
7. `glossary_drift`
   - 高优先级术语是否漂移

质量门输出应区分：

- `hard_failures`
- `repairable_warnings`
- `advisory_warnings`

---

## 6. 分阶段落地计划

### Phase 0: 建立基线与语料

目标：

- 在改逻辑前先建立评价样本

工作：

- 收集 20-30 个真实链接样本
- 分类：
  - 中文字幕自动轨
  - 中文字幕人工轨
  - 英文字幕
  - 无字幕走 Deepgram
- 为每类准备 golden output 或至少人工标注问题点
- 把当前产物保存为 baseline

交付物：

- `tests/fixtures/quality_corpus/...`
- baseline quality report

### Phase 1: 字幕规则清洗

目标：

- 先消除最影响中文阅读体验的机械噪声

工作：

- 替换 `parse_vtt()` / `parse_vtt_segments()` 的空格拼接逻辑
- 新增 `_normalize_subtitle_text()`
- 增加 cue overlap 去重
- 在 normalized document 物化时使用字幕专用清洗

验收标准：

- 中文空格异常显著下降
- 明显重叠片段显著下降
- 不影响英文字幕路径

### Phase 2: 中文长路径改为 text-first

目标：

- 降低字幕碎片进入 chunking 的概率

工作：

- 在 planner 中引入 source-kind preference policy
- 对中文单语正文优先使用 text chunking
- 保留 segments 给章节映射
- 允许 text body 与 timed metadata 并存

验收标准：

- 长视频成品重复问题下降
- 章节功能不回退

### Phase 3: Merge 后清洗

目标：

- 消除 chunk seam 造成的机械性瑕疵

工作：

- 在 `merge_content()` 后增加 deterministic seam cleanup
- 新增接缝重复检测
- 新增短段合并与空行整理

验收标准：

- merge 后文本结构更连贯
- 不引入语义删改

### Phase 4: Quality Evaluator 升级

目标：

- 把“读起来别扭”变成可报告、可阻断的指标

工作：

- 扩展 `verify-quality`
- 新增 zh-specific checks
- 区分 warning 与 hard failure
- 允许后续 runtime 根据质量门做 repair / reroute

验收标准：

- 当前已知问题样本能被 evaluator 检出
- evaluator 不会对健康样本产生大量误报

### Phase 5: 术语与专有名词稳定化

目标：

- 提高最终稿的术语一致性

工作：

- 建 glossary builder 的 transcript mode
- 从 title / channel / chapters / description 中抽取高优先级术语
- 在 `cleanup_zh` 中注入受控术语上下文
- 在 quality gate 中检查 glossary drift

验收标准：

- 高频术语漂移明显下降
- 专有名词错误率降低

### Phase 6: 质量驱动的 reroute / fallback

目标：

- 让系统能基于质量自动选择更优路径

工作：

- 为 subtitle path 增加 quality score
- 当字幕质量极差时，允许切换更强清洗或 Deepgram
- 为 reroute 行为输出解释字段

验收标准：

- 不再因为“有字幕”就被锁死在差路径
- fallback 原因可解释、可审计

---

## 7. 测试策略

### 7.1 单元测试

新增覆盖：

- CJK-aware join
- 中文空格清理
- overlap dedupe
- seam cleanup
- quality metrics 计算

### 7.2 回归测试

对真实问题样本固化回归：

- 中文字幕自动轨有空格污染
- 中文字幕自动轨有 overlap
- 长视频 chunk seam 重复
- 英文字幕双语路径不被新清洗破坏

### 7.3 Golden Output 测试

对一小批高价值样本建立人工认可版本。

比较维度：

- 结构
- 空格
- 段落
- 重复
- 术语

### 7.4 Evaluator 测试

要同时覆盖：

- 真问题能抓到
- 正常文本不会被过度拦截

---

## 8. 关键指标

建议建立以下指标并持久化到 quality report：

- `cjk_space_ratio`
- `duplicate_ngram_ratio`
- `overlap_reduction_count`
- `chunk_seam_warning_count`
- `short_paragraph_ratio`
- `header_density`
- `glossary_drift_count`
- `subtitle_quality_score`
- `source_route_reason`
- `post_merge_cleanup_applied`

这些指标既可用于：

- 质量门
- 回归趋势观察
- reroute 决策
- 用户可解释性

---

## 9. 风险与取舍

### 9.1 规则过强会误伤原文

风险：

- 去重或合并过头，删掉真实内容

策略：

- 优先做保守规则
- 每个清洗器输出 diagnostics
- 高风险规则默认只警告，不自动应用

### 9.2 中文规则可能影响英文或混排文本

风险：

- CJK 清洗误伤技术术语或版本号

策略：

- 基于字符类别做局部规则
- mixed-language token 保守处理
- 对 `API`, `LLM`, `GPT-5.4` 等保留例外

### 9.3 质量门过严会增加 rerun 成本

风险：

- 过多 warning 导致系统频繁重跑

策略：

- 初期以 advisory warning 为主
- 先收集分布，再决定哪些应升级为 hard failure

---

## 10. 推荐实施顺序

如果只按收益/风险比排序，建议优先级如下：

1. 字幕规则清洗
2. 中文长路径 text-first
3. post-merge seam cleanup
4. zh-specific quality evaluator
5. glossary 稳定化
6. 质量驱动 reroute

也就是说，最先要解决的不是“换更强模型”，而是：

> 不要继续把脏字幕直接送进后续链路。

---

## 11. 近期执行建议

建议下一轮真正实施时，按以下 PR 序列推进：

### PR-A

- 新建 subtitle cleanup 模块
- 修 `parse_vtt()` / `parse_vtt_segments()` 中文拼接问题
- 增加基础 overlap 清理

### PR-B

- 中文单语长路径改为 text-first
- 让 `segments` 与正文处理解耦

### PR-C

- merge 后 deterministic seam cleanup
- 增强 `verify-quality`

### PR-D

- glossary / proper noun 稳定化
- 引入 subtitle quality score 与 reroute policy

---

## 12. 结论

要显著提升最终交付物质量，正确方向不是“继续堆 prompt”，而是：

> 建立一条更清晰的文本质量生产链：
> `deterministic cleanup -> better chunk routing -> constrained LLM transformation -> post-merge cleanup -> stronger evaluator`

这条路线与项目当前的 `workflow-native`、`script-first`、`quality-gated` 哲学是一致的。

它不会把系统变成一个过度自由的 agent，
但会让最终交付物更像一个可控、可验证、可持续优化的生产系统产物。

---

<a id="en"></a>

## English Version

## 1. Document Scope

This document focuses on a narrower and more concrete question:

> How do we systematically improve the text quality, readability, stability, and consistency of the final Markdown deliverables produced by `yt-transcript`?

It is not a single-bug report, and it is not a prompt-tuning memo for one isolated template.

Its purpose is to break down the full text-production chain from URL to final article and make four things explicit:

1. Where the current quality problems actually come from
2. Which problems should be solved by deterministic code and which should be delegated to the LLM
3. How to build a more reliable evaluation and fallback mechanism
4. How to land the work in phases instead of rewriting the whole pipeline in one shot

This plan complements [SYSTEM_DESIGN_RESEARCH_NOTES_20260319_230057.md](./SYSTEM_DESIGN_RESEARCH_NOTES_20260319_230057.md).
That earlier note discussed system shape and runtime design. This one focuses only on final deliverable quality.

---

## 2. Current Problem Assessment

### 2.1 Surface-Level Symptoms

The current final deliverables still frequently show the following issues:

- unnatural spaces between Chinese characters
- unstable sentence boundaries and fragmented phrasing
- awkward paragraphing and too many short paragraphs
- leftover duplicate fragments caused by subtitle overlap
- chunk-boundary repetition or weak transitions
- unstable product names, proper nouns, channel names, and terminology
- outputs that technically pass `verify-quality` but still read poorly

### 2.2 Root Cause Assessment

The core issue is not that the system lacks prompts.
The real issue is:

> the subtitle path still relies too heavily on the LLM for cleanup, while the deterministic rule layer is too weak and even injects some noise during preprocessing.

More specifically:

- `parse_vtt()` and `parse_vtt_segments()` join cue text with spaces, which is unfriendly to Chinese
- subtitle deduplication only handles exact consecutive duplicates and misses common overlap patterns
- `_normalize_text_body()` only performs very light newline cleanup and does not normalize Chinese text
- long-video routing still leans toward `segments` as the chunk source, which can carry subtitle-granularity noise into later stages
- `merge_content()` only concatenates chunks and does not clean chunk seams
- `verify-quality` is still more structural than readability-oriented for Chinese text

### 2.3 Uneven Cleanup Strength Across Paths

The cleanup strength is currently inconsistent across source paths:

- YouTube Chinese subtitle path:
  mostly depends on the `cleanup_zh` prompt
- English subtitle path:
  mostly uses `structure_only -> translate_only`
- Deepgram Chinese path:
  has stronger rule-based cleanup in addition to prompting
- Deepgram English path:
  focuses mainly on structuring and translation

This creates a noticeable product inconsistency:

- "Chinese subtitles already exist, but the final output is still less clean than the Deepgram version."

---

## 3. Design Goals

### 3.1 Primary Goals

The final deliverable should satisfy four top-level goals:

1. Fidelity:
   preserve meaning, avoid hidden summarization, and never add facts that are not present
2. Readability:
   read like natural Chinese prose rather than a subtitle dump polluted by spacing artifacts
3. Stability:
   similar input classes should lead to similar output quality instead of depending too much on model variance
4. Verifiability:
   quality should not depend on "it looks okay"; it should be supported by measurable checks

### 3.2 Secondary Goals

- narrow the experience gap between Chinese subtitle output and Deepgram Chinese output
- avoid sacrificing body-text quality just to preserve chapter mapping
- prioritize deterministic cleanup before LLM-based semantic structuring
- make quality issues observable and easy to regress

### 3.3 Non-Goals

The following are explicitly out of scope for this round:

- rewriting transcripts into polished editorial articles
- large-scale stylistic polishing or summary-heavy rewriting
- embedding a full second agent inside the skill
- solving all quality problems simply by paying for a stronger model

---

## 4. Design Principles

### 4.1 Rules First, Model Second

We should solve the following with deterministic code first:

- Chinese spacing artifacts
- whitespace before punctuation
- obvious duplicate fragments
- chunk seam duplication
- VTT cue overlap residue

Only after this baseline noise is removed should the LLM handle:

- sentence boundary repair
- paragraphing
- section-title structure
- contextual terminology judgment

### 4.2 Chinese-First, Not English-Assumed

Many current operations implicitly assume that joining text with spaces is safe.
That assumption is much closer to English than Chinese.

The new cleanup layer should explicitly support:

- CJK-aware joins
- Chinese punctuation rules
- mixed Chinese-English text rules
- coexistence of proper nouns and Chinese syntax

### 4.3 Deliverable Quality Must Be Evaluated Independently

The `process` stage cannot be both producer and self-certifier.
We need a more explicit quality evaluator that can judge:

- whether Chinese spacing pollution remains
- whether obvious duplication remains
- whether paragraphing is overly fragmented
- whether chunk boundaries create discontinuities
- whether terminology drift is severe

### 4.4 Preserve Recoverability

All quality improvements should remain consistent with the current runtime philosophy:

- intermediate results stay persistent
- failures stay inspectable
- stages stay resumable
- stop/go decisions remain explicit

---

## 5. Target Architecture

The recommended architecture for deliverable quality can be described as seven layers.

### 5.1 Source Acquisition Layer

Responsibilities:

- fetch metadata, subtitles, and audio
- distinguish "subtitles exist" from "subtitles are actually downloadable"
- record source family, download failures, and fallback behavior

This layer is already relatively mature and is not the main refactor target here.
However, it should start recording subtitle quality signals, not just subtitle existence.

Recommended additions:

- record basic subtitle quality signals such as:
  - punctuation_density
  - repeated_line_ratio
  - avg_cue_length
  - cjk_space_ratio
  - overlap_suspect_ratio

### 5.2 Deterministic Subtitle Cleanup Layer

This is the most important new layer in this plan.

Goal:

> before the LLM sees subtitle text, clean it to a level where it no longer actively harms downstream processing.

The recommendation is to add a dedicated subtitle cleanup module instead of continuing to scatter this logic inside `yt_transcript_utils.py`.

Suggested module shape:

- `kernel/text_cleanup/subtitle.py`
- `kernel/text_cleanup/cjk.py`
- `kernel/text_cleanup/overlap.py`

Core capabilities should include:

1. CJK-aware join
2. Chinese inter-character spacing cleanup
3. whitespace correction around punctuation
4. cue overlap deduplication
5. repeated-phrase compression
6. lightweight terminology normalization hooks
7. diagnostic outputs for quality reports

### 5.3 Source Quality Routing Layer

Current source routing mostly answers one question: "are subtitles available?"
It now needs to answer a better question:

> are subtitles available, usable, and good enough?

Recommended routing policy:

- Chinese subtitles exist and quality is acceptable:
  stay on the subtitle path
- Chinese subtitles exist but quality is clearly poor:
  allow Deepgram, or at least require a stronger cleanup path
- Chinese subtitles do not exist, but English subtitles are acceptable:
  use the bilingual path
- no usable subtitles exist, or subtitle quality is extremely poor:
  use Deepgram

The point is not to make the system overuse Deepgram.
The point is:

> route based on quality, not just based on existence.

### 5.4 Chunking Strategy Layer

The current long-video path still defaults toward `normalized_document -> segments`.
That helps chapter mapping, but it is not necessarily best for body-text quality.

For Chinese monolingual body generation, the chunking preference should be redefined:

- prefer `raw_text` or normalized text for body optimization
- reserve `segments` primarily for:
  - time mapping
  - chapter plans
  - diagnostics

Recommended rule set:

- Chinese monolingual:
  `text-first`
- English bilingual:
  preserve the current structure path
- timed chapter mapping:
  keep relying on `segments`

This reduces the chance that cue-level subtitle fragmentation contaminates body-text optimization.

### 5.5 LLM Transformation Layer

The LLM should no longer carry four jobs at once:

- baseline cleanup
- structure editing
- terminology guessing
- seam repair

It should be narrowed to two prompt families:

1. `cleanup_zh`
   responsible only for:
   - sentence repair
   - paragraphing
   - heading structure
   - conservative correction of very obvious terminology variants

2. `translate_only`
   continuing to focus on bilingual translation only

`cleanup_zh` should explicitly avoid becoming responsible for:

- large-scale deduplication
- heavy spacing repair
- chunk seam repair

Those should be handled earlier by deterministic layers.

### 5.6 Post-Merge Cleanup Layer

This is a clearly missing layer today.

`merge_content()` currently just concatenates processed chunks.
We should add a deterministic polish step after merge to repair:

- repeated sentences across chunk seams
- neighboring short-fragment paragraphs
- excess blank lines
- broken sentence joins at chunk boundaries
- repeated headings or abnormal heading density

Suggested shape:

- `post_merge_cleanup(text, mode, diagnostics)`

Important distinction:

- this is not a style-polish pass
- it is a mechanical repair pass for chunk-induced artifacts

### 5.7 Quality Evaluation Layer

`verify-quality` should remain, but it needs stronger Chinese-specific checks.

Recommended additions:

1. `cjk_spacing_anomaly`
   - ratio of unnatural spaces between Chinese characters
2. `fragment_paragraph_ratio`
   - ratio of overly short paragraphs
3. `duplicate_ngram_ratio`
   - ratio of repeated 2-8 token fragments
4. `chunk_seam_duplication`
   - duplication detection near chunk seams
5. `punctuation_density`
   - whether punctuation density is implausibly low
6. `header_fragment_balance`
   - whether heading count is disproportionate to body length
7. `glossary_drift`
   - whether high-priority terms drifted

Quality reports should distinguish:

- `hard_failures`
- `repairable_warnings`
- `advisory_warnings`

---

## 6. Phased Delivery Plan

### Phase 0: Baseline and Corpus

Goal:

- establish evaluation samples before changing the logic

Work:

- collect 20-30 real URLs
- classify them into:
  - Chinese auto captions
  - Chinese manual captions
  - English subtitles
  - no subtitles, Deepgram fallback
- prepare golden outputs or at least human-annotated issue lists for each class
- preserve current outputs as baseline artifacts

Deliverables:

- `tests/fixtures/quality_corpus/...`
- baseline quality report

### Phase 1: Subtitle Rule Cleanup

Goal:

- remove the most damaging mechanical noise for Chinese reading

Work:

- replace the current space-joining logic in `parse_vtt()` and `parse_vtt_segments()`
- add `_normalize_subtitle_text()`
- add cue-overlap deduplication
- apply subtitle-specific cleanup during normalized document materialization

Acceptance criteria:

- Chinese spacing anomalies drop significantly
- obvious overlap residue drops significantly
- English subtitle paths are not regressed

### Phase 2: Make Chinese Long Paths Text-First

Goal:

- reduce the probability that subtitle fragments enter chunking directly

Work:

- introduce source-kind preference policy in the planner
- prefer text chunking for Chinese monolingual body generation
- keep `segments` for chapter mapping
- allow text body and timed metadata to coexist

Acceptance criteria:

- less duplication in long-video outputs
- chapter features remain intact

### Phase 3: Post-Merge Cleanup

Goal:

- remove mechanical artifacts introduced by chunk seams

Work:

- add deterministic seam cleanup after `merge_content()`
- add seam-duplication detection
- add short-paragraph merging and blank-line cleanup

Acceptance criteria:

- merged text reads more continuously
- no semantic loss is introduced

### Phase 4: Upgrade the Quality Evaluator

Goal:

- make "this reads awkwardly" detectable and reportable

Work:

- extend `verify-quality`
- add Chinese-specific checks
- distinguish warnings from hard failures
- allow runtime repair or reroute decisions based on quality gates

Acceptance criteria:

- known bad samples are detected
- healthy samples are not flooded with false positives

### Phase 5: Stabilize Terminology and Proper Nouns

Goal:

- improve terminology consistency in final outputs

Work:

- add a transcript-oriented glossary builder mode
- extract high-priority terms from title, channel, chapters, and description
- inject controlled glossary context into `cleanup_zh`
- check glossary drift in the quality gate

Acceptance criteria:

- drift on frequent terms drops significantly
- proper-noun error rate drops

### Phase 6: Quality-Driven Reroute and Fallback

Goal:

- allow the system to pick a better path based on quality

Work:

- add a quality score to subtitle paths
- allow stronger cleanup or Deepgram when subtitle quality is extremely poor
- emit explanatory fields for reroute decisions

Acceptance criteria:

- the system is no longer trapped on poor subtitle paths just because subtitles exist
- fallback reasons are explainable and auditable

---

## 7. Testing Strategy

### 7.1 Unit Tests

Add coverage for:

- CJK-aware join
- Chinese spacing cleanup
- overlap deduplication
- seam cleanup
- quality-metric calculation

### 7.2 Regression Tests

Lock in real problematic samples such as:

- Chinese auto captions with spacing pollution
- Chinese auto captions with overlap residue
- long videos with chunk seam duplication
- bilingual English subtitle paths that must not be broken by new cleanup logic

### 7.3 Golden Output Tests

Build a small set of high-value human-approved outputs.

Comparison dimensions:

- structure
- spacing
- paragraphing
- duplication
- terminology

### 7.4 Evaluator Tests

The evaluator must prove both:

- it catches real problems
- it does not over-block healthy text

---

## 8. Key Metrics

The following metrics should be persisted into the quality report:

- `cjk_space_ratio`
- `duplicate_ngram_ratio`
- `overlap_reduction_count`
- `chunk_seam_warning_count`
- `short_paragraph_ratio`
- `header_density`
- `glossary_drift_count`
- `subtitle_quality_score`
- `source_route_reason`
- `post_merge_cleanup_applied`

These metrics can then support:

- quality gates
- regression trend analysis
- reroute decisions
- user-facing explainability

---

## 9. Risks and Tradeoffs

### 9.1 Overly Strong Rules May Damage the Source

Risk:

- deduplication or merging may go too far and delete real content

Mitigation:

- keep rules conservative first
- make each cleaner emit diagnostics
- default high-risk rules to warning mode before auto-apply

### 9.2 Chinese Rules May Disturb English or Mixed-Language Text

Risk:

- CJK cleanup may damage technical terms or version strings

Mitigation:

- use character-class-aware local rules
- treat mixed-language tokens conservatively
- preserve explicit exceptions such as `API`, `LLM`, and `GPT-5.4`

### 9.3 Overly Strict Quality Gates Increase Rerun Cost

Risk:

- too many warnings may cause excessive reruns

Mitigation:

- start with advisory warnings
- collect metric distributions first, then promote only the right checks to hard failures

---

## 10. Recommended Implementation Order

If we rank strictly by impact versus risk, the recommended order is:

1. subtitle rule cleanup
2. Chinese long-path text-first routing
3. post-merge seam cleanup
4. Chinese-specific quality evaluator
5. glossary stabilization
6. quality-driven reroute

In other words, the first thing to solve is not "use a stronger model."
It is:

> stop feeding dirty subtitle text directly into the rest of the pipeline.

---

## 11. Near-Term Execution Advice

For actual implementation, the next round should be organized as the following PR sequence:

### PR-A

- add a subtitle cleanup module
- fix Chinese joining in `parse_vtt()` and `parse_vtt_segments()`
- add baseline overlap cleanup

### PR-B

- switch Chinese monolingual long paths to text-first
- decouple `segments` from body generation

### PR-C

- add deterministic seam cleanup after merge
- strengthen `verify-quality`

### PR-D

- stabilize glossary and proper nouns
- introduce subtitle quality score and reroute policy

---

## 12. Conclusion

To materially improve final deliverable quality, the right direction is not to keep stacking prompts.
The right direction is:

> build a clearer text-quality production chain:
> `deterministic cleanup -> better chunk routing -> constrained LLM transformation -> post-merge cleanup -> stronger evaluator`

This is fully aligned with the project's existing `workflow-native`, `script-first`, and `quality-gated` philosophy.

It will not turn the system into an over-free agent.
But it will make the final deliverable feel much more like the output of a controllable, verifiable, and continuously improvable production system.
