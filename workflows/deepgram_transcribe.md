# Deepgram Transcription Workflow

This workflow handles audio download and Deepgram transcription. Large audio files are split and merged by the Python utility, not by prompt-side shell logic.

---

## Context Sync

Read `/tmp/${VIDEO_ID}_state.md` and confirm:

- `vid`
- `url`
- `title`
- `channel`

If the state file is missing, STOP.

Before this workflow starts, base preflight and `--require-deepgram` preflight must already have passed.

---

## Step 1: Download Audio

```bash
AUDIO_JSON=$(bash <skill-root>/scripts/download.sh "$VIDEO_URL" audio)
```

Record from JSON:

- `download_dir`
- `audio_file`
- `audio_format`
- `extension`
- `size_bytes`

`download_dir` points to the per-video isolated temp directory used by the script.

If `audio_file` is missing, STOP.

---

## Step 2: Determine Source Language

Set:

- `LANGUAGE=zh` for primarily Chinese audio
- `LANGUAGE=en` for primarily English audio

Then write to state:

- `src: deepgram`
- `source_language: $LANGUAGE`
- `mode: chinese` if `LANGUAGE=zh`
- `mode: bilingual` if `LANGUAGE=en`
- `subtitle_source: Deepgram Transcription`

---

## Step 3: Transcribe with Unified Utility

```bash
python3 <skill-root>/yt_transcript_utils.py transcribe-deepgram \
    "$AUDIO_FILE" \
    --language "$LANGUAGE" \
    --output-json "/tmp/${VIDEO_ID}_deepgram.json" \
    --output-text "/tmp/${VIDEO_ID}_raw_text.txt" \
    --output-segments "/tmp/${VIDEO_ID}_segments.json" \
    > /tmp/${VIDEO_ID}_deepgram_result.json
```

This command automatically:

- Splits files larger than 10 MB
- Calls Deepgram once per chunk
- Processes each response
- Merges all chunk transcripts into `/tmp/${VIDEO_ID}_raw_text.txt`
- When `--output-json` is set, always writes the requested aggregate JSON path; split mode also writes sibling `*_chunk_XXX.json` payload files
- When `--output-segments` is set, writes aligned sentence-level segments (with `start_time` / `end_time`) for timed chunking + chapter mapping
- Emits lightweight observability fields in `/tmp/${VIDEO_ID}_deepgram_result.json`, including per-chunk transcript metadata and fallback warnings

No separate split-path workflow is needed.

**Error Handling**

- `transcribe-deepgram` now performs bounded automatic retries for transient Deepgram timeout/network failures
- If the command still fails after those retries, STOP
- Ask the user whether to retry only after surfacing the final error

---

## Step 4: Record Output Metadata

Read `/tmp/${VIDEO_ID}_deepgram_result.json` and record:

- `speaker_count`
- `chunk_count`
- `used_split_mode`
- `paragraph_count`
- `sentence_count`
- `word_count`
- `segment_count` (should be > 0 when `--output-segments` is enabled)
- `segments_output` (path to the segments JSON file, or empty)
- `warnings` (review especially when any chunk reports a structured-output fallback)

Write to state:

- `step: 3`
- `last_action: deepgram transcription completed`

---

## Checkpoint

Before proceeding to `workflows/text_optimization.md`, run:

```bash
python3 <skill-root>/yt_transcript_utils.py validate-state /tmp/${VIDEO_ID}_state.md --stage post-source
```

If `hard_failures` is non-empty, STOP.
