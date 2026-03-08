#!/usr/bin/env bash
set -e

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

case "$MODE" in
    metadata)
        # Extract VIDEO_ID and metadata
        echo "📋 Fetching video metadata..." >&2
        VIDEO_ID=$(yt-dlp --print "%(id)s" "$VIDEO_URL" 2>/dev/null)
        TITLE=$(yt-dlp --print "%(title)s" "$VIDEO_URL" 2>/dev/null)
        DURATION=$(yt-dlp --print "%(duration)s" "$VIDEO_URL" 2>/dev/null || echo "0")
        # Handle NA or empty duration (live videos)
        [ -z "$DURATION" ] || [ "$DURATION" = "NA" ] && DURATION=0
        UPLOAD_DATE=$(yt-dlp --print "%(upload_date)s" "$VIDEO_URL" 2>/dev/null)
        CHANNEL=$(yt-dlp --print "%(channel)s" "$VIDEO_URL" 2>/dev/null)

        VIDEO_ID="$VIDEO_ID" TITLE="$TITLE" DURATION="$DURATION" UPLOAD_DATE="$UPLOAD_DATE" CHANNEL="$CHANNEL" \
            python3 - <<'PY'
import json
import os

print(json.dumps({
    "video_id": os.environ.get("VIDEO_ID", ""),
    "title": os.environ.get("TITLE", ""),
    "duration": int(os.environ.get("DURATION", "0") or 0),
    "upload_date": os.environ.get("UPLOAD_DATE", ""),
    "channel": os.environ.get("CHANNEL", ""),
}, ensure_ascii=False))
PY
        ;;

    subtitle-info)
        echo "🔎 Inspecting subtitle availability..." >&2
        VIDEO_ID=$(yt-dlp --print "%(id)s" "$VIDEO_URL" 2>/dev/null)
        if ! LIST_OUTPUT=$(yt-dlp --list-subs "$VIDEO_URL" 2>&1); then
            printf '%s\n' "$LIST_OUTPUT" >&2
            exit 1
        fi

        VIDEO_ID="$VIDEO_ID" python3 -c '
import json
import os
import re
import sys

video_id = os.environ.get("VIDEO_ID", "")
text = sys.stdin.read()
section = None
manual = []
automatic = []

for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line:
        continue
    lower = line.lower()
    if "available subtitles" in lower and "automatic" not in lower:
        section = "manual"
        continue
    if "available automatic captions" in lower:
        section = "auto"
        continue
    if line.startswith("Language") or line.startswith("[info]"):
        continue
    match = re.match(r"^([A-Za-z0-9-]+)\s+", line)
    if not match or section is None:
        continue
    lang = match.group(1)
    if section == "manual" and lang not in manual:
        manual.append(lang)
    elif section == "auto" and lang not in automatic:
        automatic.append(lang)

all_langs = manual + [lang for lang in automatic if lang not in manual]
english_like = [lang for lang in all_langs if lang.startswith("en")]
chinese_like = [lang for lang in all_langs if lang.startswith("zh")]

preferred_source_language = ""
preferred_source_kind = ""
mode = ""
if english_like:
    preferred_source_language = english_like[0]
    preferred_source_kind = "manual" if preferred_source_language in manual else "auto"
    mode = "bilingual"
elif chinese_like:
    preferred_source_language = chinese_like[0]
    preferred_source_kind = "manual" if preferred_source_language in manual else "auto"
    mode = "chinese"
elif all_langs:
    preferred_source_language = all_langs[0]
    preferred_source_kind = "manual" if preferred_source_language in manual else "auto"
    mode = "chinese"

print(json.dumps({
    "video_id": video_id,
    "has_manual": bool(manual),
    "has_auto": bool(automatic),
    "has_any": bool(all_langs),
    "manual_languages": manual,
    "automatic_languages": automatic,
    "english_available": bool(english_like),
    "chinese_available": bool(chinese_like),
    "preferred_source_language": preferred_source_language,
    "preferred_source_kind": preferred_source_kind,
    "mode": mode,
}, ensure_ascii=False))
' <<<"$LIST_OUTPUT"
        ;;
        
    subtitles)
        # Download subtitles (auto-detects available languages)
        echo "📥 Downloading subtitles..." >&2
        VIDEO_ID=$(yt-dlp --print "%(id)s" "$VIDEO_URL" 2>/dev/null)
        
        # Download English + Chinese families so the workflow can choose deterministically.
        yt-dlp --write-sub --write-auto-sub \
               --sub-lang "en,en-orig,en-US,zh,zh-Hans,zh-CN,zh-Hant" \
               --sub-format "vtt" \
               --skip-download \
               -o "/tmp/${VIDEO_ID}" \
               "$VIDEO_URL" >&2

        SUB_INFO_JSON=$(bash "$SCRIPT_DIR/download.sh" "$VIDEO_URL" subtitle-info)
        
        VIDEO_ID="$VIDEO_ID" SUB_INFO_JSON="$SUB_INFO_JSON" python3 - <<'PY'
import glob
import json
import os
from pathlib import Path

video_id = os.environ["VIDEO_ID"]
sub_info = json.loads(os.environ["SUB_INFO_JSON"])
files = sorted(glob.glob(f"/tmp/{video_id}.*.vtt"))
manual_languages = sub_info.get("manual_languages", [])
automatic_languages = sub_info.get("automatic_languages", [])
mode = sub_info.get("mode", "")
preferred_source_language = sub_info.get("preferred_source_language", "")
preferred_source_kind = sub_info.get("preferred_source_kind", "")


def subtitle_lang(path: str) -> str:
    name = Path(path).name
    prefix = f"{video_id}."
    if not name.startswith(prefix) or not name.endswith(".vtt"):
        return ""
    return name[len(prefix):-4]


lang_to_files = {}
for path in files:
    lang = subtitle_lang(path)
    if not lang:
        continue
    lang_to_files.setdefault(lang, []).append(path)


def family_candidates(languages: list[str], family: str) -> list[str]:
    exact = [lang for lang in languages if lang == family]
    variants = [lang for lang in languages if lang.startswith(f"{family}-")]
    return exact + variants


def choose_file() -> tuple[str, str, str]:
    if mode == "bilingual":
        family = "en"
    elif mode == "chinese":
        family = "zh"
    else:
        family = preferred_source_language.split("-", 1)[0] if preferred_source_language else ""

    for source_kind, languages in (("manual", manual_languages), ("auto", automatic_languages)):
        for lang in family_candidates(languages, family):
            candidates = lang_to_files.get(lang, [])
            if candidates:
                return candidates[0], lang, source_kind

    if preferred_source_language:
        for source_kind, languages in (("manual", manual_languages), ("auto", automatic_languages)):
            if preferred_source_language in languages:
                candidates = lang_to_files.get(preferred_source_language, [])
                if candidates:
                    return candidates[0], preferred_source_language, source_kind

    if files:
        fallback_lang = subtitle_lang(files[0])
        fallback_kind = preferred_source_kind or ""
        return files[0], fallback_lang, fallback_kind

    return "", "", ""


selected_source_vtt, selected_source_language, selected_source_kind = choose_file()
english = [
    path for path in files
    if subtitle_lang(path) == "en" or subtitle_lang(path).startswith("en-")
]
chinese = [
    path for path in files
    if subtitle_lang(path) == "zh" or subtitle_lang(path).startswith("zh-")
]

print(json.dumps({
    "video_id": video_id,
    "downloaded_files": files,
    "english_files": english,
    "chinese_files": chinese,
    "selected_source_vtt": selected_source_vtt,
    "selected_source_language": selected_source_language,
    "selected_source_kind": selected_source_kind,
}, ensure_ascii=False))
PY
        ;;
        
    audio)
        # Download audio with smart format selection
        echo "🎵 Downloading audio..." >&2
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
        
        echo "Using audio format: $AUDIO_FORMAT" >&2
        yt-dlp -f "$AUDIO_FORMAT" -o "/tmp/${VIDEO_ID}.%(ext)s" "$VIDEO_URL" >&2
        
        AUDIO_FILE=$(find /tmp -maxdepth 1 -type f -name "${VIDEO_ID}.*" ! -name "*.vtt" | head -1)
        if [ -z "$AUDIO_FILE" ]; then
            echo "❌ Error: Audio download completed but output file was not found" >&2
            exit 1
        fi

        AUDIO_FILE="$AUDIO_FILE" VIDEO_ID="$VIDEO_ID" AUDIO_FORMAT="$AUDIO_FORMAT" python3 - <<'PY'
import json
import os
from pathlib import Path

audio_file = Path(os.environ["AUDIO_FILE"])
print(json.dumps({
    "video_id": os.environ["VIDEO_ID"],
    "audio_file": str(audio_file),
    "audio_format": os.environ["AUDIO_FORMAT"],
    "extension": audio_file.suffix.lstrip("."),
    "size_bytes": audio_file.stat().st_size,
}, ensure_ascii=False))
PY
        ;;
        
    *)
        echo "❌ Error: Invalid MODE '$MODE'"
        echo "Valid modes: metadata | subtitle-info | subtitles | audio"
        exit 1
        ;;
esac
