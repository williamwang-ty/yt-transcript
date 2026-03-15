#!/usr/bin/env bash
set -e

# preflight.sh - Pre-flight checks for yt-transcript skill
# Usage: ./preflight.sh [--require-deepgram] [--require-llm]
# Mode semantics:
#   base                -> metadata / subtitle-driven workflows
#   --require-deepgram  -> audio transcription path only
#   --require-llm       -> long-video chunk processing only
# The checks are intentionally staged: subtitle-only flows should not fail just
# because Deepgram or LLM settings are absent.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/config.yaml"
REQUIRE_DEEPGRAM=false
REQUIRE_LLM=false

while [ $# -gt 0 ]; do
    case "$1" in
        --require-deepgram)
            REQUIRE_DEEPGRAM=true
            ;;
        --require-llm)
            REQUIRE_LLM=true
            ;;
        *)
            echo "Usage: $0 [--require-deepgram] [--require-llm]"
            exit 1
            ;;
    esac
    shift
done

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
    if ! command -v curl &> /dev/null; then
        echo "⚠️  Skipping update check because curl is not installed"
        LATEST_VERSION_JSON=""
    else
        # Fetch latest release tag from GitHub API (best effort only)
        LATEST_VERSION_JSON=$(curl -fsS --max-time 5 https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest 2>/dev/null || true)
    fi

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
    echo "   Copy config.example.yaml to config.yaml, set output_dir,"
    echo "   and add Deepgram / LLM credentials only for the paths that need them."
    exit 1
fi
echo "✅ config.yaml found"

# 6. Load config once
CONFIG_JSON=$(python3 "$ROOT_DIR/yt_transcript_utils.py" load-config --config-path "$CONFIG_FILE")

# 7. Validate output_dir
OUTPUT_DIR=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('output_dir', ''))")
if [ -z "$OUTPUT_DIR" ]; then
    echo "❌ Error: output_dir is missing in config.yaml"
    exit 1
fi
if [ ! -d "$OUTPUT_DIR" ]; then
    echo "ℹ️  Creating output_dir: $OUTPUT_DIR"
    mkdir -p "$OUTPUT_DIR"
fi
echo "✅ output_dir is ready"

DEEPGRAM_API_KEY=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deepgram_api_key', ''))")
LLM_API_KEY=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('llm_api_key', ''))")
LLM_BASE_URL=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('llm_base_url', ''))")
LLM_MODEL=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('llm_model', ''))")

# 7. Deepgram checks only when required
if [ "$REQUIRE_DEEPGRAM" = true ]; then
    if [ -z "$DEEPGRAM_API_KEY" ] || [ "$DEEPGRAM_API_KEY" = "your_api_key_here" ] || [ "$DEEPGRAM_API_KEY" = "your_deepgram_api_key_here" ]; then
        echo "❌ Error: Deepgram API key not configured in config.yaml"
        exit 1
    fi
    echo "✅ Deepgram API key configured"

    echo "🌐 Testing Deepgram API connectivity..."
    if python3 "$ROOT_DIR/yt_transcript_utils.py" test-deepgram-api "$DEEPGRAM_API_KEY" > /dev/null 2>&1; then
        echo "✅ Deepgram API is reachable and key is valid"
    else
        echo "❌ Error: Deepgram API test failed. Check your API key or network connection"
        exit 1
    fi
else
    if [ -n "$DEEPGRAM_API_KEY" ] && [ "$DEEPGRAM_API_KEY" != "your_api_key_here" ] && [ "$DEEPGRAM_API_KEY" != "your_deepgram_api_key_here" ]; then
        echo "✅ Deepgram API key configured (not validated in base mode)"
    else
        echo "ℹ️  Deepgram API key not configured; subtitle-only workflows can still run"
    fi
fi

# 8. LLM checks only when required
if [ "$REQUIRE_LLM" = true ]; then
    if [ -z "$LLM_API_KEY" ] || [ -z "$LLM_BASE_URL" ] || [ -z "$LLM_MODEL" ]; then
        echo "❌ Error: LLM API is not fully configured in config.yaml"
        exit 1
    fi
    echo "✅ LLM API configuration is present"

    echo "🌐 Probing LLM API reachability..."
    if LLM_PROBE_JSON=$(python3 "$ROOT_DIR/yt_transcript_utils.py" test-llm-api --config-path "$CONFIG_FILE" 2>/dev/null); then
        LLM_PROBE_LATENCY=$(printf '%s' "$LLM_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('latency_ms', ''))")
        LLM_PROBE_URL=$(printf '%s' "$LLM_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('request_url', ''))")
        LLM_PROBE_STREAM=$(printf '%s' "$LLM_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('streaming_used', False))")
        echo "✅ LLM API probe succeeded (${LLM_PROBE_LATENCY} ms, stream=${LLM_PROBE_STREAM})"
        if [ -n "$LLM_PROBE_URL" ]; then
            echo "ℹ️  LLM request URL: $LLM_PROBE_URL"
        fi

        echo "🔢 Probing token count capability..."
        if TOKEN_PROBE_JSON=$(python3 "$ROOT_DIR/yt_transcript_utils.py" test-token-count --config-path "$CONFIG_FILE" 2>/dev/null); then
            TOKEN_COUNT=$(printf '%s' "$TOKEN_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token_count', ''))")
            TOKEN_SOURCE=$(printf '%s' "$TOKEN_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token_count_source', ''))")
            TOKEN_SUPPORTED=$(printf '%s' "$TOKEN_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('provider_supported', False))")
            TOKEN_URL=$(printf '%s' "$TOKEN_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('request_url', ''))")
            TOKEN_ERROR_TYPE=$(printf '%s' "$TOKEN_PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error_type', ''))")
            if [ "$TOKEN_SOURCE" = "provider" ] && [ "$TOKEN_SUPPORTED" = "True" ]; then
                echo "✅ Token count probe succeeded (${TOKEN_COUNT} tokens, source=${TOKEN_SOURCE})"
                if [ -n "$TOKEN_URL" ]; then
                    echo "ℹ️  Token count URL: $TOKEN_URL"
                fi
            else
                echo "ℹ️  Token count probe fell back to ${TOKEN_SOURCE:-local_estimate} (${TOKEN_ERROR_TYPE:-provider_unavailable})"
            fi
        else
            echo "ℹ️  Token count probe failed unexpectedly; chunk planning will use local estimate fallback"
        fi
    else
        echo "❌ Error: LLM API probe failed. Check your key, model, base URL, provider latency, or gateway settings"
        python3 "$ROOT_DIR/yt_transcript_utils.py" test-llm-api --config-path "$CONFIG_FILE"
        exit 1
    fi
elif [ -n "$LLM_API_KEY" ] && [ -n "$LLM_BASE_URL" ] && [ -n "$LLM_MODEL" ]; then
    echo "✅ LLM API configuration is present (not required for this run)"
else
    echo "ℹ️  LLM API configuration missing; long-video chunk processing will be unavailable"
fi

echo ""
echo "✅ All pre-flight checks passed!"
echo ""
