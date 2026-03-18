import json
import re
import time
from pathlib import Path


GLOSSARY_SCHEMA_VERSION = 1
GLOSSARY_FORMAT = "yt_transcript.glossary/v1"
GLOSSARY_FILENAME = "glossary.json"
MAX_SOURCE_CHUNKS_PER_TERM = 5
COMMON_CAPITALIZED_STOPWORDS = {
    "And", "But", "For", "From", "How", "Into", "The", "This", "That", "These", "Those",
    "When", "Where", "Which", "While", "With", "Without", "Why", "What", "First", "Second",
    "Third", "Fourth", "Fifth", "Then", "There", "Here", "Today", "Tomorrow", "Yesterday",
}
TERM_PATTERNS = [
    re.compile(r"\b[A-Z]{2,}(?:[A-Z0-9_-]{0,})\b"),
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z]{2,}[a-z]*)+\b"),
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b"),
    re.compile(r"\b[A-Za-z]+\d+[A-Za-z0-9_-]*\b"),
    re.compile(r"\b[A-Z][a-z]{4,}\b"),
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def glossary_path_for(work_dir: str) -> Path:
    return Path(str(work_dir or "")).expanduser().resolve() / GLOSSARY_FILENAME


def load_glossary(work_dir: str) -> dict:
    glossary_path = glossary_path_for(work_dir)
    try:
        payload = json.loads(glossary_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_candidate_terms(text: str) -> list[str]:
    candidates = []
    for pattern in TERM_PATTERNS:
        for match in pattern.findall(text or ""):
            term = str(match or "").strip()
            if len(term) < 2:
                continue
            if term in COMMON_CAPITALIZED_STOPWORDS:
                continue
            if term.isdigit():
                continue
            candidates.append(term)
    return candidates


def _iter_manifest_source_chunks(work_dir: str) -> list[tuple[int, str]]:
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    manifest_path = work_path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    chunks = manifest.get("chunks", []) if isinstance(manifest.get("chunks", []), list) else []

    collected = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_id = int(chunk.get("id", chunk.get("chunk_id", 0)) or 0)
        raw_path = str(chunk.get("raw_path", "")).strip()
        if not raw_path:
            continue
        candidate_path = work_path / raw_path
        try:
            content = candidate_path.read_text(encoding="utf-8")
        except OSError:
            continue
        collected.append((chunk_id, content))
    return collected


def build_glossary(work_dir: str, *, max_terms: int = 50,
                   min_occurrences: int = 1) -> dict:
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    if not work_path.exists():
        glossary_path = glossary_path_for(str(work_path))
        return {
            "success": False,
            "work_dir": str(work_path),
            "glossary_path": str(glossary_path),
            "error": f"Work directory not found: {work_path}",
        }

    chunk_payloads = _iter_manifest_source_chunks(str(work_path))
    term_counts = {}
    term_chunk_ids = {}
    for chunk_id, text in chunk_payloads:
        for term in _extract_candidate_terms(text):
            term_counts[term] = term_counts.get(term, 0) + 1
            term_chunk_ids.setdefault(term, set()).add(chunk_id)

    filtered_terms = [
        term for term, count in term_counts.items()
        if count >= max(1, int(min_occurrences))
    ]
    filtered_terms.sort(key=lambda term: (-term_counts[term], term.lower(), term))
    limited_terms = filtered_terms[:max(0, int(max_terms))]

    glossary_terms = []
    for term in limited_terms:
        glossary_terms.append({
            "term": term,
            "count": term_counts[term],
            "preserve_exact": True,
            "preferred_output": "",
            "chunk_ids": sorted(term_chunk_ids.get(term, set()))[:MAX_SOURCE_CHUNKS_PER_TERM],
        })

    glossary_path = glossary_path_for(str(work_path))
    payload = {
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "format": GLOSSARY_FORMAT,
        "work_dir": str(work_path),
        "glossary_path": str(glossary_path),
        "generated_at": _now_iso(),
        "source": "manifest_raw_chunks",
        "term_count": len(glossary_terms),
        "terms": glossary_terms,
    }
    glossary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True,
        **payload,
    }


def select_glossary_terms(glossary_payload: dict, source_text: str, *, max_terms: int = 8) -> list[dict]:
    source = str(source_text or "")
    terms = glossary_payload.get("terms", []) if isinstance(glossary_payload.get("terms", []), list) else []
    matched = []
    lowered_source = source.lower()
    for entry in terms:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term", "")).strip()
        if not term:
            continue
        if term.lower() in lowered_source:
            matched.append(entry)
    matched.sort(key=lambda entry: (-int(entry.get("count", 0) or 0), str(entry.get("term", "")).lower()))
    return matched[:max(0, int(max_terms))]


def build_glossary_prompt_context(glossary_payload: dict, source_text: str, *, max_terms: int = 8) -> dict:
    selected_terms = select_glossary_terms(glossary_payload, source_text, max_terms=max_terms)
    if not selected_terms:
        return {
            "text": "",
            "terms": [],
        }

    lines = [
        "## Terminology Guardrails",
        "",
        "Keep terminology consistent with the document-level glossary.",
        "If a listed term appears in the current chunk, preserve it consistently in the output.",
        "",
    ]
    for entry in selected_terms:
        term = str(entry.get("term", "")).strip()
        preferred_output = str(entry.get("preferred_output", "")).strip()
        if preferred_output:
            lines.append(f"- {term} => {preferred_output}")
        else:
            lines.append(f"- {term} (keep exact term consistent)")
    return {
        "text": "\n".join(lines).strip(),
        "terms": selected_terms,
    }


def evaluate_glossary_terms(glossary_payload: dict, source_text: str, result_text: str, *,
                            max_terms: int = 8) -> dict:
    selected_terms = select_glossary_terms(glossary_payload, source_text, max_terms=max_terms)
    if not selected_terms:
        return {
            "warnings": [],
            "retry_reasons": [],
            "matched_terms": [],
            "missing_terms": [],
        }

    result = str(result_text or "")
    missing_terms = []
    for entry in selected_terms:
        term = str(entry.get("term", "")).strip()
        preferred_output = str(entry.get("preferred_output", "")).strip()
        expected = preferred_output or term
        if expected and expected not in result:
            missing_terms.append(term)

    warnings = []
    retry_reasons = []
    if missing_terms:
        warnings.append(
            "⚠️ Glossary consistency: expected terms missing from output: " + ", ".join(missing_terms)
        )
        retry_reasons.append("missing_glossary_terms")

    return {
        "warnings": warnings,
        "retry_reasons": retry_reasons,
        "matched_terms": [str(entry.get("term", "")).strip() for entry in selected_terms],
        "missing_terms": missing_terms,
    }
