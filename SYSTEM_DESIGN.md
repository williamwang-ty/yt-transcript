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

#### Alternative considered: direct YouTube InnerTube subtitle fetching

Direct InnerTube subtitle fetching was evaluated as an alternative acquisition strategy to `yt-dlp`. It is lighter for subtitle-only flows and can reduce setup cost for simple local runs. The architecture does not adopt it as the default acquisition layer because it remains an unofficial, interface-sensitive surface, does not remove the no-subtitle fallback requirement, and would shift more extraction-maintenance burden into this project.

So the design choice is:

- `yt-dlp` remains the primary subtitle-acquisition dependency
- Deepgram remains the fallback when usable subtitles do not exist
- InnerTube is acknowledged as a possible future fast path, not the canonical default

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

### 6.8 Runtime Design: Bounded Adaptive Control

#### Why the runtime exists

The project already has a clear business workflow, but workflow structure alone is not enough once execution becomes long-running, interruptible, and recoverable.

A purely path-oriented implementation tends to accumulate:

- implicit branching hidden in `if/else` conditions
- recovery logic expressed as one-off exceptions
- pause / resume / cancel semantics scattered across commands
- difficulty explaining what the system believes, what it can do next, and why

The runtime exists to solve that problem.

It is the layer that turns execution from “a chain of commands with conditions” into “a bounded job with explicit state, allowed actions, and durable control semantics.”

#### What the runtime is — and is not

In this project, the runtime is:

- a **bounded adaptive control layer** between workflow routing and tool execution
- **state-oriented** rather than path-oriented
- **rule-first**, with LLM participation limited to constrained decision support
- **local-first**, with important truth persisted on disk
- **operable**, meaning a run can be inspected, paused, resumed, cancelled, and explained

It is not:

- a second free-form planner agent embedded inside the skill
- a replacement for the business workflow itself
- an excuse to let the model invent arbitrary next actions
- a generic multi-agent platform

So the runtime should be read as **adaptive control**, not as **autonomous agency**.

#### Runtime responsibilities

The runtime owns six core responsibilities:

1. **Normalize runtime contracts**
   - represent task intent, run state, observations, decisions, actions, quality signals, and artifacts in explicit schemas

2. **Drive lifecycle state explicitly**
   - make lifecycle state and stage transitions visible instead of leaving them implicit in branch structure

3. **Derive allowed actions**
   - decide what is legal at a given moment based on policy, budget pressure, recovery signals, and quality outcomes

4. **Select and dispatch bounded actions**
   - choose from an allowed set, invoke the compatible implementation, and record why that action was selected

5. **Persist operational truth**
   - keep state, control signals, ownership markers, and telemetry on disk so runs remain inspectable and resumable

6. **Expose a stable outer contract**
   - give external callers a small runtime-facing API instead of forcing them to understand internal helper commands

#### The runtime lifecycle loop

The runtime control model is best understood as this loop:

`Observe -> Evaluate -> Derive Allowed Actions -> Decide -> Validate -> Act -> Commit -> Transition`

Each step has a distinct purpose:

- **Observe**
  - read persisted state such as manifests, control files, ownership markers, and prior results
- **Evaluate**
  - summarize execution health, budget pressure, processing substate, and quality signals
- **Derive Allowed Actions**
  - produce an explicit set of legal next actions under current policy
- **Decide**
  - select one action from the allowed set; rules lead, and model-assisted ranking is optional and constrained
- **Validate**
  - confirm preconditions before mutation or external calls
- **Act**
  - execute the chosen bounded command
- **Commit**
  - persist resulting state, control changes, artifacts, and telemetry
- **Transition**
  - emit the new lifecycle state and transition summary

This is the conceptual upgrade from workflow control to runtime control:

- workflow still defines the nominal business path
- runtime defines the current operational state and legal next moves

#### Runtime vocabulary by layer

To keep the runtime readable, it helps to treat its vocabulary as four layers rather than one flat list.

##### Layer 1: identity and anchoring

These terms answer: *what run is this, and where does its durable truth live?*

- **`task_id`**
  - the stable identity of the task intent
- **`run_id`**
  - the stable identity of the current runtime instance exposed to outer callers
- **`work_dir`**
  - the local root directory that anchors persisted runtime truth for this run
- **`TaskSpec`**
  - the normalized contract for task intent from the outer caller’s perspective
- **`RunState`**
  - the normalized contract for the run’s current lifecycle state, stage, ownership, and effective runtime status

This layer establishes *which job we are talking about* before any control decision is made.

##### Layer 2: execution truth and control surfaces

These terms answer: *what is operationally true right now, and who is allowed to change it?*

- **`.runtime_task.json`**
  - the stable integration-facing task record used by the runtime API
- **`manifest.json`**
  - the domain execution truth for long-text plans, chunks, and runtime status
- **`ownership` / `.runtime_owner.json`**
  - the exclusive mutation lease that prevents concurrent writers from corrupting local state
- **`.runtime_pause.json`** and **`.runtime_cancel.json`**
  - out-of-band operator control signals that request pause or cancellation without rewriting the entire manifest
- **`LifecycleTransition`**
  - the explicit summary of how a command moved the run from one lifecycle state to another

The design intent is:

- `manifest.json` and control files express operational execution truth
- `.runtime_task.json` expresses stable integration-facing identity and intent
- ownership expresses mutation authority rather than business meaning

##### Layer 3: decision and adaptation

These terms answer: *given the current truth, what can the runtime legally do next, and why?*

- **`Observation`**
  - normalized facts collected from command results and persisted runtime state
- **`PolicyEvaluation`**
  - the policy output that derives pressure signals and the current `allowed_actions`
- **`allowed_actions`**
  - the explicit set of legal next runtime moves under current policy
- **`DecisionRecord`**
  - the structured record of which action was selected, with rationale and policy checks
- **`ActionResult`**
  - the normalized report of what bounded command ran and what happened
- **`ProcessingState`**
  - the fine-grained posture of ongoing processing work
- **`RecoverySummary`**
  - the resumability and recommended recovery action for the current run

This layer is where the runtime becomes adaptive, but still bounded.
It can react, but it must react through explicit policy and explicit action sets.

##### Layer 4: outputs and observability

These terms answer: *what did the run produce, how good is it, and how can operators inspect history?*

- **`ArtifactRef` / `ArtifactGraph`**
  - structured references and lineage for run outputs
- **`QualityReport` / `EvaluatorReport`**
  - structured quality and recommendation signals, separate from nominal processing
- **`telemetry.jsonl`**
  - the append-only operational journal for command history and runtime inspection
- **command envelope**
  - the stable result wrapper that returns `result`, `contracts`, and telemetry metadata together

This layer makes the runtime inspectable without confusing historical events with current state.

##### Why this layered vocabulary matters

The runtime should be explainable in three directions at once:

- to the outer caller: *what run is this?*
- to the controller: *what is true and what is allowed next?*
- to the operator: *what happened and what artifacts or warnings exist?*

That is why the vocabulary is intentionally split across identity, execution truth, decision, and observability.

#### Small runtime information flow

The runtime can be pictured as this compact information flow:

```text
Outer Agent / CLI
        |
        | create-run
        v
  .runtime_task.json
  (TaskSpec + run_id)
        |
        | inspect-run / advance-run / apply-control / resume-run / finalize-run
        v
+--------------------------- work_dir ----------------------------+
| manifest.json          -> execution truth                       |
| .runtime_owner.json    -> mutation authority                    |
| .runtime_pause.json    -> pause request                         |
| .runtime_cancel.json   -> cancel request                        |
| telemetry.jsonl        -> append-only history                   |
+----------------------------------------------------------------+
        |
        v
Observe / Summarize
(TaskSpec + RunState + Observation
 + ProcessingState + RecoverySummary)
        |
        v
PolicyEvaluation
(derive allowed_actions)
        |
        v
DecisionRecord
(select one bounded action)
        |
        v
Act
(process / replan / pause / resume / cancel / finalize)
        |
        v
Commit
(update manifest / control files / artifacts / telemetry)
        |
        v
ActionResult + LifecycleTransition
        |
        v
Stable envelope / inspect-run / finalize-run result
```

Not every command traverses the entire loop with the same depth.
For example, `create-run` mainly initializes identity and stable contracts, while `advance-run` is the entrypoint that most fully exercises the bounded control loop.

#### Decision model: rule-first, LLM-optional, never unconstrained

A key runtime design principle is that the model does not own orchestration.

Instead:

- workflow code still defines the nominal business skeleton
- policy derives the allowed action set
- the decision layer must choose from that allowed set
- an optional LLM ranker may help rank or select **within** the allowed set only
- recovery actions such as `pause`, `resume`, `cancel`, `prepare_resume`, and `replan_remaining` are first-class runtime semantics rather than one-off exceptions

This keeps adaptation without giving up control boundaries.

#### Stable external runtime API

For external callers, the preferred runtime surface is:

- `create-run`
- `inspect-run`
- `advance-run`
- `apply-control`
- `resume-run`
- `finalize-run`

This API does three things:

- hides internal helper-command detail behind a stable contract
- lets an outer agent treat the skill as a bounded runtime capability
- preserves backward compatibility because older commands still exist as implementation-level helpers

So the migration direction is:

- **preferred path**: runtime-facing API
- **compatibility path**: legacy helper commands and CLI shims

#### Implementation anchors

The runtime design maps onto the current repository like this:

- `kernel/task_runtime/contracts.py`
  - runtime object schemas and command contract bundles
- `kernel/task_runtime/lifecycle.py`
  - lifecycle shell and transition summaries
- `kernel/task_runtime/policy.py`
  - allowed-action derivation and budget-pressure evaluation
- `kernel/task_runtime/decision.py`
  - rule-first action selection with optional constrained model assistance
- `kernel/task_runtime/ledger.py`
  - budget and action-accounting summaries
- `kernel/task_runtime/recovery.py`
  - processing substates and recovery recommendations
- `kernel/task_runtime/artifacts.py`
  - artifact references and artifact-graph summaries
- `kernel/task_runtime/evaluator.py`
  - structured quality-driven recommendation signals
- `kernel/task_runtime/api.py`
  - stable outer-agent runtime API for `create-run / inspect-run / advance-run / apply-control / resume-run / finalize-run`
- `kernel/task_runtime/runtime.py`
  - command envelopes, telemetry append, and ownership helpers
- `kernel/task_runtime/state.py`
  - persisted manifest, runtime state, and control-file helpers
- `kernel/task_runtime/controller.py`
  - owned mutations and bounded control-loop wrappers
- `kernel/task_runtime/telemetry.py`
  - operator-facing telemetry query and summary surfaces

### 6.9 Layering: Workflow Façade, Generic Runtime, and Long-Text Transformation

The whole system becomes easier to understand if it is read as three stacked concerns rather than one large script.

#### A. Workflow façade layer

This layer lives mainly in `scripts/*` and `yt_transcript_utils.py`.

Its job is to define the nominal business path:

- source discovery and routing
- subtitle path vs Deepgram fallback
- short-path vs long-path planning
- normalization, assembly, and verification entrypoints
- compatibility CLI and repository command surfaces

This layer answers:

> What business path should the job take under normal conditions?

#### B. Generic task-runtime layer

This layer lives in `kernel/task_runtime/*`.

Its job is to make the job controllable as a runtime:

- explicit contracts
- lifecycle state and transitions
- allowed actions and decisions
- ownership and control signals
- recovery semantics
- telemetry and outer runtime API

This layer answers:

> Given the current persisted state, what is the run, what can it legally do next, and how do we control it safely?

#### C. Long-text transformation layer

This layer lives in `kernel/long_text/*`.

Its job is to perform domain-specific text work:

- chunking and prompt assembly
- glossary and semantic-anchor consistency
- LLM request execution
- repair, replan, merge, and resume behavior

This layer answers:

> Given an allowed bounded action, how is the long-text transformation actually carried out?

#### D. Why this split matters

This split is important because it prevents three different concerns from collapsing into one place:

- **workflow concern**: business routing
- **runtime concern**: state-oriented control and recovery
- **domain concern**: transcript-to-article transformation mechanics

That separation is the key architectural move behind the current upgrade.

The result is not “an agent all the way down.”
The result is:

- a workflow-native system
- upgraded by an explicit runtime control layer
- while keeping LLM usage bounded inside the right places

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

### 7.4 Long-text subsystem vocabulary and information flow

Before describing the implementation layers, it helps to first define the subsystem’s own vocabulary and working flow.
This makes the later architecture section easier to read because the reader already knows what each layer is operating on.

#### Long-text vocabulary by layer

As with the runtime, the long-text subsystem becomes much easier to understand when its terms are grouped into layers rather than treated as one flat list.

##### Layer 1: source shaping and boundary contracts

These terms answer: *what source are we transforming, and what are the exact output boundaries?*

- **normalized document**
  - the canonical input shape derived from raw text or timed segments before long-text processing begins
- **plan**
  - the explicit execution plan for one processing or replanning pass
- **`plan_id`**
  - the stable identifier for a specific plan instance
- **chunk contract**
  - the boundary rules that define what a chunk may output and what it must not output
- **continuity policy**
  - the policy that decides what neighboring context may be carried across boundaries as reference only
- **chunk**
  - one bounded processing unit with a stable core range
- **`chunk_id`**
  - the durable identifier of a chunk within a plan

This layer exists so later processing never has to guess what a chunk “probably means.”

##### Layer 2: prompt context and consistency guidance

These terms answer: *what guidance does the model receive before it transforms one chunk?*

- **`prompt_name` / prompt template**
  - the named transformation contract applied to the chunk, such as structure, translation, or cleanup
- **continuity context**
  - limited neighboring context, such as prior tail text or section title, carried as reference rather than output scope
- **glossary**
  - terminology guidance that protects names, terms, and abbreviations across the full document
- **semantic anchors**
  - high-signal facts such as dates, numbers, versions, percentages, and URLs that should survive transformation
- **chapter plan**
  - optional chapter-to-chunk mapping used later for deterministic merge and article structure

This layer is not global orchestration. It is the local guidance package for one bounded transformation step.

##### Layer 3: execution, repair, and replanning control

These terms answer: *how does one chunk move through execution, and what happens when the nominal path fails?*

- **processing loop**
  - the main bounded execution loop over the current chunk set
- **chunk status**
  - the persisted state of a chunk, such as pending, running, done, failed, interrupted, or superseded
- **retry**
  - repeating the same bounded action under the same local assumptions
- **repair**
  - a local corrective attempt that tries to recover one chunk without replacing the full plan
- **replan / `replan_remaining`**
  - generating a new plan for the unfinished remainder because the current plan is no longer healthy enough
- **autotune**
  - bounded adjustment of chunk sizing and output budget based on observed runtime pressure
- **pause / resume / cancel**
  - runtime control semantics applied to long-text work at safe boundaries

This layer is where the subsystem becomes operational rather than merely prompt-driven.

##### Layer 4: verification, merge, and final outputs

These terms answer: *how do we decide that local work is acceptable and reassemble it into a document?*

- **deterministic verification**
  - rule-based checks for missing structure, suspicious length, missing glossary terms, missing anchors, or missing translation
- **quality gate**
  - the final structured stop/go evaluation over the assembled output
- **merge**
  - deterministic ordered concatenation of chunk outputs plus optional document-level wrapping
- **assembled output**
  - the document reconstructed from bounded chunk outputs rather than regenerated holistically
- **warnings / attempt history**
  - the inspection trail that explains where the subsystem had to retry, repair, or degrade

This layer closes the loop by converting many bounded local results into one inspectable document-level result.

##### Why this layered vocabulary matters

The long-text subsystem must be understandable in four directions at once:

- as a boundary system: *what exactly is each chunk allowed to do?*
- as a guidance system: *what context and consistency signals shape generation?*
- as a control system: *how do repair, retry, pause, and replan work?*
- as an output system: *how do local results become a reliable final document?*

That is why the subsystem vocabulary is intentionally layered around boundaries, guidance, execution control, and outputs.

#### Small long-text information flow

The subsystem can be pictured as this compact flow:

```text
Normalized source artifacts
(raw text / segments / normalized document)
        |
        v
Plan + chunk contract
(plan_id / chunk boundaries / continuity policy / chapter mapping)
        |
        v
Manifested chunk set
(chunk states + processing metadata)
        |
        v
Prompt-context assembly
(prompt template + continuity context + glossary + semantic anchors)
        |
        v
LLM request execution
(one bounded chunk action)
        |
        v
Deterministic verification
(length / structure / glossary / anchors / translation checks)
        |
        +-----------------------------+
        | pass                        | fail
        v                             v
Mark chunk done                 Retry / repair
        |                             |
        |                             +------------+
        |                                          |
        +--------------------------> if plan unhealthy
                                              |
                                              v
                                      Replan remaining
                                              |
                                              v
                                     New chunk set / new plan
                                              |
                                              v
Deterministic merge
(chunk outputs + chapter plan + document wrapper)
        |
        v
Final quality gate
        |
        v
Article-like markdown output
```

Not every run will traverse every branch.
Shorter or healthier runs may go straight from chunk execution to merge, while troubled runs may loop through repair, pause/resume, or replanning before converging.

### 7.5 Long-text subsystem architecture

Before describing the logic in detail, it helps to state the layered implementation view explicitly.

#### Layered implementation view

- **Contract and control layer**: `kernel/long_text/contracts.py`
  - defines chunk contract, continuity policy, verification contract, repair policy, and replan policy
- **Lifecycle and resumability layer**: `kernel/long_text/lifecycle.py`
  - defines per-chunk manifest entries, runtime defaults, and resume-repair behavior
- **Prompt and context assembly layer**: `kernel/long_text/prompting.py`, `kernel/long_text/glossary.py`, `kernel/long_text/semantic.py`
  - prepares continuity, terminology, and semantic-anchor context before each LLM call
- **LLM request layer**: `kernel/long_text/llm.py`
  - owns retries, timeout behavior, and streaming fallback
- **Processing-control layer**: `kernel/long_text/processing.py`
  - owns the main chunk loop, repair decisions, autotune, abort / pause / replan behavior, and manifest updates
- **Boundary surface layer**: `kernel/long_text/chunking.py`, `kernel/long_text/merge.py`, `kernel/long_text/execution.py`
  - exposes chunking, merge, resume, and replan command surfaces to the rest of the project

This subsystem also depends on the generic runtime layer rather than reimplementing job control locally:

- `kernel/task_runtime/runtime.py`
- `kernel/task_runtime/lifecycle.py`
- `kernel/task_runtime/state.py`
- `kernel/task_runtime/controller.py`

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

### 7.6 Why this subsystem design fits the whole project

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
- if usable Chinese subtitles exist, they take precedence as the single subtitle source track
- English is used only when no usable Chinese subtitle track can be downloaded
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

The internal implementation is best read as a workflow façade above two kernel layers: `task_runtime` for generic long-running task control, and `long_text` for long-text transformation behavior.

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
- `kernel/task_runtime/contracts.py`
  - runtime object schemas and command contract bundles
- `kernel/task_runtime/lifecycle.py`
  - lifecycle shell and transition summaries
- `kernel/task_runtime/policy.py`
  - allowed-action derivation and budget-pressure evaluation
- `kernel/task_runtime/decision.py`
  - rule-first action selection and bounded decision records
- `kernel/task_runtime/ledger.py`
  - budget and action-accounting summaries
- `kernel/task_runtime/recovery.py`
  - processing substates and recovery recommendations
- `kernel/task_runtime/artifacts.py`
  - artifact references and artifact-graph summaries
- `kernel/task_runtime/evaluator.py`
  - structured quality and recommendation outputs
- `kernel/task_runtime/api.py`
  - stable external runtime API for create / inspect / advance / control / resume / finalize
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

#### 备选方案评估：直接走 YouTube InnerTube 字幕获取

项目评估过直接通过 YouTube InnerTube 获取字幕，作为 `yt-dlp` 的替代获取策略。该方案对纯字幕路径更轻量，并可降低简单本地运行的依赖成本。当前架构不将其设为默认获取层，因为它仍属于非官方、对接口变化敏感的获取面，不能消除“无字幕时仍需音频转录兜底”的要求，也会将更多提取维护成本转移到项目内部。

因此，这里的设计结论是：

- `yt-dlp` 仍然是主字幕获取依赖
- 当可用字幕不存在时，Deepgram 仍然承担兜底路径
- InnerTube 只被视为未来可选的 fast path，而不是规范默认方案

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

### 6.8 运行时设计：有边界的自适应控制层

#### 为什么需要 runtime

这个项目本来就有清晰的业务 workflow，但当执行过程变成长时间运行、可中断、可恢复的作业时，仅靠 workflow 结构本身是不够的。

如果只有 path-oriented 的流程控制，系统很容易逐渐堆积出：

- 藏在 `if/else` 里的隐式分支
- 以 special case 形式出现的恢复逻辑
- 分散在不同命令里的 pause / resume / cancel 语义
- 很难回答“系统现在相信什么、下一步能做什么、为什么这样做”

runtime 的存在，就是为了解决这个问题。

它把执行从“带条件的命令串”提升为“一个有明确状态、允许动作集合和持久控制语义的有边界作业”。

#### runtime 是什么，不是什么

在这个项目里，runtime 是：

- 位于 workflow 路由和工具执行之间的 **有边界自适应控制层**
- **state-oriented** 而不是 path-oriented
- **rule-first** 的；LLM 只在受限决策中做可选辅助
- **local-first** 的；重要真相必须落盘
- **可操作** 的；run 必须可 inspect、pause、resume、cancel、解释

它不是：

- skill 内再嵌一个自由规划的第二 agent
- 对业务 workflow 的替代
- 允许模型随意发明下一步动作的理由
- 一个通用 multi-agent 平台

所以这里的 runtime，本质上应理解为 **adaptive control**，而不是 **autonomous agency**。

#### runtime 的核心职责

runtime 主要承担六类职责：

1. **统一 runtime contracts**
   - 用显式 schema 表示 task intent、run state、observation、decision、action、quality signal 与 artifacts

2. **显式驱动 lifecycle**
   - 让 lifecycle state 与 stage transition 变成可见的一等语义，而不是隐藏在分支结构里

3. **推导 allowed actions**
   - 基于 policy、预算压力、恢复信号与质量结果，明确当前时刻哪些动作合法

4. **选择并派发受限动作**
   - 从 allowed set 中选一个动作，调用兼容实现，并记录为什么选它

5. **持久化运行真相**
   - 把状态、控制信号、ownership marker 与 telemetry 写到磁盘上，让 run 可检查、可恢复

6. **对外暴露稳定 contract**
   - 给外层调用者一个小而稳的 runtime-facing API，而不是逼它理解内部 helper command

#### runtime 的 lifecycle loop

runtime 的控制模型，最好理解成下面这个循环：

`Observe -> Evaluate -> Derive Allowed Actions -> Decide -> Validate -> Act -> Commit -> Transition`

每一步的职责都不同：

- **Observe**
  - 读取 manifest、control files、ownership marker、已有结果等持久化状态
- **Evaluate**
  - 汇总执行健康度、预算压力、processing substate 与质量信号
- **Derive Allowed Actions**
  - 在当前 policy 下生成明确的合法下一步动作集合
- **Decide**
  - 从 allowed set 中选一个动作；规则优先，模型辅助是可选且受限的
- **Validate**
  - 在真正 mutation 或外部调用前确认前置条件
- **Act**
  - 执行被选中的受限命令
- **Commit**
  - 持久化新的状态、控制变化、产物与 telemetry
- **Transition**
  - 产出新的 lifecycle state 与 transition summary

这也是从 workflow control 升级到 runtime control 的关键：

- workflow 仍然负责名义业务路径
- runtime 负责当前运行状态以及合法的下一步控制动作

#### 分层理解 runtime 词汇表

为了让 runtime 更容易读懂，最好不要把这些概念当成一个平铺的大列表，而是按四层来理解。

##### 第 1 层：身份与锚点

这一层回答的是：*这到底是哪一个 run，它的持久化真相锚在什么地方？*

- **`task_id`**
  - 任务意图的稳定身份
- **`run_id`**
  - 面向外部调用者暴露的当前 runtime instance 稳定身份
- **`work_dir`**
  - 这次 run 的本地根目录；runtime 的持久化真相主要锚定在这里
- **`TaskSpec`**
  - 从外层调用者视角描述任务意图的标准化 contract
- **`RunState`**
  - 描述当前 lifecycle state、stage、ownership 与 effective runtime status 的标准化 contract

这一层先解决“我们现在讨论的是哪份作业”这个问题，后面的控制逻辑才有意义。

##### 第 2 层：执行真相与控制面

这一层回答的是：*当前运行中到底什么是真的，谁又有权修改它？*

- **`.runtime_task.json`**
  - 面向集成层稳定 contract 的 task record
- **`manifest.json`**
  - 长文本 plan、chunk 状态与 runtime status 的领域执行真相
- **`ownership` / `.runtime_owner.json`**
  - 本地独占 mutation 租约，防止多个写入者把状态写坏
- **`.runtime_pause.json`** 与 **`.runtime_cancel.json`**
  - 带外控制信号；通过请求 pause/cancel，而不是重写整个 manifest 来表达控制意图
- **`LifecycleTransition`**
  - 一次命令把 run 从哪个 lifecycle state 推到哪个 lifecycle state 的显式摘要

设计意图是：

- `manifest.json` 与 control files 表达执行中的运行真相
- `.runtime_task.json` 表达面向集成的稳定身份与任务意图
- ownership 表达的是 mutation authority，而不是业务语义本身

##### 第 3 层：决策与自适应

这一层回答的是：*给定当前真相，runtime 合法的下一步是什么，为什么？*

- **`Observation`**
  - 从命令结果与持久化状态中抽取出来的标准化事实
- **`PolicyEvaluation`**
  - policy 输出，负责生成压力信号和当前 `allowed_actions`
- **`allowed_actions`**
  - 在当前 policy 约束下，合法的下一步动作集合
- **`DecisionRecord`**
  - 这次最终选了什么动作、理由是什么、经过了哪些 policy checks
- **`ActionResult`**
  - 被执行的受限命令以及其结果的标准化报告
- **`ProcessingState`**
  - 当前 processing 子阶段的细粒度姿态
- **`RecoverySummary`**
  - 当前 run 的可恢复性摘要与推荐恢复动作

这一层决定了 runtime 为什么能“自适应”，但又不会失控。
它可以响应变化，但只能通过显式 policy 和显式 action set 来响应。

##### 第 4 层：输出与可观察性

这一层回答的是：*这次 run 产出了什么、质量如何、操作员怎么检查历史？*

- **`ArtifactRef` / `ArtifactGraph`**
  - 产物引用以及产物之间的血缘关系
- **`QualityReport` / `EvaluatorReport`**
  - 与 nominal processing 分离的结构化质量信号和建议动作
- **`telemetry.jsonl`**
  - append-only 的运行日志与命令历史
- **command envelope**
  - 稳定的结果包装层，把 `result`、`contracts` 与 telemetry metadata 一起返回

这一层的作用，是让 runtime 可检查，但又不把“历史事件”误当成“当前状态本身”。

##### 为什么要这样分层

runtime 必须能同时朝三个方向被解释：

- 对外部调用者：*这是什么 run？*
- 对控制层：*当前什么是真的，下一步允许什么？*
- 对操作者：*发生过什么，现在有哪些产物、告警与质量信号？*

所以这些词汇必须刻意拆成“身份、执行真相、决策、可观察性”四层，而不是混在一起。

#### 小型 runtime 信息流图

可以把 runtime 理解成下面这条紧凑的信息流：

```text
Outer Agent / CLI
        |
        | create-run
        v
  .runtime_task.json
  (TaskSpec + run_id)
        |
        | inspect-run / advance-run / apply-control / resume-run / finalize-run
        v
+--------------------------- work_dir ----------------------------+
| manifest.json          -> execution truth                       |
| .runtime_owner.json    -> mutation authority                    |
| .runtime_pause.json    -> pause request                         |
| .runtime_cancel.json   -> cancel request                        |
| telemetry.jsonl        -> append-only history                   |
+----------------------------------------------------------------+
        |
        v
Observe / Summarize
(TaskSpec + RunState + Observation
 + ProcessingState + RecoverySummary)
        |
        v
PolicyEvaluation
(derive allowed_actions)
        |
        v
DecisionRecord
(select one bounded action)
        |
        v
Act
(process / replan / pause / resume / cancel / finalize)
        |
        v
Commit
(update manifest / control files / artifacts / telemetry)
        |
        v
ActionResult + LifecycleTransition
        |
        v
Stable envelope / inspect-run / finalize-run result
```

并不是每个命令都会以同样深度走完整个循环。
比如 `create-run` 主要负责初始化身份和稳定 contract，而 `advance-run` 则是最完整地驱动 bounded control loop 的入口。

#### 决策模型：规则优先，LLM 可选，但绝不越权

runtime 的关键设计原则之一，是模型不能接管 orchestration。

因此：

- workflow code 仍然定义名义业务骨架
- policy 先推导 allowed action set
- decision layer 只能在这个 allowed set 里做选择
- 可选的 LLM ranker 只能在 allowed set **内部** 排序或选择
- `pause`、`resume`、`cancel`、`prepare_resume`、`replan_remaining` 等恢复动作必须是 first-class runtime semantics，而不是 special case

这样系统才能在保留自适应能力的同时，不失去控制边界。

#### 对外稳定 runtime API

面向外部调用者，当前首选的 runtime surface 是：

- `create-run`
- `inspect-run`
- `advance-run`
- `apply-control`
- `resume-run`
- `finalize-run`

这组 API 的作用有三点：

- 用稳定 contract 隐藏内部 helper-command 细节
- 让外层 agent 可以把 skill 当成一个 bounded runtime capability 调用
- 保留向后兼容，因为旧命令仍然存在，但退居实现级 helper 的位置

所以迁移方向是：

- **preferred path**：runtime-facing API
- **compatibility path**：legacy helper commands 与 CLI shim

#### 实现锚点

runtime 设计在当前仓库里的落点如下：

- `kernel/task_runtime/contracts.py`
  - runtime 对象 schema 与 command contract bundle
- `kernel/task_runtime/lifecycle.py`
  - lifecycle shell 与 transition summary
- `kernel/task_runtime/policy.py`
  - allowed-action derivation 与 budget-pressure evaluation
- `kernel/task_runtime/decision.py`
  - rule-first action selection，以及受限的模型辅助决策
- `kernel/task_runtime/ledger.py`
  - budget 与 action accounting 摘要
- `kernel/task_runtime/recovery.py`
  - processing substate 与 recovery recommendation
- `kernel/task_runtime/artifacts.py`
  - artifact 引用与 artifact-graph 摘要
- `kernel/task_runtime/evaluator.py`
  - 结构化的质量建议信号
- `kernel/task_runtime/api.py`
  - 面向外层 agent 的稳定 runtime API：`create-run / inspect-run / advance-run / apply-control / resume-run / finalize-run`
- `kernel/task_runtime/runtime.py`
  - command envelope、telemetry append 与 ownership 辅助
- `kernel/task_runtime/state.py`
  - manifest、runtime state 与 control-file 持久化辅助
- `kernel/task_runtime/controller.py`
  - owned mutation 与 bounded control-loop 包装器
- `kernel/task_runtime/telemetry.py`
  - 面向操作者的 telemetry 查询与汇总接口

### 6.9 分层：Workflow Façade、Generic Runtime 与 Long-Text Transformation

如果把整个系统理解成三层叠放的职责，而不是一大段脚本，runtime 的位置会更清晰。

#### A. Workflow façade 层

这一层主要落在 `scripts/*` 和 `yt_transcript_utils.py`。

它负责定义名义业务路径：

- source discovery 与 routing
- 字幕路径还是 Deepgram fallback
- short-path 还是 long-path planning
- normalization、assembly、verification 的入口
- 兼容 CLI 与仓库级命令表面

这一层回答的问题是：

> 在正常业务语义下，这个任务应该走哪条路径？

#### B. Generic task-runtime 层

这一层落在 `kernel/task_runtime/*`。

它负责把任务变成一个可控制的 runtime：

- 显式 contracts
- lifecycle state 与 transition
- allowed actions 与 decisions
- ownership 与 control signals
- recovery semantics
- telemetry 与外部 runtime API

这一层回答的问题是：

> 给定当前持久化状态，这个 run 现在是什么状态、合法下一步是什么、怎样才能安全控制它？

#### C. Long-text transformation 层

这一层落在 `kernel/long_text/*`。

它负责真正的领域执行：

- chunking 与 prompt 组装
- glossary 与 semantic-anchor 一致性控制
- LLM 请求执行
- repair、replan、merge、resume 行为

这一层回答的问题是：

> 在一个被允许的受限动作已经确定之后，长文本变换到底怎样被执行？

#### D. 为什么这种拆分重要

这种拆分很重要，因为它避免了三类问题坍缩在一起：

- **workflow concern**：业务路由
- **runtime concern**：面向状态的控制与恢复
- **domain concern**：transcript-to-article 的变换机制

这正是当前升级里最关键的架构动作。

升级后的结果不是“把整个系统都做成 agent”。
而是：

- 仍然是一个 workflow-native 系统
- 但升级出了显式的 runtime control layer
- 同时把 LLM 严格限制在合适的位置使用

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

### 7.4 长文本子系统的词汇表与信息流

在展开实现分层之前，最好先定义这个子系统自己的词汇体系与工作流。
这样后面的架构章节会更容易理解，因为读者先知道“每一层到底在操作什么对象”。

#### 分层理解长文本子系统词汇表

和 runtime 一样，长文本子系统如果被当成一个平铺的大词汇表，就会很难读。
把它按层拆开，会清楚很多。

##### 第 1 层：source 整形与边界 contract

这一层回答的是：*我们到底在变换什么 source，以及每个 chunk 的输出边界到底是什么？*

- **normalized document**
  - 在长文本处理真正开始之前，由 raw text 或 timed segments 规范化出来的标准输入形态
- **plan**
  - 一次 processing 或 replanning pass 的显式执行计划
- **`plan_id`**
  - 某个具体计划实例的稳定标识
- **chunk contract**
  - 定义 chunk 允许输出什么、不允许输出什么的边界规则
- **continuity policy**
  - 决定哪些相邻上下文可以跨边界携带为 reference-only 的策略
- **chunk**
  - 一个有明确边界的处理单元
- **`chunk_id`**
  - 某个 chunk 在当前计划里的稳定标识

这一层存在的意义，是让后面的执行阶段不需要再去猜“这个 chunk 大概是什么意思”。

##### 第 2 层：prompt 上下文与一致性引导

这一层回答的是：*模型在处理某个 chunk 之前，究竟会收到哪些引导信息？*

- **`prompt_name` / prompt template**
  - 施加在当前 chunk 上的命名变换 contract，例如 structure、translation、cleanup
- **continuity context**
  - 诸如上一段 tail text、上一节标题之类的有限相邻上下文；它是 reference，不属于输出范围
- **glossary**
  - 保护整篇文档中名称、术语、缩写一致性的术语引导
- **semantic anchors**
  - 日期、数字、版本号、百分比、URL 这类应当被保留下来的高信号事实
- **chapter plan**
  - 可选的 chapter-to-chunk 映射，供后续 deterministic merge 和文章结构装配使用

这一层不是全局 orchestration，而是某一个受限变换步骤的局部引导包。

##### 第 3 层：执行、修复与重规划控制

这一层回答的是：*一个 chunk 在执行过程中怎么流转，名义路径失败时又会发生什么？*

- **processing loop**
  - 面向当前 chunk 集合的主执行循环
- **chunk status**
  - chunk 的持久化状态，例如 pending、running、done、failed、interrupted、superseded
- **retry**
  - 在同样的局部假设下，再执行一次同样的受限动作
- **repair**
  - 不更换整份 plan 的前提下，对单个 chunk 进行局部修复
- **replan / `replan_remaining`**
  - 因为当前计划已不够健康，所以为未完成剩余部分生成新计划
- **autotune**
  - 基于运行时压力，对 chunk 尺寸和输出预算做有边界的调整
- **pause / resume / cancel**
  - 在安全边界上施加到长文本作业上的 runtime control 语义

这一层使得子系统不只是“会发 prompt”，而是真正成为一个可运行、可恢复、可收敛的工程系统。

##### 第 4 层：验证、merge 与最终输出

这一层回答的是：*我们怎么判断局部结果可接受，并把它们重新装配成整篇文档？*

- **deterministic verification**
  - 基于规则的检查：结构缺失、长度异常、glossary 缺失、anchors 缺失、该翻译未翻译等
- **quality gate**
  - 对 assembled output 做最终的结构化 stop/go 评估
- **merge**
  - 通过 ordered concatenation 加上文档级包装，对 chunk 输出做确定性重组
- **assembled output**
  - 由多个受限 chunk 结果重建出来的整篇文档，而不是重新整体生成
- **warnings / attempt history**
  - 说明系统在哪些地方 retry、repair、degrade 过的检查轨迹

这一层负责把很多个受限的局部结果，收敛成一个可检查的文档级结果。

##### 为什么要这样分层

长文本子系统必须同时朝四个方向可解释：

- 作为边界系统：*每个 chunk 究竟被允许做什么？*
- 作为引导系统：*哪些上下文和一致性信号在塑造生成？*
- 作为控制系统：*repair、retry、pause、replan 是怎么运作的？*
- 作为输出系统：*局部结果最后怎样变成可靠整文？*

所以它的词汇必须围绕“边界、引导、执行控制、输出”这四层来组织，而不是混成一团。

#### 小型长文本信息流图

可以把这个子系统理解成下面这条紧凑的信息流：

```text
Normalized source artifacts
(raw text / segments / normalized document)
        |
        v
Plan + chunk contract
(plan_id / chunk boundaries / continuity policy / chapter mapping)
        |
        v
Manifested chunk set
(chunk states + processing metadata)
        |
        v
Prompt-context assembly
(prompt template + continuity context + glossary + semantic anchors)
        |
        v
LLM request execution
(one bounded chunk action)
        |
        v
Deterministic verification
(length / structure / glossary / anchors / translation checks)
        |
        +-----------------------------+
        | pass                        | fail
        v                             v
Mark chunk done                 Retry / repair
        |                             |
        |                             +------------+
        |                                          |
        +--------------------------> if plan unhealthy
                                              |
                                              v
                                      Replan remaining
                                              |
                                              v
                                     New chunk set / new plan
                                              |
                                              v
Deterministic merge
(chunk outputs + chapter plan + document wrapper)
        |
        v
Final quality gate
        |
        v
Article-like markdown output
```

并不是每个 run 都会经过每一条分支。
更短或更健康的 run，可能直接从 chunk execution 进入 merge；而遇到问题的 run，则可能先经历 repair、pause/resume 或 replanning，再逐步收敛。

### 7.5 长文本子系统的架构设计

在讲具体逻辑之前，先把实现层次直接说清楚。

#### 分层实现视图

- **Contract 与 control 层**：`kernel/long_text/contracts.py`
  - 定义 chunk contract、continuity policy、verification contract、repair policy 与 replan policy
- **Lifecycle 与 resumability 层**：`kernel/long_text/lifecycle.py`
  - 定义每个 chunk 的 manifest 记录、runtime 默认值，以及 resume repair 行为
- **Prompt 与上下文组装层**：`kernel/long_text/prompting.py`、`kernel/long_text/glossary.py`、`kernel/long_text/semantic.py`
  - 在每次 LLM 调用前准备 continuity、terminology、semantic anchor 相关上下文
- **LLM request 层**：`kernel/long_text/llm.py`
  - 管理 retries、timeout 行为，以及 streaming fallback
- **Processing control 层**：`kernel/long_text/processing.py`
  - 管理主 chunk 循环、repair 决策、autotune、abort / pause / replan 行为，以及 manifest 更新
- **边界命令层**：`kernel/long_text/chunking.py`、`kernel/long_text/merge.py`、`kernel/long_text/execution.py`
  - 向项目其它部分暴露 chunking、merge、resume、replan 这些命令接口

同时，这个子系统不会自己重复实现一套作业控制逻辑，而是直接依赖通用 runtime 层：

- `kernel/task_runtime/runtime.py`
- `kernel/task_runtime/lifecycle.py`
- `kernel/task_runtime/state.py`
- `kernel/task_runtime/controller.py`

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

### 7.6 为什么这个子系统设计适合整个项目

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
- 如果存在可用中文字幕，会优先把其中一个中文字幕轨作为唯一 source text
- 只有在没有可用中文字幕轨时，才回退到英文字幕作为 source text
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

当前内部实现最好理解为：一个 workflow façade 位于两层 kernel 之上；其中 `task_runtime` 负责通用长程任务控制，`long_text` 负责长文本变换行为。

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
- `kernel/task_runtime/contracts.py`
  - runtime 对象 schema 与 command contract bundle
- `kernel/task_runtime/lifecycle.py`
  - lifecycle shell 与 transition summary
- `kernel/task_runtime/policy.py`
  - allowed-action derivation 与 budget-pressure evaluation
- `kernel/task_runtime/decision.py`
  - rule-first action selection 与受限决策记录
- `kernel/task_runtime/ledger.py`
  - budget 与 action accounting 摘要
- `kernel/task_runtime/recovery.py`
  - processing substate 与 recovery recommendation
- `kernel/task_runtime/artifacts.py`
  - artifact 引用与 artifact-graph 摘要
- `kernel/task_runtime/evaluator.py`
  - 结构化质量与建议动作输出
- `kernel/task_runtime/api.py`
  - 稳定的外部 runtime API：create / inspect / advance / control / resume / finalize
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
