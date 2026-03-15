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
- `preferred_source_language`
- `mode`

Rules:

- If English subtitles are available, set `mode=bilingual` and use English as the source text
- Else if Chinese subtitles are available, set `mode=chinese` and use Chinese as the source text
- Else STOP
- If only unsupported subtitle languages exist, `subtitle-info` may still report `has_any=true`, but `mode` remains empty and this workflow must STOP and switch to audio transcription

Explicit product rule:

- If both English and Chinese subtitles exist, bilingual mode still uses English subtitles as the only source text for content generation
- The Chinese subtitle file is not merged into the output; bilingual output is produced by translating the English source text

Write to state:

- `src: youtube`
- `mode: chinese|bilingual`
- `source_language: <preferred_source_language>`
- `subtitle_source: YouTube Subtitles`

---

## Step 2: Download Subtitle Files

```bash
SUB_DOWNLOAD_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" subtitles)
```

Record:

- `download_dir`
- `downloaded_files`
- `english_files`
- `chinese_files`
- `selected_source_vtt`
- `selected_source_language`
- `selected_source_kind`

`download_dir` points to the per-video isolated temp directory used by the script.

The script downloads the exact selected subtitle language codes instead of a fixed whitelist, so regional variants such as `en-GB` and `zh-TW` are preserved.

If no VTT files were downloaded, STOP.

---

## Step 3: Use the Script-Selected Source VTT

Do not choose files manually in the workflow.

Use:

- `selected_source_vtt` for raw-text extraction
- `selected_source_language` for state
- `selected_source_kind` for debugging/reporting

Optional:

- When `mode=bilingual` and Chinese VTT also exists, keep it on disk for debugging only
- Do not merge two subtitle files directly in this workflow

---

## Step 4: Parse VTT to Raw Text

```bash
python3 <skill-root>/yt_transcript_utils.py parse-vtt "$SELECTED_SOURCE_VTT" > /tmp/${VIDEO_ID}_raw_text.txt
```

Write to state:

- `step: 3`
- `source_language: <selected_source_language>`
- `last_action: parsed subtitles to raw text`

---

## Checkpoint

Before proceeding to `workflows/text_optimization.md`, run:

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage post-source
```

If `hard_failures` is non-empty, STOP.
