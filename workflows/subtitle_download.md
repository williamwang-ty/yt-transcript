# Subtitle Download Workflow

This workflow handles downloading and parsing subtitle files from YouTube.

---

## Context Sync

**↳ READ State**: `cat /tmp/${VIDEO_ID}_state.md`

Extract and confirm from state file:
- `vid` = _______
- `url` = _______
- `title` = _______

**If state file is missing**: STOP. Return to SKILL.md Step 1.

---

## Step 1: Check Subtitle Languages

List available subtitles:

```bash
yt-dlp --list-subs "$VIDEO_URL" 2>&1
```

Identify:
- Chinese subtitles: `zh`, `zh-Hans`, `zh-CN`, `zh-Hant`
- English subtitles: `en`, `en-orig`, `en-US`

---

## Step 2: Download Subtitles

Use the unified download script:

```bash
bash ~/.claude/skills/yt-transcript/scripts/download.sh "$VIDEO_URL" subtitles
```

This automatically downloads available subtitles to `/tmp/${VIDEO_ID}.*.vtt`.

---

## Step 3: Parse VTT to Plain Text

For each downloaded subtitle file:

```bash
# Find the first available VTT file (prefer Chinese, then English)
VTT_FILE=$(ls /tmp/${VIDEO_ID}.zh*.vtt 2>/dev/null | head -1)
[ -z "$VTT_FILE" ] && VTT_FILE=$(ls /tmp/${VIDEO_ID}.en*.vtt 2>/dev/null | head -1)
[ -z "$VTT_FILE" ] && VTT_FILE=$(ls /tmp/${VIDEO_ID}.*.vtt 2>/dev/null | head -1)

# Parse to plain text - always output to consistent path
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py parse-vtt "$VTT_FILE" > /tmp/${VIDEO_ID}_raw_text.txt
```

---

## Step 4: Determine Output Strategy

| Available Subtitles | Strategy | Next Workflow |
|---------------------|----------|---------------|
| Chinese only | Use Chinese text | `text_optimization.md` (Chinese mode) |
| English only | Use English text | `text_optimization.md` (Bilingual mode) |
| Both | Use both texts | `text_optimization.md` (Bilingual mode) |

---

## Checkpoint

Before proceeding to `workflows/text_optimization.md`, verify:

- [ ] Raw text saved to: `/tmp/${VIDEO_ID}_raw_text.txt`
- [ ] Language mode: [ ] Chinese only / [ ] Bilingual
- [ ] Subtitle source recorded: `YouTube Subtitles`

If any is missing, STOP and review Steps 1-4.
