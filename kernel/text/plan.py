"""The text plan: which prompts run, in what order, and whether the text fits one request."""

from __future__ import annotations

import json
from pathlib import Path

from kernel.text.measure import estimate_tokens
from kernel.text.normalize import normalize_text_body

# Largest input (tokens) sent as one non-chunked prompt; longer text is chunked.
# Kept separate from the max_tokens ceiling, which no longer sizes anything.
SINGLE_PASS_TOKEN_LIMITS = {
    "structure_only": 4800,
    "translate_only": 4100,
}


def _estimate_single_pass_input_tokens(normalized_document_path: str, raw_text_path: str,
                                       config: dict | None = None) -> tuple[int, int]:
    """Estimate normalized input size for short-video single-pass routing."""
    config = config or {}
    candidates = []
    normalized_path = Path(str(normalized_document_path or "").strip())
    if normalized_path.exists():
        candidates.append((normalized_path, 'normalized_document'))
    raw_path = Path(str(raw_text_path or "").strip())
    if raw_path.exists():
        candidates.append((raw_path, 'raw_text'))

    for candidate_path, source_kind in candidates:
        try:
            if source_kind == 'normalized_document':
                payload = json.loads(candidate_path.read_text(encoding='utf-8'))
                text = str(payload.get('content', {}).get('text', '') or '')
            else:
                text = candidate_path.read_text(encoding='utf-8')
        except Exception:
            continue
        normalized_text = normalize_text_body(text)
        if normalized_text:
            return estimate_tokens(normalized_text), len(normalized_text)
    return 0, 0


def _single_pass_token_limit(mode: str, source: str, config: dict | None = None) -> int:
    """Return the largest input we should still send through one non-chunked prompt."""
    del config
    structure_limit = SINGLE_PASS_TOKEN_LIMITS['structure_only']
    translate_limit = SINGLE_PASS_TOKEN_LIMITS['translate_only']
    limit = structure_limit
    if str(mode or '').strip() == 'bilingual':
        limit = min(limit, translate_limit)
    if str(source or '').strip() == 'deepgram':
        limit = min(limit, structure_limit - 300)
    return max(3200, limit)


def plan_text_operations(*, video_id: str, duration: int, mode: str, source: str, normalization: dict,
                         raw_text_path: str, segments_path: str, work_dir: str, structured_text: str,
                         optimized_text: str, config: dict | None = None) -> dict:
    """Choose the text operations for a source and estimate their size.

    `normalization` is the result of `build_normalized_document` for that
    source; `source` is youtube or deepgram and `mode` chinese or bilingual.
    """
    planner_config = config or {}
    duration_bucket = "long" if duration >= 1800 else "short"
    raw_text_output = raw_text_path
    estimated_input_tokens, estimated_input_chars = _estimate_single_pass_input_tokens(
        normalization.get("normalized_document_path", ""),
        raw_text_output,
        planner_config,
    )
    single_pass_token_limit = _single_pass_token_limit(mode, source, planner_config)
    oversized_short_input = duration_bucket == "short" and estimated_input_tokens > single_pass_token_limit
    routing_reason = "duration_threshold" if duration_bucket == "long" else (
        "oversized_short_input" if oversized_short_input else "duration_short_single_pass"
    )
    video_path = "long" if duration_bucket == "long" or oversized_short_input else "short"
    plan_warnings = list(normalization.get("warnings", []))
    if oversized_short_input:
        plan_warnings.append(
            f"Short-duration input is oversized for single-pass optimization ({estimated_input_tokens} estimated tokens > {single_pass_token_limit}); using chunked processing."
        )
    if source == "deepgram":
        plan_warnings.append(
            "Deepgram fallback is active; review proper nouns and mixed-language terms carefully in the final output."
        )

    outputs = {
        "raw_text": raw_text_output,
        "structured_text": structured_text,
        "optimized_text": optimized_text,
        "normalized_document": normalization.get("normalized_document_path", ""),
        "work_dir": work_dir,
    }

    chinese_cleanup_prompt = "cleanup_zh"

    if video_path == "short":
        if mode == "bilingual":
            operations = [
                {
                    "kind": "prompt",
                    "prompt": "structure_only",
                    "input": outputs["raw_text"],
                    "output": outputs["structured_text"],
                    "extra_instruction": "",
                },
                {
                    "kind": "prompt",
                    "prompt": "translate_only",
                    "input": outputs["structured_text"],
                    "output": outputs["optimized_text"],
                    "extra_instruction": "",
                },
            ]
        else:
            extra_instruction = ""
            if source == "deepgram":
                extra_instruction = (
                    "Source is Deepgram ASR. Also repair obvious ASR artifacts such as broken Chinese spacing, "
                    "missing punctuation, and repeated fragments when confidence is high."
                )
            operations = [
                {
                    "kind": "prompt",
                    "prompt": chinese_cleanup_prompt,
                    "input": outputs["raw_text"],
                    "output": outputs["optimized_text"],
                    "extra_instruction": extra_instruction,
                }
            ]
    else:
        extra_instruction = ""
        if mode == "chinese" and source == "deepgram":
            extra_instruction = (
                "Source is Deepgram ASR. Also repair obvious ASR artifacts such as broken Chinese spacing, "
                "missing punctuation, and repeated fragments when confidence is high."
            )

        operations = [
            {
                "kind": "chunk",
                "prompt": chinese_cleanup_prompt if mode == "chinese" else "structure_only",
                "input_key": "raw_path",
                "extra_instruction": extra_instruction,
            }
        ]
        if mode == "bilingual":
            operations.append(
                {
                    "kind": "chunk",
                    "prompt": "translate_only",
                    "input_key": "processed_path",
                    "extra_instruction": "",
                }
            )

    return {
        "checks": {
            "duration": duration,
            "duration_bucket": duration_bucket,
            "mode": mode,
            "source": source,
            "video_path": video_path,
            "routing_reason": routing_reason,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_input_chars": estimated_input_chars,
            "single_pass_token_limit": single_pass_token_limit,
        },
        "warnings": plan_warnings,
        "hard_failures": [],
        "video_path": video_path,
        "duration_bucket": duration_bucket,
        "routing_reason": routing_reason,
        "normalization": {
            "materialized": normalization.get("materialized", False),
            "source_adapter": normalization.get("source_adapter", ""),
            "preferred_chunk_source": normalization.get("preferred_chunk_source", ""),
            "segment_count": normalization.get("segment_count", 0),
            "normalized_document_path": normalization.get("normalized_document_path", ""),
        },
        "estimated_input_tokens": estimated_input_tokens,
        "single_pass_token_limit": single_pass_token_limit,
        "operations": operations,
        "outputs": outputs,
    }
