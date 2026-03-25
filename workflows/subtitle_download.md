# Subtitle Download Workflow

This workflow handles subtitle inspection, download, source-file selection, and raw text extraction.

---

## Context Sync

Read `/tmp/${VIDEO_ID}_state.md` and confirm:

- `vid`
- `url`
- `title`
- `channel`

If the state file is missing, STOP.

---

## Step 1: Inspect Subtitle Availability

```bash
SUB_INFO_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" subtitle-info)
```

Record from JSON:

- `has_manual`
- `has_auto`
- `english_available`
- `chinese_available`
- `listed_candidates`
- `preferred_source_language`
- `mode`

Rules:

- If Chinese subtitles are available, prefer a Chinese subtitle track as the source text
- Else if English subtitles are available, set `mode=bilingual` and use English as the source text
- Else STOP
- If only unsupported subtitle languages exist, `subtitle-info` reports `has_any=false` and the workflow must switch to audio transcription

Explicit product rule:

- If Chinese subtitles exist, they take precedence as the single subtitle source track
- Only when no usable Chinese subtitle track exists do we fall back to a single English subtitle source track
- Bilingual output is produced only when the selected source track is English; subtitle files are never merged together

Write to state:

- `src: youtube`
- `mode: chinese|bilingual` (provisional; confirm with `resolved_mode` after download)
- `source_language: <preferred_source_language>`
- `subtitle_source: YouTube Subtitles`

---

## Step 2: Download Subtitle Files

```bash
SUB_DOWNLOAD_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" subtitles)
```

Record:

- `download_dir`
- `listed_candidates`
- `downloaded_files`
- `english_files`
- `chinese_files`
- `preferred_source_language`
- `preferred_source_kind`
- `preferred_mode`
- `attempted_candidates`
- `blocked_candidates`
- `selected_source_vtt`
- `selected_source_language`
- `selected_source_kind`
- `final_source_language`
- `resolved_mode`
- `fallback_used`

`download_dir` points to the per-video isolated temp directory used by the script.

The script attempts one source track at a time instead of fetching multiple subtitle families together. It tries Chinese tracks first when available, then falls back to English tracks only if no usable Chinese track can be downloaded. Regional variants such as `en-GB` and `zh-TW` are preserved.

If a preferred candidate fails with an auth-like error such as `HTTP 429`, the script first retries the same candidate with Chrome cookies before marking it as blocked and continuing to the next candidate.

If no VTT files were downloaded, STOP.

---

## Step 3: Use the Script-Selected Source VTT

Do not choose files manually in the workflow.

Use:

- `selected_source_vtt` for raw-text extraction
- `selected_source_language` for state
- `selected_source_kind` for debugging/reporting
- `resolved_mode` for the downstream optimization mode
- `attempted_candidates` / `blocked_candidates` to explain why a listed track was not actually usable
- `fallback_used` to distinguish "preferred track selected" from "preferred track listed but unavailable"

Do not merge two subtitle files directly in this workflow.

---

## Step 4: Parse VTT to Raw Text

```bash
# 1) Always produce a plain raw text file (used by quality checks)
python3 <skill-root>/yt_transcript_utils.py parse-vtt "$SELECTED_SOURCE_VTT" > /tmp/${VIDEO_ID}_raw_text.txt

# 2) Also persist time-aligned segments for long-video timed chunking + chapter mapping
python3 <skill-root>/yt_transcript_utils.py parse-vtt-segments \
    "$SELECTED_SOURCE_VTT" \
    --language "$SELECTED_SOURCE_LANGUAGE" \
    > /tmp/${VIDEO_ID}_segments.json
```

Write to state:

- `step: 3`
- `mode: <resolved_mode>`
- `source_language: <selected_source_language>`
- `last_action: parsed subtitles to raw text`

---

## Checkpoint

Before proceeding to `workflows/text_optimization.md`, run:

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage post-source
```

If `hard_failures` is non-empty, STOP.
