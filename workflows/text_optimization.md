# Text Optimization Workflow

This workflow handles structure, translation, chunk processing, merge, and quality verification.

---

## Context Sync

Read `/tmp/${VIDEO_ID}_state.md` and extract:

- `vid`
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
- `outputs`

If `hard_failures` is non-empty, STOP.

If `requires_llm_preflight=true`, ensure:

```bash
bash <skill-root>/scripts/preflight.sh --require-llm
```

---

## Short Video Path (`video_path=short`)

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

### Step 1: Detect Existing Chapters

```bash
python3 <skill-root>/yt_transcript_utils.py get-chapters "$VIDEO_URL" > /tmp/${VIDEO_ID}_chapters.json
```

### Step 2: Chunk Raw Text

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

### Step 3: Build Chapter Plan

- If YouTube chapters exist, map them to chunk indices and write `/tmp/${VIDEO_ID}_chunks/chapter_plan.json`
- Else:
  1. `process-chunks --prompt summarize`
  2. Aggregate `summary_chunk_*.txt`
  3. Create `/tmp/${VIDEO_ID}_chunks/chapter_plan.json`

### Step 4: Process Chunks

Read `operations` from `PLAN_JSON`.

- The first chunk operation always uses `prompt=structure_only`
- If its `extra_instruction` is non-empty, pass it through `--extra-instruction`
- If a second chunk operation exists, it uses `prompt=translate_only` and `--input-key processed_path`

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
