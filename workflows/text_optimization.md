# Text Optimization Workflow

This workflow handles structure, translation, chunk processing, merge, and quality verification.

---

## Context Sync

Read `/tmp/${VIDEO_ID}_state.md` and extract:

- `vid`
- `url` (use as `$VIDEO_URL` when calling helpers)
- `duration`
- `mode`
- `src`
- `source_language`
- `subtitle_source`
- `output_dir`
- `work_dir`

Run:

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage post-source
```

If `hard_failures` is non-empty, STOP.

---

## Step 1: Generate Structured Plan

```bash
PLAN_JSON=$(python3 <skill-root>/yt_transcript_utils.py plan-optimization /tmp/${VIDEO_ID}_state.md)
```

Read from JSON:

- `video_path`
- `requires_llm_preflight`
- `operations`
- `replan_contract`
- `outputs`

If `hard_failures` is non-empty, STOP.

If `requires_llm_preflight=true`, ensure:

```bash
bash <skill-root>/scripts/preflight.sh --require-llm
```

`plan-optimization` is the canonical router here. It still reports the raw `duration_bucket` (`short` for `< 1800s`, `long` for `>= 1800s`), but `video_path` may escalate a short-duration transcript to chunked execution when the normalized input is too large for reliable single-pass prompting. The separate Quick Mode from `SKILL.md` is a narrower `< 900` second shortcut inside the short-duration bucket.

---

## Short Video Path (`video_path=short`)

Use this path only when `PLAN_JSON.video_path=short`. Do not infer it from duration alone.

### Step 1: Read Raw Text

```bash
RAW_TEXT=$(cat /tmp/${VIDEO_ID}_raw_text.txt)
```

### Step 2: Follow Planned Operations

Read `operations` from `PLAN_JSON`.

- If there is one `prompt=structure_only` operation, write directly to `/tmp/${VIDEO_ID}_optimized.txt`
- If there are two operations, they will be:
  - `prompt=structure_only` from raw text to `/tmp/${VIDEO_ID}_structured.txt`
  - `prompt=translate_only` from structured text to `/tmp/${VIDEO_ID}_optimized.txt`
- If an operation has non-empty `extra_instruction`, append it when executing the prompt

### Step 3: Save Outputs

- Chinese output → `/tmp/${VIDEO_ID}_optimized.txt`
- Bilingual intermediate → `/tmp/${VIDEO_ID}_structured.txt`
- Bilingual final → `/tmp/${VIDEO_ID}_optimized.txt`

Write to state:

- `step: 4`
- `last_action: optimized short video`

---

## Long Video Path (`video_path=long`)

This path now covers both true long videos and oversized short-duration transcripts that were escalated by the planner.

### Step 1: Detect Existing Chapters

```bash
python3 <skill-root>/yt_transcript_utils.py get-chapters "$VIDEO_URL" > /tmp/${VIDEO_ID}_chapters.json
```

### Step 2: Build Chunk Work Dir

Canonical long-path rule: if `PLAN_JSON.normalization.materialized=true`, chunk from the normalized document directly:

```bash
python3 <skill-root>/yt_transcript_utils.py chunk-document \
    /tmp/${VIDEO_ID}_normalized_document.json \
    /tmp/${VIDEO_ID}_chunks \
    --prompt structure_only \
    --chapters /tmp/${VIDEO_ID}_chapters.json
```

`chunk-document` auto-selects `segments` when the normalized document carries timed segments, and otherwise falls back to normalized text.

Compatibility fallback: if you do not have a normalized document yet, the lower-level chunkers still work:

```bash
python3 <skill-root>/yt_transcript_utils.py chunk-segments \
    /tmp/${VIDEO_ID}_segments.json \
    /tmp/${VIDEO_ID}_chunks \
    --prompt structure_only \
    --chapters /tmp/${VIDEO_ID}_chapters.json
```

```bash
python3 <skill-root>/yt_transcript_utils.py chunk-text \
    /tmp/${VIDEO_ID}_raw_text.txt \
    /tmp/${VIDEO_ID}_chunks \
    --prompt structure_only
```

Update state:

- `work_dir: /tmp/${VIDEO_ID}_chunks`
- `chunk`
- `total`

### Step 3: Build Chapter Plan (Optional)

If `/tmp/${VIDEO_ID}_chapters.json` reports `has_chapters=true` **and** your chunk manifest includes timing metadata (i.e. `chunk-document` selected timed segments, or you used `chunk-segments` directly), generate `/tmp/${VIDEO_ID}_chunks/chapter_plan.json`:

```bash
python3 <skill-root>/yt_transcript_utils.py build-chapter-plan \
    /tmp/${VIDEO_ID}_chapters.json \
    /tmp/${VIDEO_ID}_chunks \
    /tmp/${VIDEO_ID}_chunks/chapter_plan.json \
    > /tmp/${VIDEO_ID}_chapter_plan_result.json
```

Notes:

- `build-chapter-plan` requires each chunk in `manifest.json` to have `start_time` / `end_time`; this works when `chunk-document` selected timed segments, or when you used `chunk-segments` directly. If the plan resolved to text-only chunking, it will STOP with an error.
- If you cannot produce timed chunks, continue without `chapter_plan.json`; `merge-content` still succeeds, just without injected YouTube chapter headers.
- If you must have headings but lack timing, generate a best-effort plan by summarizing chunks and creating `chapter_plan.json` manually (this is not a true YouTube-chapter mapping).

### Step 4: Process Chunks

Read `operations` from `PLAN_JSON`.

If you are resuming after an interrupted run and want to inspect the repaired manifest explicitly first, you may run:

```bash
python3 <skill-root>/yt_transcript_utils.py prepare-resume \
    /tmp/${VIDEO_ID}_chunks \
    --prompt structure_only
```

This step is optional because `process-chunks` now runs the same resume repair automatically before execution starts.

- The first chunk operation always uses `prompt=structure_only`
- Read `PLAN_JSON.chunking.driver`; when it is `chunk-document`, prefer the canonical normalized-document chunking path above instead of re-deriving raw-text vs timed-segment branching manually
- If its `extra_instruction` is non-empty, pass it through `--extra-instruction`
- Every chunk operation includes an `execution` object; follow it instead of re-deriving replan behavior in prose
- When `execution.supports_auto_replan=true`, include every flag in `execution.recommended_cli_flags`
- When `execution.on_replan_required=stop_and_review`, do not call `replan-remaining`; STOP and surface the manifest/runtime state for manual review

Canonical command shape:

- Raw structure stage:
  ```bash
  python3 <skill-root>/yt_transcript_utils.py process-chunks \
      /tmp/${VIDEO_ID}_chunks \
      --prompt structure_only \
      --auto-replan
  ```
- Processed translation stage (when present):
  ```bash
  python3 <skill-root>/yt_transcript_utils.py process-chunks \
      /tmp/${VIDEO_ID}_chunks \
      --prompt translate_only \
      --input-key processed_path
  ```

Current contract:

- `input_key=raw_path` → use `--auto-replan`
- Existing `chapter_plan.json` mappings remain valid after raw-path replans because `replan-remaining` remaps chapter start chunks onto the replacement plan
- `input_key=processed_path` → auto-replan is unsupported; if JSON reports `replan_required=true`, STOP and review before continuing

### Step 5: Merge

```bash
python3 <skill-root>/yt_transcript_utils.py merge-content \
    /tmp/${VIDEO_ID}_chunks \
    /tmp/${VIDEO_ID}_optimized.txt
```

Write to state:

- `step: 4`
- `last_action: processed long video`

---

## Quality Verification

```bash
python3 <skill-root>/yt_transcript_utils.py verify-quality \
    /tmp/${VIDEO_ID}_optimized.txt \
    --raw-text /tmp/${VIDEO_ID}_raw_text.txt
```

Add `--bilingual` if `mode=bilingual`.

If `hard_failures` is non-empty, STOP.

If only `warnings` are present, review them before deciding to continue.

Do not continue to final assembly until verification passes.
