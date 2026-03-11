---
name: contentpipe-wechat-draft-publisher
description: Save finalized ContentPipe articles into WeChat Official Account drafts. Use when Publisher needs to upload正文图片到微信CDN, upload封面永久素材, assemble draft payload, create公众号草稿, or explain the safe draft-first publishing workflow. Do not use for automatic正式发布 unless the task explicitly asks for free publish.
---

# ContentPipe WeChat Draft Publisher

Use this skill for the **draft-first** WeChat publishing workflow.

## Responsibilities

- understand the safe draft-first workflow
- upload body images to WeChat CDN-compatible URLs
- upload cover image as permanent media (`thumb_media_id`)
- create a WeChat draft article
- return/store draft identifiers (`media_id`, `thumb_media_id`)

## Workflow

1. Confirm the article is finalized enough for draft saving.
2. Ensure HTML body exists and images are ready.
3. Upload inline images to WeChat image hosting where required.
4. Upload the chosen cover image as permanent material to get `thumb_media_id`.
5. Create draft via WeChat draft API.
6. Return a structured draft result rather than claiming final publication.

## Suggested structured result

```yaml
publish_result:
  platform: "wechat"
  status: "draft_saved"
  media_id: "..."
  thumb_media_id: "..."
  images_uploaded: 3
  cover_source: "cover.jpg"
```

## Safety

- Default to **draft only**, not final publish.
- Never claim "published" when the result is only a saved draft.
- Do not auto-trigger final publish unless explicitly requested.
- If cover upload or draft creation fails, surface the exact failure instead of pretending success.
- Respect ContentPipe validators and final artifact paths; do not bypass the pipeline’s structured outputs.

## Notes

This skill documents and exposes the publishing workflow to the blank agent, but deterministic WeChat API calls should still be executed by ContentPipe's Python publisher/runtime layer when available.
