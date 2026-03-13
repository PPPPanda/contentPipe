# ContentPipe × OpenClaw 审核集成设计

> v0.2 — 2026-03-13
> 
> 核心决策：**应用层方案** — 审核指引嵌入通知消息本身，无需修改 OpenClaw 配置。

## 1. 目标

让用户**在 Discord/飞书/KOOK 频道内**完成全部审核流程，无需打开网页：

- 收到审核通知 → 直接回复意见 → Agent 桥接转发 → approve/reject
- Web UI 仍可用，操作双向同步
- 零配置：不依赖 OpenClaw systemPrompt、agent binding 或任何外部配置

## 2. 方案选型

### 调研过程

分析了 OpenClaw 源码中 `groupSystemPrompt` 的注入链路：

```
channels.discord.guilds.*.channels.*.systemPrompt
  → buildDiscordGroupSystemPrompt()           (src/discord/monitor/inbound-context.ts)
  → sessionCtx.GroupSystemPrompt              (src/auto-reply/templating.ts:118)
  → extraSystemPromptParts[]                  (src/auto-reply/reply/get-reply-run.ts:270-274)
  → agent run params.extraSystemPrompt        (src/auto-reply/reply/get-reply-run.ts:525)
  → buildEmbeddedSystemPrompt()               (src/agents/pi-embedded-runner/system-prompt.ts)
```

这是 OpenClaw 唯一的频道级 LLM 指令注入点。对用户不可见，只对 LLM 可见。

### 三种可行方案

| 方案 | 原理 | 优点 | 缺点 |
|------|------|------|------|
| **① Agent Binding** | `bindings` 路由频道到专属 agent，agent 有独立 `AGENTS.md` | 完全隔离、独立 session/workspace/skills | 与主 agent **不共享 session 和记忆** |
| **② systemPrompt** | `channels.discord.guilds.*.channels.*.systemPrompt` 注入到 LLM | 原生支持、最简单、共享主 session | 需改 `openclaw.json`、prompt 维护在配置里 |
| **③ 通知内嵌指引** ⭐ | 审核通知消息自带结构化标记 + 操作提示，Agent 自然识别 | 零配置、共享主 session、纯应用层 | 指引文字出现在频道消息中（但设计为用户也可读） |

### 最终决策：方案 ③

**理由**：
- 不需要修改任何 OpenClaw 配置
- 通知消息对人类和 AI 同样可读 — 用户看到操作提示，Agent 看到结构化标记
- 指引随通知传递，不会因为 session 重置或 config 变更而丢失
- 与主 Agent 共享 session，可以利用记忆和上下文

## 3. 通知消息格式

### 3.1 审核等待通知（已实现 — commit `fc8beb0`）

```
⏸️ 🔍 scout 等待审核  [REVIEW]
`run_id: run_20260313_143000` · `node: scout`
> 选题摘要内容...

💬 直接回复审核意见 → contentpipe_chat(run_20260313_143000)
✅ 说「通过/OK」→ contentpipe_approve(run_20260313_143000)
🔗 网页审核: http://localhost:8765/runs/run_xxx/review?node=scout
```

**设计要点**：
- `[REVIEW]` 标记 — Agent 识别入口
- `run_id` + `node` 以 inline code 格式 — 机器可解析
- 操作提示直接写出工具名和参数 — Agent 无需猜测
- 人类用户也能一目了然操作方式

### 3.2 节点完成通知

```
🔍 scout 完成
> AI Agent 2026: 从工具到同事
🔗 审核: http://localhost:8765/runs/run_xxx/review?node=scout
```

### 3.3 Pipeline 完成通知

```
✅ Pipeline 完成: AI Agent 2026: 从工具到同事
📱 预览: http://localhost:8765/runs/run_xxx/preview
```

### 3.4 Pipeline 失败通知

```
❌ Pipeline 失败
```error message```
🔗 http://localhost:8765/runs/run_xxx
```

## 4. Agent 桥接行为

Agent 看到 `[REVIEW]` 标记的通知后，根据后续用户消息的意图选择操作：

### 4.1 意图识别

| 用户意图 | 触发词 | Agent 动作 |
|---------|--------|-----------|
| 审核反馈 | 任何修改意见 | `contentpipe_chat(run_id, message)` |
| 通过 | 通过/OK/可以了/approve/没问题/继续/LGTM | `contentpipe_approve(run_id)` |
| 驳回 | 不行/重做/reject/重来 | `contentpipe_reject(run_id, reason)` |
| 回退 | 回退到 xxx | `contentpipe_rollback(run_id, target_node)` |
| 查看详情 | 看一下/展示/摘要 | `contentpipe_status(run_id)` |
| 无关消息 | — | 正常对话，不触发审核 |

### 4.2 转发回复格式

```
💬 **scout** 回复:
{AI 回复内容}

📝 产物变更:
```diff
- 旧内容
+ 新内容
```
```

### 4.3 通过确认格式

```
✅ **scout** 已通过 → **researcher** 开始执行
```

### 4.4 多轮对话

一个节点可能需要多轮修改。Agent 保持 `run_id` + `node` 上下文不变，直到：
- 该节点被 approve → 上下文清除
- 新的 `[REVIEW]` 通知到来 → 切换到新上下文
- 用户明确说"退出审核" → 上下文清除

### 4.5 多 Run 并发

- 默认关联**最新**的 `[REVIEW]` 通知
- 用户可以指定 run_id：如 "run_xxx 通过"
- 有歧义时主动询问

## 5. 完整交互示例

```
[ContentPipe 通知]
⏸️ 🔍 scout 等待审核  [REVIEW]
`run_id: run_20260313_143000` · `node: scout`
> 标题: AI Agent 2026: 从工具到同事的进化之路

💬 直接回复审核意见 → contentpipe_chat(run_20260313_143000)
✅ 说「通过/OK」→ contentpipe_approve(run_20260313_143000)

────────────────────────────

[用户]
标题太长了，而且"进化之路"太俗

[Agent]  ← 识别为审核反馈，调用 contentpipe_chat
💬 **scout** 回复:
好的，已将标题精简。

📝 变更:
```diff
- 标题: AI Agent 2026: 从工具到同事的进化之路
+ 标题: AI Agent：当同事变成 AI
```

────────────────────────────

[用户]
还行，角度改一下，聚焦 Coding Agent

[Agent]  ← 继续转发
💬 **scout** 回复:
已将角度聚焦到 Coding Agent 领域。

📝 变更:
```diff
- 角度: 技术趋势分析 × 实际落地案例
+ 角度: Coding Agent 工具链深度评测（Cursor/Codex/Claude Code）
```

────────────────────────────

[用户]
可以了

[Agent]  ← 识别为通过
✅ **scout** 已通过 → **researcher** 开始执行

（几分钟后）

[ContentPipe 通知]
⏸️ 📚 researcher 等待审核  [REVIEW]
`run_id: run_20260313_143000` · `node: researcher`
> 研究发现 5 条关键论点，引用 12 个来源

💬 直接回复审核意见 → contentpipe_chat(run_20260313_143000)
✅ 说「通过/OK」→ contentpipe_approve(run_20260313_143000)
```

## 6. 现有资产

### 已有 ✅

| 组件 | 状态 | 说明 |
|------|------|------|
| Pipeline 执行引擎 | ✅ | 6 节点顺序执行，交互节点暂停等待 approve |
| Web UI 审核聊天 | ✅ | `/runs/{run_id}/review?node={node}` |
| Discord 通知 (增强版) | ✅ | `[REVIEW]` 标记 + 操作提示 (`fc8beb0`) |
| OpenClaw AI 工具 (5 个) | ✅ | `contentpipe_create/status/list/approve/chat` |
| Chat API | ✅ | `POST /api/runs/{run_id}/chat` |
| Approve API | ✅ | `POST /api/runs/{run_id}/submit-review` |
| Diff API | ✅ | `GET /api/runs/{run_id}/diff` |
| 审核行为参考文档 | ✅ | `prompts/channel-review-guide.md` |

### 缺失 ❌

| 需求 | 优先级 | 说明 |
|------|--------|------|
| 节点级产物摘要 `_build_node_summary()` | P1 | 通知中包含人类可读摘要 |
| 节点级 REST API 路由 | P2 | `/api/runs/{run_id}/nodes/{node}/chat` 等 |
| contentpipe_reject 工具 | P2 | 驳回/重做操作 |
| contentpipe_rollback 工具 | P2 | 回退到指定节点 |
| 聊天记录 `source` 字段 | P2 | 区分 web/discord/openclaw 来源 |
| SSE 实时推送 | P3 | 替换 Web UI polling |
| Web → 频道反向推送 | P3 | Web 操作同步到频道 |

## 7. OpenClaw AI 工具定义

### 7.1 现有工具（`openclaw.plugin.yaml`）

```yaml
tools:
  - name: contentpipe_create
    description: "创建新的内容生产任务"
    parameters:
      topic: { type: string, required: true }
      platform: { type: string, default: wechat }
      auto_approve: { type: boolean, default: false }

  - name: contentpipe_status
    description: "查看当前/指定 Run 的状态"
    parameters:
      run_id: { type: string }

  - name: contentpipe_list
    description: "列出所有 Run"
    parameters:
      limit: { type: number }

  - name: contentpipe_approve
    description: "审批通过当前节点，继续执行"
    parameters:
      run_id: { type: string, required: true }

  - name: contentpipe_chat
    description: "与当前节点 AI 对话"
    parameters:
      run_id: { type: string, required: true }
      message: { type: string, required: true }
```

### 7.2 待新增工具

```yaml
  - name: contentpipe_reject
    description: "驳回当前节点，带反馈重新执行"
    parameters:
      run_id: { type: string, required: true }
      reason: { type: string, required: true }

  - name: contentpipe_rollback
    description: "回退到指定节点重新执行"
    parameters:
      run_id: { type: string, required: true }
      target_node: { type: string, required: true }
      reason: { type: string }
```

## 8. API 设计（增量）

### 8.1 节点摘要（新增）

```
GET /api/runs/{run_id}/nodes/{node}/summary
```

返回节点产物的人类可读摘要：

| 节点 | 摘要内容 |
|------|---------|
| **scout** | 标题、角度、参考文章数、关键词 |
| **researcher** | 研究发现数量、关键论点、引用来源数 |
| **writer** | 文章标题、字数、段落数、风格 |
| **de_ai_editor** | 修改处数量、主要修改类型 |
| **director** | 配图数量、封面描述、每张图位置和描述 |
| **formatter** | 模板名、HTML 大小、图片数 |

### 8.2 节点级 Chat/Approve/Reject（新增路由）

```
POST /api/runs/{run_id}/nodes/{node}/chat       → 路由到现有 chat 逻辑
POST /api/runs/{run_id}/nodes/{node}/approve     → 路由到 submit_review
POST /api/runs/{run_id}/nodes/{node}/reject      → 带反馈重新执行
POST /api/runs/{run_id}/nodes/{node}/rollback    → 通用回退
```

> 现有 `POST /api/runs/{run_id}/chat` 和 `POST /api/runs/{run_id}/submit-review` 保留兼容。

### 8.3 SSE 事件流（新增）

```
GET /api/runs/{run_id}/events
```

```
event: review_needed
data: {"node":"scout","summary":{...}}

event: chat_message
data: {"node":"writer","role":"assistant","message":"已修改...","source":"discord"}

event: approved
data: {"node":"writer","source":"web","next":"de_ai_editor"}

event: run_complete
data: {"run_id":"run_xxx","title":"..."}
```

## 9. 双向同步

### 频道 → Web UI

```
用户在频道: "标题改短"
  → Agent 调用 contentpipe_chat API
    → chat_scout.json 写入（source: "discord"）
    → blank agent 处理 → 产物更新
    → SSE 广播 → Web UI 实时更新
```

### Web UI → 频道

```
用户在 Web UI 提交审核意见
  → chat_scout.json 写入（source: "web"）
  → ContentPipe 推送消息到频道: "💬 [网页] 用户审核意见: xxx"
  → Agent 看到 [网页] 标记，不重复转发
```

## 10. 边界情况

| 场景 | 行为 |
|------|------|
| 用户聊无关话题 | Agent 正常对话，不触发审核 |
| 多 Run 同时等待 | 按最新通知处理；用户可指定 run_id |
| Web UI 已 approve | 通知频道 "✅ {node} 已在网页端通过" |
| 审核超时 | Agent 定期 `contentpipe_status` 提醒用户 |
| `[网页]` 标记消息 | Agent 不重复转发 |

## 11. 实施计划

### Phase 1: 通知增强 + 摘要 ✅ 部分完成

- [x] 通知包含 `[REVIEW]` 标记 + `run_id`/`node` + 操作提示（`fc8beb0`）
- [x] 审核行为参考文档 `prompts/channel-review-guide.md`（`2135747`）
- [ ] 实现 `_build_node_summary(state, node)` — 每节点人类可读摘要
- [ ] 通知中嵌入摘要内容

### Phase 2: 工具实现 + API 路由

- [ ] contentpipe_reject 工具 + API
- [ ] contentpipe_rollback 工具 + API
- [ ] 节点级 REST API 路由（`/nodes/{node}/chat` 等）
- [ ] 聊天记录加 `source` 字段

### Phase 3: 双向同步

- [ ] SSE 事件流 `/api/runs/{run_id}/events`
- [ ] Web UI 订阅 SSE 替换 polling
- [ ] Web → 频道反向推送

### Phase 4: 自动审核（可选）

- [ ] `auto_review_agent: true` 配置项
- [ ] Agent 根据质量标准自动 approve/revise
- [ ] 人类仅审核最终产物

## 12. 关键约束

1. **state.yaml 是单一真相源** — 所有修改通过 `_save_state()` 落盘
2. **审核操作必须幂等** — 重复 approve 不会重复执行
3. **session generation 隔离** — rollback 后 generation +1
4. **认证** — API 通过 `LOCAL_AUTH_TOKEN` 认证
5. **竞态安全** — 同一节点同一时刻只能有一个 approve/reject 操作
6. **不依赖 OpenClaw 配置** — 纯应用层方案，所有指引随通知消息传递
