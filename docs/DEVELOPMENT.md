# DEVELOPMENT.md

## 2026-03-14

### In-Process Plugin Migration（Phase 0）
- 新增 `docs/INPROCESS-MIGRATION.md`
- 明确当前 OpenClaw `api.runtime` **没有直接 LLM helper**，因此不能一步到位改成 `api.runtime.llm`
- 采用过渡策略：先建立 in-process plugin scaffold，后续通过 `api.runtime.subagent.run()` 做 bridge，再逐步替换 Python managed-service
- 当前仍保留 Python/FastAPI 主线，避免中断现网能力
