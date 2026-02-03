#!/usr/bin/env python3
"""
yt-transcript utility script
Provides VTT parsing, Deepgram result processing, audio splitting, filename sanitization, etc.

Usage:
    python3 yt_transcript_utils.py <command> [args]

Commands:
    parse-vtt <vtt_path>           Parse VTT subtitle file, output plain text
    process-deepgram <json_path>   Process Deepgram JSON, output cleaned text
    sanitize-filename "<title>"    Clean illegal filename characters
    test-deepgram-api <api_key>    Test Deepgram API key validity
    split-audio <audio_path>       Split large audio at silence points (--max-size, --max-deviation)
    chunk-text <input> <output_dir> Split text file into chunks by sentence boundary
    get-chapters <video_url>       Fetch YouTube video chapter metadata
    merge-content <work_dir> <output_file>  Merge processed chunks with chapter headers
"""

import argparse
import bisect
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path


def parse_vtt(vtt_path: str) -> str:
    """
    Parse VTT subtitle file, extract plain text

    Processing:
    - Remove VTT header (WEBVTT, Kind:, Language:)
    - Remove timestamp lines (00:00:00.000 --> 00:00:05.000)
    - Remove VTT tags (<c>, </c>, <00:00:01.000>, etc.)
    - Remove cue numbers (pure digit lines)
    - Remove consecutive duplicate lines (common in auto-captions)
    """
    path = Path(vtt_path)
    if not path.exists():
        print(f"Error: File does not exist {vtt_path}", file=sys.stderr)
        sys.exit(1)

    try:
        content = path.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    lines = content.split('\n')
    text_lines = []

    for line in lines:
        # Skip timestamp lines
        if '-->' in line:
            continue
        # Skip VTT header
        if line.startswith('WEBVTT') or line.startswith('Kind:') or line.startswith('Language:'):
            continue
        # Skip empty lines and pure digit lines (cue numbers)
        if not line.strip() or line.strip().isdigit():
            continue
        # Remove VTT tags
        clean_line = re.sub(r'<[^>]+>', '', line)
        if clean_line.strip():
            text_lines.append(clean_line.strip())

    # Remove consecutive duplicate lines
    deduplicated = []
    for line in text_lines:
        if not deduplicated or line != deduplicated[-1]:
            deduplicated.append(line)

    return ' '.join(deduplicated)


def process_deepgram(json_path: str) -> dict:
    """
    Process Deepgram API JSON result

    Processing:
    - Extract complete transcript text
    - Remove spaces between Chinese characters (multiple passes for thoroughness)
    - Fix spaces around punctuation
    - Remove consecutive repeated phrases
    - Count number of speakers

    Returns:
        {"transcript": "cleaned text", "speaker_count": N}
    """
    path = Path(json_path)
    if not path.exists():
        print(f"Error: File does not exist {json_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        print(f"Error: JSON parsing failed {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)

    try:
        transcript = data['results']['channels'][0]['alternatives'][0]['transcript']
    except (KeyError, IndexError) as e:
        print(f"Error: Deepgram JSON structure unexpected {e}", file=sys.stderr)
        sys.exit(2)

    # 1. Remove spaces between Chinese characters (multiple passes for thoroughness)
    for _ in range(10):
        transcript = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', transcript)

    # 2. Fix spaces around punctuation
    transcript = re.sub(r'\s+([。，！？、：；])', r'\1', transcript)

    # 3. Remove consecutive repeated phrases (3-20 characters)
    transcript = re.sub(r'([\u4e00-\u9fff]{3,20})\1{1,5}', r'\1', transcript)

    # 4. Get speaker count
    speakers = set()
    try:
        paragraphs = data['results']['channels'][0]['alternatives'][0].get('paragraphs', {}).get('paragraphs', [])
        for para in paragraphs:
            for sent in para.get('sentences', []):
                speakers.add(sent.get('speaker', 0))
    except (KeyError, TypeError):
        pass

    speaker_count = len(speakers) if speakers else 1

    return {
        "transcript": transcript,
        "speaker_count": speaker_count
    }


def sanitize_filename(title: str) -> str:
    """
    Clean illegal characters from filename

    Processing:
    - Replace illegal characters: / \\ : * ? " < > |
    - Remove leading/trailing spaces and periods
    - Limit length to 200 characters
    """
    # Replace illegal characters
    sanitized = re.sub(r'[/\\:*?"<>|]', '_', title)
    # Remove leading/trailing spaces and periods
    sanitized = sanitized.strip(' .')
    # Limit length
    if len(sanitized) > 200:
        sanitized = sanitized[:200]
    return sanitized


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


def test_deepgram_api(api_key: str) -> dict:
    """
    Quick test of Deepgram API key validity
    
    Makes a minimal request to verify:
    - API key is valid
    - Network connectivity works
    - Account has credits
    
    Returns:
        {"valid": bool, "error": str or None, "balance_warning": bool}
    """
    import urllib.request
    import urllib.error
    
    url = "https://api.deepgram.com/v1/listen?model=nova-2&language=en"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav"
    }
    
    # Send empty audio to trigger auth check (will fail with audio error if auth works)
    req = urllib.request.Request(url, data=b'', headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            # Unexpected success with empty audio
            return {"valid": True, "error": None, "balance_warning": False}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"valid": False, "error": "Invalid API key (401 Unauthorized)", "balance_warning": False}
        elif e.code == 402:
            return {"valid": False, "error": "Insufficient credits (402 Payment Required)", "balance_warning": True}
        elif e.code == 400:
            # Bad request usually means auth worked but audio was invalid
            return {"valid": True, "error": None, "balance_warning": False}
        else:
            return {"valid": False, "error": f"HTTP Error {e.code}: {e.reason}", "balance_warning": False}
    except urllib.error.URLError as e:
        return {"valid": False, "error": f"Network error: {e.reason}", "balance_warning": False}
    except Exception as e:
        return {"valid": False, "error": f"Unexpected error: {e}", "balance_warning": False}


def chunk_text(input_path: str, output_dir: str, chunk_size: int = 8000) -> dict:
    """
    Split text file into chunks by sentence boundary
    
    Algorithm:
    1. Read entire text file
    2. Split by sentence-ending punctuation (.!?。！？)
    3. Accumulate sentences until reaching chunk_size
    4. Write each chunk to separate file
    5. Generate manifest.json for tracking
    
    Args:
        input_path: Path to raw text file
        output_dir: Directory to write chunks
        chunk_size: Target size per chunk in characters (default 8000)
    
    Returns:
        {"total_chunks": N, "manifest_path": "...", "chunks": [...], "warnings": [...]}
    """
    path = Path(input_path)
    if not path.exists():
        print(f"Error: File does not exist {input_path}", file=sys.stderr)
        sys.exit(1)
    
    # Create output directory
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Read text content
    try:
        text = path.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Error: Cannot read file {e}", file=sys.stderr)
        sys.exit(2)
    
    # Split by sentence boundaries - more robust pattern
    # Handles: period/exclamation/question (EN and CN) followed by space OR followed by next sentence start
    # Also handles cases where there's no space after punctuation (common in Chinese)
    # Split by sentence boundaries - robust pattern
    # 1. Match punctuation: . ! ? 。 ！ ？
    # 2. Look behind to ensure it's punctuation
    # 3. Handle optional quotes/brackets often found after punctuation: " ' ” 」 )
    # 4. Require either whitespace OR a lookahead to specific sentence-starting chars (or end of string)
    # Ideally, we split AFTER punctuation (+ optional quote)
    
    # Simplified approach: Split after punctuation, then rejoin if needed
    # But here we use a split pattern that keeps the delimiter if wrapped in capturing group
    # However, re.split behavior with lookbehind is cleaner for "split at this boundary"
    
    # Improved pattern:
    # (?<=[.!?。！？])  Lookbehind: preceded by punctuation
    # [”"']?           Optional closing quote
    # (?:\s+|(?=[A-Z0-9\u4e00-\u9fff]))  Followed by whitespace OR followed by typical sentence starter (Capital, digit, Chinese)
    
    sentence_pattern = r'(?<=[.!?。！？][”"’」\)]?)(?:\s+|(?=[A-Z0-9\u4e00-\u9fff]))'
    try:
        sentences = re.split(sentence_pattern, text)
    except re.error:
        # Fallback to simple split if regex fails (e.g. strict lookbehind issues)
        sentences = re.split(r'(?<=[.!?。！？])\s+', text)
    
    # Filter empty sentences and strip whitespace
    sentences = [s.strip() for s in sentences if s.strip()]
    
    # Accumulate sentences into chunks
    chunks = []
    current_chunk = []
    current_size = 0
    warnings = []
    
    for i, sentence in enumerate(sentences):
        sentence_len = len(sentence)
        
        # Warning for extremely long sentences
        if sentence_len > chunk_size:
            warnings.append(f"Sentence {i} exceeds chunk_size ({sentence_len} > {chunk_size}), will be its own chunk")
        
        # If adding this sentence exceeds chunk_size and we have content, start new chunk
        if current_size + sentence_len > chunk_size and current_chunk:
            chunks.append(' '.join(current_chunk))
            current_chunk = [sentence]
            current_size = sentence_len
        else:
            current_chunk.append(sentence)
            current_size += sentence_len + 1  # +1 for space
    
    # Don't forget the last chunk
    if current_chunk:
        chunks.append(' '.join(current_chunk))
    
    # Write chunks to files and build manifest
    manifest = {
        "total_chunks": len(chunks),
        "chunk_size": chunk_size,
        "source_file": str(path.absolute()),  # Use absolute path for clarity
        "work_dir": str(out_dir.absolute()),   # Record work directory
        "chunks": []
    }
    
    for i, chunk_content in enumerate(chunks):
        chunk_filename = f"chunk_{i:03d}.txt"
        chunk_path = out_dir / chunk_filename
        chunk_path.write_text(chunk_content, encoding='utf-8')
        
        manifest["chunks"].append({
            "id": i,
            "raw_path": chunk_filename,
            "processed_path": f"processed_{i:03d}.md",
            "char_count": len(chunk_content),
            "status": "pending"
        })
    
    # Write manifest
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    
    # Print warnings if any
    for warning in warnings:
        print(f"⚠️ {warning}", file=sys.stderr)
    
    return {
        "total_chunks": len(chunks),
        "manifest_path": str(manifest_path),
        "chunks": [c["raw_path"] for c in manifest["chunks"]],
        "warnings": warnings
    }


def get_chapters(video_url: str, timeout: int = 30) -> dict:
    """
    Fetch YouTube video chapter metadata using yt-dlp
    
    Args:
        video_url: YouTube video URL
        timeout: Timeout in seconds for yt-dlp command (default 30)
    
    Returns:
        {"has_chapters": bool, "chapters": [{"title": ..., "start_time": ..., "end_time": ...}, ...]}
    """
    try:
        cmd = [
            "yt-dlp", "--print", "%(chapters)j", video_url
        ]
        # Don't use check=True - some warnings may cause non-zero exit but still output data
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout.strip()
        
        # Handle various empty/null cases
        if not output or output == 'null' or output == 'NA' or output == 'None':
            return {"has_chapters": False, "chapters": []}
        
        try:
            chapters = json.loads(output)
        except json.JSONDecodeError:
            # Sometimes yt-dlp outputs non-JSON error messages
            return {"has_chapters": False, "chapters": []}
        
        if not chapters or not isinstance(chapters, list):
            return {"has_chapters": False, "chapters": []}
        
        return {
            "has_chapters": True,
            "chapters": chapters
        }
    except subprocess.TimeoutExpired:
        print(f"Error: yt-dlp timed out after {timeout}s", file=sys.stderr)
        return {"has_chapters": False, "chapters": [], "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        print("Error: yt-dlp not found. Please install it: pip install yt-dlp", file=sys.stderr)
        return {"has_chapters": False, "chapters": [], "error": "yt-dlp not found"}
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return {"has_chapters": False, "chapters": [], "error": str(e)}


def merge_content(work_dir: str, output_file: str, header_content: str = "") -> dict:
    """
    Merge processed chunks with chapter headers based on chapter_plan.json
    
    Algorithm:
    1. Read manifest.json to get chunk list
    2. Read chapter_plan.json (if exists) to get chapter structure
    3. For each chunk:
       - If chunk ID matches a chapter start, insert chapter header
       - Append processed chunk content
    4. Write final merged file
    
    Args:
        work_dir: Directory containing manifest.json, chapter_plan.json, and processed_*.md files
        output_file: Path to write merged output
        header_content: Optional header content to prepend (e.g., YAML frontmatter)
    
    Returns:
        {"success": bool, "output_file": str, "total_lines": int, "chapters_inserted": int}
    """
    work_path = Path(work_dir)
    
    # Read manifest
    manifest_path = work_path / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)
    
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    
    # Read chapter plan (optional)
    chapter_plan_path = work_path / "chapter_plan.json"
    chapter_starts = {}  # {chunk_id: {"title_en": ..., "title_zh": ...}}
    if chapter_plan_path.exists():
        try:
            chapter_plan = json.loads(chapter_plan_path.read_text(encoding='utf-8'))
            if isinstance(chapter_plan, list):
                for chapter in chapter_plan:
                    if not isinstance(chapter, dict):
                        continue
                        
                    start_chunk = chapter.get("start_chunk")
                    # Ensure start_chunk is an integer
                    if start_chunk is not None:
                        try:
                            start_chunk_int = int(start_chunk)
                            chapter_starts[start_chunk_int] = {
                                "title_en": str(chapter.get("title_en", "")),
                                "title_zh": str(chapter.get("title_zh", ""))
                            }
                        except (ValueError, TypeError):
                            print(f"Warning: Invalid start_chunk value: {start_chunk}", file=sys.stderr)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Could not parse chapter_plan.json: {e}", file=sys.stderr)
    
    # Merge content
    output_lines = []
    chapters_inserted = 0  # Counts logical chapters, not individual title lines
    missing_files = []
    
    # Smart header handling - avoid duplicate separators
    if header_content:
        header_content = header_content.strip()
        output_lines.append(header_content)
        # Only add separator if header doesn't already end with one
        if not header_content.endswith('---'):
            output_lines.append("\n---\n")
        else:
            output_lines.append("\n")
    
    for chunk_info in manifest["chunks"]:
        chunk_id = chunk_info["id"]
        processed_path = work_path / chunk_info["processed_path"]
        
        # Check if this is the start of a new chapter
        if chunk_id in chapter_starts:
            chapter = chapter_starts[chunk_id]
            title_en = chapter["title_en"]
            title_zh = chapter["title_zh"]
            if title_en or title_zh:
                output_lines.append(f"\n## {title_en}\n")
                if title_zh:
                    output_lines.append(f"## {title_zh}\n")
                output_lines.append("\n")
                chapters_inserted += 1
        
        # Read and append processed content
        if processed_path.exists():
            content = processed_path.read_text(encoding='utf-8')
            output_lines.append(content)
            output_lines.append("\n")
        else:
            missing_files.append(str(processed_path))
            print(f"Warning: Processed file not found: {processed_path}", file=sys.stderr)
    
    # Write output file
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_content = ''.join(output_lines)
    output_path.write_text(final_content, encoding='utf-8')
    
    return {
        "success": len(missing_files) == 0,
        "output_file": str(output_path),
        "total_lines": final_content.count('\n'),
        "total_chars": len(final_content),
        "chapters_inserted": chapters_inserted,
        "missing_files": missing_files
    }


def load_config(config_path: str = None) -> dict:
    """
    Load configuration from config.yaml
    
    Args:
        config_path: Optional path to config file. 
                     Defaults to ~/.claude/skills/yt-transcript/config.yaml
    
    Returns:
        {"output_dir": "...", "deepgram_api_key": "...", "config_path": "..."}
    """
    # Default config path
    if config_path is None:
        config_path = os.path.expanduser("~/.claude/skills/yt-transcript/config.yaml")
    
    path = Path(config_path)
    if not path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    
    try:
        content = path.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Error: Cannot read config file: {e}", file=sys.stderr)
        sys.exit(2)
    
    # Simple YAML parsing for key: value pairs (no external dependency)
    config = {}
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            key, _, value = line.partition(':')
            key = key.strip()
            # Remove inline comments (e.g. value # comment) but allow # inside quotes if needed (simple approximation)
            if '#' in value:
                # Naive comment stripping: assume # starts a comment unless it's a color code or inside quotes
                # For this simple config, stripping from first # is likely safe enough
                value = value.split('#', 1)[0]
            
            value = value.strip().strip('"').strip("'")
            if key:
                config[key] = value
    
    # Expand ~ in output_dir
    output_dir = config.get('output_dir', '')
    if output_dir:
        output_dir = os.path.expanduser(output_dir)
        if not os.path.isdir(output_dir):
            print(f"Warning: output_dir does not exist: {output_dir}", file=sys.stderr)
    
    return {
        "output_dir": output_dir,
        "deepgram_api_key": config.get('deepgram_api_key', ''),
        "config_path": str(path.absolute())
    }


def main():
    parser = argparse.ArgumentParser(
        description='yt-transcript utility script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # parse-vtt command
    vtt_parser = subparsers.add_parser(
        'parse-vtt',
        help='Parse VTT subtitle file, output plain text'
    )
    vtt_parser.add_argument('vtt_path', help='VTT file path')

    # process-deepgram command
    dg_parser = subparsers.add_parser(
        'process-deepgram',
        help='Process Deepgram JSON, output cleaned text and speaker count'
    )
    dg_parser.add_argument('json_path', help='Deepgram JSON file path')

    # sanitize-filename command
    fn_parser = subparsers.add_parser(
        'sanitize-filename',
        help='Clean illegal filename characters'
    )
    fn_parser.add_argument('title', help='Original title')

    # test-deepgram-api command
    api_parser = subparsers.add_parser(
        'test-deepgram-api',
        help='Test Deepgram API key validity'
    )
    api_parser.add_argument('api_key', help='Deepgram API key')

    # split-audio command
    split_parser = subparsers.add_parser(
        'split-audio',
        help='Split large audio file at silence points'
    )
    split_parser.add_argument('audio_path', help='Audio file path')
    split_parser.add_argument('--max-size', type=float, default=10.0,
                              help='Max chunk size in MB (default: 10)')
    split_parser.add_argument('--max-deviation', type=float, default=60.0,
                              help='Max deviation from split point in seconds (default: 60)')

    # chunk-text command
    chunk_parser = subparsers.add_parser(
        'chunk-text',
        help='Split text file into chunks by sentence boundary'
    )
    chunk_parser.add_argument('input_path', help='Input text file path')
    chunk_parser.add_argument('output_dir', help='Output directory for chunks')
    chunk_parser.add_argument('--chunk-size', type=int, default=8000,
                              help='Target chunk size in characters (default: 8000)')

    # get-chapters command
    chapters_parser = subparsers.add_parser(
        'get-chapters',
        help='Fetch YouTube video chapter metadata'
    )
    chapters_parser.add_argument('video_url', help='YouTube video URL')

    # merge-content command
    merge_parser = subparsers.add_parser(
        'merge-content',
        help='Merge processed chunks with chapter headers'
    )
    merge_parser.add_argument('work_dir', help='Working directory with manifest.json')
    merge_parser.add_argument('output_file', help='Output file path')
    merge_parser.add_argument('--header', default='', help='Optional header content to prepend')

    # load-config command
    config_parser = subparsers.add_parser(
        'load-config',
        help='Load and return configuration from config.yaml'
    )
    config_parser.add_argument('--config-path', default=None,
                               help='Optional path to config file')

    args = parser.parse_args()

    if args.command == 'parse-vtt':
        result = parse_vtt(args.vtt_path)
        print(result)

    elif args.command == 'process-deepgram':
        result = process_deepgram(args.json_path)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'sanitize-filename':
        result = sanitize_filename(args.title)
        print(result)

    elif args.command == 'test-deepgram-api':
        result = test_deepgram_api(args.api_key)
        print(json.dumps(result, ensure_ascii=False))
        if not result['valid']:
            sys.exit(1)

    elif args.command == 'split-audio':
        result = split_audio(args.audio_path, args.max_size, args.max_deviation)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'chunk-text':
        result = chunk_text(args.input_path, args.output_dir, args.chunk_size)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'get-chapters':
        result = get_chapters(args.video_url)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'merge-content':
        result = merge_content(args.work_dir, args.output_file, args.header)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'load-config':
        result = load_config(args.config_path)
        print(json.dumps(result, ensure_ascii=False))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()

