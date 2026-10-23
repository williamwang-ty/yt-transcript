#!/usr/bin/env bash
# update_yt_dlp.sh - Let Homebrew update yt-dlp, at most once an hour (best effort).
# YouTube keeps breaking older yt-dlp releases, so `run` calls this before it
# fetches a video's metadata, inside the machine-wide YouTube lock: no yt-dlp
# runs while Homebrew replaces it, and only one update runs at a time.
# It asks the installer, not GitHub: Homebrew packages a release a day or two
# after GitHub does, and that lag is normal. A yt-dlp that Homebrew does not
# manage is only reported (update it with the tool that installed it).
# The time of every attempt is kept in $YT_TRANSCRIPT_LOCK_DIR, whatever the
# result. Never fails the caller. YT_TRANSCRIPT_YT_DLP_UPDATE=0 skips it (the
# offline tests set it).

if [ "${YT_TRANSCRIPT_YT_DLP_UPDATE:-1}" = "0" ]; then
    exit 0
fi
YT_DLP_PATH=$(command -v yt-dlp) || exit 0

STAMP_DIR="${YT_TRANSCRIPT_LOCK_DIR:-/tmp/yt-transcript/.locks}"
STAMP="$STAMP_DIR/yt-dlp-update.stamp"
CHECK_EVERY_SEC=3600

# A file's modification time in epoch seconds, with macOS or GNU `stat`.
file_mtime_epoch() {
    if stat -f %m "$1" >/dev/null 2>&1; then
        stat -f %m "$1"
    elif stat -c %Y "$1" >/dev/null 2>&1; then
        stat -c %Y "$1"
    else
        echo 0
    fi
}

if [ -f "$STAMP" ] && [ $(( $(date +%s) - $(file_mtime_epoch "$STAMP") )) -lt "$CHECK_EVERY_SEC" ]; then
    exit 0
fi
mkdir -p "$STAMP_DIR" 2>/dev/null && touch "$STAMP" 2>/dev/null

VERSION=$(yt-dlp --version 2>/dev/null || echo "unknown version")
BREW_PREFIX=$(brew --prefix 2>/dev/null || true)
if [ -z "$BREW_PREFIX" ] || [[ "$YT_DLP_PATH" != "$BREW_PREFIX"/* ]]; then
    echo "ℹ️  yt-dlp $VERSION ($YT_DLP_PATH) is not managed by Homebrew; update it with the tool that installed it."
    exit 0
fi

echo "🍺 Asking Homebrew for a newer yt-dlp (installed: $VERSION)..."
if OUTPUT=$(brew upgrade yt-dlp 2>&1); then
    NEW_VERSION=$(yt-dlp --version 2>/dev/null || echo "unknown version")
    if [ "$NEW_VERSION" != "$VERSION" ]; then
        echo "✅ yt-dlp updated: $VERSION -> $NEW_VERSION"
    else
        echo "✅ yt-dlp $VERSION is the newest Homebrew has"
    fi
else
    echo "⚠️  brew upgrade yt-dlp failed; continuing with $VERSION: $(printf '%s\n' "$OUTPUT" | tail -1)"
fi
exit 0
