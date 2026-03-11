---
name: contentpipe-web-research
description: Run lightweight web research for ContentPipe using search + fetch. Use when Scout or Researcher needs current web results, when the user asks to搜一下/查一下某个主题, or when a node needs source-backed notes instead of freeform speculation.
---

# ContentPipe Web Research

Use this skill for lightweight, source-backed web research.

## Workflow

1. Start with `web_search` to get relevant result candidates.
2. Prefer result snippets first; do **not** blindly `web_fetch` every hit.
3. Pick only the best 2-4 readable article pages for `web_fetch`.
4. Avoid homepage / login-wall / feed-root pages when possible (for example generic Zhihu home/feed pages that are likely to fail or provide no article body).
5. If `web_fetch` fails or returns low-value content, fall back to the search snippet instead of looping on the same bad domain.
6. Distill findings into concise, source-labeled notes.
7. Separate:
   - observed facts
   - interpretations
   - open questions
8. Prefer multiple independent sources over one loud source.

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
