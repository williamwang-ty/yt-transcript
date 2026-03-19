"""Execution command surfaces for processing, resume, and replan flows."""

import sys
from pathlib import Path

from ..task_runtime import controller as kernel_controller
from ..task_runtime import state as kernel_state


def runtime_status(work_dir: str) -> dict:
    """Return the current runtime summary for a long-text work directory."""
    return kernel_state.summarize_runtime_status(work_dir)


def cancel_run(work_dir: str, reason: str = "") -> dict:
    """Request cancellation for the active long-text run."""
    return kernel_state.request_runtime_cancel(work_dir, reason=reason)


def pause_run(work_dir: str, reason: str = "") -> dict:
    """Request pausing the active long-text run."""
    return kernel_state.request_runtime_pause(work_dir, reason=reason)


def resume_run(work_dir: str, reason: str = "", runtime_ownership: dict | None = None) -> dict:
    """Resume a paused long-text run under runtime ownership protection."""
    import yt_transcript_utils as utils

    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    return kernel_controller.run_owned_mutation(
        work_dir,
        "resume-run",
        runtime_ownership=runtime_ownership,
        conflict_result_builder=utils._build_resume_run_ownership_conflict_result,
        mutation_fn=lambda ownership: utils._resume_run_impl(work_dir, reason=reason),
    )


def prepare_resume(work_dir: str, prompt_name: str = "", config_path: str = None,
                   input_key: str = "raw_path", runtime_ownership: dict | None = None) -> dict:
    """Repair manifest state so an interrupted long-text run can resume safely."""
    import yt_transcript_utils as utils

    manifest_path = Path(work_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest.json not found in {work_dir}", file=sys.stderr)
        sys.exit(1)

    return kernel_controller.run_owned_mutation(
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

    return kernel_controller.run_owned_mutation(
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

    return kernel_controller.run_owned_mutation(
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

    return kernel_controller.run_owned_mutation(
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
    )
