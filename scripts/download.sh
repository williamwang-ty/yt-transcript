#!/usr/bin/env bash
set -euo pipefail

# download.sh - Unified download wrapper for yt-transcript skill
# Usage: ./download.sh <VIDEO_URL> <MODE>
# MODE: metadata | subtitle-info | subtitles | audio

if [ $# -lt 2 ]; then
    echo "Usage: $0 <VIDEO_URL> <MODE>"
    echo "MODE: metadata | subtitle-info | subtitles | audio"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/config.yaml"
VIDEO_URL="$1"
MODE="$2"

CONFIG_JSON=""
if [ -f "$CONFIG_FILE" ]; then
    CONFIG_JSON=$(python3 "$ROOT_DIR/yt_transcript_utils.py" load-config --config-path "$CONFIG_FILE" 2>/dev/null || true)
fi

YT_DLP_SOCKET_TIMEOUT_SEC="${YT_DLP_SOCKET_TIMEOUT_SEC:-}"
YT_DLP_RETRIES="${YT_DLP_RETRIES:-}"
YT_DLP_EXTRACTOR_RETRIES="${YT_DLP_EXTRACTOR_RETRIES:-}"
YT_DLP_COOKIES_FROM_BROWSER="${YT_DLP_COOKIES_FROM_BROWSER:-}"
YT_DLP_COOKIES_FILE="${YT_DLP_COOKIES_FILE:-}"
YTDLP_SESSION_BROWSER_COOKIES=""
YTDLP_SESSION_STATE_FILE="/tmp/yt_transcript_${$}_browser_session"
trap 'rm -f "$YTDLP_SESSION_STATE_FILE"' EXIT
YTDLP_CHROME_COOKIES_RETRY_MAX=3
YTDLP_DEFAULT_SOCKET_TIMEOUT_SEC=15
YTDLP_DEFAULT_RETRIES=1
YTDLP_DEFAULT_EXTRACTOR_RETRIES=1

if [ -n "$CONFIG_JSON" ]; then
    if [ -z "$YT_DLP_SOCKET_TIMEOUT_SEC" ]; then
        YT_DLP_SOCKET_TIMEOUT_SEC=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('yt_dlp_socket_timeout_sec',''))" 2>/dev/null)
    fi
    if [ -z "$YT_DLP_RETRIES" ]; then
        YT_DLP_RETRIES=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('yt_dlp_retries',''))" 2>/dev/null)
    fi
    if [ -z "$YT_DLP_EXTRACTOR_RETRIES" ]; then
        YT_DLP_EXTRACTOR_RETRIES=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('yt_dlp_extractor_retries',''))" 2>/dev/null)
    fi
    if [ -z "$YT_DLP_COOKIES_FROM_BROWSER" ]; then
        YT_DLP_COOKIES_FROM_BROWSER=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('yt_dlp_cookies_from_browser',''))" 2>/dev/null)
    fi
    if [ -z "$YT_DLP_COOKIES_FILE" ]; then
        YT_DLP_COOKIES_FILE=$(printf '%s' "$CONFIG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('yt_dlp_cookies_file',''))" 2>/dev/null)
    fi
fi

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

current_session_browser() {
    if [ -s "$YTDLP_SESSION_STATE_FILE" ]; then
        cat "$YTDLP_SESSION_STATE_FILE" 2>/dev/null || true
    elif [ -n "$YTDLP_SESSION_BROWSER_COOKIES" ]; then
        printf '%s' "$YTDLP_SESSION_BROWSER_COOKIES"
    fi
}

current_auth_strategy() {
    if [ -n "$YT_DLP_COOKIES_FILE" ]; then
        printf 'cookies_file'
    elif [ -n "$YT_DLP_COOKIES_FROM_BROWSER" ]; then
        printf 'configured_browser'
    elif [ -n "$(current_session_browser)" ]; then
        printf 'chrome_retry_session'
    else
        printf 'anonymous'
    fi
}

current_ytdlp_runtime_json() {
    local session_browser=""
    session_browser=$(current_session_browser)

    env \
        YT_DLP_SOCKET_TIMEOUT_SEC="$YT_DLP_SOCKET_TIMEOUT_SEC" \
        YT_DLP_RETRIES="$YT_DLP_RETRIES" \
        YT_DLP_EXTRACTOR_RETRIES="$YT_DLP_EXTRACTOR_RETRIES" \
        YT_DLP_COOKIES_FROM_BROWSER="$YT_DLP_COOKIES_FROM_BROWSER" \
        YT_DLP_COOKIES_FILE="$YT_DLP_COOKIES_FILE" \
        YTDLP_SESSION_BROWSER_COOKIES="$session_browser" \
        YTDLP_CHROME_COOKIES_RETRY_MAX="$YTDLP_CHROME_COOKIES_RETRY_MAX" \
        YTDLP_AUTH_STRATEGY="$(current_auth_strategy)" \
        python3 - <<'PY'
import json
import os

print(json.dumps({
    'socket_timeout_sec': int(os.environ.get('YT_DLP_SOCKET_TIMEOUT_SEC', '0') or 0),
    'retries': int(os.environ.get('YT_DLP_RETRIES', '0') or 0),
    'extractor_retries': int(os.environ.get('YT_DLP_EXTRACTOR_RETRIES', '0') or 0),
    'auth_strategy': os.environ.get('YTDLP_AUTH_STRATEGY', 'anonymous'),
    'cookies_from_browser': os.environ.get('YT_DLP_COOKIES_FROM_BROWSER', ''),
    'cookies_file': os.environ.get('YT_DLP_COOKIES_FILE', ''),
    'session_browser_cookies': os.environ.get('YTDLP_SESSION_BROWSER_COOKIES', ''),
    'chrome_retry_max': int(os.environ.get('YTDLP_CHROME_COOKIES_RETRY_MAX', '3') or 3),
}, ensure_ascii=False))
PY
}

is_impersonation_warning_line() {
    local text="$1"
    case "$text" in
        *"extractor specified to use impersonation for this download, but no impersonate target is available"*)
            return 0
            ;;
    esac
    return 1
}

emit_filtered_stderr() {
    local stderr_text="$1"
    local saw_impersonation=false
    [ -z "$stderr_text" ] && return 0
    while IFS= read -r line; do
        if is_impersonation_warning_line "$line"; then
            saw_impersonation=true
            continue
        fi
        printf '%s\n' "$line" >&2
    done <<< "$stderr_text"
    if [ "$saw_impersonation" = true ]; then
        echo "ℹ️  yt-dlp extractor impersonation is unavailable in this build; continuing without it." >&2
    fi
}

# Detect the common YouTube bot-verification failure strings returned by `yt-dlp`.
is_not_a_bot_error() {
    local text="$1"
    case "$text" in
        *"Sign in to confirm you're not a bot"*|*"Sign in to confirm you’re not a bot"*|*"LOGIN_REQUIRED"*)
            return 0
            ;;
    esac
    return 1
}

# Explain how to provide browser cookies when automatic retries still fail.
emit_cookies_import_guidance() {
    local attempts="${1:-}"
    local suffix=""
    if [[ "$attempts" =~ ^[0-9]+$ ]] && [ "$attempts" -gt 0 ]; then
        suffix=" after ${attempts} attempt(s)"
    fi

    echo "ℹ️  Automatic Chrome cookies retry failed${suffix}." >&2
    cat >&2 <<'EOF'
   Common causes:
   - Chrome is not installed in this environment
   - Chrome is installed but not logged into YouTube
   - This is a remote or container environment without browser profile access

   To continue, export a Netscape-format cookies.txt from a logged-in browser and configure one of:
   - config.yaml: yt_dlp_cookies_file: "/path/to/youtube_cookies.txt"
   - env var: YT_DLP_COOKIES_FILE=/path/to/youtube_cookies.txt

   Recommended import flow:
   1. Open YouTube in a logged-in browser on your local machine
   2. Export youtube.com cookies in Netscape cookies.txt format
   3. Copy the file to this machine/container
   4. Point yt-transcript to that file and retry
EOF
}

# Run `yt-dlp` with the currently resolved retry, timeout, and cookie flags.
run_yt_dlp_command() {
    local stdout_file="$1"
    local stderr_file="$2"
    shift 2

    local -a args=()
    if (( ${#YTDLP_ARGS[@]} )); then
        args+=("${YTDLP_ARGS[@]}")
    fi

    local session_browser=""
    session_browser=$(current_session_browser)
    if [ -n "$session_browser" ] && [ -z "$YT_DLP_COOKIES_FILE" ] && [ -z "$YT_DLP_COOKIES_FROM_BROWSER" ]; then
        args+=(--cookies-from-browser "$session_browser")
    fi

    local -a passthrough=("$@")
    local passthrough_count=${#passthrough[@]}
    local url="${passthrough[$((passthrough_count - 1))]}"
    unset 'passthrough[$((passthrough_count - 1))]'

    command yt-dlp ${passthrough[@]+"${passthrough[@]}"} ${args[@]+"${args[@]}"} "$url" >"$stdout_file" 2>"$stderr_file"
}

# Wrap `yt-dlp` with bot-check detection and a best-effort Chrome-cookies retry.
yt_dlp() {
    local stdout_file stderr_file status stderr_text
    stdout_file=$(mktemp)
    stderr_file=$(mktemp)

    if run_yt_dlp_command "$stdout_file" "$stderr_file" "$@"; then
        cat "$stdout_file"
        if [ -s "$stderr_file" ]; then
            emit_filtered_stderr "$(cat "$stderr_file")"
        fi
        rm -f "$stdout_file" "$stderr_file"
        return 0
    else
        status=$?
    fi

    stderr_text=$(cat "$stderr_file")
    rm -f "$stdout_file" "$stderr_file"

    if [ -z "$YT_DLP_COOKIES_FILE" ] && [ -z "$YT_DLP_COOKIES_FROM_BROWSER" ] && [ -z "$YTDLP_SESSION_BROWSER_COOKIES" ] && [ ! -s "$YTDLP_SESSION_STATE_FILE" ] && is_not_a_bot_error "$stderr_text"; then
        local attempt retry_stdout retry_stderr retry_status retry_stderr_text

        emit_filtered_stderr "$stderr_text"

        attempt=0
        retry_status="$status"
        while [ "$attempt" -lt "$YTDLP_CHROME_COOKIES_RETRY_MAX" ]; do
            attempt=$((attempt + 1))
            echo "ℹ️  yt-dlp hit YouTube bot verification; retrying with Chrome cookies (attempt ${attempt}/${YTDLP_CHROME_COOKIES_RETRY_MAX})..." >&2

            retry_stdout=$(mktemp)
            retry_stderr=$(mktemp)

            local -a retry_passthrough=("$@")
            local retry_count=${#retry_passthrough[@]}
            local retry_url="${retry_passthrough[$((retry_count - 1))]}"
            unset 'retry_passthrough[$((retry_count - 1))]'

            if command yt-dlp ${retry_passthrough[@]+"${retry_passthrough[@]}"} ${YTDLP_ARGS[@]+"${YTDLP_ARGS[@]}"} --cookies-from-browser chrome "$retry_url" >"$retry_stdout" 2>"$retry_stderr"; then
                YTDLP_SESSION_BROWSER_COOKIES="chrome"
                printf '%s' "$YTDLP_SESSION_BROWSER_COOKIES" > "$YTDLP_SESSION_STATE_FILE"
                cat "$retry_stdout"
                if [ -s "$retry_stderr" ]; then
                    emit_filtered_stderr "$(cat "$retry_stderr")"
                fi
                rm -f "$retry_stdout" "$retry_stderr"
                return 0
            else
                retry_status=$?
            fi

            retry_stderr_text=$(cat "$retry_stderr")
            rm -f "$retry_stdout" "$retry_stderr"

            if [ -n "$retry_stderr_text" ]; then
                emit_filtered_stderr "$retry_stderr_text"
            fi

            if ! is_not_a_bot_error "$retry_stderr_text"; then
                break
            fi
        done

        emit_cookies_import_guidance "$attempt"
        return "$retry_status"
    fi

    if [ -n "$stderr_text" ]; then
        emit_filtered_stderr "$stderr_text"
    fi
    return "$status"
}

# Extract the normalized YouTube video ID from a URL.
video_id_for_url() {
    yt_dlp --print "%(id)s" "$VIDEO_URL"
}

# Fail fast when a URL does not resolve to a plausible video ID.
require_valid_video_id() {
    local video_id="$1"
    if [ -z "$video_id" ]; then
        echo "❌ Error: Could not resolve a video ID for: $VIDEO_URL" >&2
        exit 1
    fi
    case "$video_id" in
        *[!A-Za-z0-9_-]*)
            echo "❌ Error: Resolved video ID contains unsafe characters: $video_id" >&2
            exit 1
            ;;
    esac
}

# Compute the work directory used for one video download/transcription job.
download_root_for_video() {
    local video_id="$1"
    printf '/tmp/%s_downloads' "$video_id"
}

# Reset the download directory so one acquisition mode starts from clean state.
reset_download_dir() {
    local dir="$1"
    rm -rf "$dir"
    mkdir -p "$dir"
}

# Derive structured subtitle availability information from `yt-dlp --dump-json` output.
emit_subtitle_info_from_metadata_json() {
    local video_id="$1"
    local metadata_json ytdlp_runtime_json
    metadata_json=$(cat)
    ytdlp_runtime_json=$(current_ytdlp_runtime_json)
    VIDEO_ID="$video_id" METADATA_JSON="$metadata_json" YTDLP_RUNTIME_JSON="$ytdlp_runtime_json" python3 - <<'PY'
import json
import os

video_id = os.environ.get('VIDEO_ID', '')
payload = json.loads(os.environ['METADATA_JSON'])
manual_map = payload.get('subtitles') if isinstance(payload.get('subtitles'), dict) else {}
auto_map = payload.get('automatic_captions') if isinstance(payload.get('automatic_captions'), dict) else {}
manual = [lang for lang, entries in manual_map.items() if entries]
automatic = [lang for lang, entries in auto_map.items() if entries]
if not manual and not automatic:
    raise SystemExit(1)

all_langs = manual + [lang for lang in automatic if lang not in manual]
english_like = [lang for lang in all_langs if lang == 'en' or lang.startswith('en-')]
chinese_like = [lang for lang in all_langs if lang == 'zh' or lang.startswith('zh-')]

preferred_source_language = ''
preferred_source_kind = ''
mode = ''
if english_like:
    preferred_source_language = english_like[0]
    preferred_source_kind = 'manual' if preferred_source_language in manual else 'auto'
    mode = 'bilingual'
elif chinese_like:
    preferred_source_language = chinese_like[0]
    preferred_source_kind = 'manual' if preferred_source_language in manual else 'auto'
    mode = 'chinese'

print(json.dumps({
    'video_id': payload.get('id') or video_id,
    'has_manual': bool(manual),
    'has_auto': bool(automatic),
    'has_any': bool(all_langs),
    'manual_languages': manual,
    'automatic_languages': automatic,
    'english_available': bool(english_like),
    'chinese_available': bool(chinese_like),
    'preferred_source_language': preferred_source_language,
    'preferred_source_kind': preferred_source_kind,
    'mode': mode,
    'yt_dlp_runtime': json.loads(os.environ.get('YTDLP_RUNTIME_JSON', '{}') or '{}'),
}, ensure_ascii=False))
PY
}

# Fallback parser for subtitle listings when full metadata JSON is unavailable.
emit_subtitle_info_from_list_output() {
    local video_id="$1"
    local list_output ytdlp_runtime_json
    list_output=$(cat)
    ytdlp_runtime_json=$(current_ytdlp_runtime_json)
    VIDEO_ID="$video_id" LIST_OUTPUT="$list_output" YTDLP_RUNTIME_JSON="$ytdlp_runtime_json" python3 - <<'PY'
import json
import os
import re

video_id = os.environ.get('VIDEO_ID', '')
text = os.environ.get('LIST_OUTPUT', '')
section = None
manual = []
automatic = []

for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line:
        continue
    lower = line.lower()
    if 'available subtitles' in lower and 'automatic' not in lower:
        section = 'manual'
        continue
    if 'available automatic captions' in lower:
        section = 'auto'
        continue
    if line.startswith('Language') or line.startswith('[info]'):
        continue
    match = re.match(r'^([A-Za-z0-9-]+)\s+', line)
    if not match or section is None:
        continue
    lang = match.group(1)
    if section == 'manual' and lang not in manual:
        manual.append(lang)
    elif section == 'auto' and lang not in automatic:
        automatic.append(lang)

all_langs = manual + [lang for lang in automatic if lang not in manual]
english_like = [lang for lang in all_langs if lang == 'en' or lang.startswith('en-')]
chinese_like = [lang for lang in all_langs if lang == 'zh' or lang.startswith('zh-')]

preferred_source_language = ''
preferred_source_kind = ''
mode = ''
if english_like:
    preferred_source_language = english_like[0]
    preferred_source_kind = 'manual' if preferred_source_language in manual else 'auto'
    mode = 'bilingual'
elif chinese_like:
    preferred_source_language = chinese_like[0]
    preferred_source_kind = 'manual' if preferred_source_language in manual else 'auto'
    mode = 'chinese'

print(json.dumps({
    'video_id': video_id,
    'has_manual': bool(manual),
    'has_auto': bool(automatic),
    'has_any': bool(all_langs),
    'manual_languages': manual,
    'automatic_languages': automatic,
    'english_available': bool(english_like),
    'chinese_available': bool(chinese_like),
    'preferred_source_language': preferred_source_language,
    'preferred_source_kind': preferred_source_kind,
    'mode': mode,
    'yt_dlp_runtime': json.loads(os.environ.get('YTDLP_RUNTIME_JSON', '{}') or '{}'),
}, ensure_ascii=False))
PY
}

# Choose the best available audio-only format from `yt-dlp` metadata JSON.
select_audio_format_from_metadata_json() {
    local metadata_json
    metadata_json=$(cat)
    METADATA_JSON="$metadata_json" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ['METADATA_JSON'])
formats = payload.get('formats') if isinstance(payload.get('formats'), list) else []
audio_only = []
for entry in formats:
    if not isinstance(entry, dict):
        continue
    acodec = str(entry.get('acodec') or '')
    vcodec = str(entry.get('vcodec') or '')
    if not acodec or acodec == 'none':
        continue
    if vcodec and vcodec != 'none':
        continue
    audio_only.append(entry)

if not audio_only:
    raise SystemExit(1)


def is_chinese(entry):
    language = str(entry.get('language') or '').lower()
    format_note = str(entry.get('format_note') or '').lower()
    name = str(entry.get('name') or '').lower()
    return language.startswith('zh') or ' chinese' in format_note or ' chinese' in name

preferred = next((entry for entry in audio_only if is_chinese(entry)), None)
if preferred is None:
    preferred = next((entry for entry in audio_only if str(entry.get('format_id') or '') == '140'), None)
if preferred is None:
    preferred = audio_only[0]

format_id = str(preferred.get('format_id') or '').strip()
if not format_id:
    raise SystemExit(1)
print(format_id)
PY
}

case "$MODE" in
    metadata)
        echo "📋 Fetching video metadata..." >&2
        VIDEO_ID=$(video_id_for_url)
        require_valid_video_id "$VIDEO_ID"
        if ! TITLE=$(yt_dlp --print "%(title)s" "$VIDEO_URL"); then
            exit 1
        fi
        DURATION=$(yt_dlp --print "%(duration)s" "$VIDEO_URL" 2>/dev/null || echo "0")
        [ -z "$DURATION" ] || [ "$DURATION" = "NA" ] && DURATION=0
        if ! UPLOAD_DATE=$(yt_dlp --print "%(upload_date)s" "$VIDEO_URL"); then
            exit 1
        fi
        if ! CHANNEL=$(yt_dlp --print "%(channel)s" "$VIDEO_URL"); then
            exit 1
        fi

        YTDLP_RUNTIME_JSON=$(current_ytdlp_runtime_json)
        VIDEO_ID="$VIDEO_ID" TITLE="$TITLE" DURATION="$DURATION" UPLOAD_DATE="$UPLOAD_DATE" CHANNEL="$CHANNEL" YTDLP_RUNTIME_JSON="$YTDLP_RUNTIME_JSON" \
            python3 - <<'PY'
import json
import os

print(json.dumps({
    'video_id': os.environ.get('VIDEO_ID', ''),
    'title': os.environ.get('TITLE', ''),
    'duration': int(os.environ.get('DURATION', '0') or 0),
    'upload_date': os.environ.get('UPLOAD_DATE', ''),
    'channel': os.environ.get('CHANNEL', ''),
    'yt_dlp_runtime': json.loads(os.environ.get('YTDLP_RUNTIME_JSON', '{}') or '{}'),
}, ensure_ascii=False))
PY
        ;;

    subtitle-info)
        echo "🔎 Inspecting subtitle availability..." >&2
        VIDEO_ID=$(video_id_for_url)
        require_valid_video_id "$VIDEO_ID"
        METADATA_JSON=$(yt_dlp -J "$VIDEO_URL" 2>/dev/null || true)
        if [ -n "$METADATA_JSON" ]; then
            if SUB_INFO_JSON=$(printf '%s' "$METADATA_JSON" | emit_subtitle_info_from_metadata_json "$VIDEO_ID" 2>/dev/null); then
                printf '%s\n' "$SUB_INFO_JSON"
                exit 0
            fi
        fi

        if ! LIST_OUTPUT=$(yt_dlp --list-subs "$VIDEO_URL" 2>&1); then
            printf '%s\n' "$LIST_OUTPUT" >&2
            exit 1
        fi

        printf '%s' "$LIST_OUTPUT" | emit_subtitle_info_from_list_output "$VIDEO_ID"
        ;;

    subtitles)
        echo "📥 Downloading subtitles..." >&2
        VIDEO_ID=$(video_id_for_url)
        require_valid_video_id "$VIDEO_ID"
        SUBTITLE_DIR="$(download_root_for_video "$VIDEO_ID")/subtitles"
        reset_download_dir "$SUBTITLE_DIR"

        SUB_INFO_JSON=$(bash "$SCRIPT_DIR/download.sh" "$VIDEO_URL" subtitle-info)
        if ! SUB_LANGS=$(SUB_INFO_JSON="$SUB_INFO_JSON" python3 - <<'PY'
import json
import os

sub_info = json.loads(os.environ['SUB_INFO_JSON'])
mode = sub_info.get('mode', '')
preferred_source_language = str(sub_info.get('preferred_source_language', '') or '').strip()
manual_languages = sub_info.get('manual_languages', [])
automatic_languages = sub_info.get('automatic_languages', [])
all_langs = manual_languages + [lang for lang in automatic_languages if lang not in manual_languages]
chinese_like = [lang for lang in all_langs if lang == 'zh' or lang.startswith('zh-')]

requested = []
if mode == 'bilingual' and preferred_source_language:
    requested.append(preferred_source_language)
    requested.extend(chinese_like)
elif mode == 'chinese' and preferred_source_language:
    requested.append(preferred_source_language)
else:
    raise SystemExit(1)

deduped = []
seen = set()
for lang in requested:
    normalized = str(lang or '').strip()
    if not normalized or normalized in seen:
        continue
    seen.add(normalized)
    deduped.append(normalized)

if not deduped:
    raise SystemExit(1)

print(','.join(deduped))
PY
); then
            echo "❌ Error: Subtitle workflow supports English or Chinese subtitle tracks only; use audio transcription fallback for other languages" >&2
            exit 1
        fi

        yt_dlp --write-sub --write-auto-sub \
               --sub-lang "$SUB_LANGS" \
               --sub-format "vtt" \
               --skip-download \
               -o "$SUBTITLE_DIR/${VIDEO_ID}" \
               "$VIDEO_URL" >&2

        YTDLP_RUNTIME_JSON=$(current_ytdlp_runtime_json)
        VIDEO_ID="$VIDEO_ID" SUB_INFO_JSON="$SUB_INFO_JSON" SUBTITLE_DIR="$SUBTITLE_DIR" YTDLP_RUNTIME_JSON="$YTDLP_RUNTIME_JSON" python3 - <<'PY'
import json
import os
from pathlib import Path

video_id = os.environ['VIDEO_ID']
sub_info = json.loads(os.environ['SUB_INFO_JSON'])
subtitle_dir = Path(os.environ['SUBTITLE_DIR'])
files = sorted(str(path) for path in subtitle_dir.glob(f'{video_id}.*.vtt'))
manual_languages = sub_info.get('manual_languages', [])
automatic_languages = sub_info.get('automatic_languages', [])
mode = sub_info.get('mode', '')
preferred_source_language = sub_info.get('preferred_source_language', '')
preferred_source_kind = sub_info.get('preferred_source_kind', '')

if not files:
    raise SystemExit('❌ Error: Subtitle download completed but no VTT files were found')


def subtitle_lang(path):
    name = Path(path).name
    prefix = f'{video_id}.'
    if not name.startswith(prefix) or not name.endswith('.vtt'):
        return ''
    return name[len(prefix):-4]


lang_to_files = {}
for path in files:
    lang = subtitle_lang(path)
    if not lang:
        continue
    lang_to_files.setdefault(lang, []).append(path)


def family_candidates(languages, family):
    exact = [lang for lang in languages if lang == family]
    variants = [lang for lang in languages if lang.startswith(f'{family}-')]
    return exact + variants


def choose_file():
    if mode == 'bilingual':
        family = 'en'
    elif mode == 'chinese':
        family = 'zh'
    else:
        family = preferred_source_language.split('-', 1)[0] if preferred_source_language else ''

    for source_kind, languages in (('manual', manual_languages), ('auto', automatic_languages)):
        for lang in family_candidates(languages, family):
            candidates = lang_to_files.get(lang, [])
            if candidates:
                return candidates[0], lang, source_kind

    if preferred_source_language:
        for source_kind, languages in (('manual', manual_languages), ('auto', automatic_languages)):
            if preferred_source_language in languages:
                candidates = lang_to_files.get(preferred_source_language, [])
                if candidates:
                    return candidates[0], preferred_source_language, source_kind

    fallback_lang = subtitle_lang(files[0])
    fallback_kind = preferred_source_kind or ''
    return files[0], fallback_lang, fallback_kind


selected_source_vtt, selected_source_language, selected_source_kind = choose_file()
english = [
    path for path in files
    if subtitle_lang(path) == 'en' or subtitle_lang(path).startswith('en-')
]
chinese = [
    path for path in files
    if subtitle_lang(path) == 'zh' or subtitle_lang(path).startswith('zh-')
]

print(json.dumps({
    'video_id': video_id,
    'download_dir': str(subtitle_dir),
    'downloaded_files': files,
    'english_files': english,
    'chinese_files': chinese,
    'selected_source_vtt': selected_source_vtt,
    'selected_source_language': selected_source_language,
    'selected_source_kind': selected_source_kind,
    'yt_dlp_runtime': json.loads(os.environ.get('YTDLP_RUNTIME_JSON', '{}') or '{}'),
}, ensure_ascii=False))
PY
        ;;

    audio)
        echo "🎵 Downloading audio..." >&2
        VIDEO_ID=$(video_id_for_url)
        require_valid_video_id "$VIDEO_ID"
        AUDIO_DIR="$(download_root_for_video "$VIDEO_ID")/audio"
        reset_download_dir "$AUDIO_DIR"

        AUDIO_FORMAT=""
        METADATA_JSON=$(yt_dlp -J "$VIDEO_URL" 2>/dev/null || true)
        if [ -n "$METADATA_JSON" ]; then
            AUDIO_FORMAT=$(printf '%s' "$METADATA_JSON" | select_audio_format_from_metadata_json 2>/dev/null || true)
        fi

        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT=$(yt_dlp --list-formats "$VIDEO_URL" 2>&1 | grep -E "audio.*zh" | head -1 | awk '{print $1}' || true)
        fi
        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT=$(yt_dlp --list-formats "$VIDEO_URL" 2>&1 | grep -E "^140-0|^140 " | head -1 | awk '{print $1}' || true)
        fi
        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT="bestaudio"
        fi

        echo "Using audio format: $AUDIO_FORMAT" >&2
        yt_dlp -f "$AUDIO_FORMAT" -o "$AUDIO_DIR/${VIDEO_ID}.%(ext)s" "$VIDEO_URL" >&2

        AUDIO_FILE=$(find "$AUDIO_DIR" -maxdepth 1 -type f ! -name '*.part' ! -name '*.ytdl' | sort | head -1 || true)
        if [ -z "$AUDIO_FILE" ]; then
            echo "❌ Error: Audio download completed but output file was not found" >&2
            exit 1
        fi

        YTDLP_RUNTIME_JSON=$(current_ytdlp_runtime_json)
        AUDIO_FILE="$AUDIO_FILE" VIDEO_ID="$VIDEO_ID" AUDIO_FORMAT="$AUDIO_FORMAT" AUDIO_DIR="$AUDIO_DIR" YTDLP_RUNTIME_JSON="$YTDLP_RUNTIME_JSON" python3 - <<'PY'
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
    'yt_dlp_runtime': json.loads(os.environ.get('YTDLP_RUNTIME_JSON', '{}') or '{}'),
}, ensure_ascii=False))
PY
        ;;

    *)
        echo "❌ Error: Invalid MODE '$MODE'"
        echo "Valid modes: metadata | subtitle-info | subtitles | audio"
        exit 1
        ;;
esac
