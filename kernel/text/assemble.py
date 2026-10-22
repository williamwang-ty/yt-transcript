"""Final article assembly: frontmatter, title and body into the output Markdown file."""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from pathlib import Path


def _yaml_string(value: str) -> str:
    # Intentionally quote every scalar. The frontmatter should favor predictable
    # parsing over pretty YAML, especially for titles/channels that may contain
    # punctuation, quotes, or comment-like characters.
    """Yaml string."""
    return json.dumps(str(value).replace("\r\n", "\n"), ensure_ascii=False)


def _single_line_text(value: str) -> str:
    """Single line text."""
    return " ".join(str(value).split())


def _escape_markdown_text(value: str) -> str:
    """
    Escape Markdown-significant characters in inline text contexts.
    """
    text = _single_line_text(value)
    return re.sub(r'([\\`*_{}\[\]()#+!<>|])', r'\\\1', text)


def _sanitize_markdown_url(value: str) -> str:
    """
    Encode a URL so it remains valid inside Markdown link destinations.
    """
    text = _single_line_text(value)
    return urllib.parse.quote(text, safe=":/?&=#%@+,-._~")


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


def _is_valid_compact_date_fragment(value: str) -> bool:
    """Return whether an 8-digit string is a real YYYYMMDD calendar date."""
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return False
    try:
        time.strptime(text, "%Y%m%d")
    except ValueError:
        return False
    return True


def _normalize_frontmatter_date(value: str) -> str:
    """Normalize compact YYYYMMDD dates for article frontmatter."""
    text = str(value or "").strip()
    if _is_valid_compact_date_fragment(text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def assemble_final(optimized_text_path: str, output_file: str,
                    title: str = "", source: str = "", channel: str = "",
                    date: str = "", created: str = "", duration: int = 0,
                    transcript_source: str = "", bilingual: bool = False) -> dict:
    """
    Assemble final markdown file from optimized text and metadata.
    
    Pure file operation: reads optimized text, prepends YAML frontmatter
    and metadata header, appends footer, writes to output file.
    The Agent never needs to read the optimized text into its context.
    
    Args:
        optimized_text_path: Path to the optimized text file
        output_file: Path to write the final markdown file
        title: Video title
        source: Video URL
        channel: Channel name
        date: Video upload date
        created: File creation date (today)
        duration: Video duration in seconds
        transcript_source: 'youtube' or 'deepgram'
        bilingual: Whether the content is bilingual
    
    Returns:
        {"success": bool, "output_file": str, "total_chars": int, "total_lines": int}
    """
    # Read optimized text
    opt_path = Path(optimized_text_path)
    if not opt_path.exists():
        print(f"Error: Optimized text file not found: {optimized_text_path}", file=sys.stderr)
        sys.exit(1)
    
    try:
        optimized_text = opt_path.read_text(encoding='utf-8').strip()
    except Exception as e:
        print(f"Error: Cannot read optimized text file: {e}", file=sys.stderr)
        sys.exit(2)
    
    # Calculate duration in minutes
    duration_min = duration // 60 if duration > 0 else 0
    bilingual_str = "true" if bilingual else "false"
    language_mode = "Bilingual" if bilingual else "Chinese"
    safe_title = _escape_markdown_text(title)
    safe_channel = _escape_markdown_text(channel)
    safe_source = _sanitize_markdown_url(source)
    frontmatter_date = _normalize_frontmatter_date(date)
    frontmatter_created = _normalize_frontmatter_date(created)
    
    # Build YAML frontmatter
    frontmatter_lines = [
        "---",
        f"title: {_yaml_string(title)}",
        f"source: {_yaml_string(source)}",
        f"url: {_yaml_string(source)}",
        f"channel: {_yaml_string(channel)}",
        f"author: {_yaml_string(channel)}",
    ]
    if frontmatter_date:
        frontmatter_lines.append(f"date: {_yaml_string(frontmatter_date)}")
    frontmatter_lines.extend([
        f"created: {_yaml_string(frontmatter_created)}",
        "type: video-transcript",
        f"bilingual: {bilingual_str}",
        f"duration: {_yaml_string(f'{duration_min}m')}",
        f"transcript_source: {_yaml_string(transcript_source)}",
        "tags: [youtube, video-transcript]",
        f"via: {_yaml_string('youtube')}",
        "---",
    ])
    
    # Build header section
    header_lines = [
        "",
        f"# {safe_title}",
        "",
        f"> Video source: [YouTube - {safe_channel}]({safe_source})",
        f"> Language mode: {language_mode}",
        f"> Duration: {duration_min} minutes",
        "",
        "---",
        "",
    ]
    
    # Build footer
    footer_lines = [
        "",
        "---",
        "",
        f"*This article was generated by AI voice transcription ({transcript_source}), for reference only.*",
        "",
    ]
    
    # Assemble final content
    final_content = '\n'.join(frontmatter_lines) + '\n' + '\n'.join(header_lines) + optimized_text + '\n' + '\n'.join(footer_lines)
    
    # Write output file
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(final_content, encoding='utf-8')
    
    return {
        "success": True,
        "output_file": str(out_path.absolute()),
        "total_chars": len(final_content),
        "total_lines": final_content.count('\n') + 1
    }
