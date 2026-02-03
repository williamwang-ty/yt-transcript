---
name: yt-transcript
description: Transcribe YouTube videos into formatted Markdown articles. Supports subtitle download or Deepgram speech-to-text. Use this skill when a user provides a YouTube link and wants a text version.
---

# YouTube Video Transcription

Transcribe YouTube videos into formatted Markdown articles with optional bilingual support.

> [!IMPORTANT]
> **For Weak Models**: This skill uses a modular workflow design. You will load specific workflow files as needed, keeping context manageable.

---

## Quick Mode (Recommended for Simple Cases)

Use this streamlined path for:
- Videos **< 15 minutes**
- **Has subtitles** available

### Quick Mode Steps

1. **Run pre-flight check**:
   ```bash
   bash ~/.claude/skills/yt-transcript/scripts/preflight.sh
   ```

2. **Get metadata**:
   ```bash
   bash ~/.claude/skills/yt-transcript/scripts/download.sh "$VIDEO_URL" metadata
   ```
   Record: `VIDEO_ID`, `TITLE`, `DURATION`

3. **Check subtitles exist**:
   ```bash
   yt-dlp --list-subs "$VIDEO_URL" 2>&1 | grep -q "has no subtitles"
   # If output contains "has no subtitles" AND "has no automatic captions", EXIT Quick Mode
   ```

4. **Download subtitles**:
   ```bash
   bash ~/.claude/skills/yt-transcript/scripts/download.sh "$VIDEO_URL" subtitles
   ```

5. **Parse VTT to text**:
   ```bash
   # Find the first available VTT file
   VTT_FILE=$(ls /tmp/${VIDEO_ID}.*.vtt 2>/dev/null | head -1)
   python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py parse-vtt "$VTT_FILE" > /tmp/${VIDEO_ID}_raw_text.txt
   ```

6. **Apply quick cleanup**:
   - Load `prompts/quick_cleanup.md`
   - Replace `{RAW_TEXT}` with content from `/tmp/${VIDEO_ID}_raw_text.txt`
   - Send to model, save output to `/tmp/${VIDEO_ID}_optimized.txt`

7. **Generate final file** (see Step 5 below)

8. **Cleanup**:
   ```bash
   bash ~/.claude/skills/yt-transcript/scripts/cleanup.sh "$VIDEO_ID"
   ```

**Exit Quick Mode if**:
- Duration > 900 seconds (15 min)
- No subtitles available
- User requests advanced features (chapter detection, multi-speaker)

---

## Full Workflow (For Complex Cases)

### Step 0: Pre-flight Check

**Always run first**:
```bash
bash ~/.claude/skills/yt-transcript/scripts/preflight.sh
```

If this fails, STOP and report error to user.

---

### Step 1: Get Video Metadata

```bash
bash ~/.claude/skills/yt-transcript/scripts/download.sh "$VIDEO_URL" metadata
```

**Record the following**:
- `VIDEO_ID` = _______
- `TITLE` = _______
- `DURATION` = _______ (seconds)
- `CHANNEL` = _______

---

### Step 2: Check Subtitle Availability

```bash
yt-dlp --list-subs "$VIDEO_URL" 2>&1
```

**Decision**:
- Output contains "has no subtitles" AND "has no automatic captions"?
  - **YES** → Go to **Step 3B** (Audio Transcription)
  - **NO** → Go to **Step 3A** (Subtitle Download)

---

### Step 3A: Subtitle Download Path

**Load and follow**: `workflows/subtitle_download.md`

This workflow will:
- Detect available subtitle languages
- Download and parse VTT files
- Save raw text to `/tmp/${VIDEO_ID}_*_text.txt`

**After completion**, proceed to **Step 4**.

---

### Step 3B: Audio Transcription Path

**Load and follow**: `workflows/deepgram_transcribe.md`

This workflow will:
- Download audio file
- Split if > 10MB
- Call Deepgram API
- Save raw text to `/tmp/${VIDEO_ID}_raw_text.txt`

**After completion**, proceed to **Step 4**.

> [!WARNING]
> **Error Recovery**: If Deepgram API fails, do NOT retry automatically. Report error and ask user: "Retry or skip?"

---

### Checkpoint After Step 3

**Verify you have**:
- [ ] `VIDEO_ID` = _______
- [ ] `TITLE` = _______
- [ ] Raw text saved to: _______
- [ ] Subtitle source: [ ] YouTube Subtitles / [ ] Deepgram Transcription
- [ ] Language mode: [ ] Chinese only / [ ] Bilingual

**If any is missing, STOP. Review Step 3A or 3B.**

---

### Step 4: Text Optimization

**Load and follow**: `workflows/text_optimization.md`

This workflow will:
- Determine if video is short (< 30 min) or long (≥ 30 min)
- Apply appropriate optimization strategy
- Handle chapter detection and chunking for long videos
- Save optimized text to `/tmp/${VIDEO_ID}_optimized.txt`

---

### Checkpoint After Step 4

**Verify you have**:
- [ ] Optimized text saved to: `/tmp/${VIDEO_ID}_optimized.txt`
- [ ] Text is structured with sections
- [ ] Translation complete (if bilingual mode)

**If any is missing, STOP. Review Step 4.**

---

### Step 5: Generate Final Markdown File

#### 5.1 Create Frontmatter

```bash
DATE=$(date +%Y-%m-%d)
DURATION_MIN=$((DURATION / 60))
BILINGUAL=$([ "$LANGUAGE_MODE" = "Bilingual" ] && echo "true" || echo "false")
```

#### 5.2 Build Final File

```markdown
---
title: $TITLE
source: $VIDEO_URL
channel: $CHANNEL
date: $UPLOAD_DATE
created: $DATE
type: video-transcript
bilingual: $BILINGUAL
duration: ${DURATION_MIN}m
transcript_source: $SUBTITLE_SOURCE
---

# $TITLE

> Video source: [YouTube - $CHANNEL]($VIDEO_URL)
> Language mode: $LANGUAGE_MODE
> Duration: ${DURATION_MIN} minutes

---

[Insert optimized text from /tmp/${VIDEO_ID}_optimized.txt]

---

*This article was generated by AI voice transcription ($SUBTITLE_SOURCE), for reference only.*
```

---

### Step 6: Save File

#### 6.1 Sanitize Filename

```bash
CLEAN_TITLE=$(python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py sanitize-filename "$TITLE")
```

#### 6.2 Save to Output Directory

```bash
CONFIG_FILE=~/.claude/skills/yt-transcript/config.yaml
OUTPUT_DIR=$(grep 'output_dir' "$CONFIG_FILE" | sed 's/.*: *"\(.*\)"/\1/' | sed "s|~|$HOME|")

OUTPUT_FILE="$OUTPUT_DIR/${DATE}. ${CLEAN_TITLE}.md"
```

Write final markdown content to `$OUTPUT_FILE`.

---

### Step 7: Cleanup

```bash
bash ~/.claude/skills/yt-transcript/scripts/cleanup.sh "$VIDEO_ID"
```

---

### Step 8: Report Success

```
✅ Video transcription complete
   Title: $TITLE
   Language: $LANGUAGE_MODE
   Subtitle source: $SUBTITLE_SOURCE
   Output file: $OUTPUT_FILE
```

---

## Multi-Link Processing

When processing **multiple YouTube links**:

1. **Process serially** (one at a time, not parallel)
2. **Clear context** between videos (to prevent memory buildup)
3. **Track results** in a table

### Batch Summary Format

```
✅ Batch transcription complete (N videos total)

| # | Title | Status | Output File |
|---|-------|--------|-------------|
| 1 | <Title1> | ✅ Success | <Path1> |
| 2 | <Title2> | ✅ Success | <Path2> |
| 3 | <Title3> | ❌ Failed: <Reason> | - |
```

---

## Error Handling Policy

> [!CAUTION]
> **Never retry indefinitely**. If an operation fails after max retries, STOP and report to user.

### Maximum Retry Attempts

| Operation | Max Retries | Action on Failure |
|-----------|-------------|-------------------|
| yt-dlp commands | 1 | Report error, suggest updating yt-dlp |
| Deepgram API call | 0 | Report error, ask user for retry |
| File read/write | 1 | Report error |

### Failure Response Template

```
❌ Transcription failed at Step X: <Step Name>

**Error**: <specific error message>
**Suggestion**: <actionable next step>

Options:
1. Retry with different settings
2. Skip this video (for batch processing)
3. Abort the entire task
```

---

## Configuration

**Config file**: `~/.claude/skills/yt-transcript/config.yaml`

Required settings:
- `deepgram_api_key`: Your Deepgram API key
- `output_dir`: Directory to save transcripts

---

## Dependencies

- `yt-dlp` (keep updated: `brew upgrade yt-dlp`)
- `ffmpeg` (for audio splitting: `brew install ffmpeg`)
- `python3` (built-in utilities)
- `curl` (for API calls)
- Deepgram API account and key

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| HTTP 403 error | Update yt-dlp: `brew upgrade yt-dlp` |
| Deepgram timeout | Increase `--max-time` or split file |
| API error 401 | Check Deepgram API key in config.yaml |
| API error 402 | Insufficient credits, top up account |

---

## File Structure

```
~/.claude/skills/yt-transcript/
├── SKILL.md                    # This file (main entry point)
├── workflows/                  # Modular workflow files
│   ├── subtitle_download.md
│   ├── deepgram_transcribe.md
│   └── text_optimization.md
├── prompts/                    # Single-task prompt templates
│   ├── structure_only.md
│   ├── translate_only.md
│   └── quick_cleanup.md
├── scripts/                    # Helper shell scripts
│   ├── preflight.sh
│   ├── download.sh
│   └── cleanup.sh
└── yt_transcript_utils.py      # Python utilities
```

---

## Version History

- **v4.0** (2026-02): Refactored for weak model adaptability - modular workflows, single-task prompts
- **v3.1** (2026-02): Extracted Python utilities to standalone script
- **v3.0** (2026-02): Added AI text optimization step
- **v2.0** (2025-02): Multi-track video support, improved Chinese processing
- **v1.0**: Initial version
