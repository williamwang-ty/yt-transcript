# Text Optimization Workflow

This workflow handles Chinese cleanup, structure, translation, chunk processing, merge, and quality verification.

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
- `outputs`
- `source_route_reason`
- `reroute_recommended`
- `reroute_target`

If `hard_failures` is non-empty, STOP.

If `reroute_recommended=true` and `reroute_target=deepgram`, STOP this subtitle optimization path and switch back to the Deepgram source workflow before continuing.

If `requires_llm_preflight=true`, ensure:

```bash
YT_TRANSCRIPT_AGENT_CLI=<your-agent> bash <skill-root>/scripts/preflight.sh --require-llm
```

Chunk LLM backend (`llm_backend`, default `auto`): `process-chunks` sends chunks to your own CLI so long videos use the same model family as you, and falls back to the configured API when that CLI is missing, fails its probe, or fails mid-run. Tell it who you are:

- Built-in agents: `claude-code` (`claude -p`), `codex` (`codex exec`), `opencode` (`opencode run`), `pi` (`pi -p`), `gemini` (`gemini -p`), `cursor` (`cursor-agent -p --trust`). Set `YT_TRANSCRIPT_AGENT_CLI=<name>` or pass `--agent-cli <name>` to `process-chunks`.
- Any other agent: pass your own non-interactive command instead, e.g. `--agent-cli "mytool run {prompt}"` (`{prompt}` marks where the prompt goes; without it the prompt is sent on stdin). The command must print only the answer text.
- If you pass nothing, the host is detected from its environment variables; when nothing is detected the API is used.
- `llm_backend: api` in config.yaml always uses the API.

`plan-optimization` is the canonical router here. It still reports the raw `duration_bucket` (`short` for `< 1800s`, `long` for `>= 1800s`), but `video_path` may escalate a short-duration transcript to chunked execution when the normalized input is too large for reliable single-pass prompting. The separate Quick Mode from `SKILL.md` is a narrower `< 900` second shortcut inside the short-duration bucket.

`routing_reason` explains duration/size routing. `source_route_reason` explains source-quality routing, including subtitle quality scoring and any Deepgram fallback recommendation.

---

## Short Video Path (`video_path=short`)

Use this path only when `PLAN_JSON.video_path=short`. Do not infer it from duration alone.

### Step 1: Read Raw Text

```bash
RAW_TEXT=$(cat /tmp/${VIDEO_ID}_raw_text.txt)
```

### Step 2: Follow Planned Operations

Read `operations` from `PLAN_JSON`.

- If there is one monolingual Chinese cleanup operation, write directly to `/tmp/${VIDEO_ID}_optimized.txt`; it currently uses `prompt=cleanup_zh`
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
    --prompt <RAW_STAGE_PROMPT> \
    --chapters /tmp/${VIDEO_ID}_chapters.json
```

`chunk-document` auto-selects the normalized document's preferred source shape. Most timed sources still prefer `segments`, but Chinese YouTube-subtitle long paths now prefer cleaned `text` while retaining `segments` for timing/chapter metadata.
Use the first raw-path operation prompt from `PLAN_JSON.operations` as `<RAW_STAGE_PROMPT>`:

- `cleanup_zh` for Chinese monolingual output
- `structure_only` for bilingual English-source output

Compatibility fallback: if you do not have a normalized document yet, the lower-level chunkers still work:

```bash
python3 <skill-root>/yt_transcript_utils.py chunk-segments \
    /tmp/${VIDEO_ID}_segments.json \
    /tmp/${VIDEO_ID}_chunks \
    --prompt <RAW_STAGE_PROMPT> \
    --chapters /tmp/${VIDEO_ID}_chapters.json
```

```bash
python3 <skill-root>/yt_transcript_utils.py chunk-text \
    /tmp/${VIDEO_ID}_raw_text.txt \
    /tmp/${VIDEO_ID}_chunks \
    --prompt <RAW_STAGE_PROMPT>
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

- `build-chapter-plan` requires each chunk in `manifest.json` to have `start_time` / `end_time`; this works when `chunk-document` selected timed segments, or when you used `chunk-segments` directly. If the plan resolved to text-first chunking (including Chinese YouTube-subtitle long paths), it will STOP with an error.
- If you cannot produce timed chunks, continue without `chapter_plan.json`; `merge-content` still succeeds, just without injected YouTube chapter headers.
- If you must have headings but lack timing, generate a best-effort plan by summarizing chunks and creating `chapter_plan.json` manually (this is not a true YouTube-chapter mapping). `process-chunks --prompt summarize` writes `summary_chunk_*.txt` next to the chunks and does not change which executor the cleanup stage uses.

### Step 4: Process Chunks

Read `operations` from `PLAN_JSON`.

If you are resuming after an interrupted run and want to inspect the repaired manifest explicitly first, you may run:

```bash
python3 <skill-root>/yt_transcript_utils.py prepare-resume \
    /tmp/${VIDEO_ID}_chunks \
    --prompt <RAW_STAGE_PROMPT>
```

This step is optional and only applies to fixed chunks (`long_text_mode: chunks`): `process-chunks` runs the same resume repair automatically before execution starts. A window run needs no repair; re-running `process-chunks` resumes at the cursor saved in `<work_dir>/window/state.json`.

- The first chunk operation is the monolingual cleanup / structuring stage:
  - `prompt=cleanup_zh` for Chinese monolingual output
  - `prompt=structure_only` for bilingual English-source output
- Read `PLAN_JSON.chunking.driver`; when it is `chunk-document`, prefer the canonical normalized-document chunking path above instead of re-deriving raw-text vs timed-segment branching manually
- If its `extra_instruction` is non-empty, pass it through `--extra-instruction`
- When the raw-stage prompt is `cleanup_zh`, `process-chunks` now auto-builds a transcript glossary if `/tmp/${VIDEO_ID}_chunks/glossary.json` does not exist yet; you may still run `build-glossary --mode transcript` explicitly beforehand if you want to inspect the terms first. In window mode these terms reach the rounds only when there is no outline; with an outline, its own term list is used, and `glossary.json` still feeds the `verify-quality` glossary checks

Canonical command shape:

- Raw first stage:
  ```bash
  python3 <skill-root>/yt_transcript_utils.py process-chunks \
      /tmp/${VIDEO_ID}_chunks \
      --prompt <RAW_STAGE_PROMPT> \
      --agent-cli <your-agent>
  ```
- Processed translation stage (when present):
  ```bash
  python3 <skill-root>/yt_transcript_utils.py process-chunks \
      /tmp/${VIDEO_ID}_chunks \
      --prompt translate_only \
      --agent-cli <your-agent> \
      --input-key processed_path
  ```

Sliding-window mode (`long_text_mode: window`, the default): for the `cleanup_zh` / `quick_cleanup` / `structure_only` raw stage, `process-chunks` runs the window executor instead of fixed chunks: it reads the source from the chunk files, writes paragraphs to `<work_dir>/window/`, and JSON reports `long_text_mode=window`, `status`, `coverage` and `efficiency`. The `translate_only --input-key processed_path` stage then also runs as a window over that output (into `<work_dir>/window/processed/`); if the raw stage used fixed chunks, translation does too. Re-running the same command resumes at the cursor. Before the first `cleanup_zh` / `structure_only` window round, the executor asks the model once for a global outline (section titles + start units, and a name/term list); the rounds then use those titles instead of inventing headings. Pass `--chapters /tmp/${VIDEO_ID}_chapters.json` to `chunk-document` (as above) so YouTube chapters are used as outline hints even on text-first Chinese paths. `merge-content` merges the latest window stage; for an unfinished window run it still writes the file but reports `success=false`. `chapter_plan.json` headings are only inserted when the run had no outline (the whole source fits in one round, `quick_cleanup`, `window_outline: false`, or the outline failed). If a window run reports `status=failed` (an LLM error that outlasted the backend's retries), fix the cause and re-run the same command; it resumes at the cursor.

Fixed chunks (`long_text_mode: chunks`, kept as a legacy fallback): the same commands process one chunk file at a time. A work dir that already has finished fixed chunks keeps resuming that way even in window mode. If `process-chunks` reports `status=aborted` (for example after repeated consecutive timeouts), STOP and surface the manifest/runtime state before re-running it.

### Step 5: Merge

```bash
python3 <skill-root>/yt_transcript_utils.py merge-content \
    /tmp/${VIDEO_ID}_chunks \
    /tmp/${VIDEO_ID}_optimized.txt
```

`merge-content` now includes a deterministic post-merge cleanup step for chunk seams. It can trim repeated seam text, merge obviously split short fragments, and remove duplicate chapter/title lines introduced at the merge boundary.

Write to state:

- `step: 4`
- `last_action: processed long video`

---

## Quality Verification

```bash
python3 <skill-root>/yt_transcript_utils.py verify-quality \
    /tmp/${VIDEO_ID}_optimized.txt \
    --raw-text /tmp/${VIDEO_ID}_raw_text.txt \
    --work-dir /tmp/${VIDEO_ID}_chunks
```

Add `--bilingual` if `mode=bilingual`.

If `hard_failures` is non-empty, STOP.

If only `warnings` are present, review them before deciding to continue.

`checks` now also carries advisory readability metrics such as chunk seam duplication,
Chinese spacing anomalies, repeated phrase density, short paragraph ratio,
header density, punctuation density, and glossary drift so the warning reasons stay explainable.
For a window run (`--work-dir` given) it also reports source coverage: `window_coverage_ratio`, `window_uncovered_ranges`, `window_low_output_ratio_paragraphs` and `window_fallback_paragraphs`.

Do not continue to final assembly until verification passes.
When continuing to final assembly, construct any date fragment in the final Markdown filename as `yyyy-mm-dd`; do not reuse raw metadata `upload_date` (`yyyymmdd`) in the filename.
