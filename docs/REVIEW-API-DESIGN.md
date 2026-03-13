# ContentPipe × OpenClaw 审核集成设计

> v0.1 — 2026-03-13

## 1. 目标

让 OpenClaw 的主 Agent（或任何 Agent）能**像人类在网页审核一样**与 ContentPipe Pipeline 交互：

- 收到通知时看到完整的节点产物摘要（人话，不是 raw YAML）
- 直接在 Discord/飞书/KOOK 聊天中审核，无需打开网页
- 所有操作**实时同步**到 Web UI（反之亦然）

## 2. 现状分析

### 已有

| 组件 | 状态 | 说明 |
|------|------|------|
| Pipeline 执行引擎 | ✅ | 6 节点顺序执行，交互节点暂停等待 approve |
| Web UI 审核聊天 | ✅ | `/runs/{run_id}/review?node={node}` |
| Discord 通知 | ⚠️ 基础 | 只发文本摘要，不含 session 信息 |
| OpenClaw AI 工具 | ⚠️ 壳子 | 5 个工具已注册但实现是 stub |
| Chat API | ✅ | `POST /api/runs/{run_id}/chat` |
| Approve API | ✅ | `POST /api/runs/{run_id}/submit-review` |
| Rollback API | ⚠️ 部分 | 只有 `image_gen → director` 的回退 |

### 缺失

| 需求 | 状态 |
|------|------|
| 通知中携带 session key + 结构化产物摘要 | ❌ |
| 节点级通用 approve/reject/rollback REST API | ❌ |
| 节点产物人类可读摘要生成 | ❌ |
| 操作后 Web UI 实时同步 | ❌ (需 SSE/轮询) |
| OpenClaw tool 实现对接后端 API | ❌ |

## 3. 架构设计

```
┌─────────────────┐          ┌──────────────────────┐
│  OpenClaw Agent  │◄────────►│   ContentPipe API    │
│  (主 Session)    │  HTTP    │   (port 8765)        │
│                  │          │                      │
│  收到通知 ──────►│  调用    │  /api/nodes/{node}/  │
│  阅读摘要       │  ─────►  │    summary           │
│  发送审核意见   │  ─────►  │    chat              │
│  approve/reject │  ─────►  │    approve           │
│                  │          │    reject            │
│                  │          │    rollback          │
└─────────────────┘          └──────┬───────────────┘
                                    │
                              ┌─────▼──────┐
                              │ state.yaml │  ◄── 单一真相源
                              │ chat_*.json│
                              └─────┬──────┘
                                    │ SSE / polling
                              ┌─────▼──────┐
                              │  Web UI    │
                              │  (浏览器)  │
                              └────────────┘
```

### 3.1 通知增强

当节点完成并等待审核时，通知消息包含：

```
⏸️ 🔍 scout 等待审核

📋 选题摘要:
  标题: AI Agent 2026: 从工具到同事
  角度: 技术趋势分析 × 实际落地案例
  参考: 3 篇文章
  关键词: AI Agent, 工作流自动化, LLM

🔗 网页审核: http://localhost:8765/runs/run_xxx/review?node=scout

💬 你可以直接在这里回复审核意见，或使用以下命令:
  ✅ 通过: /contentpipe approve run_xxx
  ❌ 回退: /contentpipe reject run_xxx
  💬 聊天: 直接回复本消息
```

### 3.2 结构化通知 Payload

```json
{
  "event": "review_needed",
  "run_id": "run_20260313_143000",
  "node": "scout",
  "session_key": "contentpipe:run_20260313_143000:scout:main",
  "status": "review",
  "summary": {
    "title": "AI Agent 2026: 从工具到同事",
    "angle": "技术趋势分析 × 实际落地案例",
    "references": 3,
    "key_points": ["AI Agent", "工作流自动化"],
    "word_count": null
  },
  "web_url": "http://localhost:8765/runs/run_xxx/review?node=scout",
  "available_actions": ["approve", "reject", "chat"]
}
```

### 3.3 每个节点的摘要格式

| 节点 | 摘要内容 |
|------|---------|
| **scout** | 标题、角度、参考文章数、关键词 |
| **researcher** | 研究发现数量、关键论点、引用来源数 |
| **writer** | 文章标题、字数、段落数、风格 |
| **de_ai_editor** | 修改处数量、主要修改类型 |
| **director** | 配图数量、封面描述、每张图位置和描述 |
| **formatter** | 模板名、HTML 大小、图片数 |

## 4. API 设计

### 4.1 节点摘要（新增）

```
GET /api/runs/{run_id}/nodes/{node}/summary
```

**Response:**
```json
{
  "run_id": "run_xxx",
  "node": "scout",
  "status": "review",
  "session_key": "contentpipe:run_xxx:scout:main",
  "summary": {
    "title": "...",
    "fields": [
      {"label": "角度", "value": "技术趋势分析"},
      {"label": "参考文章", "value": "3 篇"},
      {"label": "关键词", "value": "AI Agent, LLM"}
    ]
  },
  "artifact_path": "output/runs/run_xxx/topic.yaml",
  "artifact_preview": "# 前 500 字的产物内容..."
}
```

### 4.2 审核聊天（增强现有）

```
POST /api/runs/{run_id}/nodes/{node}/chat
```

**Request:**
```json
{
  "message": "标题太长了，能不能缩短到 15 字以内？",
  "source": "openclaw"
}
```

**Response:**
```json
{
  "ok": true,
  "reply": "好的，已将标题缩短为「AI Agent：从工具到同事」，共 11 字。",
  "artifact_changed": true,
  "artifact_diff": "- title: AI Agent 2026: 从工具到同事的进化之路\n+ title: AI Agent：从工具到同事",
  "session_key": "contentpipe:run_xxx:scout:main"
}
```

> `source: "openclaw"` 标记让 Web UI 知道这条消息来自 Agent 而非人类。

### 4.3 通过审核（增强现有）

```
POST /api/runs/{run_id}/nodes/{node}/approve
```

**Request:**
```json
{
  "source": "openclaw",
  "comment": "选题方向不错，继续"
}
```

**Response:**
```json
{
  "ok": true,
  "next_node": "researcher",
  "message": "scout approved, pipeline continuing to researcher"
}
```

### 4.4 驳回/修改请求（新增）

```
POST /api/runs/{run_id}/nodes/{node}/reject
```

**Request:**
```json
{
  "source": "openclaw",
  "reason": "角度太泛，聚焦到 Coding Agent 这一个垂直领域",
  "action": "revise"
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Feedback recorded, node will re-execute with revisions",
  "session_key": "contentpipe:run_xxx:scout:main:g1"
}
```

> `action` 可选值:
> - `"revise"` — 带反馈重新执行当前节点
> - `"rollback"` — 回退到上一个节点

### 4.5 通用回退（新增）

```
POST /api/runs/{run_id}/nodes/{node}/rollback
```

**Request:**
```json
{
  "target_node": "writer",
  "reason": "配图方案不对，需要从写作阶段重新调整"
}
```

**Response:**
```json
{
  "ok": true,
  "rolled_back_to": "writer",
  "cleared_nodes": ["de_ai_editor", "director"],
  "new_generation": 2,
  "session_key": "contentpipe:run_xxx:writer:main:g2"
}
```

### 4.6 Pipeline 状态 SSE（增强现有）

```
GET /api/runs/{run_id}/events
```

**SSE Events:**
```
event: node_complete
data: {"node": "scout", "duration_ms": 12000}

event: review_needed
data: {"node": "researcher", "summary": {...}}

event: chat_message
data: {"node": "writer", "role": "assistant", "message": "已修改...", "source": "openclaw"}

event: approved
data: {"node": "writer", "source": "web", "next": "de_ai_editor"}

event: run_complete
data: {"run_id": "run_xxx", "title": "..."}
```

> Web UI 和 OpenClaw 都可以订阅 SSE，实现**双向实时同步**。

## 5. OpenClaw AI 工具实现

### 5.1 工具注册（`openclaw.plugin.yaml`）

```yaml
tools:
  - name: contentpipe_create
    description: "创建新的内容生产任务"
    parameters:
      topic: { type: string, required: true, description: "文章主题/话题" }
      style: { type: string, description: "风格: tech-digital|business-finance|news-insight|lifestyle|education" }
      references: { type: array, description: "参考文章 URL 列表" }

  - name: contentpipe_status
    description: "查看 Run 的当前状态和节点摘要"
    parameters:
      run_id: { type: string, description: "Run ID（留空查看最新）" }

  - name: contentpipe_list
    description: "列出所有 Run"
    parameters:
      limit: { type: number, description: "最多返回数量" }
      status: { type: string, description: "过滤状态: running|review|completed|failed" }

  - name: contentpipe_review
    description: "查看当前等待审核节点的产物摘要"
    parameters:
      run_id: { type: string, required: true }

  - name: contentpipe_chat
    description: "与当前审核节点的 AI 对话（修改产物）"
    parameters:
      run_id: { type: string, required: true }
      message: { type: string, required: true, description: "审核意见或修改请求" }

  - name: contentpipe_approve
    description: "通过当前节点审核，继续 Pipeline"
    parameters:
      run_id: { type: string, required: true }
      comment: { type: string, description: "可选的通过备注" }

  - name: contentpipe_reject
    description: "驳回当前节点，要求修改或回退"
    parameters:
      run_id: { type: string, required: true }
      reason: { type: string, required: true, description: "驳回原因" }
      action: { type: string, description: "revise（重做当前节点）或 rollback（回退到上一节点）" }

  - name: contentpipe_rollback
    description: "回退到指定节点重新执行"
    parameters:
      run_id: { type: string, required: true }
      target_node: { type: string, required: true, description: "回退目标节点" }
      reason: { type: string, description: "回退原因" }
```

### 5.2 工具实现路径

每个工具的 handler 调用 ContentPipe REST API：

```
contentpipe_review(run_id)
  → GET http://localhost:8765/api/runs/{run_id}/nodes/{current_node}/summary
  → 返回人类可读的产物摘要给 Agent

contentpipe_chat(run_id, message)
  → POST http://localhost:8765/api/runs/{run_id}/nodes/{current_node}/chat
  → 返回 AI 回复 + 是否有产物变更 + diff

contentpipe_approve(run_id)
  → POST http://localhost:8765/api/runs/{run_id}/nodes/{current_node}/approve
  → 返回下一节点信息

contentpipe_reject(run_id, reason, action)
  → POST http://localhost:8765/api/runs/{run_id}/nodes/{current_node}/reject
  → 返回重做/回退结果
```

## 6. 通知 → 审核 完整交互流程

```
Pipeline:  scout 执行完毕，进入 review 状态
    │
    ▼
ContentPipe:  调用 notify_review_needed()
    │
    ▼
Discord/飞书:  收到富文本通知
    ┌──────────────────────────────────────────┐
    │ ⏸️ 🔍 scout 等待审核                     │
    │                                          │
    │ 📋 选题摘要:                              │
    │   标题: AI Agent 2026: 从工具到同事       │
    │   角度: 技术趋势分析 × 实际落地案例       │
    │   参考: 3 篇文章                          │
    │                                          │
    │ 🔗 网页: http://...                       │
    │                                          │
    │ Session: contentpipe:run_xxx:scout:main   │
    └──────────────────────────────────────────┘
    │
    ▼
OpenClaw Agent:  收到通知，决定审核
    │
    ├─ 1. contentpipe_review(run_id)  ← 获取完整摘要
    │      返回: 标题/角度/参考/关键词 + 产物预览
    │
    ├─ 2. contentpipe_chat(run_id, "标题能更吸引人吗？")
    │      返回: AI 修改后的标题 + diff
    │      Web UI: 同步显示这条聊天记录
    │
    ├─ 3. contentpipe_chat(run_id, "好多了，但关键词加上 LangChain")
    │      返回: AI 更新关键词 + diff
    │      Web UI: 实时更新
    │
    └─ 4. contentpipe_approve(run_id, comment="选题 OK")
           返回: "pipeline continuing to researcher"
           Web UI: 页面自动跳转到 researcher
           Discord: 通知 "✅ scout approved by Agent"
```

## 7. Web UI 同步机制

### 7.1 SSE 事件流

Web UI 订阅 `GET /api/runs/{run_id}/events`，收到以下事件时更新页面：

| 事件 | UI 动作 |
|------|---------|
| `chat_message` | 聊天面板追加消息（标记来源：🤖 Agent / 👤 Web） |
| `artifact_updated` | 左侧产物面板刷新 |
| `approved` | 页面提示"已通过"，跳转到下一节点 |
| `rejected` | 页面提示"已驳回"，显示驳回原因 |
| `rolled_back` | 页面提示"已回退到 {node}" |

### 7.2 消息来源标记

聊天记录增加 `source` 字段：

```json
{
  "role": "user",
  "content": "标题缩短一下",
  "source": "openclaw",
  "timestamp": "2026-03-13T14:30:00Z"
}
```

Web UI 显示:
- `source: "web"` → 👤 用户头像
- `source: "openclaw"` → 🤖 Agent 头像
- `source: "system"` → ⚙️ 系统消息

## 8. 实施计划

### Phase 1: 通知增强 + Summary API（1-2 天）

- [ ] 实现 `_build_node_summary(state, node)` — 每个节点的人类可读摘要生成器
- [ ] 新增 `GET /api/runs/{run_id}/nodes/{node}/summary` 端点
- [ ] 增强 `notify_review_needed()` — 包含结构化摘要 + session key
- [ ] 通知消息格式化为富文本（Discord embed / 飞书卡片）

### Phase 2: 节点级 CRUD API（1-2 天）

- [ ] 新增 `POST /api/runs/{run_id}/nodes/{node}/chat` — 路由到现有 chat 逻辑
- [ ] 新增 `POST /api/runs/{run_id}/nodes/{node}/approve` — 路由到 submit_review
- [ ] 新增 `POST /api/runs/{run_id}/nodes/{node}/reject` — 带 revise/rollback 选项
- [ ] 新增 `POST /api/runs/{run_id}/nodes/{node}/rollback` — 通用回退
- [ ] 聊天记录加 `source` 字段

### Phase 3: SSE 实时同步（1 天）

- [ ] 实现 `GET /api/runs/{run_id}/events` SSE 端点
- [ ] Pipeline 执行、审核、聊天操作均发 SSE 事件
- [ ] Web UI 订阅 SSE，替换当前的 polling 机制
- [ ] 聊天面板区分 Agent vs Web 来源

### Phase 4: OpenClaw 工具实现（1 天）

- [ ] `openclaw.plugin.yaml` 更新工具定义
- [ ] 实现 tool handler（调用 ContentPipe REST API）
- [ ] E2E 测试：Agent 收到通知 → 审核 → approve

### Phase 5: 自动审核模式（可选）

- [ ] 配置项：`auto_review_agent: true` — Agent 自动审核所有节点
- [ ] 审核策略：Agent 根据质量标准自动决定 approve/revise/reject
- [ ] 人类仅收到最终产物的审核请求

## 9. 关键约束

1. **state.yaml 是单一真相源** — 所有 API 修改都通过 `_save_state(state)` 落盘
2. **审核操作必须幂等** — 重复 approve 不会重复执行节点
3. **session generation 隔离** — rollback 后 generation +1，新 session 不受旧上下文污染
4. **认证** — 所有 API 需通过 ContentPipe 的 `LOCAL_AUTH_TOKEN` 认证
5. **竞态安全** — 同一节点同一时刻只能有一个 approve/reject 操作

## 10. 迁移兼容

- 现有 Web UI 审核流程**完全不变**
- 现有 `POST /api/runs/{run_id}/submit-review` 保留，新 API 是更细粒度的替代
- 现有 `POST /api/runs/{run_id}/chat` 保留，新 API 增加 `source` 字段
- 旧通知格式作为 fallback，新通知是增强版

---

## 11. 频道内闭环审核（OpenClaw Agent 桥接模式）

### 11.1 目标

用户**不需要打开网页**，在 Discord/飞书/KOOK 频道内完成全部审核流程：

```
ContentPipe Pipeline
  │
  ▼ 节点完成
ContentPipe notify → Discord #图文生成 频道
  │                    │
  │  ┌─────────────────┤
  │  │ 📋 富文本通知     │
  │  │ + 产物摘要       │
  │  │ + session 元数据  │
  │  └─────────────────┘
  │                    │
  │              用户回复: "标题太长了"
  │                    │
  │              OpenClaw Agent 识别 → 这是审核反馈
  │                    │
  │              Agent 调用 contentpipe_chat(run_id, "标题太长了")
  │                    │
  ◄────────────────────┤ ContentPipe LLM 处理
  │                    │
  │              Agent 转发回复: "已缩短为…" + diff
  │                    │
  │              用户: "好的，通过"
  │                    │
  │              Agent 调用 contentpipe_approve(run_id)
  │                    │
  ▼ 下一节点开始执行
```

### 11.2 OpenClaw Agent 频道指令

在 `#图文生成` 频道中，OpenClaw Agent 需要一段**频道级 system prompt**来引导行为。

#### 方案：频道专属 prompt 文件

创建 `plugins/content-pipeline/prompts/channel-review-guide.md`，在 OpenClaw 的频道配置或 AGENTS.md 中引用。

**核心指令内容：**

```markdown
## ContentPipe 审核频道行为规则

你是 ContentPipe 审核流程的桥接 Agent。当本频道收到 ContentPipe 的审核通知时：

### 识别审核上下文

当你看到包含以下标记的消息时，进入审核桥接模式：
- `⏸️` + `等待审核` 关键词
- `[REVIEW]` 标签
- `run_id:` 和 `node:` 字段

记住当前的 `run_id` 和 `node`，直到该节点审核完成。

### 处理用户回复

当用户在审核通知之后发消息时：

1. **判断意图**：
   - 审核反馈/修改意见 → 调用 `contentpipe_chat`
   - 明确通过（"OK"/"通过"/"可以"/"approve"/"没问题"/"继续"） → 调用 `contentpipe_approve`
   - 驳回/回退（"不行"/"重做"/"回退到xxx"） → 调用 `contentpipe_reject`
   - 查看详情（"看一下"/"展示"/"摘要"） → 调用 `contentpipe_review`
   - 无关消息 → 正常对话，不触发审核操作

2. **转发反馈**：
   调用 `contentpipe_chat(run_id=当前run_id, message=用户的审核意见)`
   将返回的 AI 回复 + 产物变更 diff 发送到频道

3. **转发结果格式**：
   ```
   💬 **{node} 回复**:
   {AI 回复内容}

   📝 产物变更:
   ```diff
   - 旧内容
   + 新内容
   ```
   ```

4. **通过审核**：
   当用户表示满意时：
   - 调用 `contentpipe_approve(run_id=当前run_id)`
   - 发送确认消息: "✅ {node} 已通过，Pipeline 继续执行 → {next_node}"

5. **多轮对话**：
   一个节点可能需要多轮修改，保持 run_id 和 node 上下文不变。
   每轮转发后等待用户下一步指令。

### 通知消息模板

ContentPipe 发送的审核通知格式：
```
⏸️ {emoji} {node} 等待审核  [REVIEW]
run_id: {run_id}
node: {node}
session: {session_key}

📋 {node_type} 摘要:
{摘要内容}

---
💬 在此频道直接回复审核意见
✅ 回复 "通过" 继续 Pipeline
🔗 网页审核: {url}
```
```

### 11.3 通知消息格式设计

ContentPipe 的通知需要包含机器可读的元数据，让 OpenClaw Agent 可以解析：

```
⏸️ 🔍 scout 等待审核  [REVIEW]
run_id: run_20260313_143000
node: scout
session: contentpipe:run_20260313_143000:scout:main

📋 选题摘要:
  标题: AI Agent 2026: 从工具到同事
  角度: 技术趋势分析 × 实际落地案例
  参考文章: 3 篇
  关键词: AI Agent, 工作流自动化, LLM

> 选题来源：用户输入 "写一篇关于 AI Agent 趋势的文章"

---
💬 在此频道直接回复审核意见，我会转达给选题 AI
✅ 回复 "通过" 或 "OK" 继续 Pipeline
🔗 网页审核: http://localhost:8765/runs/run_xxx/review?node=scout
```

### 11.4 完整交互示例

```
[ContentPipe Bot]
⏸️ 🔍 scout 等待审核  [REVIEW]
run_id: run_20260313_143000
node: scout

📋 选题摘要:
  标题: AI Agent 2026: 从工具到同事的进化之路
  角度: 技术趋势分析 × 实际落地案例
  参考: 3 篇文章

💬 直接回复审核意见 | ✅ "通过" 继续

────────────────────────────

[用户]
标题太长了，而且"进化之路"太俗

────────────────────────────

[OpenClaw Agent]  ← 识别为审核反馈，调用 contentpipe_chat

💬 scout 回复:
好的，已将标题精简。

📝 变更:
- 标题: AI Agent 2026: 从工具到同事的进化之路
+ 标题: AI Agent：当同事变成 AI

────────────────────────────

[用户]
还行，角度改一下，聚焦 Coding Agent

────────────────────────────

[OpenClaw Agent]  ← 继续转发

💬 scout 回复:
已将角度聚焦到 Coding Agent 领域。

📝 变更:
- 角度: 技术趋势分析 × 实际落地案例
+ 角度: Coding Agent 工具链深度评测（Cursor/Codex/Claude Code）

────────────────────────────

[用户]
可以了

────────────────────────────

[OpenClaw Agent]  ← 识别为通过，调用 contentpipe_approve

✅ scout 已通过，Pipeline 继续 → researcher
📚 researcher 开始执行...

────────────────────────────

（几分钟后）

[ContentPipe Bot]
⏸️ 📚 researcher 等待审核  [REVIEW]
run_id: run_20260313_143000
node: researcher
...
```

### 11.5 OpenClaw 配置变更

#### 11.5.1 频道 prompt 注入

在 OpenClaw 的 `openclaw.json` 中为 `#图文生成` 频道添加专属 prompt：

```json
{
  "channels": {
    "discord": {
      "guilds": {
        "1465578452923977830": {
          "channels": {
            "1480223789626294466": {
              "allow": true,
              "requireMention": false,
              "systemPrompt": "file://plugins/content-pipeline/prompts/channel-review-guide.md"
            }
          }
        }
      }
    }
  }
}
```

> 如果 OpenClaw 不支持 `systemPrompt` 字段，替代方案：
> 1. 在 `AGENTS.md` 中添加频道判断规则
> 2. 或在 `skills/` 中创建一个自动激活的 skill

#### 11.5.2 工具可用性

确保 `contentpipe_*` 工具在主 Agent 的工具列表中可用。
插件工具通过 `openclaw.plugin.yaml` 自动注册。

### 11.6 状态同步

#### 频道 → Web UI

```
用户在频道发 "标题改短"
  → OpenClaw 调用 contentpipe_chat API
    → API 写入 chat_scout.json（source: "discord"）
    → API 调用 blank agent 处理
    → 产物更新 → state.yaml 更新
    → SSE 事件广播 → Web UI 实时更新聊天和产物面板
```

#### Web UI → 频道

```
用户在 Web UI 发审核意见
  → API 写入 chat_scout.json（source: "web"）
  → SSE 事件广播
  → ContentPipe 主动推送消息到频道:
    "💬 [网页] 用户审核意见: xxx"
  → OpenClaw Agent 看到但不重复转发（识别 source: web）
```

### 11.7 边界情况处理

| 场景 | Agent 行为 |
|------|-----------|
| 用户在审核期间聊无关话题 | Agent 正常回复，不触发审核 |
| 多个 Run 同时等待审核 | Agent 按最新通知的 run_id 处理；用户可指定 run_id |
| 用户在 Web UI 已 approve | Agent 收到 SSE → 通知频道 "✅ {node} 已在网页端通过" |
| 审核超时（Pipeline stall） | Agent 定期检查 contentpipe_status，提醒用户 |
| 用户说"看一下详情" | Agent 调用 contentpipe_review 获取完整产物 |
| 用户说"回退到 writer" | Agent 调用 contentpipe_rollback(target_node="writer") |

### 11.8 实施补充

在 Phase 1-4 基础上追加：

#### Phase 1.5: 通知格式升级

- [ ] `notify_review_needed()` 输出包含 `[REVIEW]` 标记 + 结构化字段
- [ ] 实现 `_build_node_summary()` 为每个节点生成人类可读摘要
- [ ] 通知末尾加操作提示（直接回复/通过/网页链接）

#### Phase 4.5: 频道 prompt + Agent 桥接

- [ ] 创建 `prompts/channel-review-guide.md`
- [ ] 配置 OpenClaw `#图文生成` 频道的 systemPrompt
- [ ] 或写入 `AGENTS.md` 的频道行为规则
- [ ] contentpipe_chat 返回值包含 diff
- [ ] contentpipe_approve 返回下一节点信息
- [ ] Agent 转发时格式化 diff 为 Discord 代码块

#### Phase 6: Web → 频道反向推送

- [ ] 当 Web UI 操作时，ContentPipe 推送消息到频道
- [ ] Agent 识别 `source: "web"` 消息，不重复转发
- [ ] 双向同步完成闭环
