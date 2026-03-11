---
name: contentpipe-style-reference
description: Read one or more reference articles and distill their writing style, structure, rhythm, tone, hook pattern, and imitation boundaries for ContentPipe Writer or De-AI nodes. Use when a user pastes a style reference link, asks to模仿某篇文章的风格, wants style DNA extracted from a reference article, or needs concrete style notes without copying the original text.
---

# ContentPipe Style Reference

Use this skill when the task is not mainly about factual extraction, but about understanding **how a reference piece is written**.

## Workflow

1. Identify the reference source:
   - If it is a WeChat article, use `contentpipe-wechat-reader` style of workflow (`web_fetch` on the article URL, concise extraction).
   - If it is a normal URL, use `contentpipe-url-reader` style of workflow.
2. Read only enough text to identify stylistic patterns. Do not dump the full article.
3. Distill the article into reusable writing guidance.
4. Explicitly separate:
   - what to imitate
   - what not to imitate
5. Keep excerpts short. Prefer abstracted style notes over copying original sentences.

## What to extract

Focus on these dimensions:

- hook pattern (scene / question / data / contrast / anecdote)
- sentence rhythm (short-long alternation, punchiness, density)
- paragraph shape (short paragraphs, narrative blocks, list usage)
- tone (cold, warm, sarcastic, restrained, intimate, analytical)
- diction (plainspoken, technical, metaphor-heavy, emotionally charged)
- perspective (first-person, observer, commentator, narrator)
- ending style (sharp ending, open question, emotional drop, no-summary ending)

## Suggested structured form

```yaml
style_reference:
  title: "..."
  url: "..."
  style_summary: "..."
  imitate:
    - "..."
  avoid:
    - "..."
  hook_pattern: "..."
  tone: "..."
  rhythm: "..."
  structure_notes:
    - "..."
```

## Safety

- Do not copy long passages from the reference article.
- Do not present stylistic imitation as permission to plagiarize.
- Keep quoted excerpts short and only when necessary.
- Prefer reusable style instructions over sentence-level copying.
