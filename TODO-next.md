# ContentPipe Next Plan

> 创建时间：2026-03-11 04:26 GMT+8
> 最后更新：2026-03-11 21:04 GMT+8
> 状态：P0 主骨架已基本落地，进入收尾与验证阶段
> 目标：把 ContentPipe 从“基本可跑”推进到“blank-agent 驱动 + 内置 skills + 文件产物稳定 + 端到端可信”的下一阶段

## 总原则

- 唯一真源目录：`plugins/content-pipeline/`
- 统一走 `contentpipe-blank` 低污染执行 lane
- 每个节点独立 session key
- 节点输出以**文件产物**为准，不以聊天窗口文字为准
- 技能统一内置在 `plugins/content-pipeline/skills/`，随版本一起发布
- Python pipeline 保留：编排 / state / validator / 持久化 / 确定性发布步骤

---

## Phase A — Blank Agent 执行平面落地（P0）

### A1. 接入 `contentpipe-blank`
- [x] 研究 OpenClaw `/v1/chat/completions` 如何显式指定 agent（header / model 方案）
- [x] 把 ContentPipe 的 Gateway 调用统一路由到 `contentpipe-blank`
- [x] 验证 blank agent 独立 workspace、生效模型、工具权限
- [x] 统一正式产物路径到 `plugins/content-pipeline/output/runs/<run_id>/`

### A2. 稳定 session 语义
- [x] 节点执行 / 聊天 / 同步 / prompt helper 的稳定 session key 已补
- [x] 验证 blank agent 路线下 session key 仍然稳定可复用
- [x] 记录 session-key 命名规则到文档

---

## Phase B — 文件产物优先，隔离聊天污染（P0）

### B1. Writer / De-AI 产物护栏
- [x] Writer 输出必须落 `article_draft.md`
- [x] De-AI 输出必须落 `article_edited.md`
- [x] 如果输出包含元解释（如“我来根据…/自检清单/改写完成”）→ 视为失败
- [x] 失败时自动 retry / repair（same-session）
- [ ] 补一轮真实聊天侧 smoke，验证“贴风格链接 → 左侧文章改写”闭环

### B2. 结构化节点护栏
- [x] Scout → 只认 `topic.yaml`
- [x] Researcher → 只认 `research.yaml`
- [x] Director → 只认 `visual_plan.json`
- [x] Director Refine → 只认 `image_candidates.json`
- [x] schema / 结构校验不过则 retry / repair

### B3. UI / 下游消费统一
- [x] review 页面优先显示文件 / state 产物内容
- [x] 下游节点一律从文件 / state 结构字段读，不读“说明性回复”
- [x] 明确区分：展示文字 vs 可消费产物
- [x] 聊天消息模型已支持 `attachments[]`
- [x] 所有节点已支持“文字 + 图片上传”
- [ ] 附件用途细分不做（后续将由富文本替换），保持当前简单模型即可

---

## Phase C — 内置 Skill 单源架构（P0）

### C1. 技能单源与安全
- [x] `openclaw.plugin.yaml` 已声明 `skills: ["skills"]`
- [x] 新增 `docs/SKILL-POLICY.md`
- [x] 新增 `docs/BUILTIN-SKILLS.md`
- [x] 明确：ContentPipe skills 统一内置，随版本一起发布，不搞外装双轨制
- [x] `install-agent` 会写入 `skills.load.extraDirs`
- [x] `install-agent` 会写入 blank-agent 的 `skills` allowlist

### C2. 已内置的 skills
- [x] `contentpipe-wechat-reader`
- [x] `contentpipe-url-reader`
- [x] `contentpipe-web-research`
- [x] `contentpipe-social-research`
- [x] `contentpipe-style-reference`
- [x] `contentpipe-wechat-draft-publisher`

### C3. 节点 skill-driven 迁移
- [x] Scout 改为 skill-driven
- [x] Researcher 改为 skill-driven
- [x] 聊天侧 enrichment 改为 skill-driven hints（不再 Python 偷抓内容注入）
- [x] style-reference smoke 已通过
- [ ] Writer / De-AI 的聊天侧 style-link 真正闭环再做一轮实测

---

## Phase D — 端到端回归验证（P0）

### D1. 自动跑一条完整 pipeline
- [x] 新建标准 run（wechat）
- [x] 跑通过完整主链（已有成功 run）
- [x] 检查关键文件存在且内容基本正确

### D2. 重点断言
- [x] `topic.yaml` 是 YAML，不含聊天解释
- [x] `research.yaml` 是 YAML，不含聊天解释
- [x] `article_draft.md` 是正文，不含“我来根据/以下是/自检清单”
- [x] `article_edited.md` 是正文，不含“改写完成/结构粉碎/风格拟态”
- [x] `visual_plan.json` 是合法 JSON
- [x] `formatted.html` 成功生成
- [x] 聊天附件 `attachments[]` 已真实 smoke 跑通

### D3. 回退 / 审核路径验证
- [x] “丢弃当前节点成果，回到上一个节点”按钮已实现并验证主语义
- [ ] 再补一轮 UI 级回归，确认上一个节点聊天历史保留、继续下一步不误重跑

---

## Phase E — 文档与可维护性（P1）

### E1. 文档补齐
- [x] README 增加 blank-agent 执行模式说明
- [x] README 增加 Gateway troubleshooting（404 / 401 / chatCompletions）
- [x] ARCHITECTURE.md 补 blank-agent / skills 单源说明
- [ ] 增加 MODEL_MAP / EXECUTION_MAP 文档（还没单独成文）
- [ ] `CHANGELOG` / release note 补一轮本次 skill-driven/attachments 变更

### E2. 开发辅助
- [ ] 新增 `scripts/smoke_pipeline.py` / `make smoke` 之类的本地冒烟脚本
- [ ] 把 run 成功判据固化成测试/检查脚本
- [ ] 更新本文件后，再同步一份 release / audit 摘要

---

## Phase F — 发布链后续（P1 / P2）

### F1. WeChat draft-first
- [x] draft publisher skill 已内置
- [x] Python 发布链已支持正文图上传 / 封面永久素材 / draft_add
- [ ] 再补一轮真草稿 smoke（含 `thumb_media_id` / `media_id` 断言）

### F2. WeChat free publish
- [ ] `contentpipe-wechat-freepublish`
- [ ] `freepublish_submit`
- [ ] `publish_id`
- [ ] 发布状态轮询 / 回调
- [ ] `published / failed / manual_action_required` 状态建模

---

## Phase G — 可选优化（P2）

- [ ] Rich text 聊天窗口（当前先不展开，附件模型已先顶上）
- [ ] 更细的发布器能力
- [ ] 更完整的 release checklist
- [ ] smoke / regression 仪表板

---

## 现在最值得优先做的 4 件事

1. **聊天侧 style-link 真 smoke**
   - 右侧贴风格参考链接
   - blank-agent 用 `contentpipe-style-reference`
   - 左侧文章真的变化

2. **真 WeChat draft smoke**
   - 验证 `thumb_media_id` / `media_id`
   - 验证封面链、正文图链

3. **补 `scripts/smoke_pipeline.py`**
   - 把现在这些临时 smoke 收敛成固定脚本

4. **补 MODEL_MAP / EXECUTION_MAP 文档**
   - 把节点 → 模型 / 节点 → skills / 节点 → artifact 的映射写清楚
