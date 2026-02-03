# Structure-Only Optimization Prompt

**Task**: Add paragraph breaks and section headers to raw transcript text.

---

## Instructions

You are a professional transcript editor. The following is raw transcript text that needs structure.

**Do the following**:

1. **Paragraph Breaks**: 
   - Divide text into natural paragraphs (3-8 sentences each)
   - Break at logical pauses and topic shifts

2. **Section Headers**:
   - Identify major topic transitions
   - Insert Markdown level 2 headers: `## Header Name`
   - Aim for 3-5 clear sections (adjust based on content length)

**Do NOT**:
- Translate content
- Fix speech recognition errors
- Remove or summarize content
- Add speaker labels

**Output**: The structured text only. No explanations.

---

## Input Text

{RAW_TEXT}
