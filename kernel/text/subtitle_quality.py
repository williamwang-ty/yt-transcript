"""Subtitle quality score and the source-routing signal derived from it.

The normalize stage scores the cleaned subtitle text; the source stage reroutes
to Deepgram when the signal says the subtitles are not good enough.
"""

from __future__ import annotations

from kernel.config import parse_float, parse_int
from kernel.text.measure import count_quality_chars, is_quality_fragment_paragraph


def is_youtube_subtitle_source(source_payload: dict) -> bool:
    """Return whether the normalized document source came from YouTube subtitles."""
    if not isinstance(source_payload, dict):
        return False
    source_type = str(source_payload.get("type", "")).strip().lower()
    subtitle_source = str(source_payload.get("subtitle_source", "")).strip().lower()
    return source_type == "youtube" and "subtitle" in subtitle_source


def _subtitle_quality_penalty(density: float, *, safe: float, scale: float, max_penalty: float) -> float:
    """Convert a cleanup density into a bounded subtitle-quality penalty."""
    bounded_density = max(0.0, parse_float(density, 0.0))
    return round(min(max_penalty, max(0.0, bounded_density - safe) * scale), 4)


def _classify_subtitle_quality_band(score: float) -> str:
    """Return a stable quality band for subtitle-derived source text."""
    bounded_score = max(0.0, min(1.0, parse_float(score, 0.0)))
    if bounded_score >= 0.92:
        return "excellent"
    if bounded_score >= 0.78:
        return "good"
    if bounded_score >= 0.58:
        return "fair"
    if bounded_score >= 0.35:
        return "poor"
    return "critical"


def _default_subtitle_quality_signal() -> dict:
    """Return an empty subtitle-quality payload for non-applicable routes."""
    return {
        "applicable": False,
        "evaluated": False,
        "subtitle_quality_score": None,
        "subtitle_quality_band": "",
        "subtitle_quality_reasons": [],
        "overlap_reduction_count": 0,
        "metrics": {
            "cue_count": 0,
            "line_count": 0,
            "fragment_line_count": 0,
            "fragment_line_ratio": 0.0,
            "avg_line_char_count": 0.0,
            "exact_duplicate_cue_ratio": 0.0,
            "overlap_reduction_ratio": 0.0,
            "spacing_cleanup_ratio": 0.0,
            "markup_cleanup_ratio": 0.0,
            "empty_cue_ratio": 0.0,
        },
    }


def evaluate_subtitle_quality(normalized_text: str, cleanup_diagnostics: dict | None = None, *,
                               source_payload: dict | None = None, workflow_payload: dict | None = None,
                               segment_count: int = 0) -> dict:
    """Build explainable quality signals for subtitle-derived YouTube source text."""
    source_payload = source_payload if isinstance(source_payload, dict) else {}
    workflow_payload = workflow_payload if isinstance(workflow_payload, dict) else {}
    if not is_youtube_subtitle_source(source_payload):
        return _default_subtitle_quality_signal()

    diagnostics = cleanup_diagnostics if isinstance(cleanup_diagnostics, dict) else {}
    mode = str(workflow_payload.get("mode", "")).strip().lower()
    lines = [line.strip() for line in str(normalized_text or "").splitlines() if line.strip()]
    cue_count = max(1, parse_int(diagnostics.get("cue_count"), 0) or int(segment_count) or len(lines) or 1)
    density_base = max(cue_count, 20)

    exact_duplicate_cue_count = max(0, parse_int(diagnostics.get("exact_duplicate_cue_count"), 0))
    overlap_trim_count = max(0, parse_int(diagnostics.get("overlap_trim_count"), 0))
    overlap_collapsed_cue_count = max(0, parse_int(diagnostics.get("overlap_collapsed_cue_count"), 0))
    collapsed_cjk_spacing_count = max(0, parse_int(diagnostics.get("collapsed_cjk_spacing_count"), 0))
    tightened_punctuation_spacing_count = max(0, parse_int(diagnostics.get("tightened_punctuation_spacing_count"), 0))
    tightened_bracket_spacing_count = max(0, parse_int(diagnostics.get("tightened_bracket_spacing_count"), 0))
    markup_tag_count = max(0, parse_int(diagnostics.get("markup_tag_count"), 0))
    nbsp_entity_count = max(0, parse_int(diagnostics.get("nbsp_entity_count"), 0))
    empty_cue_count = max(0, parse_int(diagnostics.get("empty_cue_count"), 0))

    overlap_reduction_count = overlap_trim_count + overlap_collapsed_cue_count
    spacing_cleanup_count = (
        collapsed_cjk_spacing_count
        + tightened_punctuation_spacing_count
        + tightened_bracket_spacing_count
    )
    markup_cleanup_count = markup_tag_count + nbsp_entity_count
    exact_duplicate_cue_ratio = exact_duplicate_cue_count / density_base
    overlap_reduction_ratio = overlap_reduction_count / density_base
    spacing_cleanup_ratio = spacing_cleanup_count / density_base
    markup_cleanup_ratio = markup_cleanup_count / density_base
    empty_cue_ratio = empty_cue_count / density_base

    fragment_line_count = sum(1 for line in lines if is_quality_fragment_paragraph(line))
    fragment_line_ratio = fragment_line_count / len(lines) if lines else 0.0
    avg_line_char_count = (
        sum(count_quality_chars(line) for line in lines) / len(lines)
        if lines else 0.0
    )

    duplicate_penalty = _subtitle_quality_penalty(
        exact_duplicate_cue_ratio,
        safe=0.04,
        scale=3.5,
        max_penalty=0.35,
    )
    overlap_penalty = _subtitle_quality_penalty(
        overlap_reduction_ratio,
        safe=0.06,
        scale=3.0,
        max_penalty=0.30,
    )
    spacing_penalty = _subtitle_quality_penalty(
        spacing_cleanup_ratio,
        safe=0.10,
        scale=2.0,
        max_penalty=0.20,
    )
    markup_penalty = _subtitle_quality_penalty(
        markup_cleanup_ratio,
        safe=0.15,
        scale=1.2,
        max_penalty=0.08,
    )
    empty_penalty = _subtitle_quality_penalty(
        empty_cue_ratio,
        safe=0.05,
        scale=2.0,
        max_penalty=0.10,
    )
    fragmentation_penalty = 0.0
    if mode == "chinese" and len(lines) >= 10:
        fragmentation_penalty = _subtitle_quality_penalty(
            fragment_line_ratio,
            safe=0.55,
            scale=0.6,
            max_penalty=0.12,
        )

    penalties = (
        duplicate_penalty
        + overlap_penalty
        + spacing_penalty
        + markup_penalty
        + empty_penalty
        + fragmentation_penalty
    )
    subtitle_quality_score = round(max(0.0, 1.0 - penalties), 3)
    subtitle_quality_band = _classify_subtitle_quality_band(subtitle_quality_score)
    subtitle_quality_reasons = []
    if duplicate_penalty > 0:
        subtitle_quality_reasons.append("duplicate_cue_density_high")
    if overlap_penalty > 0:
        subtitle_quality_reasons.append("overlap_reduction_density_high")
    if spacing_penalty > 0:
        subtitle_quality_reasons.append("spacing_cleanup_density_high")
    if markup_penalty > 0:
        subtitle_quality_reasons.append("markup_cleanup_density_high")
    if empty_penalty > 0:
        subtitle_quality_reasons.append("empty_cue_density_high")
    if fragmentation_penalty > 0:
        subtitle_quality_reasons.append("subtitle_fragmentation_high")

    return {
        "applicable": True,
        "evaluated": True,
        "subtitle_quality_score": subtitle_quality_score,
        "subtitle_quality_band": subtitle_quality_band,
        "subtitle_quality_reasons": subtitle_quality_reasons,
        "overlap_reduction_count": overlap_reduction_count,
        "metrics": {
            "cue_count": cue_count,
            "line_count": len(lines),
            "fragment_line_count": fragment_line_count,
            "fragment_line_ratio": round(fragment_line_ratio, 3),
            "avg_line_char_count": round(avg_line_char_count, 1),
            "exact_duplicate_cue_ratio": round(exact_duplicate_cue_ratio, 3),
            "overlap_reduction_ratio": round(overlap_reduction_ratio, 3),
            "spacing_cleanup_ratio": round(spacing_cleanup_ratio, 3),
            "markup_cleanup_ratio": round(markup_cleanup_ratio, 3),
            "empty_cue_ratio": round(empty_cue_ratio, 3),
        },
    }


def build_source_routing_signal(source: str, mode: str, subtitle_quality: dict | None = None) -> dict:
    """Return explainable source-routing fields for planning and audits."""
    quality_payload = dict(_default_subtitle_quality_signal())
    if isinstance(subtitle_quality, dict):
        quality_payload.update(subtitle_quality)
    quality_payload["metrics"] = dict(quality_payload.get("metrics", {}))

    score = quality_payload.get("subtitle_quality_score")
    band = str(quality_payload.get("subtitle_quality_band", "")).strip()
    reasons = quality_payload.get("subtitle_quality_reasons", [])
    subtitle_quality_reasons = reasons if isinstance(reasons, list) else []
    overlap_reduction_count = max(0, parse_int(quality_payload.get("overlap_reduction_count"), 0))

    source_route_reason = "source_route_unknown"
    reroute_recommended = False
    reroute_target = ""
    reroute_reasons = []

    normalized_source = str(source or "").strip().lower()
    normalized_mode = str(mode or "").strip().lower()
    if normalized_source == "deepgram":
        source_route_reason = "deepgram_source_selected"
    elif quality_payload.get("applicable"):
        if quality_payload.get("evaluated"):
            if band == "critical" and normalized_mode == "chinese":
                source_route_reason = "subtitle_quality_critical_deepgram_recommended"
                reroute_recommended = True
                reroute_target = "deepgram"
                reroute_reasons = list(subtitle_quality_reasons)
            elif band == "critical":
                source_route_reason = "subtitle_quality_critical_manual_review_only"
            elif band == "poor":
                source_route_reason = "subtitle_quality_poor_review_subtitle_path"
            else:
                source_route_reason = "subtitle_quality_acceptable"
        else:
            source_route_reason = "subtitle_quality_pending"
    elif normalized_source == "youtube":
        source_route_reason = "subtitle_quality_pending"
    elif normalized_source:
        source_route_reason = f"{normalized_source}_source_selected"

    return {
        "source_route_reason": source_route_reason,
        "reroute_recommended": reroute_recommended,
        "reroute_target": reroute_target,
        "reroute_reasons": reroute_reasons,
        "subtitle_quality_score": score,
        "subtitle_quality_band": band,
        "subtitle_quality_reasons": subtitle_quality_reasons,
        "overlap_reduction_count": overlap_reduction_count,
        "subtitle_quality": quality_payload,
    }
