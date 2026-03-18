# Long-Text Transformation Kernel Design (staging draft)

**Status**: non-authoritative staging draft

**Authority**: `SYSTEM_DESIGN.md` is the canonical system design document. This file is a temporary target-state scratchpad whose stable content will be folded into `SYSTEM_DESIGN.md` in later implementation stages.

## 1. Purpose

This document defines a more implementation-ready design for refactoring the current `yt-transcript` long-text pipeline into a reusable **Long-Text Transformation Kernel**.

The kernel is intended to support document-scale tasks such as:

- transcript cleanup
- structure restoration
- translation
- rewriting
- style transfer
- long-form summarization
- document-level verification
- targeted repair and controlled replan

The goal is **not** to build a generic agent platform. The goal is to build a **small, explicit, script-first kernel** that preserves the strengths of the current repository:

- low external dependency surface
- deterministic file and state handling
- resumable execution
- explicit quality gates
- compatibility with weak and strong models
- compatibility with both human-facing workflows and machine-run pipelines

---

## 2. Scope

### 2.1 Goals

The kernel should provide:

1. a unified document representation across multiple source types
2. a stable planning contract for long-text transformation tasks
3. semantic chunking with bounded context
4. chunk-isolated transformation execution
5. deterministic merge and output assembly
6. deterministic-first verification
7. targeted repair and controlled replan
8. concurrency-safe, resumable state persistence
9. structured telemetry, testing, and debugging support

### 2.2 Non-Goals

This kernel should **not** attempt to become:

- a full visual workflow platform
- a generic multi-agent framework
- a cloud orchestration runtime
- a hard dependency on LangGraph, Dify, Flowise, or similar systems
- a mandatory heavy semantic retrieval system

External workflow runtimes may be added later as optional orchestration layers, but they are not part of the core kernel design.

---

## 3. Core Problem Analysis

### 3.1 Problem Statement

Long-text transformation is not a single-prompt generation problem. It is a **global-consistency-constrained document transformation problem under limited context**.

The system must transform a document that is larger than the model's reliable working window while preserving or improving:

- semantic fidelity
- structural integrity
- style consistency
- terminology consistency
- formatting correctness
- resumability and traceability

### 3.2 Why This Problem Is Hard

#### A. Local Visibility vs Global Consistency

The model processes local chunks, but users evaluate output globally.

Typical failure modes:

- headings drift across chunks
- names and terms are translated inconsistently
- early and late sections differ in style
- repeated or missing transitions appear after merge
- the same entity is rendered differently across distant chunks

#### B. Multi-Objective Prompt Interference

When one prompt tries to restore structure, translate, rewrite, polish, and deduplicate at once, weaker models often collapse objectives or ignore secondary constraints.

#### C. Error Accumulation Across Chunks

Small chunk-level errors become document-level defects:

- slight summarization becomes material content loss
- slight markdown drift becomes broken structure
- slight translation looseness becomes factual drift

#### D. Context and Session Fragility

If orchestration depends on chat memory instead of persisted state, interruptions can cause:

- loss of progress
- invalid routing decisions
- duplicated work
- unsafe merge behavior

#### E. Cost, Latency, and Recovery Constraints

Long documents require many model calls. Without careful design, multi-pass processing can multiply cost and latency. The kernel must support:

- selective pass skipping
- per-pass model tiering
- safe concurrency
- resumable execution
- optional caching

### 3.3 Kernel-Level Requirements

A reusable kernel must guarantee:

1. **explicit state** instead of implicit memory
2. **deterministic orchestration** instead of prose-only routing
3. **semantic chunking** instead of naive fixed slicing
4. **multi-pass execution** instead of overloaded prompts
5. **global constraint propagation** instead of local-only continuity
6. **generate / verify / repair loops** instead of one-shot optimism
7. **idempotent resumability** instead of fragile reruns
8. **concurrency-safe persistence** instead of single-file race conditions
9. **pluggable adapters** instead of task-specific hardcoding

---

## 4. Design Principles

### 4.1 Compiler-Style Pipeline

Treat long-text transformation like a compiler pipeline:

1. `ingest`
2. `normalize`
3. `extract_global_constraints` (optional)
4. `plan`
5. `chunk`
6. `transform`
7. `verify`
8. `repair`
9. `replan` (if required)
10. `merge`
11. `emit`

Each stage has a narrow responsibility, explicit inputs and outputs, and recoverable intermediate artifacts.

### 4.2 Script-First, Prompt-Bounded

High-risk decisions should live in deterministic code that emits structured JSON.

Use prompts for transformation work, not for deciding:

- which branch to take
- whether prerequisites are satisfied
- whether outputs pass hard minimum thresholds
- whether replan is required
- whether a repair loop should continue

### 4.3 Deterministic-First Verification

Verification should prefer deterministic checks first. Semantic checks may be layered on top, but they must be:

- clearly classified as heuristic or advisory
- isolated from stop/go rules unless confidence is high
- prevented from causing unbounded repair loops

### 4.4 Weak-Model Compatibility by Design

The kernel should assume weaker models may be used in some deployments.

Implications:

- narrow prompt responsibilities
- script-owned branch logic
- explicit state synchronization
- bounded chunk context
- machine-checkable outputs whenever possible

### 4.5 Control Loops Must Be Separated

The design must clearly separate:

- **repair**: targeted correction of already-planned work
- **replan**: changing chunking or execution strategy because the plan is no longer healthy
- **resume**: recovering a partially executed run without changing the plan

These are different control loops and must not be conflated.

---

## 5. Canonical Architecture

### 5.1 Layered View

```text
Source Adapters
  -> Normalizer
  -> Global Constraint Extractor (optional)
  -> Planner
  -> Chunkers
  -> Continuity Builder
  -> Transform Runner
  -> Verification Engine
  -> Repair Engine
  -> Replan Controller
  -> Merge Engine
  -> Output Adapters
```

### 5.2 Core Architectural Decisions

For this repository, the recommended direction is:

- **do not** embed a heavyweight external workflow platform as a hard dependency
- **do not** introduce a graph runtime unless cross-skill orchestration becomes a real requirement
- **do** extract the current long-text machinery into a reusable kernel with stable contracts
- **do** keep existing low-level CLI commands for debuggability while gradually adding higher-level facades

### 5.3 Core Layers

#### A. Source Adapters

Convert task-specific inputs into a unified source payload.

Examples:

- YouTube subtitles
- Deepgram transcript segments
- plain text files
- Markdown documents
- future Docling or PDF parser outputs

#### B. Normalizer

Convert source payloads into a canonical intermediate representation.

Responsibilities:

- normalize text encoding and line structure
- preserve source segmentation if available
- preserve provenance metadata
- preserve timing metadata if available
- produce a stable `NormalizedDocument`

#### C. Global Constraint Extractor

Optionally extract document-level constraints before chunk transformation.

Responsibilities:

- build a glossary of terms and entities
- extract style guidance or transformation constraints
- capture document-wide summary or logic tree when needed
- emit a reusable `ConstraintBundle`

This pass is optional and task-dependent. It is most useful for translation, rewriting, style transfer, and long documents with repeated entity references.

#### D. Planner

Produce a structured execution plan.

Inputs:

- normalized document metadata
- task type
- language mode
- quality constraints
- model budget config
- available source artifacts

Outputs:

- pass sequence
- chunking policy
- model tier policy
- verification policy
- repair policy
- replan policy
- expected artifacts

#### E. Chunkers

Produce stable chunk manifests.

Preferred priority order:

1. source-native segments with timestamps or boundaries
2. chapter or section boundaries
3. sentence-aware token chunking
4. character fallback only for compatibility

#### F. Continuity Builder

Build bounded local continuity context and structured boundary metadata.

#### G. Transform Runner

Run isolated model calls on chunks or sections.

Requirements:

- chunk isolation
- bounded retries
- provider-agnostic API abstraction
- concurrency-safe execution
- chunk telemetry
- resumable status updates
- progress and cancellation support

#### H. Verification Engine

Perform deterministic-first validation plus optional semantic checks.

#### I. Repair Engine

Perform targeted repair of affected chunks or sections.

#### J. Replan Controller

Trigger replanning only when the current plan is unhealthy.

#### K. Merge Engine

Compose final transformed content deterministically.

#### L. Output Adapters

Package final artifacts into Markdown, JSON, or future downstream formats.

---

## 6. Canonical Data Model

### 6.1 Source of Truth Strategy

The kernel must support two state surfaces:

1. **Machine state**: authoritative structured state for scripts and kernel modules
2. **Human sync state**: a compact, derived projection for workflow documents, weak models, and human inspection

#### Authoritative state

- `machine_state.json` is the canonical source of truth for kernel execution
- chunk-level state lives in per-chunk state files, not in a single monolithic JSON blob

#### Human-readable projection

- `human_sync_state.md` is generated from `machine_state.json`
- it exists for workflow ergonomics and compatibility with current skill patterns
- it is **not** the authoritative kernel state
- if human-edited input is needed, it must be explicitly imported through a controlled sync step

This preserves current workflow ergonomics without weakening the machine contract.

### 6.2 Canonical Artifacts

Recommended work directory layout:

```text
/work_dir/
├── machine_state.json
├── human_sync_state.md
├── constraints.json
├── plan.json
├── verification_report.json
├── repair_queue.json
├── telemetry/
│   └── events.jsonl
├── chunks/
│   ├── 0001/
│   │   ├── raw.txt
│   │   ├── state.json
│   │   ├── continuity.json
│   │   ├── outputs/
│   │   │   ├── structure_only.txt
│   │   │   └── translate_only.txt
│   │   └── attempts/
│   │       ├── structure_only.attempt1.json
│   │       └── structure_only.attempt2.json
│   └── 0002/
└── merged/
    └── optimized.md
```

This directory-as-database layout avoids single-file locking issues and makes resumability and debugging easier.

### 6.3 Core Contracts

#### `NormalizedDocument`

```json
{
  "document_id": "abc123",
  "source_type": "youtube_subtitle",
  "task": "transcript_rewrite",
  "language_mode": "bilingual",
  "metadata": {
    "title": "Example Title",
    "channel": "Example Channel",
    "source_url": "https://youtube.com/watch?v=...",
    "upload_date": "2026-03-18",
    "duration_sec": 5420
  },
  "sections": [],
  "segments": [],
  "artifacts": {
    "raw_text": "/tmp/abc123_raw.txt"
  },
  "provenance": {
    "subtitle_source": "YouTube Subtitles",
    "source_language": "en"
  }
}
```

#### `ConstraintBundle`

```json
{
  "document_id": "abc123",
  "style_guide": {
    "tone": "faithful_and_readable",
    "preserve_structure": true,
    "forbid_summarization": true
  },
  "glossary": [
    {
      "source": "retrieval-augmented generation",
      "target": "检索增强生成",
      "confidence": 0.96,
      "scope": "global"
    }
  ],
  "entities": [
    {
      "name": "LangGraph",
      "canonical_form": "LangGraph",
      "aliases": ["lang graph"]
    }
  ],
  "document_outline": [],
  "notes": []
}
```

#### `MachineState`

```json
{
  "schema_version": 1,
  "document_id": "abc123",
  "task": "transcript_rewrite",
  "status": "running",
  "current_pass": "structure_only",
  "current_stage": "transform",
  "work_dir": "/tmp/abc123_work",
  "artifacts": {
    "raw_text": "/tmp/abc123_raw.txt",
    "structured_text": "/tmp/abc123_structured.txt",
    "optimized_text": "/tmp/abc123_optimized.txt",
    "output_file": ""
  },
  "source": {
    "type": "youtube_subtitle",
    "url": "https://youtube.com/watch?v=...",
    "title": "Example Title",
    "channel": "Example Channel",
    "upload_date": "2026-03-18",
    "duration_sec": 5420,
    "source_language": "en",
    "subtitle_source": "YouTube Subtitles"
  },
  "execution": {
    "active_plan_id": "plan_001",
    "replan_count": 0,
    "repair_count": 0,
    "requires_human_review": false
  }
}
```

#### `ExecutionPlan`

```json
{
  "schema_version": 1,
  "plan_id": "plan_001",
  "document_id": "abc123",
  "path": "long",
  "chunking": {
    "mode": "tokens",
    "source": "segments",
    "target_input_tokens": 1200,
    "hard_cap_tokens": 1600,
    "boundary_strategy": "strict_core_with_reference_overlap"
  },
  "model_tiering": {
    "structure_only": "small_fast_model",
    "translate_only": "default_model",
    "polish_style": "high_quality_model"
  },
  "passes": [
    {
      "pass_id": "p1",
      "kind": "chunk",
      "prompt": "structure_only",
      "input_key": "raw_path",
      "output_key": "processed_path",
      "supports_auto_replan": true,
      "verification_policy": "structure_minimum"
    },
    {
      "pass_id": "p2",
      "kind": "chunk",
      "prompt": "translate_only",
      "input_key": "processed_path",
      "output_key": "translated_path",
      "supports_auto_replan": false,
      "verification_policy": "bilingual_balance"
    }
  ],
  "repair_policy": "default_repair_v1",
  "replan_policy": "default_replan_v1"
}
```

#### `ChunkStateRecord`

```json
{
  "schema_version": 1,
  "chunk_id": "0007",
  "plan_id": "plan_001",
  "status": "completed",
  "raw_path": "/tmp/abc123_work/chunks/0007/raw.txt",
  "processed_path": "/tmp/abc123_work/chunks/0007/outputs/structure_only.txt",
  "translated_path": "/tmp/abc123_work/chunks/0007/outputs/translate_only.txt",
  "core_start": 945.2,
  "core_end": 1118.6,
  "reference_pre_start": 930.0,
  "reference_post_end": 1133.0,
  "estimated_input_tokens": 1098,
  "actual_output_tokens": 1322,
  "attempts": 2,
  "last_error": "",
  "warnings": [],
  "incomplete_structures": ["list"],
  "lineage": {
    "superseded_by": "",
    "derived_from": ""
  }
}
```

#### `VerificationReport`

```json
{
  "schema_version": 1,
  "passed": true,
  "hard_failures": [],
  "warnings": [
    "Chunk 0012 has low bilingual alignment confidence"
  ],
  "repairable_issues": [
    {
      "issue_id": "issue_001",
      "kind": "chunk_repair",
      "target": "0012",
      "reason": "language_balance"
    }
  ],
  "metrics": {
    "coverage_ratio": 0.97,
    "bilingual_ratio": 0.92
  }
}
```

#### `RepairAction`

```json
{
  "action_id": "repair_001",
  "kind": "rerun_chunk",
  "target": "0012",
  "reason": "language_balance",
  "depends_on": [],
  "attempt": 1,
  "max_attempts": 2,
  "fallback_strategy": "requires_human_review"
}
```

#### `ReplanDecision`

```json
{
  "required": false,
  "reason": "",
  "trigger": "",
  "scope": "pass",
  "preserve_completed": true,
  "max_replan_attempts": 2
}
```

---

## 7. Global Constraint Extraction

### 7.1 Why It Exists

Local continuity context is not enough for long documents. It helps adjacent chunks, but it does not solve cross-document consistency for terms, entities, tone, or repeated motifs.

### 7.2 Recommended Pass 0

Introduce an optional **Global Constraint Extraction Pass** between normalization and chunk transformation.

Outputs:

- global glossary
- canonical entity forms
- style guide
- document outline or logic tree when needed
- transformation constraints

### 7.3 When to Run It

Recommended for:

- translation
- rewriting
- style transfer
- very long documents
- documents with repeated entities or domain terms

Optional or skippable for:

- punctuation-only cleanup
- minimal structure restoration
- small short-form content

### 7.4 Constraint Injection Policy

Per-chunk prompts may receive:

- bounded local continuity context
- relevant slice of the global glossary
- global style guide
- task-specific hard constraints

Do **not** blindly dump the full glossary into every prompt. The planner should slice the relevant subset when possible.

---

## 8. Chunking and Continuity Strategy

### 8.1 Strict Boundary Chunking

The preferred strategy is:

```text
[ previous reference overlap ] + [ core transform region ] + [ next reference overlap ]
```

The prompt must explicitly require:

- transform only the **core region**
- use the reference regions only for context
- never emit reference overlap content in the output

This makes deterministic merge tractable and avoids overlap deduplication problems.

### 8.2 Continuity Context

A minimal but useful continuity model should be formalized.

```python
class ContinuityContext:
    tail_text: str
    tail_tokens: int
    section_title: str
    section_level: int
    incomplete_structures: list[str]
    language_state: dict
```

Recommended semantics:

- `tail_text`: the tail of the previous **core** output, clipped to a bounded token budget
- `tail_tokens`: the actual estimated budget consumed
- `section_title`: current nearest structural heading
- `section_level`: heading depth when available
- `incomplete_structures`: structures that continue across chunks, such as list, quote, table, or code block
- `language_state`: bilingual hints such as expected pairing mode or active translation state

### 8.3 Tail Construction Rules

Tail construction should follow deterministic rules:

1. prefer complete sentence or paragraph boundaries
2. avoid cutting inside code blocks or markdown constructs
3. if no clean sentence boundary exists, fall back to token clipping
4. never use unbounded tails

### 8.4 Special Boundary Cases

The chunker and continuity builder must handle:

- chapter boundaries
- heading boundaries
- lists spanning chunks
- block quotes spanning chunks
- code fences spanning chunks
- bilingual paired paragraphs spanning chunks

---

## 9. State Store and Resumability

### 9.1 Persistence Strategy

Use a concurrency-aware file layout rather than a single mutable JSON blob for all chunk state.

Recommended approach for v1:

- `machine_state.json` for document-level state
- per-chunk `state.json` files under `chunks/<chunk_id>/`
- atomic writes via temporary file + rename
- append-only JSONL telemetry for event history

SQLite in WAL mode is a valid future upgrade, but directory-as-db is the simpler starting point for this repository.

### 9.2 Chunk Lifecycle

Every chunk should move through a constrained lifecycle:

```text
pending -> running -> completed
pending -> running -> failed
pending -> running -> requires_repair
pending -> running -> requires_replan
pending -> running -> requires_human_review
pending -> superseded
```

### 9.3 Resume Semantics

The kernel should support explicit recovery from interrupted runs.

```python
def resume_from_checkpoint(state_path: str) -> dict:
    """
    - validate authoritative state integrity
    - detect incomplete or inconsistent chunk states
    - reconcile temp files and final outputs
    - return chunks that must be retried
    - emit repair or review suggestions when state corruption is detected
    """
```

### 9.4 Resume Rules

Resume logic should:

- skip chunks whose output artifact exists and whose `state.json` is `completed`
- retry chunks whose `state.json` is `running` but lack a valid completed output
- mark chunks with partial or corrupt output as `failed_recoverable`
- detect orphaned outputs without valid state and quarantine them for inspection
- detect state-file corruption and stop with a structured recovery report

### 9.5 Checkpoint Frequency

Checkpoint after:

- plan creation
- chunk manifest creation
- each chunk attempt result
- verification completion
- repair completion
- merge completion
- final emit

Never checkpoint mid-write to final output files. Always write temp files first, then atomically promote.

---

## 10. Transform Runner, Concurrency, and Caching

### 10.1 Execution Principles

The transform runner should preserve chunk isolation while supporting bounded concurrency.

### 10.2 Transform Configuration

```python
class TransformConfig:
    max_concurrent_chunks: int = 4
    rate_limit_per_minute: int = 60
    enable_progress_callback: bool = True
    support_cancellation: bool = True
    support_pause_resume: bool = True
    batch_small_chunks: bool = False
    batch_size_threshold_tokens: int = 500
```

### 10.3 Concurrency Policy

Default v1 policy:

- process independent chunks concurrently within a pass
- do not process multiple dependent passes for the same chunk at the same time
- do not mutate one shared chunk manifest file from multiple workers
- each worker updates only its chunk-local state, while document-level counters are aggregated safely

### 10.4 Rate Limiting

The LLM client should support:

- fixed or sliding-window request throttling
- retry with bounded backoff for transient transport errors
- provider-aware timeout strategy
- cooperative cancellation for long-running work

### 10.5 Caching Policy

A disk cache is useful but should be scoped carefully.

Recommended cache key:

```text
hash(prompt_template + prompt_version + model + api_format + config_snapshot + input_text)
```

Cache should be:

- optional
- local to the work directory or cache root
- invalidated by prompt or config changes
- safe to bypass when debugging

Caching is a v1.5 feature, not a blocker for kernel extraction.

---

## 11. Verification Policy

### 11.1 Deterministic-First Verification Stack

Verification should be layered.

#### Layer 1: deterministic hard checks

Examples:

- file exists and is non-empty
- no abrupt truncation markers
- markdown fence balance
- minimum heading and paragraph structure
- output/input size ratios within allowed bounds
- expected language presence when bilingual mode is required

#### Layer 2: deterministic heuristics

Examples:

- bilingual token ratio
- heading density
- repeated-line detection
- URL/code block exclusion patterns
- glossary coverage checks

#### Layer 3: optional semantic checks

Examples:

- embedding similarity
- keyphrase preservation
- optional LLM judge for advisory review

Layer 3 must not be the sole basis for automatic destructive repair.

### 11.2 Example Verification Configuration

```json
{
  "verification": {
    "bilingual_balance": {
      "metric": "token_ratio",
      "target_ratio": 1.0,
      "tolerance": 0.3,
      "exclude_patterns": ["```", "`", "http://", "https://"]
    },
    "coverage": {
      "min_output_input_ratio": 0.5,
      "max_output_input_ratio": 2.5
    },
    "drift_detection": {
      "enabled": false,
      "method": "embedding_similarity",
      "threshold": 0.85,
      "baseline": "source_chunk"
    }
  }
}
```

### 11.3 Verification Output Rules

Verification results should classify findings into:

- `hard_failures`
- `warnings`
- `repairable_issues`
- `requires_human_review`

Stop/go rules:

- any `hard_failures` => stop current stage
- `warnings` only => continue or repair according to policy
- `requires_human_review` => stop final emit unless explicitly configured otherwise

---

## 12. Repair Policy

### 12.1 Purpose

Repair is for targeted correction of already planned work. Repair must not silently become unbounded re-execution.

### 12.2 Repair Policy Contract

```python
class RepairPolicy:
    max_retries: int = 2
    backoff_multiplier: float = 2.0
    fallback_strategy: str = "requires_human_review"
    repair_dependencies: dict[str, list[str]] = {}
```

### 12.3 Repair Rules

Repair should:

- affect only the smallest impacted unit possible
- respect dependency order between repair actions
- stop after bounded retries
- preserve previous attempts for auditability
- mark irrecoverable units as `requires_human_review`

### 12.4 Fallback Strategies

Supported fallback strategies may include:

- `requires_human_review`
- `use_previous_successful_output`
- `use_original_content`
- `skip_and_block_emit`

The default for this repository should be conservative:

- mark as `requires_human_review`
- block final emit unless the caller explicitly allows partial output

---

## 13. Replan Policy

### 13.1 Purpose

Replan exists when the current plan is no longer healthy. It is not the same as repair.

Typical triggers:

- chunk failure rate exceeds threshold
- repeated timeout or transport instability suggests the plan budget is unsafe
- validation shows systemic issues caused by chunk sizing or pass design
- explicit manual request

### 13.2 Replan Trigger Contract

```python
class ReplanTrigger:
    condition: str
    threshold: float
    scope: str
    preserve_completed: bool = True
    require_user_confirmation: bool = False
    max_replan_attempts: int = 2
```

### 13.3 Replan Scopes

Allowed scopes:

- `failed_chunks`
- `pass`
- `document`

Preferred order:

1. smallest valid scope first
2. preserve completed work whenever safe
3. mark superseded chunks clearly in lineage metadata

### 13.4 Replan Guardrails

To avoid control-loop thrashing:

- cap total replans per document
- store explicit replan reasons
- never auto-replan indefinitely
- if replan budget is exhausted, mark `requires_human_review`

---

## 14. Merge and Emit Policy

### 14.1 Deterministic Merge

The merge engine must operate on deterministic artifacts only.

Responsibilities:

- resolve output ordering
- stitch core outputs only
- inject chapter or section headers from structured metadata
- preserve bilingual pairing rules where required
- detect missing chunk outputs before final merge

### 14.2 Overlap Rule

The merge engine should **not** attempt fuzzy semantic overlap resolution by default.

Instead, the kernel should enforce:

- reference overlap is context-only
- chunk output contains transformed core only
- merge is simple ordered concatenation plus deterministic structural insertion

This is more robust than post hoc overlap deduplication.

### 14.3 Partial Output Policy

By default:

- if any chunk is in `requires_human_review`, block final emit

Optional explicit mode:

- emit partial output with visible placeholders or tags for downstream review

Partial emit must never be the silent default.

---

## 15. Testing Strategy

This refactor requires a formal test plan.

### 15.1 Unit Tests

Target modules:

- `chunkers.py`
- `planner.py`
- `continuity.py`
- `merge_engine.py`
- `verify_engine.py`
- `repair_engine.py`
- `state_store.py`

Focus areas:

- edge boundaries
- broken markdown structures
- chunk lifecycle transitions
- state corruption handling
- resume logic
- replan decision logic

### 15.2 Integration Tests

Required flows:

- short-path Chinese cleanup
- long-path subtitle structure restoration
- bilingual long-path transformation
- recovery after interrupted chunk execution
- repair after verification failure

### 15.3 Regression Tests

Maintain fixtures for:

- known weak-model failure cases
- chunk merge regressions
- bilingual balance regressions
- state migration regressions
- performance and cost baselines on small representative samples

### 15.4 Mocking Strategy

Tests should support:

- mocked LLM responses
- deterministic provider failure simulation
- forced timeout and retry simulation
- manifest corruption fixtures

---

## 16. Telemetry and Observability

### 16.1 Telemetry Contract

```python
class TelemetryEvent:
    trace_id: str
    document_id: str
    chunk_id: str | None
    pass_id: str | None
    event_type: str
    timestamp: float
    duration_ms: float | None
    tokens_used: int | None
    cost_usd: float | None
    metadata: dict
```

### 16.2 Minimum Observability Requirements

The kernel should record:

- document-level trace ID
- pass-level start and finish events
- chunk-level attempts
- provider request latency
- retry history
- token estimates and actual usage when available
- verification outcomes
- repair and replan decisions

### 16.3 Logging Strategy

Recommended approach:

- JSONL event logs under `telemetry/events.jsonl`
- human-readable summaries for CLI output
- no requirement for an external observability backend in v1

This keeps the kernel lightweight while preserving debuggability.

---

## 17. Suggested Module Split

The current `yt_transcript_utils.py` can be refactored into a kernel-oriented layout.

```text
yt-transcript/
├── kernel/
│   ├── contracts.py
│   ├── normalizer.py
│   ├── constraint_store.py
│   ├── state_store.py
│   ├── planner.py
│   ├── chunkers.py
│   ├── continuity.py
│   ├── llm_client.py
│   ├── transform_runner.py
│   ├── verify_engine.py
│   ├── repair_engine.py
│   ├── replan_controller.py
│   ├── merge_engine.py
│   ├── source_adapters.py
│   ├── output_adapters.py
│   └── telemetry.py
├── prompts/
├── workflows/
├── scripts/
└── yt_transcript_utils.py
```

### Module Responsibilities

- `contracts.py`: schemas, enums, versioned data contracts
- `normalizer.py`: canonical `NormalizedDocument` generation
- `constraint_store.py`: `ConstraintBundle` extraction and lookup
- `state_store.py`: atomic state writes, resume, reconciliation, migrations
- `planner.py`: pass planning, budget-aware strategy, model tiering
- `chunkers.py`: chunk construction and chunk manifest generation
- `continuity.py`: continuity extraction and boundary metadata
- `llm_client.py`: provider abstraction, retries, rate limiting, optional cache
- `transform_runner.py`: per-pass concurrent chunk execution
- `verify_engine.py`: deterministic-first verification pipeline
- `repair_engine.py`: targeted repair planning and execution
- `replan_controller.py`: plan health monitoring and replanning
- `merge_engine.py`: deterministic merge and final assembly
- `source_adapters.py`: ingestion bridges
- `output_adapters.py`: markdown and structured exports
- `telemetry.py`: event emission and summaries

---

## 18. CLI and Python API Surface

### 18.1 CLI Strategy

Do not replace the current low-level commands immediately. Preserve them for debugging and workflow stability.

Add higher-level facades gradually.

Recommended high-level shape:

```bash
python3 yt_transcript_utils.py ingest-source ...
python3 yt_transcript_utils.py normalize-document /tmp/machine_state.json
python3 yt_transcript_utils.py extract-constraints /tmp/machine_state.json
python3 yt_transcript_utils.py plan-transform /tmp/machine_state.json
python3 yt_transcript_utils.py chunk-document /tmp/machine_state.json
python3 yt_transcript_utils.py run-pass /tmp/machine_state.json --pass-id p1
python3 yt_transcript_utils.py verify-document /tmp/machine_state.json
python3 yt_transcript_utils.py repair-document /tmp/machine_state.json
python3 yt_transcript_utils.py merge-document /tmp/machine_state.json
python3 yt_transcript_utils.py emit-output /tmp/machine_state.json
```

### 18.2 Compatibility Strategy

Current commands should map incrementally:

- `plan-optimization` -> planner facade
- `chunk-text` / `chunk-segments` -> chunker primitives
- `process-chunks` -> transform runner primitive
- `merge-content` / `assemble-final` -> merge and output layers
- `verify-quality` -> verification layer
- `validate-state` -> compatibility projection validation

### 18.3 Python API Surface

Suggested high-level functions:

```python
def ingest_source(source_type: str, source_ref: str, config: dict) -> dict: ...
def normalize_document(state_path: str) -> dict: ...
def extract_constraints(state_path: str) -> dict: ...
def plan_transform(state_path: str) -> dict: ...
def chunk_document(state_path: str, pass_id: str | None = None) -> dict: ...
def run_pass(state_path: str, pass_id: str, force: bool = False) -> dict: ...
def verify_document(state_path: str) -> dict: ...
def repair_document(state_path: str, issue_filter: list[str] | None = None) -> dict: ...
def merge_document(state_path: str) -> dict: ...
def emit_output(state_path: str, output_format: str = "markdown") -> dict: ...
def resume_from_checkpoint(state_path: str) -> dict: ...
```

### 18.4 Error Contract

Every kernel API should return a structured result:

```json
{
  "passed": false,
  "warnings": [],
  "hard_failures": ["manifest missing chunk 0014"],
  "requires_human_review": false,
  "replan_required": false,
  "artifacts": {}
}
```

---

## 19. Migration from Current Project

### 19.1 Existing Capabilities Already Present

The current project already contains many kernel primitives:

- `plan-optimization` -> planning layer
- `chunk-text` / `chunk-segments` -> chunking layer
- `process-chunks` -> transform runner primitive
- `merge-content` / `assemble-final` -> merge and output layers
- `verify-quality` -> verification layer
- `validate-state` -> compatibility checkpoint enforcement
- manifest runtime and attempt logs -> telemetry seeds
- `replan_required` runtime behavior -> replan controller seeds

### 19.2 Recommended Refactor Sequence

#### Phase 1: Extract Contracts and State Store

Create:

- `kernel/contracts.py`
- `kernel/state_store.py`
- `kernel/telemetry.py`

Goal:

- stabilize schemas
- add atomic writes and resume support
- reduce JSON shape drift

#### Phase 2: Extract Engine Modules

Create:

- `kernel/normalizer.py`
- `kernel/planner.py`
- `kernel/chunkers.py`
- `kernel/continuity.py`
- `kernel/merge_engine.py`
- `kernel/verify_engine.py`

Goal:

- isolate responsibilities
- make testing cheaper and more focused

#### Phase 3: Add Constraint and Control Loops

Create:

- `kernel/constraint_store.py`
- `kernel/repair_engine.py`
- `kernel/replan_controller.py`

Goal:

- formalize global consistency support
- separate repair from replan

#### Phase 4: Lift CLI Facades

Add new high-level commands while preserving current low-level commands.

Goal:

- keep workflows stable
- improve composability

#### Phase 5: Expand Test and Fixture Coverage

Add unit, integration, regression, and recovery fixtures.

Goal:

- lock down behavior before aggressive refactors

---

## 20. Recommended Decision

For this repository, the best path is:

1. keep the existing script-first architecture
2. extract a reusable long-text kernel from the current utilities
3. make `machine_state.json` authoritative while keeping a derived human-readable sync file
4. add a formal global constraint layer for terminology and style consistency
5. make resume, repair, verify, and replan first-class contracts
6. adopt concurrency-safe per-chunk persistence rather than a monolithic mutable manifest
7. preserve current low-level CLI commands during migration
8. defer heavyweight framework adoption unless the repository evolves into a broader service or multi-skill runtime

In short:

> Build a **small, explicit, resumable, concurrency-safe long-text transformation kernel** with deterministic control loops and bounded LLM usage.
