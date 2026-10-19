#!/usr/bin/env bash
# update_yt_dlp.sh - Update yt-dlp when a newer release exists (best effort).
# YouTube keeps breaking older yt-dlp releases, so `run` calls this before it
# fetches a video's metadata. The GitHub check
# runs at most once an hour; a failed check or update never fails the caller.
# Set YT_TRANSCRIPT_YT_DLP_UPDATE=0 to skip it (the offline tests do).

if [ "${YT_TRANSCRIPT_YT_DLP_UPDATE:-1}" = "0" ]; then
    exit 0
fi
if ! command -v yt-dlp &> /dev/null; then
    exit 0
fi

# Return a file modification time in epoch seconds across macOS and Linux `stat`.
file_mtime_epoch() {
    local path="$1"
    if stat -f %m "$path" >/dev/null 2>&1; then
        stat -f %m "$path"
        return 0
    fi
    if stat -c %Y "$path" >/dev/null 2>&1; then
        stat -c %Y "$path"
        return 0
    fi
    echo 0
}

CURRENT_VERSION=$(yt-dlp --version)
echo "ℹ️  Current yt-dlp version: $CURRENT_VERSION"

# Version cache to avoid GitHub API rate limits (check at most once per hour)
VERSION_CACHE="/tmp/yt-dlp-version-cache"
CACHE_MAX_AGE=3600  # 1 hour in seconds

SHOULD_CHECK_UPDATE=true
if [ -f "$VERSION_CACHE" ]; then
    CACHE_AGE=$(($(date +%s) - $(file_mtime_epoch "$VERSION_CACHE")))
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

exit 0
