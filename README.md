# ContentPipe

> AI 图文内容生产流水线：从选题、调研、写作、配图、排版到发布，全流程可视化、可审核、可回退。

ContentPipe 是一个面向公众号 / 图文平台的内容生产系统。它把一篇内容拆成多个清晰节点：

- **Scout**：选题与切入角度
- **Researcher**：事实核查与证据包
- **Writer**：成稿生成
- **Director**：配图规划与视觉风格
- **Image Gen**：生图
- **Formatter**：排版与模板适配
- **Publisher**：发布或导出

每个节点都支持：
- 暂停审核
- 继续对话
- 局部修改
- 重跑当前节点
- 断点续跑

## 界面截图

![ContentPipe Web Console](docs/images/web-console-dashboard.png)

> 当前 Web 控制台总览页：展示运行统计、待审核任务、最近 runs，以及侧边栏快捷导航。

![ContentPipe Scout Review](docs/images/web-console-review-scout.png)

> Scout（选题监控）审核页：左侧展示结构化选题结果，中间是节点输出卡片，右侧是与 AI 的交互式讨论区，可直接调整选题、角度、摘要和写作要求。

![ContentPipe Pipeline Overview](docs/images/web-console-run-detail-pipeline.png)

> Run 详情页：展示一篇内容从选题监控、深度调研、写作、AI 导演、图片生成、排版预览到发布的完整 7 节点流水线状态，并可查看文章预览与各阶段产物。

---

## 1. 当前形态

本仓库当前以 **OpenClaw managed-service plugin** 方式组织：

- 插件目录：`plugins/content-pipeline/`
- Web 服务：FastAPI（默认 `http://localhost:8765`）
- LLM 调用：通过 OpenClaw Gateway
- Discord 通知：可选，默认关闭，部署时配置频道 ID

> 说明：仓库内保留了 `SKILL.md`，用于让 OpenClaw agent 理解如何调用/管理这个插件服务；核心运行形态是 **后台服务 + Web 控制台**。

---

## 2. 功能概览

### 2.1 交互式 Pipeline

```text
scout → researcher → writer → director → image_gen → formatter → publisher
```

| 节点 | 作用 | 是否可审核 |
|---|---|---|
| Scout | 热点扫描、选题提案、切入角度、writer brief | ✅ |
| Researcher | 事实核查、数据点、风险与禁写项 | ✅ |
| Writer | 生成文章草稿，结合 writer_context 三层结构 | ✅ |
| Director | 规划配图位置、风格、目的、描述 | ✅ |
| Image Gen | 根据规划生成图片 | ⚙️ |
| Formatter | 将 Markdown 转为平台 HTML，并插图 | ✅ |
| Publisher | 公众号 / 小红书导出或发布 | ⚙️ |

### 2.2 关键特性

- **Per-node session**：每个节点独立会话，执行记录与审核聊天共享上下文
- **实时同步**：审核聊天中的明确修改，会自动同步回左侧结构化数据 / 文章正文
- **图文精确匹配**：使用 `after_section` 定位，把图片插入指定段落下方
- **模板适配**：支持深色/浅色模板的内联样式输出
- **导向式审核**：在 Web UI 中查看节点输出卡片、文章、配图方案、预览
- **图片管理**：支持替换、删除指定配图，并同步左侧视觉方案
- **服务化运行**：支持健康检查、启动脚本、Discord 通知

---

## 3. 架构

### 3.1 总体结构

```text
OpenClaw Gateway
├─ LLM 请求转发
├─ Discord message API（可选）
└─ Agent / Skill 调用

ContentPipe Service (FastAPI)
├─ Web UI（Jinja2 + HTMX）
├─ REST API
├─ SSE 事件推送
├─ Run 状态持久化
└─ Pipeline 节点执行
```

### 3.2 目录结构

```text
content-pipeline/
├─ README.md
├─ SKILL.md
├─ openclaw.plugin.yaml
├─ start.sh
├─ .gitignore
├─ config/
│  ├─ pipeline.yaml
│  ├─ template-mapping.yaml
│  └─ styles/
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ schema-scout.yaml
│  ├─ schema-researcher.yaml
│  └─ schema-writer-context.yaml
├─ prompts/
├─ scripts/
│  ├─ nodes.py
│  ├─ tools.py
│  ├─ formatter.py
│  ├─ publisher.py
│  ├─ hot_news.py
│  ├─ jimeng.py
│  ├─ image_engines/
│  └─ web/
│     ├─ app.py
│     ├─ notify.py
│     ├─ events.py
│     ├─ run_manager.py
│     ├─ routes/
│     ├─ templates/
│     └─ static/
├─ templates/
│  └─ wechat/
└─ output/
   └─ runs/
```

### 3.3 状态持久化

每个 run 目录下会保存：

- `state.yaml`
- `topic.yaml`
- `research.yaml`
- `article_draft.md`
- `article_edited.md`
- `visual_plan.json`
- `formatted.html`
- `chat_<node>.json`
- `images/*`

默认 `.gitignore` 会忽略 `output/runs/`，避免把运行产物、聊天记录、图片和测试数据提交到公开仓库。

---

## 4. 运行要求

### 4.1 系统依赖

- Python 3.10+
- OpenClaw Gateway
- 可选：Discord channel 配置
- 可选：微信公众号 / 小红书发布配置

### 4.2 Python 依赖

建议使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn sse-starlette python-multipart jinja2 pyyaml httpx
```

如果你打算启用更多能力，还可能需要：

- `playwright`
- `pillow`
- 相关浏览器驱动 / 浏览器 relay 环境

---

## 5. 配置

### 5.1 主配置：`config/pipeline.yaml`

关键字段：

```yaml
pipeline:
  default_platform: "wechat"
  llm_mode: "direct"   # 或 gateway
  default_llm: "dashscope/qwen3.5-plus"
  gateway_url: "http://localhost:18789"
  llm_overrides:
    scout: "dashscope/qwen3.5-plus"
    researcher: "dashscope/qwen3.5-plus"
    writer: "openai-codex/gpt-5.4"
    de_ai_editor: "anthropic/claude-sonnet-4-6"
    director: "dashscope/qwen3.5-plus"
```

### 5.2 环境变量

常用环境变量：

```bash
OPENCLAW_GATEWAY_URL=http://localhost:18789
CONTENTPIPE_PORT=8765
CONTENTPIPE_HOST=0.0.0.0
CONTENTPIPE_NOTIFY_CHANNEL=<discord_channel_id>
CONTENTPIPE_PUBLIC_BASE_URL=http://localhost:8765
CONTENTPIPE_AUTH_TOKEN=change-me
CONTENTPIPE_LOG_LEVEL=INFO

WECHAT_APPID=...
WECHAT_SECRET=...
OPENAI_API_KEY=...
DASHSCOPE_API_KEY=...
ANTHROPIC_API_KEY=...
```

说明：
- `CONTENTPIPE_NOTIFY_CHANNEL` 为空时，不会发送 Discord 通知
- `CONTENTPIPE_PUBLIC_BASE_URL` 用于 Discord 通知里的回链地址
- `CONTENTPIPE_AUTH_TOKEN` 非空时，Web UI / API 会开启鉴权（浏览器登录或请求头 `X-ContentPipe-Token`）
- 发布相关密钥建议只通过环境变量或本地未跟踪配置注入

---

## 6. 启动方式

### 6.1 使用启动脚本（推荐）

```bash
./start.sh start
./start.sh status
./start.sh logs
./start.sh stop
./start.sh restart
```

### 6.2 Docker 一键部署（推荐给外部用户）

```bash
cp .env.example .env
# 修改 .env 里的 CONTENTPIPE_AUTH_TOKEN / OPENCLAW_GATEWAY_URL

docker compose up -d --build
```

启动后：
- Web UI: `http://localhost:8765`
- 首次访问会要求输入 `CONTENTPIPE_AUTH_TOKEN`

### 6.3 直接启动 uvicorn

```bash
cd scripts
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8765
```

### 6.4 健康检查

```bash
curl http://localhost:8765/api/health
curl http://localhost:8765/api/info
```

### 6.5 生产部署 / 反向代理 / HTTPS

如果你要把它部署给别人使用，推荐的最小方案是：

1. ContentPipe 只监听内网或 Docker 网络
2. 用 Nginx / Caddy 做反向代理
3. 打开 `CONTENTPIPE_AUTH_TOKEN`
4. 通过 HTTPS 暴露外部访问
5. 把 `CONTENTPIPE_PUBLIC_BASE_URL` 设成最终对外域名

示例（Nginx）：

```nginx
server {
    listen 80;
    server_name contentpipe.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name contentpipe.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

对应 `.env` 建议：

```bash
CONTENTPIPE_AUTH_TOKEN=change-me
CONTENTPIPE_PUBLIC_BASE_URL=https://contentpipe.example.com
OPENCLAW_GATEWAY_URL=http://host.docker.internal:18789
```

如果只在本机使用，可以不配反代和 HTTPS；但**只要要给别人访问，就建议必须开 HTTPS + 鉴权**。

---

## 7. Web UI

主要页面：

- `/`：Dashboard
- `/runs`：运行列表
- `/runs/{run_id}`：运行详情
- `/runs/{run_id}/review?node=scout`：节点审核页
- `/runs/{run_id}/preview`：排版预览
- `/settings`：配置页面

### 7.1 审核页能力

在节点审核页中，你可以：

- 查看结构化卡片
- 与当前节点 AI 对话
- 修改标题 / 切角 / 写法
- 重跑节点
- 在 Director 页面替换 / 删除图片
- 在 Writer 页面直接编辑文章
- 在 Formatter 页面查看最终预览

---

## 8. API 概览

### 基础

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/info` | 插件信息 |
| GET | `/api/runs` | 列出 run |
| POST | `/api/runs` | 创建 run |
| POST | `/api/runs/{id}/start` | 启动 pipeline |

### 审核 / 交互

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/runs/{id}/chat/history` | 获取聊天记录 |
| POST | `/api/runs/{id}/chat` | 与当前节点对话 |
| POST | `/api/runs/{id}/review` | 审批继续 |
| POST | `/api/runs/{id}/nodes/{node}/rerun` | 重跑节点 |

### Writer / Director 相关

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/runs/{id}/article` | 获取当前文章 |
| POST | `/api/runs/{id}/article` | 保存编辑后的文章 |
| POST | `/api/runs/{id}/images/upload` | 上传 / 替换图片 |
| DELETE | `/api/runs/{id}/placements/{pid}` | 删除一个配图位 |

### 预览 / 导出

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/runs/{id}/preview/html` | 获取排版 HTML |
| GET | `/api/runs/{id}/images/{image_name}` | 获取图片 |
| GET | `/sse/{id}` | SSE 事件流 |

---

## 9. 代码审查结论（当前版本）

在公开前做过一轮仓库级检查，当前需要特别注意：

### 已处理

- ✅ 忽略 `output/runs/` 运行产物
- ✅ 忽略日志、缓存、虚拟环境
- ✅ Discord 通知频道改为环境变量注入，不再硬编码公开仓库默认值
- ✅ Gateway 地址支持环境变量覆盖

### 仍建议后续继续优化

- `scripts/tools.py` / `scripts/publisher.py` 中有部分发布逻辑重复，可进一步收敛
- `scripts/nodes.py` 体积较大，建议未来按节点拆分模块
- 插件清单目前是仓库内的 manifest 文档；如果要做成真正 OpenClaw 原生 TS 扩展，还需要补注册层
- 发布器能力仍偏平台定制，公开 release 前建议补一份最小 demo 配置

---

## 10. 开发说明

### 10.1 本地检查建议

```bash
python3 -m compileall scripts
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8765
curl http://localhost:8765/api/health
```

### 10.2 推荐提交流程

```bash
git status
git add .
git commit -m "feat: ..."
git push origin main
```

### 10.3 如果你要新接入平台

通常需要改动：

1. `config/pipeline.yaml`
2. `templates/`
3. `formatter.py`
4. `publisher.py`
5. 对应节点 prompt

---

## 11. 路线图

### 已完成

- 交互式多节点 pipeline
- 实时同步（聊天 → 结构化状态 / 文章）
- Writer 三层上下文
- 图文精确匹配
- Director 配图管理
- 基础插件化（服务清单、健康检查、通知）

### 待完成

- 真正的一键发布链路
- 定时任务 / cron 编排
- 更完整的公开安装流程
- 更多平台模板
- 更细粒度的权限和配置注入

---

## 12. License / 开源配套文件

仓库当前已经补齐以下公开发布基础文件：

- `LICENSE`（MIT）
- `CONTRIBUTING.md`
- `SECURITY.md`

如果你准备正式 release，建议下一步继续补：

- `CHANGELOG.md`
- GitHub Release notes
- 部署示例截图 / demo 数据
