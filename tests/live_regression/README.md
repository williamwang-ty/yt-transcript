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
one folder per video with every intermediate file and a log per command. The
exit code is non-zero when any video errors or fails `verify-quality`.

The full stage always uses the chunked path (`process-chunks` + `merge-content`),
even when `plan-optimization` says `video_path=short`, because the short path is
in-agent prompting that a script cannot reproduce. Both paths use the same prompts.
