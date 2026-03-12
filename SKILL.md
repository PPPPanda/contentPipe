---
name: content-pipeline
description: "AI 图文内容生产线插件：选题→调研→写作→配图→排版→发布，每步可交互审核。支持 Discord 通知和 AI 工具调用。"
user-invocable: true
metadata:
  openclaw:
    emoji: 📝
    type: managed-service
    port: 8765
    requires:
      bins: ["python3"]
      config: ["pipeline.llm_mode"]
allowed-tools: ["message", "web_search", "web_fetch", "browser"]
---

# ContentPipe — AI 图文内容生产插件

> 从选题到发布的全流程 AI 内容生产。每一步都可以和专属 AI 讨论、调整、重跑。

## 触发条件

用户说以下任一：
- "写文章"、"选题"、"生成内容"、"content pipeline"、"contentpipe"
- "帮我写一篇关于 X 的公众号文章"
- "今天有什么热门话题"、"跑一个内容任务"

## 插件架构

```
┌──────────────────────────────────────────────────┐
│  OpenClaw Gateway                                 │
│  ├── AI 工具调用 (contentpipe_*)                   │
│  ├── Discord 通知 (message tool)                  │
│  └── Cron 定时任务                                │
├──────────────────────────────────────────────────┤
│  ContentPipe Service (Python, port 8765)          │
│  ├── REST API (/api/*)                            │
│  ├── Web 控制台 (Jinja2 + HTMX + Tailwind)       │
│  ├── SSE 实时推送                                  │
│  └── Pipeline Engine (7 节点)                     │
├──────────────────────────────────────────────────┤
│  LLM Provider                                     │
│  ├── DashScope (qwen3.5-plus/flash)              │
│  ├── Anthropic (claude-sonnet-4-6)               │
│  └── OpenAI Codex (gpt-5.4)                      │
└──────────────────────────────────────────────────┘
```

## 快速启动

```bash
# 方式 1: 启动脚本
cd <PLUGIN_DIR>
./start.sh start

# 方式 2: 直接运行
cd <PLUGIN_DIR>/scripts
python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8765

# 管理
./start.sh status    # 查看状态
./start.sh stop      # 停止
./start.sh restart   # 重启
./start.sh logs      # 查看日志
```

Web UI: `http://localhost:8765`

## Pipeline 流程（7 步）

```
scout → researcher → writer → director → image_gen → formatter → publisher
```

| 节点 | AI 角色 | 交互 | 说明 |
|------|---------|------|------|
| 🔍 Scout | 选题策划 | ✅ | 热搜分析+社交搜索，输出完整 Briefing |
| 📚 Researcher | 深度调研 | ✅ | 事实核查+证据包，输出 writer_packet |
| ✍️ Writer | 唯一作者人格 | ✅ | GPT 5.4 写稿/审核聊天 → Extractor 分离 reply/article → Sonnet 4.6 内部 polish |
| 🎬 Director | 视觉导演 | ✅ | 配图方案，支持图片上传/替换/删除 |
| 🖼️ Image Gen | 图片生成 | ⚙️ | Pollinations.ai 自动生成 |
| 📐 Formatter | 排版引擎 | ✅ | Markdown→微信 HTML，图文精确匹配 |
| 📤 Publisher | 发布器 | ⚙️ | 微信/小红书 |

## Discord 集成

Pipeline 事件自动推送到 #图文生成 频道：
- ⏸️ 节点等待审核 → 带 Web UI 链接
- ✅ Pipeline 完成 → 带预览链接
- ❌ 失败 → 带错误信息

## AI 工具（对话中可用）

| 工具 | 说明 | 用法 |
|------|------|------|
| contentpipe_create | 创建任务 | "帮我写一篇关于 AI 的文章" |
| contentpipe_status | 查看状态 | "pipeline 跑到哪了" |
| contentpipe_list | 列出任务 | "最近的任务列表" |
| contentpipe_approve | 审核通过 | "继续下一步" |
| contentpipe_chat | 节点聊天 | "告诉 Writer 换个开头" |

## AI 工具实现

当用户触发上述工具时，通过 HTTP 调用本地 API：

```python
# contentpipe_create
POST http://localhost:8765/api/runs
{"topic": "...", "platform": "wechat", "auto_approve": false}

# contentpipe_status
GET http://localhost:8765/api/runs/{run_id}

# contentpipe_list
GET http://localhost:8765/api/runs?limit=10

# contentpipe_approve
POST http://localhost:8765/api/runs/{run_id}/review
{"action": "approve"}

# contentpipe_chat
POST http://localhost:8765/api/runs/{run_id}/chat
{"message": "...", "node": "writer"}
```

## 模型分配

| 节点 | 模型 | 原因 |
|------|------|------|
| Scout | qwen3.5-plus | 快速+结构化 |
| Researcher | qwen3.5-plus | 量大+理解力 |
| Writer Main | gpt-5.4 | 唯一作者人格，写稿+审核聊天改稿 |
| Writer Extractor | qwen3.5-flash | reply/article 分离，避免正文混入说明文字 |
| De-AI | sonnet-4-6 | 内部 polish，隐藏工序 |
| Director | qwen3.5-plus | 策划能力 |
| 意图判断 | qwen3.5-flash | 轻量快速（结构化节点） |
| YAML 同步 | qwen3.5-flash | 结构化快速 |

可在 `config/pipeline.yaml` 的 `llm_overrides` 覆盖。

## 项目结构

```
plugins/content-pipeline/
├── openclaw.plugin.yaml    # 🆕 插件清单
├── start.sh                # 🆕 启动脚本
├── SKILL.md                # 技能入口（本文件）
├── README.md
├── config/
│   ├── pipeline.yaml       # 模型+平台配置
│   ├── template-mapping.yaml
│   └── styles/*.yaml
├── prompts/                # 6 个 Agent prompt
├── templates/wechat/*.html # 6 个排版模板
├── docs/
│   └── ARCHITECTURE.md     # 架构文档 v0.7.0
├── scripts/
│   ├── nodes.py            # Pipeline 节点实现
│   ├── tools.py            # LLM 调用+搜索工具
│   ├── formatter.py        # Markdown→HTML 排版
│   ├── publisher.py        # 发布器
│   ├── hot_news.py         # 热搜聚合
│   ├── env_loader.py       # API key 加载
│   ├── image_engines/      # 图片生成引擎
│   └── web/
│       ├── app.py          # FastAPI 主应用
│       ├── notify.py       # 🆕 Discord 通知
│       ├── events.py       # SSE 事件总线
│       ├── run_manager.py  # Run 状态管理
│       ├── routes/
│       │   ├── api.py      # REST API
│       │   ├── pages.py    # 页面路由
│       │   └── sse.py      # SSE 推送
│       └── templates/      # 10 个 Web 页面
└── output/runs/            # 运行产物
```

## Web 控制台 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 🆕 健康检查 |
| GET | `/api/info` | 🆕 插件信息 |
| GET | `/` | Dashboard |
| POST | `/api/runs` | 新建 Run |
| POST | `/api/runs/{id}/start` | 启动 Pipeline |
| GET | `/runs/{id}/review` | 节点交互页 |
| POST | `/api/runs/{id}/review` | 审批通过 |
| POST | `/api/runs/{id}/chat` | AI 聊天 |
| GET | `/api/runs/{id}/chat/history` | 聊天历史 |
| POST | `/api/runs/{id}/nodes/{node}/rerun` | 重跑节点 |
| POST | `/api/runs/{id}/images/upload` | 🆕 上传/替换配图 |
| DELETE | `/api/runs/{id}/placements/{pid}` | 🆕 删除配图 |
| GET | `/api/runs/{id}/article` | 获取文章 |
| POST | `/api/runs/{id}/article` | 保存编辑后文章 |
| GET | `/runs/{id}/preview` | 排版预览 |
| GET | `/sse/{id}` | SSE 实时事件 |

## 开发进度

| Phase | 内容 | 状态 |
|-------|------|------|
| 1-8 | 基础架构+全节点实现+Web 控制台 | ✅ |
| 9 | Scout 新 schema + Writer 三层消费 | ✅ |
| 10 | Per-node session + 消息分层 | ✅ |
| 11 | 实时同步（对话→YAML/文章） | ✅ |
| 12 | 模板匹配+深色适配 | ✅ |
| 13 | 图文精确匹配 | ✅ |
| 14 | Director 配图管理 | ✅ |
| 15 | **插件化改造** | ✅ 🆕 |
| 16 | Publisher 真实发布 | 🔴 |
| 17 | Cron 定时调度 | 🔴 |
