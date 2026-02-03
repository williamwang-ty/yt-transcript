#!/usr/bin/env bash
set -e

# preflight.sh - Pre-flight checks for yt-transcript skill
# Usage: ./preflight.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/../config.yaml"

echo "🔍 Running pre-flight checks..."

# 1. Check yt-dlp is installed
if ! command -v yt-dlp &> /dev/null; then
    echo "❌ Error: yt-dlp not found. Please install: pip install yt-dlp"
    exit 1
fi
echo "✅ yt-dlp is installed"

# 2. Check yt-dlp version and update if needed
echo "📦 Updating yt-dlp..."
brew upgrade yt-dlp 2>/dev/null || pip install -U yt-dlp --break-system-packages 2>/dev/null || echo "⚠️ Could not auto-update, please manually update yt-dlp"

# 3. Check ffmpeg is installed (needed for audio splitting)
if ! command -v ffmpeg &> /dev/null; then
    echo "⚠️ Warning: ffmpeg not found. Required for audio splitting (videos > 10MB)"
    echo "   Install with: brew install ffmpeg"
fi

# 4. Check Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 not found"
    exit 1
fi
echo "✅ python3 is available"

# 5. Check config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Error: config.yaml not found at $CONFIG_FILE"
    echo "   Copy config.example.yaml to config.yaml and add your Deepgram API key"
    exit 1
fi
echo "✅ config.yaml found"

# 6. Extract and validate Deepgram API key
DEEPGRAM_API_KEY=$(grep 'deepgram_api_key' "$CONFIG_FILE" | sed 's/.*: *"\(.*\)"/\1/')
if [ -z "$DEEPGRAM_API_KEY" ] || [ "$DEEPGRAM_API_KEY" = "your_api_key_here" ]; then
    echo "❌ Error: Deepgram API key not configured in config.yaml"
    exit 1
fi
echo "✅ Deepgram API key configured"

# 7. Test Deepgram API connectivity (quick validation)
echo "🌐 Testing Deepgram API connectivity..."
if python3 "$SCRIPT_DIR/../yt_transcript_utils.py" test-deepgram-api "$DEEPGRAM_API_KEY" > /dev/null 2>&1; then
    echo "✅ Deepgram API is reachable and key is valid"
else
    echo "❌ Error: Deepgram API test failed. Check your API key or network connection"
    exit 1
fi

echo ""
echo "✅ All pre-flight checks passed!"
echo ""
