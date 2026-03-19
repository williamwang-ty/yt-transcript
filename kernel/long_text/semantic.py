import re


URL_PATTERN = re.compile(r"https?://[^\s)]+")
DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
VERSION_PATTERN = re.compile(r"\bv?\d+(?:\.\d+){1,3}\b", re.IGNORECASE)
PERCENT_PATTERN = re.compile(r"\d+(?:\.\d+)?%")
NUMBER_PATTERN = re.compile(r"\b\d{2,}(?:,\d{3})*(?:\.\d+)?\b")
ANCHOR_WARNING_PREFIX = "⚠️ Semantic anchors"


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for item in items:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def _normalize_source_for_anchor_extraction(source_text: str) -> str:
    source = str(source_text or "")
    for _ in range(3):
        updated = re.sub(r'(https?://[^\s]+)\s+([A-Za-z0-9./?=&_%#-]+)', r'\1\2', source)
        if updated == source:
            break
        source = updated
    return source


def extract_semantic_anchors(source_text: str, *, max_items: int = 8) -> dict:
    source = _normalize_source_for_anchor_extraction(source_text)
    anchors = {
        "urls": _dedupe_keep_order(URL_PATTERN.findall(source)),
        "dates": _dedupe_keep_order(DATE_PATTERN.findall(source)),
        "times": _dedupe_keep_order(TIME_PATTERN.findall(source)),
        "versions": _dedupe_keep_order(VERSION_PATTERN.findall(source)),
        "percentages": _dedupe_keep_order(PERCENT_PATTERN.findall(source)),
        "numbers": _dedupe_keep_order(NUMBER_PATTERN.findall(source)),
    }

    higher_priority = anchors["urls"] + anchors["dates"] + anchors["times"] + anchors["versions"] + anchors["percentages"]
    anchors["numbers"] = [
        number for number in anchors["numbers"]
        if not any(number in value for value in higher_priority)
    ]

    ordered = []
    for key in ("urls", "dates", "times", "versions", "percentages", "numbers"):
        ordered.extend(anchors[key])
    ordered = ordered[:max(0, int(max_items))]

    limited = {key: [] for key in anchors}
    remaining = set(ordered)
    for key in ("urls", "dates", "times", "versions", "percentages", "numbers"):
        limited[key] = [value for value in anchors[key] if value in remaining]
    limited["ordered"] = ordered
    return limited


def build_anchor_prompt_context(source_text: str, *, max_items: int = 8) -> dict:
    anchors = extract_semantic_anchors(source_text, max_items=max_items)
    ordered = anchors["ordered"]
    if not ordered:
        return {
            "text": "",
            "anchors": anchors,
        }

    lines = [
        "## Semantic Anchors",
        "",
        "Preserve these high-signal anchors exactly if they appear in the current chunk.",
        "Do not drop or alter them unless the source chunk itself changes them.",
        "",
    ]
    for anchor in ordered:
        lines.append(f"- {anchor}")
    return {
        "text": "\n".join(lines).strip(),
        "anchors": anchors,
    }


def _normalized_numeric(anchor: str) -> str:
    return str(anchor or "").replace(",", "")


def evaluate_semantic_anchors(source_text: str, result_text: str, *, max_items: int = 8) -> dict:
    anchors = extract_semantic_anchors(source_text, max_items=max_items)
    ordered = anchors["ordered"]
    if not ordered:
        return {
            "warnings": [],
            "retry_reasons": [],
            "anchors": anchors,
            "missing_anchors": [],
        }

    result = str(result_text or "")
    normalized_result = result.replace(",", "")
    missing = []
    for anchor in ordered:
        if anchor in anchors["numbers"]:
            if _normalized_numeric(anchor) not in normalized_result:
                missing.append(anchor)
            continue
        if anchor not in result:
            missing.append(anchor)

    warnings = []
    retry_reasons = []
    if missing:
        warnings.append(f"{ANCHOR_WARNING_PREFIX}: missing anchors in output: " + ", ".join(missing))
        retry_reasons.append("missing_semantic_anchors")

    return {
        "warnings": warnings,
        "retry_reasons": retry_reasons,
        "anchors": anchors,
        "missing_anchors": missing,
    }
