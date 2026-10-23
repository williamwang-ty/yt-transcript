#!/usr/bin/env bash
set -euo pipefail

# download.sh - the yt-dlp calls of `yt_transcript_utils.py run`
# Usage: ./download.sh <VIDEO_URL> <MODE>
# MODE: metadata | subtitle-info | subtitles | audio
#
# Each yt-dlp call runs once: no retries here and no reading of yt-dlp's error
# text. `run` holds the machine-wide YouTube lock around this script, reads
# yt-dlp's errors (kernel/sources/youtube.py) and decides whether one retry with
# browser cookies makes sense. yt-dlp's own `-t sleep` preset paces the requests.
#
# One page extraction per video: `yt-dlp -J` runs once and its output is saved
# to $YT_TRANSCRIPT_INFO_JSON; every mode reads that file, and subtitles are
# downloaded from it (`--load-info-json`, no new page fetch). Audio downloads by
# URL, because its saved format URLs expire.

if [ $# -lt 2 ]; then
    echo "Usage: $0 <VIDEO_URL> <MODE>" >&2
    echo "MODE: metadata | subtitle-info | subtitles | audio" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VIDEO_URL="$1"
MODE="$2"

# yt-dlp settings; `run` sets them from config.yaml (yt_dlp_* keys).
YT_DLP_SOCKET_TIMEOUT_SEC="${YT_DLP_SOCKET_TIMEOUT_SEC:-}"
YT_DLP_RETRIES="${YT_DLP_RETRIES:-}"
YT_DLP_EXTRACTOR_RETRIES="${YT_DLP_EXTRACTOR_RETRIES:-}"
YT_DLP_COOKIES_FROM_BROWSER="${YT_DLP_COOKIES_FROM_BROWSER:-}"
YT_DLP_COOKIES_FILE="${YT_DLP_COOKIES_FILE:-}"
YTDLP_DEFAULT_SOCKET_TIMEOUT_SEC=15
YTDLP_DEFAULT_RETRIES=1
YTDLP_DEFAULT_EXTRACTOR_RETRIES=1
# Set by `run`: the saved `yt-dlp -J` output, the folder that holds subtitles/
# and audio/, and the output mode whose subtitle tracks may be used
# (chinese | bilingual; empty means any).
YT_TRANSCRIPT_INFO_JSON="${YT_TRANSCRIPT_INFO_JSON:-}"
YT_TRANSCRIPT_DOWNLOAD_DIR="${YT_TRANSCRIPT_DOWNLOAD_DIR:-}"
YT_TRANSCRIPT_SUBTITLE_MODE="${YT_TRANSCRIPT_SUBTITLE_MODE:-}"

TEMP_INFO_JSON=""
if [ -z "$YT_TRANSCRIPT_INFO_JSON" ]; then
    TEMP_INFO_JSON=$(mktemp)
    YT_TRANSCRIPT_INFO_JSON="$TEMP_INFO_JSON"
fi
trap 'rm -f ${TEMP_INFO_JSON:+"$TEMP_INFO_JSON"} "${YT_TRANSCRIPT_INFO_JSON}.tmp"' EXIT

resolve_positive_int() {
    local raw_value="$1"
    local fallback="$2"
    if [[ "$raw_value" =~ ^[0-9]+$ ]] && [ "$raw_value" -gt 0 ]; then
        printf '%s' "$raw_value"
    else
        printf '%s' "$fallback"
    fi
}

resolve_nonnegative_int() {
    local raw_value="$1"
    local fallback="$2"
    if [[ "$raw_value" =~ ^[0-9]+$ ]] && [ "$raw_value" -ge 0 ]; then
        printf '%s' "$raw_value"
    else
        printf '%s' "$fallback"
    fi
}

YT_DLP_SOCKET_TIMEOUT_SEC=$(resolve_positive_int "$YT_DLP_SOCKET_TIMEOUT_SEC" "$YTDLP_DEFAULT_SOCKET_TIMEOUT_SEC")
YT_DLP_RETRIES=$(resolve_nonnegative_int "$YT_DLP_RETRIES" "$YTDLP_DEFAULT_RETRIES")
YT_DLP_EXTRACTOR_RETRIES=$(resolve_nonnegative_int "$YT_DLP_EXTRACTOR_RETRIES" "$YTDLP_DEFAULT_EXTRACTOR_RETRIES")

declare -a YTDLP_ARGS=(
    --socket-timeout "$YT_DLP_SOCKET_TIMEOUT_SEC"
    --retries "$YT_DLP_RETRIES"
    --extractor-retries "$YT_DLP_EXTRACTOR_RETRIES"
)

if [ -n "$YT_DLP_COOKIES_FILE" ]; then
    YTDLP_ARGS+=(--cookies "$YT_DLP_COOKIES_FILE")
elif [ -n "$YT_DLP_COOKIES_FROM_BROWSER" ]; then
    YTDLP_ARGS+=(--cookies-from-browser "$YT_DLP_COOKIES_FROM_BROWSER")
fi

# yt-dlp's pacing preset (2026.10: --sleep-requests 0.75 --sleep-subtitles 5
# --sleep-interval 10 --max-sleep-interval 20). Releases before mid-2025 lack it;
# `--list-extractors` makes yt-dlp parse the option and exit without a request.
SLEEP_PRESET=false
if yt-dlp -t sleep --list-extractors >/dev/null 2>&1; then
    YTDLP_ARGS+=(-t sleep)
    SLEEP_PRESET=true
else
    echo "ℹ️  This yt-dlp ($(yt-dlp --version 2>/dev/null || echo 'unknown version')) has no '-t sleep' preset; running without its request pacing." >&2
fi

# The settings above, reported in every result.
runtime_json() {
    YT_DLP_SOCKET_TIMEOUT_SEC="$YT_DLP_SOCKET_TIMEOUT_SEC" YT_DLP_RETRIES="$YT_DLP_RETRIES" \
        YT_DLP_EXTRACTOR_RETRIES="$YT_DLP_EXTRACTOR_RETRIES" YT_DLP_COOKIES_FILE="$YT_DLP_COOKIES_FILE" \
        YT_DLP_COOKIES_FROM_BROWSER="$YT_DLP_COOKIES_FROM_BROWSER" SLEEP_PRESET="$SLEEP_PRESET" \
        python3 - <<'PY'
import json
import os

cookies_file = os.environ.get('YT_DLP_COOKIES_FILE', '')
browser = os.environ.get('YT_DLP_COOKIES_FROM_BROWSER', '')
print(json.dumps({
    'socket_timeout_sec': int(os.environ['YT_DLP_SOCKET_TIMEOUT_SEC']),
    'retries': int(os.environ['YT_DLP_RETRIES']),
    'extractor_retries': int(os.environ['YT_DLP_EXTRACTOR_RETRIES']),
    'auth_strategy': 'cookies_file' if cookies_file else 'browser_cookies' if browser else 'anonymous',
    'cookies_file': cookies_file,
    'cookies_from_browser': browser,
    'sleep_preset': os.environ.get('SLEEP_PRESET') == 'true',
}, ensure_ascii=False))
PY
}

# Pass yt-dlp's stderr on, except one harmless warning about impersonation.
emit_filtered_stderr() {
    local stderr_file="$1"
    local saw_impersonation=false
    local line
    [ -s "$stderr_file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *"extractor specified to use impersonation for this download, but no impersonate target is available"*)
                saw_impersonation=true
                continue
                ;;
        esac
        printf '%s\n' "$line" >&2
    done < "$stderr_file"
    if [ "$saw_impersonation" = true ]; then
        echo "ℹ️  yt-dlp extractor impersonation is unavailable in this build; continuing without it." >&2
    fi
}

# Run yt-dlp once with the settings above; stdout passes through.
yt_dlp() {
    local stderr_file status=0
    stderr_file=$(mktemp)
    command yt-dlp "$@" "${YTDLP_ARGS[@]}" 2>"$stderr_file" || status=$?
    emit_filtered_stderr "$stderr_file"
    rm -f "$stderr_file"
    return "$status"
}

# Make sure $YT_TRANSCRIPT_INFO_JSON holds the video's `yt-dlp -J` metadata,
# fetching it only when the file is missing or empty.
ensure_metadata_json() {
    if [ ! -s "$YT_TRANSCRIPT_INFO_JSON" ]; then
        yt_dlp -J "$VIDEO_URL" > "${YT_TRANSCRIPT_INFO_JSON}.tmp"
        mv "${YT_TRANSCRIPT_INFO_JSON}.tmp" "$YT_TRANSCRIPT_INFO_JSON"
    fi
}

# The video id from the saved metadata; fails unless it is a safe path component.
video_id_from_metadata() {
    python3 - "$YT_TRANSCRIPT_INFO_JSON" <<'PY'
import json
import re
import sys

try:
    payload = json.load(open(sys.argv[1], encoding='utf-8'))
except ValueError:
    payload = {}
video_id = str((payload if isinstance(payload, dict) else {}).get('id') or '').strip()
if not re.fullmatch(r'[A-Za-z0-9_-]+', video_id):
    print(f'❌ Error: Could not resolve a video ID from the yt-dlp metadata (got {video_id!r})', file=sys.stderr)
    raise SystemExit(1)
print(video_id)
PY
}

# Language detection and subtitle candidates for the saved metadata.
subtitle_info_json() {
    python3 "$ROOT_DIR/yt_transcript_utils.py" subtitle-info \
        --video-id "$1" \
        --yt-dlp-runtime-json "$(runtime_json)" < "$YT_TRANSCRIPT_INFO_JSON"
}

# The folder that holds one video's downloads.
download_root_for_video() {
    local video_id="$1"
    if [ -n "$YT_TRANSCRIPT_DOWNLOAD_DIR" ]; then
        printf '%s' "$YT_TRANSCRIPT_DOWNLOAD_DIR"
    else
        printf '/tmp/%s_downloads' "$video_id"
    fi
}

# Reset the download directory so one acquisition mode starts from clean state.
reset_download_dir() {
    local dir="$1"
    rm -rf "$dir"
    mkdir -p "$dir"
}

case "$MODE" in
    metadata)
        echo "📋 Fetching video metadata..." >&2
        ensure_metadata_json
        video_id_from_metadata >/dev/null
        YTDLP_RUNTIME_JSON="$(runtime_json)" python3 - "$YT_TRANSCRIPT_INFO_JSON" <<'PY'
import json
import os
import sys

payload = json.load(open(sys.argv[1], encoding='utf-8'))
try:
    duration = int(float(payload.get('duration') or 0))
except (TypeError, ValueError):
    duration = 0
print(json.dumps({
    'video_id': str(payload.get('id') or '').strip(),
    'title': str(payload.get('title') or ''),
    'duration': duration,
    'upload_date': str(payload.get('upload_date') or ''),
    'channel': str(payload.get('channel') or ''),
    'yt_dlp_runtime': json.loads(os.environ['YTDLP_RUNTIME_JSON']),
}, ensure_ascii=False))
PY
        ;;

    subtitle-info)
        echo "🔎 Inspecting subtitle availability..." >&2
        ensure_metadata_json
        VIDEO_ID=$(video_id_from_metadata)
        subtitle_info_json "$VIDEO_ID"
        ;;

    subtitles)
        echo "📥 Downloading subtitles..." >&2
        ensure_metadata_json
        VIDEO_ID=$(video_id_from_metadata)
        SUB_INFO_JSON=$(subtitle_info_json "$VIDEO_ID")
        SUBTITLE_DIR="$(download_root_for_video "$VIDEO_ID")/subtitles"
        reset_download_dir "$SUBTITLE_DIR"

        # One "language<TAB>mode<TAB>kind" row per trusted candidate, best first.
        if ! CANDIDATE_ROWS=$(SUB_INFO_JSON="$SUB_INFO_JSON" python3 - <<'PY'
import json
import os
import sys

sub_info = json.loads(os.environ['SUB_INFO_JSON'])
wanted_mode = os.environ.get('YT_TRANSCRIPT_SUBTITLE_MODE', '').strip()
rows = []
for candidate in sub_info.get('listed_candidates', []):
    fields = [str(candidate.get(key, '') or '').strip() for key in ('language', 'mode', 'source_kind')]
    if all(fields) and (not wanted_mode or fields[1] == wanted_mode):
        rows.append('\t'.join(fields))
if not rows:
    reason = str(sub_info.get('unsupported_reason', '') or '').strip()
    if reason:
        print(f'❌ Error: {reason}', file=sys.stderr)
    raise SystemExit(1)
print('\n'.join(rows))
PY
); then
            echo "❌ Error: No trusted Chinese or English subtitle track for this video" >&2
            exit 1
        fi

        RESOLVED_MODE=""
        RESOLVED_SOURCE_LANGUAGE=""
        RESOLVED_SOURCE_KIND=""
        ATTEMPT_ROWS=""
        WARNING_LINES=""
        while IFS=$'\t' read -r CANDIDATE_LANG CANDIDATE_MODE CANDIDATE_KIND; do
            [ -z "$CANDIDATE_LANG" ] && continue
            find "$SUBTITLE_DIR" -maxdepth 1 -type f -name "${VIDEO_ID}.*" -delete 2>/dev/null || true
            if DOWNLOAD_LOG=$(yt_dlp --load-info-json "$YT_TRANSCRIPT_INFO_JSON" \
                                     --write-sub --write-auto-sub \
                                     --sub-lang "$CANDIDATE_LANG" \
                                     --sub-format "vtt" \
                                     --skip-download \
                                     -o "$SUBTITLE_DIR/${VIDEO_ID}" 2>&1); then
                if [ -n "$DOWNLOAD_LOG" ]; then printf '%s\n' "$DOWNLOAD_LOG" >&2; fi
                RESOLVED_MODE="$CANDIDATE_MODE"
                RESOLVED_SOURCE_LANGUAGE="$CANDIDATE_LANG"
                RESOLVED_SOURCE_KIND="$CANDIDATE_KIND"
                ATTEMPT_ROWS="${ATTEMPT_ROWS}${CANDIDATE_LANG}"$'\t'"${CANDIDATE_MODE}"$'\t'"${CANDIDATE_KIND}"$'\t'"selected"$'\t'$'\n'
                break
            fi
            if [ -n "$DOWNLOAD_LOG" ]; then printf '%s\n' "$DOWNLOAD_LOG" >&2; fi
            # yt-dlp's last ERROR line (else its last line); `run` reads what it means.
            ERROR_LINE=$(printf '%s\n' "$DOWNLOAD_LOG" | tr '\t' ' ' \
                | awk 'NF { last = $0 } /^ERROR/ { error = $0 } END { print (error != "" ? error : last) }')
            ATTEMPT_ROWS="${ATTEMPT_ROWS}${CANDIDATE_LANG}"$'\t'"${CANDIDATE_MODE}"$'\t'"${CANDIDATE_KIND}"$'\t'"blocked"$'\t'"${ERROR_LINE}"$'\n'
            WARNING_MESSAGE="subtitle download failed for preferred candidate '$CANDIDATE_LANG'; trying next available source track"
            echo "⚠️  $WARNING_MESSAGE" >&2
            WARNING_LINES="${WARNING_LINES}${WARNING_MESSAGE}"$'\n'
        done <<< "$CANDIDATE_ROWS"

        if [ -z "$RESOLVED_SOURCE_LANGUAGE" ]; then
            echo "❌ Error: No usable English or Chinese subtitle track could be downloaded; use audio transcription fallback" >&2
            exit 1
        fi

        VIDEO_ID="$VIDEO_ID" SUB_INFO_JSON="$SUB_INFO_JSON" SUBTITLE_DIR="$SUBTITLE_DIR" \
            YTDLP_RUNTIME_JSON="$(runtime_json)" WARNING_LINES="$WARNING_LINES" RESOLVED_MODE="$RESOLVED_MODE" \
            RESOLVED_SOURCE_LANGUAGE="$RESOLVED_SOURCE_LANGUAGE" RESOLVED_SOURCE_KIND="$RESOLVED_SOURCE_KIND" \
            ATTEMPT_ROWS="$ATTEMPT_ROWS" python3 - <<'PY'
import json
import os
from pathlib import Path

video_id = os.environ['VIDEO_ID']
sub_info = json.loads(os.environ['SUB_INFO_JSON'])
subtitle_dir = Path(os.environ['SUBTITLE_DIR'])
files = sorted(str(path) for path in subtitle_dir.glob(f'{video_id}.*.vtt'))
if not files:
    raise SystemExit('❌ Error: Subtitle download completed but no VTT files were found')

resolved_language = os.environ['RESOLVED_SOURCE_LANGUAGE']
wanted = str(subtitle_dir / f'{video_id}.{resolved_language}.vtt')
selected_vtt = wanted if wanted in files else files[0]
selected_language = Path(selected_vtt).name[len(video_id) + 1:-len('.vtt')]

attempted, blocked = [], []
for row in os.environ.get('ATTEMPT_ROWS', '').splitlines():
    parts = row.split('\t')
    if len(parts) != 5:
        continue
    language, mode, source_kind, status, error = parts
    candidate = {'language': language, 'mode': mode, 'source_kind': source_kind, 'status': status}
    if status == 'blocked':
        candidate['error'] = error
        blocked.append(candidate)
    attempted.append(candidate)

preferred_language = sub_info.get('preferred_source_language', '')
print(json.dumps({
    'video_id': video_id,
    'download_dir': str(subtitle_dir),
    'listed_candidates': sub_info.get('listed_candidates', []),
    'downloaded_files': files,
    'preferred_source_language': preferred_language,
    'preferred_source_kind': sub_info.get('preferred_source_kind', ''),
    'preferred_mode': str(sub_info.get('mode', '') or '').strip(),
    'attempted_candidates': attempted,
    'blocked_candidates': blocked,
    'selected_source_vtt': selected_vtt,
    'selected_source_language': selected_language,
    'selected_source_kind': os.environ['RESOLVED_SOURCE_KIND'] or sub_info.get('preferred_source_kind', ''),
    'resolved_mode': os.environ['RESOLVED_MODE'] or str(sub_info.get('mode', '') or '').strip(),
    'fallback_used': bool(preferred_language and selected_language and preferred_language != selected_language),
    'warnings': [line for line in os.environ.get('WARNING_LINES', '').splitlines() if line.strip()],
    'yt_dlp_runtime': json.loads(os.environ['YTDLP_RUNTIME_JSON']),
}, ensure_ascii=False))
PY
        ;;

    audio)
        echo "🎵 Downloading audio..." >&2
        ensure_metadata_json
        VIDEO_ID=$(video_id_from_metadata)
        AUDIO_DIR="$(download_root_for_video "$VIDEO_ID")/audio"
        reset_download_dir "$AUDIO_DIR"

        # An audio-only format: one in Chinese if there is one, else 140 (m4a), else the first.
        AUDIO_FORMAT=$(python3 - "$YT_TRANSCRIPT_INFO_JSON" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding='utf-8'))
formats = payload.get('formats') if isinstance(payload.get('formats'), list) else []
audio_only = [entry for entry in formats if isinstance(entry, dict)
              and str(entry.get('acodec') or 'none') != 'none' and str(entry.get('vcodec') or 'none') == 'none']


def is_chinese(entry):
    language = str(entry.get('language') or '').lower()
    return language.startswith('zh') or ' chinese' in str(entry.get('format_note') or '').lower() \
        or ' chinese' in str(entry.get('name') or '').lower()


preferred = next((entry for entry in audio_only if is_chinese(entry)), None) \
    or next((entry for entry in audio_only if str(entry.get('format_id') or '') == '140'), None) \
    or (audio_only[0] if audio_only else {})
print(str(preferred.get('format_id') or '').strip() or 'bestaudio')
PY
)

        echo "Using audio format: $AUDIO_FORMAT" >&2
        yt_dlp -f "$AUDIO_FORMAT" -o "$AUDIO_DIR/${VIDEO_ID}.%(ext)s" "$VIDEO_URL" >&2

        AUDIO_FILE=$(find "$AUDIO_DIR" -maxdepth 1 -type f ! -name '*.part' ! -name '*.ytdl' | sort | head -1 || true)
        if [ -z "$AUDIO_FILE" ]; then
            echo "❌ Error: Audio download completed but output file was not found" >&2
            exit 1
        fi

        AUDIO_FILE="$AUDIO_FILE" VIDEO_ID="$VIDEO_ID" AUDIO_FORMAT="$AUDIO_FORMAT" AUDIO_DIR="$AUDIO_DIR" \
            YTDLP_RUNTIME_JSON="$(runtime_json)" python3 - <<'PY'
import json
import os
from pathlib import Path

audio_file = Path(os.environ['AUDIO_FILE'])
print(json.dumps({
    'video_id': os.environ['VIDEO_ID'],
    'download_dir': os.environ['AUDIO_DIR'],
    'audio_file': str(audio_file),
    'audio_format': os.environ['AUDIO_FORMAT'],
    'extension': audio_file.suffix.lstrip('.'),
    'size_bytes': audio_file.stat().st_size,
    'yt_dlp_runtime': json.loads(os.environ['YTDLP_RUNTIME_JSON']),
}, ensure_ascii=False))
PY
        ;;

    *)
        echo "❌ Error: Invalid MODE '$MODE'" >&2
        echo "Valid modes: metadata | subtitle-info | subtitles | audio" >&2
        exit 1
        ;;
esac
