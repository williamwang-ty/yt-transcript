# yt-transcript 系统设计思考研究笔记

[中文版](#zh-cn) | [English Version](#en)

<a id="zh-cn"></a>

## 中文版

## 1. 笔记定位

这份笔记不是对现有代码的逐文件注释，也不是立刻执行的重构任务单。

它的目标是基于本轮讨论，对 `yt-transcript` 当前系统形态、潜在升级方向、与 agent 架构关系、以及下一阶段最合适的设计演化路径，做一份系统化梳理。

本笔记重点回答五个问题：

1. 这个项目当前到底属于什么架构范式
2. 它为什么不是典型的 `agentic native` 架构
3. 如果要引入更强的自适应能力，应该怎么做才不会和外部 agent 重复
4. 为什么下一步最适合升级为更清晰的 `adaptive runtime`
5. 在这个问题领域里，什么样的设计模式组合最优雅

---

## 2. 当前项目的架构判断

### 2.1 核心判断

`yt-transcript` 当前的主导架构，不是“LLM agent 主导、harness 负责约束边界”的 `agentic native` 架构。

它更准确地属于：

- `workflow-native`
- `script-first`
- `local-first`
- `stateful`
- `quality-gated`

换句话说，它的本质是：

> 由确定性代码与脚本拥有编排权，LLM 仅作为部分节点的文本处理手段。

### 2.2 依据

从项目文档和实现可以归纳出以下特征：

- 高风险分支放在脚本与结构化 JSON 输出中，而不是依赖 prompt 内隐推理
- 显式持久化 state / manifest / telemetry / control signals
- pause / resume / cancel / ownership / retry / replan 都由 runtime 代码控制
- LLM 被嵌入在“文本变换”与“长文本处理”阶段，而非全局 orchestrator
- 最终 stop/go 依赖验证与质量门，而不是依赖模型自信

所以，这个项目的运行哲学不是“让 LLM 自主完成任务”，而是：

> 把 URL 到 Markdown 的整条生产链做成一个可靠、可恢复、可检查、可验证的本地系统。

### 2.3 当前架构可以怎么概括

可以把现状概括为：

`workflow-native process + local runtime controls + LLM-powered transformation nodes`

这里的关键词是：

- `process`：主流程是显式定义的
- `runtime controls`：已经有 pause/cancel/replan/ownership 等薄控制层
- `LLM-powered nodes`：LLM 是节点处理器，而不是整个系统的控制面

---

## 3. 为什么它不是 agentic native

### 3.1 `agentic native` 的典型特征

如果一个系统是典型 `agentic native`，通常会有以下特征：

- 编排权主要在 agent runtime，而不是固定工作流
- harness 主要负责权限、预算、工具边界、输出 contract
- agent 自己做任务分解、路径选择、动态重规划
- 系统主抽象是 goal / tools / observations / actions / reflections
- 工作流是 agent 运行时生成的，而不是事先编码好的主骨架

### 3.2 当前项目与之相反的地方

当前项目不是“先有 agent，再由 agent 选择工具执行”；而是“先有流程，再把 LLM 插到少数处理点里”。

具体表现为：

- 流程的大框架是预先定义好的，不是 agent 临场生成的
- 路由与状态转移主要由代码做，不由模型来决定
- replan 虽然存在，但仍是 bounded、局部、受限的自动控制，而非开放式 agent 策略推演
- 跨阶段的业务结构不是由 agent runtime 动态产生，而是由设计文档和脚本预先规定

### 3.3 这不是缺点，而是有意设计

这不意味着“当前系统落后”。

恰恰相反，对当前问题域而言，这样的选择是合理的：

- 任务骨架高度稳定
- 输入不确定，但解决路径的大类是可枚举的
- 输出是交付型产物，不是开放式对话
- 可恢复性和可审计性远比“完全自由自主”更重要

因此，当前系统不是“不够 agentic”，而是有意选择了：

> `reliability-first orchestration`

---

## 4. 当前方案的优势与边界

### 4.1 当前方案的优势

对这个项目来说，现有 `workflow-native process` 有明显优势：

#### 1. 主流程稳定

这个项目不是开放式任务空间。其 nominal path 大体稳定：

`preflight -> source -> normalize -> plan -> process -> verify -> assemble`

当主路径已知时，用工作流骨架做编排通常比让 agent 每次重新规划更稳。

#### 2. 可恢复性强

长视频处理天然需要：

- 断点续跑
- 中间产物保留
- chunk 级状态
- pause / resume / cancel
- 失败后的 repair / replan

这些需求更接近“runtime system”，而不是开放式 agent 对话系统。

#### 3. 成本与可预测性更优

如果全局 orchestration 都交给 agent：

- 会产生额外 planning token
- 会产生额外 reflection / review token
- 会增加决策链的不确定性
- 会让失败定位变得更困难

现有架构在成本、调试性、时延上往往更友好。

#### 4. 更适合作为 skill 被调用

因为这个项目本身就是一个供外部 agent 调用的 skill。

如果 skill 内部再内嵌一个完整 agent，就会出现控制面重复：

- 外部 agent 做高层任务编排
- skill 内部 agent 再做一遍高层任务编排

这会造成职责冲突、状态分裂、调试困难。

### 4.2 当前方案的边界

尽管现状合理，它也存在边界：

#### 1. 执行期自适应能力仍偏分散

现在已经有 pause/cancel/auto-replan/ownership 等控制语义，但它们还没有被统一抽象成完整 runtime model。

#### 2. 条件逻辑分散在流程实现中

很多控制行为仍以局部 if/else、局部 helper、局部 repair/replan 形式存在，而不是由统一生命周期循环驱动。

#### 3. 模糊决策尚未有统一归属

例如：

- 字幕质量一般时是否切 Deepgram
- 某些 chunk 是否更适合 repair 还是 replan
- 紧预算下是换模型还是降级输出

这些决策目前还不是一套显式 runtime decision system 的一部分。

---

## 5. 为什么不适合“再加一层完整 agent”

### 5.1 表面看似合理，实际容易重复

因为这个项目是 skill，很容易产生一种冲动：

> 既然外面是 agent，里面再做一层 agentic control layer，是不是会更智能？

但如果所谓 control layer 被设计成“第二个完整 agent”，问题会立刻出现：

- 内外两层都在做 planning
- 内外两层都在做 replan
- 内外两层都在做 retry / review / reflection
- 内外两层都在解释任务目标

最终结果不是更优雅，而是“agent 套 agent”。

### 5.2 重复控制面的典型问题

#### 1. 职责冲突

外层 agent 说优先速度，内层 agent 说优先质量。

#### 2. 状态割裂

外层有会话上下文，内层又维护一套决策历史，真相来源会变模糊。

#### 3. 调试困难

失败时不容易判断：

- 是工具失败
- 是 runtime 失败
- 还是内层 agent 选错了动作

#### 4. 成本飙升

一个长任务如果每次都多一层 planner/reviewer/self-reflection，会显著增加 token 与时延。

### 5.3 正确升级方向：不是第二个大脑，而是局部自动驾驶

因此，下一步合适的方向不是在 skill 内部再放一个完整 agent，而是升级为：

> `bounded adaptive runtime`

其基本原则是：

- 外层 agent 仍然是唯一“主 agent”
- skill 内部只负责局部执行域的自适应控制
- skill 内部不重新理解用户意图
- skill 内部不做跨 skill 编排
- skill 内部只在已知任务 contract 内决定“下一步执行动作”

---

## 6. 下一步升级的本质：从 workflow control 到 runtime control

本轮讨论中最关键的认识可以总结为以下四个变化：

- `workflow control -> runtime control`
- `path-oriented -> state-oriented`
- `implicit branching -> explicit transitions`
- `special-case recovery -> first-class recovery semantics`

### 6.1 这四句话到底是什么意思

#### 1. `workflow control -> runtime control`

当前系统更多是在“流程代码里决定下一步”，升级后则是“由 runtime 统一驱动状态演化”。

也就是说，真正的核心不再是“代码执行到了哪个函数分支”，而是：

- 当前 run 处于什么生命周期状态
- 当前观察到了什么事实
- 当前允许做哪些动作
- 当前应该从哪些合法动作中选一个

#### 2. `path-oriented -> state-oriented`

当前更偏向看“走到了哪条路径”。

升级后更关注：

- 任务现在处于什么状态
- 该状态允许哪些动作
- 完成动作后会进入什么下一个状态

#### 3. `implicit branching -> explicit transitions`

现有很多行为是隐藏在实现中的条件分支。

升级后，分支不会消失，但会被提升为显式对象：

- transition guards
- allowed actions
- policy checks
- failure handling rules

#### 4. `special-case recovery -> first-class recovery semantics`

现在一些恢复逻辑像“特判技巧”：

- 某函数失败后手工 repair
- 某路径失败后临时 replan
- 某个 chunk 超时后特殊 retry

升级后，这些都应变成 runtime 的一等动作：

- `retry_action`
- `repair_chunk`
- `replan_remaining`
- `fallback_to_deepgram`
- `degrade_output_mode`
- `request_human_escalation`

### 6.2 这不是简单“把 if/else 改成状态机”

更精确地说，升级的本质是：

> 从以命令式流程分支为中心的控制结构，转向以生命周期状态、事件观察、允许动作、状态转移为中心的 runtime。

条件逻辑并不会消失，它们只是从分散的实现细节，变成统一的控制语义。

---

## 7. 最合适的目标形态：Bounded Adaptive Runtime

### 7.1 定义

下一步升级后的 skill 内核，最合适的定义是：

> `workflow-native kernel + bounded adaptive runtime + external agent orchestration`

拆开理解：

- `workflow-native kernel`：保留现有分阶段业务骨架
- `bounded adaptive runtime`：在执行期做有限自适应控制
- `external agent orchestration`：继续由外部 agent 负责高层目标与跨工具编排

### 7.2 为什么强调 bounded

这里的“自适应”绝不能是开放式自由规划，而必须是受限自治。

runtime 不能：

- 自己重新定义任务目标
- 自己决定去调用别的 skill
- 自己生成任意新动作
- 自己绕过预算与策略边界

runtime 只能：

- 读取当前状态与观测
- 从允许动作集合中选择一个动作
- 在本地执行域内 retry / repair / replan / fallback / degrade
- 把全部决策和结果持久化下来

### 7.3 设计目的

`bounded adaptive runtime` 主要解决的是：

- 当前控制逻辑过于分散
- 当前恢复语义还不够统一
- 当前模糊决策缺少清晰归宿
- 当前外层 agent 缺少稳定、可解释的 skill runtime contract

---

## 8. 这个升级真正带来的收益

### 8.1 统一生命周期控制

所有执行期控制行为都由一个统一循环驱动，而不是散落在各阶段实现里。

这意味着：

- pause / cancel / resume 是统一 runtime 语义
- retry / repair / replan / fallback 是统一 runtime action
- checkpoint / recover / finalize 都有统一位置

### 8.2 更强的可恢复性

当系统的运行状态、动作历史、观察结果、产物关系都成为一等对象时，resume 和 debug 就不再依赖“猜测当前系统可能停在了哪一步”。

### 8.3 更清晰的边界

外层 agent 和内层 skill 的职责边界会变清楚：

- 外层 agent 负责“做什么”
- skill runtime 负责“怎么稳地做完”

### 8.4 更易扩展复杂策略

未来如果想加：

- model routing
- 成本优先 / 质量优先 / 速度优先
- 不同视频类型的优化策略
- 批处理时的系列术语一致性

有统一 runtime 和 policy engine 会比继续往 workflow 里塞分支更自然。

### 8.5 更强可解释性

runtime 如果记录显式 `DecisionRecord`，外层 agent 或操作者就能回答：

- 为什么切到了 Deepgram
- 为什么某一轮触发了 replan
- 为什么选择换模型而不是直接降级

### 8.6 LLM 可以“辅助决策”，但不抢编排权

这个升级还有一个微妙但重要的收益：

> 可以把 LLM 放在 decision phase 做有限辅助，但不把全局 orchestration 交给它。

---

## 9. 对“lifecycle 状态机 loop”的准确理解

### 9.1 它不是所有复杂系统的唯一答案

讨论中一个重要问题是：

> 既然 `workflow control -> runtime control`、`implicit branching -> explicit transitions` 看起来很强，是不是所有复杂系统都该这么做？

答案是否定的。

状态机 / lifecycle runtime 不是复杂系统的普适唯一答案。

它最适合解决的是：

- 长运行
- 有中间状态
- 有外部副作用
- 需要 pause / resume / cancel
- 失败恢复复杂
- 需要质量门
- 需要显式可观测性

### 9.2 其它常见架构模式

复杂系统还有很多其它主模式：

#### 1. Pipeline / 函数组合

适合短流程、纯变换、幂等性高的问题。

#### 2. DAG / Workflow Engine

适合依赖关系清晰、调度和并行重要的问题。

#### 3. Durable Workflow / Durable Execution

适合长运行、需要 checkpoint、定时器、恢复和幂等执行的问题。

#### 4. Actor Model

适合多实体并发互动，每个实体有独立状态和消息收发。

#### 5. Saga / Process Manager

适合分布式事务和跨服务副作用协调。

#### 6. Behavior Tree

适合反应式控制、优先级切换、局部回退。

#### 7. Planner-Executor / Agent Loop

适合开放任务、不确定路径、目标需要动态拆解的问题。

### 9.3 所以这个领域最优雅的不是单一模式

对 `yt-transcript` 这种问题域，最优雅的通常不是单一设计模式，而是组合：

> `Nominal Workflow + Hierarchical Adaptive Runtime + Policy-Gated Decisions + Typed Tools + Persisted Artifacts + Evaluator`

这里各自负责：

- Workflow：名义路径
- Runtime：执行期控制与恢复
- Policy：阈值、预算、合法动作
- Tools：可靠动作执行
- Artifacts：中间真相与血缘
- Evaluator：质量门

---

## 10. 推荐的六层架构模型

### 10.1 总体视图

建议把下一步升级后的系统视为六层：

1. `Workflow Layer`
2. `Adaptive Runtime Layer`
3. `Policy Layer`
4. `Tools Layer`
5. `Artifact & Event Layer`
6. `Evaluator Layer`

### 10.2 Workflow Layer

这层负责表达业务的名义阶段顺序，而不是处理所有异常。

推荐维持以下主骨架：

`preflight -> source -> normalize -> plan -> process -> verify -> assemble -> publish`

这层回答的问题是：

> 正常情况下，这个业务能力应该按什么阶段组织？

### 10.3 Adaptive Runtime Layer

这层是升级的核心。

它负责一个统一闭环：

`Observe -> Evaluate -> Decide -> Act -> Commit`

这层回答的问题是：

> 现在处于什么状态、观察到了什么、允许做什么、下一步该做哪一个动作？

### 10.4 Policy Layer

这层负责：

- 预算约束
- retry / replan 上限
- 哪些阶段允许降级
- 哪些错误必须 fail-fast
- 哪些情况必须请求人工升级

这层回答的问题是：

> 在当前状态下，哪些动作合法，哪些动作不合法？

### 10.5 Tools Layer

这层是确定性执行能力面。

建议继续保留现有：

- `scripts/download.sh`
- `scripts/preflight.sh`
- `scripts/cleanup.sh`
- `yt_transcript_utils.py`
- `kernel/long_text/*`

只是统一对它们做 typed action 包装。

### 10.6 Artifact & Event Layer

这层负责：

- 所有中间产物的身份、版本、血缘
- 所有运行事件和决策事件的 append-only 记录

这层回答的问题是：

> 当前 run 的真相是什么？它为什么会变成现在这样？

### 10.7 Evaluator Layer

这层负责独立质量判断，而不是让 processing 阶段自己说“我觉得差不多好了”。

它负责输出：

- coverage
- omission risk
- term consistency
- translation risk
- structure completeness
- accept / repair / replan / degrade / escalate

---

## 11. 推荐的数据模型

### 11.1 `TaskSpec`

这是外层 agent 传给 skill 的唯一任务 contract。

建议包含：

- `task_id`
- `url`
- `output_mode`
- `bilingual`
- `quality_profile`
- `speed_priority`
- `cost_budget`
- `latency_budget`
- `allowed_fallbacks`
- `human_escalation_policy`

### 11.2 `RunState`

记录这个 run 的顶层生命周期状态。

建议包含：

- `run_id`
- `task_id`
- `lifecycle_state`
- `active_stage`
- `effective_runtime_status`
- `policy_profile`
- `budget_ledger`
- `ownership`
- `started_at`
- `updated_at`

### 11.3 `Observation`

它只记录“观察到的事实”，不夹带决策。

例如：

- 字幕存在与否
- 字幕质量指标
- 视频时长
- verify 未通过项
- 连续超时次数
- 当前 token 消耗
- pause/cancel 请求

### 11.4 `DecisionRecord`

这是 runtime 的关键产物。

建议包含：

- `decision_id`
- `state_before`
- `observations_used`
- `allowed_actions`
- `selected_action`
- `policy_checks`
- `reason`
- `confidence`
- `decider_type`（rule / llm-assisted / human）

### 11.5 `ActionRequest` / `ActionResult`

动作是 runtime 对工具层发出的标准执行请求。

建议包含：

- `action_id`
- `action_type`
- `tool_name`
- `inputs`
- `artifacts_created`
- `warnings`
- `cost`
- `success`
- `failure_type`

### 11.6 `ArtifactRef`

每个产物都应该有稳定身份。

建议类型包括：

- `source_vtt`
- `audio_file`
- `raw_text`
- `normalized_document`
- `chunk_plan`
- `chunk_output`
- `merged_text`
- `quality_report`
- `final_markdown`

### 11.7 `QualityReport`

这应成为 evaluator 的标准输出。

建议包含：

- `coverage_score`
- `missing_sections`
- `term_consistency_score`
- `translation_risk`
- `structure_integrity`
- `recommended_action`

---

## 12. 推荐的状态机与控制循环

### 12.1 顶层生命周期状态

建议顶层状态为：

- `created`
- `preflighted`
- `sourcing`
- `normalized`
- `planned`
- `processing`
- `verifying`
- `assembling`
- `completed`
- `degraded`
- `paused`
- `failed_terminal`

### 12.2 控制类派生状态

建议显式保留：

- `pause_requested`
- `cancellation_requested`
- `human_escalation_requested`
- `repair_pending`
- `replan_pending`

### 12.3 `processing` 子状态机

因为长文本处理是当前系统最复杂的内部子系统，建议单独建子状态机，例如：

- `chunk_queue_ready`
- `chunk_running`
- `chunk_warning`
- `repair_pending`
- `replan_pending`
- `merge_pending`
- `processing_done`

### 12.4 统一 runtime 循环

推荐 runtime 主循环固定成：

1. `Observe`
2. `Evaluate`
3. `Derive Allowed Actions`
4. `Decide`
5. `Validate`
6. `Act`
7. `Commit`
8. `Transition`

其语义分别是：

- `Observe`：读取事实
- `Evaluate`：评估当前风险、质量、预算、失败模式
- `Derive Allowed Actions`：根据状态和 policy 生成合法动作集合
- `Decide`：在合法动作中选择下一步
- `Validate`：确认动作没有越权、超预算或破坏状态一致性
- `Act`：调用工具执行
- `Commit`：落盘 observation / decision / result / artifacts
- `Transition`：推进状态机

---

## 13. LLM 在 decision phase 的正确角色

### 13.1 正确理解

本轮讨论中有一个重要澄清：

> 升级的主收益，不是“让 LLM 来做决策”，而是先建立统一的 runtime lifecycle；LLM 只是可选地参与 decision phase。

也就是说：

- **第一收益**是显式 runtime
- **第二收益**才是让 LLM 在模糊场景做 action ranking

### 13.2 LLM 可以做什么

LLM 适合参与以下模糊判断：

- 字幕质量一般时，继续走字幕路径还是切 Deepgram
- verify 失败后，更适合 repair 还是 replan
- 预算紧张时，换模型还是降级输出

### 13.3 LLM 不应该做什么

LLM 不应该：

- 定义任务目标
- 发明新的动作类型
- 绕过 policy
- 篡改预算约束
- 直接替代 runtime transition logic

### 13.4 正确用法：从 allowed actions 中选一个

因此正确设计是：

- runtime 先根据状态与 policy 得到 `allowed_actions`
- LLM 只在这些选项中进行排序或选择
- 最终动作仍需经过 validate 与 commit

这就避免了 LLM 抢走 orchestration 权。

---

## 14. 为什么这个方向比“纯 agentic native 改造”更合适

### 14.1 任务空间并不开放

这个项目的大多数步骤是可预知且高度工程化的：

- 取 metadata
- 判断字幕
- 下载字幕或音频
- 转写
- 规范化
- chunk
- merge
- verify
- assemble

对于这种任务，完全交给 agent 自由编排通常不是最优。

### 14.2 真正复杂的不是“规划”，而是“执行控制”

项目真正困难的地方不是“想不出下一步大类是什么”，而是：

- 状态怎么持久化
- 中断后怎么恢复
- 某一段失败后怎么 repair/replan
- 质量没过时怎么处理
- 如何在预算内稳定完成

所以，下一步优先补的应该是 runtime，而不是全局 agent planning。

### 14.3 这也更符合它作为 skill 的位置

作为 skill，它最好的状态不是“在内部再造一个 agent”，而是：

> 对外表现为一个可靠、可观测、可恢复、可解释的执行能力面。

外部 agent 调它时，不需要知道内部所有分支，只需要知道：

- 给它一个 `TaskSpec`
- 它会返回 `RunState`
- 它能持续推进 run
- 它会在必要时给出结构化决策与质量报告

---

## 15. 推荐的最佳实践结论

综合本轮讨论，对这个问题领域最优雅的最佳实践可以总结为：

> `WorkflowGraph + Hierarchical Adaptive Runtime + Policy-Gated Decisions + Typed Tools + Event-Sourced Artifacts + Independent Evaluator`

这里每一部分都不可随意省略：

- 没有 `WorkflowGraph`，业务结构会失去清晰骨架
- 没有 `Adaptive Runtime`，执行期控制会继续散落
- 没有 `Policy`，动作选择会变得不可控
- 没有 `Typed Tools`，可恢复性和确定性都会下降
- 没有 `Artifacts/Event Log`，系统不可审计、不可回放
- 没有 `Evaluator`，质量门会再次滑回“模型自己说自己做得不错”

从系统设计的角度看，这是一种“混合式最优解”：

- 既不走纯 workflow 僵化路线
- 也不走纯 agentic 漂移路线
- 而是让结构性与自适应性在不同层级承担各自职责

---

## 16. 对现有项目的具体升级建议

### 16.1 保留不动的部分

以下部分应继续作为系统稳定内核：

- 脚本优先与 JSON 输出
- 本地状态、manifest、telemetry、control files
- 长文本能力层
- verify / assemble 等确定性步骤

### 16.2 需要系统化提升的部分

下一步应该重点提升：

#### 1. 显式 runtime contract

引入统一的：

- `TaskSpec`
- `RunState`
- `DecisionRecord`
- `ActionResult`
- `ArtifactRef`
- `QualityReport`

#### 2. 显式 lifecycle 管理器

把现在散落的控制行为，统一收拢为 `Observe -> Evaluate -> Decide -> Act -> Commit`。

#### 3. policy engine

把阈值、上限、动作可用性、降级条件，从分散判断提升为显式规则层。

#### 4. action model

把当前零散的 retry / repair / replan / fallback 统一抽象成动作。

#### 5. decision log

把为什么选这个动作显式记录下来，供 resume、debug、外层 agent 解释使用。

### 16.3 不建议做的事情

不建议的方向包括：

- skill 内再放一个完整 planner agent
- 让 LLM 自由生成下一步动作
- 把原本 deterministic 的工具执行变成 prompt 内自然语言调度
- 让 verify 退化为“模型自评质量”

---

## 17. 一个简洁的最终判断

可以把本轮讨论沉淀成一句高度概括的话：

> `yt-transcript` 当前是一个以确定性流程编排为主、以 LLM 为局部处理器的 workflow-native transcript production system；它的下一步最合理演化，不是改造成内嵌 agent，而是升级为拥有显式生命周期、受限动作模型、策略约束、结构化决策记录的 bounded adaptive runtime。

如果继续压缩成更短的一句话：

> 不是再造一个 agent，而是把现有薄控制层提升为清晰的 runtime。

---

## 18. 后续可继续展开的主题

这份研究笔记之后，仍然可以继续深化几个方向：

1. 把六层架构展开成 `SYSTEM_DESIGN_VNEXT.md`
2. 把状态机具体化成 `state / observation / allowed actions / transition rules` 表
3. 设计 `TaskSpec`、`DecisionRecord`、`QualityReport` 的 JSON schema
4. 设计 runtime event log 和 artifact graph 的持久化格式
5. 规划从当前代码到 vNext runtime 的分阶段迁移路线

这些内容将把当前的“设计判断”进一步推进到“可实施设计规格”。

---

<a id="en"></a>

# English Version

## 1. Note Positioning

This note is neither a line-by-line commentary on the current codebase nor an immediate refactoring task list.

Its purpose is to distill this session's discussion into a structured research note about the current architectural shape of `yt-transcript`, its potential evolution path, its relationship to agent architectures, and the most suitable next-step design direction.

This note focuses on five questions:

1. What architectural paradigm does this project currently belong to?
2. Why is it not a typical `agentic native` architecture?
3. If stronger adaptivity is needed, how can it be introduced without duplicating the role of the outer agent?
4. Why is the next sensible step to upgrade the current thin control layer into a clearer `adaptive runtime`?
5. In this problem domain, what combination of design patterns is the most elegant?

---

## 2. Architectural Judgment of the Current Project

### 2.1 Core Judgment

The dominant architecture of `yt-transcript` is not an `agentic native` architecture in which an LLM agent owns orchestration and the harness merely defines boundaries.

A more accurate description is:

- `workflow-native`
- `script-first`
- `local-first`
- `stateful`
- `quality-gated`

In other words, its essence is:

> Deterministic code and scripts own orchestration, while the LLM is used as a text-processing mechanism at selected nodes.

### 2.2 Evidence

The project documents and implementation point to several clear traits:

- High-risk branching is placed in scripts and structured JSON outputs rather than hidden inside prompt interpretation.
- State, manifest, telemetry, and control signals are explicitly persisted.
- Pause / resume / cancel / ownership / retry / replan are controlled by runtime code.
- The LLM is inserted into text transformation and long-text processing stages, not used as the global orchestrator.
- Final stop/go decisions depend on validation and quality gates rather than model confidence.

So the system philosophy is not “let the LLM autonomously complete the task”, but rather:

> Turn the full URL-to-Markdown production chain into a reliable, recoverable, inspectable, and verifiable local system.

### 2.3 A Compact Summary of the Current Architecture

The current project can be summarized as:

`workflow-native process + local runtime controls + LLM-powered transformation nodes`

The key phrases are:

- `process`: the main flow is explicitly defined
- `runtime controls`: the project already has a thin control layer such as pause/cancel/replan/ownership
- `LLM-powered nodes`: the LLM is a node processor, not the system’s control plane

---

## 3. Why It Is Not Agentic Native

### 3.1 Typical Traits of Agentic Native Systems

A typical `agentic native` system usually has the following properties:

- orchestration is primarily owned by an agent runtime rather than a fixed workflow
- the harness mainly defines permissions, budgets, tool boundaries, and output contracts
- the agent performs task decomposition, path selection, and dynamic replanning itself
- the system’s main abstractions are goal / tools / observations / actions / reflections
- the workflow is generated at runtime by the agent rather than pre-encoded as a fixed business skeleton

### 3.2 How the Current Project Differs

This project is not “first define an agent, then let the agent choose tools.”

It is closer to “first define the process, then inject the LLM into a small number of processing points.”

Concretely:

- the broad process skeleton is predefined rather than generated on the fly
- routing and state transitions are mainly decided by code rather than the model
- replanning exists, but it is bounded, local, and constrained rather than open-ended agent strategy generation
- the cross-stage business structure is pre-specified in documents and scripts rather than dynamically synthesized by an agent runtime

### 3.3 This Is Intentional, Not a Defect

This does not mean the current system is inferior.

On the contrary, this is a reasonable choice for the present problem domain:

- the task skeleton is highly stable
- the input is uncertain, but the major solution path categories are enumerable
- the output is a deliverable artifact rather than an open-ended conversation
- recoverability and auditability matter more than unrestricted autonomy

So the current system is not “insufficiently agentic”; it is deliberately designed around:

> `reliability-first orchestration`

---

## 4. Strengths and Boundaries of the Current Approach

### 4.1 Strengths

For this project, the existing `workflow-native process` has clear advantages.

#### 1. Stable Main Path

This is not an open task space. Its nominal path is broadly stable:

`preflight -> source -> normalize -> plan -> process -> verify -> assemble`

When the main path is known in advance, using a workflow skeleton for orchestration is usually more stable than asking an agent to replan the full path every time.

#### 2. Strong Recoverability

Long-video processing naturally requires:

- resumability
- preservation of intermediate artifacts
- chunk-level status
- pause / resume / cancel
- repair / replan after failures

These needs are much closer to a runtime system than to an open-ended conversational agent.

#### 3. Better Cost and Predictability

If global orchestration is handed to an agent:

- additional planning tokens are introduced
- additional reflection / review tokens are introduced
- decision-chain uncertainty increases
- failure diagnosis becomes harder

The current architecture is usually better in cost, latency, and debuggability.

#### 4. Better as a Skill Invoked by an Outer Agent

This project is itself a skill meant to be called by an outer agent.

If a full agent is embedded inside the skill, control-plane duplication appears immediately:

- the outer agent performs high-level task orchestration
- the inner agent performs another layer of high-level task orchestration

That leads to role conflicts, state fragmentation, and debugging difficulty.

### 4.2 Boundaries

Even though the current design is reasonable, it still has limitations.

#### 1. Execution-Time Adaptivity Is Still Scattered

The project already has control semantics such as pause/cancel/auto-replan/ownership, but they have not yet been unified into a full runtime model.

#### 2. Conditional Logic Is Still Embedded in Process Implementations

Many control behaviors still appear as local if/else blocks, helper functions, or special repair/replan branches rather than being driven by a unified lifecycle loop.

#### 3. Ambiguous Decisions Still Lack a Clear Home

For example:

- whether to switch to Deepgram when subtitles are available but low quality
- whether certain chunk failures are better handled by repair or replan
- whether to switch models or degrade output under budget pressure

These decisions are not yet part of an explicit runtime decision system.

---

## 5. Why It Is Not Appropriate to Add Another Full Agent Layer

### 5.1 It Looks Attractive but Quickly Becomes Redundant

Because the project is a skill, one tempting thought is:

> Since the outer layer is already an agent, wouldn’t an agentic control layer inside the skill make the system smarter?

But if that control layer is implemented as a second full agent, problems appear immediately:

- both layers do planning
- both layers do replanning
- both layers do retry / review / reflection
- both layers reinterpret the task goal

The result is not a cleaner design, but “an agent inside another agent.”

### 5.2 Typical Problems of Duplicated Control Planes

#### 1. Role Conflicts

The outer agent may prioritize speed, while the inner agent prioritizes quality.

#### 2. State Fragmentation

The outer layer keeps conversational context while the inner layer keeps another decision history, making the true source of system state ambiguous.

#### 3. Harder Debugging

When a run fails, it becomes hard to tell whether:

- the tool failed
- the runtime failed
- or the inner agent picked the wrong action

#### 4. Higher Cost

If a long-running task introduces another planner/reviewer/self-reflection loop internally, both token usage and latency increase significantly.

### 5.3 The Correct Upgrade Direction: Not a Second Brain, but Local Autopilot

Therefore, the correct next step is not to place another full agent inside the skill, but to upgrade the current thin control layer into:

> `bounded adaptive runtime`

Its basic principles are:

- the outer agent remains the only “main agent”
- the inside of the skill only handles adaptive control within the local execution domain
- the inside of the skill does not reinterpret user intent
- the inside of the skill does not orchestrate other skills
- the inside of the skill only decides the next execution action within a known task contract

---

## 6. The Essence of the Next Upgrade: From Workflow Control to Runtime Control

The key insight from this discussion can be compressed into four shifts:

- `workflow control -> runtime control`
- `path-oriented -> state-oriented`
- `implicit branching -> explicit transitions`
- `special-case recovery -> first-class recovery semantics`

### 6.1 What These Four Shifts Actually Mean

#### 1. `workflow control -> runtime control`

Today, the project mostly decides “what to do next” inside process code.

After the upgrade, state evolution should be driven by a unified runtime.

This means the real center of control is no longer “which branch of the code is currently executing”, but:

- what lifecycle state the run is in
- what has been observed
- what actions are currently allowed
- which legal action should be selected next

#### 2. `path-oriented -> state-oriented`

The current style is more focused on “which path has been taken.”

The upgraded style focuses on:

- what state the task is in now
- what actions are legal in that state
- what next state each action leads to

#### 3. `implicit branching -> explicit transitions`

Existing behavior is often hidden inside implementation-level conditionals.

After the upgrade, branching does not disappear, but is elevated into explicit objects:

- transition guards
- allowed actions
- policy checks
- failure-handling rules

#### 4. `special-case recovery -> first-class recovery semantics`

Today some recovery behaviors feel like ad hoc special cases:

- repair after a specific function fails
- temporary replanning after a path breaks
- special retries after chunk timeout

After the upgrade, these should become first-class runtime actions:

- `retry_action`
- `repair_chunk`
- `replan_remaining`
- `fallback_to_deepgram`
- `degrade_output_mode`
- `request_human_escalation`

### 6.2 This Is Not Just “Replace if/else with a State Machine”

More precisely, the essence of the upgrade is:

> Move from a control structure centered on imperative process branching to a runtime centered on lifecycle state, observed events, allowed actions, and explicit state transitions.

Conditional logic does not disappear; it is lifted from scattered implementation details into a coherent control model.

---

## 7. The Most Suitable Target Form: Bounded Adaptive Runtime

### 7.1 Definition

The most suitable next-stage architecture for this skill kernel is:

> `workflow-native kernel + bounded adaptive runtime + external agent orchestration`

Broken down:

- `workflow-native kernel`: keep the current stage-oriented business skeleton
- `bounded adaptive runtime`: introduce limited adaptive execution control
- `external agent orchestration`: continue to let the outer agent own high-level goals and cross-tool orchestration

### 7.2 Why “Bounded” Matters

Adaptivity here must not mean open-ended free planning.

The runtime must not:

- redefine the task goal
- decide to invoke unrelated skills on its own
- invent arbitrary new action types
- bypass budget or policy boundaries

The runtime may only:

- read current state and observations
- choose from an allowed action set
- retry / repair / replan / fallback / degrade within the local execution domain
- persist all decisions and results structurally

### 7.3 What This Runtime Is Supposed to Solve

The bounded adaptive runtime primarily addresses the following problems:

- control logic is too scattered today
- recovery semantics are not yet unified enough
- ambiguous decisions do not have a clear runtime home
- the outer agent lacks a stable, explainable skill-runtime contract

---

## 8. The Real Benefits of This Upgrade

### 8.1 Unified Lifecycle Control

All execution-time control behaviors are driven by one loop rather than scattered across stage implementations.

This means:

- pause / cancel / resume become unified runtime semantics
- retry / repair / replan / fallback become unified runtime actions
- checkpoint / recover / finalize all gain a clear home

### 8.2 Stronger Recoverability

Once run state, action history, observations, and artifact relationships all become first-class objects, resume and debugging no longer depend on guessing where the system may have stopped.

### 8.3 Clearer Boundary with the Outer Agent

The responsibilities of the outer agent and the inner skill become cleaner:

- the outer agent decides **what to do**
- the skill runtime decides **how to complete it reliably**

### 8.4 Easier Extension of Complex Strategies

If the project later adds:

- model routing
- cost-first / quality-first / speed-first policy profiles
- different optimization strategies for different kinds of videos
- terminology consistency across batches or series

A unified runtime and policy engine provide a much cleaner place to put those rules than adding more branches to the workflow.

### 8.5 Better Explainability

If the runtime records explicit `DecisionRecord`s, the outer agent and operators can answer questions such as:

- Why did the system switch to Deepgram?
- Why was replanning triggered at that point?
- Why was model switching chosen instead of degradation?

### 8.6 LLM Can Assist Decision-Making Without Owning Orchestration

A subtle but important benefit of this upgrade is:

> The LLM can be used inside the decision phase for bounded assistance, without turning it into the global orchestrator.

---

## 9. A Precise Understanding of the Lifecycle State-Machine Loop

### 9.1 It Is Not the Universal Answer for All Complex Systems

A key question in this discussion was:

> If ideas like `workflow control -> runtime control` and `implicit branching -> explicit transitions` are so powerful, should every complex system use a lifecycle state-machine loop?

The answer is no.

A state machine or lifecycle runtime is not the only correct answer for all complex systems.

It is most suitable when the hard part of the system is:

- long-running execution
- meaningful intermediate state
- external side effects
- pause / resume / cancel
- difficult failure recovery
- quality gates
- explicit observability requirements

### 9.2 Other Common Architectural Patterns

There are many other dominant patterns for complex systems.

#### 1. Pipeline / Functional Composition

Best for short flows, pure transformation, and highly idempotent processing.

#### 2. DAG / Workflow Engine

Best when dependency graphs, scheduling, and parallelism are central concerns.

#### 3. Durable Workflow / Durable Execution

Best for long-running flows that need checkpoints, timers, retries, and replayable execution.

#### 4. Actor Model

Best when many independent entities each hold their own state and communicate via messages.

#### 5. Saga / Process Manager

Best for distributed side-effect coordination across services.

#### 6. Behavior Tree

Best for reactive control, priority switching, and local fallback behavior.

#### 7. Planner-Executor / Agent Loop

Best for open-ended tasks where the path cannot be fixed in advance and goals need dynamic decomposition.

### 9.3 So the Most Elegant Solution in This Domain Is Not a Single Pattern

For a problem like `yt-transcript`, the most elegant architecture is usually not a single pattern, but a combination:

> `Nominal Workflow + Hierarchical Adaptive Runtime + Policy-Gated Decisions + Typed Tools + Persisted Artifacts + Evaluator`

Each part answers a different question:

- Workflow: what is the nominal path?
- Runtime: what is happening now, and how should execution be controlled?
- Policy: what is legal and what is not?
- Tools: how are actions executed reliably?
- Artifacts: what is the persisted truth of the run?
- Evaluator: can the output pass quality gates?

---

## 10. Recommended Six-Layer Architecture Model

### 10.1 Overall View

The next-stage architecture can be understood as six layers:

1. `Workflow Layer`
2. `Adaptive Runtime Layer`
3. `Policy Layer`
4. `Tools Layer`
5. `Artifact & Event Layer`
6. `Evaluator Layer`

### 10.2 Workflow Layer

This layer expresses the nominal business stage order rather than handling every exception.

The recommended skeleton remains:

`preflight -> source -> normalize -> plan -> process -> verify -> assemble -> publish`

This layer answers:

> Under normal conditions, how should this capability be organized as stages?

### 10.3 Adaptive Runtime Layer

This is the core of the upgrade.

It drives a unified loop:

`Observe -> Evaluate -> Decide -> Act -> Commit`

This layer answers:

> What state are we in now, what have we observed, what actions are allowed, and which action should be taken next?

### 10.4 Policy Layer

This layer manages:

- budget constraints
- retry / replan limits
- when degradation is allowed
- when certain failures must fail fast
- when human escalation is required

This layer answers:

> Given the current state, which actions are legal and which are blocked?

### 10.5 Tools Layer

This is the deterministic capability plane.

It should continue to preserve the existing tools:

- `scripts/download.sh`
- `scripts/preflight.sh`
- `scripts/cleanup.sh`
- `yt_transcript_utils.py`
- `kernel/long_text/*`

But they should be wrapped as typed actions.

### 10.6 Artifact & Event Layer

This layer manages:

- identity, versioning, and lineage of all intermediate artifacts
- append-only logs for runtime events and decision events

This layer answers:

> What is the truth of the current run, and why did it become what it is now?

### 10.7 Evaluator Layer

This layer performs quality judgment independently rather than letting the processing stage effectively self-certify its own output.

It should produce:

- coverage
- omission risk
- terminology consistency
- translation risk
- structural completeness
- accept / repair / replan / degrade / escalate recommendations

---

## 11. Recommended Data Model

### 11.1 `TaskSpec`

This is the only task contract passed from the outer agent into the skill.

Suggested fields:

- `task_id`
- `url`
- `output_mode`
- `bilingual`
- `quality_profile`
- `speed_priority`
- `cost_budget`
- `latency_budget`
- `allowed_fallbacks`
- `human_escalation_policy`

### 11.2 `RunState`

This records the top-level lifecycle state of the run.

Suggested fields:

- `run_id`
- `task_id`
- `lifecycle_state`
- `active_stage`
- `effective_runtime_status`
- `policy_profile`
- `budget_ledger`
- `ownership`
- `started_at`
- `updated_at`

### 11.3 `Observation`

This records observed facts only and should not contain decisions.

Examples:

- whether subtitles exist
- subtitle quality indicators
- video duration
- verify failures
- consecutive timeout count
- current token cost
- pause/cancel requests

### 11.4 `DecisionRecord`

This is one of the most important runtime artifacts.

Suggested fields:

- `decision_id`
- `state_before`
- `observations_used`
- `allowed_actions`
- `selected_action`
- `policy_checks`
- `reason`
- `confidence`
- `decider_type` (`rule`, `llm-assisted`, `human`)

### 11.5 `ActionRequest` / `ActionResult`

Actions are standardized execution requests issued by the runtime to the tool layer.

Suggested fields:

- `action_id`
- `action_type`
- `tool_name`
- `inputs`
- `artifacts_created`
- `warnings`
- `cost`
- `success`
- `failure_type`

### 11.6 `ArtifactRef`

Each artifact should have a stable identity.

Suggested artifact types include:

- `source_vtt`
- `audio_file`
- `raw_text`
- `normalized_document`
- `chunk_plan`
- `chunk_output`
- `merged_text`
- `quality_report`
- `final_markdown`

### 11.7 `QualityReport`

This should become the evaluator’s standard output.

Suggested fields:

- `coverage_score`
- `missing_sections`
- `term_consistency_score`
- `translation_risk`
- `structure_integrity`
- `recommended_action`

---

## 12. Recommended State Machine and Control Loop

### 12.1 Top-Level Lifecycle States

Suggested top-level states:

- `created`
- `preflighted`
- `sourcing`
- `normalized`
- `planned`
- `processing`
- `verifying`
- `assembling`
- `completed`
- `degraded`
- `paused`
- `failed_terminal`

### 12.2 Derived Control States

Suggested explicit control-related states:

- `pause_requested`
- `cancellation_requested`
- `human_escalation_requested`
- `repair_pending`
- `replan_pending`

### 12.3 `processing` Sub-State Machine

Because long-text processing is the most complex internal subsystem, it should have its own sub-state machine, for example:

- `chunk_queue_ready`
- `chunk_running`
- `chunk_warning`
- `repair_pending`
- `replan_pending`
- `merge_pending`
- `processing_done`

### 12.4 Unified Runtime Loop

The runtime main loop should be standardized as:

1. `Observe`
2. `Evaluate`
3. `Derive Allowed Actions`
4. `Decide`
5. `Validate`
6. `Act`
7. `Commit`
8. `Transition`

The semantics are:

- `Observe`: read facts
- `Evaluate`: assess risk, quality, budget, and failure patterns
- `Derive Allowed Actions`: produce the legal action set based on state and policy
- `Decide`: select the next action from the legal set
- `Validate`: ensure the action does not violate budget, policy, or state consistency
- `Act`: call tools to execute
- `Commit`: persist observations, decisions, results, and artifacts
- `Transition`: advance the state machine

---

## 13. The Correct Role of the LLM in the Decision Phase

### 13.1 The Most Important Clarification

One major clarification from this discussion is:

> The main benefit of the upgrade is not “letting the LLM make decisions.” The primary benefit is establishing an explicit runtime lifecycle; the LLM is only an optional participant in the decision phase.

So the priority order is:

- **first**: explicit runtime
- **second**: optional LLM-based action ranking in ambiguous cases

### 13.2 What the LLM May Do

The LLM is suitable for ambiguous judgments such as:

- whether to continue a subtitle path or switch to Deepgram when subtitle quality is mediocre
- whether a verify failure is better handled by repair or replan
- whether to switch models or degrade output under budget pressure

### 13.3 What the LLM Must Not Do

The LLM must not:

- redefine the task goal
- invent new action types
- bypass policy
- modify budget constraints
- replace runtime transition logic directly

### 13.4 The Correct Usage Pattern

The right design is:

- the runtime first derives `allowed_actions` based on state and policy
- the LLM only ranks or selects among those options
- the final action still goes through validation and commit

This prevents the LLM from taking over orchestration.

---

## 14. Why This Direction Is Better Than a Full Agentic-Native Rewrite

### 14.1 The Task Space Is Not Open-Ended

Most steps in this project are predictable and engineering-heavy:

- fetch metadata
- inspect subtitles
- download subtitles or audio
- transcribe
- normalize
- chunk
- merge
- verify
- assemble

For this kind of task, giving full orchestration to an agent is usually not optimal.

### 14.2 The Real Difficulty Is Not Planning, but Execution Control

The hardest part of the project is not “what general class of next step exists”, but:

- how state is persisted
- how interrupted work is resumed
- how failures are repaired or replanned
- what to do when quality gates fail
- how to finish reliably under budget

So the next thing to strengthen should be the runtime, not global agent planning.

### 14.3 This Better Matches Its Role as a Skill

As a skill, its best form is not “a second embedded agent”, but:

> a reliable, observable, recoverable, and explainable execution surface.

When the outer agent calls it, the outer agent should not need to understand all internal branches. It should only need to know:

- pass in a `TaskSpec`
- receive a `RunState`
- continue advancing the run
- inspect structured decisions and quality reports when needed

---

## 15. Recommended Best-Practice Conclusion

Across this discussion, the most elegant best-practice formulation for this problem domain is:

> `WorkflowGraph + Hierarchical Adaptive Runtime + Policy-Gated Decisions + Typed Tools + Event-Sourced Artifacts + Independent Evaluator`

Each component matters:

- without `WorkflowGraph`, the business skeleton loses clarity
- without `Adaptive Runtime`, execution-time control remains scattered
- without `Policy`, action selection becomes hard to constrain
- without `Typed Tools`, recoverability and determinism weaken
- without `Artifacts/Event Log`, the system cannot be audited or replayed
- without `Evaluator`, quality gates slide back toward “the model thinks it looks fine”

From a system-design perspective, this is a hybrid optimum:

- not a rigid pure-workflow system
- not a drifting pure-agent system
- but a layered architecture where structure and adaptivity each own the right level of responsibility

---

## 16. Concrete Upgrade Recommendations for This Project

### 16.1 What Should Stay

The following parts should remain as the stable kernel:

- script-first workflows and JSON outputs
- local state, manifests, telemetry, and control files
- the long-text capability layer
- deterministic verify / assemble stages

### 16.2 What Should Be Elevated Systematically

The next step should focus on the following upgrades.

#### 1. Explicit Runtime Contracts

Introduce unified contracts for:

- `TaskSpec`
- `RunState`
- `DecisionRecord`
- `ActionResult`
- `ArtifactRef`
- `QualityReport`

#### 2. Explicit Lifecycle Manager

Gather the currently scattered control behaviors into a unified `Observe -> Evaluate -> Decide -> Act -> Commit` loop.

#### 3. Policy Engine

Lift thresholds, limits, action availability, and degradation conditions into an explicit rule layer.

#### 4. Action Model

Turn today’s scattered retry / repair / replan / fallback behavior into a standardized action model.

#### 5. Decision Log

Explicitly record why a specific action was chosen so that resume, debugging, and outer-agent explanation become much easier.

### 16.3 What Should Not Be Done

The following directions are not recommended:

- embedding a full planner agent inside the skill
- letting the LLM invent arbitrary next actions
- replacing deterministic execution with free-form prompt choreography
- degrading verification into model self-assessment

---

## 17. One Concise Final Judgment

The entire discussion can be compressed into one high-level conclusion:

> `yt-transcript` is currently a workflow-native transcript production system in which deterministic process orchestration is primary and LLMs act as local processors; its most appropriate next evolution is not an embedded agent, but a bounded adaptive runtime with explicit lifecycle state, constrained action selection, policy checks, and structured decision logging.

An even shorter version is:

> Do not build a second agent; upgrade the current thin control layer into a clearer runtime.

---

## 18. Topics Worth Expanding Next

This research note can be extended in several concrete directions:

1. expand the six-layer model into a full `SYSTEM_DESIGN_VNEXT.md`
2. turn the state machine into a concrete `state / observation / allowed actions / transition rules` table
3. define JSON schemas for `TaskSpec`, `DecisionRecord`, and `QualityReport`
4. design persisted formats for the runtime event log and artifact graph
5. create a phased migration path from the current codebase to the vNext runtime

Those would move this discussion from architectural judgment to an implementation-oriented design spec.
