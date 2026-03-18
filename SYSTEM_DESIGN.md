# System Design (v5.4-stage4)

**Stage**: 4

**Status**: accepted and implemented

**Behavior change in this stage**: formalized transform-runner lifecycle and resume semantics, added `prepare-resume`, and made `process-chunks` auto-repair stale chunk/runtime state before resuming execution

**Source of truth**: This file is the canonical system design document for the repository.

**Non-authoritative companion**: `LONG_TEXT_TRANSFORMATION_KERNEL.md` remains a staging draft for future kernelization ideas and does not override this document.

---

## 1. Stage 4 Goal

Stage 4 hardens the execution side of the long-text kernel.

Its goals are:

- make chunk lifecycle states explicit enough to support safe resume
- define concrete checkpoint rules for chunk outputs versus manifest state
- add a manual recovery command for stale manifests
- make normal chunk execution automatically recover resumable state before work continues

Stage 4 remains intentionally bounded. It does **not** yet formalize verification / repair / replan as one unified control system.

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
- deterministic merge and final markdown assembly
- workflow validation and stop/go quality checks

### 2.2 Current Primary Design Goal

> Enable reliable long-text transcript transformation under context limits, especially on weaker models, using script-owned state, explicit normalization, explicit chunk contracts, bounded continuity, explicit lifecycle semantics, and deterministic verification.

### 2.3 Current Non-Goals

At Stage 4, the system is **not yet**:

- a generalized multi-source document transformation framework
- a reusable extracted kernel package
- a global glossary / terminology propagation system
- a formal verification / repair / replan policy engine
- a concurrency-safe persistent state store
- a telemetry-first production runtime

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
  -> chunking
  -> resume preflight / manifest repair
  -> per-chunk prompt execution
  -> deterministic merge
  -> deterministic final assembly
  -> quality verification
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
  - chunk plan, chunk contract, continuity policy, runtime state, and resume / autotune / replan metadata
  - Stage 4 manifest schema is now `v4`
- `/tmp/${VIDEO_ID}_raw_text.txt`
  - raw extracted transcript-like text
- `/tmp/${VIDEO_ID}_segments.json`
  - optional timed source segments from subtitles or Deepgram
- `/tmp/${VIDEO_ID}_structured.txt`
  - optional intermediate structured text
- `/tmp/${VIDEO_ID}_optimized.txt`
  - transformed output before final assembly
- final markdown output under configured output directory

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

### 3.11 Current Planning Behavior

`plan-optimization` still:

- validates workflow state
- auto-materializes normalization when source artifacts exist
- emits an explicit `chunking` block

The current `chunking` block reports:

- canonical chunk driver
- preferred source kind
- boundary mode
- continuity mode
- merge strategy

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

State validation, normalization, lifecycle repair, and final assembly should stay deterministic.

### 4.8 Weak-Model Compatibility Matters

The system should continue to work with smaller or weaker models by keeping prompt responsibilities narrow and runtime control explicit.

### 4.9 Debuggability Is a First-Class Requirement

Intermediate artifacts, manifest plans, lifecycle state, and resume repairs should all remain inspectable on disk.

---

## 5. Stage 4 Deliverables

Completed in this stage:

- formalized chunk lifecycle states enough for safe resume behavior
- formalized current checkpoint rules around chunk outputs and manifest state
- introduced `prepare-resume` for explicit manifest repair
- made `process-chunks` auto-repair stale chunk/runtime state before execution
- extended manifest runtime metadata with resume-oriented fields
- preserved Stage 3 chunking / continuity behavior
- added regression coverage for interrupted, promoted, and demoted checkpoint recovery flows

Not done in this stage:

- no concurrency-safe shared state store yet
- no cancellation / pause API yet
- no formal verification / repair / replan policy engine yet
- no global glossary extraction pass yet
- no dedicated telemetry module yet

---

## 6. Current Known Gaps

The implementation is meaningfully stronger after Stage 4, but still not fully kernelized.

The main remaining gaps are:

1. verification, repair, and replan are still not unified as one explicit control model
2. global terminology / entity consistency is still not first-class
3. runtime control is resumable, but not yet concurrency-safe
4. cancellation, pause, and long-lived task ownership are not formalized
5. telemetry and testing structure are stronger, but not yet extracted into dedicated subsystems
6. module boundaries are still logical inside one script, not yet split into a kernel package layout

---

## 7. Staged Implementation Roadmap

Each stage should satisfy all of the following:

- code changes are coherent and runnable
- `SYSTEM_DESIGN.md` is updated to describe the new current state
- tests or validation steps are updated with the change
- planned work remains clearly separated from implemented behavior

### Stage 5 — Verification, Repair, and Replan Control Loops

Planned scope:

- formalize verification policy
- separate repair from replan more explicitly
- add bounded control-loop behavior and failure contracts

### Stage 6 — Interfaces, Testing, and Observability

Planned scope:

- formalize CLI and Python API layers
- improve fixture and regression structure
- define telemetry and debugging expectations more explicitly

---

## 8. Stage 4 Validation

The Stage 4 implementation is considered valid because:

- stale runner state is now reconciled against on-disk checkpoints before execution resumes
- chunk lifecycle semantics are explicit enough to support interrupted resumptions safely
- `prepare-resume` provides a manual inspection / repair entrypoint
- `process-chunks` automatically repairs resumable state before continuing work
- Stage 3 chunking and continuity guarantees remain preserved

Representative validated behaviors in this stage include:

- marking stale `running` chunks as `interrupted` when no output checkpoint exists
- promoting stale `running` chunks to `done` when an output checkpoint already exists
- demoting inconsistent `done` chunks back to `interrupted` when outputs are missing
- auto-repairing resume state at `process-chunks` startup and skipping already recovered completed chunks

---

## 9. Next Stage Entry Criteria

Stage 5 should begin only when the following are agreed:

1. which verification checks are deterministic hard gates versus heuristic advisory checks
2. what bounded repair loop policy should exist per chunk and per document
3. how replan triggers should differ from repair triggers in the canonical contract
