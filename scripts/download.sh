#!/usr/bin/env bash
set -e

# download.sh - Unified download wrapper for yt-transcript skill
# Usage: ./download.sh <VIDEO_URL> <MODE>
# MODE: metadata | subtitles | audio

if [ $# -lt 2 ]; then
    echo "Usage: $0 <VIDEO_URL> <MODE>"
    echo "MODE: metadata | subtitles | audio"
    exit 1
fi

VIDEO_URL="$1"
MODE="$2"

case "$MODE" in
    metadata)
        # Extract VIDEO_ID and metadata
        echo "📋 Fetching video metadata..."
        VIDEO_ID=$(yt-dlp --print "%(id)s" "$VIDEO_URL" 2>/dev/null)
        TITLE=$(yt-dlp --print "%(title)s" "$VIDEO_URL" 2>/dev/null)
        DURATION=$(yt-dlp --print "%(duration)s" "$VIDEO_URL" 2>/dev/null || echo "0")
        # Handle NA or empty duration (live videos)
        [ -z "$DURATION" ] || [ "$DURATION" = "NA" ] && DURATION=0
        UPLOAD_DATE=$(yt-dlp --print "%(upload_date)s" "$VIDEO_URL" 2>/dev/null)
        CHANNEL=$(yt-dlp --print "%(channel)s" "$VIDEO_URL" 2>/dev/null)
        
        # Output as JSON for easy parsing
        cat <<EOF
{
  "video_id": "$VIDEO_ID",
  "title": "$TITLE",
  "duration": $DURATION,
  "upload_date": "$UPLOAD_DATE",
  "channel": "$CHANNEL"
}
EOF
        ;;
        
    subtitles)
        # Download subtitles (auto-detects available languages)
        echo "📥 Downloading subtitles..."
        VIDEO_ID=$(yt-dlp --print "%(id)s" "$VIDEO_URL" 2>/dev/null)
        
        # Try bilingual first, fallback to single language
        yt-dlp --write-sub --write-auto-sub \
               --sub-lang "zh,zh-Hans,zh-CN,en" \
               --sub-format "vtt" \
               --skip-download \
               -o "/tmp/${VIDEO_ID}" \
               "$VIDEO_URL" 2>&1
        
        # Report what was downloaded
        echo ""
        echo "✅ Subtitles downloaded to /tmp/${VIDEO_ID}.*.vtt"
        ls -lh /tmp/${VIDEO_ID}.*.vtt 2>/dev/null || echo "⚠️ No subtitle files found"
        ;;
        
    audio)
        # Download audio with smart format selection
        echo "🎵 Downloading audio..."
        VIDEO_ID=$(yt-dlp --print "%(id)s" "$VIDEO_URL" 2>/dev/null)
        
        # Check for Chinese audio track first
        AUDIO_FORMAT=$(yt-dlp --list-formats "$VIDEO_URL" 2>&1 | grep -E "audio.*zh" | head -1 | awk '{print $1}')
        
        # Fallback to default track
        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT=$(yt-dlp --list-formats "$VIDEO_URL" 2>&1 | grep -E "^140-0|^140 " | head -1 | awk '{print $1}')
        fi
        
        # Ultimate fallback
        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT="bestaudio"
        fi
        
        echo "Using audio format: $AUDIO_FORMAT"
        yt-dlp -f "$AUDIO_FORMAT" -o "/tmp/${VIDEO_ID}.%(ext)s" "$VIDEO_URL"
        
        # Report what was downloaded
        AUDIO_FILE=$(ls /tmp/${VIDEO_ID}.* 2>/dev/null | grep -v vtt | head -1)
        echo ""
        echo "✅ Audio downloaded to: $AUDIO_FILE"
        ls -lh "$AUDIO_FILE"
        ;;
        
    *)
        echo "❌ Error: Invalid MODE '$MODE'"
        echo "Valid modes: metadata | subtitles | audio"
        exit 1
        ;;
esac
