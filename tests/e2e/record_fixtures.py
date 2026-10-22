#!/usr/bin/env python3
"""Record end-to-end fixtures from real YouTube videos (run on a machine that can reach YouTube).

For each case it saves, under <out>/<video_id>/:

    info.json        `yt-dlp -J` output trimmed to what the pipeline reads
                     (metadata, subtitle track lists, audio formats; no stream URLs)
    subs/<lang>.vtt  the subtitle track download.sh selected, cut to --max-minutes
    deepgram.json    one raw Deepgram response for videos without trusted subtitles,
                     cut to --max-minutes and with unused word fields dropped

and writes <out>/videos.json, a video set for scripts/run_regression.py.

Usage:
    python3 tests/e2e/record_fixtures.py --config ~/.agent/shared/skills/yt-transcript/config.yaml
    python3 tests/e2e/record_fixtures.py --only CIatbV3_Rjg --deepgram-json DQGrPstFRVI=/tmp/x/DQGrPstFRVI_deepgram.json

--config is read for the Deepgram API key and yt-dlp cookie settings only.
--deepgram-json reuses a response saved by an earlier run instead of paying
for a new one (a raw response, a chunk cache envelope, or a split-mode file,
whose first chunk is used).

--offline-deepgram SOURCE_ID needs no network or key: it adds the case
OFFLINE_DEEPGRAM_ID, a copy of an already recorded video with its subtitle
tracks removed and a Deepgram-shaped response built from its subtitle file
(one sentence per cue), so the no-subtitles -> Deepgram path runs on real
speech text. Use it when a real Deepgram recording is not available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from kernel import config as kernel_config  # noqa: E402
from kernel.sources import deepgram as kernel_deepgram  # noqa: E402

DOWNLOAD_SH = ROOT_DIR / "scripts" / "download.sh"
DEFAULT_OUT = ROOT_DIR / "tests" / "fixtures" / "e2e"

# One video per routing case, from tests/live_regression/videos.json.
CASES = (
    {"id": "Fhx_qNS2P8g", "case": "zh_manual_long", "expected_case": "zh_manual", "max_minutes": 30,
     "label": "Long Chinese podcast, manual zh-Hans (硅谷101 E255); window runs several rounds with an outline"},
    {"id": "CIatbV3_Rjg", "case": "en_manual_bilingual", "expected_case": "en_manual", "max_minutes": 0,
     "label": "English, manual en, no Chinese track (Lex Clips); bilingual mode"},
    {"id": "DQGrPstFRVI", "case": "deepgram_zh", "expected_case": "deepgram", "max_minutes": 8,
     "label": "Chinese speech, no subtitles at all (老高與小茉); Deepgram"},
    {"id": "tGVSOK3PNUI", "case": "unsupported_ja", "expected_case": "unsupported", "max_minutes": 0,
     "label": "Japanese, auto ja-orig only; unsupported"},
)

INFO_KEYS = ("id", "title", "fulltitle", "channel", "uploader", "upload_date", "duration", "language",
             "webpage_url", "chapters")
FORMAT_KEYS = ("format_id", "ext", "acodec", "vcodec", "language", "language_preference", "format_note",
               "audio_channels", "abr")
TRACK_QUERY_KEYS = ("lang", "tlang", "kind")
WORD_KEYS = ("word", "punctuated_word", "start", "end", "speaker")
OFFLINE_DEEPGRAM_ID = "E2Edeepgram"
OFFLINE_DEEPGRAM_MINUTES = 8
CUE_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[.,](\d{3})\s+-->")


def trim_track_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    query = urllib.parse.parse_qs(parsed.query)
    kept = [(key, query[key][0]) for key in TRACK_QUERY_KEYS if query.get(key)]
    return f"https://www.youtube.com/api/timedtext?{urllib.parse.urlencode(kept)}" if kept else ""


def trim_tracks(tracks: dict) -> dict:
    trimmed = {}
    for lang, entries in (tracks or {}).items():
        entries = [e for e in entries or [] if isinstance(e, dict)]
        entry = next((e for e in entries if e.get("ext") == "vtt"), entries[0] if entries else None)
        if entry is None:
            continue
        trimmed[lang] = [{"ext": entry.get("ext", ""), "name": entry.get("name", ""),
                          "url": trim_track_url(entry.get("url", ""))}]
    return trimmed


def trim_info(info: dict) -> dict:
    out = {key: info[key] for key in INFO_KEYS if key in info}
    out["subtitles"] = trim_tracks(info.get("subtitles") or {})
    out["automatic_captions"] = trim_tracks(info.get("automatic_captions") or {})
    out["formats"] = [{key: f[key] for key in FORMAT_KEYS if key in f}
                      for f in info.get("formats") or [] if isinstance(f, dict)]
    return out


def cut_vtt(text: str, max_minutes: float) -> str:
    """Drop cues starting after max_minutes (0 keeps everything)."""
    if not max_minutes:
        return text
    limit = max_minutes * 60
    blocks = re.split(r"\n\s*\n", text.strip())
    kept = []
    for block in blocks:
        time_line = next((line for line in block.splitlines() if "-->" in line), "")
        match = CUE_TIME.match(time_line.strip())
        if match:
            h, m, s, ms = (int(g) for g in match.groups())
            if h * 3600 + m * 60 + s + ms / 1000 >= limit:
                break
        kept.append(block)
    return "\n\n".join(kept) + "\n"


def cut_deepgram(payload: dict, max_minutes: float) -> dict:
    """Keep the response shape; drop content after max_minutes and unused word fields."""
    limit = max_minutes * 60 if max_minutes else float("inf")
    results = payload.get("results") or {}
    for channel in results.get("channels") or []:
        for alt in channel.get("alternatives") or []:
            if isinstance(alt.get("words"), list):
                alt["words"] = [{k: w[k] for k in WORD_KEYS if k in w}
                                for w in alt["words"] if float(w.get("start", 0)) < limit]
            paragraphs = alt.get("paragraphs")
            if isinstance(paragraphs, dict) and isinstance(paragraphs.get("paragraphs"), list):
                kept = []
                for para in paragraphs["paragraphs"]:
                    if float(para.get("start", 0)) >= limit:
                        break
                    para["sentences"] = [s for s in para.get("sentences") or [] if float(s.get("start", 0)) < limit]
                    kept.append(para)
                paragraphs["paragraphs"] = kept
                sentences = [s.get("text", "") for p in kept for s in p.get("sentences") or []]
                if max_minutes:
                    joiner = "" if re.search(r"[一-鿿]", "".join(sentences[:5])) else " "
                    paragraphs["transcript"] = joiner.join(sentences)
                    alt["transcript"] = joiner.join(sentences)
    if isinstance(results.get("utterances"), list):
        results["utterances"] = [
            {**{k: v for k, v in u.items() if k != "words"},
             "words": [{k: w[k] for k in WORD_KEYS if k in w} for w in u.get("words") or []]}
            for u in results["utterances"] if float(u.get("start", 0)) < limit
        ]
    metadata = payload.get("metadata") or {}
    payload["metadata"] = {k: metadata[k] for k in ("duration", "channels", "models", "model_info") if k in metadata}
    if max_minutes:
        payload["metadata"]["fixture_cut_minutes"] = max_minutes
    return payload


def cue_seconds(stamp: str) -> float:
    h, m, rest = stamp.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest.replace(",", "."))


def deepgram_from_vtt(text: str, language: str) -> dict:
    """A Deepgram-shaped response with one sentence per cue and five sentences per paragraph."""
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        start, end = (cue_seconds(part.split()[0]) for part in lines[timing].split("-->"))
        body = re.sub(r"<[^>]+>", "", " ".join(lines[timing + 1:])).strip()
        if language.startswith("zh"):
            body = re.sub(r"(?<=[^\x00-\x7f]) | (?=[^\x00-\x7f])", "", body)
        if body and (not cues or cues[-1]["text"] != body):
            cues.append({"text": body, "start": start, "end": end})
    cjk = language.startswith("zh")
    words, paragraphs, utterances = [], [], []
    for index, cue in enumerate(cues):
        tokens = list(cue["text"].replace(" ", "")) if cjk else cue["text"].split()
        step = (cue["end"] - cue["start"]) / max(1, len(tokens))
        cue_words = [{"word": token, "punctuated_word": token, "start": round(cue["start"] + i * step, 3),
                      "end": round(cue["start"] + (i + 1) * step, 3), "speaker": 0}
                     for i, token in enumerate(tokens)]
        words += cue_words
        utterances.append({"start": cue["start"], "end": cue["end"], "transcript": cue["text"], "speaker": 0,
                           "words": []})
        if index % 5 == 0:
            paragraphs.append({"speaker": 0, "start": cue["start"], "sentences": []})
        paragraphs[-1]["sentences"].append({"text": cue["text"], "start": cue["start"], "end": cue["end"]})
        paragraphs[-1]["end"] = cue["end"]
    joiner = "" if cjk else " "
    transcript = joiner.join(cue["text"] for cue in cues)
    return {
        "metadata": {"duration": cues[-1]["end"] if cues else 0},
        "results": {
            "channels": [{"detected_language": language.split("-")[0], "alternatives": [{
                "transcript": transcript, "words": words,
                "paragraphs": {"transcript": transcript, "paragraphs": paragraphs}}]}],
            "utterances": utterances,
        },
    }


def load_saved_deepgram(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") == kernel_deepgram.DEEPGRAM_CHUNK_CACHE_FORMAT:
        return data["payload"]
    if isinstance(data.get("chunks"), list) and data["chunks"]:
        return data["chunks"][0]["payload"]
    if "results" in data:
        return data
    raise SystemExit(f"{path} is not a Deepgram response")


def run_json(args: list[str], env: dict) -> dict:
    proc = subprocess.run(args, cwd=ROOT_DIR, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
    out = proc.stdout.strip()
    return json.loads(out[out.find("{"):])


def make_offline_deepgram_case(out_dir: Path, source_id: str) -> dict:
    """Build OFFLINE_DEEPGRAM_ID from a recorded subtitle video; see the module docstring."""
    source = out_dir / source_id
    tracks = sorted((source / "subs").glob("*.vtt"))
    if not (source / "info.json").exists() or not tracks:
        raise SystemExit(f"record {source_id} with subtitles first")
    info = json.loads((source / "info.json").read_text(encoding="utf-8"))
    language = tracks[0].stem
    info.update(id=OFFLINE_DEEPGRAM_ID, subtitles={}, automatic_captions={}, chapters=None,
                duration=OFFLINE_DEEPGRAM_MINUTES * 60,
                title=f"Fixture: {info.get('title', '')} (no subtitles)",
                webpage_url=f"https://www.youtube.com/watch?v={OFFLINE_DEEPGRAM_ID}")
    vdir = out_dir / OFFLINE_DEEPGRAM_ID
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    (vdir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    payload = cut_deepgram(deepgram_from_vtt(tracks[0].read_text(encoding="utf-8"), language),
                           OFFLINE_DEEPGRAM_MINUTES)
    payload["metadata"]["fixture_source"] = f"synthesized from {source_id}/subs/{tracks[0].name}"
    (vdir / "deepgram.json").write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "id": OFFLINE_DEEPGRAM_ID,
        "label": (f"Chinese speech, no subtitles; offline stand-in built from {source_id}'s subtitles "
                  "(info.json without tracks, Deepgram response synthesized from the cues)"),
        "expected_case": "deepgram",
        "case": "deepgram_zh",
        "fixture_cut_minutes": OFFLINE_DEEPGRAM_MINUTES,
    }


def record(case: dict, out_dir: Path, config: dict, env: dict, saved_deepgram: dict, cookie_args: list[str]) -> dict:
    vid = case["id"]
    url = f"https://www.youtube.com/watch?v={vid}"
    vdir = out_dir / vid
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    summary = {"id": vid, "case": case["case"]}

    proc = subprocess.run(["yt-dlp", "-J", "--skip-download", *cookie_args, url],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp -J failed: {proc.stderr.strip()[-500:]}")
    info = trim_info(json.loads(proc.stdout))
    (vdir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    sub_info = run_json(["bash", str(DOWNLOAD_SH), url, "subtitle-info"], env)
    summary["subtitle_info"] = {k: sub_info.get(k) for k in (
        "has_any", "preferred_source_language", "mode", "source_language", "source_language_supported")}
    if sub_info.get("has_any"):
        subs = run_json(["bash", str(DOWNLOAD_SH), url, "subtitles"], env)
        if subs.get("blocked_candidates"):
            raise RuntimeError(f"YouTube blocked {subs['blocked_candidates']}; retry later so the fixture "
                               "matches the preferred track")
        lang = subs["selected_source_language"]
        (vdir / "subs").mkdir()
        text = Path(subs["selected_source_vtt"]).read_text(encoding="utf-8")
        (vdir / "subs" / f"{lang}.vtt").write_text(cut_vtt(text, case["max_minutes"]), encoding="utf-8")
        summary["selected"] = f"{lang} ({subs.get('selected_source_kind')}, {subs.get('resolved_mode')})"
    elif sub_info.get("source_language_supported") is not False:
        if vid in saved_deepgram:
            payload = load_saved_deepgram(saved_deepgram[vid])
            summary["deepgram"] = f"reused {saved_deepgram[vid]}"
        else:
            api_key = config.get("deepgram_api_key", "")
            if not api_key:
                raise RuntimeError("no deepgram_api_key in --config and no --deepgram-json for this video")
            audio = run_json(["bash", str(DOWNLOAD_SH), url, "audio"], env)
            payload = kernel_deepgram._call_deepgram_api(
                audio["audio_file"], api_key=api_key, language=sub_info.get("source_language") or "auto",
                timeout=900, model=kernel_config.DEFAULT_DEEPGRAM_MODEL, enable_utterances=True)
            summary["deepgram"] = "requested"
        payload = cut_deepgram(payload, case["max_minutes"])
        (vdir / "deepgram.json").write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        summary["unsupported"] = sub_info.get("unsupported_reason", "")
    summary["bytes"] = sum(p.stat().st_size for p in vdir.rglob("*") if p.is_file())
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--config", default="", help="config.yaml with deepgram_api_key / yt-dlp cookie settings")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--deepgram-json", action="append", default=[], help="VIDEO_ID=PATH of a saved response")
    parser.add_argument("--offline-deepgram", default="", metavar="SOURCE_ID",
                        help=f"Only build the offline {OFFLINE_DEEPGRAM_ID} case from a recorded video")
    args = parser.parse_args()

    config = kernel_config.load_config(args.config) if args.config else {}
    env = dict(os.environ)
    cookie_args = []
    if config.get("yt_dlp_cookies_file"):
        env["YT_DLP_COOKIES_FILE"] = config["yt_dlp_cookies_file"]
        cookie_args = ["--cookies", config["yt_dlp_cookies_file"]]
    elif config.get("yt_dlp_cookies_from_browser"):
        env["YT_DLP_COOKIES_FROM_BROWSER"] = config["yt_dlp_cookies_from_browser"]
        cookie_args = ["--cookies-from-browser", config["yt_dlp_cookies_from_browser"]]
    saved = dict(item.split("=", 1) for item in args.deepgram_json)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    videos_path = out_dir / "videos.json"
    if args.offline_deepgram:
        offline = make_offline_deepgram_case(out_dir, args.offline_deepgram)
        video_set = json.loads(videos_path.read_text(encoding="utf-8"))
        video_set["videos"] = [v for v in video_set["videos"] if v["id"] != offline["id"]] + [offline]
        videos_path.write_text(json.dumps(video_set, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"id": offline["id"], "source": args.offline_deepgram}))
        return 0
    version = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    failures = 0
    for case in CASES:
        if args.only and case["id"] not in args.only:
            continue
        try:
            summary = record(case, out_dir, config, env, saved, cookie_args)
            print(json.dumps(summary, ensure_ascii=False))
        except (RuntimeError, json.JSONDecodeError, KeyError) as error:
            failures += 1
            print(json.dumps({"id": case["id"], "error": str(error)}, ensure_ascii=False))

    videos = [{**{k: c[k] for k in ("id", "label", "expected_case", "case")},
               "fixture_cut_minutes": c["max_minutes"]}
              for c in CASES if (out_dir / c["id"] / "info.json").exists()]
    # Keep an offline stand-in entry (see --offline-deepgram) across re-recordings.
    if videos_path.exists():
        videos += [v for v in json.loads(videos_path.read_text(encoding="utf-8"))["videos"]
                   if v["id"] == OFFLINE_DEEPGRAM_ID and (out_dir / OFFLINE_DEEPGRAM_ID / "info.json").exists()]
    videos_path.write_text(json.dumps({
        "description": ("End-to-end fixture set, recorded with tests/e2e/record_fixtures.py "
                        f"(yt-dlp {version}, {recorded_at}). Replayed offline by "
                        "scripts/run_regression.py --fixtures and tests/regression_e2e.py."),
        "videos": videos,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
