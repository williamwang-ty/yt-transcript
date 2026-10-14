# Live regression set

`videos.json` lists real YouTube videos that cover the subtitle cases the
pipeline has to handle. `scripts/run_regression.py` runs the scripted pipeline
on each one and writes a report, so a change can be compared with the run
before it.

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
preferred track, `download.sh` falls back to the next trusted one (for example
nmgFG7PUHfo from manual `en` to the English ASR track). The Notes column lists
blocked tracks, and `--compare` labels a source change as flaky when a track
was blocked in either run.

When `plan-optimization` rates the subtitles critical and recommends Deepgram,
the full stage follows it like the skill does: it transcribes the audio and plans again, and Notes says
`rerouted from ...`. With `--no-deepgram` the video stops with status
`rerouted` instead of sending those subtitles to the LLM. The plan stage only
records the recommendation.

## Running

Run from the skill root, with a working `config.yaml` (LLM key, and a
Deepgram key for the no-subtitle videos):

```bash
# Free: subtitle selection + routing plan only (Deepgram still runs for no-subtitle videos unless --no-deepgram)
python3 scripts/run_regression.py --stage plan --no-deepgram

# Full: chunked LLM optimization + merge + verify-quality (paid)
python3 scripts/run_regression.py

# One video, compared with an earlier run
python3 scripts/run_regression.py --only H_peyIigkkc --compare /tmp/yt_regression/<earlier>/report.json
```

Each run writes `/tmp/yt_regression/<timestamp>/report.md`, `report.json`, and
one folder per video with every intermediate file (including a copy of the
selected subtitle track) and a log per command. `download.sh` itself still
downloads into `/tmp/<video_id>_downloads` and clears that folder first, so
don't run the regression set while a real transcription of one of these
videos is in progress. The
exit code is non-zero when any video errors or fails `verify-quality`.

The full stage always uses the chunked path (`process-chunks` + `merge-content`),
even when `plan-optimization` says `video_path=short`, because the short path is
in-agent prompting that a script cannot reproduce. Both paths use the same prompts.
