# System Design (v5.0-stage0)

**Stage**: 0

**Status**: accepted and implemented

**Behavior change in this stage**: none

**Source of truth**: This file is the canonical system design document for the repository from this stage forward.

**Temporary companion document**: `LONG_TEXT_TRANSFORMATION_KERNEL.md` is now a staging draft only. It may contain useful target-state thinking, but it is not authoritative. Stable content will be folded into this file stage by stage.

---

## 1. Stage 0 Goal

Stage 0 is a **documentation canonicalization stage**.

Its purpose is to establish a durable rule for all later implementation stages:

- each implementation stage updates `SYSTEM_DESIGN.md`
- each stage leaves the code and design documentation in a mutually consistent state
- `SYSTEM_DESIGN.md` describes the **current implemented system first**, not the eventual end state
- future work is documented as staged roadmap items, not as already-true architecture

Stage 0 intentionally makes **no behavior changes** to the running system.

---

## 2. Current System Identity

### 2.1 What This Repository Is Today

At the current implementation stage, this repository is a **script-first long-text processing system centered on YouTube transcript workflows**.

Its current implemented use cases are:

- transcript extraction from subtitles or Deepgram fallback
- transcript cleanup and structure restoration
- bilingual translation workflow
- chunked processing for long inputs
- deterministic merge and final markdown assembly
- workflow validation and stop/go quality checks

This is broader than a simple transcript downloader, but narrower than a fully generalized long-text transformation kernel.

### 2.2 Primary Design Goal

The current primary design goal is:

> Enable reliable long-text transcript transformation under context limits, especially on weaker models, using script-owned state, chunking, and deterministic verification.

### 2.3 Current Non-Goals

At Stage 0, the system is **not yet**:

- a generalized multi-source document transformation framework
- a formalized reusable kernel package
- a concurrent chunk execution runtime with a first-class state store
- a fully specified repair/replan engine
- a complete observability platform

These may become future stages, but they are not part of the current implemented contract.

---

## 3. Current Implemented Architecture

### 3.1 Current Execution Shape

The current system behaves approximately like this:

```text
source acquisition
  -> source selection (subtitles vs Deepgram)
  -> raw text extraction
  -> optimization planning
  -> chunking for long inputs
  -> per-chunk prompt execution
  -> deterministic merge
  -> deterministic final assembly
  -> quality verification
```

This flow is implemented through shell scripts, workflow documents, prompts, and the Python utility layer.

### 3.2 Current Persisted Artifacts

The current implementation relies on the following persisted artifacts:

- `/tmp/${VIDEO_ID}_state.md`
  - workflow-facing state file
  - currently the main explicit state surface used by workflow docs and helpers
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

### 3.3 Current Core Commands

The current Python utility surface includes important orchestration primitives such as:

- `validate-state`
- `plan-optimization`
- `chunk-text`
- `chunk-segments`
- `process-chunks`
- `merge-content`
- `assemble-final`
- `verify-quality`

These commands are already the architectural core of the system.

### 3.4 Current Architectural Strengths

The system already has several strong design properties:

- script-first routing for high-risk decisions
- explicit state instead of relying only on chat memory
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

### 4.4 Deterministic Steps Stay out of Prompts

Tasks such as file assembly, state validation, and basic quality gating should remain deterministic.

### 4.5 Weak-Model Compatibility Matters

The system should keep working with smaller or weaker models by reducing prompt burden and maintaining explicit checkpoints.

### 4.6 Debuggability Is a First-Class Requirement

Contributors should be able to inspect intermediate artifacts, chunk state, and quality outputs without reverse-engineering hidden runtime behavior.

---

## 5. Current Known Gaps

The current implementation is effective, but not yet fully formalized.

The main gaps at Stage 0 are:

1. the authoritative state model is still workflow-oriented rather than kernel-oriented
2. current state and target state are not yet unified in one implemented contract
3. global glossary / entity / style constraint handling is not yet first-class
4. continuity handling exists, but is still lightweight and not fully formalized
5. verification, repair, resume, and replan are not yet described in one canonical control model
6. concurrency, testing, and telemetry are real concerns but not yet fully specified in the main system design

These gaps define the next implementation stages.

---

## 6. Staged Implementation Roadmap

The long-text transformation refactor will proceed in vertical stages.

Each stage should be a separate commit and should satisfy all of the following:

- code changes are coherent and runnable
- `SYSTEM_DESIGN.md` is updated to describe the new current state
- tests or validation steps are updated with the change
- planned work remains clearly separated from implemented behavior

### Stage 1 — Authoritative State Model and Compatibility Layer

Planned scope:

- introduce a more formal authoritative machine-readable state model
- preserve compatibility with current workflow-facing state usage
- clarify which state surface is authoritative

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

## 7. Stage 0 Deliverables

Completed in this stage:

- `SYSTEM_DESIGN.md` is now the canonical system design document
- the design doc now describes the current implemented system first
- future architecture is represented as staged roadmap items instead of assumed truth
- the repository now has a clear “one stage, one coherent design/code state” rule

Not done in this stage:

- no code behavior changes
- no state-model changes
- no API changes
- no chunking or verification logic changes

---

## 8. Next Stage Entry Criteria

Stage 1 should begin only when the following are agreed:

1. what the authoritative state file format should be
2. how compatibility with current workflow-facing state will be preserved
3. which minimum tests or validation checks must land with that state-model change

