# yt-transcript 正式升级 Roadmap 实现计划

[中文版](#zh-cn) | [English Version](#en)

<a id="zh-cn"></a>

## 中文版

## 1. 文档定位

这份文档是在 `SYSTEM_DESIGN_RESEARCH_NOTES_20260319_230057.md` 基础上，进一步收敛出的正式升级 roadmap。

它不是纯概念讨论，而是面向实现与落地的阶段性计划，目标是把当前项目从：

- `workflow-native process + thin local controls`

演进为：

- `workflow-native kernel + bounded adaptive runtime + external agent orchestration`

这份 roadmap 主要回答四个问题：

1. 升级要达到的目标终态是什么
2. 升级分成哪些阶段最稳妥
3. 每一阶段的代码工作、交付物、验收标准是什么
4. 如何在不中断当前能力的前提下完成迁移

---

## 2. 总体目标

### 2.1 目标终态

目标终态不是把项目改造成一个新的内嵌 agent，而是把当前 skill 内部零散的控制逻辑，提升为一个清晰、可恢复、可解释、可扩展的 `bounded adaptive runtime`。

升级后系统应满足以下目标：

- 保留当前 `script-first`、`local-first`、`quality-gated` 的核心设计
- 保留现有 YouTube URL -> Markdown 的稳定业务骨架
- 用显式 runtime lifecycle 取代分散的控制分支
- 把 retry / repair / replan / fallback / degrade 统一为标准 action model
- 为外层 agent 提供稳定的 `TaskSpec -> RunState -> advance/inspect/finalize` contract
- 让 LLM 只在受限 decision phase 中辅助排序或选择 action，而不接管 orchestration

### 2.2 非目标

以下事项不属于本次升级目标：

- 不将 skill 内核改造成完整 planner agent
- 不让 LLM 自由生成任意下一步动作
- 不推翻现有脚本优先与 JSON 输出体系
- 不一次性重写整个长文本子系统
- 不在当前阶段引入复杂分布式基础设施

---

## 3. 设计原则

升级实施过程中应始终遵守以下原则：

### 3.1 Compatibility First

旧命令、旧状态文件、旧工作流入口尽量继续可用；新增 runtime 应先以兼容层形式落地。

### 3.2 Runtime over Branching

优先把控制逻辑提升为 runtime 抽象，而不是继续在 workflow 代码里增加分支。

### 3.3 Rule First, LLM Optional

优先通过 policy 和规则确定可行动作；LLM 只在模糊场景参与 action ranking。

### 3.4 Persist Everything Important

状态、观察、决策、产物、质量报告、预算消耗都必须有持久化表示。

### 3.5 Explicit Contracts

所有运行时核心对象都应有明确 schema，避免“约定俗成”的隐式结构继续扩散。

### 3.6 Incremental Migration

通过分阶段替换和包裹现有能力，而不是大爆炸式重构。

---

## 4. 目标架构摘要

升级完成后的系统建议由六层组成：

1. `Workflow Layer`
2. `Adaptive Runtime Layer`
3. `Policy Layer`
4. `Tools Layer`
5. `Artifact & Event Layer`
6. `Evaluator Layer`

### 4.1 Workflow Layer

继续表达名义业务骨架：

`preflight -> source -> normalize -> plan -> process -> verify -> assemble -> publish`

### 4.2 Adaptive Runtime Layer

统一驱动以下执行循环：

`Observe -> Evaluate -> Derive Allowed Actions -> Decide -> Validate -> Act -> Commit -> Transition`

### 4.3 Policy Layer

负责预算、阶段约束、失败阈值、动作合法性、降级条件、人工升级条件。

### 4.4 Tools Layer

继续复用现有：

- `scripts/*.sh`
- `yt_transcript_utils.py`
- `kernel/long_text/*`

但统一包装为 typed actions。

### 4.5 Artifact & Event Layer

建立 append-only event log 与 artifact graph，统一管理运行真相。

### 4.6 Evaluator Layer

独立输出质量判断和建议动作，不让 processing 阶段自证正确。

---

## 5. 工作流拆分（Workstreams）

为了避免 roadmap 只按时间切分但缺少职责视角，建议并行按以下 8 个工作流推进。

### 5.1 Workstream A：Runtime Contracts

目标：建立统一运行时对象模型。

核心对象：

- `TaskSpec`
- `RunState`
- `Observation`
- `DecisionRecord`
- `ActionRequest`
- `ActionResult`
- `ArtifactRef`
- `QualityReport`

建议新增模块：

- `kernel/task_runtime/contracts.py`

### 5.2 Workstream B：Lifecycle Runtime

目标：从当前 thin control layer 提升为显式 lifecycle manager。

建议新增模块：

- `kernel/task_runtime/lifecycle.py`
- `kernel/task_runtime/recovery.py`

### 5.3 Workstream C：Policy & Decision

目标：把 allowed actions、阈值、预算、降级、人工升级，统一为显式策略层。

建议新增模块：

- `kernel/task_runtime/policy.py`
- `kernel/task_runtime/decision.py`
- `kernel/task_runtime/ledger.py`

### 5.4 Workstream D：Artifacts & Events

目标：建立统一 event sourcing 风格的运行记录与 artifact 血缘。

建议新增模块：

- `kernel/task_runtime/artifacts.py`
- 可复用/扩展 `kernel/task_runtime/runtime.py`
- 可复用/扩展 `kernel/task_runtime/state.py`

### 5.5 Workstream E：Tool Standardization

目标：把现有工具面收敛为统一 action 接口。

涉及模块：

- `scripts/download.sh`
- `scripts/preflight.sh`
- `yt_transcript_utils.py`
- `kernel/long_text/execution.py`
- `kernel/long_text/processing.py`

### 5.6 Workstream F：Evaluator & Quality Gates

目标：统一 verify、repair trigger、replan trigger 的判定方式。

建议扩展：

- `verify-quality` 结果 schema
- chunk 级质量报告
- run 级质量报告

### 5.7 Workstream G：Outer Agent Contract

目标：对外提供稳定调用面，而不是暴露过多内部命令细节。

建议目标接口：

- `create_run(task_spec)`
- `advance_run(run_id)`
- `inspect_run(run_id)`
- `apply_control(run_id, signal)`
- `resume_run(run_id)`
- `finalize_run(run_id)`

### 5.8 Workstream H：Testing, Migration, Rollout

目标：在不破坏现有功能的前提下逐步切换到新 runtime。

涉及：

- regression tests
- golden fixtures
- state compatibility tests
- migration toggles / feature flags

---

## 6. 分阶段 Roadmap

下面给出建议的正式分阶段实施计划。时间预估以单仓库、局部重构为假设，可按团队规模调整。

## Phase 0：Baseline 与护栏搭建

**目标**
- 固化现状
- 建立升级边界
- 为后续重构提供可回归基础

**建议周期**
- `0.5 ~ 1 周`

**主要工作**
- 梳理当前 runtime 入口与控制点
- 明确现有命令面与 JSON 输出 contract
- 识别必须兼容的状态文件、manifest、telemetry 格式
- 列出现有关键回归路径：短视频字幕、长视频 chunk、Deepgram fallback、pause/resume、replan
- 增加/完善最关键 regression fixtures

**重点文件**
- `yt_transcript_utils.py`
- `kernel/task_runtime/runtime.py`
- `kernel/task_runtime/state.py`
- `kernel/task_runtime/controller.py`
- `kernel/long_text/execution.py`
- `tests/regression_runtime.py`
- `tests/regression_workflow.py`

**交付物**
- 基线回归矩阵
- 兼容性边界清单
- vNext runtime 模块骨架清单

**验收标准**
- 可以清楚回答：哪些接口必须兼容，哪些内部实现允许调整
- 回归测试可覆盖最关键路径

---

## Phase 1：Contracts 与可观测性统一

**目标**
- 先统一核心数据模型与事件记录，不急于替换控制逻辑

**建议周期**
- `1 ~ 2 周`

**主要工作**
- 新建 `kernel/task_runtime/contracts.py`
- 定义 `TaskSpec`、`RunState`、`Observation`、`DecisionRecord`、`ActionResult`、`ArtifactRef`、`QualityReport`
- 在不改变现有行为的前提下，把旧命令结果包裹进统一 envelope
- 扩展 telemetry / event schema，使运行事件可统一落盘
- 为新旧字段建立适配层

**重点文件**
- 新增 `kernel/task_runtime/contracts.py`
- 扩展 `kernel/task_runtime/runtime.py`
- 扩展 `kernel/task_runtime/state.py`
- 可能部分触达 `yt_transcript_utils.py`

**交付物**
- 首版 runtime contracts
- 统一 command/result/event envelope
- 兼容旧输出的适配器

**验收标准**
- 不改业务流程也能生成统一结构化事件与状态对象
- 新 schema 可覆盖当前主要运行结果

---

## Phase 2：Lifecycle Runtime Shell

**目标**
- 建立显式 lifecycle manager，但先不引入复杂 decision logic

**建议周期**
- `1 ~ 2 周`

**主要工作**
- 新建 `kernel/task_runtime/lifecycle.py`
- 设计顶层 lifecycle states
- 设计 `Observe -> Evaluate -> Derive Allowed Actions -> Decide -> Validate -> Act -> Commit -> Transition` 的运行壳
- 将当前 `pause/cancel/resume/ownership` 语义并入 lifecycle shell
- 保留旧的控制逻辑，但通过 shell 调用

**重点文件**
- 新增 `kernel/task_runtime/lifecycle.py`
- 调整 `kernel/task_runtime/controller.py`
- 适配 `kernel/long_text/execution.py`

**交付物**
- 首版 lifecycle runtime shell
- 统一的 run summary 视图
- 顶层状态转换规则草案

**验收标准**
- 当前运行流程可通过 lifecycle shell 进入和退出主要阶段
- pause / cancel / resume 逻辑保持兼容

---

## Phase 3：Action Model 与 Policy Engine

**目标**
- 把“散落的 if/else 控制”提升为动作与策略系统

**建议周期**
- `2 周`

**主要工作**
- 新建 `kernel/task_runtime/policy.py`
- 新建 `kernel/task_runtime/decision.py`
- 新建 `kernel/task_runtime/ledger.py`
- 定义标准动作集合：
  - `continue_stage`
  - `retry_action`
  - `repair_chunk`
  - `replan_remaining`
  - `shrink_chunk_size`
  - `switch_model_profile`
  - `fallback_to_deepgram`
  - `degrade_output_mode`
  - `pause_run`
  - `abort_run`
  - `request_human_escalation`
- 把 allowed actions derivation 做成显式逻辑
- 引入 budget / retry / replan / degrade 策略
- 记录 `DecisionRecord`

**交付物**
- 首版 action model
- policy engine
- decision log
- budget ledger

**验收标准**
- 在关键阶段可输出明确 allowed actions
- 关键控制决策均有结构化 `DecisionRecord`
- 不再依赖隐式分支解释“为什么这么做”

---

## Phase 4：接入长文本处理与自适应控制

**目标**
- 让 runtime 真正接管 long-text processing 的局部执行控制

**建议周期**
- `2 ~ 3 周`

**主要工作**
- 给 `processing` 阶段建立子状态机
- 把 chunk retry / repair / replan / merge trigger 纳入统一动作模型
- 将当前 `process_chunks_with_replans` 逻辑逐步收口到 runtime
- 把 chunk warnings / failures / superseded 状态纳入统一 event / artifact 体系
- 实现 `recovery.py` 中的 resume-safe 逻辑

**重点文件**
- `kernel/long_text/execution.py`
- `kernel/long_text/processing.py`
- `kernel/task_runtime/lifecycle.py`
- `kernel/task_runtime/recovery.py`
- `kernel/task_runtime/artifacts.py`

**交付物**
- processing 子状态机
- runtime 驱动的 chunk-level 控制
- 更完整的 resume / recover 机制

**验收标准**
- 长视频路径可以通过新 runtime 完成至少一条端到端流程
- retry / repair / replan 均通过统一 action 执行
- 中断恢复不依赖手工猜测状态

---

## Phase 5：Evaluator、质量门与 LLM-Optional Decisioning

**目标**
- 把质量判断和模糊决策从实现细节提升到系统层

**建议周期**
- `2 周`

**主要工作**
- 统一 `QualityReport` schema
- 建立 run 级 / chunk 级质量报告
- 把 verify 失败映射到标准动作建议
- 仅在模糊场景引入 LLM-assisted action ranking
- 为 LLM decision 设置强约束：只能在 `allowed_actions` 中选
- 加入预算压力、质量风险、超时模式等策略输入

**交付物**
- evaluator contract
- quality-gated decision flow
- LLM-assisted 决策原型（受限）

**验收标准**
- 质量门不再只是零散校验点，而是可结构化消费的 evaluator 输出
- LLM 参与决策时不能越权
- rule-only 路径仍然成立，可作为降级方案

---

## Phase 6：对外接口收敛与迁移完成

**目标**
- 对外暴露稳定 skill runtime contract，完成新旧控制面的迁移

**建议周期**
- `1 ~ 2 周`

**主要工作**
- 收敛外部调用面为 `create_run / advance_run / inspect_run / finalize_run`
- 保留兼容 CLI，同时逐步将 CLI 实现映射到 runtime API
- 更新文档：`README.md`、`SYSTEM_DESIGN.md`
- 标注 deprecated 的旧控制 helper
- 建立 feature flag / migration toggle 的默认切换策略

**交付物**
- 稳定的 runtime-facing API
- 完整迁移说明
- 更新后的系统设计文档

**验收标准**
- 外层 agent 可把 skill 当成稳定 runtime 能力调用
- 新 runtime 成为首选路径
- 旧路径保留兼容但不再承担主设计语义

---

## 7. 里程碑定义

### M1：Schema Ready

完成条件：
- 核心 contracts 可用
- 统一 event/result envelope 落地
- 现有流程仍可运行

对应阶段：
- `Phase 0 ~ Phase 1`

### M2：Runtime Shell Ready

完成条件：
- lifecycle shell 可驱动主阶段
- ownership / pause / cancel / resume 全部统一接入

对应阶段：
- `Phase 2`

### M3：Policy-Controlled Runtime Ready

完成条件：
- allowed actions、policy、decision log 已落地
- 关键流程可通过 action model 控制

对应阶段：
- `Phase 3`

### M4：Adaptive Long-Text Runtime Ready

完成条件：
- 长文本处理由 runtime 主导局部控制
- repair / replan / recover 成为一等运行语义

对应阶段：
- `Phase 4`

### M5：Quality-Gated Adaptive Runtime Ready

完成条件：
- evaluator 可统一输出质量报告
- LLM-assisted decisioning 可在约束下工作

对应阶段：
- `Phase 5`

### M6：Production Contract Ready

完成条件：
- 对外 runtime contract 稳定
- 文档、测试、迁移说明完整

对应阶段：
- `Phase 6`

---

## 8. 模块映射建议

### 8.1 保留并扩展

- `kernel/task_runtime/runtime.py`
  - 继续负责 ownership、command envelope、telemetry append
- `kernel/task_runtime/state.py`
  - 继续负责 manifest/runtime persistence、pause/cancel、runtime summary
- `kernel/long_text/*`
  - 继续作为长文本能力面
- `yt_transcript_utils.py`
  - 继续作为 CLI / compatibility façade

### 8.2 拆分与重定位

- `kernel/task_runtime/controller.py`
  - 从通用 helper 升级为更薄的 façade；复杂控制语义迁移到新 runtime 模块

### 8.3 新增模块

- `kernel/task_runtime/contracts.py`
- `kernel/task_runtime/lifecycle.py`
- `kernel/task_runtime/policy.py`
- `kernel/task_runtime/decision.py`
- `kernel/task_runtime/recovery.py`
- `kernel/task_runtime/artifacts.py`
- `kernel/task_runtime/ledger.py`

---

## 9. 测试与验证策略

### 9.1 测试层级

建议分四层验证：

#### 1. Schema / contract tests
- 验证各 runtime 对象 schema 稳定

#### 2. Unit tests
- 验证 policy derivation
- 验证 state transitions
- 验证 budget ledger
- 验证 decision validation

#### 3. Integration tests
- 验证 `create_run -> advance_run -> inspect_run`
- 验证 pause/resume/cancel
- 验证 repair/replan/fallback

#### 4. Regression tests
- 短视频字幕路径
- 长视频 chunk 路径
- 无字幕 Deepgram 路径
- bilingual 路径
- 中断恢复路径

### 9.2 回滚策略

每个 Phase 都应保留回滚能力：

- 新 runtime 入口 behind feature flag
- 新 schema 对旧字段保持兼容
- 核心 CLI 行为可临时回退至旧控制面

---

## 10. 风险与缓解

### 10.1 风险：状态机过度设计

**表现**
- 顶层状态过多
- 子状态机过细
- 开发成本高于收益

**缓解**
- 顶层状态只保留真正必要的生命周期语义
- 复杂性优先留给 action model 与 policy，而不是堆叠状态数量

### 10.2 风险：新 runtime 与旧逻辑双轨太久

**表现**
- 双维护成本高
- 行为难以解释

**缓解**
- 每个阶段都定义明确替换边界
- 到 `Phase 6` 必须明确首选路径

### 10.3 风险：LLM-assisted decisioning 抢走控制权

**表现**
- 动作越界
- 决策不可预测
- 调试困难

**缓解**
- 先 rule-only，再引入 llm-assisted
- LLM 只允许在 `allowed_actions` 中选
- 所有决策必须生成 `DecisionRecord`

### 10.4 风险：兼容性破坏当前稳定路径

**表现**
- 老命令输出被破坏
- 老状态文件无法 resume

**缓解**
- 先做 contract adapter
- 先做 envelope，不先改底层业务路径
- 每阶段用 regression fixtures 验证

---

## 11. 建议的近期执行顺序（未来 2~4 周）

如果需要一个更现实的“马上开始做什么”版本，建议按下面顺序推进：

### Step 1
- 固化 baseline
- 补强 regression tests
- 形成兼容性边界清单

### Step 2
- 先落 `contracts.py`
- 统一 result / event / state envelope

### Step 3
- 建立 `lifecycle.py`
- 让现有流程经过 runtime shell 执行

### Step 4
- 建立 `policy.py` 与 `decision.py`
- 引入 allowed actions
- 先不让 LLM 参与决策

### Step 5
- 把 long-text `process_chunks_with_replans` 纳入 runtime action model

### Step 6
- 最后再给 decision phase 增加 llm-assisted ranking

这是最稳的路径，因为它保证：

- 先有 runtime 结构
- 再有 runtime 策略
- 最后才增加 runtime 智能性

---

## 12. 一个简洁的最终执行结论

本次正式升级 roadmap 的核心结论可以压缩成一句话：

> 先把当前项目从“由 workflow 分支控制的能力系统”升级成“由显式 lifecycle runtime 驱动的能力系统”，再在 policy 约束下逐步引入 action model、quality evaluator 与受限 LLM-assisted decisioning。

如果继续压缩成更适合工程决策的一句话：

> 先做 runtime substrate，再做 adaptive behavior，最后才做 optional intelligence。

---

<a id="en"></a>

## English Version

## 1. Document Positioning

This document is the formal implementation roadmap derived from `SYSTEM_DESIGN_RESEARCH_NOTES_20260319_230057.md`.

It is no longer just conceptual analysis. It is an implementation-oriented plan for evolving the current system from:

- `workflow-native process + thin local controls`

into:

- `workflow-native kernel + bounded adaptive runtime + external agent orchestration`

It answers four practical questions:

1. What is the target end state?
2. What phases should the upgrade be split into?
3. What code work, deliverables, and exit criteria belong to each phase?
4. How can migration happen without disrupting the current stable capability?

---

## 2. Overall Objective

### 2.1 Target End State

The target is not to turn the skill into a new embedded planner agent.

The target is to elevate the current scattered execution controls into a clear, recoverable, explainable, and extensible `bounded adaptive runtime`.

The upgraded system should:

- preserve the current `script-first`, `local-first`, and `quality-gated` design philosophy
- preserve the stable YouTube URL -> Markdown business skeleton
- replace scattered control branching with an explicit runtime lifecycle
- unify retry / repair / replan / fallback / degrade as a standard action model
- expose a stable `TaskSpec -> RunState -> advance/inspect/finalize` contract to the outer agent
- let the LLM assist only inside a constrained decision phase rather than owning orchestration

### 2.2 Non-Goals

The following are explicitly out of scope:

- turning the skill into a full planner agent
- letting the LLM invent arbitrary next actions
- replacing the current script-first and JSON-output architecture
- rewriting the whole long-text subsystem in one step
- introducing heavy distributed infrastructure at this stage

---

## 3. Design Principles

The implementation should follow these principles throughout:

### 3.1 Compatibility First

Existing commands, state files, and workflow entrypoints should remain usable wherever possible. The new runtime should initially land as a compatibility layer.

### 3.2 Runtime over Branching

Prefer lifting control logic into runtime abstractions rather than adding more workflow branches.

### 3.3 Rule First, LLM Optional

Use policy and deterministic rules to derive legal actions. The LLM only participates in ambiguous action ranking.

### 3.4 Persist Everything Important

State, observations, decisions, artifacts, quality reports, and budget usage must all have durable representations.

### 3.5 Explicit Contracts

All core runtime objects should have clear schemas rather than relying on implicit conventions.

### 3.6 Incremental Migration

Wrap and replace current capabilities in stages rather than attempting a big-bang rewrite.

---

## 4. Target Architecture Summary

The upgraded system should be understood as six layers:

1. `Workflow Layer`
2. `Adaptive Runtime Layer`
3. `Policy Layer`
4. `Tools Layer`
5. `Artifact & Event Layer`
6. `Evaluator Layer`

### 4.1 Workflow Layer

Keep expressing the nominal business skeleton:

`preflight -> source -> normalize -> plan -> process -> verify -> assemble -> publish`

### 4.2 Adaptive Runtime Layer

Drive a unified execution loop:

`Observe -> Evaluate -> Derive Allowed Actions -> Decide -> Validate -> Act -> Commit -> Transition`

### 4.3 Policy Layer

Own budgets, stage constraints, failure thresholds, legal actions, degrade rules, and human escalation conditions.

### 4.4 Tools Layer

Continue reusing:

- `scripts/*.sh`
- `yt_transcript_utils.py`
- `kernel/long_text/*`

but wrap them as typed actions.

### 4.5 Artifact & Event Layer

Introduce append-only event logs and an artifact graph as the persisted source of truth.

### 4.6 Evaluator Layer

Produce independent quality judgments and recommended actions instead of letting processing effectively self-certify.

---

## 5. Workstreams

To avoid a roadmap that is only time-based, implementation should also be organized by responsibility.

### 5.1 Workstream A: Runtime Contracts

Goal: establish the unified runtime object model.

Core objects:

- `TaskSpec`
- `RunState`
- `Observation`
- `DecisionRecord`
- `ActionRequest`
- `ActionResult`
- `ArtifactRef`
- `QualityReport`

Recommended module:

- `kernel/task_runtime/contracts.py`

### 5.2 Workstream B: Lifecycle Runtime

Goal: evolve the current thin control layer into an explicit lifecycle manager.

Recommended modules:

- `kernel/task_runtime/lifecycle.py`
- `kernel/task_runtime/recovery.py`

### 5.3 Workstream C: Policy & Decision

Goal: make allowed actions, thresholds, budgets, degradation rules, and escalation conditions explicit.

Recommended modules:

- `kernel/task_runtime/policy.py`
- `kernel/task_runtime/decision.py`
- `kernel/task_runtime/ledger.py`

### 5.4 Workstream D: Artifacts & Events

Goal: build unified runtime records and artifact lineage in an event-sourced style.

Recommended modules:

- `kernel/task_runtime/artifacts.py`
- extend `kernel/task_runtime/runtime.py`
- extend `kernel/task_runtime/state.py`

### 5.5 Workstream E: Tool Standardization

Goal: standardize the existing tool plane behind typed actions.

Relevant modules:

- `scripts/download.sh`
- `scripts/preflight.sh`
- `yt_transcript_utils.py`
- `kernel/long_text/execution.py`
- `kernel/long_text/processing.py`

### 5.6 Workstream F: Evaluator & Quality Gates

Goal: unify verify results, repair triggers, and replan triggers into a coherent evaluator surface.

Suggested scope:

- `verify-quality` schema cleanup
- chunk-level quality reports
- run-level quality reports

### 5.7 Workstream G: Outer Agent Contract

Goal: expose a stable runtime-facing skill interface.

Target interface:

- `create_run(task_spec)`
- `advance_run(run_id)`
- `inspect_run(run_id)`
- `apply_control(run_id, signal)`
- `resume_run(run_id)`
- `finalize_run(run_id)`

### 5.8 Workstream H: Testing, Migration, Rollout

Goal: transition to the new runtime without breaking the current capability.

Scope:

- regression tests
- golden fixtures
- state compatibility tests
- migration toggles / feature flags

---

## 6. Phase-by-Phase Roadmap

The following is the recommended formal staged implementation plan.

## Phase 0: Baseline and Guardrails

**Goal**
- freeze the current baseline
- define upgrade boundaries
- create regression safety for refactoring

**Suggested duration**
- `0.5 ~ 1 week`

**Primary work**
- inventory current runtime entrypoints and control surfaces
- identify command and JSON contracts that must stay compatible
- identify state, manifest, and telemetry formats that must remain resumable
- define key regression paths: short subtitle flow, long chunk flow, Deepgram fallback, pause/resume, replan
- strengthen the most important regression fixtures

**Key files**
- `yt_transcript_utils.py`
- `kernel/task_runtime/runtime.py`
- `kernel/task_runtime/state.py`
- `kernel/task_runtime/controller.py`
- `kernel/long_text/execution.py`
- `tests/regression_runtime.py`
- `tests/regression_workflow.py`

**Deliverables**
- baseline regression matrix
- compatibility boundary list
- vNext runtime module skeleton plan

**Exit criteria**
- the team can clearly state what must remain compatible and what may change internally
- regression coverage exists for the most important flows

---

## Phase 1: Contracts and Observability

**Goal**
- unify core data contracts and event recording before replacing control logic

**Suggested duration**
- `1 ~ 2 weeks`

**Primary work**
- add `kernel/task_runtime/contracts.py`
- define `TaskSpec`, `RunState`, `Observation`, `DecisionRecord`, `ActionResult`, `ArtifactRef`, `QualityReport`
- wrap existing results in a unified envelope without changing business behavior
- extend telemetry / event schema to support unified persistence
- create adapters between old and new field layouts

**Deliverables**
- initial runtime contracts
- unified command/result/event envelope
- compatibility adapters for legacy outputs

**Exit criteria**
- the system can emit unified structured state and events without changing nominal behavior
- the new schema covers the primary current execution outcomes

---

## Phase 2: Lifecycle Runtime Shell

**Goal**
- introduce an explicit lifecycle manager without yet adding advanced decisioning

**Suggested duration**
- `1 ~ 2 weeks`

**Primary work**
- add `kernel/task_runtime/lifecycle.py`
- define top-level lifecycle states
- build the shell around `Observe -> Evaluate -> Derive Allowed Actions -> Decide -> Validate -> Act -> Commit -> Transition`
- route current pause/cancel/resume/ownership semantics through the lifecycle shell
- keep old control logic but invoke it through the new shell

**Deliverables**
- initial lifecycle runtime shell
- unified run summary view
- first state transition rules

**Exit criteria**
- current flows can enter and leave major stages through the lifecycle shell
- pause / cancel / resume semantics remain compatible

---

## Phase 3: Action Model and Policy Engine

**Goal**
- lift scattered control branching into a standardized action and policy system

**Suggested duration**
- `2 weeks`

**Primary work**
- add `kernel/task_runtime/policy.py`
- add `kernel/task_runtime/decision.py`
- add `kernel/task_runtime/ledger.py`
- define a standard action set:
  - `continue_stage`
  - `retry_action`
  - `repair_chunk`
  - `replan_remaining`
  - `shrink_chunk_size`
  - `switch_model_profile`
  - `fallback_to_deepgram`
  - `degrade_output_mode`
  - `pause_run`
  - `abort_run`
  - `request_human_escalation`
- make allowed-action derivation explicit
- add budget / retry / replan / degrade policy logic
- persist `DecisionRecord`

**Deliverables**
- initial action model
- policy engine
- decision log
- budget ledger

**Exit criteria**
- major runtime stages can derive explicit allowed actions
- key control decisions produce structured `DecisionRecord`s
- “why did the system do this?” no longer depends on implicit branching

---

## Phase 4: Long-Text Integration and Adaptive Control

**Goal**
- let the runtime actually own local control of long-text processing

**Suggested duration**
- `2 ~ 3 weeks`

**Primary work**
- create a `processing` sub-state machine
- bring chunk retry / repair / replan / merge triggers into the unified action model
- gradually fold `process_chunks_with_replans` into the runtime
- unify chunk warnings / failures / superseded status inside the event and artifact system
- implement resume-safe behavior in `recovery.py`

**Deliverables**
- processing sub-state machine
- runtime-driven chunk control
- improved resume / recover behavior

**Exit criteria**
- at least one long-video path completes end to end through the new runtime
- retry / repair / replan all run as standardized actions
- interrupted runs can be resumed without manual guesswork

---

## Phase 5: Evaluator, Quality Gates, and LLM-Optional Decisioning

**Goal**
- elevate quality judgment and ambiguous decisions into system-level components

**Suggested duration**
- `2 weeks`

**Primary work**
- unify the `QualityReport` schema
- create chunk-level and run-level quality reports
- map verify failures to standardized recommended actions
- add LLM-assisted action ranking only for ambiguous scenarios
- strictly constrain LLM-based decisions to the `allowed_actions` set
- include budget pressure, quality risk, and timeout patterns as decision inputs

**Deliverables**
- evaluator contract
- quality-gated decision flow
- constrained LLM-assisted decisioning prototype

**Exit criteria**
- quality gates become structured evaluator outputs rather than scattered checks
- LLM participation cannot violate policy or invent actions
- a rule-only path still exists as the fallback mode

---

## Phase 6: External Interface Convergence and Migration Completion

**Goal**
- expose a stable skill runtime contract and finish migration from the old control model

**Suggested duration**
- `1 ~ 2 weeks`

**Primary work**
- converge outer-facing usage to `create_run / advance_run / inspect_run / finalize_run`
- preserve CLI compatibility while mapping CLI behavior onto the runtime API
- update `README.md` and `SYSTEM_DESIGN.md`
- mark old control helpers as deprecated
- define default migration-toggle behavior

**Deliverables**
- stable runtime-facing API
- migration notes
- updated system design docs

**Exit criteria**
- the outer agent can treat the skill as a stable runtime capability
- the new runtime becomes the preferred path
- the old path remains for compatibility but is no longer the primary design center

---

## 7. Milestones

### M1: Schema Ready

Done when:
- core contracts exist
- unified event/result envelopes are in place
- current flows still run

Maps to:
- `Phase 0 ~ Phase 1`

### M2: Runtime Shell Ready

Done when:
- the lifecycle shell drives major stages
- ownership / pause / cancel / resume are unified

Maps to:
- `Phase 2`

### M3: Policy-Controlled Runtime Ready

Done when:
- allowed actions, policy, and decision logs are live
- key flows are controlled through the action model

Maps to:
- `Phase 3`

### M4: Adaptive Long-Text Runtime Ready

Done when:
- long-text processing is locally controlled by the runtime
- repair / replan / recover are first-class runtime semantics

Maps to:
- `Phase 4`

### M5: Quality-Gated Adaptive Runtime Ready

Done when:
- evaluator outputs structured quality reports
- constrained LLM-assisted decisioning works

Maps to:
- `Phase 5`

### M6: Production Contract Ready

Done when:
- the runtime-facing external contract is stable
- docs, tests, and migration notes are complete

Maps to:
- `Phase 6`

---

## 8. Suggested Module Mapping

### 8.1 Keep and Extend

- `kernel/task_runtime/runtime.py`
  - keep ownership, command envelope, telemetry append
- `kernel/task_runtime/state.py`
  - keep persistence, pause/cancel, runtime summary
- `kernel/long_text/*`
  - keep as the long-text capability plane
- `yt_transcript_utils.py`
  - keep as the CLI and compatibility façade

### 8.2 Split and Reposition

- `kernel/task_runtime/controller.py`
  - reduce it into a thinner façade; migrate richer semantics into new runtime modules

### 8.3 New Modules

- `kernel/task_runtime/contracts.py`
- `kernel/task_runtime/lifecycle.py`
- `kernel/task_runtime/policy.py`
- `kernel/task_runtime/decision.py`
- `kernel/task_runtime/recovery.py`
- `kernel/task_runtime/artifacts.py`
- `kernel/task_runtime/ledger.py`

---

## 9. Testing and Validation Strategy

### 9.1 Test Layers

Recommended four-layer validation:

#### 1. Schema / contract tests
- verify runtime object schemas remain stable

#### 2. Unit tests
- policy derivation
- state transitions
- budget ledger behavior
- decision validation

#### 3. Integration tests
- `create_run -> advance_run -> inspect_run`
- pause/resume/cancel
- repair/replan/fallback

#### 4. Regression tests
- short subtitle path
- long chunk path
- Deepgram fallback path
- bilingual path
- interrupted-resume path

### 9.2 Rollback Strategy

Each phase should be reversible:

- keep the new runtime entry behind a feature flag at first
- maintain compatibility with legacy fields
- allow CLI behavior to fall back to the old control surface when needed

---

## 10. Risks and Mitigations

### 10.1 Risk: Overdesigned State Machine

**Symptoms**
- too many top-level states
- too many sub-states
- implementation cost exceeds practical benefit

**Mitigation**
- keep top-level lifecycle states minimal
- put more complexity into action/policy logic rather than multiplying states

### 10.2 Risk: Dual Runtime Too Long

**Symptoms**
- high maintenance cost
- hard-to-explain behavior differences

**Mitigation**
- define replacement boundaries for every phase
- by `Phase 6`, clearly define the preferred path

### 10.3 Risk: LLM-Assisted Decisioning Grabs Too Much Control

**Symptoms**
- action overreach
- unpredictable decisions
- debugging difficulty

**Mitigation**
- start rule-only first
- restrict the LLM to `allowed_actions`
- require every decision to emit a `DecisionRecord`

### 10.4 Risk: Compatibility Breaks Stable Paths

**Symptoms**
- legacy command outputs break
- legacy state cannot resume

**Mitigation**
- build contract adapters first
- add envelopes first, before changing business paths
- verify each phase with regression fixtures

---

## 11. Recommended Near-Term Execution Order (Next 2–4 Weeks)

If a more immediate “what should we start doing now?” version is needed, the recommended order is:

### Step 1
- freeze the baseline
- strengthen regression tests
- produce a compatibility-boundary list

### Step 2
- land `contracts.py`
- unify result / event / state envelopes

### Step 3
- build `lifecycle.py`
- route current flows through the runtime shell

### Step 4
- build `policy.py` and `decision.py`
- introduce allowed actions
- keep decisioning rule-only at first

### Step 5
- fold long-text `process_chunks_with_replans` into the runtime action model

### Step 6
- only then add constrained LLM-assisted ranking in the decision phase

This is the safest order because it guarantees:

- runtime structure first
- runtime policy second
- runtime intelligence last

---

## 12. One Compact Execution Conclusion

The roadmap can be compressed into one sentence:

> First upgrade the project from a capability system controlled by workflow branching into a capability system driven by an explicit lifecycle runtime; then gradually add a standardized action model, a quality evaluator, and constrained LLM-assisted decisioning under policy control.

An even shorter engineering version is:

> Build the runtime substrate first, adaptive behavior second, and optional intelligence last.
