"""The quality check of the verify stage: structure, coverage, repetition, glossary drift and semantic anchors."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from kernel.quality import semantic as kernel_semantic
from kernel.text import glossary as kernel_glossary
from kernel.text import window as kernel_window
from kernel.text.cleanup import overlap as kernel_overlap
from kernel.text.cleanup.cjk import is_cjk_family_char
from kernel.text.measure import (
    QUALITY_PUNCTUATION_CHARS,
    count_cjk_chars,
    count_quality_chars,
    is_quality_fragment_paragraph,
)

QUALITY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
QUALITY_CJK_SPACE_PATTERNS = (
    re.compile(r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+(?=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])"),
    re.compile(r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+(?=[，。！？、；：,.!?;:])"),
    re.compile(r"(?<=[（《「『【(])\s+(?=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])"),
    re.compile(r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+(?=[）》」』】)])"),
)
# Paragraph length past which the prompts ask the model to look for a split.
PARAGRAPH_SPLIT_ABOVE_CHARS = 500
PARAGRAPH_SPLIT_ABOVE_WORDS = 250


def _has_unbalanced_terminal_markers(text: str) -> bool:
    """Return whether the trailing line has obvious unclosed quotes or brackets."""
    sample = str(text or "")
    pairs = [("(", ")"), ("（", "）"), ("[", "]"), ("{", "}"), ('"', '"'), ("“", "”"), ("‘", "’")]
    for opener, closer in pairs:
        if opener == closer:
            if sample.count(opener) % 2 == 1:
                return True
            continue
        if sample.count(opener) > sample.count(closer):
            return True
    return False


def _line_looks_truncated(last_line: str) -> bool:
    """Return whether the final visible line has strong cut-off signals."""
    text = str(last_line or "").strip()
    if not text:
        return True

    proper_endings = ('.', '!', '?', '。', '！', '？', '*', '`', '"', ')', '）', '」', '>', '-', ':', '：')
    if text.endswith(proper_endings) or text.startswith('#') or len(text) < 10:
        return False

    unfinished_tail_patterns = [
        r'[，,、；;—-]$',
        r'(?:and|or|but|because|so|to|with|for|of|in|on|at|by|the|a|an)$',
        r'(?:的|了|和|与|并|但|而|及|在|把|将|对|让|给|从|向|为|等)$',
    ]
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in unfinished_tail_patterns):
        return True
    if _has_unbalanced_terminal_markers(text):
        return True
    return len(text) > 120 and text[-1].isalnum()


def _tokenize_quality_text(text: str) -> list[str]:
    """Tokenize text into Latin words and single CJK-family characters."""
    tokens = []
    for token in QUALITY_TOKEN_RE.findall(str(text or "")):
        tokens.append(token.lower() if token.isascii() else token)
    return tokens


def _is_strong_quality_overlap(text: str) -> bool:
    """Return whether an adjacent overlap is suspicious enough to warn about."""
    candidate = str(text or "").strip()
    if not candidate:
        return False

    meaningful_chars = count_quality_chars(candidate)
    if any(is_cjk_family_char(char) for char in candidate):
        return meaningful_chars >= 4
    return meaningful_chars >= 12 and len(_tokenize_quality_text(candidate)) >= 3


def _count_chunk_seam_warnings(paragraphs: list[str]) -> int:
    """Count duplicated adjacent paragraph seams that survived post-merge cleanup."""
    warning_count = 0
    normalized_blocks = [re.sub(r"\s+", " ", block.strip()) for block in paragraphs if block.strip()]
    for previous, current in zip(normalized_blocks, normalized_blocks[1:]):
        if previous == current:
            warning_count += 1
            continue
        overlap = kernel_overlap.find_leading_overlap(previous, current)
        if _is_strong_quality_overlap(overlap):
            warning_count += 1
    return warning_count


def _compute_duplicate_ngram_metrics(paragraphs: list[str], *, ngram_size: int = 5) -> dict:
    """Measure repeated phrase density inside paragraph bodies."""
    windows = []
    for block in paragraphs:
        tokens = _tokenize_quality_text(block)
        if len(tokens) < ngram_size:
            continue
        for idx in range(len(tokens) - ngram_size + 1):
            window = tuple(tokens[idx:idx + ngram_size])
            if len(set(window)) < 2:
                continue
            windows.append(window)

    total_windows = len(windows)
    if total_windows == 0:
        return {
            "duplicate_ngram_ratio": 0.0,
            "duplicate_ngram_count": 0,
            "duplicate_ngram_windows": 0,
        }

    counts = Counter(windows)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    ratio = duplicate_count / total_windows if total_windows else 0.0
    return {
        "duplicate_ngram_ratio": round(ratio, 3),
        "duplicate_ngram_count": duplicate_count,
        "duplicate_ngram_windows": total_windows,
    }


def _compute_cjk_readability_metrics(body_paragraphs: list[str]) -> dict:
    """Compute advisory readability signals for Chinese-heavy body text."""
    cjk_body_paragraphs = [block for block in body_paragraphs if count_cjk_chars(block) >= 4]
    cjk_text = "\n\n".join(cjk_body_paragraphs)
    cjk_chars = count_cjk_chars(cjk_text)
    cjk_applicable = cjk_chars >= 24 and len(cjk_body_paragraphs) >= 2

    spacing_anomaly_count = sum(
        1
        for pattern in QUALITY_CJK_SPACE_PATTERNS
        for _ in pattern.finditer(cjk_text)
    )
    cjk_space_ratio = spacing_anomaly_count / cjk_chars if cjk_chars else 0.0

    short_paragraph_count = sum(1 for block in cjk_body_paragraphs if is_quality_fragment_paragraph(block))
    short_paragraph_ratio = short_paragraph_count / len(cjk_body_paragraphs) if cjk_body_paragraphs else 0.0

    punctuation_count = sum(1 for char in cjk_text if char in QUALITY_PUNCTUATION_CHARS)
    meaningful_chars = count_quality_chars(cjk_text)
    punctuation_density = punctuation_count / meaningful_chars if meaningful_chars else 0.0

    duplicate_metrics = _compute_duplicate_ngram_metrics(cjk_body_paragraphs)
    cjk_spacing_ok = (not cjk_applicable) or not (
        spacing_anomaly_count >= 2 and cjk_space_ratio > 0.01
    )
    duplicate_ngram_ok = (not cjk_applicable) or not (
        duplicate_metrics["duplicate_ngram_count"] >= 4 and duplicate_metrics["duplicate_ngram_ratio"] > 0.06
    )
    short_paragraph_ok = (not cjk_applicable) or not (
        len(cjk_body_paragraphs) >= 4
        and short_paragraph_count >= 4
        and short_paragraph_ratio > 0.45
    )
    punctuation_density_ok = (not cjk_applicable) or not (
        cjk_chars >= 120 and punctuation_density < 0.01
    )

    return {
        "cjk_readability_applicable": cjk_applicable,
        "cjk_body_paragraph_count": len(cjk_body_paragraphs),
        "cjk_body_char_count": cjk_chars,
        "cjk_spacing_anomaly_count": spacing_anomaly_count,
        "cjk_space_ratio": round(cjk_space_ratio, 3),
        "cjk_spacing_ok": cjk_spacing_ok,
        "duplicate_ngram_ratio": duplicate_metrics["duplicate_ngram_ratio"],
        "duplicate_ngram_count": duplicate_metrics["duplicate_ngram_count"],
        "duplicate_ngram_windows": duplicate_metrics["duplicate_ngram_windows"],
        "duplicate_ngram_ok": duplicate_ngram_ok,
        "short_paragraph_count": short_paragraph_count,
        "short_paragraph_ratio": round(short_paragraph_ratio, 3),
        "fragment_paragraph_ratio": round(short_paragraph_ratio, 3),
        "short_paragraph_ratio_ok": short_paragraph_ok,
        "punctuation_density": round(punctuation_density, 3),
        "punctuation_density_ok": punctuation_density_ok,
    }


def _compute_paragraph_length_metrics(body_paragraphs: list[str], *, bilingual: bool) -> dict:
    """Measure the longest paragraph against the length guide in the prompts (advisory only).

    Chinese output is counted in characters; bilingual output by its English
    paragraphs in words, since each Chinese translation follows its English
    paragraph. The limits are the "split above" points of the paragraph rule
    in prompts/cleanup_zh.md and prompts/structure_only.md.
    """
    if bilingual:
        unit, limit = "words", PARAGRAPH_SPLIT_ABOVE_WORDS
        lengths = [len(block.split()) for block in body_paragraphs if count_cjk_chars(block) < 4]
    else:
        unit, limit = "chars", PARAGRAPH_SPLIT_ABOVE_CHARS
        lengths = [len(re.sub(r"\s+", "", block)) for block in body_paragraphs if count_cjk_chars(block) >= 4]
    return {
        "paragraph_length_unit": unit,
        "max_paragraph_len": max(lengths, default=0),
        "over_limit_paragraph_count": sum(1 for length in lengths if length > limit),
        # A paragraph with a single line break inside may be two paragraphs
        # written without the blank line between them.
        "paragraph_line_break_count": sum(1 for block in body_paragraphs if "\n" in block),
    }


def _count_bilingual_paragraph_pairs(paragraphs: list[str]) -> int:
    """Count adjacent English-to-Chinese body paragraph pairs."""
    normalized_blocks = [str(block or "").strip() for block in paragraphs if str(block or "").strip()]
    paired_blocks = 0
    for first, second in zip(normalized_blocks, normalized_blocks[1:]):
        first_has_english = any(ch.isascii() and ch.isalpha() for ch in first)
        second_has_chinese = any('\u4e00' <= ch <= '\u9fff' for ch in second)
        if first_has_english and second_has_chinese:
            paired_blocks += 1
    return paired_blocks


def _load_quality_glossary_payload(*, work_dir: str = "", glossary_path: str = "") -> dict:
    """Load glossary payload for final quality verification."""
    explicit_path_text = str(glossary_path or "").strip()
    if explicit_path_text:
        explicit_path = Path(explicit_path_text).expanduser()
        try:
            payload = json.loads(explicit_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    resolved_work_dir = str(work_dir or "").strip()
    if resolved_work_dir:
        return kernel_glossary.load_glossary(resolved_work_dir)
    return {}


def verify_quality(optimized_text_path: str, raw_text_path: str = None,
                   bilingual: bool = False, work_dir: str = "",
                   glossary_path: str = "", glossary_max_terms: int = 12) -> dict:
    """
    Verify quality of optimized text file with structural checks.
    
    Pure file-based validation. The Agent only reads the JSON report,
    never the actual text content. Checks:
    1. File exists and is non-empty
    2. Has section headers (##)
    3. No abrupt truncation (last line is complete)
    4. Bilingual balance (Chinese char ratio in expected range)
    5. Chinese readability warnings (spacing, duplication, paragraph fragmentation)
    6. Size ratio vs raw text (if raw_text_path provided)
    7. Glossary drift warnings (if glossary context is available)
    
    Args:
        optimized_text_path: Path to the optimized text file
        raw_text_path: Optional path to raw text file for comparison
        bilingual: Whether the content should be bilingual
    
    Returns:
        {"passed": bool, "checks": {...}, "warnings": [...], "hard_failures": [...]}

    Stop/go contract:
        - non-empty hard_failures => STOP
        - warnings only => review before proceeding
    """
    warnings = []
    hard_failures = []
    checks = {}
    
    # Read optimized text
    opt_path = Path(optimized_text_path)
    if not opt_path.exists():
        return {
            "passed": False,
            "checks": {"file_exists": False},
            "warnings": [],
            "hard_failures": ["Optimized text file not found"],
        }
    
    try:
        text = opt_path.read_text(encoding='utf-8')
    except Exception as e:
        return {
            "passed": False,
            "checks": {"file_exists": True, "file_readable": False},
            "warnings": [],
            "hard_failures": [f"Cannot read file: {e}"],
        }
    
    total_chars = len(text)
    total_lines = text.count('\n') + 1
    
    checks["file_exists"] = True
    checks["total_chars"] = total_chars
    checks["total_lines"] = total_lines
    paragraphs = [block.strip() for block in re.split(r'\n\s*\n', text) if block.strip()]
    body_paragraphs = [
        block for block in paragraphs
        if not block.startswith('#') and not block.startswith('>') and not block.startswith('---')
    ]
    checks["paragraph_count"] = len(body_paragraphs)
    checks.update(_compute_paragraph_length_metrics(body_paragraphs, bilingual=bilingual))

    # Check 1: Non-empty
    checks["non_empty"] = total_chars > 0
    if not checks["non_empty"]:
        hard_failures.append("File is empty")
    
    # Check 2: Has section headers
    section_headers = re.findall(r'^##\s+.+', text, re.MULTILINE)
    checks["section_count"] = len(section_headers)
    checks["has_sections"] = len(section_headers) > 0
    if not checks["has_sections"] and total_chars > 1200:
        hard_failures.append(f"No section headers (##) found in {total_chars} chars of text")
    if len(body_paragraphs) < 2 and total_chars > 400:
        warnings.append(f"Only {len(body_paragraphs)} body paragraph found in {total_chars} chars of text")
    
    # Check 3: No abrupt truncation
    lines = [l for l in text.strip().split("\n") if l.strip()]
    if lines:
        last_line = lines[-1].strip()
        checks["no_truncation"] = not _line_looks_truncated(last_line)
        if not checks["no_truncation"]:
            warnings.append(f'Possible truncation: last line looks incomplete: "{last_line[-50:]}"')
    else:
        checks["no_truncation"] = False
        warnings.append("No non-empty lines found")

    chunk_seam_warning_count = _count_chunk_seam_warnings(body_paragraphs)
    checks["chunk_seam_warning_count"] = chunk_seam_warning_count
    checks["chunk_seam_duplication_ok"] = chunk_seam_warning_count == 0
    if chunk_seam_warning_count:
        warnings.append(
            "Chunk seam duplication signals detected between adjacent paragraphs "
            f"({chunk_seam_warning_count} suspect boundary/boundaries)"
        )

    body_char_count = sum(len(block) for block in body_paragraphs)
    header_density = len(section_headers) / len(body_paragraphs) if body_paragraphs else float(len(section_headers))
    avg_chars_per_section = body_char_count / len(section_headers) if section_headers else float(body_char_count)
    checks["header_density"] = round(header_density, 3)
    checks["avg_chars_per_section"] = round(avg_chars_per_section, 1)
    checks["header_fragment_balance_ok"] = not (
        len(section_headers) >= 4
        and len(body_paragraphs) >= 4
        and header_density > 0.6
        and avg_chars_per_section < 220
    )
    if not checks["header_fragment_balance_ok"]:
        warnings.append(
            "Header density looks unusually high for the available body text "
            f"({len(section_headers)} sections across {len(body_paragraphs)} body paragraphs)"
        )

    cjk_readability_checks = _compute_cjk_readability_metrics(body_paragraphs)
    checks.update(cjk_readability_checks)
    if not checks["cjk_spacing_ok"]:
        warnings.append(
            "Chinese spacing anomalies detected "
            f"({checks['cjk_spacing_anomaly_count']} issue(s), ratio {checks['cjk_space_ratio']:.3f})"
        )
    if not checks["duplicate_ngram_ok"]:
        warnings.append(
            "Repeated phrase density is high in Chinese body text "
            f"(ratio {checks['duplicate_ngram_ratio']:.3f})"
        )
    if not checks["short_paragraph_ratio_ok"]:
        warnings.append(
            "Too many short Chinese body paragraphs look like subtitle fragments "
            f"({checks['short_paragraph_count']} of {checks['cjk_body_paragraph_count']})"
        )
    if not checks["punctuation_density_ok"]:
        warnings.append(
            "Chinese punctuation density looks unusually low "
            f"(density {checks['punctuation_density']:.3f})"
        )

    checks["glossary_drift_applicable"] = False
    checks["glossary_selected_term_count"] = 0
    checks["glossary_matched_term_count"] = 0
    checks["glossary_drift_count"] = 0
    checks["glossary_preservation_ratio"] = 1.0
    checks["glossary_drift_ok"] = True

    # Check 4: Bilingual balance
    if bilingual:
        cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        en_chars = sum(1 for c in text if c.isascii() and c.isalpha())
        cn_ratio = cn_chars / total_chars if total_chars > 0 else 0
        en_ratio = en_chars / total_chars if total_chars > 0 else 0
        
        checks["cn_char_ratio"] = round(cn_ratio, 3)
        checks["en_char_ratio"] = round(en_ratio, 3)
        
        # Expect both languages present in reasonable proportions
        checks["bilingual_balanced"] = cn_ratio > 0.1 and en_ratio > 0.05
        if not checks["bilingual_balanced"]:
            if cn_ratio < 0.1:
                warnings.append(f"Chinese character ratio too low ({cn_ratio:.1%}), translation may be missing")
            if en_ratio < 0.05:
                warnings.append(f"English character ratio too low ({en_ratio:.1%}), original text may be missing")

        text_blocks = body_paragraphs
        paired_blocks = _count_bilingual_paragraph_pairs(text_blocks)
        checks["bilingual_pairs"] = paired_blocks
        if text_blocks and paired_blocks == 0:
            hard_failures.append("No English/Chinese paragraph pairs detected in bilingual output")
    
    # Check 5: Size ratio vs raw text
    if raw_text_path:
        raw_path = Path(raw_text_path)
        if raw_path.exists():
            try:
                raw_text = raw_path.read_text(encoding='utf-8')
                raw_chars = len(raw_text)
                if raw_chars > 0:
                    size_ratio = total_chars / raw_chars
                    checks["raw_text_chars"] = raw_chars
                    checks["size_ratio_vs_raw"] = round(size_ratio, 2)
                    
                    # For bilingual, expect ~1.5-3x; for monolingual, expect ~0.8-1.5x
                    if bilingual:
                        checks["size_ratio_ok"] = 1.2 <= size_ratio <= 4.0
                        if not checks["size_ratio_ok"]:
                            warnings.append(f"Size ratio {size_ratio:.2f}x vs raw text is outside expected range (1.2-4.0x for bilingual)")
                    else:
                        checks["size_ratio_ok"] = 0.7 <= size_ratio <= 2.0
                        if not checks["size_ratio_ok"]:
                            warnings.append(f"Size ratio {size_ratio:.2f}x vs raw text is outside expected range (0.7-2.0x for monolingual)")

                    semantic_evaluation = kernel_semantic.evaluate_semantic_anchors(raw_text, text, max_items=12)
                    checks["semantic_anchor_count"] = len(semantic_evaluation["anchors"].get("ordered", []))
                    checks["semantic_missing_count"] = len(semantic_evaluation["missing_anchors"])
                    checks["semantic_anchor_coverage_ok"] = len(semantic_evaluation["missing_anchors"]) == 0
                    if semantic_evaluation["missing_anchors"]:
                        warnings.extend(semantic_evaluation["warnings"])

                    glossary_payload = _load_quality_glossary_payload(
                        work_dir=work_dir,
                        glossary_path=glossary_path,
                    )
                    glossary_evaluation = kernel_glossary.evaluate_glossary_drift(
                        glossary_payload,
                        raw_text,
                        text,
                        max_terms=max(0, int(glossary_max_terms)),
                    ) if glossary_payload else {
                        "warnings": [],
                        "selected_terms": [],
                        "preserved_terms": [],
                        "missing_terms": [],
                        "glossary_drift_count": 0,
                        "preservation_ratio": 1.0,
                    }
                    selected_terms = glossary_evaluation.get("selected_terms", []) if isinstance(glossary_evaluation.get("selected_terms", []), list) else []
                    preserved_terms = glossary_evaluation.get("preserved_terms", []) if isinstance(glossary_evaluation.get("preserved_terms", []), list) else []
                    checks["glossary_drift_applicable"] = bool(selected_terms)
                    checks["glossary_selected_term_count"] = len(selected_terms)
                    checks["glossary_matched_term_count"] = len(preserved_terms)
                    checks["glossary_drift_count"] = int(glossary_evaluation.get("glossary_drift_count", 0) or 0)
                    checks["glossary_preservation_ratio"] = float(glossary_evaluation.get("preservation_ratio", 1.0) or 0.0)
                    checks["glossary_drift_ok"] = checks["glossary_drift_count"] == 0
                    if glossary_evaluation["warnings"]:
                        warnings.extend(glossary_evaluation["warnings"])
            except Exception:
                pass  # Non-critical, skip if raw text unreadable

    window_state = kernel_window.load_window_state(work_dir) if work_dir else {}
    if window_state:
        window_coverage = kernel_window.coverage_report(window_state)
        checks["window_coverage_ratio"] = window_coverage["coverage_ratio"]
        checks["window_uncovered_ranges"] = window_coverage["uncovered_ranges"]
        checks["window_low_output_ratio_paragraphs"] = window_coverage["low_output_ratio_paragraphs"]
        checks["window_fallback_paragraphs"] = window_coverage["fallback_paragraphs"]
        if window_coverage["uncovered_ranges"]:
            preview = ", ".join(window_coverage["uncovered_ranges"][:5])
            warnings.append(f"Window run left source sentences without output: {preview}")
        if window_coverage["low_output_ratio_paragraphs"]:
            preview = ", ".join(item["sentences"] for item in window_coverage["low_output_ratio_paragraphs"][:5])
            warnings.append(f"Window paragraphs much shorter than their source (possible skipped content): {preview}")
    
    # Overall result
    passed = len(hard_failures) == 0
    
    return {
        "passed": passed,
        "checks": checks,
        "warnings": warnings,
        "hard_failures": hard_failures,
    }
