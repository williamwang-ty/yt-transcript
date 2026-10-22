# Structure-Only Optimization Prompt

**Task**: Add paragraph breaks and section headers to raw transcript text.

---

## Instructions

You are a professional transcript editor. The following is raw transcript text that needs structure.

**Do the following**:

1. **Paragraph Breaks**:
   - Divide the text into paragraphs by meaning, following the Paragraph Rule below

2. **Section Headers**:
   - Identify major topic transitions
   - Insert Markdown level 2 headers: `## Header Name`
   - Aim for 3-5 clear sections (adjust based on content length)

## Paragraph Rule

A paragraph covers one thing: one point, one example or story, one step, or one speaker's turn in a conversation.

- **Always start a new paragraph** when the speaker changes or a new section begins, however short the paragraph is.
- **Start a new paragraph** when the topic or argument turns; when an example or story begins, or ends and the speaker returns to the point; when the time or scene changes.
- **Do not start a new paragraph** for a pause, a filler word or a single transitional sentence; join it to the same speaker's neighbouring paragraph.
- **Length guide**: usually 2-6 sentences (about 50-150 words). Above about 250 words, split at a turn of meaning inside the paragraph; keep it whole only if it really is one unbroken point. A paragraph under 50 words that continues its neighbour's point joins that neighbour. Never break a sentence to meet the length.

**Do NOT**:
- Translate content
- Fix speech recognition errors, except names and terms whose spelling a Terminology section below gives
- Remove or summarize content
- Add speaker labels

**Output**: The structured text only. No explanations.

---

## Input Text

{RAW_TEXT}
