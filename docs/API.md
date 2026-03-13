# ContentPipe API Reference

> Version: 0.8.1 | Base URL: `http://localhost:8765/api`
>
> 所有 `POST`/`PUT`/`PATCH`/`DELETE` 请求使用 `Content-Type: application/json`（除非特别标注 multipart）。
> 如果启用了认证（`CONTENTPIPE_AUTH_TOKEN`），需在 Header 添加 `x-contentpipe-token: <token>` 或 Cookie `contentpipe_auth`。

---

## 目录

- [1. 系统](#1-系统)
- [2. 配置管理](#2-配置管理)
- [3. Run 管理](#3-run-管理)
- [4. 节点审核](#4-节点审核)
- [5. 产物管理](#5-产物管理)
- [6. 图片管理](#6-图片管理)
- [7. 设置与向导](#7-设置与向导)
- [8. SSE 实时推送](#8-sse-实时推送)
- [9. OpenClaw AI 工具](#9-openclaw-ai-工具)

---

## 1. 系统

### `GET /api/health`

健康检查。

**Response:**
```json
{
  "status": "healthy",
  "plugin": "content-pipeline",
  "version": "0.8.1",
  "total_runs": 14,
  "active_runs": 3
}
```

### `GET /api/info`

插件信息。

**Response:**
```json
{
  "name": "ContentPipe",
  "version": "0.8.1",
  "description": "AI 驱动的图文内容生成流水线"
}
```

### `GET /api/system/status`

系统全景状态（Gateway 连接、Run 统计、通知配置）。

**Response:**
```json
{
  "gateway": {
    "url": "http://localhost:18789",
    "connected": true,
    "latency_ms": 57
  },
  "llm_mode": "gateway",
  "default_model": "dashscope/qwen3.5-plus",
  "runs": {
    "total": 14,
    "active": 3,
    "completed": 8
  },
  "notifications": {
    "discord_configured": true,
    "notify_channel": "1480223789626294466"
  },
  "auth_enabled": false,
  "version": "0.8.1"
}
```

### `GET /api/system/engines`

列出图片引擎及其可用状态。

**Response:**
```json
{
  "current": "auto",
  "engines": [
    {"name": "pollinations", "mode": "api", "available": true},
    {"name": "dall-e-3", "mode": "api", "available": false},
    {"name": "dashscope", "mode": "api", "available": true},
    {"name": "browser:jimeng", "mode": "browser", "available": null}
  ]
}
```

### `POST /api/system/test-llm`

测试 LLM 调用。

**Request:**
```json
{
  "model": "dashscope/qwen3.5-plus",
  "prompt": "Reply OK"
}
```

**Response:**
```json
{
  "ok": true,
  "model": "dashscope/qwen3.5-plus",
  "reply": "OK",
  "latency_ms": 1234
}
```

### `POST /api/system/test-notify`

发送测试通知到 Discord。

**Response:**
```json
{"ok": true, "channel": "1480223789626294466"}
```

### `GET /api/system/logs`

获取最近日志。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 50 | 返回行数 |
| `level` | string | "" | 按级别过滤（INFO/WARNING/ERROR） |

**Response:**
```json
{
  "logs": ["2026-03-13 22:42:58 | INFO | ..."],
  "count": 50,
  "total": 200
}
```

### `POST /api/restart`

重启 ContentPipe 服务。

**Response:**
```json
{"ok": true, "message": "Restarting..."}
```

---

## 2. 配置管理

### `GET /api/config`

读取当前完整配置。

**Response:**
```json
{
  "gateway_url": "http://localhost:18789",
  "llm_mode": "gateway",
  "default_llm": "dashscope/qwen3.5-plus",
  "gateway_agent_id": "contentpipe-blank",
  "llm_overrides": {
    "scout": "anthropic-sonnet/claude-sonnet-4-6",
    "writer": "openai-codex/gpt-5.4"
  },
  "image_engine": "auto",
  "notify_channel": "1480223789626294466",
  "public_base_url": "",
  "wechat_author": "Mister Panda",
  "scout": {
    "domain_keywords": ["AI", "科技"],
    "suggestions_count": 5
  }
}
```

### `PATCH /api/config`

部分更新配置（deep merge 到 `pipeline.local.yaml`）。

**Request:**
```json
{
  "default_llm": "dashscope/qwen3.5-plus",
  "llm_overrides": {"writer": "openai-codex/gpt-5.4"},
  "notify_channel": "1480223789626294466",
  "image_engine": "auto"
}
```

支持的顶层 key：`default_llm`, `gateway_url`, `llm_mode`, `gateway_agent_id`, `image_engine`, `llm_overrides`, `wechat_author`, `notify_channel`, `public_base_url`, `scout`

**Response:**
```json
{"ok": true, "message": "Config updated", "updated_keys": ["default_llm", "llm_overrides"]}
```

### `GET /api/config/models`

列出各角色当前使用的模型。

**Response:**
```json
{
  "default_llm": "dashscope/qwen3.5-plus",
  "roles": {
    "scout": "anthropic-sonnet/claude-sonnet-4-6",
    "researcher": "anthropic-sonnet/claude-sonnet-4-6",
    "writer": "openai-codex/gpt-5.4",
    "de_ai_editor": "dashscope/qwen3.5-plus",
    "director": "anthropic/claude-opus-4-6",
    "director_refine": "dashscope/qwen3.5-plus"
  },
  "overrides": {
    "scout": "anthropic-sonnet/claude-sonnet-4-6",
    "writer": "openai-codex/gpt-5.4"
  }
}
```

### `PUT /api/config/models`

设置模型配置。`overrides` 为空对象表示全部使用默认模型。

**Request — 全部用默认:**
```json
{
  "default_llm": "dashscope/qwen3.5-plus",
  "overrides": {}
}
```

**Request — 按角色指定:**
```json
{
  "default_llm": "dashscope/qwen3.5-plus",
  "overrides": {
    "writer": "openai-codex/gpt-5.4",
    "director": "anthropic/claude-opus-4-6"
  }
}
```

**Response:**
```json
{"ok": true, "message": "Models updated"}
```

### `GET /api/config/notify`

获取通知配置。

**Response:**
```json
{
  "notify_channel": "1480223789626294466",
  "discord_bot_token_set": true,
  "public_base_url": ""
}
```

### `PUT /api/config/notify`

设置通知频道。

**Request:**
```json
{
  "notify_channel": "1480223789626294466",
  "public_base_url": "https://my-server:8765"
}
```

**Response:**
```json
{"ok": true, "updated": ["notify_channel", "public_base_url"]}
```

### `GET /api/config/image-engine`

获取图片引擎配置。

**Response:**
```json
{
  "current": "auto",
  "available": [
    {"name": "pollinations", "mode": "api", "available": true},
    {"name": "dall-e-3", "mode": "api", "available": false}
  ]
}
```

### `PUT /api/config/image-engine`

设置图片引擎。

可选值：`auto`, `pollinations`, `dall-e-3`, `dashscope`, `browser:jimeng`, `browser:tongyi`

**Request:**
```json
{"engine": "dall-e-3"}
```

**Response:**
```json
{"ok": true, "engine": "dall-e-3"}
```

### `GET /api/config/prompts`

列出所有 prompt 文件。

**Response:**
```json
{
  "prompts": [
    {"name": "scout.md", "title": "Scout — 选题策划 Agent", "size": 4487, "lines": 199},
    {"name": "writer.md", "title": "Writer — 微信公众号主笔", "size": 3206, "lines": 179}
  ]
}
```

### `GET /api/config/prompts/{name}`

读取指定 prompt 全文。

**Response:**
```json
{"name": "scout.md", "content": "# Scout — 选题策划 Agent\n\n..."}
```

### `PUT /api/config/prompts/{name}`

更新 prompt 内容（自动保存 `.prev` 备份）。

**Request:**
```json
{"content": "# Scout — 选题策划 Agent\n\n更新后的内容..."}
```

**Response:**
```json
{"ok": true, "name": "scout.md", "size": 4500}
```

---

## 3. Run 管理

### `GET /api/runs`

列出所有 Run。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 20 | 最多返回条数 |
| `status` | string | "" | 按状态过滤 |

**Response:**
```json
{
  "runs": [
    {
      "run_id": "run_20260313_162023",
      "status": "completed",
      "topic": {"title": "..."},
      "current_stage": "publisher",
      "created_at": "2026-03-13T16:20:23"
    }
  ]
}
```

### `POST /api/runs`

创建新 Run。

**Request:**
```json
{
  "topic": "AI Agent 行业趋势分析",
  "platform": "wechat",
  "auto_approve": false
}
```

**Response:**
```json
{"ok": true, "run_id": "run_20260313_230100"}
```

### `GET /api/runs/{run_id}`

获取单个 Run 详情。

**Response:** 完整的 `state.yaml` 内容，JSON 格式。

### `DELETE /api/runs/{run_id}`

删除 Run。

**Response:**
```json
{"ok": true}
```

### `POST /api/runs/{run_id}/delete`

删除 Run（POST 兼容，同 DELETE）。

### `POST /api/runs/{run_id}/start`

启动 Run 的 Pipeline 执行。

**Response:**
```json
{"ok": true, "message": "Pipeline started"}
```

### `POST /api/runs/{run_id}/cancel`

取消正在执行的 Run。

**Response:**
```json
{"ok": true}
```

### `GET /api/runs/{run_id}/article`

获取当前文章内容（`article_edited.md` 或 `article_draft.md`）。

**Response:**
```json
{"content": "# 文章标题\n\n正文内容...", "source": "article_edited.md"}
```

### `POST /api/runs/{run_id}/article`

手动修改文章内容。

**Request:**
```json
{"content": "# 修改后的文章\n\n新内容..."}
```

### `GET /api/runs/{run_id}/diff`

获取当前节点的产物 diff（与 `.prev` 文件对比）。

**Response:**
```json
{
  "node": "writer",
  "filename": "article_edited.md",
  "diff": "--- article_edited.md.prev\n+++ article_edited.md\n@@ -1,3 +1,3 @@..."
}
```

### `GET /api/runs/{run_id}/nodes/{node_id}/output`

获取节点输出（HTML 片段，供 HTMX 渲染）。

### `GET /api/runs/{run_id}/nodes/{node_id}/input`

获取节点输入上下文（HTML 片段）。

### `POST /api/runs/{run_id}/auto-skip`

设置自动跳过节点列表。

**Request:**
```json
{"skip_nodes": ["de_ai_editor"]}
```

### `GET /api/runs/{run_id}/preview/html`

获取最终排版 HTML 预览。

### `POST /api/runs/{run_id}/clone`

克隆 Run（可修改主题）。

**Request:**
```json
{"new_topic": "新的主题（可选）"}
```

**Response:**
```json
{"ok": true, "new_run_id": "run_20260313_231500", "cloned_from": "run_20260313_162023"}
```

### `GET /api/runs/{run_id}/timeline`

获取执行时间线。

**Response:**
```json
{
  "run_id": "run_20260313_162023",
  "status": "completed",
  "timeline": [
    {"node": "scout", "started": "2026-03-13T16:20:23", "ended": "2026-03-13T16:21:45", "messages": 5},
    {"node": "researcher", "started": "2026-03-13T16:22:00", "ended": "2026-03-13T16:25:30", "messages": 2}
  ]
}
```

### `POST /api/runs/{run_id}/auto-approve`

开启/关闭全自动模式（跳过所有审核）。

**Request:**
```json
{"enabled": true}
```

**Response:**
```json
{"ok": true, "auto_approve": true}
```

---

## 4. 节点审核

### `POST /api/runs/{run_id}/review`

审批通过当前节点。

**Request:**
```json
{
  "approved": true,
  "feedback": "审核意见（可选）",
  "source": "web"
}
```

**Response:**
```json
{"ok": true, "node": "scout", "next_stage": "researcher"}
```

### `GET /api/runs/{run_id}/chat/history`

获取当前节点的聊天历史。

| 参数 | 类型 | 说明 |
|------|------|------|
| `node` | string | 指定节点（留空用当前节点） |

**Response:**
```json
{
  "messages": [
    {"role": "assistant", "content": "...", "timestamp": "2026-03-13T16:20:23"},
    {"role": "user", "content": "...", "timestamp": "2026-03-13T16:21:00"}
  ]
}
```

### `POST /api/runs/{run_id}/chat`

与当前节点 AI 角色对话。

**Request:**
```json
{
  "message": "把第二段改得更口语化",
  "node": "writer",
  "source": "web"
}
```

**Response:**
```json
{
  "reply": "好的，已修改...",
  "state_updated": true
}
```

### `POST /api/runs/{run_id}/nodes/{node_id}/rerun`

重新执行指定节点。

**Response:**
```json
{"ok": true, "node": "researcher"}
```

### `POST /api/runs/{run_id}/reject`

驳回当前节点（带反馈重新执行）。

**Request:**
```json
{
  "reason": "引用的数据有误，请重新查证",
  "source": "web"
}
```

**Response:**
```json
{"ok": true, "node": "researcher", "message": "Node rejected and will rerun"}
```

### `POST /api/runs/{run_id}/rollback`

回退到指定节点重新执行。

**Request:**
```json
{
  "target_node": "scout",
  "reason": "选题方向需要调整",
  "source": "web"
}
```

**Response:**
```json
{"ok": true, "target_node": "scout", "message": "Rolled back to scout"}
```

### `POST /api/runs/{run_id}/rollback/image-gen-to-director`

特殊回退：从图片生成阶段回退到导演阶段。

---

## 5. 产物管理

### `GET /api/runs/{run_id}/artifacts`

列出所有产物文件。

**Response:**
```json
{
  "run_id": "run_20260313_162023",
  "count": 28,
  "artifacts": [
    {"name": "article_edited.md", "size": 5293, "type": "markdown"},
    {"name": "topic.yaml", "size": 15701, "type": "yaml"},
    {"name": "images/cover.png", "size": 1735591, "type": "image"}
  ]
}
```

### `GET /api/runs/{run_id}/artifacts/{filename}`

读取产物文件内容。图片文件直接返回二进制。

**Response（文本文件）:**
```json
{
  "name": "topic.yaml",
  "type": "yaml",
  "content": "title: ...\nangle: ...",
  "parsed": {"title": "...", "angle": "..."}
}
```

### `PUT /api/runs/{run_id}/artifacts/{filename}`

写入/修改产物文件（自动保存 `.prev` 备份）。

**Request:**
```json
{"content": "修改后的文件内容"}
```

**Response:**
```json
{"ok": true, "name": "topic.yaml", "size": 1500}
```

### `GET /api/runs/{run_id}/visual-plan`

获取导演视觉方案。

**Response:**
```json
{
  "run_id": "run_20260313_162023",
  "visual_plan": {
    "style": "tech-digital",
    "cover": {"title": "...", "description": "..."},
    "placements": [
      {"id": "img_001", "after_section": "h2_title", "description": "..."}
    ]
  },
  "existing_images": ["cover.png", "img_001.png"],
  "has_images": true
}
```

### `PUT /api/runs/{run_id}/visual-plan`

直接设置/修改视觉方案。

**Request:**
```json
{
  "style": "tech-digital",
  "cover": {"title": "封面标题", "description": "封面描述"},
  "placements": [
    {"id": "img_001", "after_section": "AI Agent 的三个层次", "description": "架构图"}
  ]
}
```

---

## 6. 图片管理

### `GET /api/runs/{run_id}/images/{image_name}`

获取图片文件。

### `POST /api/runs/{run_id}/images/upload`

上传图片（multipart/form-data）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `file` | file | 图片文件 |
| `placement_id` | string | 配图 ID（可选） |

### `POST /api/runs/{run_id}/images/upload-cover`

上传封面图片。支持两种方式：

**方式 1 — JSON (base64):**
```json
{
  "image": "data:image/png;base64,iVBORw0KGgo...",
  "ext": ".png"
}
```

**方式 2 — multipart/form-data:**
`file` 字段上传图片文件。

**Response:**
```json
{"ok": true, "filename": "cover.png", "size": 102400, "path": "images/cover.png"}
```

### `POST /api/runs/{run_id}/images/upload-placement`

上传指定位置的配图。

**Request (JSON):**
```json
{
  "placement_id": "img_001",
  "image": "data:image/png;base64,iVBORw0KGgo...",
  "ext": ".png"
}
```

**Request (multipart):**
- `file`: 图片文件
- `placement_id`: 配图 ID

**Response:**
```json
{"ok": true, "placement_id": "img_001", "filename": "img_001.png", "size": 204800}
```

### `POST /api/runs/{run_id}/placements/{pid}/caption`

设置配图说明文字。

**Request:**
```json
{"caption": "图1：系统架构图"}
```

### `DELETE /api/runs/{run_id}/placements/{pid}`

删除配图。

---

## 7. 设置与向导

### `GET /api/settings`

获取设置页面数据。

### `PUT /api/settings` / `POST /api/settings`

保存设置（deep merge 到 `pipeline.local.yaml` + 环境变量）。

**Request:**
```json
{
  "gateway_url": "http://localhost:18789",
  "default_llm": "dashscope/qwen3.5-plus",
  "llm_overrides": {},
  "notify_channel": "1480223789626294466",
  "image_engine": "auto"
}
```

### `POST /api/setup/test-gateway`

测试 Gateway 连接。

**Request:**
```json
{"gateway_url": "http://localhost:18789"}
```

**Response:**
```json
{"ok": true, "latency_ms": 45, "models_count": 12}
```

### `GET /api/setup/discover`

发现可用资源（模型、频道）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `gateway_url` | string | Gateway 地址 |

**Response:**
```json
{
  "models": [
    {"id": "dashscope/qwen3.5-plus", "label": "qwen3.5-plus (125k)"}
  ],
  "channels": [
    {"id": "1480223789626294466", "name": "#图文生成", "platform": "discord"}
  ]
}
```

### `POST /api/setup/save`

保存向导配置。

---

## 8. SSE 实时推送

### `GET /sse/{run_id}`

HTMX SSE 端点（HTML 片段推送，供 Web UI 实时更新）。

### `GET /api/runs/{run_id}/events`

JSON SSE 端点（供 OpenClaw Agent / 外部客户端订阅）。

**事件类型:**

| 事件 | 数据 | 触发时机 |
|------|------|----------|
| `chat_message` | `{"node": "writer", "role": "assistant", "content": "..."}` | 聊天消息 |
| `approved` | `{"node": "scout", "next_stage": "researcher"}` | 节点审批通过 |
| `rejected` | `{"node": "researcher", "reason": "..."}` | 节点驳回 |
| `rolled_back` | `{"target_node": "scout", "reason": "..."}` | 回退 |
| `review_needed` | `{"node": "writer", "run_id": "..."}` | 节点等待审核 |

**连接示例:**
```bash
curl -N http://localhost:8765/api/runs/run_20260313_162023/events
```

---

## 9. OpenClaw AI 工具

ContentPipe 注册了 11 个 AI 工具，OpenClaw LLM 可直接调用：

| 工具 | 说明 | 对应 API |
|------|------|----------|
| `contentpipe_create` | 创建新任务 | `POST /api/runs` |
| `contentpipe_status` | 查看 Run 状态 | `GET /api/runs/{id}` |
| `contentpipe_list` | 列出所有 Run | `GET /api/runs` |
| `contentpipe_approve` | 审批通过 | `POST /api/runs/{id}/review` |
| `contentpipe_chat` | 与节点对话 | `POST /api/runs/{id}/chat` |
| `contentpipe_reject` | 驳回节点 | `POST /api/runs/{id}/reject` |
| `contentpipe_rollback` | 回退到指定节点 | `POST /api/runs/{id}/rollback` |
| `contentpipe_config` | 读取/修改配置 | `GET/PATCH /api/config` |
| `contentpipe_artifacts` | 读取/修改产物 | `GET/PUT /api/runs/{id}/artifacts/*` |
| `contentpipe_system` | 系统诊断 | `GET /api/system/*` |

### 工具参数详情

#### `contentpipe_config`
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | string | ✅ | `get` 或 `set` |
| `key` | string | | `models` / `notify` / `image_engine` / `prompts` / `all` |
| `value` | object | | set 时的新值 |

#### `contentpipe_artifacts`
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `run_id` | string | ✅ | Run ID |
| `action` | string | ✅ | `list` / `get` / `put` / `upload_cover` / `upload_image` |
| `filename` | string | | get/put 时的文件名 |
| `content` | string | | put 时的内容 |
| `image` | string | | upload 时的 base64 图片 |
| `placement_id` | string | | upload_image 时的配图 ID |

#### `contentpipe_system`
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | string | ✅ | `status` / `engines` / `test_llm` / `test_notify` / `logs` |
| `model` | string | | test_llm 时指定模型 |
| `limit` | number | | logs 返回行数（默认 20） |

---

## 错误响应

所有错误返回标准格式：

```json
{
  "detail": "Run not found: run_20260313_999999"
}
```

| HTTP 状态码 | 说明 |
|-------------|------|
| 400 | 请求参数错误 |
| 404 | 资源不存在（Run / 文件 / Prompt） |
| 429 | 请求频率超限（60 次/分钟 per IP） |
| 500 | 服务器内部错误 |

---

## 速率限制

- **GET 请求**: 无限制
- **POST/PUT/PATCH/DELETE**: 60 次/分钟（per IP）
- 超限返回 `429 Too Many Requests`
- 可通过 `CONTENTPIPE_RATE_LIMIT` 环境变量调整

---

## 认证

设置 `CONTENTPIPE_AUTH_TOKEN` 环境变量启用认证。

- **Header**: `x-contentpipe-token: <token>`
- **Cookie**: `contentpipe_auth=<sha256(token)>`
- **免认证路径**: `/api/health`, `/api/info`, `/login`, `/static/*`

---

*生成时间: 2026-03-13 | 端点总数: 63*
