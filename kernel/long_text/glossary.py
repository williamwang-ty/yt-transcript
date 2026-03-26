"""Glossary extraction and terminology-consistency helpers for long-text jobs."""

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
TRANSCRIPT_SOURCE_WEIGHTS = {
    "raw_chunks": 1,
    "source_title": 6,
    "source_channel": 5,
    "chapter_titles": 4,
    "metadata_description": 2,
}
HIGH_PRIORITY_SOURCE_KINDS = {"source_title", "source_channel", "chapter_titles", "metadata_description"}


def _now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def glossary_path_for(work_dir: str) -> Path:
    """Return the glossary file path for a work directory."""
    return Path(str(work_dir or "")).expanduser().resolve() / GLOSSARY_FILENAME


def load_glossary(work_dir: str) -> dict:
    """Load the persisted glossary payload, or return an empty default shell."""
    glossary_path = glossary_path_for(work_dir)
    try:
        payload = json.loads(glossary_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_candidate_terms(text: str) -> list[str]:
    """Extract candidate glossary terms from chunk text using simple heuristics."""
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


def _read_json_document(path_text: str) -> dict | list | None:
    """Read a JSON document from disk, returning None when unavailable."""
    path = Path(str(path_text or "")).expanduser()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_optional_text(path_text: str) -> str:
    """Read a text file when available, otherwise return an empty string."""
    path = Path(str(path_text or "")).expanduser()
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _is_simple_ascii_term(term: str) -> bool:
    """Return whether a term can safely use ASCII token-boundary matching."""
    token = str(term or "").strip()
    return bool(token) and all(char.isascii() and (char.isalnum() or char in "_-") for char in token)


def _build_term_search_pattern(term: str, *, ignore_case: bool = False):
    """Build a boundary-aware regex for glossary term matching."""
    token = str(term or "").strip()
    if not token:
        return None

    flags = re.IGNORECASE if ignore_case and any(char.isascii() and char.isalpha() for char in token) else 0
    escaped = re.escape(token)
    if _is_simple_ascii_term(token):
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", flags)
    return re.compile(escaped, flags)


def _text_contains_term(text: str, term: str, *, ignore_case: bool = False) -> bool:
    """Return whether text contains a glossary term without substring false positives."""
    pattern = _build_term_search_pattern(term, ignore_case=ignore_case)
    if pattern is None:
        return False
    return bool(pattern.search(str(text or "")))


def _load_manifest_payload(work_dir: str) -> dict:
    """Load manifest.json from a work directory."""
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    manifest_path = work_path / "manifest.json"
    payload = _read_json_document(str(manifest_path))
    return payload if isinstance(payload, dict) else {}


def _resolve_normalized_document_path(work_dir: str, manifest_payload: dict,
                                      normalized_document_path: str = "") -> str:
    """Resolve the normalized document path from explicit or manifest-owned fields."""
    explicit = str(normalized_document_path or "").strip()
    if explicit:
        return str(Path(explicit).expanduser().resolve())

    manifest_payload = manifest_payload if isinstance(manifest_payload, dict) else {}
    candidate = str(manifest_payload.get("normalized_document_path", "")).strip()
    if candidate:
        return str(Path(candidate).expanduser().resolve())

    plan = manifest_payload.get("plan", {}) if isinstance(manifest_payload.get("plan", {}), dict) else {}
    chunk_contract = plan.get("chunk_contract", {}) if isinstance(plan.get("chunk_contract", {}), dict) else {}
    candidate = str(chunk_contract.get("normalized_document_path", "")).strip()
    if candidate:
        return str(Path(candidate).expanduser().resolve())
    return ""


def _load_normalized_document_context(normalized_document_path: str) -> list[tuple[str, str]]:
    """Extract source-level transcript context from normalized_document.json."""
    payload = _read_json_document(normalized_document_path)
    if not isinstance(payload, dict):
        return []

    source = payload.get("source", {}) if isinstance(payload.get("source", {}), dict) else {}
    contexts = []
    title = str(source.get("title", "")).strip()
    channel = str(source.get("channel", "")).strip()
    if title:
        contexts.append(("source_title", title))
    if channel:
        contexts.append(("source_channel", channel))
    return contexts


def _load_metadata_context(metadata_json_path: str) -> list[tuple[str, str]]:
    """Extract title/channel/description context from a metadata JSON file."""
    payload = _read_json_document(metadata_json_path)
    if not isinstance(payload, dict):
        return []

    contexts = []
    title = str(payload.get("title", "")).strip()
    channel = str(payload.get("channel", "")).strip()
    description = str(payload.get("description", "")).strip()
    if title:
        contexts.append(("source_title", title))
    if channel:
        contexts.append(("source_channel", channel))
    if description:
        contexts.append(("metadata_description", description))
    return contexts


def _load_chapter_titles(chapters_path: str) -> list[str]:
    """Load titles from chapter JSON or chapter_plan JSON."""
    payload = _read_json_document(chapters_path)
    if isinstance(payload, dict):
        chapters = payload.get("chapters", [])
    elif isinstance(payload, list):
        chapters = payload
    else:
        chapters = []

    titles = []
    for chapter in chapters if isinstance(chapters, list) else []:
        if not isinstance(chapter, dict):
            continue
        title = str(chapter.get("title", "") or chapter.get("heading", "")).strip()
        if title:
            titles.append(title)
    return titles


def _default_chapters_path_for(work_dir: str) -> str:
    """Return the default chapter-plan path for a work directory when present."""
    candidate = Path(str(work_dir or "")).expanduser().resolve() / "chapter_plan.json"
    return str(candidate) if candidate.exists() else ""


def _iter_transcript_contexts(work_dir: str, *, manifest_payload: dict | None = None,
                              normalized_document_path: str = "", chapters_path: str = "",
                              metadata_json_path: str = "", description_text: str = "",
                              description_path: str = "") -> list[dict]:
    """Collect transcript-oriented glossary context from document metadata."""
    manifest_payload = manifest_payload if isinstance(manifest_payload, dict) else {}
    contexts = []
    seen = set()

    resolved_normalized_document_path = _resolve_normalized_document_path(
        work_dir,
        manifest_payload,
        normalized_document_path,
    )
    for source_kind, text in _load_normalized_document_context(resolved_normalized_document_path):
        key = (source_kind, text)
        if key in seen:
            continue
        seen.add(key)
        contexts.append({
            "source_kind": source_kind,
            "text": text,
            "score_weight": TRANSCRIPT_SOURCE_WEIGHTS[source_kind],
        })

    for source_kind, text in _load_metadata_context(metadata_json_path):
        key = (source_kind, text)
        if key in seen:
            continue
        seen.add(key)
        contexts.append({
            "source_kind": source_kind,
            "text": text,
            "score_weight": TRANSCRIPT_SOURCE_WEIGHTS[source_kind],
        })

    resolved_description = str(description_text or "").strip() or _read_optional_text(description_path).strip()
    if resolved_description:
        key = ("metadata_description", resolved_description)
        if key not in seen:
            seen.add(key)
            contexts.append({
                "source_kind": "metadata_description",
                "text": resolved_description,
                "score_weight": TRANSCRIPT_SOURCE_WEIGHTS["metadata_description"],
            })

    resolved_chapters_path = str(chapters_path or "").strip() or _default_chapters_path_for(work_dir)
    for title in _load_chapter_titles(resolved_chapters_path):
        key = ("chapter_titles", title)
        if key in seen:
            continue
        seen.add(key)
        contexts.append({
            "source_kind": "chapter_titles",
            "text": title,
            "score_weight": TRANSCRIPT_SOURCE_WEIGHTS["chapter_titles"],
        })
    return contexts


def _record_term(term_stats: dict, term: str, *, source_kind: str,
                 chunk_id: int | None = None, score_weight: int = 1,
                 count_weight: int = 1) -> None:
    """Update one term accumulator."""
    if not term:
        return
    stats = term_stats.setdefault(term, {
        "count": 0,
        "score": 0,
        "chunk_ids": set(),
        "source_kinds": set(),
    })
    stats["count"] += max(1, int(count_weight))
    stats["score"] += max(1, int(score_weight))
    stats["source_kinds"].add(str(source_kind or "").strip() or "raw_chunks")
    if chunk_id is not None:
        stats["chunk_ids"].add(int(chunk_id))


def _iter_manifest_source_chunks(work_dir: str) -> list[tuple[int, str]]:
    """Iter manifest source chunks."""
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    manifest = _load_manifest_payload(str(work_path))
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
                   min_occurrences: int = 1, mode: str = "auto",
                   normalized_document_path: str = "", chapters_path: str = "",
                   metadata_json_path: str = "", description_text: str = "",
                   description_path: str = "") -> dict:
    """Build and persist a document-level glossary from source chunks."""
    work_path = Path(str(work_dir or "")).expanduser().resolve()
    if not work_path.exists():
        glossary_path = glossary_path_for(str(work_path))
        return {
            "success": False,
            "work_dir": str(work_path),
            "glossary_path": str(glossary_path),
            "error": f"Work directory not found: {work_path}",
        }

    requested_mode = str(mode or "auto").strip().lower()
    if requested_mode not in {"auto", "manifest", "transcript"}:
        glossary_path = glossary_path_for(str(work_path))
        return {
            "success": False,
            "work_dir": str(work_path),
            "glossary_path": str(glossary_path),
            "error": f"Unsupported glossary mode: {mode}",
        }

    manifest_payload = _load_manifest_payload(str(work_path))
    chunk_payloads = _iter_manifest_source_chunks(str(work_path))
    term_stats = {}
    for chunk_id, text in chunk_payloads:
        for term in _extract_candidate_terms(text):
            _record_term(
                term_stats,
                term,
                source_kind="raw_chunks",
                chunk_id=chunk_id,
                score_weight=TRANSCRIPT_SOURCE_WEIGHTS["raw_chunks"],
            )

    transcript_contexts = []
    if requested_mode != "manifest":
        transcript_contexts = _iter_transcript_contexts(
            str(work_path),
            manifest_payload=manifest_payload,
            normalized_document_path=normalized_document_path,
            chapters_path=chapters_path,
            metadata_json_path=metadata_json_path,
            description_text=description_text,
            description_path=description_path,
        )
        for context in transcript_contexts:
            extracted_terms = sorted(set(_extract_candidate_terms(context.get("text", ""))))
            for term in extracted_terms:
                _record_term(
                    term_stats,
                    term,
                    source_kind=context.get("source_kind", "raw_chunks"),
                    score_weight=context.get("score_weight", 1),
                )

    filtered_terms = [
        term for term, stats in term_stats.items()
        if (
            stats["count"] >= max(1, int(min_occurrences))
            or any(kind in HIGH_PRIORITY_SOURCE_KINDS for kind in stats["source_kinds"])
        )
    ]
    filtered_terms.sort(
        key=lambda term: (
            -int(term_stats[term].get("score", term_stats[term].get("count", 0)) or 0),
            -int(term_stats[term].get("count", 0) or 0),
            term.lower(),
            term,
        )
    )
    limited_terms = filtered_terms[:max(0, int(max_terms))]

    glossary_terms = []
    for term in limited_terms:
        stats = term_stats[term]
        source_kinds = sorted(stats.get("source_kinds", set()))
        glossary_terms.append({
            "term": term,
            "count": stats["count"],
            "score": stats["score"],
            "preserve_exact": True,
            "preferred_output": "",
            "priority": "high" if any(kind in HIGH_PRIORITY_SOURCE_KINDS for kind in source_kinds) else "normal",
            "source_kinds": source_kinds,
            "chunk_ids": sorted(stats.get("chunk_ids", set()))[:MAX_SOURCE_CHUNKS_PER_TERM],
        })

    glossary_path = glossary_path_for(str(work_path))
    resolved_mode = "transcript" if transcript_contexts and requested_mode != "manifest" else "manifest"
    resolved_normalized_document_path = _resolve_normalized_document_path(
        str(work_path),
        manifest_payload,
        normalized_document_path,
    )
    resolved_chapters_path = str(chapters_path or "").strip() or _default_chapters_path_for(str(work_path))
    payload = {
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "format": GLOSSARY_FORMAT,
        "work_dir": str(work_path),
        "glossary_path": str(glossary_path),
        "generated_at": _now_iso(),
        "mode": resolved_mode,
        "source": "transcript_context" if resolved_mode == "transcript" else "manifest_raw_chunks",
        "source_details": {
            "raw_chunk_count": len(chunk_payloads),
            "transcript_context_count": len(transcript_contexts),
            "normalized_document_path": resolved_normalized_document_path,
            "chapters_path": resolved_chapters_path,
            "metadata_json_path": str(metadata_json_path or "").strip(),
        },
        "term_count": len(glossary_terms),
        "terms": glossary_terms,
    }
    glossary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True,
        **payload,
    }


def select_glossary_terms(glossary_payload: dict, source_text: str, *, max_terms: int = 8) -> list[dict]:
    """Select the glossary terms most relevant to the current source chunk."""
    source = str(source_text or "")
    terms = glossary_payload.get("terms", []) if isinstance(glossary_payload.get("terms", []), list) else []
    matched = []
    for entry in terms:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term", "")).strip()
        if not term:
            continue
        if _text_contains_term(source, term, ignore_case=True):
            matched.append(entry)
    matched.sort(
        key=lambda entry: (
            -int(entry.get("score", entry.get("count", 0)) or 0),
            -int(entry.get("count", 0) or 0),
            str(entry.get("term", "")).lower(),
        )
    )
    return matched[:max(0, int(max_terms))]


def build_glossary_prompt_context(glossary_payload: dict, source_text: str, *, max_terms: int = 8) -> dict:
    """Build glossary prompt context."""
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
    """Check whether required glossary terms were preserved in the result text."""
    selected_terms = select_glossary_terms(glossary_payload, source_text, max_terms=max_terms)
    if not selected_terms:
        return {
            "warnings": [],
            "retry_reasons": [],
            "selected_terms": [],
            "matched_terms": [],
            "preserved_terms": [],
            "missing_terms": [],
        }

    result = str(result_text or "")
    selected_term_names = []
    preserved_terms = []
    missing_terms = []
    for entry in selected_terms:
        term = str(entry.get("term", "")).strip()
        preferred_output = str(entry.get("preferred_output", "")).strip()
        expected = preferred_output or term
        if term:
            selected_term_names.append(term)
        if expected and not _text_contains_term(result, expected, ignore_case=False):
            missing_terms.append(term)
        elif term:
            preserved_terms.append(term)

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
        "selected_terms": selected_term_names,
        "matched_terms": selected_term_names,
        "preserved_terms": preserved_terms,
        "missing_terms": missing_terms,
    }


def evaluate_glossary_drift(glossary_payload: dict, source_text: str, result_text: str, *,
                            max_terms: int = 12) -> dict:
    """Evaluate glossary drift for final assembled output."""
    evaluation = evaluate_glossary_terms(
        glossary_payload,
        source_text,
        result_text,
        max_terms=max_terms,
    )
    selected_terms = evaluation.get("selected_terms", []) if isinstance(evaluation.get("selected_terms", []), list) else []
    preserved_terms = evaluation.get("preserved_terms", []) if isinstance(evaluation.get("preserved_terms", []), list) else []
    missing_terms = evaluation.get("missing_terms", []) if isinstance(evaluation.get("missing_terms", []), list) else []
    selected_count = len(selected_terms)
    preserved_count = len(preserved_terms)
    preservation_ratio = preserved_count / selected_count if selected_count > 0 else 1.0

    warnings = []
    if missing_terms:
        warnings.append(
            "Glossary drift detected in final output: expected terms missing or changed: "
            + ", ".join(missing_terms)
        )

    return {
        "warnings": warnings,
        "selected_terms": selected_terms,
        "preserved_terms": preserved_terms,
        "missing_terms": missing_terms,
        "glossary_drift_count": len(missing_terms),
        "preservation_ratio": round(preservation_ratio, 3),
    }
