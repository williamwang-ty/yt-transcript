# Deepgram Transcription Workflow

This workflow handles audio download and speech-to-text transcription using Deepgram API.

---

## Context Sync

**↳ READ State**: `cat /tmp/${VIDEO_ID}_state.md`

Extract and confirm from state file:
- `vid` = _______
- `url` = _______
- `title` = _______

From system:
- `CONFIG_FILE`: `~/.claude/skills/yt-transcript/config.yaml`

**If state file is missing**: STOP. Return to SKILL.md Step 1.

---

## Step 1: Download Audio

Use the unified download script:

```bash
bash ~/.claude/skills/yt-transcript/scripts/download.sh "$VIDEO_URL" audio
```

This downloads the best available audio to `/tmp/${VIDEO_ID}.*` (extension auto-detected).

---

## Step 2: Determine Language

Based on video title and channel name, determine primary language:
- Primarily Chinese → `LANGUAGE=zh`
- Primarily English → `LANGUAGE=en`

Record: `LANGUAGE=_______`

---

## Step 3: Check File Size and Split if Needed

```bash
AUDIO_FILE=$(ls /tmp/${VIDEO_ID}.* 2>/dev/null | grep -v vtt | head -1)
FILE_SIZE=$(stat -f%z "$AUDIO_FILE" 2>/dev/null || stat -c%s "$AUDIO_FILE")
MAX_SIZE=10485760  # 10MB

if [ "$FILE_SIZE" -gt "$MAX_SIZE" ]; then
    echo "⚠️ Audio file exceeds 10MB, splitting..."
    python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py split-audio "$AUDIO_FILE" --max-size 10
    SPLIT_MODE=true
else
    SPLIT_MODE=false
fi
```

---

## Step 4: Call Deepgram API

### For Single File (< 10MB)

```bash
CONFIG_FILE=~/.claude/skills/yt-transcript/config.yaml
DEEPGRAM_API_KEY=$(grep 'deepgram_api_key' "$CONFIG_FILE" | sed 's/.*: *"\(.*\)"/\1/')

# Detect file extension and set Content-Type
AUDIO_FILE=$(ls /tmp/${VIDEO_ID}.* 2>/dev/null | grep -v vtt | head -1)
EXT="${AUDIO_FILE##*.}"
case "$EXT" in
    m4a|mp4) CONTENT_TYPE="audio/mp4" ;;
    webm)    CONTENT_TYPE="audio/webm" ;;
    opus)    CONTENT_TYPE="audio/opus" ;;
    mp3)     CONTENT_TYPE="audio/mpeg" ;;
    *)       CONTENT_TYPE="audio/mp4" ;;
esac

curl -s -X POST "https://api.deepgram.com/v1/listen?model=nova-2&language=$LANGUAGE&diarize=true&punctuate=true&paragraphs=true&smart_format=true" \
  -H "Authorization: Token $DEEPGRAM_API_KEY" \
  -H "Content-Type: $CONTENT_TYPE" \
  --data-binary @"$AUDIO_FILE" \
  --max-time 300 \
  -o /tmp/${VIDEO_ID}_deepgram.json
```

**Error Handling**:
- If curl fails, do NOT retry automatically
- Print error and ask user: "Deepgram API failed. Retry or skip?"
- Wait for user response

### For Split Files (≥ 10MB)

Process each chunk sequentially:

```bash
CHUNK_INDEX=0
ALL_TRANSCRIPTS=""

for CHUNK in /tmp/${VIDEO_ID}_chunk_*.mp3; do
    echo "🔄 Processing chunk $((CHUNK_INDEX + 1))..."
    
    curl -s -X POST "https://api.deepgram.com/v1/listen?model=nova-2&language=$LANGUAGE&diarize=true&punctuate=true&paragraphs=true&smart_format=true" \
      -H "Authorization: Token $DEEPGRAM_API_KEY" \
      -H "Content-Type: audio/mpeg" \
      --data-binary @"$CHUNK" \
      --max-time 300 \
      -o "/tmp/${VIDEO_ID}_chunk_${CHUNK_INDEX}_deepgram.json"
    
    CHUNK_TEXT=$(python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py process-deepgram "/tmp/${VIDEO_ID}_chunk_${CHUNK_INDEX}_deepgram.json" | python3 -c "import sys,json; print(json.load(sys.stdin)['transcript'])")
    ALL_TRANSCRIPTS="$ALL_TRANSCRIPTS $CHUNK_TEXT"
    
    CHUNK_INDEX=$((CHUNK_INDEX + 1))
done

echo "$ALL_TRANSCRIPTS" > /tmp/${VIDEO_ID}_combined_transcript.txt
```

---

## Step 5: Parse Transcription Result

```bash
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py process-deepgram "/tmp/${VIDEO_ID}_deepgram.json" > /tmp/${VIDEO_ID}_processed.json

# Extract cleaned transcript and speaker count
TRANSCRIPT=$(cat /tmp/${VIDEO_ID}_processed.json | python3 -c "import sys,json; print(json.load(sys.stdin)['transcript'])")
SPEAKER_COUNT=$(cat /tmp/${VIDEO_ID}_processed.json | python3 -c "import sys,json; print(json.load(sys.stdin)['speaker_count'])")

# Save to file
echo "$TRANSCRIPT" > /tmp/${VIDEO_ID}_raw_text.txt
```

---

## Checkpoint

Before proceeding to `workflows/text_optimization.md`, verify:

- [ ] Raw text saved to: `/tmp/${VIDEO_ID}_raw_text.txt`
- [ ] Language: `_______` (zh or en)
- [ ] Speaker count: `_______`
- [ ] Subtitle source recorded: `Deepgram Transcription`

If any is missing, STOP and review Steps 1-5.
