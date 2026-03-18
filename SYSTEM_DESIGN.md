# System Design (v5.3-stage3)

**Stage**: 3

**Status**: accepted and implemented

**Behavior change in this stage**: formalized chunk contracts and continuity policy, introduced `chunk-document` as the canonical normalized-document chunker, and made planning expose an explicit chunking contract

**Source of truth**: This file is the canonical system design document for the repository.

**Non-authoritative companion**: `LONG_TEXT_TRANSFORMATION_KERNEL.md` remains a staging draft for future kernelization ideas and does not override this document.

---

## 1. Stage 3 Goal

Stage 3 formalizes the boundary between normalization and long-text execution.

Its goals are:

- turn chunking assumptions into explicit manifest contract data
- make continuity policy explicit and plan-owned instead of purely config-implied
- add a canonical chunker that consumes `normalized_document.json`
- preserve existing `chunk-text` and `chunk-segments` workflows as compatible lower-level drivers

Stage 3 is still intentionally bounded. It does **not** yet redesign the whole transform runner or verification / repair loop.

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
- chunked processing for long inputs
- deterministic merge and final markdown assembly
- workflow validation and stop/go quality checks

### 2.2 Current Primary Design Goal

> Enable reliable long-text transcript transformation under context limits, especially on weaker models, using script-owned state, explicit normalization, explicit chunk contracts, bounded continuity, and deterministic verification.

### 2.3 Current Non-Goals

At Stage 3, the system is **not yet**:

- a generalized multi-source document transformation framework
- a formal reusable kernel package layout
- a global glossary / terminology propagation system
- a unified verification / repair / replan kernel
- a concurrency-optimized state store
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
  -> per-chunk prompt execution
  -> deterministic merge
  -> deterministic final assembly
  -> quality verification
```

This flow is implemented through shell scripts, workflow documents, prompt templates, and the Python utility layer.

### 3.2 Current Persisted Artifacts

The current implementation relies on these persisted artifacts:

- `/tmp/${VIDEO_ID}_machine_state.json`
  - authoritative machine-readable state surface introduced in Stage 1
- `/tmp/${VIDEO_ID}_state.md`
  - workflow-facing compatibility projection
- `/tmp/${VIDEO_ID}_normalized_document.json`
  - normalized source artifact introduced in Stage 2
- `manifest.json` in chunk work directories
  - chunk plan, chunk contract, continuity policy, runtime state, and autotune / replan metadata
  - Stage 3 manifest schema is now `v3`
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

The current system still uses a two-surface state model:

#### Authoritative surface

- `machine_state.json` is authoritative for helper commands
- it stores machine-readable source, artifact, workflow, and normalization metadata
- normalization writes normalized artifact references back into this state

#### Compatibility surface

- `state.md` remains the workflow-facing compatibility surface
- helper commands still accept it directly and auto-sync `machine_state.json`

#### Authority rule

At Stage 3, authority works as follows:

- legacy `state.md` input is synced into `machine_state.json`
- direct `machine_state.json` input is treated as authoritative
- planning and normalization read through that bridge
- chunking operates on downstream artifacts, not on chat-memory assumptions

### 3.4 Current Normalization Layer

Stage 2 introduced an explicit normalization layer, and Stage 3 now treats it as the canonical chunking input whenever available.

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

Stage 3 formalizes chunking around three drivers:

- `chunk-text`
  - low-level text chunker
  - still supported for direct raw-text chunking
- `chunk-segments`
  - low-level timed chunker
  - still supported for direct timed-segment chunking
- `chunk-document`
  - new Stage 3 canonical chunker
  - consumes `normalized_document.json`
  - auto-selects `segments` vs `text` from normalized content, unless overridden with `--prefer`

#### Canonical rule

When a normalized document exists, `chunk-document` is the preferred chunking entrypoint.

The lower-level chunkers remain valid compatibility tools and internal building blocks.

#### Current chunk contract

Stage 3 adds an explicit `plan.chunk_contract` section to `manifest.json`.

Its current contract expresses:

- `driver`
- `source_kind` (`text` or `segments`)
- `boundary_mode = strict`
- `output_scope = current_chunk_only`
- `continuity_mode = reference_only`
- `merge_strategy = ordered_concat`
- `overlap_strategy = context_only_no_output_overlap`
- optional normalized-document reference and source-adapter metadata

This formalizes the current merge assumption:

- chunk outputs should correspond only to the chunk body being transformed
- continuity is reference-only, not part of emitted output
- merge remains deterministic ordered concatenation, not fuzzy overlap deduplication

### 3.6 Current Continuity Model

Stage 3 also formalizes continuity as `plan.continuity` in `manifest.json`.

The current policy expresses:

- `mode = reference_only`
- `tail_sentences`
- `summary_token_cap`
- `carry_section_title`
- `carry_tail_text`
- `boundary_rule`
- `output_rule`

#### Important behavioral rule

`process-chunks` now consumes continuity policy from the manifest plan, not only from current runtime config.

That means:

- chunk planning decides continuity policy once
- execution follows that recorded policy
- later config drift does not silently change continuity semantics for an existing plan

This is an explicit Stage 3 hardening step.

### 3.7 Current Planning Behavior

`plan-optimization` now does three relevant things:

- validates workflow state
- auto-materializes normalization when source artifacts exist
- emits an explicit `chunking` block

The current `chunking` block reports:

- canonical chunk driver (`chunk-document` when normalization is materialized)
- preferred source kind for chunking
- boundary mode
- continuity mode
- merge strategy

This makes chunking behavior visible at the planning boundary instead of leaving it implicit in workflow prose.

### 3.8 Current Core Commands

The current Python utility surface includes the following architectural primitives:

- `sync-state`
- `normalize-document`
- `validate-state`
- `plan-optimization`
- `chunk-document`
- `chunk-text`
- `chunk-segments`
- `process-chunks`
- `replan-remaining`
- `merge-content`
- `assemble-final`
- `verify-quality`

### 3.9 Current Architectural Strengths

The system now has the following concrete strengths:

- script-first routing for high-risk decisions
- explicit state instead of relying on chat memory
- backward-compatible state evolution
- explicit normalization instead of hidden source assumptions
- explicit chunk contract instead of implied merge behavior
- plan-owned continuity instead of runtime drift
- canonical normalized-document chunking
- manifest-backed resumability for chunk runs
- deterministic merge and final file assembly
- explicit verification checkpoints before final output

---

## 4. Current Design Principles

These principles are already true in the implemented system and should remain true during later refactors.

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

### 4.6 Deterministic Steps Stay out of Prompts

State validation, normalization, chunk-plan metadata, and final assembly should stay deterministic.

### 4.7 Weak-Model Compatibility Matters

The system should continue to work with smaller or weaker models by keeping prompt responsibilities narrow and chunk contracts explicit.

### 4.8 Debuggability Is a First-Class Requirement

Intermediate artifacts, manifest plans, chunk contracts, and continuity settings should all remain inspectable on disk.

---

## 5. Stage 3 Deliverables

Completed in this stage:

- introduced `chunk-document` as the canonical normalized-document chunker
- formalized `plan.chunk_contract` in `manifest.json`
- formalized `plan.continuity` in `manifest.json`
- made chunk manifests record strict boundary / reference-only continuity assumptions explicitly
- made `process-chunks` consume continuity policy from the manifest plan
- made `plan-optimization` emit a `chunking` contract block
- preserved compatibility with direct `chunk-text` and `chunk-segments` workflows
- added regression coverage for normalized-document chunking, planning contract output, and plan-owned continuity behavior

Not done in this stage:

- no unified chunk lifecycle state machine beyond the current manifest/runtime model
- no generalized global glossary extraction pass
- no formal verification / repair / replan policy module
- no concurrency-safe state store redesign yet
- no dedicated telemetry module yet

---

## 6. Current Known Gaps

The implementation is meaningfully stronger after Stage 3, but still not fully kernelized.

The main remaining gaps are:

1. transform runner lifecycle and resume semantics are still embedded in the current manifest/runtime logic
2. global terminology / entity consistency is still not first-class
3. verification, repair, and replan are still not unified as one explicit control model
4. concurrency, state-store strategy, and cancellation are not yet formal system modules
5. telemetry and testing structure are stronger, but not yet extracted into dedicated subsystems
6. module boundaries are still logical inside one script, not yet split into a kernel package layout

---

## 7. Staged Implementation Roadmap

The long-text transformation refactor proceeds in vertical stages.

Each stage should satisfy all of the following:

- code changes are coherent and runnable
- `SYSTEM_DESIGN.md` is updated to describe the new current state
- tests or validation steps are updated with the change
- planned work remains clearly separated from implemented behavior

### Stage 4 — Transform Runner and Resume Semantics

Planned scope:

- harden resumability and partial-run recovery
- clarify chunk lifecycle states and checkpoint rules
- prepare for safer concurrency or more explicit execution control

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

## 8. Stage 3 Validation

The Stage 3 implementation is considered valid because:

- chunking is now routed through an explicit canonical driver when normalization exists
- manifest plans now record chunk and continuity contracts explicitly
- execution follows manifest continuity policy rather than silently drifting with runtime config
- direct `chunk-text` and `chunk-segments` compatibility is preserved
- planning now exposes chunking assumptions explicitly

Representative validated behaviors in this stage include:

- chunking normalized documents through `chunk-document`
- preferring segment-aware chunking when normalized segments are available
- forcing text-mode chunking from the same normalized document when requested
- recording strict boundary and reference-only continuity in manifest plans
- exposing chunking contract data from `plan-optimization`
- keeping continuity behavior stable even when runtime config disables it after planning

---

## 9. Next Stage Entry Criteria

Stage 4 should begin only when the following are agreed:

1. what the minimum explicit chunk lifecycle state machine should be
2. what counts as a resumable checkpoint versus a recoverable partial write
3. whether resumability remains file-based only or gains a more explicit state-store abstraction
