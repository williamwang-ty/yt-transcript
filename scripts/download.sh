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
VIDEO_URL="$1"
MODE="$2"

video_id_for_url() {
    yt-dlp --print "%(id)s" "$VIDEO_URL" 2>/dev/null
}

download_root_for_video() {
    local video_id="$1"
    printf '/tmp/%s_downloads' "$video_id"
}

reset_download_dir() {
    local dir="$1"
    rm -rf "$dir"
    mkdir -p "$dir"
}

emit_subtitle_info_from_metadata_json() {
    local video_id="$1"
    local metadata_json
    metadata_json=$(cat)
    VIDEO_ID="$video_id" METADATA_JSON="$metadata_json" python3 - <<'PY'
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
}, ensure_ascii=False))
PY
}

emit_subtitle_info_from_list_output() {
    local video_id="$1"
    local list_output
    list_output=$(cat)
    VIDEO_ID="$video_id" LIST_OUTPUT="$list_output" python3 - <<'PY'
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
}, ensure_ascii=False))
PY
}

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
        TITLE=$(yt-dlp --print "%(title)s" "$VIDEO_URL" 2>/dev/null)
        DURATION=$(yt-dlp --print "%(duration)s" "$VIDEO_URL" 2>/dev/null || echo "0")
        [ -z "$DURATION" ] || [ "$DURATION" = "NA" ] && DURATION=0
        UPLOAD_DATE=$(yt-dlp --print "%(upload_date)s" "$VIDEO_URL" 2>/dev/null)
        CHANNEL=$(yt-dlp --print "%(channel)s" "$VIDEO_URL" 2>/dev/null)

        VIDEO_ID="$VIDEO_ID" TITLE="$TITLE" DURATION="$DURATION" UPLOAD_DATE="$UPLOAD_DATE" CHANNEL="$CHANNEL" \
            python3 - <<'PY'
import json
import os

print(json.dumps({
    'video_id': os.environ.get('VIDEO_ID', ''),
    'title': os.environ.get('TITLE', ''),
    'duration': int(os.environ.get('DURATION', '0') or 0),
    'upload_date': os.environ.get('UPLOAD_DATE', ''),
    'channel': os.environ.get('CHANNEL', ''),
}, ensure_ascii=False))
PY
        ;;

    subtitle-info)
        echo "🔎 Inspecting subtitle availability..." >&2
        VIDEO_ID=$(video_id_for_url)
        METADATA_JSON=$(yt-dlp -J "$VIDEO_URL" 2>/dev/null || true)
        if [ -n "$METADATA_JSON" ]; then
            if SUB_INFO_JSON=$(printf '%s' "$METADATA_JSON" | emit_subtitle_info_from_metadata_json "$VIDEO_ID" 2>/dev/null); then
                printf '%s\n' "$SUB_INFO_JSON"
                exit 0
            fi
        fi

        if ! LIST_OUTPUT=$(yt-dlp --list-subs "$VIDEO_URL" 2>&1); then
            printf '%s\n' "$LIST_OUTPUT" >&2
            exit 1
        fi

        printf '%s' "$LIST_OUTPUT" | emit_subtitle_info_from_list_output "$VIDEO_ID"
        ;;

    subtitles)
        echo "📥 Downloading subtitles..." >&2
        VIDEO_ID=$(video_id_for_url)
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

        yt-dlp --write-sub --write-auto-sub \
               --sub-lang "$SUB_LANGS" \
               --sub-format "vtt" \
               --skip-download \
               -o "$SUBTITLE_DIR/${VIDEO_ID}" \
               "$VIDEO_URL" >&2

        VIDEO_ID="$VIDEO_ID" SUB_INFO_JSON="$SUB_INFO_JSON" SUBTITLE_DIR="$SUBTITLE_DIR" python3 - <<'PY'
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
}, ensure_ascii=False))
PY
        ;;

    audio)
        echo "🎵 Downloading audio..." >&2
        VIDEO_ID=$(video_id_for_url)
        AUDIO_DIR="$(download_root_for_video "$VIDEO_ID")/audio"
        reset_download_dir "$AUDIO_DIR"

        AUDIO_FORMAT=""
        METADATA_JSON=$(yt-dlp -J "$VIDEO_URL" 2>/dev/null || true)
        if [ -n "$METADATA_JSON" ]; then
            AUDIO_FORMAT=$(printf '%s' "$METADATA_JSON" | select_audio_format_from_metadata_json 2>/dev/null || true)
        fi

        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT=$(yt-dlp --list-formats "$VIDEO_URL" 2>&1 | grep -E "audio.*zh" | head -1 | awk '{print $1}' || true)
        fi
        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT=$(yt-dlp --list-formats "$VIDEO_URL" 2>&1 | grep -E "^140-0|^140 " | head -1 | awk '{print $1}' || true)
        fi
        if [ -z "$AUDIO_FORMAT" ]; then
            AUDIO_FORMAT="bestaudio"
        fi

        echo "Using audio format: $AUDIO_FORMAT" >&2
        yt-dlp -f "$AUDIO_FORMAT" -o "$AUDIO_DIR/${VIDEO_ID}.%(ext)s" "$VIDEO_URL" >&2

        AUDIO_FILE=$(find "$AUDIO_DIR" -maxdepth 1 -type f ! -name '*.part' ! -name '*.ytdl' | sort | head -1 || true)
        if [ -z "$AUDIO_FILE" ]; then
            echo "❌ Error: Audio download completed but output file was not found" >&2
            exit 1
        fi

        AUDIO_FILE="$AUDIO_FILE" VIDEO_ID="$VIDEO_ID" AUDIO_FORMAT="$AUDIO_FORMAT" AUDIO_DIR="$AUDIO_DIR" python3 - <<'PY'
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
}, ensure_ascii=False))
PY
        ;;

    *)
        echo "❌ Error: Invalid MODE '$MODE'"
        echo "Valid modes: metadata | subtitle-info | subtitles | audio"
        exit 1
        ;;
esac
