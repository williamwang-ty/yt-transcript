"""Merge the window run's paragraphs into one Markdown document."""

from __future__ import annotations

from pathlib import Path

from kernel.long_text import window as kernel_window
from kernel.text_cleanup import post_merge as kernel_post_merge_cleanup


def merge_content(work_dir: str, output_file: str) -> dict:
    """Write the latest window stage's paragraphs to `output_file`, after the post-merge cleanup."""
    window_state = kernel_window.load_window_state(work_dir)
    body, sections = kernel_window.merged_window_text(work_dir, window_state)
    cleaned_body, cleanup_diagnostics = kernel_post_merge_cleanup.post_merge_cleanup(body)
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(cleaned_body, encoding="utf-8")

    status = window_state.get("status", "")
    result = {
        "success": status == "done",
        "output_file": str(output_path),
        "total_chars": len(cleaned_body),
        "sections": sections,
        "post_merge_cleanup_applied": cleaned_body != body,
        "cleanup_diagnostics": cleanup_diagnostics,
        "window": {
            "status": status,
            "paragraph_count": len(window_state.get("paragraphs", [])),
            "coverage": kernel_window.coverage_report(window_state),
        },
    }
    if status != "done":
        result["message"] = f"The window run is {status or 'not started'}; rerun to finish it before merging"
    return result
