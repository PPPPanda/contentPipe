---
name: contentpipe-social-research
description: Gather social-platform discussion signals for ContentPipe using search queries targeted at X/Twitter, XiaoHongShu, Bilibili, Reddit, or similar communities. Use when Scout/Researcher needs community reactions, creator viewpoints, discussion patterns, examples, or anecdotal evidence instead of only mainstream web results.
---

# ContentPipe Social Research

Use this skill when you need community discussion rather than only traditional web search.

## Workflow

1. Start with `web_search`, but bias queries toward social/community sources.
2. Use query patterns such as:
   - `site:x.com <topic>`
   - `site:twitter.com <topic>`
   - `site:xiaohongshu.com <topic>`
   - `site:bilibili.com <topic>`
   - `site:reddit.com <topic>`
3. Collect a small set of representative discussion links.
4. Prefer search snippets first. Only use `web_fetch` on readable discussion/article pages.
5. Avoid homepage/feed/login pages that usually fail or provide no useful article body.
6. If `web_fetch` fails, keep the link + snippet as weak evidence instead of retrying the same bad page repeatedly.
7. Distill:
   - recurring opinions
   - representative examples
   - disagreements / controversy
   - signal strength vs anecdotal noise

## Suggested structure

```yaml
social_research:
  topic: "..."
  recurring_views:
    - "..."
  examples:
    - platform: "xiaohongshu"
      url: "..."
      point: "..."
  disagreements:
    - "..."
```

## Safety

- Treat social content as anecdotal evidence unless independently verified.
- Do not inflate one or two posts into a broad trend without saying confidence is low.
- Prefer diversity of sources over repeating near-duplicate posts.
