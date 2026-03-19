"""Prompt assembly and chunking-context preparation helpers."""

from __future__ import annotations

_LOCAL_NAMES = {
    '_bind_utils_globals',
    '_inject_context_block',
    '_inject_continuity_context',
    '_inject_glossary_context',
    '_inject_semantic_context',
    '_build_chunk_prompt',
    '_prepare_chunking_context'
}


def _bind_utils_globals() -> None:
    """Bind delegated helper names from `yt_transcript_utils` into module globals."""
    import yt_transcript_utils as utils

    for name, value in utils.__dict__.items():
        if name.startswith("__") or name in _LOCAL_NAMES:
            continue
        globals()[name] = value


def _inject_context_block(prompt_template: str, context_text: str) -> str:
    """Inject a reference-only context block before the prompt input section."""
    _bind_utils_globals()
    if not context_text:
        return prompt_template

    input_anchor = "\n## Input Text\n"
    if input_anchor in prompt_template:
        return prompt_template.replace(input_anchor, f"\n{context_text}\n\n## Input Text\n", 1)
    return prompt_template.rstrip() + "\n\n" + context_text + "\n"

def _inject_continuity_context(prompt_template: str, continuity_text: str) -> str:
    """Inject continuity context into a chunk prompt template."""
    _bind_utils_globals()
    return _inject_context_block(prompt_template, continuity_text)

def _inject_glossary_context(prompt_template: str, glossary_text: str) -> str:
    """Inject glossary context into a chunk prompt template."""
    _bind_utils_globals()
    return _inject_context_block(prompt_template, glossary_text)

def _inject_semantic_context(prompt_template: str, semantic_text: str) -> str:
    """Inject semantic-anchor context into a chunk prompt template."""
    _bind_utils_globals()
    return _inject_context_block(prompt_template, semantic_text)

def _build_chunk_prompt(prompt_template: str, chunk_body: str,
                        continuity_text: str = "", glossary_text: str = "",
                        semantic_text: str = "") -> str:
    """Assemble the final chunk prompt with continuity and consistency context."""
    _bind_utils_globals()
    template = _inject_semantic_context(prompt_template, semantic_text)
    template = _inject_glossary_context(template, glossary_text)
    template = _inject_continuity_context(template, continuity_text)
    if "{RAW_TEXT}" in template:
        return template.replace("{RAW_TEXT}", chunk_body)
    if "{STRUCTURED_TEXT}" in template:
        return template.replace("{STRUCTURED_TEXT}", chunk_body)
    return template.rstrip() + "\n\n" + chunk_body

def _prepare_chunking_context(prompt_name: str = "", chunk_size: int = 0,
                              config_path: str = None) -> dict:
    """Prepare prompt and budget context used before chunk generation starts."""
    _bind_utils_globals()
    config = _load_optional_config(config_path)
    prompt_template = ""
    if prompt_name:
        try:
            prompt_path = _resolve_prompt_template_path(prompt_name)
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            print(f"Available prompts: {_available_prompt_names()}", file=sys.stderr)
            sys.exit(1)
        prompt_template = prompt_path.read_text(encoding="utf-8")

    chunk_plan = _build_chunk_plan(prompt_name, chunk_size, config, prompt_template)
    budget = chunk_plan["budget"]
    chunk_mode = chunk_plan["chunk_mode"]
    use_legacy_char_override = chunk_plan["use_legacy_char_override"]
    recommended_chunk_size = chunk_plan["recommended_chunk_size"]
    effective_chunk_size = chunk_plan["effective_chunk_size"]
    hard_cap_size = chunk_plan["hard_cap_size"]
    target_tokens = chunk_plan["target_tokens"]
    hard_cap_tokens = chunk_plan["hard_cap_tokens"]
    autotune_budget = dict(budget)
    autotune_budget["target_tokens"] = target_tokens
    autotune_budget["hard_cap_tokens"] = hard_cap_tokens
    autotune_budget["chunk_mode"] = chunk_mode
    autotune_state = _build_autotune_state(autotune_budget, config)
    autotune_state["enabled"] = autotune_state["enabled"] and chunk_mode == "tokens"
    return {
        "config": config,
        "prompt_template": prompt_template,
        "budget": budget,
        "chunk_mode": chunk_mode,
        "use_legacy_char_override": use_legacy_char_override,
        "recommended_chunk_size": recommended_chunk_size,
        "effective_chunk_size": effective_chunk_size,
        "hard_cap_size": hard_cap_size,
        "target_tokens": target_tokens,
        "hard_cap_tokens": hard_cap_tokens,
        "autotune_state": autotune_state,
    }
