# Live regression set

`videos.json` lists real YouTube videos that cover the subtitle cases the
pipeline has to handle. `scripts/run_regression.py` runs
`yt_transcript_utils.py run` on each one and writes a report, so a change can
be compared with the run before it.

| Case | Videos |
|---|---|
| Chinese, manual subs | H_peyIigkkc, wu0zS1AuI_E, Fhx_qNS2P8g (long, >30 min) |
| Chinese speech, only a translated auto track | JkMWjp5AXhM |
| English, manual subs | CIatbV3_Rjg, nmgFG7PUHfo (also has the full auto-translation list) |
| English, auto captions only | ijFSEb2W6_8 |
| Japanese, auto captions only (unsupported language) | tGVSOK3PNUI |
| No subtitles | DQGrPstFRVI |

`expected_case` is the source the pipeline should pick (`zh_manual`,
`en_auto`, `deepgram`, `unsupported`, ...). The report marks a video with ⚠
when the source it actually picked differs. `subtitle-info` ignores YouTube's
auto-translation targets and reports the spoken language; Deepgram runs use
that language, or `auto` detection when the metadata has none.
`tests/fixtures/subtitle_language_metadata.json` keeps a trimmed copy of each
video's yt-dlp metadata so the unit tests cover the same cases offline.

The selected source is not fully deterministic. When YouTube rate-limits a
preferred track, `run` stops with `needs_user` (reason `rate_limited`) and
names the fallback track; the regression runner then accepts that track with
`--mode` (for example nmgFG7PUHfo from manual `en` to the English ASR track),
as the agent-driven flow used to do silently. The Notes column lists blocked
tracks, and `--compare` labels a source change as flaky when a track was
blocked in either run.

A trusted subtitle track is always used, human or ASR: the text stage fixes
punctuation and recognition slips, and only a video without one goes to
Deepgram. With `--stage plan` or `--no-deepgram` Deepgram never runs: a video
without usable subtitles ends with status `skipped`.

## Running

Run from the skill root. The runs use the skill's `config.yaml` unless
`--config-path` names another one (for example a temporary config whose
`llm_agent_cli` picks a cheaper model). A Deepgram key is needed only for the
no-subtitle videos.

```bash
# Free: subtitle selection, subtitle quality and the text plan (`run --stage plan`; no LLM, no Deepgram)
python3 scripts/run_regression.py --stage plan

# Full: `run` to the finished article (paid)
python3 scripts/run_regression.py

# Full, but stop where a video would need Deepgram
python3 scripts/run_regression.py --no-deepgram --config-path /tmp/regression_config.yaml

# One video, compared with an earlier run
python3 scripts/run_regression.py --only H_peyIigkkc --compare /tmp/yt_regression/<earlier>/report.json
```

Each run writes `/tmp/yt_regression/<timestamp>/report.md`, `report.json`, a
work root `work/` with each video's `state.json`, and one folder per video
with the run log (`run.log`), the last result (`result.json`), a copy of
`state.json` and the article (`output/`). The runs use their own work root and
output folder, so a real transcription of the same video is not disturbed
and nothing is written to the configured `output_dir`. The exit code is
non-zero when any video errors or fails the quality check.
