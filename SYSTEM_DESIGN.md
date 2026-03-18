# System Design (v5.6-stage6)

**Stage**: 6

**Status**: accepted and implemented

**Behavior change in this stage**: formalized a stable kernel command envelope for Python and CLI use, added local telemetry journals, and documented a compatibility-preserving public interface layer on top of the Stage 5 control model

**Source of truth**: This file is the canonical system design document for the repository.

**Non-authoritative companion**: `LONG_TEXT_TRANSFORMATION_KERNEL.md` remains a staging draft for future kernelization ideas and does not override this document.

---

## 1. Stage 6 Goal

Stage 6 hardens the interface and observability side of the long-text kernel.

Its goals are:

- define a stable Python command API without breaking existing flat function returns
- define a stable CLI envelope mode without breaking existing flat JSON output
- persist lightweight command telemetry in a local journal for debugging and post-run inspection
- turn interface-level regression checks into part of the kernel contract

Stage 6 remains intentionally bounded. It does **not** extract a separate telemetry module, split tests into multiple packages, or solve concurrency-safe task ownership.

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
- deterministic merge and final markdown assembly
- workflow validation and stop/go quality checks

### 2.2 Current Primary Design Goal

> Enable reliable long-text transcript transformation under context limits, especially on weaker models, using script-owned state, explicit normalization, explicit chunk contracts, bounded continuity, explicit lifecycle semantics, explicit control contracts, deterministic verification, and compatibility-preserving command interfaces.

### 2.3 Current Non-Goals

At Stage 6, the system is **not yet**:

- a generalized multi-source document transformation framework
- a reusable extracted kernel package
- a global glossary / terminology propagation system
- an LLM-judge semantic verification system
- a concurrency-safe persistent state store
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
  -> chunking
  -> resume preflight / manifest repair
  -> per-chunk prompt execution
  -> chunk output verification
  -> bounded same-plan repair or document replan decision
  -> deterministic merge
  -> deterministic final assembly
  -> quality verification
  -> local telemetry journal append
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
  - manifest schema remains `v5` in Stage 6
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

### 3.11 Current Planning, Interface, and Control Surfaces

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

Stage 6 also defines a compatibility-preserving interface layer:

- direct Python functions still return the existing flat dictionaries
- `run_kernel_command(...)` is now the stable Python envelope API for kernel commands
- `python3 yt_transcript_utils.py --api-envelope ...` emits the same stable envelope for kernel commands on the CLI

The stable envelope currently uses:

- `format = yt_transcript.command_result/v1`
- `schema_version = 1`
- `command`
- `trace_id`
- `generated_at`
- `ok`
- `telemetry`
- `result`

The local telemetry journal currently uses:

- `format = yt_transcript.telemetry_event/v1`
- `event_type = command_result`
- `trace_id`
- `command`
- `timestamp`
- `duration_ms`
- `success`
- `warning_count`
- inferred local sink path in `telemetry.jsonl`

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

## 5. Stage 6 Deliverables

Completed in this stage:

- introduced `run_kernel_command(...)` as the stable Python envelope API for kernel commands
- introduced CLI `--api-envelope` output mode for the same kernel command surface
- formalized `yt_transcript.command_result/v1` as the current command envelope format
- formalized `yt_transcript.telemetry_event/v1` as the current local telemetry event format
- appended kernel command telemetry to local `telemetry.jsonl` sinks when a stable path can be inferred
- kept legacy flat Python and default CLI JSON outputs compatible
- added interface-level regression coverage for envelope behavior and telemetry persistence

Not done in this stage:

- no remote telemetry backend or distributed tracing yet
- no extracted telemetry / interface package yet
- no test directory split by subsystem yet
- no concurrency-safe task ownership or cancellation protocol yet

---

## 6. Current Known Gaps

The implementation is meaningfully stronger after Stage 6, but still not fully kernelized.

The main remaining gaps are:

1. the stable interface layer exists, but it still wraps one large script instead of dedicated packages
2. telemetry is local and append-only, but not yet a first-class subsystem with querying or aggregation
3. verification remains deterministic / heuristic only and does not yet include semantic judge layers
4. global terminology / entity consistency is still not first-class
5. runtime control is resumable, but not yet concurrency-safe
6. cancellation, pause, and long-lived task ownership are not formalized
7. test coverage is stronger at the interface level, but fixtures are not yet split into dedicated suites by subsystem

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

### Stage 7 — Module Extraction and Runtime Ownership

Planned scope:

- extract interface / telemetry / controller code into dedicated modules
- formalize task ownership, cancellation, and concurrency-safe state handling
- decide which envelope and telemetry fields are now long-term stable

---

## 8. Stage 6 Validation

The Stage 6 implementation is considered valid because:

- kernel commands now have one stable envelope surface for both Python and CLI consumers
- the default flat outputs remain compatible for existing workflows
- telemetry is persisted locally without entangling prompt logic or remote services
- interface-level regressions now verify both the envelope surface and telemetry side effects
- Stage 5 control semantics remain preserved underneath the new interface layer

Representative validated behaviors in this stage include:

- returning `yt_transcript.command_result/v1` envelopes from `run_kernel_command(...)`
- appending `yt_transcript.telemetry_event/v1` entries into inferred local `telemetry.jsonl` sinks
- emitting the same envelope format from CLI kernel commands when `--api-envelope` is used
- preserving legacy flat JSON output when `--api-envelope` is not used
- keeping Stage 5 repair / replan behavior intact behind the new interface layer

---

## 9. Next Stage Entry Criteria

Stage 7 should begin only when the following are agreed:

1. which envelope and telemetry fields are now stable enough to preserve across extraction
2. how controller, interface, and telemetry responsibilities should split into modules
3. what concurrency, ownership, and cancellation guarantees the extracted runtime must enforce
