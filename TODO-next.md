# ContentPipe Next Plan

> 创建时间：2026-03-11 04:26 GMT+8
> 状态：执行中
> 目标：把 ContentPipe 从“基本可跑”推进到“blank-agent 驱动 + 文件产物稳定 + 端到端可信”的下一阶段

## 总原则

- 唯一真源目录：`plugins/content-pipeline/`
- 统一走 `contentpipe-blank` 低污染执行 lane
- 每个节点独立 session key
- 节点输出以**文件产物**为准，不以聊天窗口文字为准
- UI 可展示解释文字，但下游消费必须读 YAML / JSON / Markdown 文件

---

## Phase A — Blank Agent 执行平面落地（P0）

### A1. 接入 `contentpipe-blank`
- [ ] 研究 OpenClaw `/v1/chat/completions` 如何显式指定 agent（model 或 header）
- [ ] 把 ContentPipe 的 Gateway 调用统一路由到 `contentpipe-blank`
- [ ] 验证 blank agent 独立 workspace、生效模型、工具权限

### A2. 稳定 session 语义
- [x] 节点执行 / 聊天 / 同步 / prompt helper 的稳定 session key 已补
- [ ] 验证 blank agent 路线下 session key 仍然稳定可复用
- [ ] 记录 session-key 命名规则到文档

---

## Phase B — 文件产物优先，隔离聊天污染（P0）

### B1. Writer / De-AI 产物护栏
- [ ] Writer 输出必须落 `article_draft.md`
- [ ] De-AI 输出必须落 `article_edited.md`
- [ ] 如果输出包含元解释（如“我来根据…/自检清单/改写完成”）→ 视为失败
- [ ] 失败时自动 retry / repair
- [ ] 连续失败时 fallback：保留原正文，不污染 `article_edited`

### B2. 结构化节点护栏
- [ ] Scout → 只认 `topic.yaml`
- [ ] Researcher → 只认 `research.yaml`
- [ ] Director → 只认 `visual_plan.json`
- [ ] schema 校验不过则 retry / repair

### B3. UI / 下游消费统一
- [ ] review 页面优先显示文件产物内容
- [ ] 下游节点一律从文件 / state 结构字段读，不读“说明性回复”
- [ ] 明确区分：展示文字 vs 可消费产物

---

## Phase C — 端到端回归验证（P0）

### C1. 自动跑一条完整 pipeline
- [ ] 新建标准 run（wechat）
- [ ] 跑通：scout → researcher → writer → director → image_gen → formatter → publisher
- [ ] 检查每个关键文件是否存在并内容正确

### C2. 重点断言
- [ ] `topic.yaml` 是 YAML，不含聊天解释
- [ ] `research.yaml` 是 YAML，不含聊天解释
- [ ] `article_draft.md` 是正文，不含“我来根据/以下是/自检清单”
- [ ] `article_edited.md` 是正文，不含“改写完成/结构粉碎/风格拟态”
- [ ] `visual_plan.json` 是合法 JSON
- [ ] `formatted.html` 成功生成

### C3. 回退 / 审核路径验证
- [ ] 验证“丢弃当前节点成果，回到上一个节点”按钮
- [ ] 验证上一个节点聊天历史保留
- [ ] 验证继续下一步时不会重跑上一个节点

---

## Phase D — 文档与可维护性（P1）

### D1. 文档补齐
- [ ] README 增加 blank-agent 执行模式说明
- [ ] README 增加 Gateway troubleshooting（404 / 401 / chatCompletions）
- [ ] ARCHITECTURE.md 补“控制平面 vs 生成平面”说明
- [ ] 增加 MODEL_MAP / EXECUTION_MAP 文档

### D2. 开发辅助
- [ ] 新增一个 `scripts/smoke_pipeline.py` / `make smoke` 之类的本地冒烟脚本
- [ ] 把 run 成功判据固化成测试/检查脚本

---

## Phase E — 可选优化（P2）

- [ ] Rich text 聊天窗口 / 图片拖拽体验
- [ ] 更细的发布器能力
- [ ] 更完整的 release checklist
- [ ] `CHANGELOG` / release notes 自动化

---

## 明早优先顺序

1. **先把 blank-agent 指定接到所有 gateway 调用上**
2. **再做 Writer / De-AI 文件产物护栏**
3. **然后跑完整 E2E**
4. **最后补文档**

如果时间不够，只做前 2 步也值回票价。
