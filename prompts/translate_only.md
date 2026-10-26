# Translation-Only Prompt

**Task**: Translate English content to bilingual Chinese-English format.

> `bilingual` here means: keep each English paragraph as the source text, then place the Chinese translation below it. It does not mean merging two subtitle files.

---

## Instructions

You are a professional translator. The following is English text that needs Chinese translation.

**Format**:
```
[English Paragraph]

[Chinese Translation]
```

A section header gets its Chinese translation on the next line, as a header of the same level:
```
## [English Header]
## [Chinese Header]
```

**Translation Guidelines**:
1. Keep English paragraph unchanged
2. Add fluent Chinese translation below each paragraph
3. Keep technical terms in English (e.g., API, LLM)
4. Maintain the same paragraph structure
5. Keep each section header (## ...) in English as it is, and add its Chinese translation as the header line right below it

**Do NOT**:
- Add new section headers
- Change paragraph breaks
- Summarize or expand content
- Fix grammar in English text

**Output**: Bilingual text only. No explanations.

---

## Input Text

{STRUCTURED_TEXT}
