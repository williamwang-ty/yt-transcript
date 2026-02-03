# Quick Cleanup Prompt

**Task**: Minimal cleanup of transcript text (for Quick Mode).

---

## Instructions

You are cleaning up a transcript. Make minimal changes.

> **SAFETY NET**: If the input text has ZERO or nearly zero punctuation (no periods, commas, or question marks), ignore the "minimal changes" rule below. Instead, FULLY punctuate the text based on meaning and pauses.

**Fix only**:
1. Add missing punctuation (periods, commas, question marks)
2. Remove filler words: "uh", "um", "like" (when excessive)
3. Add paragraph breaks at natural pauses (every 3-5 sentences)

**Keep**:
- Original wording and phrasing
- All content (do not summarize)
- Language (do not translate)

**Output**: Cleaned text only. No explanations.

---

## Input Text

{RAW_TEXT}
