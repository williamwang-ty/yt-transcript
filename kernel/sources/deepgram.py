"""Deepgram transcription: audio splitting, the API call with retries and a chunk cache, and payload parsing."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from kernel import config as kernel_config
from kernel.config import (
    DEFAULT_DEEPGRAM_ENABLE_UTTERANCES,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT,
    coerce_float_or_none,
    parse_bool,
    parse_int,
    resolve_deepgram_model,
)

DEFAULT_DEEPGRAM_REQUEST_RETRIES = 2
DEFAULT_DEEPGRAM_RETRY_BACKOFF_SEC = 1.0


DEEPGRAM_CHUNK_CACHE_SCHEMA_VERSION = 1
DEEPGRAM_CHUNK_CACHE_FORMAT = "yt_transcript.deepgram_chunk_cache/v1"


def _normalize_confusable_ascii_token(token: str) -> str:
    """Normalize conservative digit/letter confusions in mostly alphabetic tokens."""
    text = str(token or "")
    if len(text) < 5:
        return text
    if not re.search(r'[A-Za-z]', text) or not re.search(r'[0-9]', text):
        return text

    digit_count = sum(ch.isdigit() for ch in text)
    alpha_count = sum(ch.isalpha() for ch in text)
    if digit_count > 2 or alpha_count < 3:
        return text

    normalized = re.sub(r'(?<=[A-Za-z])0(?=[A-Za-z])', 'o', text)
    normalized = re.sub(r'(?<=[a-z])1(?=[a-z])', 'l', normalized)
    normalized = re.sub(r'(?<=[A-Za-z])5(?=[A-Za-z])', 's', normalized)
    normalized = re.sub(r'^5(?=[A-Za-z]{3,})', 's', normalized)
    normalized = re.sub(r'^0(?=[A-Za-z]{4,})', 'o', normalized)
    return normalized


def _normalize_transcript_text(text: str, *, remove_repeated_phrases: bool = True) -> str:
    """Normalize transcript text."""
    transcript = str(text or "")

    # 1. Remove spaces between Chinese characters (multiple passes for thoroughness)
    for _ in range(10):
        transcript = re.sub(r'([一-鿿])\s+([一-鿿])', r'\1\2', transcript)

    # 2. Fix spaces around punctuation
    transcript = re.sub(r'\s+([。，！？、：；])', r'\1', transcript)

    # 3. Normalize conservative mixed alnum OCR-like tokens without touching model/version IDs.
    transcript = re.sub(r'\b[A-Za-z0-9]{5,}\b', lambda match: _normalize_confusable_ascii_token(match.group(0)), transcript)

    # 4. Remove consecutive repeated phrases (3-20 characters)
    if remove_repeated_phrases:
        transcript = re.sub(r'([一-鿿]{3,20})\1{1,5}', r'\1', transcript)

    return transcript.strip()


def _join_deepgram_words(words: list[dict]) -> str:
    """Join deepgram words."""
    parts = []
    for word in words:
        token = str(word.get('punctuated_word') or word.get('word') or '').strip()
        if token:
            parts.append(token)
    return ' '.join(parts).strip()


def _deepgram_primary_alternative(data: dict) -> dict:
    """Return the primary Deepgram alternative payload."""
    return data['results']['channels'][0]['alternatives'][0]


def _deepgram_paragraphs(alternative: dict) -> list[dict]:
    """Return normalized paragraph structures from a Deepgram alternative."""
    paragraphs = alternative.get('paragraphs', {}).get('paragraphs', [])
    return paragraphs if isinstance(paragraphs, list) else []


def _deepgram_words(alternative: dict) -> list[dict]:
    """Return normalized word structures from a Deepgram alternative."""
    words = alternative.get('words', [])
    return words if isinstance(words, list) else []


def _deepgram_utterances(alternative: dict) -> list[dict]:
    """Return normalized utterance structures from a Deepgram alternative."""
    utterances = alternative.get('utterances', [])
    return utterances if isinstance(utterances, list) else []


def _inspect_deepgram_payload(data: dict) -> dict:
    """Collect observability fields from a Deepgram payload without changing strategy."""
    alternative = _deepgram_primary_alternative(data)
    paragraphs = _deepgram_paragraphs(alternative)
    words = _deepgram_words(alternative)
    utterances = _deepgram_utterances(alternative)

    paragraph_count = 0
    sentence_count = 0
    sentence_text_count = 0
    timed_sentence_count = 0
    speaker_ids = set()

    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        paragraph_count += 1

        sentences = paragraph.get('sentences', [])
        if not isinstance(sentences, list):
            continue

        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            sentence_count += 1
            if str(sentence.get('text', '')).strip():
                sentence_text_count += 1
            if coerce_float_or_none(sentence.get('start')) is not None and coerce_float_or_none(sentence.get('end')) is not None:
                timed_sentence_count += 1
            speaker = sentence.get('speaker')
            if speaker is not None:
                speaker_ids.add(str(speaker))

    transcript = str(alternative.get('transcript', '') or '')
    return {
        "transcript": transcript,
        "transcript_source": "alternative.transcript",
        "paragraph_count": paragraph_count,
        "sentence_count": sentence_count,
        "sentence_text_count": sentence_text_count,
        "timed_sentence_count": timed_sentence_count,
        "word_count": len(words),
        "utterance_count": len([utterance for utterance in utterances if isinstance(utterance, dict)]),
        "has_paragraphs": paragraph_count > 0,
        "has_words": bool(words),
        "has_utterances": bool(utterances),
        "speaker_count": len(speaker_ids) if speaker_ids else 1,
    }


def _normalized_deepgram_text(text: str) -> str:
    """Normalize Deepgram-derived text without aggressive phrase de-duplication."""
    return _normalize_transcript_text(text, remove_repeated_phrases=False)


def _extract_utterance_text(utterance: dict) -> str:
    """Return the best text field from a Deepgram utterance."""
    return str(utterance.get('transcript') or utterance.get('text') or '').strip()


def _build_deepgram_transcript(data: dict, *, prefer_structured_output: bool = DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT) -> dict:
    """Build transcript text from a Deepgram payload with optional structured-field priority."""
    inspection = _inspect_deepgram_payload(data)
    if not prefer_structured_output:
        return {
            "transcript": _normalize_transcript_text(inspection["transcript"]),
            "transcript_source": inspection["transcript_source"],
            "warnings": [],
            "prefer_structured_output": False,
        }

    alternative = _deepgram_primary_alternative(data)
    utterances = _deepgram_utterances(alternative)
    paragraphs = _deepgram_paragraphs(alternative)
    words = _deepgram_words(alternative)

    utterance_blocks = []
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        normalized_text = _normalized_deepgram_text(_extract_utterance_text(utterance))
        if normalized_text:
            utterance_blocks.append(normalized_text)
    if utterance_blocks:
        return {
            "transcript": "\n\n".join(utterance_blocks).strip(),
            "transcript_source": "utterances",
            "warnings": [],
            "prefer_structured_output": True,
        }

    paragraph_blocks = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        sentences = paragraph.get('sentences', [])
        if not isinstance(sentences, list):
            continue
        sentence_texts = []
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            normalized_text = _normalized_deepgram_text(str(sentence.get('text', '')).strip())
            if normalized_text:
                sentence_texts.append(normalized_text)
        if sentence_texts:
            paragraph_blocks.append(_normalized_deepgram_text(" ".join(sentence_texts).strip()))
    if paragraph_blocks:
        return {
            "transcript": "\n\n".join(paragraph_blocks).strip(),
            "transcript_source": "paragraphs.sentences[].text",
            "warnings": ["transcript fell back to paragraphs.sentences[].text because no utterances were available"],
            "prefer_structured_output": True,
        }

    word_transcript = _normalized_deepgram_text(_join_deepgram_words(words))
    if word_transcript:
        return {
            "transcript": word_transcript,
            "transcript_source": "words[].punctuated_word",
            "warnings": ["transcript fell back to words[].punctuated_word because no utterances or sentence text were available"],
            "prefer_structured_output": True,
        }

    transcript = _normalize_transcript_text(inspection["transcript"])
    warnings = []
    if transcript:
        warnings.append("transcript fell back to alternative.transcript because richer Deepgram structures were unavailable")
    return {
        "transcript": transcript,
        "transcript_source": inspection["transcript_source"],
        "warnings": warnings,
        "prefer_structured_output": True,
    }


def _extract_deepgram_segments_with_report(data: dict, *, time_offset: float = 0.0,
                                           source_chunk_index: int = 0,
                                           starting_segment_id: int = 0,
                                           prefer_structured_output: bool = DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT) -> tuple[list[dict], dict]:
    """Extract Deepgram segments plus observability metadata."""
    alternative = _deepgram_primary_alternative(data)
    paragraphs = _deepgram_paragraphs(alternative)
    words = _deepgram_words(alternative)
    utterances = _deepgram_utterances(alternative)

    if prefer_structured_output:
        utterance_segments = []
        next_segment_id = starting_segment_id
        for utterance_index, utterance in enumerate(utterances):
            if not isinstance(utterance, dict):
                continue
            normalized_text = _normalized_deepgram_text(_extract_utterance_text(utterance))
            if not normalized_text:
                continue
            start_time = coerce_float_or_none(utterance.get('start'))
            end_time = coerce_float_or_none(utterance.get('end'))
            utterance_segments.append({
                'id': next_segment_id,
                'text': normalized_text,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': utterance.get('speaker'),
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': utterance_index,
                'source_sentence_index': 0,
            })
            next_segment_id += 1

        if utterance_segments:
            return utterance_segments, {
                "segment_source": "utterances",
                "segment_count": len(utterance_segments),
                "paragraph_sentence_count": 0,
                "utterance_segment_count": len(utterance_segments),
                "word_aligned_segment_count": 0,
                "sentence_text_fallback_count": 0,
                "transcript_fallback_used": False,
                "warnings": [],
            }

        sentence_segments = []
        next_segment_id = starting_segment_id
        sentence_count = 0
        for para_index, paragraph in enumerate(paragraphs):
            if not isinstance(paragraph, dict):
                continue
            paragraph_speaker = paragraph.get('speaker')
            sentences = paragraph.get('sentences', [])
            if not isinstance(sentences, list):
                continue
            for sentence_index, sentence in enumerate(sentences):
                if not isinstance(sentence, dict):
                    continue
                sentence_count += 1
                normalized_text = _normalized_deepgram_text(str(sentence.get('text', '')).strip())
                if not normalized_text:
                    continue
                start_time = coerce_float_or_none(sentence.get('start'))
                end_time = coerce_float_or_none(sentence.get('end'))
                speaker = sentence.get('speaker')
                if speaker is None:
                    speaker = paragraph_speaker
                sentence_segments.append({
                    'id': next_segment_id,
                    'text': normalized_text,
                    'start_time': None if start_time is None else round(start_time + time_offset, 3),
                    'end_time': None if end_time is None else round(end_time + time_offset, 3),
                    'speaker': speaker,
                    'source_chunk_index': source_chunk_index,
                    'source_paragraph_index': para_index,
                    'source_sentence_index': sentence_index,
                })
                next_segment_id += 1

        if sentence_segments:
            return sentence_segments, {
                "segment_source": "paragraphs.sentences[].text",
                "segment_count": len(sentence_segments),
                "paragraph_sentence_count": sentence_count,
                "utterance_segment_count": 0,
                "word_aligned_segment_count": 0,
                "sentence_text_fallback_count": len(sentence_segments),
                "transcript_fallback_used": False,
                "warnings": ["segments fell back to paragraphs.sentences[].text because no utterances were available"],
            }

        word_transcript = _normalized_deepgram_text(_join_deepgram_words(words))
        if word_transcript:
            word_starts = [coerce_float_or_none(word.get('start')) for word in words if isinstance(word, dict)]
            word_ends = [coerce_float_or_none(word.get('end')) for word in words if isinstance(word, dict)]
            start_time = min((value for value in word_starts if value is not None), default=None)
            end_time = max((value for value in word_ends if value is not None), default=None)
            return [{
                'id': starting_segment_id,
                'text': word_transcript,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': None,
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': 0,
                'source_sentence_index': 0,
            }], {
                "segment_source": "words[].punctuated_word",
                "segment_count": 1,
                "paragraph_sentence_count": 0,
                "utterance_segment_count": 0,
                "word_aligned_segment_count": 1,
                "sentence_text_fallback_count": 0,
                "transcript_fallback_used": False,
                "warnings": ["segments fell back to words[].punctuated_word because no utterances or sentence text were available"],
            }

    segments = []
    next_segment_id = starting_segment_id
    paragraph_sentence_count = 0
    word_aligned_segment_count = 0
    sentence_text_fallback_count = 0
    warnings = []

    for para_index, paragraph in enumerate(paragraphs):
        if not isinstance(paragraph, dict):
            continue
        paragraph_speaker = paragraph.get('speaker')
        sentences = paragraph.get('sentences', [])
        if not isinstance(sentences, list):
            continue

        for sentence_index, sentence in enumerate(sentences):
            if not isinstance(sentence, dict):
                continue
            paragraph_sentence_count += 1
            speaker = sentence.get('speaker')
            if speaker is None:
                speaker = paragraph_speaker
            start_time = coerce_float_or_none(sentence.get('start'))
            end_time = coerce_float_or_none(sentence.get('end'))

            matched_words = []
            if start_time is not None and end_time is not None and words:
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    word_start = coerce_float_or_none(word.get('start'))
                    word_end = coerce_float_or_none(word.get('end'))
                    if word_start is None or word_end is None:
                        continue
                    if word_end <= start_time - 1e-6 or word_start >= end_time + 1e-6:
                        continue
                    matched_words.append(word)

            if matched_words:
                sentence_text = _join_deepgram_words(matched_words)
                word_aligned_segment_count += 1
            else:
                sentence_text = str(sentence.get('text', '')).strip()
                if sentence_text:
                    sentence_text_fallback_count += 1

            normalized_text = _normalize_transcript_text(sentence_text, remove_repeated_phrases=False)
            if not normalized_text:
                continue

            segments.append({
                'id': next_segment_id,
                'text': normalized_text,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': speaker,
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': para_index,
                'source_sentence_index': sentence_index,
            })
            next_segment_id += 1

    transcript_fallback_used = False
    if not segments:
        transcript = _normalize_transcript_text(alternative.get('transcript', ''), remove_repeated_phrases=False)
        if transcript:
            transcript_fallback_used = True
            word_starts = [coerce_float_or_none(word.get('start')) for word in words if isinstance(word, dict)]
            word_ends = [coerce_float_or_none(word.get('end')) for word in words if isinstance(word, dict)]
            start_time = min((value for value in word_starts if value is not None), default=None)
            end_time = max((value for value in word_ends if value is not None), default=None)
            segments = [{
                'id': starting_segment_id,
                'text': transcript,
                'start_time': None if start_time is None else round(start_time + time_offset, 3),
                'end_time': None if end_time is None else round(end_time + time_offset, 3),
                'speaker': None,
                'source_chunk_index': source_chunk_index,
                'source_paragraph_index': 0,
                'source_sentence_index': 0,
            }]

    if transcript_fallback_used:
        warnings.append("segments fell back to alternative.transcript because no paragraph sentences were usable")
        segment_source = "alternative.transcript"
    elif word_aligned_segment_count and sentence_text_fallback_count:
        warnings.append(
            f"segments fell back to sentence.text for {sentence_text_fallback_count} paragraph sentence(s)"
        )
        segment_source = "paragraph_sentence_mixed"
    elif word_aligned_segment_count:
        segment_source = "paragraph_sentence_word_join"
    elif sentence_text_fallback_count:
        warnings.append(
            f"segments used sentence.text for all {sentence_text_fallback_count} paragraph sentence(s)"
        )
        segment_source = "paragraph_sentence_text"
    else:
        segment_source = "none"

    return segments, {
        "segment_source": segment_source,
        "segment_count": len(segments),
        "paragraph_sentence_count": paragraph_sentence_count,
        "utterance_segment_count": 0,
        "word_aligned_segment_count": word_aligned_segment_count,
        "sentence_text_fallback_count": sentence_text_fallback_count,
        "transcript_fallback_used": transcript_fallback_used,
        "warnings": warnings,
    }


def process_deepgram_payload(data: dict, *, prefer_structured_output: bool = DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT) -> dict:
    """Process deepgram payload."""
    inspection = _inspect_deepgram_payload(data)
    transcript_result = _build_deepgram_transcript(data, prefer_structured_output=prefer_structured_output)
    transcript = transcript_result["transcript"]
    return {
        "transcript": transcript.strip(),
        "speaker_count": inspection["speaker_count"],
        "transcript_source": transcript_result["transcript_source"],
        "paragraph_count": inspection["paragraph_count"],
        "sentence_count": inspection["sentence_count"],
        "sentence_text_count": inspection["sentence_text_count"],
        "timed_sentence_count": inspection["timed_sentence_count"],
        "word_count": inspection["word_count"],
        "utterance_count": inspection["utterance_count"],
        "has_paragraphs": inspection["has_paragraphs"],
        "has_words": inspection["has_words"],
        "has_utterances": inspection["has_utterances"],
        "warnings": list(transcript_result.get("warnings", [])),
        "prefer_structured_output": bool(transcript_result.get("prefer_structured_output", False)),
    }


def split_audio(audio_path: str, max_size_mb: float = 10.0, max_deviation_sec: float = 60.0) -> dict:
    """
    Split large audio file based on silence detection
    
    Algorithm:
    1. Calculate rough split points (based on file size and max_size_mb interval)
    2. Use FFmpeg silencedetect to find all silence intervals
    3. For each rough split point, find the nearest silence point (before or after)
    4. If both silence points exceed max_deviation_sec, force split at the rough point
    
    Args:
        audio_path: Path to audio file
        max_size_mb: Max chunk size in MB, default 10MB
        max_deviation_sec: Max allowed deviation in seconds, default 60s
    
    Returns:
        {"chunks": ["path1.mp3", ...], "total_chunks": N, "split_points": [t1, t2, ...]}
    """
    path = Path(audio_path)
    if not path.exists():
        print(f"Error: File does not exist {audio_path}", file=sys.stderr)
        sys.exit(1)
    
    if max_size_mb <= 0:
        print(f"Error: max_size_mb must be positive, got {max_size_mb}", file=sys.stderr)
        sys.exit(1)
    
    if max_deviation_sec < 0:
        print(f"Error: max_deviation_sec must be non-negative, got {max_deviation_sec}", file=sys.stderr)
        sys.exit(1)
    
    file_size = path.stat().st_size
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # If file size is within limit, no splitting needed
    if file_size <= max_size_bytes:
        return {
            "chunks": [str(path)],
            "total_chunks": 1,
            "split_points": [],
            "message": "File size within limit, no splitting needed"
        }
    
    # 1. Get audio duration
    duration_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]
    try:
        result = subprocess.run(duration_cmd, capture_output=True, text=True, check=True)
        total_duration = float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"Error: Cannot get audio duration: {e}", file=sys.stderr)
        sys.exit(2)
    
    # 2. Calculate rough split points (based on file size ratio)
    num_chunks = math.ceil(file_size / max_size_bytes)
    rough_split_times = []
    for i in range(1, num_chunks):
        rough_time = (i / num_chunks) * total_duration
        rough_split_times.append(rough_time)
    
    # 3. Detect silence intervals using FFmpeg
    silence_cmd = [
        "ffmpeg", "-i", str(path), "-af",
        "silencedetect=noise=-30dB:d=0.5",
        "-f", "null", "-"
    ]
    result = subprocess.run(silence_cmd, capture_output=True, text=True)
    # silencedetect output is in stderr, even if returncode is not 0
    silence_output = result.stderr
    
    # Parse silence intervals: silence_start: 10.5 | silence_end: 11.2
    silence_points = []  # [(start, end), ...]
    starts = re.findall(r'silence_start: ([\d.]+)', silence_output)
    ends = re.findall(r'silence_end: ([\d.]+)', silence_output)
    for s, e in zip(starts, ends):
        silence_points.append((float(s), float(e)))
    
    # Calculate midpoint of each silence interval
    silence_midpoints = [(s + e) / 2 for s, e in silence_points]
    
    # Log warning if no silence detected
    if not silence_midpoints:
        print("⚠️ No silence detected in audio, using rough split points", file=sys.stderr)
    
    # 4. Find best split point for each rough point
    actual_split_times = []
    for rough_time in rough_split_times:
        best_point = _find_best_split_point(rough_time, silence_midpoints, max_deviation_sec)
        actual_split_times.append(best_point)
    
    # Deduplicate and sort (avoid selecting same silence point for adjacent rough points)
    actual_split_times = sorted(set(actual_split_times))
    
    # 5. Split audio using FFmpeg
    output_dir = path.parent
    base_name = path.stem
    chunks = []
    
    split_times = [0] + actual_split_times + [total_duration]
    for i in range(len(split_times) - 1):
        start_time = split_times[i]
        end_time = split_times[i + 1]
        duration = end_time - start_time
        chunk_path = output_dir / f"{base_name}_chunk_{i:03d}.mp3"
        
        # -ss before -i for fast seek, using -t (duration) instead of -to
        split_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", str(path),
            "-t", str(duration),
            "-c:a", "libmp3lame", "-q:a", "2",
            str(chunk_path)
        ]
        try:
            subprocess.run(split_cmd, capture_output=True, check=True)
            chunks.append(str(chunk_path))
        except subprocess.CalledProcessError as e:
            print(f"Error: FFmpeg split failed for chunk {i}: {e}", file=sys.stderr)
            sys.exit(2)
    
    return {
        "chunks": chunks,
        "total_chunks": len(chunks),
        "split_points": actual_split_times
    }


def _find_best_split_point(rough_time: float, silence_midpoints: list, max_deviation: float) -> float:
    """
    Find best split point near rough split point (using binary search optimization)
    
    Args:
        rough_time: Rough split time point
        silence_midpoints: List of silence interval midpoints (sorted)
        max_deviation: Max allowed deviation in seconds
    
    Returns:
        Actual split time point
    """
    if not silence_midpoints:
        return rough_time
    
    # Use binary search to find insertion position
    idx = bisect.bisect_left(silence_midpoints, rough_time)
    
    # Get nearest silence points before and after
    prev_silence = silence_midpoints[idx - 1] if idx > 0 else None
    next_silence = silence_midpoints[idx] if idx < len(silence_midpoints) else None
    
    # Calculate distances
    prev_dist = rough_time - prev_silence if prev_silence is not None else float('inf')
    next_dist = next_silence - rough_time if next_silence is not None else float('inf')
    
    # Choose the nearer one
    if prev_dist <= next_dist and prev_dist <= max_deviation:
        return prev_silence
    elif next_dist < prev_dist and next_dist <= max_deviation:
        return next_silence
    else:
        # Both exceed limit, force split at rough point
        return rough_time


def detect_audio_content_type(audio_path: str) -> str:
    """Detect audio content type."""
    ext = Path(audio_path).suffix.lower().lstrip(".")
    return {
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "webm": "audio/webm",
        "opus": "audio/opus",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
    }.get(ext, "application/octet-stream")


def _resolve_deepgram_request_settings(config: dict | None = None, *, language: str = "",
                                       model: str = "", enable_utterances: bool | None = None,
                                       prefer_structured_output: bool | None = None) -> dict:
    """Resolve Deepgram request and transcript strategy settings."""
    config = config or {}
    request_model, model_warning = resolve_deepgram_model(
        model or config.get("deepgram_model", DEFAULT_DEEPGRAM_MODEL),
    )

    if enable_utterances is None:
        utterances_enabled = parse_bool(
            config.get("deepgram_enable_utterances", DEFAULT_DEEPGRAM_ENABLE_UTTERANCES),
            DEFAULT_DEEPGRAM_ENABLE_UTTERANCES,
        )
    else:
        utterances_enabled = bool(enable_utterances)

    if prefer_structured_output is None:
        structured_output_enabled = parse_bool(
            config.get("deepgram_prefer_structured_output", DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT),
            DEFAULT_DEEPGRAM_PREFER_STRUCTURED_OUTPUT,
        )
    else:
        structured_output_enabled = bool(prefer_structured_output)

    settings = {
        "model": request_model,
        "language": language,
        "diarize": True,
        "punctuate": True,
        "paragraphs": True,
        "smart_format": True,
        "utterances": utterances_enabled,
        "prefer_structured_output": structured_output_enabled,
    }
    if _deepgram_detects_language(language):
        settings["detect_language"] = True
    if model_warning:
        settings["model_warning"] = model_warning
    return settings


def _deepgram_detects_language(language: str) -> bool:
    """Return whether a Deepgram request should detect the language itself."""
    return str(language or "").strip().lower() in {"", "auto"}


def _deepgram_detected_language(payload: dict) -> str:
    """Return the language Deepgram detected for a payload, if it reported one."""
    channels = ((payload or {}).get("results") or {}).get("channels") or []
    if channels and isinstance(channels[0], dict):
        return str(channels[0].get("detected_language") or "").strip()
    return ""


def _call_deepgram_api_once(audio_path: str, api_key: str, language: str,
                            timeout: int = 300, *, model: str = DEFAULT_DEEPGRAM_MODEL,
                            enable_utterances: bool = DEFAULT_DEEPGRAM_ENABLE_UTTERANCES) -> dict:
    """Perform one Deepgram API request."""

    audio_file = Path(audio_path)
    if not audio_file.exists():
        print(f"Error: Audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    request_model, model_warning = resolve_deepgram_model(model)
    if model_warning:
        print(f"Warning: {model_warning}", file=sys.stderr)

    if _deepgram_detects_language(language):
        language_param = "detect_language=true"
    else:
        language_param = f"language={urllib.parse.quote(str(language))}"
    params = (
        f"model={urllib.parse.quote(request_model)}&{language_param}"
        "&diarize=true&punctuate=true&paragraphs=true&smart_format=true"
    )
    if enable_utterances:
        params += "&utterances=true"
    url = f"https://api.deepgram.com/v1/listen?{params}"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": detect_audio_content_type(audio_path),
        "User-Agent": "yt-transcript/4.0",
    }

    data = audio_file.read_bytes()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _is_retryable_deepgram_error(error: Exception) -> bool:
    """Return whether a Deepgram request error is worth retrying once or twice."""

    if isinstance(error, (socket.timeout, TimeoutError)):
        return True
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 409, 425, 429, 500, 502, 503, 504}
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, 'reason', '')
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        reason_text = str(reason).lower()
        return any(fragment in reason_text for fragment in (
            'timed out',
            'timeout',
            'temporarily unavailable',
            'connection reset',
            'connection aborted',
            'connection refused',
        ))
    error_text = str(error).lower()
    return any(fragment in error_text for fragment in (
        'timed out',
        'timeout',
        'temporarily unavailable',
        'connection reset',
        'connection aborted',
    ))


def _call_deepgram_api(audio_path: str, api_key: str, language: str,
                       timeout: int = 300, request_retries: int = DEFAULT_DEEPGRAM_REQUEST_RETRIES,
                       retry_backoff_sec: float = DEFAULT_DEEPGRAM_RETRY_BACKOFF_SEC, *,
                       model: str = DEFAULT_DEEPGRAM_MODEL,
                       enable_utterances: bool = DEFAULT_DEEPGRAM_ENABLE_UTTERANCES) -> dict:
    """Call Deepgram with bounded retries for transient timeout/network failures."""

    attempts = max(1, request_retries + 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return _call_deepgram_api_once(
                audio_path,
                api_key=api_key,
                language=language,
                timeout=timeout,
                model=model,
                enable_utterances=enable_utterances,
            )
        except urllib.error.HTTPError as error:
            last_error = error
            if attempt < attempts and _is_retryable_deepgram_error(error):
                print(
                    f"Warning: Deepgram API returned HTTP {error.code} on attempt {attempt}/{attempts}; retrying in {retry_backoff_sec:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(max(0.0, retry_backoff_sec))
                continue
            error_body = error.read().decode("utf-8", errors="replace")
            print(f"Error: Deepgram API returned HTTP {error.code}: {error_body}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as error:
            last_error = error
            if attempt < attempts and _is_retryable_deepgram_error(error):
                print(
                    f"Warning: Deepgram API network error on attempt {attempt}/{attempts}: {error.reason}. Retrying in {retry_backoff_sec:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(max(0.0, retry_backoff_sec))
                continue
            print(f"Error: Cannot reach Deepgram API: {error.reason}", file=sys.stderr)
            sys.exit(1)
        except Exception as error:
            last_error = error
            if attempt < attempts and _is_retryable_deepgram_error(error):
                print(
                    f"Warning: Deepgram API call failed on attempt {attempt}/{attempts}: {error}. Retrying in {retry_backoff_sec:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(max(0.0, retry_backoff_sec))
                continue
            print(f"Error: Deepgram API call failed: {error}", file=sys.stderr)
            sys.exit(1)

    print(f"Error: Deepgram API call failed: {last_error}", file=sys.stderr)
    sys.exit(1)


def _deepgram_chunk_json_path(output_json: str, chunk_count: int, chunk_index: int) -> Path | None:
    """Return the per-chunk Deepgram JSON path when JSON output is enabled."""
    if not output_json:
        return None
    output_base = Path(output_json)
    if chunk_count == 1:
        return output_base
    return output_base.with_name(f"{output_base.stem}_chunk_{chunk_index:03d}{output_base.suffix or '.json'}")


def _sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest for a file path."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _deepgram_request_cache_fingerprint(request_settings: dict | None = None) -> dict:
    """Return the stable subset of Deepgram request settings that affect payloads."""
    settings = request_settings if isinstance(request_settings, dict) else {}
    return {
        "model": str(settings.get("model", "")).strip(),
        "language": str(settings.get("language", "")).strip(),
        "diarize": bool(settings.get("diarize", False)),
        "punctuate": bool(settings.get("punctuate", False)),
        "paragraphs": bool(settings.get("paragraphs", False)),
        "smart_format": bool(settings.get("smart_format", False)),
        "utterances": bool(settings.get("utterances", False)),
    }


def _build_deepgram_chunk_cache_metadata(chunk_path: str | Path, *, chunk_count: int,
                                         chunk_index: int, request_settings: dict | None = None) -> dict:
    """Build the validation metadata stored alongside a Deepgram chunk payload."""
    path = Path(chunk_path)
    stat = path.stat()
    return {
        "chunk_index": int(chunk_index),
        "chunk_count": int(chunk_count),
        "chunk_name": path.name,
        "chunk_size_bytes": int(stat.st_size),
        "chunk_sha256": _sha256_file(path),
        "request_fingerprint": _deepgram_request_cache_fingerprint(request_settings),
    }


def _build_deepgram_chunk_cache_envelope(payload: dict, *, cache_metadata: dict | None = None) -> dict:
    """Wrap a raw Deepgram payload with resumable cache metadata."""
    return {
        "schema_version": DEEPGRAM_CHUNK_CACHE_SCHEMA_VERSION,
        "format": DEEPGRAM_CHUNK_CACHE_FORMAT,
        "cache_metadata": dict(cache_metadata or {}),
        "payload": payload,
    }


def _deepgram_chunk_cache_matches_expected(cache_metadata: dict | None, expected_metadata: dict | None) -> str:
    """Return an empty string when chunk-cache metadata matches; otherwise a reason."""
    actual = cache_metadata if isinstance(cache_metadata, dict) else {}
    expected = expected_metadata if isinstance(expected_metadata, dict) else {}
    if not actual:
        return "cache_metadata_missing"
    for key in ("chunk_index", "chunk_count", "chunk_size_bytes", "chunk_sha256"):
        if actual.get(key) != expected.get(key):
            return f"{key}_mismatch"
    if actual.get("request_fingerprint") != expected.get("request_fingerprint"):
        return "request_fingerprint_mismatch"
    return ""


def _load_existing_deepgram_chunk_payload(json_path: Path, *, expected_metadata: dict | None = None) -> tuple[dict | None, str]:
    """Best-effort load for a previously completed and validated Deepgram chunk payload."""
    if not json_path.exists():
        return None, "missing"
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "unreadable_or_invalid_json"
    if not isinstance(payload, dict):
        return None, "not_a_json_object"
    if str(payload.get("format", "")).strip() != DEEPGRAM_CHUNK_CACHE_FORMAT:
        return None, "legacy_or_unversioned_payload"
    cached_payload = payload.get("payload")
    if not isinstance(cached_payload, dict):
        return None, "payload_missing_or_invalid"
    mismatch_reason = _deepgram_chunk_cache_matches_expected(
        payload.get("cache_metadata"),
        expected_metadata,
    )
    if mismatch_reason:
        return None, mismatch_reason
    return cached_payload, ""


def transcribe_deepgram(audio_path: str, language: str, config_path: str = None,
                        api_key: str = "", max_size_mb: float = 10.0,
                        max_deviation_sec: float = 60.0, timeout: int = 300,
                        output_json: str = "", output_text: str = "",
                        output_segments: str = "", deepgram_model: str = "",
                        enable_utterances: bool | None = None,
                        prefer_structured_output: bool | None = None,
                        resume_existing_chunks: bool = False) -> dict:
    """
    Transcribe audio via Deepgram. Automatically splits large files and merges
    chunk transcripts into one raw transcript output.

    The returned result now also includes lightweight observability fields such
    as paragraph/sentence/word counts, per-chunk reports, and warnings when
    segment extraction falls back to less structured Deepgram fields.
    """
    config = kernel_config.load_config(config_path, allow_missing=(config_path is None))
    request_settings = _resolve_deepgram_request_settings(
        config,
        language=language,
        model=deepgram_model,
        enable_utterances=enable_utterances,
        prefer_structured_output=prefer_structured_output,
    )

    if not api_key:
        api_key = config.get("deepgram_api_key", "")

    if not api_key:
        print("Error: Deepgram API key not configured", file=sys.stderr)
        sys.exit(1)

    path = Path(audio_path)
    if not path.exists():
        print(f"Error: Audio file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    split_result = split_audio(audio_path, max_size_mb=max_size_mb, max_deviation_sec=max_deviation_sec)
    chunk_paths = split_result["chunks"]
    split_points = [float(point) for point in split_result.get("split_points", [])]

    chunk_offsets = [0.0] + split_points
    if len(chunk_offsets) < len(chunk_paths):
        chunk_offsets.extend([chunk_offsets[-1] if chunk_offsets else 0.0] * (len(chunk_paths) - len(chunk_offsets)))

    transcripts = []
    speaker_count = 1
    json_outputs = []
    raw_payloads = []
    segments = []
    chunk_reports = []
    chunk_cache_metadatas = []
    warnings = []
    total_paragraph_count = 0
    total_sentence_count = 0
    total_sentence_text_count = 0
    total_timed_sentence_count = 0
    total_word_count = 0
    total_utterance_count = 0
    detected_languages = []
    if request_settings.get("model_warning"):
        warnings.append(str(request_settings["model_warning"]))

    for idx, chunk_path in enumerate(chunk_paths):
        chunk_json_path = _deepgram_chunk_json_path(output_json, len(chunk_paths), idx)
        cache_metadata = None
        if chunk_json_path is not None:
            try:
                cache_metadata = _build_deepgram_chunk_cache_metadata(
                    chunk_path,
                    chunk_count=len(chunk_paths),
                    chunk_index=idx,
                    request_settings=request_settings,
                )
            except OSError as error:
                print(f"Error: Cannot fingerprint Deepgram chunk {chunk_path}: {error}", file=sys.stderr)
                sys.exit(2)
        payload = None
        reused_existing_payload = False
        if resume_existing_chunks and chunk_json_path is not None:
            payload, reuse_reason = _load_existing_deepgram_chunk_payload(
                chunk_json_path,
                expected_metadata=cache_metadata,
            )
            if payload is not None:
                reused_existing_payload = True
                print(
                    f"Info: Reusing existing Deepgram chunk payload {chunk_json_path.name} ({idx + 1}/{len(chunk_paths)})",
                    file=sys.stderr,
                )
            else:
                if reuse_reason == "missing":
                    print(
                        f"Info: No reusable Deepgram chunk payload for chunk {idx + 1}/{len(chunk_paths)}; requesting it now",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"Info: Existing Deepgram chunk payload {chunk_json_path.name} is not reusable ({reuse_reason}); requesting it now",
                        file=sys.stderr,
                    )

        if payload is None:
            payload = _call_deepgram_api(
                chunk_path,
                api_key=api_key,
                language=language,
                timeout=timeout,
                model=request_settings["model"],
                enable_utterances=request_settings["utterances"],
            )
        raw_payloads.append(payload)
        chunk_detected_language = _deepgram_detected_language(payload)
        if chunk_detected_language:
            detected_languages.append(chunk_detected_language)
        processed = process_deepgram_payload(
            payload,
            prefer_structured_output=request_settings["prefer_structured_output"],
        )
        transcripts.append(processed["transcript"])
        speaker_count = max(speaker_count, processed["speaker_count"])
        total_paragraph_count += parse_int(processed.get("paragraph_count"), 0)
        total_sentence_count += parse_int(processed.get("sentence_count"), 0)
        total_sentence_text_count += parse_int(processed.get("sentence_text_count"), 0)
        total_timed_sentence_count += parse_int(processed.get("timed_sentence_count"), 0)
        total_word_count += parse_int(processed.get("word_count"), 0)
        total_utterance_count += parse_int(processed.get("utterance_count"), 0)
        warnings.extend(
            f"Chunk {idx}: {warning}" for warning in processed.get("warnings", [])
        )

        chunk_report = {
            "chunk_index": idx,
            "transcript_source": str(processed.get("transcript_source", "alternative.transcript") or "alternative.transcript"),
            "paragraph_count": parse_int(processed.get("paragraph_count"), 0),
            "sentence_count": parse_int(processed.get("sentence_count"), 0),
            "sentence_text_count": parse_int(processed.get("sentence_text_count"), 0),
            "timed_sentence_count": parse_int(processed.get("timed_sentence_count"), 0),
            "word_count": parse_int(processed.get("word_count"), 0),
            "utterance_count": parse_int(processed.get("utterance_count"), 0),
            "prefer_structured_output": parse_bool(processed.get("prefer_structured_output"), False),
            "reused_existing_payload": reused_existing_payload,
        }

        if output_segments:
            chunk_segments, segment_report = _extract_deepgram_segments_with_report(
                payload,
                time_offset=chunk_offsets[idx],
                source_chunk_index=idx,
                starting_segment_id=len(segments),
                prefer_structured_output=request_settings["prefer_structured_output"],
            )
            segments.extend(chunk_segments)
            chunk_report.update({
                "segment_source": segment_report["segment_source"],
                "segment_count": segment_report["segment_count"],
                "paragraph_sentence_count": segment_report["paragraph_sentence_count"],
                "utterance_segment_count": segment_report["utterance_segment_count"],
                "word_aligned_segment_count": segment_report["word_aligned_segment_count"],
                "sentence_text_fallback_count": segment_report["sentence_text_fallback_count"],
                "transcript_fallback_used": segment_report["transcript_fallback_used"],
            })
            warnings.extend(
                f"Chunk {idx}: {warning}" for warning in segment_report.get("warnings", [])
            )

        chunk_reports.append(chunk_report)
        chunk_cache_metadatas.append(cache_metadata or {})

        if chunk_json_path is not None:
            json_path = chunk_json_path
            json_path.write_text(
                json.dumps(
                    _build_deepgram_chunk_cache_envelope(payload, cache_metadata=cache_metadata),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            json_outputs.append(str(json_path))

    transcript = "\n\n".join(t for t in transcripts if t).strip()
    used_split_mode = len(chunk_paths) > 1
    detected_language = Counter(detected_languages).most_common(1)[0][0] if detected_languages else ""
    effective_language = detected_language if _deepgram_detects_language(language) else language
    transcript_sources = {report.get("transcript_source", "alternative.transcript") for report in chunk_reports}
    transcript_source = transcript_sources.pop() if len(transcript_sources) == 1 else "mixed"
    warnings = list(dict.fromkeys(warnings))

    if output_json and used_split_mode:
        output_base = Path(output_json)
        output_base.write_text(
            json.dumps(
                {
                    "deepgram_request": request_settings,
                    "chunk_count": len(chunk_paths),
                    "split_points": split_points,
                    "chunk_reports": chunk_reports,
                    "warnings": warnings,
                    "chunks": [
                        {
                            "index": idx,
                            "chunk_path": str(chunk_path),
                            "cache_metadata": cache_metadata,
                            "payload": payload,
                        }
                        for idx, (chunk_path, cache_metadata, payload) in enumerate(
                            zip(chunk_paths, chunk_cache_metadatas, raw_payloads)
                        )
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if output_text:
        Path(output_text).write_text(transcript, encoding="utf-8")

    if output_segments:
        Path(output_segments).write_text(
            json.dumps(
                {
                    "source": "deepgram",
                    "deepgram_request": request_settings,
                    "language": effective_language,
                    "used_split_mode": used_split_mode,
                    "chunk_count": len(chunk_paths),
                    "split_points": split_points,
                    "chunk_reports": chunk_reports,
                    "warnings": warnings,
                    "segments": segments,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return {
        "transcript": transcript,
        "speaker_count": speaker_count,
        "language": effective_language,
        "detected_language": detected_language,
        "deepgram_request": request_settings,
        "transcript_source": transcript_source,
        "chunk_count": len(chunk_paths),
        "json_outputs": json_outputs,
        "split_points": split_points,
        "used_split_mode": used_split_mode,
        "paragraph_count": total_paragraph_count,
        "sentence_count": total_sentence_count,
        "sentence_text_count": total_sentence_text_count,
        "timed_sentence_count": total_timed_sentence_count,
        "word_count": total_word_count,
        "utterance_count": total_utterance_count,
        "chunk_reports": chunk_reports,
        "warnings": warnings,
        "segment_count": len(segments),
        "segments_output": output_segments,
    }
