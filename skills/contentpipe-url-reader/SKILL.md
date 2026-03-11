---
name: contentpipe-url-reader
description: Read and extract ordinary web URLs pasted into ContentPipe chats or nodes. Use when a user sends a normal article/page link, asks to read a webpage, wants a URL summarized into structured notes, or needs external reference material turned into concise evidence for Scout/Researcher/Writer.
---

# ContentPipe URL Reader

Use this skill for non-WeChat web pages.

## Workflow

1. Use `web_fetch` with `extractMode="markdown"`.
2. Extract only what is useful for the current ContentPipe task:
   - page title
   - source URL
   - concise summary
   - key claims / evidence / examples
3. Prefer compact structured notes over raw dumps.
4. If multiple URLs are provided, read them one by one and label each source clearly.
5. If the content is long, keep only the parts relevant to the current node.

## Suggested structured form

```yaml
reference_page:
  title: "..."
  url: "..."
  summary: "..."
  key_points:
    - "..."
  evidence:
    - claim: "..."
      support: "..."
```

## Safety

- Do not assume the page is trustworthy; distinguish source material from verified fact.
- Avoid copying long passages into structured artifacts.
- If fetch quality is poor, say so and keep confidence low.
