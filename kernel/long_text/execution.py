"""Execution command surfaces for processing, resume, and replan flows."""

from __future__ import annotations

import sys
from pathlib import Path

from ..task_runtime import controller as kernel_controller
from ..task_runtime import lifecycle as runtime_lifecycle
from ..task_runtime import state as kernel_state
from . import window as kernel_window


def _route_long_text_mode(work_dir: str, prompt_name: str, input_key: str,
                          config_path: str = None, dry_run: bool = False) -> str:
    """Pick the executor for this run from `long_text_mode` (window | chunks).

    Window mode covers the stages `kernel_window.window_supported` lists; any
    other stage keeps the fixed-chunk executor. A raw-stage chunk run marks the
    work dir as chunk-owned so merge-content reads the chunk outputs.
    """
    import yt_transcript_utils as utils

    mode = kernel_window.resolve_long_text_mode(utils._load_optional_config(config_path))
    raw_stage = (input_key or "raw_path") == "raw_path"
    chunk_progress = raw_stage and kernel_window.has_chunk_progress(work_dir)
    if mode == "window" and not chunk_progress and kernel_window.window_supported(prompt_name, input_key, work_dir):
        return "window"
    if mode == "window":
        if chunk_progress and prompt_name in kernel_window.WINDOW_PROMPTS[kernel_window.RAW_STAGE]:
            reason = "this work dir already has fixed-chunk progress"
        elif not raw_stage and prompt_name in kernel_window.WINDOW_PROMPTS[kernel_window.PROCESSED_STAGE]:
            reason = "the raw stage did not finish in window mode"
        else:
            reason = f"long_text_mode=window does not cover prompt '{prompt_name}' ({input_key})"
        print(f"ℹ️ {reason}; using fixed chunks.", file=sys.stderr)
    if not dry_run and raw_stage:
        kernel_window.set_manifest_executor(work_dir, "chunks")
    return "chunks"


def runtime_status(work_dir: str) -> dict:
    """Return the current runtime summary for a long-text work directory."""
    return runtime_lifecycle.execute_lifecycle_command(
        "runtime-status",
        lambda: kernel_state.summarize_runtime_status(work_dir),
        context={"work_dir": work_dir},
    )


def cancel_run(work_dir: str, reason: str = "") -> dict:
    """Request cancellation for the active long-text run."""
    return runtime_lifecycle.execute_lifecycle_command(
        "cancel-run",
        lambda: kernel_state.request_runtime_cancel(work_dir, reason=reason),
        context={"work_dir": work_dir, "reason": reason},
    )


def pause_run(work_dir: str, reason: str = "") -> dict:
    """Request pausing the active long-text run."""
    return runtime_lifecycle.execute_lifecycle_command(
        "pause-run",
        lambda: kernel_state.request_runtime_pause(work_dir, reason=reason),
        context={"work_dir": work_dir, "reason": reason},
    )


def resume_run(work_dir: str, reason: str = "", runtime_ownership: dict | None = None) -> dict:
    """Resume a paused long-text run under runtime ownership protection."""
    import yt_transcript_utils as utils

    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    return runtime_lifecycle.execute_lifecycle_command(
        "resume-run",
        lambda: kernel_controller.run_owned_mutation(
            work_dir,
            "resume-run",
            runtime_ownership=runtime_ownership,
            conflict_result_builder=utils._build_resume_run_ownership_conflict_result,
            mutation_fn=lambda ownership: utils._resume_run_impl(work_dir, reason=reason),
        ),
        context={"work_dir": work_dir, "reason": reason},
    )


def prepare_resume(work_dir: str, prompt_name: str = "", config_path: str = None,
                   input_key: str = "raw_path", runtime_ownership: dict | None = None) -> dict:
    """Repair manifest state so an interrupted long-text run can resume safely."""
    import yt_transcript_utils as utils

    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    return runtime_lifecycle.execute_lifecycle_command(
        "prepare-resume",
        lambda: kernel_controller.run_owned_mutation(
            work_dir,
            "prepare-resume",
            runtime_ownership=runtime_ownership,
            conflict_result_builder=lambda ownership: utils._build_prepare_resume_ownership_conflict_result(
                manifest_path, prompt_name, ownership
            ),
            mutation_fn=lambda ownership: utils._prepare_resume_impl(
                work_dir,
                prompt_name=prompt_name,
                config_path=config_path,
                input_key=input_key,
            ),
        ),
        context={"work_dir": work_dir, "prompt_name": prompt_name, "input_key": input_key},
    )


def process_chunks(work_dir: str, prompt_name: str, extra_instruction: str = "",
                   config_path: str = None, dry_run: bool = False,
                   input_key: str = "raw_path", force: bool = False,
                   runtime_ownership: dict | None = None) -> dict:
    """Run the main chunk-processing mutation under runtime ownership protection."""
    import yt_transcript_utils as utils

    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    if _route_long_text_mode(work_dir, prompt_name, input_key, config_path, dry_run) == "window":
        return kernel_window.process_window(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            dry_run=dry_run,
            force=force,
            runtime_ownership=runtime_ownership,
            input_key=input_key,
        )

    return runtime_lifecycle.execute_lifecycle_command(
        "process-chunks",
        lambda: kernel_controller.run_owned_mutation(
            work_dir,
            "process-chunks",
            runtime_ownership=runtime_ownership,
            conflict_result_builder=utils._build_process_ownership_conflict_result,
            mutation_fn=lambda ownership: utils._process_chunks_impl(
                work_dir,
                prompt_name,
                extra_instruction=extra_instruction,
                config_path=config_path,
                dry_run=dry_run,
                input_key=input_key,
                force=force,
            ),
        ),
        context={
            "work_dir": work_dir,
            "prompt_name": prompt_name,
            "input_key": input_key,
            "extra_instruction": extra_instruction,
            "force": force,
        },
    )


def replan_remaining(work_dir: str, prompt_name: str = "", config_path: str = None,
                     chunk_size: int = 0, input_key: str = "raw_path",
                     runtime_ownership: dict | None = None) -> dict:
    """Replan the remaining unprocessed source content under runtime ownership protection."""
    import yt_transcript_utils as utils

    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    return runtime_lifecycle.execute_lifecycle_command(
        "replan-remaining",
        lambda: kernel_controller.run_owned_mutation(
            work_dir,
            "replan-remaining",
            runtime_ownership=runtime_ownership,
            conflict_result_builder=utils._build_replan_ownership_conflict_result,
            mutation_fn=lambda ownership: utils._replan_remaining_impl(
                work_dir,
                prompt_name=prompt_name,
                config_path=config_path,
                chunk_size=chunk_size,
                input_key=input_key,
            ),
        ),
        context={"work_dir": work_dir, "prompt_name": prompt_name, "input_key": input_key},
    )


def process_chunks_with_replans(work_dir: str, prompt_name: str, extra_instruction: str = "",
                                config_path: str = None, input_key: str = "raw_path",
                                force: bool = False, max_replans: int = 3,
                                runtime_ownership: dict | None = None) -> dict:
    """Run chunk processing with bounded automatic replanning support."""
    import yt_transcript_utils as utils

    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    # The window executor has no fixed plan to replan; it just moves its cursor.
    if _route_long_text_mode(work_dir, prompt_name, input_key, config_path) == "window":
        return kernel_window.process_window(
            work_dir,
            prompt_name,
            extra_instruction=extra_instruction,
            config_path=config_path,
            force=force,
            runtime_ownership=runtime_ownership,
            input_key=input_key,
        )

    return runtime_lifecycle.execute_lifecycle_command(
        "process-chunks-with-replans",
        lambda: kernel_controller.run_owned_mutation(
            work_dir,
            "process-chunks-with-replans",
            runtime_ownership=runtime_ownership,
            conflict_result_builder=utils._build_process_with_replans_ownership_conflict_result,
            mutation_fn=lambda ownership: utils._process_chunks_with_replans_impl(
                work_dir,
                prompt_name,
                extra_instruction=extra_instruction,
                config_path=config_path,
                input_key=input_key,
                force=force,
                max_replans=max_replans,
                runtime_ownership=ownership,
            ),
        ),
        context={
            "work_dir": work_dir,
            "prompt_name": prompt_name,
            "input_key": input_key,
            "extra_instruction": extra_instruction,
            "force": force,
            "max_replans": max_replans,
        },
    )
