---
name: yt-transcript
description: Transcribe YouTube videos into formatted Markdown articles. Supports subtitle download or Deepgram speech-to-text. Use this skill when a user provides a YouTube link and wants a text version.
---

# YouTube Video Transcription

Transcribe YouTube videos into formatted Markdown articles with optional bilingual support.

> [!IMPORTANT]
> This skill uses script-first workflows. Prefer the helper scripts' JSON output over manual parsing or ad-hoc shell heuristics.

> [!NOTE]
> Intentional product decisions:
> - `bilingual` always means English source text plus Chinese translation
> - If both English and Chinese subtitles exist, English remains the only source text for generation
> - `scripts/preflight.sh` is intentionally staged so subtitle-only paths do not require Deepgram or LLM credentials

---

## Core Rules

1. Always start with `scripts/preflight.sh`
2. Use `scripts/download.sh` JSON output to make decisions
3. Persist all workflow state to `/tmp/${VIDEO_ID}_state.md`
4. Read values from state before every irreversible action
5. Do not read large optimized files into context; use utility scripts instead
6. Use `validate-state` and `verify-quality` JSON output for stop/go decisions
7. Use `plan-optimization` for text-processing decisions instead of re-deriving prompt branches in the workflow

---

## State File Schema

Create `/tmp/${VIDEO_ID}_state.md` after metadata is loaded.

```markdown
# State
vid: ${VIDEO_ID}
url: ${VIDEO_URL}
title: ${TITLE}
channel: ${CHANNEL}
upload_date: ${UPLOAD_DATE}
duration: ${DURATION}
output_dir: ${OUTPUT_DIR}
mode: unknown
src: unknown
source_language: unknown
subtitle_source: unknown
language_mode: unknown
output_file:
work_dir: /tmp/${VIDEO_ID}_chunks

# Progress
step: 1
chunk: 0
total: 0
last_action: got metadata

# Rules
- On error: STOP and report
- Always trust state over memory
```

---

## Quick Mode

Use this streamlined path only when all conditions are true:

- Video duration `< 900` seconds
- `subtitle-info` reports `has_any=true`
- No chapter planning or multi-speaker output is required

Quick Mode is an optional fast path inside the broader `video_path=short` bucket from `plan-optimization`. The canonical short/long routing remains `< 1800s = short`, `>= 1800s = long`.

### Quick Mode Steps

1. Run base preflight:
   ```bash
   bash <skill-root>/scripts/preflight.sh
   ```

2. Fetch metadata:
   ```bash
   META_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" metadata)
   ```
   Record `video_id`, `title`, `channel`, `upload_date`, `duration`.

3. Load config:
   ```bash
   CONFIG_JSON=$(python3 <skill-root>/yt_transcript_utils.py load-config)
   ```
   Record `output_dir`.

4. Create state file using the schema above.

5. Inspect subtitles:
   ```bash
   SUB_INFO_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" subtitle-info)
   ```
   Record:
   - `has_any`
   - `mode` (`chinese` or `bilingual`)
   - `preferred_source_language`

   Exit Quick Mode if `has_any=false`.

6. Download subtitles:
   ```bash
   SUB_DOWNLOAD_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" subtitles)
   ```

7. Read subtitle selection from JSON:
   - `selected_source_vtt`
   - `selected_source_language`
   - `selected_source_kind`

8. Parse source VTT:
   ```bash
   python3 <skill-root>/yt_transcript_utils.py parse-vtt "$SELECTED_SOURCE_VTT" > /tmp/${VIDEO_ID}_raw_text.txt
   ```

9. Optimize text:
   - If `mode=chinese`, use `prompts/quick_cleanup.md` and save to `/tmp/${VIDEO_ID}_optimized.txt`
   - If `mode=bilingual`, first run `prompts/structure_only.md` to `/tmp/${VIDEO_ID}_structured.txt`, then `prompts/translate_only.md` to `/tmp/${VIDEO_ID}_optimized.txt`

10. Assemble final file:
   ```bash
   python3 <skill-root>/yt_transcript_utils.py assemble-final \
       /tmp/${VIDEO_ID}_optimized.txt \
       "$OUTPUT_DIR/${DATE}. ${CLEAN_TITLE}.md" \
       --title "$TITLE" \
       --source "$VIDEO_URL" \
       --channel "$CHANNEL" \
       --date "$UPLOAD_DATE" \
       --created "$DATE" \
       --duration "$DURATION" \
       --transcript-source "YouTube Subtitles"
   ```
   Add `--bilingual` if `mode=bilingual`.

11. Cleanup:
   ```bash
   bash <skill-root>/scripts/cleanup.sh "$VIDEO_ID"
   ```

---

## Full Workflow

### Step 0: Base Preflight

```bash
bash <skill-root>/scripts/preflight.sh
```

If this fails, STOP.

### Step 1: Metadata

```bash
META_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" metadata)
CONFIG_JSON=$(python3 <skill-root>/yt_transcript_utils.py load-config)
```

Create the state file immediately with:

- `vid`
- `url`
- `title`
- `channel`
- `upload_date`
- `duration`
- `output_dir`
- placeholders for `mode`, `src`, `source_language`, `subtitle_source`, `language_mode`, `output_file`

Then validate:

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage metadata
```

### Step 2: Subtitle Availability

```bash
SUB_INFO_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" subtitle-info)
```

Decision:

- `has_any=true` → Step 3A Subtitle Path
- `has_any=false` → Step 3B Deepgram Path

Write `step: 2` to state.

### Step 3A: Subtitle Path

Load and follow [workflows/subtitle_download.md](workflows/subtitle_download.md).

### Step 3B: Deepgram Path

Before entering this path:

```bash
bash <skill-root>/scripts/preflight.sh --require-deepgram
```

Then load and follow [workflows/deepgram_transcribe.md](workflows/deepgram_transcribe.md).

### Checkpoint After Step 3

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage post-source
```

If `hard_failures` is non-empty, STOP.

### Step 4: Text Optimization

Load and follow [workflows/text_optimization.md](workflows/text_optimization.md).

### Checkpoint After Step 4

```bash
python3 <skill-root>/yt_transcript_utils.py verify-quality \
    /tmp/${VIDEO_ID}_optimized.txt \
    --raw-text /tmp/${VIDEO_ID}_raw_text.txt
```

Add `--bilingual` if `mode=bilingual`.

If `hard_failures` is non-empty, STOP.

Warnings are advisory only and should trigger manual review, not an automatic stop.

### Step 5: Assemble and Save Final File

Read all metadata from state, not from memory.

Before assembling:

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage pre-assemble
```

If `hard_failures` is non-empty, STOP.

```bash
python3 <skill-root>/yt_transcript_utils.py assemble-final \
    /tmp/${VIDEO_ID}_optimized.txt \
    "$OUTPUT_DIR/${DATE}. ${CLEAN_TITLE}.md" \
    --title "$TITLE" \
    --source "$VIDEO_URL" \
    --channel "$CHANNEL" \
    --date "$UPLOAD_DATE" \
    --created "$DATE" \
    --duration "$DURATION" \
    --transcript-source "$SUBTITLE_SOURCE"
```

Add `--bilingual` if `mode=bilingual`.

Write `output_file` to state.

Then validate:

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage final
```

### Step 6: Cleanup

```bash
bash <skill-root>/scripts/cleanup.sh "$VIDEO_ID"
```

No separate `rm` is needed; the cleanup script removes the state file by default.

### Step 7: Report Success

Return:

```text
✅ Video transcription complete
   Title: $TITLE
   Language mode: $MODE
   Transcript source: $SUBTITLE_SOURCE
   Output file: $OUTPUT_FILE
```

---

## Multi-Link Processing

When processing multiple URLs:

1. Process serially
2. Complete cleanup between videos
3. Track results in a table

---

## Error Handling Policy

- `yt-dlp` failure: stop and report
- Deepgram failure: stop and ask whether to retry
- Missing state fields: stop and repair state before continuing
- Non-empty `hard_failures` from `verify-quality`: stop before assembly
- `warnings` from `verify-quality`: continue only after manual review

---

## Troubleshooting

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| `download.sh metadata` or `subtitle-info` fails | `yt-dlp` unavailable, outdated, or video unavailable | Run `bash <skill-root>/scripts/preflight.sh`, then retry once |
| `subtitle-info` returns `has_any=false` | No usable manual or auto subtitles | Switch to Deepgram path and run `bash <skill-root>/scripts/preflight.sh --require-deepgram` |
| Deepgram transcription fails | Invalid key, network issue, or API rejection | Stop, surface the error, and ask whether to retry |
| `verify-quality` returns non-empty `hard_failures` | Hard structural gate failed | Do not assemble final output; fix the optimization step |
| `verify-quality` returns only `warnings` | Soft quality concern | Review the warnings, then decide whether to proceed |
| State file is missing or fields are empty | Interrupted run or incomplete manual recovery | Rebuild the state from metadata/config before resuming |
