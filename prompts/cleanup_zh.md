# Chinese Cleanup Prompt

**Task**: Clean up Chinese transcript text into readable Markdown without changing meaning.

---

## Instructions

You are a careful Chinese transcript editor. The following text may come from subtitles or ASR output.

**Do the following**:

1. **Sentence cleanup**:
   - Add or repair Chinese punctuation based on meaning
   - Fix obvious broken sentence boundaries
   - Remove accidental duplicate fragments caused by subtitle overlap or ASR repetition

2. **Chinese readability**:
   - Fix unnatural spacing between Chinese characters
   - Merge obviously broken line fragments back into fluent Chinese sentences
   - Preserve technical English terms when they are part of the original speech

3. **Structure**:
   - Divide the text into paragraphs by meaning, following the Paragraph Rule below
   - Insert Markdown level 2 headers: `## Header Name`
   - Keep the original topic order

4. **Conservative normalization**:
   - Correct only obvious recurring product or proper-name variants when the context is highly clear
   - A Terminology section or a Video Information block below, when present, gives the correct spelling of names; use it to fix misheard names, never to add content
   - If uncertain, keep the original wording instead of guessing

## Paragraph Rule

A paragraph covers one thing: one point, one example or story, one step, or one speaker's turn in a conversation.

- **Always start a new paragraph** when the speaker changes or a new section begins, however short the paragraph is.
- **Start a new paragraph** when the topic or argument turns; when an example or story begins, or ends and the speaker returns to the point; when the time or scene changes.
- **Do not start a new paragraph** for a pause, a filler word or a single transitional sentence; join it to the same speaker's neighbouring paragraph.
- **Length guide**: usually 100-300 Chinese characters. Above about 500 characters, split at a turn of meaning inside the paragraph; keep it whole only if it really is one unbroken point. A paragraph under 100 characters that continues its neighbour's point joins that neighbour. Never break a sentence to meet the length.

**Do NOT**:
- Translate content
- Summarize or expand content
- Invent details not present in the source
- Rewrite the text into a different style
- Remove content just because it sounds informal

**Output**: The cleaned and structured Chinese text only. No explanations.

---

## Input Text

{RAW_TEXT}
