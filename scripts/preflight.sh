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

# 2. Check yt-dlp version and smart update (yt-dlp existence verified in step 1)
CURRENT_VERSION=$(yt-dlp --version)
echo "ℹ️  Current yt-dlp version: $CURRENT_VERSION"

# Version cache to avoid GitHub API rate limits (check at most once per hour)
VERSION_CACHE="/tmp/yt-dlp-version-cache"
CACHE_MAX_AGE=3600  # 1 hour in seconds

SHOULD_CHECK_UPDATE=true
if [ -f "$VERSION_CACHE" ]; then
    CACHE_AGE=$(($(date +%s) - $(stat -f %m "$VERSION_CACHE" 2>/dev/null || echo 0)))
    if [ "$CACHE_AGE" -lt "$CACHE_MAX_AGE" ]; then
        CACHED_VERSION=$(cat "$VERSION_CACHE")
        if [ "$CURRENT_VERSION" = "$CACHED_VERSION" ]; then
            echo "✅ yt-dlp is up to date (cached)"
            SHOULD_CHECK_UPDATE=false
        fi
    fi
fi

if [ "$SHOULD_CHECK_UPDATE" = true ]; then
    echo "🌐 Checking for yt-dlp updates..."
    # Fetch latest release tag from GitHub API
    LATEST_VERSION_JSON=$(curl -s --max-time 5 https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest)
    
    if [ -n "$LATEST_VERSION_JSON" ]; then
        # Parse tag_name using python to avoid jq dependency
        LATEST_VERSION=$(echo "$LATEST_VERSION_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('tag_name', ''))" 2>/dev/null)
        
        if [ -n "$LATEST_VERSION" ]; then
            if [ "$CURRENT_VERSION" != "$LATEST_VERSION" ]; then
                echo "📦 Update available: $LATEST_VERSION (current: $CURRENT_VERSION)"
                echo "   Updating yt-dlp..."
                if brew upgrade yt-dlp 2>/dev/null || pip install -U yt-dlp --break-system-packages 2>/dev/null; then
                    # Verify update success
                    NEW_VERSION=$(yt-dlp --version)
                    if [ "$NEW_VERSION" = "$LATEST_VERSION" ]; then
                        echo "✅ yt-dlp updated to $NEW_VERSION"
                        echo "$NEW_VERSION" > "$VERSION_CACHE"
                    else
                        echo "⚠️ Update ran but version is $NEW_VERSION (expected $LATEST_VERSION)"
                    fi
                else
                    echo "⚠️ Auto-update failed, please update manually: brew upgrade yt-dlp"
                fi
            else
                echo "✅ yt-dlp is up to date"
                echo "$CURRENT_VERSION" > "$VERSION_CACHE"
            fi
        else
            echo "⚠️  Could not parse latest version from GitHub"
        fi
    else
        echo "⚠️  Could not check for updates (network or API limit)"
    fi
fi

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
