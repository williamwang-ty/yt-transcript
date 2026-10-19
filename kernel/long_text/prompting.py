"""Prompt assembly helpers."""

from __future__ import annotations

_LOCAL_NAMES = {
    '_bind_utils_globals',
    '_inject_context_block',
    '_inject_glossary_context',
    '_build_chunk_prompt',
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

def _inject_glossary_context(prompt_template: str, glossary_text: str) -> str:
    """Inject glossary context into a chunk prompt template."""
    _bind_utils_globals()
    return _inject_context_block(prompt_template, glossary_text)

def _build_chunk_prompt(prompt_template: str, chunk_body: str,
                        context_text: str = "", glossary_text: str = "") -> str:
    """Assemble the final chunk prompt with the glossary and an optional context block."""
    _bind_utils_globals()
    template = _inject_glossary_context(prompt_template, glossary_text)
    template = _inject_context_block(template, context_text)
    if "{RAW_TEXT}" in template:
        return template.replace("{RAW_TEXT}", chunk_body)
    if "{STRUCTURED_TEXT}" in template:
        return template.replace("{STRUCTURED_TEXT}", chunk_body)
    return template.rstrip() + "\n\n" + chunk_body
