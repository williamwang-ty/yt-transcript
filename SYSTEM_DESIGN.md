# System Design (v5.9-stage9)

**Stage**: 9

**Status**: accepted and implemented

**Behavior change in this stage**: extracted mutation wrappers and bounded auto-replan orchestration into `kernel_controller.py`, added a public `cancel-run` command, and made `process-chunks` consume local cancellation markers safely while preserving Stage 8 state and Stage 7 ownership contracts

**Source of truth**: This file is the canonical system design document for the repository.

**Non-authoritative companion**: `LONG_TEXT_TRANSFORMATION_KERNEL.md` remains a staging draft for future kernelization ideas and does not override this document.

---

## 1. Stage 9 Goal

Stage 9 hardens the controller boundary of the long-text kernel.

Its goals are:

- extract mutation wrappers and bounded replan orchestration into a dedicated controller module
- promote local cancellation from an inspectable marker to a public command contract
- make chunk execution observe cancellation at safe boundaries without breaking ownership or resumability semantics
- keep state-store, envelope, and telemetry contracts compatible while expanding runtime control behavior

Stage 9 remains intentionally bounded. It does **not** introduce distributed scheduling, public pause/resume, or a fully concurrency-safe persistent state store.

---

## 2. System Scope

### 2.1 What This Repository Is

This repository is a script-first long-text transformation system specialized around YouTube transcript workflows.

Its current implemented use cases are:

- transcript extraction from subtitles or Deepgram fallback
- transcript cleanup and structure restoration
- bilingual translation workflow
- explicit normalization from raw text or timed segments
- canonical chunking from normalized documents
- resumable chunk execution with manifest repair
- explicit chunk verification, bounded repair, and bounded replan control loops
- stable kernel command envelopes for Python and CLI consumers
- local telemetry journals for kernel command runs
- local runtime ownership for manifest-mutating commands
- local runtime status inspection for work directories
- public local cancellation for active chunk-processing runs
- deterministic merge and final markdown assembly
- workflow validation and stop/go quality checks

### 2.2 Current Primary Design Goal

> Enable reliable long-text transcript transformation under context limits, especially on weaker models, using script-owned state, explicit normalization, explicit chunk contracts, bounded continuity, explicit lifecycle semantics, explicit control contracts, deterministic verification, compatibility-preserving command interfaces, and local single-owner runtime mutation guards.

### 2.3 Current Non-Goals

At Stage 9, the system is **not yet**:

- a generalized multi-source document transformation framework
- a reusable extracted kernel package
- a global glossary / terminology propagation system
- an LLM-judge semantic verification system
- a fully concurrency-safe persistent state store beyond local single-owner work-dir mutation
- a public pause / resume protocol for long-running jobs beyond cancellation
- a telemetry-first production runtime with remote aggregation or tracing backends

---

## 3. Current Architecture

### 3.1 Current End-to-End Flow

The current system behaves approximately like this:

```text
source acquisition
  -> source selection (subtitles vs Deepgram)
  -> raw text and/or timed segments extraction
  -> state synchronization
  -> normalization
  -> planning
  -> canonical chunk contract selection
  -> operation control contract emission
  -> stable command envelope / trace allocation
  -> runtime ownership acquisition for manifest-mutating commands
  -> chunking
  -> resume preflight / manifest repair
  -> per-chunk prompt execution
  -> chunk output verification
  -> bounded same-plan repair or document replan decision
  -> deterministic merge
  -> deterministic final assembly
  -> quality verification
  -> runtime ownership release
  -> local telemetry journal append

read-only runtime inspection
  -> manifest / ownership / local control snapshot
  -> stable runtime-status result

public runtime cancellation
  -> cancel-run writes local cancellation marker
  -> process-chunks consumes marker at the next safe boundary
```

### 3.2 Current Persisted Artifacts

The current implementation relies on these persisted artifacts:

- `/tmp/${VIDEO_ID}_machine_state.json`
  - authoritative machine-readable state surface
- `/tmp/${VIDEO_ID}_state.md`
  - workflow-facing compatibility projection
- `/tmp/${VIDEO_ID}_normalized_document.json`
  - normalized source artifact
- `manifest.json` in chunk work directories
  - chunk plan, chunk contract, continuity policy, runtime state, resume / autotune / replan metadata, and explicit operation-control state
  - manifest schema remains `v5` in Stage 9
- `/tmp/${VIDEO_ID}_raw_text.txt`
  - raw extracted transcript-like text
- `/tmp/${VIDEO_ID}_segments.json`
  - optional timed source segments from subtitles or Deepgram
- `/tmp/${VIDEO_ID}_structured.txt`
  - optional intermediate structured text
- `/tmp/${VIDEO_ID}_optimized.txt`
  - transformed output before final assembly
- final markdown output under configured output directory
- `telemetry.jsonl` beside inferred kernel work artifacts when a stable local sink can be resolved
  - append-only local command journal for envelope-producing kernel commands
- `.runtime_owner.json` inside chunk work directories during manifest-mutating operations
  - inspectable single-owner runtime marker used by `prepare-resume`, `process-chunks`, `replan-remaining`, and `process-chunks-with-replans`
- optional `.runtime_cancel.json` inside chunk work directories
  - public local cancellation marker written by `cancel-run`, surfaced by `runtime-status`, and consumed by `process-chunks`

### 3.3 Current State Surfaces

The system still uses a two-surface state model:

- `machine_state.json` is authoritative for helper commands
- `state.md` remains the workflow-facing compatibility surface
- helper commands auto-sync legacy state into machine state
- planning and normalization run through that bridge
- chunk execution operates on downstream artifacts and manifests, not on chat-memory assumptions

### 3.4 Current Normalization Layer

The normalization layer remains the same Stage 2 / Stage 3 substrate.

The current supported normalization source adapters are:

- `raw_text_file`
- `segments_json`

The current `normalized_document.json` contains:

- source metadata
- workflow mode metadata
- artifact references
- normalized plain text
- optional normalized segment list
- `preferred_chunk_source` (`text` or `segments`)

### 3.5 Current Chunking Layer

Stage 3 introduced three chunking drivers:

- `chunk-text`
- `chunk-segments`
- `chunk-document`

The canonical rule still holds:

- when a normalized document exists, `chunk-document` is the preferred chunking entrypoint
- lower-level chunkers remain supported compatibility drivers

### 3.6 Current Chunk Contract

`manifest.json` records `plan.chunk_contract` with the current explicit assumptions:

- `driver`
- `source_kind`
- `boundary_mode = strict`
- `output_scope = current_chunk_only`
- `continuity_mode = reference_only`
- `merge_strategy = ordered_concat`
- `overlap_strategy = context_only_no_output_overlap`
- optional normalized-document and source-adapter references

This means the merge contract remains deterministic ordered concatenation of chunk-local outputs.

### 3.7 Current Continuity Model

`manifest.json` also records `plan.continuity`.

The current policy expresses:

- `mode = reference_only`
- `tail_sentences`
- `summary_token_cap`
- `carry_section_title`
- `carry_tail_text`
- `boundary_rule`
- `output_rule`

`process-chunks` follows this manifest-owned policy rather than silently drifting with later config changes.

### 3.8 Current Runner Lifecycle Model

Stage 4 formalizes the minimal chunk lifecycle model.

#### Chunk statuses

Current active chunk statuses are:

- `pending`
- `running`
- `interrupted`
- `done`
- `failed`
- `superseded`

#### Runtime statuses

Current manifest runtime statuses are:

- `pending`
- `running`
- `resumable`
- `aborted`
- `completed_with_errors`
- `completed`

#### Current interpretation

- `pending`: never completed under the active plan
- `running`: execution had started and the manifest was last written mid-run
- `interrupted`: a resumable chunk that must be rerun under the current plan
- `done`: a durable chunk checkpoint exists
- `failed`: the chunk failed in the current run, but may still be resumable depending on overall runtime state
- `superseded`: the chunk belongs to an older plan and is no longer active

### 3.9 Current Checkpoint Rules

Stage 4 now makes the current checkpoint rules explicit:

- a **durable chunk checkpoint** means:
  - chunk status is `done`
  - expected output file exists on disk
- a stale `running` chunk with an output file is promoted to `done` during resume repair
- a stale `running` chunk without an output file is demoted to `interrupted`
- a `done` chunk missing its output file is demoted to `interrupted`
- `process-chunks` performs this repair automatically before resuming work
- `prepare-resume` exposes the same repair step explicitly for inspection and manual control

### 3.10 Current Resume Model

Stage 4 adds a new explicit helper command:

- `prepare-resume`

Its current role is:

- inspect `manifest.json`
- reconcile chunk status against output-file checkpoints
- repair stale runtime status
- persist the repaired manifest
- return a structured JSON resume report

`process-chunks` now calls the same repair logic automatically before execution starts and returns the resume report in its JSON result.

### 3.11 Current Planning, Interface, State, Controller, Control, and Ownership Surfaces

`plan-optimization` now:

- validates workflow state
- auto-materializes normalization when source artifacts exist
- emits an explicit `chunking` block
- emits per-operation `control` contracts
- emits top-level `quality_contract` and `replan_contract` surfaces

The current `chunking` block reports:

- canonical chunk driver
- preferred source kind
- boundary mode
- continuity mode
- merge strategy

The current per-operation `control` contract reports:

- chunk-output verification rules
- bounded repair rules for suspicious chunk outputs
- replan triggers and the required action (`auto_replan_remaining` or `stop_and_review`)
- final quality-gate expectations for the operation output

Stage 7 now defines an extracted runtime boundary in `kernel_runtime.py`.

Its current responsibilities are:

- stable command-envelope construction
- local telemetry sink inference and append-only journal writes
- compatibility-preserving command dispatch wrapping for kernel commands
- local runtime ownership acquisition / release helpers for manifest-mutating commands

Stage 8 now also defines a state-store boundary in `kernel_state.py`.

Its current responsibilities are:

- manifest path resolution and JSON read/write helpers
- atomic local file writes for manifest-adjacent runtime artifacts
- local runtime-control file summaries
- read-only runtime inspection via `runtime-status`
- local cancellation marker read/write / consume helpers

Stage 9 now also defines a controller boundary in `kernel_controller.py`.

Its current responsibilities are:

- owned mutation wrapper orchestration for manifest-mutating commands
- delegated ownership propagation into nested control loops
- bounded auto-replan loop execution for `process-chunks-with-replans`
- compatibility-preserving mutation result finalization

The stable envelope now treated as long-term compatible uses:

- `format = yt_transcript.command_result/v1`
- `schema_version = 1`
- `command`
- `trace_id`
- `generated_at`
- `ok`
- `telemetry`
- `result`

The local telemetry event now treated as long-term compatible uses:

- `format = yt_transcript.telemetry_event/v1`
- `schema_version = 1`
- `event_type = command_result`
- `trace_id`
- `command`
- `timestamp`
- `duration_ms`
- `success`
- `warning_count`
- `document_id`
- inferred local sink path in `telemetry.jsonl`

Stage 7 also adds a local runtime ownership surface:

- `format = yt_transcript.runtime_owner/v1`
- `schema_version = 1`
- `owner_id`
- `operation`
- `pid`
- `work_dir`
- `acquired_at`
- owner file path `.runtime_owner.json`

Stage 8 also adds a read-only runtime status surface:

- `runtime-status` is a stable kernel command for local inspection
- it reports manifest presence, runtime state, chunk-status counts, ownership state, and local cancellation-marker state
- it does not mutate the work directory and therefore does not acquire runtime ownership

Stage 9 also adds a public local cancellation surface:

- `cancel-run` is a stable kernel command for requesting cancellation of active chunk-processing work
- it writes `.runtime_cancel.json` in the target work directory
- `process-chunks` checks for cancellation before work starts and between chunks
- observed cancellation markers are consumed and cleared before the runner aborts at the next safe boundary

The current ownership policy is:

- only one manifest-mutating command may actively own a given work directory at a time
- `prepare-resume`, `process-chunks`, `replan-remaining`, and `process-chunks-with-replans` all claim ownership before mutation
- a stale owner is currently recoverable when the owner file is invalid, missing a usable pid, or points to a dead process
- `process-chunks-with-replans` acquires once and passes the same ownership through inner `process-chunks` / `replan-remaining` steps
- ownership conflicts return structured JSON failures instead of silently racing on `manifest.json`

`process-chunks` continues to persist the active operation surface into `manifest.json` via:

- `runtime.operation_prompt_name`
- `runtime.operation_input_key`
- `runtime.operation_control`
- `runtime.control` counters and last replan trigger/action
- per-chunk `control` status (`verification_status`, warnings, retry reasons, repair exhaustion)

Current control semantics remain:

- verification = inspect produced text and classify warnings versus hard stop/go failures
- repair = rerun the same chunk under the same active plan
- replan = abort the current run because the active plan is no longer trusted
- cancellation = stop an active chunk-processing run at the next safe boundary without disturbing completed chunk checkpoints
- `process-chunks-with-replans` = bounded wrapper that auto-replans raw-path plans up to `max_replans`

### 3.12 Current Core Commands

The current Python utility surface includes the following architectural primitives:

- `sync-state`
- `normalize-document`
- `validate-state`
- `plan-optimization`
- `chunk-document`
- `chunk-text`
- `chunk-segments`
- `prepare-resume`
- `process-chunks`
- `replan-remaining`
- `runtime-status`
- `cancel-run`
- `merge-content`
- `assemble-final`
- `verify-quality`

### 3.13 Current Architectural Strengths

The system now has the following concrete strengths:

- script-first routing for high-risk decisions
- explicit state instead of relying on chat memory
- backward-compatible state evolution
- explicit normalization instead of hidden source assumptions
- explicit chunk contract instead of implied merge behavior
- plan-owned continuity instead of runtime drift
- canonical normalized-document chunking
- explicit lifecycle and checkpoint semantics for chunk runs
- automatic manifest repair before resume
- explicit operation control contracts from planner to runner
- explicit separation between repair and replan
- bounded same-plan recovery and bounded auto-replan behavior
- compatibility-preserving stable envelope APIs for kernel commands
- lightweight append-only telemetry journals for kernel command runs
- deterministic merge and final file assembly
- explicit verification checkpoints before final output

---

## 4. Current Design Principles

### 4.1 Script-First Decisions

Branching and validation logic should live in scripts, not prompt prose, whenever reliability matters.

### 4.2 Explicit State over Conversational Recall

The system should prefer persisted state files and structured artifacts over model memory.

### 4.3 Explicit Normalization over Implicit Source Assumptions

Downstream logic should consume a named normalized artifact whenever possible.

### 4.4 Explicit Chunk Contract over Hidden Merge Assumptions

Chunk boundaries, output scope, continuity mode, and merge expectations should be recorded in the manifest plan.

### 4.5 Plan-Owned Continuity over Runtime Drift

Once a chunk plan exists, continuity semantics should come from the manifest plan rather than from whichever config happens to be loaded later.

### 4.6 Explicit Checkpoints over Optimistic Resume

Resuming long-running work should reconcile manifest state against durable output files rather than trusting stale in-memory assumptions.

### 4.7 Deterministic Steps Stay out of Prompts

State validation, normalization, lifecycle repair, control gating, and final assembly should stay deterministic whenever possible.

### 4.8 Weak-Model Compatibility Matters

The system should continue to work with smaller or weaker models by keeping prompt responsibilities narrow and runtime control explicit.

### 4.9 Debuggability Is a First-Class Requirement

Intermediate artifacts, manifest plans, lifecycle state, resume repairs, and local telemetry journals should all remain inspectable on disk.

### 4.10 Stable Interfaces Should Preserve Compatibility

New public interfaces should prefer additive envelope layers over breaking changes to existing flat command results.

---

## 5. Stage 9 Deliverables

Completed in this stage:

- extracted mutation wrappers and bounded auto-replan orchestration into `kernel_controller.py`
- routed manifest-mutating command wrappers through the controller module while preserving Stage 7 ownership semantics
- added `cancel-run` as a public local cancellation command on the stable kernel interface
- made `process-chunks` consume `.runtime_cancel.json` before work starts and between chunks
- recorded cancellation abort details in runtime state and returned cancellation details in command results
- added regression coverage for cancellation requests, cancellation-aware chunk aborts, and CLI envelope behavior for `cancel-run`

Not done in this stage:

- no public pause / resume protocol yet
- no distributed or remote runtime backend yet
- no fully concurrency-safe persistent state store yet
- no subsystem test-package split yet
- no broad extraction of the chunk-execution algorithm itself yet

---

## 6. Current Known Gaps

The implementation is meaningfully stronger after Stage 9, but still not fully kernelized.

The main remaining gaps are:

1. runtime, state-store, and controller boundaries are extracted, but much of the chunk-execution algorithm still lives in one large script
2. telemetry is local and append-only, but not yet a first-class subsystem with querying or aggregation
3. verification remains deterministic / heuristic only and does not yet include semantic judge layers
4. global terminology / entity consistency is still not first-class
5. ownership is local single-writer gating, not a general concurrent state-store protocol
6. cancellation is public and local, but pause / resume and long-lived job scheduling are not formalized
7. test coverage is stronger around runtime control, but fixtures are not yet split into dedicated suites by subsystem

---

## 7. Staged Implementation Roadmap

Each stage should satisfy all of the following:

- code changes are coherent and runnable
- `SYSTEM_DESIGN.md` is updated to describe the new current state
- tests or validation steps are updated with the change
- planned work remains clearly separated from implemented behavior

### Stage 5 — Verification, Repair, and Replan Control Loops

Implemented scope:

- formalized verification policy surfaces
- separated repair from replan explicitly in runtime contracts
- added bounded chunk-repair and document auto-replan behavior

### Stage 6 — Interfaces, Testing, and Observability

Implemented scope:

- formalized CLI and Python API layers through a stable envelope mode
- improved regression coverage for interface-level contracts
- defined local telemetry and debugging expectations explicitly

### Stage 7 — Runtime Extraction and Ownership

Implemented scope:

- extracted envelope / telemetry / command-dispatch helpers into `kernel_runtime.py`
- formalized local work-dir ownership for manifest-mutating commands
- fixed stable envelope / telemetry fields for future extraction work

### Stage 8 — State Store Extraction and Runtime Inspection

Implemented scope:

- extracted local manifest and runtime-control file handling into `kernel_state.py`
- added read-only `runtime-status` inspection for work directories
- kept ownership and interface contracts stable while making state surfaces reusable

### Stage 9 — Controller Extraction and Public Cancellation

Implemented scope:

- extracted mutation wrappers and bounded control-loop orchestration into `kernel_controller.py`
- promoted local cancellation into the public `cancel-run` command surface
- integrated safe-boundary cancellation checks into `process-chunks`

### Stage 10 — Stronger Runtime Guarantees and Deeper Extraction

Planned scope:

- decide what stronger multi-process state guarantees are worth adding beyond the current owner-file gate
- extract more of the chunk-execution algorithm from `yt_transcript_utils.py`
- decide whether pause / resume becomes part of the public runtime contract

---

## 8. Stage 9 Validation

The Stage 9 implementation is considered valid because:

- mutating command wrappers and bounded replan orchestration now have a dedicated controller module in `kernel_controller.py`
- `cancel-run` exposes a public cancellation contract without breaking Stage 8 runtime inspection or Stage 7 ownership behavior
- `process-chunks` now aborts cleanly at safe boundaries when cancellation is requested, without touching completed outputs
- Stage 6 envelope behavior remains compatible for the new cancellation command
- local runtime-control artifacts remain inspectable on disk while becoming operationally useful

Representative validated behaviors in this stage include:

- writing `.runtime_cancel.json` through `cancel-run`
- surfacing the same cancellation marker through `runtime-status`
- consuming and clearing cancellation markers before `process-chunks` makes further LLM calls
- recording cancellation abort reasons in manifest runtime state
- emitting `yt_transcript.command_result/v1` envelopes for `cancel-run` in CLI `--api-envelope` mode

---

## 9. Next Stage Entry Criteria

Stage 10 should begin only when the following are agreed:

1. which parts of the chunk-execution algorithm should move out of `yt_transcript_utils.py` next
2. what stronger state-store guarantees are worth adding beyond `.runtime_owner.json` plus local cancellation markers
3. whether pause / resume should remain local-only or become a public runtime contract
