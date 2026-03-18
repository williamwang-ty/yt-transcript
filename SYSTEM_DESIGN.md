# System Design (v5.2-stage2)

**Stage**: 2

**Status**: accepted and implemented

**Behavior change in this stage**: introduced an explicit normalization layer and a minimal source-adapter boundary, with `normalized_document.json` materialized from raw text or timed segments

**Source of truth**: This file is the canonical system design document for the repository.

**Temporary companion document**: `LONG_TEXT_TRANSFORMATION_KERNEL.md` remains a non-authoritative staging draft. Stable content is folded into this file only when implemented or accepted for the next stage.

---

## 1. Stage 2 Goal

Stage 2 makes source normalization explicit without attempting a full kernel extraction.

Its goals are:

- introduce a first-class normalized document artifact
- make current source adaptation visible at the helper-command boundary
- reduce hidden assumptions between source extraction and planning
- keep current transcript workflows backward-compatible

Stage 2 is still intentionally small. It does **not** yet introduce a fully generalized multi-source document model, but it makes the current normalization path explicit enough for later chunking and control-loop work.

---

## 2. Current System Identity

### 2.1 What This Repository Is Today

At the current implementation stage, this repository is a **script-first long-text processing system centered on YouTube transcript workflows**, with an emerging kernel-style core.

Its current implemented use cases are:

- transcript extraction from subtitles or Deepgram fallback
- transcript cleanup and structure restoration
- bilingual translation workflow
- explicit normalization from raw text or timed segments
- chunked processing for long inputs
- deterministic merge and final markdown assembly
- workflow validation and stop/go quality checks

This is broader than a simple transcript downloader, but still narrower than a fully generalized long-text transformation framework.

### 2.2 Primary Design Goal

The current primary design goal is:

> Enable reliable long-text transcript transformation under context limits, especially on weaker models, using script-owned state, explicit normalization, chunking, and deterministic verification.

### 2.3 Current Non-Goals

At Stage 2, the system is **not yet**:

- a generalized multi-source document transformation framework
- a formalized reusable kernel package layout
- a concurrent chunk execution runtime with per-chunk authoritative state stores
- a fully specified repair/replan engine
- a complete observability platform

These remain future stages, not current contract.

---

## 3. Current Implemented Architecture

### 3.1 Current Execution Shape

The current system behaves approximately like this:

```text
source acquisition
  -> source selection (subtitles vs Deepgram)
  -> raw text and/or timed segments extraction
  -> state synchronization
  -> normalization
  -> optimization planning
  -> chunking for long inputs
  -> per-chunk prompt execution
  -> deterministic merge
  -> deterministic final assembly
  -> quality verification
```

This flow is implemented through shell scripts, workflow documents, prompts, and the Python utility layer.

### 3.2 Current Persisted Artifacts

The current implementation relies on these persisted artifacts:

- `/tmp/${VIDEO_ID}_machine_state.json`
  - authoritative machine-readable state surface introduced in Stage 1
  - materialized automatically from legacy state when needed
- `/tmp/${VIDEO_ID}_state.md`
  - workflow-facing compatibility projection
  - still accepted by current workflow docs and current operator habits
- `/tmp/${VIDEO_ID}_normalized_document.json`
  - explicit normalized document artifact introduced in Stage 2
  - materialized from raw text or timed segments
- `manifest.json` in chunk work directories
  - chunk plan + runtime execution metadata
  - used for resumability and chunk-level processing state
- `/tmp/${VIDEO_ID}_raw_text.txt`
  - raw extracted text
- `/tmp/${VIDEO_ID}_segments.json`
  - optional timed source segments from subtitles or Deepgram
- `/tmp/${VIDEO_ID}_structured.txt`
  - optional intermediate structured text
- `/tmp/${VIDEO_ID}_optimized.txt`
  - transformed output before final assembly
- final markdown output under configured output directory

### 3.3 Current State Model

The current system uses a two-surface state model:

#### Authoritative surface

- `machine_state.json` is the authoritative state surface for helper commands
- it stores machine-readable state plus a compatibility projection
- it also records normalization metadata and the normalized artifact path when normalization is materialized

#### Compatibility surface

- `state.md` remains the workflow-facing compatibility surface
- current workflow docs can continue reading and writing it
- helper commands automatically import it into `machine_state.json`
- helper commands also accept direct `machine_state.json` input

#### Current authority rule

At Stage 2, authority works as follows:

- if a legacy `state.md` is passed to a helper command, the command syncs or refreshes `machine_state.json`
- if a `machine_state.json` path is passed directly, the helper uses it as authoritative input
- `sync-state` exists for explicit manual synchronization and recovery tasks
- normalization writes its artifact path and summary back into `machine_state.json`

### 3.4 Current Normalization Layer

Stage 2 introduces an explicit normalization artifact and a minimal source-adapter boundary.

The current supported source adapters are:

- `raw_text_file`
  - reads a transcript-like raw text artifact
  - normalizes line endings, trailing whitespace, and blank-line density
- `segments_json`
  - reads a timed segments payload
  - normalizes segment text and emits a segment-aware normalized document

The normalization layer currently produces a `normalized_document.json` with:

- source metadata
- workflow mode metadata
- artifact references
- normalized text content
- optional normalized segment list
- a preferred chunk source hint (`text` vs `segments`)

### 3.5 Current Planning Behavior

`plan-optimization` now attempts to materialize normalization automatically when source artifacts are available.

This means:

- planning still works when only legacy state is provided
- if raw text or segments are already present, a normalized document artifact is generated during planning
- if normalization artifacts are not yet available, planning continues without failure and reports normalization as not materialized

This keeps current workflows working while making the normalization layer real instead of purely conceptual.

### 3.6 Current Core Commands

The current Python utility surface includes the following architectural primitives:

- `sync-state`
- `normalize-document`
- `validate-state`
- `plan-optimization`
- `chunk-text`
- `chunk-segments`
- `process-chunks`
- `merge-content`
- `assemble-final`
- `verify-quality`

These commands remain the system’s current orchestration center.

### 3.7 Current Architectural Strengths

The system already has several strong design properties:

- script-first routing for high-risk decisions
- explicit state instead of relying only on chat memory
- backward-compatible state evolution
- explicit normalization instead of hidden raw-source assumptions
- prompt specialization by task
- chunked long-text execution
- manifest-backed resumability for chunk runs
- deterministic merge and final file assembly
- explicit verification checkpoints before final output

---

## 4. Current Design Principles

These principles are already true in the implemented system and should remain true during later refactors.

### 4.1 Script-First Decisions

Branching and validation logic should live in scripts, not in prompt prose, whenever reliability matters.

### 4.2 Narrow Prompt Responsibilities

One prompt should have one primary job whenever possible.

### 4.3 Explicit State over Conversational Recall

The system should prefer persisted state files and structured artifacts over relying on model memory.

### 4.4 Explicit Normalization over Implicit Source Assumptions

The system should prefer a named normalized artifact over having downstream logic infer source shape indirectly from raw files.

### 4.5 Deterministic Steps Stay out of Prompts

Tasks such as state validation, normalization, file assembly, and basic quality gating should remain deterministic.

### 4.6 Weak-Model Compatibility Matters

The system should keep working with smaller or weaker models by reducing prompt burden and maintaining explicit checkpoints.

### 4.7 Debuggability Is a First-Class Requirement

Contributors should be able to inspect intermediate artifacts, chunk state, and quality outputs without reverse-engineering hidden runtime behavior.

### 4.8 Compatibility Is Allowed When It Preserves Forward Motion

State and artifact evolution are allowed to proceed behind compatibility layers, as long as:

- existing workflows continue to operate
- the new authoritative or canonical surface is explicit
- the migration path remains inspectable and reversible

---

## 5. Stage 2 Deliverables

Completed in this stage:

- introduced `normalized_document.json` as an explicit normalized artifact
- introduced a minimal source-adapter boundary for `raw_text_file` and `segments_json`
- added `normalize-document` command for explicit normalization
- made `plan-optimization` auto-materialize normalization when source artifacts are available
- extended machine state to retain normalization metadata and normalized artifact references
- preserved Stage 1 backward compatibility with legacy `state.md` and direct machine-state input
- added regression coverage for raw-text normalization, segments-based normalization, and planning-driven normalization materialization

Not done in this stage:

- no generalized document schema across many source domains yet
- no dedicated kernel package layout yet
- no formal chunk contract or continuity model yet
- no formal repair/replan contract yet
- no telemetry module yet

---

## 6. Current Known Gaps

The current implementation is stronger than Stage 1, but still not fully kernelized.

The main remaining gaps are:

1. chunk contracts and continuity metadata are still implicit or lightweight
2. normalization exists, but the broader canonical document model is still transcript-shaped
3. global glossary / entity / style constraint handling is not yet first-class
4. verification, repair, resume, and replan are not yet unified in one canonical control model
5. concurrency, testing structure, and telemetry are not yet fully specified as system-level modules
6. module boundaries are still mostly logical rather than extracted into a dedicated kernel package structure

These gaps define the next implementation stages.

---

## 7. Staged Implementation Roadmap

The long-text transformation refactor proceeds in vertical stages.

Each stage should satisfy all of the following:

- code changes are coherent and runnable
- `SYSTEM_DESIGN.md` is updated to describe the new current state
- tests or validation steps are updated with the change
- planned work remains clearly separated from implemented behavior

### Stage 3 — Chunking and Continuity Formalization

Planned scope:

- formalize chunk contracts
- improve continuity metadata
- tighten chunk boundaries and merge assumptions

### Stage 4 — Transform Runner and Resume Semantics

Planned scope:

- harden resumability
- improve chunk lifecycle semantics
- prepare for safer concurrency or more explicit execution control

### Stage 5 — Verification, Repair, and Replan Control Loops

Planned scope:

- formalize verification policy
- separate repair from replan
- add bounded control-loop behavior

### Stage 6 — Interfaces, Testing, and Observability

Planned scope:

- formalize CLI and Python API layers
- improve tests and fixtures
- define telemetry and debugging expectations

---

## 8. Stage 2 Validation

The Stage 2 implementation is considered valid because:

- normalization is now a real artifact rather than a conceptual step
- planning can materialize normalization automatically when source artifacts already exist
- both raw-text and timed-segment source shapes are now handled explicitly at the helper boundary
- current workflows remain compatible with Stage 1 state surfaces
- regression coverage now exercises both normalization paths

Representative validated behaviors in this stage include:

- materializing normalized documents from raw text artifacts
- preferring timed segments when both segments and raw text exist
- recording normalization metadata back into machine state
- materializing normalized documents during `plan-optimization`

---

## 9. Next Stage Entry Criteria

Stage 3 should begin only when the following are agreed:

1. what the minimum explicit chunk contract should be
2. what continuity metadata belongs to chunk planning versus chunk execution
3. whether chunk boundary strictness should become a hard contract or remain a best-effort behavior in this repository

