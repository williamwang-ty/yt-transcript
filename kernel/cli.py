"""
yt-transcript: one YouTube video to one Markdown article.

`run` drives the whole flow (kernel/pipeline.py); its stages call the source,
text and quality layers under kernel/.

Usage:
    python3 yt_transcript_utils.py <command> [args]

Commands:
    run <url>                      Run or continue the whole pipeline for one video (state.json per video)
    status [video_id]              Show the runs under the work root, or one video's state.json
    cleanup <video_id>             Remove a video's work directory (--force for an unfinished run)

Debug commands:
    subtitle-info                  Detect the spoken language and pick subtitle candidates from yt-dlp metadata on stdin
    verify-quality <optimized_text>  Rerun the quality check on any file
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

from kernel import pipeline as kernel_pipeline
from kernel.quality import verify as kernel_verify
from kernel.sources import subtitle_language as kernel_subtitle_language


def main():
    """Main."""
    parser = argparse.ArgumentParser(
        description='yt-transcript utility script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    run_parser = subparsers.add_parser(
        'run',
        help='Run or continue the whole pipeline for one video (one JSON result, fixed exit codes)'
    )
    run_parser.add_argument('url', help='YouTube video URL (or an 11-character video id)')
    run_parser.add_argument('--source', choices=['subtitles', 'deepgram'], default=None,
                            help='Force the text source; remembered for later reruns of this video')
    run_parser.add_argument('--mode', choices=['chinese', 'bilingual'], default=None,
                            help='Force the article mode; remembered for later reruns of this video')
    run_parser.add_argument('--from', dest='from_stage', choices=list(kernel_pipeline.STAGES), default=None,
                            help='Redo this stage and every later one')
    run_parser.add_argument('--stage', choices=[kernel_pipeline.PLAN_STAGE], default=None,
                            help='Stop after the plan stage (routing check; no LLM, no Deepgram)')
    run_parser.add_argument('--max-seconds', type=float, default=None,
                            help='Return in_progress at the next safe point after this many seconds')
    run_parser.add_argument('--restart', action='store_true', help="Discard this video's work directory first")
    run_parser.add_argument('--llm-api', choices=list(kernel_pipeline.LLM_API_ANSWERS), default=None,
                            help='Answer an llm_api_fallback question: use the paid API for this video (once) '
                                 'or from now on (always), or not for this video (no) or ever (never)')
    run_parser.add_argument('--config-path', default=None, help='Path to config.yaml')

    status_parser = subparsers.add_parser(
        'status',
        help='Show the runs under the work root, or one video\'s state.json'
    )
    status_parser.add_argument('video_id', nargs='?', default='', help='Video id (omit to list every run)')

    cleanup_parser = subparsers.add_parser(
        'cleanup',
        help="Remove a video's work directory (an unfinished run needs --force)"
    )
    cleanup_parser.add_argument('video_id', help='Video id')
    cleanup_parser.add_argument('--force', action='store_true', help='Also remove an unfinished run')

    # Debug command; download.sh also pipes yt-dlp metadata through it.
    subtitle_info_parser = subparsers.add_parser(
        'subtitle-info',
        help='Detect the spoken language and pick subtitle candidates from yt-dlp metadata on stdin'
    )
    subtitle_info_parser.add_argument('--video-id', default='', help='Video id to report when metadata lacks one')
    subtitle_info_parser.add_argument('--yt-dlp-runtime-json', default='',
                                      help='yt-dlp runtime JSON to echo back in the result')

    # Debug command: the quality check `run` applies in its verify stage.
    vq_parser = subparsers.add_parser(
        'verify-quality',
        help='Verify quality of optimized text file (structural checks)'
    )
    vq_parser.add_argument('optimized_text_path', help='Path to optimized text file')
    vq_parser.add_argument('--raw-text', default=None, help='Path to raw text file for size comparison')
    vq_parser.add_argument('--bilingual', action='store_true', help='Whether content should be bilingual')
    vq_parser.add_argument('--work-dir', default='', help='Optional work_dir used to load glossary.json for drift checks')
    vq_parser.add_argument('--glossary-path', default='', help='Optional glossary.json path for final drift checks')
    vq_parser.add_argument('--glossary-max-terms', type=int, default=12,
                           help='Maximum glossary terms to verify in final output')

    args = parser.parse_args()

    if args.command == 'run':
        # Progress goes to stderr; stdout carries exactly one JSON result.
        stderr_tail = kernel_pipeline._StderrTail(sys.stderr)
        with contextlib.redirect_stderr(stderr_tail), contextlib.redirect_stdout(stderr_tail):
            result, code = kernel_pipeline.run(
                args.url,
                source=args.source,
                mode=args.mode,
                from_stage=args.from_stage,
                stage=args.stage,
                max_seconds=args.max_seconds,
                restart=args.restart,
                config_path=args.config_path,
                stderr_tail=stderr_tail,
                llm_api=args.llm_api,
            )
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(code)

    elif args.command == 'status':
        result, code = kernel_pipeline.status(args.video_id)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(code)

    elif args.command == 'cleanup':
        result, code = kernel_pipeline.cleanup(args.video_id, force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(code)

    elif args.command == 'subtitle-info':
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as error:
            print(f"Error: subtitle-info expects yt-dlp -J metadata on stdin: {error}", file=sys.stderr)
            sys.exit(1)
        try:
            runtime = json.loads(args.yt_dlp_runtime_json) if args.yt_dlp_runtime_json else {}
        except json.JSONDecodeError:
            runtime = {}
        result = kernel_subtitle_language.build_subtitle_info(payload, video_id=args.video_id, yt_dlp_runtime=runtime)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == 'verify-quality':
        result = kernel_verify.verify_quality(
            args.optimized_text_path,
            raw_text_path=args.raw_text,
            bilingual=args.bilingual,
            work_dir=args.work_dir,
            glossary_path=args.glossary_path,
            glossary_max_terms=args.glossary_max_terms,
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result['passed']:
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)
