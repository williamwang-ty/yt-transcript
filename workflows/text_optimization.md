# Text Optimization Workflow

This workflow handles AI-powered text optimization, including structure, translation, and cleanup.

---

## Context Sync

**↳ READ State**: `cat /tmp/${VIDEO_ID}_state.md`

Extract and confirm from state file (do not rely on memory):
- `vid` = _______ (copy from state)
- `duration` = _______ (copy from state, in seconds)
- `mode` = _______ (copy from state)
- `src` = _______ (copy from state)
- `work_dir` = _______ (copy from state, if present)
- `output_dir` = _______ (copy from state)

**If state file is missing or corrupt**: STOP. Return to SKILL.md Step 1.

---

## Decision: Short or Long Video?

Calculate: `DURATION < 1800` (30 minutes)?
- **YES** → Follow **Short Video Path** below
- **NO** → Follow **Long Video Path** below

---

## Short Video Path (< 30 minutes)

### Step 1: Read Raw Text

```bash
RAW_TEXT=$(cat /tmp/${VIDEO_ID}_raw_text.txt)
```

### Step 2: Determine Processing Mode

| LANGUAGE | SUBTITLE_SOURCE | Processing Mode |
|----------|-----------------|------------------|
| `zh` | YouTube Subtitles | Structure only (light cleanup) |
| `zh` | Deepgram | Structure + Heavy cleanup (fix spaces, punctuation) |
| `en` | YouTube Subtitles | Structure + Translate |
| `en` | Deepgram | Structure + Translate |

### Step 3: Apply Optimization

**For Chinese + YouTube Subtitles**:
1. Load `prompts/structure_only.md`
2. Replace `{RAW_TEXT}` with actual raw text
3. Send to model, save output to `/tmp/${VIDEO_ID}_optimized.txt`

**For Chinese + Deepgram** (needs extra cleanup):
1. Load `prompts/structure_only.md`
2. Add to prompt: "Also fix: Chinese character spacing, add punctuation based on context, remove repeated phrases"
3. Send to model, save output to `/tmp/${VIDEO_ID}_optimized.txt`

**For Bilingual mode** (English content):
1. Load `prompts/structure_only.md`
2. Replace `{RAW_TEXT}` with actual raw text
3. Send to model, save output to `/tmp/${VIDEO_ID}_structured.txt`
4. Load `prompts/translate_only.md`
5. Replace `{STRUCTURED_TEXT}` with content from step 3
6. Send to model, save output to `/tmp/${VIDEO_ID}_optimized.txt`

**↳ WRITE State** (优化完成后):
更新 `step: 4`, `last_action: optimized (short video)`

---

## Long Video Path (≥ 30 minutes)

### Step 1: Check for YouTube Chapters

```bash
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py get-chapters "$VIDEO_URL" > /tmp/${VIDEO_ID}_chapters.json

HAS_CHAPTERS=$(cat /tmp/${VIDEO_ID}_chapters.json | python3 -c "import sys,json; print(json.load(sys.stdin)['has_chapters'])")
```

### Step 2: Split Text into Chunks

```bash
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py chunk-text \
    /tmp/${VIDEO_ID}_raw_text.txt \
    /tmp/${VIDEO_ID}_chunks \
    --chunk-size 8000
```

**↳ WRITE State**: 更新 `chunk: 0`, `total: N`, `work_dir: /tmp/${VIDEO_ID}_chunks`

### Step 3: Generate Chapter Plan (if no YouTube chapters)

If `HAS_CHAPTERS=false`:

1. For each chunk, generate a 1-2 sentence summary (store as `summary_chunk_XXX.txt`)
2. Aggregate all summaries
3. Ask model to create chapter structure as JSON:
   ```json
   [
     {"title_en": "Introduction", "title_zh": "介绍", "start_chunk": 0},
     {"title_en": "Main Topic", "title_zh": "主题", "start_chunk": 2}
   ]
   ```
4. Save to `/tmp/${VIDEO_ID}_chunks/chapter_plan.json`

If `HAS_CHAPTERS=true`:
1. Convert YouTube chapters to the same format
2. Map chapter start times to chunk indices
3. Save to `/tmp/${VIDEO_ID}_chunks/chapter_plan.json`

### Step 4: Process Each Chunk

**↳ READ State**: `cat /tmp/${VIDEO_ID}_state.md` (每个 chunk 处理前)

For each chunk file in `/tmp/${VIDEO_ID}_chunks/chunk_*.txt`:

**For Chinese-only mode**:
- Use `prompts/structure_only.md` (simplified: no section headers)
- Save to `processed_XXX.md`

**For Bilingual mode**:
- Use `prompts/translate_only.md` (no section headers)
- Save to `processed_XXX.md`

**↳ WRITE State** (每个 chunk 处理后):
更新 `chunk: N+1`, `last_action: wrote processed_N.md`, `next: process chunk_N+1`

### Step 5: Merge with Chapter Headers

> [!IMPORTANT]
> After merge, the final file will be moved to `$OUTPUT_DIR` in SKILL.md Step 6. Do not save directly to output_dir here.

```bash
python3 ~/.claude/skills/yt-transcript/yt_transcript_utils.py merge-content \
    /tmp/${VIDEO_ID}_chunks \
    /tmp/${VIDEO_ID}_optimized.txt \
    --header "---
title: \"$TITLE\"
date: $(date +%Y-%m-%d)
---"
```

### Step 6: Verify Merge

Check:
- [ ] All `processed_*.md` files exist
- [ ] Final file size > raw text size × 1.5 (for bilingual)
- [ ] Chapter headers are present
- [ ] No abrupt content cuts

---

## Checkpoint

Before proceeding to final markdown generation, verify:

- [ ] Optimized text saved to: `/tmp/${VIDEO_ID}_optimized.txt`
- [ ] Text is properly structured with sections
- [ ] Translation complete (if bilingual mode)
- [ ] Quality check passed (no obvious errors)

If any is missing, STOP and review the appropriate path above.

---

## Next Step

Return to main SKILL.md, Step 5: Generate Final Markdown File
