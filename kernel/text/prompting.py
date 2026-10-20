"""Prompt templates: locating them under prompts/ and assembling the final request text."""

from __future__ import annotations

import re
from pathlib import Path

from kernel.config import skill_root


def resolve_prompt_template_path(prompt_name: str) -> Path:
    """Resolve prompt template path."""
    prompt = (prompt_name or "").strip()
    if not prompt:
        raise ValueError("Prompt name is required")
    if not re.fullmatch(r"[A-Za-z0-9_]+", prompt):
        raise ValueError(f"Invalid prompt name: {prompt}")

    prompt_path = (skill_root() / "prompts" / f"{prompt}.md").resolve()
    prompts_root = (skill_root() / "prompts").resolve()
    try:
        prompt_path.relative_to(prompts_root)
    except ValueError as exc:
        raise ValueError(f"Invalid prompt name: {prompt}") from exc
    if not prompt_path.exists():
        raise ValueError(f"Prompt template not found: {prompt_path}")
    return prompt_path


def inject_context_block(prompt_template: str, context_text: str) -> str:
    """Inject a reference-only context block before the prompt input section."""
    if not context_text:
        return prompt_template

    input_anchor = "\n## Input Text\n"
    if input_anchor in prompt_template:
        return prompt_template.replace(input_anchor, f"\n{context_text}\n\n## Input Text\n", 1)
    return prompt_template.rstrip() + "\n\n" + context_text + "\n"

def _inject_glossary_context(prompt_template: str, glossary_text: str) -> str:
    """Inject glossary context into a chunk prompt template."""
    return inject_context_block(prompt_template, glossary_text)

def build_chunk_prompt(prompt_template: str, chunk_body: str,
                       context_text: str = "", glossary_text: str = "") -> str:
    """Assemble the final chunk prompt with the glossary and an optional context block."""
    template = _inject_glossary_context(prompt_template, glossary_text)
    template = inject_context_block(template, context_text)
    if "{RAW_TEXT}" in template:
        return template.replace("{RAW_TEXT}", chunk_body)
    if "{STRUCTURED_TEXT}" in template:
        return template.replace("{STRUCTURED_TEXT}", chunk_body)
    return template.rstrip() + "\n\n" + chunk_body
