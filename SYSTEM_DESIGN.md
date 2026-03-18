# System Design (v5.1-stage1)

**Stage**: 1

**Status**: accepted and implemented

**Behavior change in this stage**: introduced an authoritative machine-readable state model with backward-compatible legacy state projection support

**Source of truth**: This file is the canonical system design document for the repository.

**Temporary companion document**: `LONG_TEXT_TRANSFORMATION_KERNEL.md` remains a non-authoritative staging draft. Stable content is folded into this file only when implemented or accepted for the next stage.

---

## 1. Stage 1 Goal

Stage 1 establishes the first formal kernel-oriented substrate without breaking the current workflow surface.

Its goals are:

- introduce an authoritative machine-readable state model
- preserve compatibility with the existing workflow-facing `state.md` file
- make current orchestration commands work with either state surface
- keep the rest of the system behavior unchanged

Stage 1 is intentionally narrow. It does **not** attempt to generalize the full kernel yet. It only formalizes the state boundary so later stages have a stable base.

---

## 2. Current System Identity

### 2.1 What This Repository Is Today

At the current implementation stage, this repository is a **script-first long-text processing system centered on YouTube transcript workflows**, with an emerging kernel-style core.

Its current implemented use cases are:

- transcript extraction from subtitles or Deepgram fallback
- transcript cleanup and structure restoration
- bilingual translation workflow
- chunked processing for long inputs
- deterministic merge and final markdown assembly
- workflow validation and stop/go quality checks

This is broader than a simple transcript downloader, but still narrower than a fully generalized long-text transformation framework.

### 2.2 Primary Design Goal

The current primary design goal is:

> Enable reliable long-text transcript transformation under context limits, especially on weaker models, using script-owned state, chunking, and deterministic verification.

### 2.3 Current Non-Goals

At Stage 1, the system is **not yet**:

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
  -> raw text extraction
  -> state synchronization
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
- `manifest.json` in chunk work directories
  - chunk plan + runtime execution metadata
  - used for resumability and chunk-level processing state
- `/tmp/${VIDEO_ID}_raw_text.txt`
  - raw extracted text
- `/tmp/${VIDEO_ID}_structured.txt`
  - optional intermediate structured text
- `/tmp/${VIDEO_ID}_optimized.txt`
  - transformed output before final assembly
- final markdown output under configured output directory

### 3.3 Current State Model

Stage 1 introduces a two-surface state model:

#### Authoritative surface

- `machine_state.json` is the authoritative state surface for helper commands
- it stores a structured machine-readable payload plus a compatibility projection
- it is intended as the base for later kernel extraction work

#### Compatibility surface

- `state.md` remains the workflow-facing compatibility surface
- current workflow docs can continue reading and writing it
- helper commands automatically import it into `machine_state.json`
- helper commands also accept direct `machine_state.json` input

#### Current authority rule

At Stage 1, authority is implemented as follows:

- if a legacy `state.md` is passed to a helper command, the command syncs or refreshes `machine_state.json`
- if a `machine_state.json` path is passed directly, the helper uses it as authoritative input
- `sync-state` exists for explicit manual synchronization and recovery tasks

This design keeps the current workflow surface stable while introducing a stronger internal contract.

### 3.4 Current Core Commands

The current Python utility surface includes the following architectural primitives:

- `sync-state`
- `validate-state`
- `plan-optimization`
- `chunk-text`
- `chunk-segments`
- `process-chunks`
- `merge-content`
- `assemble-final`
- `verify-quality`

These commands remain the system’s current orchestration center.

### 3.5 Current Architectural Strengths

The system already has several strong design properties:

- script-first routing for high-risk decisions
- explicit state instead of relying only on chat memory
- prompt specialization by task
- chunked long-text execution
- manifest-backed resumability for chunk runs
- deterministic merge and final file assembly
- explicit verification checkpoints before final output
- backward-compatible state evolution without breaking current workflows

---

## 4. Current Design Principles

These principles are already true in the implemented system and should remain true during later refactors.

### 4.1 Script-First Decisions

Branching and validation logic should live in scripts, not in prompt prose, whenever reliability matters.

### 4.2 Narrow Prompt Responsibilities

One prompt should have one primary job whenever possible.

### 4.3 Explicit State over Conversational Recall

The system should prefer persisted state files and structured artifacts over relying on model memory.

### 4.4 Deterministic Steps Stay out of Prompts

Tasks such as file assembly, state validation, and basic quality gating should remain deterministic.

### 4.5 Weak-Model Compatibility Matters

The system should keep working with smaller or weaker models by reducing prompt burden and maintaining explicit checkpoints.

### 4.6 Debuggability Is a First-Class Requirement

Contributors should be able to inspect intermediate artifacts, chunk state, and quality outputs without reverse-engineering hidden runtime behavior.

### 4.7 Compatibility Is Allowed When It Preserves Forward Motion

State evolution is allowed to proceed behind a compatibility layer, as long as:

- existing workflows continue to operate
- the new authoritative surface is explicit
- the migration path remains inspectable and reversible

---

## 5. Stage 1 Deliverables

Completed in this stage:

- introduced `machine_state.json` as an authoritative machine-readable state surface
- preserved `state.md` as a workflow-facing compatibility surface
- made `validate-state` work through the state bridge
- made `plan-optimization` accept both legacy markdown state and machine JSON state
- added explicit `sync-state` command for manual synchronization and recovery workflows
- updated cleanup behavior so `--keep-state` preserves both state surfaces
- added regression coverage for state materialization, direct JSON input, legacy projection rewrite, and cleanup behavior

Not done in this stage:

- no broader normalization layer yet
- no canonical multi-source document model yet
- no per-chunk authoritative state files yet
- no formal repair/replan contract yet
- no telemetry module yet

---

## 6. Current Known Gaps

The current implementation is stronger than Stage 0, but still not fully kernelized.

The main remaining gaps are:

1. input normalization is still implicit rather than a formal layer
2. source adapters are still task-shaped rather than canonicalized
3. global glossary / entity / style constraint handling is not yet first-class
4. continuity handling exists, but is still lightweight and not fully formalized
5. verification, repair, resume, and replan are not yet unified in one canonical control model
6. concurrency, testing structure, and telemetry are not yet fully specified as system-level modules

These gaps define the next implementation stages.

---

## 7. Staged Implementation Roadmap

The long-text transformation refactor proceeds in vertical stages.

Each stage should satisfy all of the following:

- code changes are coherent and runnable
- `SYSTEM_DESIGN.md` is updated to describe the new current state
- tests or validation steps are updated with the change
- planned work remains clearly separated from implemented behavior

### Stage 2 — Normalization and Source Adapter Layer

Planned scope:

- make input normalization more explicit
- define a cleaner source adapter boundary
- reduce task-specific assumptions in the core flow

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

## 8. Stage 1 Validation

The Stage 1 implementation is considered valid because:

- existing state-based workflows continue to work with legacy markdown input
- helper commands can now materialize and read an authoritative machine state
- cleanup semantics remain explicit and debuggable
- regression coverage now exercises both state surfaces

Representative validated behaviors in this stage include:

- validating legacy state and auto-materializing machine state
- planning directly from machine JSON state
- rewriting a legacy projection from machine state
- preserving both state surfaces under `cleanup.sh --keep-state`

---

## 9. Next Stage Entry Criteria

Stage 2 should begin only when the following are agreed:

1. what the minimal canonical normalized document shape should be
2. which current source-specific assumptions should remain in adapters versus move into normalization
3. which current commands should continue operating on state only versus begin consuming normalized document artifacts

