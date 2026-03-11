---
name: contentpipe-web-research
description: Run lightweight web research for ContentPipe using search + fetch. Use when Scout or Researcher needs current web results, when the user asks to搜一下/查一下某个主题, or when a node needs source-backed notes instead of freeform speculation.
---

# ContentPipe Web Research

Use this skill for lightweight, source-backed web research.

## Workflow

1. Start with `web_search` to get relevant result candidates.
2. Pick the best 2-4 results and use `web_fetch` to read them.
3. Distill findings into concise, source-labeled notes.
4. Separate:
   - observed facts
   - interpretations
   - open questions
5. Prefer multiple independent sources over one loud source.

## Suggested output form

```yaml
web_research:
  query: "..."
  findings:
    - point: "..."
      sources:
        - title: "..."
          url: "..."
  open_questions:
    - "..."
```

## Safety

- Do not present search snippets as verified facts without reading sources.
- If evidence is weak or conflicting, say so explicitly.
- Keep research compact; do not paste full articles into the context unless absolutely necessary.
