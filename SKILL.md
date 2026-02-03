---
name: yt-transcript
description: Transcribe YouTube videos into formatted Markdown articles. Supports subtitle download or Deepgram speech-to-text (with multi-speaker recognition). Use this skill when a user provides a YouTube link and wants a text version of the video content.
---

# YouTube Video Transcription

Transcribe YouTube videos into formatted Markdown articles.

## Trigger Conditions

Automatically use this skill when a user provides a YouTube link and wants a text version of the video content.

## Multi-Link Processing

When the user provides **multiple YouTube links**:

1. **Serial processing**: Process one at a time, not in parallel
2. **Clear context after each video** (use /clear or equivalent) to prevent performance degradation from long text accumulation
3. Output summary after all are completed

### Summary Output Format

```
✅ Batch transcription complete (N videos total)

| # | Title | Status | Output File |
|---|-------|--------|-------------|
| 1 | <Title1> | ✅ Success | <Path1> |
| 2 | <Title2> | ✅ Success | <Path2> |
| 3 | <Title3> | ❌ Failed: <Reason> | - |
```

## Configuration

**Config file**: `config.yaml` (in the same directory as this file)

```bash
# Before first use, read the config file
CONFIG_FILE="$(dirname "$0")/config.yaml"
# Or use the config in the skill directory
CONFIG_FILE=~/.claude/skills/yt-transcript/config.yaml

# Extract config values
DEEPGRAM_API_KEY=$(grep 'deepgram_api_key' "$CONFIG_FILE" | sed 's/.*: *"\(.*\)"/\1/')
OUTPUT_DIR=$(grep 'output_dir' "$CONFIG_FILE" | sed 's/.*: *"\(.*\)"/\1/' | sed "s|~|$HOME|")
```

## Error Handling & Retry Policy

> [!IMPORTANT]
> **Never retry indefinitely.** If an operation fails after max retries, stop and report to the user.

### Maximum Retry Attempts

| Operation | Max Retries | Action on Failure |
|-----------|-------------|-------------------|
| yt-dlp metadata/subtitle commands | 2 | Report error, suggest updating yt-dlp |
| yt-dlp audio download | 2 | Report error, check video availability |
| Deepgram API call | 1 | Report error, do NOT retry automatically |
| File read/write | 1 | Report error |

### Pre-flight Checks (Before Step 1)

Before starting the workflow, perform these checks:

```bash
# 1. Verify Deepgram API key is configured
CONFIG_FILE=~/.claude/skills/yt-transcript/config.yaml
DEEPGRAM_API_KEY=$(grep 'deepgram_api_key' "$CONFIG_FILE" | sed 's/.*: *"\(.*\)"/\1/')
if [ -z "$DEEPGRAM_API_KEY" ] || [ "$DEEPGRAM_API_KEY" = "your_api_key_here" ]; then
    echo "❌ Error: Deepgram API key not configured"
    exit 1
fi

# 2. Test Deepgram API connectivity (quick validation)
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py test-deepgram-api "$DEEPGRAM_API_KEY"
```

### Timeout Guidelines (for Deepgram API)

Based on audio file size, use appropriate timeout:

| File Size | Recommended --max-time | Estimated Time |
|-----------|------------------------|----------------|
| <10MB     | 300 (5 min)            | ~2-3 min       |
| 10-30MB   | 600 (10 min)           | ~5-7 min       |
| 30-50MB   | 900 (15 min)           | ~8-12 min      |
| >50MB     | 1200 (20 min)          | Warn user first |

```bash
# Calculate timeout based on file size
FILE_SIZE=$(stat -f%z "$AUDIO_FILE" 2>/dev/null || stat -c%s "$AUDIO_FILE")
if [ "$FILE_SIZE" -lt 10485760 ]; then
    MAX_TIME=300
elif [ "$FILE_SIZE" -lt 31457280 ]; then
    MAX_TIME=600
elif [ "$FILE_SIZE" -lt 52428800 ]; then
    MAX_TIME=900
else
    MAX_TIME=1200
    echo "⚠️ Large file detected ($(($FILE_SIZE/1048576))MB). Upload may take 10-20 minutes."
fi
```

### Failure Reporting Format

When a step fails after max retries, report to user immediately:

```
❌ Transcription failed at Step X: <Step Name>

**Error**: <specific error message>
**Attempts**: <number of retries> / <max retries>
**Suggestion**: <actionable next step>

Options:
1. Retry with different settings
2. Skip this video (for batch processing)
3. Abort the entire task
```

### Common Failure Scenarios

| Scenario | Detection | Response |
|----------|-----------|----------|
| Video unavailable | yt-dlp returns "Video unavailable" | Stop, report to user |
| No subtitles + Deepgram fails | Deepgram returns error JSON | Stop, report error details |
| Network timeout | curl returns exit code 28 | Stop after 1 attempt, do NOT auto-retry |
| API key invalid | Deepgram returns 401 | Stop immediately, ask user to check config |
| Insufficient credits | Deepgram returns 402 | Stop immediately, ask user to top up |



## Workflow

### Step 0: Ensure yt-dlp is up to date (Important!)

YouTube frequently updates its API, older yt-dlp versions may fail to download. **Update before each execution**:

```bash
brew upgrade yt-dlp 2>/dev/null || pip install -U yt-dlp --break-system-packages 2>/dev/null || echo "Please manually update yt-dlp"
```

### Step 1: Extract VIDEO_ID and Get Video Basic Info

#### 1.1 Extract VIDEO_ID (supports all formats)

```bash
# Use yt-dlp directly, compatible with all YouTube URL formats
VIDEO_ID=$(yt-dlp --print "%(id)s" "<VIDEO_URL>" 2>/dev/null)
```

Supported URL formats:
- `https://www.youtube.com/watch?v=xxx`
- `https://youtu.be/xxx`
- `https://youtube.com/shorts/xxx`
- `https://youtube.com/live/xxx`

#### 1.2 Get Video Metadata

```bash
yt-dlp --print "%(title)s" --print "%(duration)s" --print "%(upload_date)s" --print "%(channel)s" "<VIDEO_URL>" 2>/dev/null
```

Record:
- VIDEO_ID (used in subsequent steps)
- Video title
- Duration (seconds)
- Upload date
- Channel name

### Step 2: Check Subtitle Availability

```bash
yt-dlp --list-subs "<VIDEO_URL>" 2>&1
```

**Decision logic**:
- If output contains "has no subtitles" AND "has no automatic captions" → No subtitles, go to Step 3B
- Otherwise → Has subtitles, go to Step 3A

### Step 3A: Has Subtitles - Download and Parse Subtitle File

#### 3A.1 Check Available Subtitle Languages

From Step 2's `--list-subs` output, determine:
- Whether Chinese subtitles exist (zh, zh-Hans, zh-CN, zh-Hant)
- Whether English subtitles exist (en, en-orig, en-US)

**Subtitle strategy**:
| Available Subtitles | Download Strategy | Output Format |
|---------------------|-------------------|---------------|
| Chinese only | Download Chinese | Chinese only |
| English only | Download English | Bilingual (translate English) |
| Chinese + English | Download both | Bilingual (side-by-side) |

#### 3A.2 Download Subtitles

**Scenario 1: Download Chinese subtitles only**
```bash
yt-dlp --write-sub --write-auto-sub --sub-lang "zh,zh-Hans,zh-CN" --sub-format "vtt" --skip-download -o "/tmp/${VIDEO_ID}" "<VIDEO_URL>"
```

**Scenario 2: Download English subtitles only**
```bash
yt-dlp --write-sub --write-auto-sub --sub-lang "en" --sub-format "vtt" --skip-download -o "/tmp/${VIDEO_ID}" "<VIDEO_URL>"
```

**Scenario 3: Download bilingual subtitles**
```bash
yt-dlp --write-sub --write-auto-sub --sub-lang "zh,zh-Hans,zh-CN,en" --sub-format "vtt" --skip-download -o "/tmp/${VIDEO_ID}" "<VIDEO_URL>"
```

#### 3A.3 Parse VTT File to Extract Plain Text

```bash
# Use utility script to parse VTT file
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py parse-vtt "/tmp/${VIDEO_ID}.zh.vtt"
# Output: plain text content to stdout
```

The script automatically handles: removes timestamps, VTT tags, cue numbers, consecutive duplicate lines.

Priority: Manual subtitles > Auto subtitles, Chinese > English

### Step 3B: No Subtitles - Audio Transcription

#### 3B.1 Check Available Audio Formats and Download (Important!)

**Don't specify format directly**, check available formats first:

```bash
# List all available formats
yt-dlp --list-formats "<VIDEO_URL>" 2>&1 | grep -E "audio|ID"
```

**Multi-track Video Handling**:

Modern YouTube videos may have multiple audio tracks (e.g., Chinese dubbing, English dubbing). Format IDs may have suffixes:
- `140-0`, `251-0` = Default/English track
- `140-1`, `251-1` = Chinese original track
- `140-drc`, `251-drc` = Dynamic range compression version

**Download strategy**:

```bash
# Method 1: Prioritize Chinese original track (recommended for Chinese videos)
# Check for zh-Hant or zh tagged audio tracks
AUDIO_FORMAT=$(yt-dlp --list-formats "<VIDEO_URL>" 2>&1 | grep -E "audio.*zh" | head -1 | awk '{print $1}')

# If no explicit Chinese track, use highest quality track without language tag
if [ -z "$AUDIO_FORMAT" ]; then
    AUDIO_FORMAT=$(yt-dlp --list-formats "<VIDEO_URL>" 2>&1 | grep -E "^140-0|^140 " | head -1 | awk '{print $1}')
fi

# If still nothing, use bestaudio
if [ -z "$AUDIO_FORMAT" ]; then
    AUDIO_FORMAT="bestaudio"
fi

# Download audio
yt-dlp -f "$AUDIO_FORMAT" -o "/tmp/${VIDEO_ID}.%(ext)s" "<VIDEO_URL>"
```

**Fallback** (if above fails):

```bash
# Try common format IDs directly
yt-dlp -f "140-0" -o "/tmp/${VIDEO_ID}.m4a" "<VIDEO_URL>" 2>&1 || \
yt-dlp -f "140" -o "/tmp/${VIDEO_ID}.m4a" "<VIDEO_URL>" 2>&1 || \
yt-dlp -f "bestaudio[ext=m4a]" -o "/tmp/${VIDEO_ID}.m4a" "<VIDEO_URL>" 2>&1
```

#### 3B.2 Determine Language

**Let Claude determine directly**, combining the following information:
- Video title
- Channel name
- Video description (if available)

Decision rules:
- Title/channel primarily Chinese → `language=zh`
- Title/channel primarily English or other languages → `language=en`
- For mixed Chinese-English, determine primary language based on channel type and content

#### 3B.3 Call Deepgram API

**Set Content-Type based on audio file extension**:

```bash
# Detect file extension
AUDIO_FILE=$(ls /tmp/${VIDEO_ID}.* 2>/dev/null | head -1)
EXT="${AUDIO_FILE##*.}"

# Set correct Content-Type
case "$EXT" in
    m4a|mp4) CONTENT_TYPE="audio/mp4" ;;
    webm)    CONTENT_TYPE="audio/webm" ;;
    opus)    CONTENT_TYPE="audio/opus" ;;
    mp3)     CONTENT_TYPE="audio/mpeg" ;;
    *)       CONTENT_TYPE="audio/mp4" ;;
esac

# Call API (using API Key from config file)
curl -s -X POST "https://api.deepgram.com/v1/listen?model=nova-2&language=<LANG>&diarize=true&punctuate=true&paragraphs=true&smart_format=true" \
  -H "Authorization: Token $DEEPGRAM_API_KEY" \
  -H "Content-Type: $CONTENT_TYPE" \
  --data-binary @"$AUDIO_FILE" \
  --max-time 300 \
  -o /tmp/${VIDEO_ID}_deepgram.json
```

**Note**: Set timeout to 300 seconds (5 minutes), longer videos need more time.

#### 3B.4 Parse Transcription Result

**Known issues with Deepgram Chinese transcription**:
1. Spaces between Chinese characters
2. Almost no punctuation
3. Repeated fragments

**Use utility script to process**:

```bash
# Process Deepgram JSON result
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py process-deepgram "/tmp/${VIDEO_ID}_deepgram.json"
# Output: JSON {"transcript": "cleaned text", "speaker_count": N}
```

The script automatically handles:
- Removes spaces between Chinese characters (multiple passes for thoroughness)
- Fixes spaces around punctuation
- Removes consecutive repeated phrases
- Counts number of speakers

### Step 4: AI Text Optimization (Core Step)

After obtaining preliminary text in Step 3, **must** use the model's own language understanding capability to deeply optimize the text. This is the key step for improving transcription quality.

#### 4.1 Why This Step is Needed

| Source | Typical Issues |
|--------|----------------|
| YouTube auto-captions | Poor sentence breaks, missing punctuation, homophone errors, poor proper noun recognition |
| Deepgram Chinese transcription | Almost no punctuation, no paragraph structure, spaces between Chinese chars, repeated fragments |
| YouTube manual subtitles | Better quality, but may still have poor segmentation |

#### 4.2 Processing Method

**Chunk** the raw text from Step 3 and send to the model for processing (about 2000-3000 characters per chunk to avoid omissions), using the following prompt to guide optimization:

**Processing prompt template**:

```
You are a professional video transcript editor. The following is raw transcript text extracted from the YouTube video "{video_title}" (source: {subtitle_source}).

Please optimize the text as follows, output the optimized plain text directly (don't explain what you did):

1. **Structure & Sectioning (CRITICAL)**:
   - **Paragraphs**: Divide text into natural paragraphs (3-8 sentences) based on logical pauses.
   - **Sections**: Identify distinct topic transitions and insert descriptive headers using Markdown Level 2 (## Header Name). **Every transcript must have at least 3-5 clear section headers.**

2. **Bilingual Translation (CRITICAL)**:
   - **If the content is English**: You MUST output in **Bilingual Mode**.
     - Format:
       [English Paragraph]
       
       [Chinese Translation Paragraph]
     - The Chinese translation must be fluent, natural, and accurate.
   - **If the content is Chinese**: Output in Chinese only.

3. **Error Correction & Cleanup**:
   - Fix speech recognition errors (homophones), proper noun spellings, and grammar.
   - Remove filler words (uh, um, like) and meaningless repetitions.
   - Add correct punctuation based on tone and semantics.

Notes:
- **Headers**: Use English headers if the video is English, but they serve as section dividers for both languages.
- **Translation**: Keep technical terms in English (e.g., "API", "LLM") within the Chinese text if appropriate.
- **Fidelity**: Preserve the original meaning and style. Do NOT summarize; translate the full content.

Raw text:
---
{chunk_text}
---
```

#### 4.3 Chunked Processing Flow

```
1. Divide complete raw text into chunks of about 2000-3000 characters
   - Try to split at sentence ends or obvious pauses, avoid breaking sentences
   - Overlap about 100 characters between adjacent chunks for context continuity

2. Execute AI optimization for each chunk:
   - Use the above prompt template
   - Collect the optimized text returned by the model

3. Merge all optimized chunks:
   - Handle overlapping regions (deduplicate)
   - Ensure section titles don't repeat
   - Check that paragraph transitions are natural

4. Final review:
   - Read through the entire text for coherence
   - Confirm section divisions are reasonable
   - Verify proper nouns are consistent throughout
```

#### 4.4 Special Handling for Multi-Speaker Scenarios

If Step 3 detected multiple speakers (Deepgram diarize result), add to the optimization prompt:

```
This video has multiple speakers. Please mark speaker changes at appropriate positions.
Use format: [Speaker X] followed by that speaker's content.
```

#### 4.5 Quality Checklist

After optimization, verify the following:
- [ ] Every sentence has ending punctuation
- [ ] Paragraph length is moderate (no more than 300 characters/paragraph)
- [ ] Reasonable section divisions (at least 3-5 sections, depending on content length)
- [ ] Proper nouns are spelled correctly and consistently
- [ ] No obvious repeated paragraphs
- [ ] Text flows naturally and is readable

### Step 5: Text Processing and Formatting

#### 5.1 Speech Recognition Error Correction (Supplementary)

Step 4's AI optimization handles most corrections. This step does final rule-based fixes:

Common correction mappings (extend based on context):

| Error | Correct |
|-------|---------|
| Number 0 replacing letter O | e.g., 0penc0de → OpenCode |
| ai → AI | Case normalization |
| agent → Agent | Proper nouns |
| mcp → MCP | Acronym normalization |

#### 5.2 Language Processing Rules

**Decision logic** (by priority):

1. **Has English subtitles** (regardless of video language) → Output bilingual format
2. **Chinese video (no English subtitles)** → Output Chinese only
3. **English video (no subtitles, transcribed via Deepgram)** → Output bilingual format

**Bilingual processing flow**:

When English subtitles are detected:
1. Download English subtitles (en or en-orig)
2. Also download Chinese subtitles (if available) or translate English content
3. Format in side-by-side paragraphs

**Bilingual format example**:
```markdown
## Introduction

**Speaker:** This is the original English content. We're going to discuss how technology shapes our future.

**讲者：** 这是原始的英文内容。我们将讨论技术如何塑造我们的未来。

---

## Main Topic

**Speaker:** The key point here is that innovation drives change.

**讲者：** 这里的关键点是创新推动变革。
```

**Bilingual formatting rules**:
- Within each topic paragraph, English first then Chinese
- English uses `**Speaker:**` label
- Chinese uses `**讲者：**` label
- Separate paragraphs with blank lines
- Separate topics with `---` dividers

**Translation requirements** (when translating English):
- Maintain accurate meaning
- Use natural, fluent Chinese expressions
- Keep technical terms in English with parenthetical Chinese notes, e.g.: API (Application Programming Interface)
- Keep names, place names and other proper nouns in English

#### 5.3 Formatting Requirements

**Speaker labels**:
- Single speaker: `**Speaker:**`
- Multiple speakers: `**Host:**`, `**Guest A:**`, `**Guest B:**`
- Deepgram returns speaker 0, 1, 2... map to Speaker, Guest A, Guest B...

**Paragraph structure**:
- Create paragraphs by natural topics
- Use second-level headings (##) to divide main topics
- Use dividers (---) to separate major sections

**Content optimization**:
- Remove: filler words (uh, um, like), repeated content, excessive pauses
- Keep: Emotional expressions in parentheses `(laughter)`, `(emphasis)`
- Special marks: Background music in *italics*
- Corrections: Fix speech recognition errors based on context

### Step 6: Generate Markdown File

File structure template:

```markdown
---
title: <Video Title>
source: <YouTube Link>
channel: <Channel Name>
date: <Upload Date YYYY-MM-DD>
created: <Today's Date>
type: video-transcript
bilingual: <true/false>
duration: <Duration>
transcript_source: <YouTube Subtitles/Deepgram Transcription>
---

# <Video Title>

> Video source: [YouTube - <Channel Name>](<YouTube Link>)
> Language mode: <Chinese only / Bilingual Chinese-English>
> Duration: <X minutes>

---

<Body content, arranged in paragraphs>

---

*This article was generated by AI voice transcription (<source>), for reference only.*
```

### Step 7: Save File

#### 7.1 Clean Filename

Video titles may contain illegal filename characters, use utility script to clean:

```bash
# Clean filename
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py sanitize-filename "Video Title: Special Characters?"
# Output: Video Title_ Special Characters_
```

The script automatically handles: replaces `/ \ : * ? " < > |` with `_`, removes leading/trailing spaces and periods, limits length to 200 characters.

#### 7.2 Save

File naming: `<YYYY-MM-DD>. <Cleaned Video Title>.md`

Save path: `$OUTPUT_DIR` (read from config.yaml)

### Step 8: Clean Temporary Files

```bash
rm -f /tmp/${VIDEO_ID}.* /tmp/${VIDEO_ID}_deepgram.json /tmp/${VIDEO_ID}.*.vtt
```

### Step 9: Output Confirmation

```
✅ Video transcription complete
   Title: <Video Title>
   Language: <Chinese/English/Bilingual>
   Subtitle source: <YouTube Subtitles/Deepgram Transcription>
   Bilingual mode: <Yes/No>
   Number of speakers: <N>
   Output file: <Full Path>
```

## Dependencies

- `yt-dlp`: Download YouTube videos/audio/subtitles (**keep updated**)
- `curl`: Call Deepgram API
- `python3`: Process JSON and text formatting
- Deepgram API account and key

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| **HTTP 403 error** | yt-dlp version outdated, YouTube API updated | `brew upgrade yt-dlp` or `pip install -U yt-dlp` |
| **"Requested format is not available"** | Specified format ID doesn't exist | Check available formats with `--list-formats` first |
| **Multi-track video downloads wrong language** | Video has multiple audio tracks | Check format list, select track with `zh` tag |
| **Deepgram Chinese has no punctuation** | Deepgram Chinese punctuation recognition is weak | Use post-processing with character count segmentation |
| **Spaces between Chinese characters** | Deepgram Chinese processing characteristic | Use regex to remove spaces multiple times |
| **Repeated fragments** | Common speech recognition issue | Use regex deduplication |
| **Deepgram API timeout** | Video too long (>30 minutes) | Increase `--max-time 300` or longer |
| **API error** | Invalid API Key or insufficient balance | Check Deepgram console |
| **Video inaccessible** | Private video or region restricted | Use VPN or proxy |

## Version History

- **v3.1** (2026-02): Refactor: Extract Python code to standalone script `yt_transcript_utils.py`, language detection changed to direct LLM judgment
- **v3.0** (2026-02): Added Step 4 "AI Text Optimization", use model understanding to add punctuation, sentence/paragraph/section splitting, error correction
- **v2.0** (2025-02): Fixed yt-dlp 403 error, support multi-track videos, improved Deepgram Chinese processing
- **v1.0**: Initial version
