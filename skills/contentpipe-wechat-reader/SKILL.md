---
name: contentpipe-wechat-reader
description: Read and extract WeChat Official Account articles from pasted公众号 links (mp.weixin.qq.com / weixin.qq.com) for ContentPipe nodes and review chats. Use when a user pastes a 公众号文章链接, asks to参考某篇公众号文章, wants style/reference extraction from WeChat, or needs title/author/body/key points from a WeChat article.
---

# ContentPipe WeChat Reader

Use this skill when the input contains a WeChat Official Account article URL.

## Workflow

1. Confirm the URL is a WeChat article link (`mp.weixin.qq.com` or equivalent article page).
2. Use `web_fetch` first with `extractMode="markdown"`.
3. If extraction works, return/use a compact structured result:
   - title
   - author (if visible)
   - url
   - key points
   - short excerpt(s)
4. Keep excerpts short. Prefer distilled notes over dumping the whole article.
5. If the article is being used as ContentPipe input, preserve the article’s role explicitly:
   - factual reference
   - style reference
   - structural reference
6. If the user asks to rewrite or imitate style, describe the style characteristics instead of copying long passages.

## Output guidance

Prefer this structure in reasoning/output when relevant:

```yaml
reference_article:
  title: "..."
  author: "..."
  url: "..."
  use_as: "fact|style|structure"
  key_points:
    - "..."
  excerpts:
    - "short excerpt"
```

## Safety

- Do not treat a pasted WeChat article as verified truth automatically.
- Keep article excerpts short; do not dump full long-form content unless explicitly needed.
- If extraction is poor or blocked, say extraction quality is limited instead of inventing details.
