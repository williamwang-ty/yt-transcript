"""Source-language detection and subtitle track selection from yt-dlp metadata.

YouTube lists ~150 machine-translated caption tracks next to the real ones, so
picking "any zh track" routes English or Japanese videos into Chinese mode.
This module decides the video's spoken language first, keeps only tracks that
can be trusted for that language (human subtitles and the original-language
ASR track), and only then picks a source track and an output mode.
"""

from __future__ import annotations

import re
import urllib.parse


SUPPORTED_SOURCE_FAMILIES = ("zh", "en")
MODE_BY_FAMILY = {"zh": "chinese", "en": "bilingual"}

# Without caption URLs (``--list-subs`` output, old fixtures) a translation list
# can only be recognised by its size: YouTube offers 100+ targets at once.
_UNLABELLED_AUTO_TRACK_LIMIT = 10


def language_family(code: str | None) -> str:
    """Return the primary subtag of a language code (``zh-Hans`` -> ``zh``)."""
    text = str(code or "").strip().lower()
    if text.endswith("-orig"):
        text = text[:-5]
    return text.split("-", 1)[0].split("_", 1)[0]


def _first_entry(entries) -> dict:
    """Return the first caption entry dict, or an empty dict."""
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                return entry
    return {}


def _url_query(entry: dict) -> dict | None:
    """Return the caption URL query as a flat dict, or None when there is no URL."""
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {key: values[-1] for key, values in parsed.items() if values}


def _track_map(payload: dict, key: str) -> dict:
    """Return a subtitles map from metadata, dropping empty and live-chat entries."""
    raw = payload.get(key)
    if not isinstance(raw, dict):
        return {}
    return {
        str(lang): entries
        for lang, entries in raw.items()
        if entries and str(lang).strip() and str(lang) != "live_chat"
    }


def classify_tracks(payload: dict) -> dict:
    """Split yt-dlp caption maps into manual, original-ASR and translated tracks.

    yt-dlp repeats every manual track inside ``automatic_captions`` (without
    ``kind``), lists the original ASR track twice (``en`` and ``en-orig``, both
    ``kind=asr`` without ``tlang``), and adds one ``tlang=<target>`` entry per
    machine-translation target.
    """
    manual_map = _track_map(payload, "subtitles")
    auto_map = _track_map(payload, "automatic_captions")
    manual = list(manual_map)

    asr_tracks: list[dict] = []
    translated: list[str] = []
    unlabelled: list[str] = []
    for lang, entries in auto_map.items():
        query = _url_query(_first_entry(entries))
        if query is None:
            if lang in manual_map:
                continue
            unlabelled.append(lang)
            continue
        tlang = query.get("tlang", "")
        source_lang = query.get("lang", "")
        if tlang and language_family(tlang) != language_family(source_lang):
            translated.append(lang)
            continue
        if query.get("kind") != "asr" and lang in manual_map:
            continue  # manual track repeated under automatic_captions
        asr_tracks.append({"language": lang, "spoken": language_family(source_lang or lang)})

    if unlabelled:
        orig_families = {language_family(lang) for lang in unlabelled if lang.endswith("-orig")}
        for lang in unlabelled:
            family = language_family(lang)
            if orig_families:
                is_original = family in orig_families
            else:
                is_original = len(unlabelled) <= _UNLABELLED_AUTO_TRACK_LIMIT
            if is_original:
                asr_tracks.append({"language": lang, "spoken": family})
            else:
                translated.append(lang)

    # Prefer the plain key over its ``-orig`` twin so downloaded files keep the
    # usual ``<id>.<lang>.vtt`` name.
    plain_keys = {track["language"] for track in asr_tracks if not track["language"].endswith("-orig")}
    deduped = []
    for track in asr_tracks:
        lang = track["language"]
        if lang.endswith("-orig") and lang[:-5] in plain_keys:
            continue
        deduped.append(track)

    return {
        "manual": manual,
        "auto": list(auto_map),
        "asr": deduped,
        "translated": translated,
    }


def _original_audio_language(payload: dict) -> str:
    """Return the language of the audio track YouTube marks as original, if any."""
    formats = payload.get("formats")
    if not isinstance(formats, list):
        return ""
    for fmt in formats:
        if not isinstance(fmt, dict) or fmt.get("acodec") in (None, "none"):
            continue
        note = str(fmt.get("format_note") or "").lower()
        language = str(fmt.get("language") or "").strip()
        if language and "original" in note:
            return language
    return ""


def detect_source_language(payload: dict, tracks: dict | None = None) -> dict:
    """Decide the spoken language of a video from metadata signals, strongest first.

    1. the audio format YouTube labels ``original`` (survives dubbed tracks)
    2. the top-level ``language`` field
    3. the original-language ASR caption track
    4. the manual subtitle tracks, when they all share one language
    """
    tracks = tracks if tracks is not None else classify_tracks(payload)
    candidates = (
        ("original_audio_track", _original_audio_language(payload)),
        ("metadata_language", str(payload.get("language") or "").strip()),
    )
    for signal, value in candidates:
        if value:
            return {"language": language_family(value), "signal": signal, "raw": value}

    asr_families = list(dict.fromkeys(track["spoken"] for track in tracks["asr"]))
    if len(asr_families) == 1:
        return {"language": asr_families[0], "signal": "asr_track", "raw": tracks["asr"][0]["language"]}

    manual_families = list(dict.fromkeys(language_family(lang) for lang in tracks["manual"]))
    if len(manual_families) == 1:
        return {"language": manual_families[0], "signal": "manual_tracks", "raw": tracks["manual"][0]}

    return {"language": "", "signal": "unknown", "raw": ""}


def _family_tracks(languages: list[str], family: str) -> list[str]:
    """Return languages of one family, keeping their original order."""
    return [lang for lang in languages if language_family(lang) == family]


def select_subtitle_candidates(payload: dict) -> dict:
    """Return the detected source language and the ordered subtitle candidates.

    Manual Chinese subtitles come first for every video (human translations are
    the preferred Chinese source). Otherwise only tracks in the spoken language
    qualify; auto-translated tracks never do.
    """
    tracks = classify_tracks(payload)
    detected = detect_source_language(payload, tracks)
    source_family = detected["language"]
    asr_by_family: dict[str, list[str]] = {}
    for track in tracks["asr"]:
        asr_by_family.setdefault(track["spoken"], []).append(track["language"])

    candidates: list[dict] = []
    seen: set[str] = set()

    def add(languages: list[str], family: str, source_kind: str) -> None:
        for lang in languages:
            if lang in seen:
                continue
            seen.add(lang)
            candidates.append({"language": lang, "mode": MODE_BY_FAMILY[family], "source_kind": source_kind})

    add(_family_tracks(tracks["manual"], "zh"), "zh", "manual")
    if source_family in SUPPORTED_SOURCE_FAMILIES:
        add(_family_tracks(tracks["manual"], source_family), source_family, "manual")
        add(asr_by_family.get(source_family, []), source_family, "auto")
    elif not source_family:
        # Spoken language unknown: fall back to every trusted zh/en track.
        add(_family_tracks(tracks["manual"], "en"), "en", "manual")
        add(asr_by_family.get("zh", []), "zh", "auto")
        add(asr_by_family.get("en", []), "en", "auto")

    supported = None if not source_family else source_family in SUPPORTED_SOURCE_FAMILIES
    return {
        "tracks": tracks,
        "source_language": source_family,
        "source_language_signal": detected["signal"],
        "source_language_raw": detected["raw"],
        "source_language_supported": supported,
        "candidates": candidates,
    }


def build_subtitle_info(payload: dict, video_id: str = "", yt_dlp_runtime: dict | None = None) -> dict:
    """Build the ``download.sh subtitle-info`` JSON payload from yt-dlp metadata."""
    selection = select_subtitle_candidates(payload)
    tracks = selection["tracks"]
    candidates = selection["candidates"]
    preferred = candidates[0] if candidates else {}
    source_family = selection["source_language"]
    supported = selection["source_language_supported"]

    unsupported_reason = ""
    if supported is False and not candidates:
        unsupported_reason = (
            f"Spoken language '{source_family}' is not supported yet; only Chinese and English "
            "videos can be transcribed, and no manual Chinese subtitles are available"
        )

    return {
        "video_id": str(payload.get("id") or video_id or ""),
        "has_manual": bool(tracks["manual"]),
        "has_auto": bool(tracks["auto"]),
        "has_any": bool(candidates),
        "manual_languages": tracks["manual"],
        "automatic_languages": [track["language"] for track in tracks["asr"]],
        "ignored_translated_count": len(tracks["translated"]),
        "english_available": any(language_family(c["language"]) == "en" for c in candidates),
        "chinese_available": any(language_family(c["language"]) == "zh" for c in candidates),
        "listed_candidates": candidates,
        "preferred_source_language": preferred.get("language", ""),
        "preferred_source_kind": preferred.get("source_kind", ""),
        "mode": preferred.get("mode", ""),
        "source_language": source_family,
        "source_language_signal": selection["source_language_signal"],
        "source_language_supported": supported,
        "unsupported_reason": unsupported_reason,
        "yt_dlp_runtime": yt_dlp_runtime or {},
    }


_LIST_SUBS_ROW = re.compile(r"^([A-Za-z0-9_-]+)\s+")


def metadata_from_list_subs(text: str, video_id: str = "") -> dict:
    """Turn ``yt-dlp --list-subs`` output into a minimal metadata payload.

    The listing has no caption URLs, so translated tracks are recognised by the
    ``-orig`` marker or by the size of the automatic list.
    """
    section = None
    manual: dict[str, list] = {}
    automatic: dict[str, list] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if "available subtitles" in lower and "automatic" not in lower:
            section = manual
            continue
        if "available automatic captions" in lower:
            section = automatic
            continue
        if line.startswith("Language") or line.startswith("[") or section is None:
            continue
        if " has no subtitles" in lower or " has no automatic captions" in lower:
            continue
        match = _LIST_SUBS_ROW.match(line)
        if match:
            section.setdefault(match.group(1), [{"ext": "vtt"}])
    return {"id": video_id, "subtitles": manual, "automatic_captions": automatic}
