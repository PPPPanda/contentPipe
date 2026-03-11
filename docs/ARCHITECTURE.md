# ContentPipe Architecture v0.7.1

> 最后更新: 2026-03-11
> 形态: OpenClaw Plugin（managed-service）

## 1. 设计哲学

**每一步都是一次对话**。传统 Pipeline 是批处理——输入进去、结果出来。ContentPipe 把每个节点变成一个可交互的 AI 角色，用户可以在任何节点暂停、讨论、调整方向、重新执行。

核心原则：
- **节点即角色**：每个 Pipeline 节点都有专属 AI 人格（选题策划、调研员、写手、导演）
- **信息瀑布**：上游节点的输出自动成为下游的输入，用户的讨论意见也随之传递
- **断点可恢复**：状态持久化到 YAML，任意节点可断点续跑
- **双模式**：人工审核（每步暂停）或全自动（一键到底）
- **实时同步**：审核对话中的修改意见实时写回 YAML/文章，下游节点以最新版本为准

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    Web 控制台 (8765)                      │
│  FastAPI + Jinja2 + HTMX + Tailwind CSS + SSE           │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Dashboard │  │ Run 详情  │  │ 审核交互  │              │
│  │  仪表盘   │  │  时间线   │  │ 聊天+编辑 │              │
│  └──────────┘  └──────────┘  └──────────┘              │
├─────────────────────────────────────────────────────────┤
│                  Pipeline Engine                         │
│  scout → researcher → writer → director                  │
│  → image_gen → formatter → publisher                     │
│                                                          │
│  ┌─────────────────────────────────────────┐            │
│  │ Per-Node Session (chat_{node}.json)     │            │
│  │ internal msgs (LLM context) + visible   │            │
│  │ msgs (user chat) = 同一个 session       │            │
│  └─────────────────────────────────────────┘            │
├─────────────────────────────────────────────────────────┤
│                    LLM Gateway                           │
│  OpenClaw Gateway (localhost:18789)                      │
│  provider models + explicit blank-agent routing          │
└─────────────────────────────────────────────────────────┘
```

### 2.1 blank-agent 执行平面（v0.7.1）

为降低 OpenClaw 常规 agent 提示词/工作区污染，Gateway 模式下新增一条显式执行平面：

- ContentPipe 通过 `x-openclaw-agent-id: contentpipe-blank` 路由到空白 agent
- 同时保留独立 `x-openclaw-session-key`，保证每个 run / node 的上下文隔离
- blank-agent 的职责不是返回一段“可解析聊天文本”，而是**直接把正式产物写入项目目录**
- Pipeline 下游节点以文件为准，不以聊天输出为准

当前正式约束：

- agent id: `contentpipe-blank`
- 安装方式：`./start.sh install-agent`
- 生效方式：`openclaw gateway restart`
- 正式产物路径：`plugins/content-pipeline/output/runs/<run_id>/`

### 2.2 内置 skill 单源架构

ContentPipe 的能力增强不再继续堆进 Python pipeline，而是逐步内置为插件自带 skills：

```text
plugins/content-pipeline/skills/
```

设计约束：
- skills 作为插件内容的一部分，**与插件版本一起发布**
- 不采用“仓库内置 + 运行时外装”的双轨制
- blank-agent 只暴露 allowlist 中的 ContentPipe skills
- Python pipeline 保留：state / validator / 文件 contract / 确定性发布步骤

这套约束的目标是同时满足：
- 能力可查（agent 知道自己有哪些增强能力）
- 低污染（不把 blank-agent 重新做成大工作区 agent）
- 可部署（外部用户无需手工拼 skills）
- 可审计（所有 skills 进入 git 与 release）

## 3. Session 架构

### 3.1 节点 = Session

每个 Pipeline 节点维护独立的 session 文件：

```
output/runs/{run_id}/
├── chat_scout.json         ← Scout 执行 + 审核聊天
├── chat_researcher.json    ← Researcher 执行 + 审核聊天
├── chat_writer.json        ← Writer 执行 + 审核聊天
├── chat_director.json      ← Director 执行 + 审核聊天
└── ...
```

典型 Gateway 路由键：

```text
x-openclaw-agent-id: contentpipe-blank
x-openclaw-session-key: contentpipe:{run_id}:{node_id}:main
```

### 3.2 消息分层：internal vs visible

```json
// internal=true: 节点执行数据（前端不显示，LLM 能看到）
{"role": "user", "content": "[热搜数据]...", "internal": true, "tag": "scout_exec"}
{"role": "assistant", "content": "```yaml\ntitle: ...", "internal": true, "tag": "scout_exec"}

// internal=false: 用户审核对话（前端显示）
{"role": "user", "content": "换个角度，从成本切入", "tag": "user_chat"}
{"role": "assistant", "content": "好的，从成本角度...", "tag": "user_chat"}
```

### 3.3 实时同步机制

审核对话中的修改**实时写回 state**（不需要等用户点「继续」）：

```
用户发消息 → AI 回复 → 意图判断(qwen3.5-flash)
                           ↓ YES=有修改意图
                      完整同步: 当前YAML + 对话 → LLM → 更新后YAML → 写回state
                           ↓
                      前端左栏卡片自动刷新（绿框闪一下）
```

**同步覆盖的节点**：

| 节点 | 同步的 state 字段 | 同步方式 |
|------|------------------|---------|
| Scout | topic, writer_brief, handoff_to_researcher, reference_articles, user_requirements | YAML 同步 |
| Researcher | writer_packet, verification_results, evidence_backed_insights, open_issues | YAML 同步 |
| Writer | article_edited（文章全文） | 文章改写同步 |
| Director | visual_plan | YAML 同步 |

### 3.4 跨节点数据传递

```
Scout session                    Researcher session
┌──────────────────┐            ┌──────────────────┐
│ [internal] 热搜   │            │ [internal] 搜索   │
│ [internal] 搜索   │            │ [internal] Perp   │
│ [internal] YAML   │───topic──→│ [internal] 核查   │
│ [visible] 用户讨论│  writer   │ [visible] 用户讨论│
│ [visible] AI回复  │  brief    │ [visible] AI回复  │
└──────────────────┘  handoff   └──────────────────┘
                        ↓                ↓
                   writer_context    writer_packet
                        ↓                ↓
                   Writer session ←─────┘
```

## 4. 节点信息源

### Scout 数据源
| 来源 | 方法 | 产出 |
|------|------|------|
| 百度/Brave | web_search | 热点趋势 |
| 小红书 | mcporter (xiaohongshu MCP) | 社交话题 |
| Twitter/X | xreach CLI | 国际趋势 |
| 微信文章 | Playwright headless | 参考文章全文 |
| 用户指定 | 审核聊天中发链接 | 自动提取内容 |

### Researcher 数据源
| 来源 | 方法 | 产出 |
|------|------|------|
| Brave Search | web_search | 事实核查 |
| 小红书/Twitter/B站 | search_social | 社区观点 |
| 微信文章 | Playwright | 深度参考 |
| 用户指定 | 审核聊天 | 补充材料 |

### 审核聊天自动工具
用户在聊天中触发的自动工具：
- **发微信链接** → Playwright 自动提取正文
- **发任意 URL** → Jina Reader 抓取
- **"搜一下 XXX"** → Brave Search + 小红书搜索

## 5. Writer 三层消费结构

Writer 不再直接拼接零散字段，而是消费一份结构化的 `writer_context`：

```yaml
# 第 1 层：立题层（来自 Scout）
topic:
  title: "AI 一人公司：从概念到落地的成本账"
  content_angle: "从工具成本角度切入"
  proposed_thesis: "AI 降低了启动门槛，但隐性成本被严重低估"

# 第 2 层：执行层（来自 Scout writer_brief）
writer_brief:
  core_message: "..."
  must_cover: [...]
  structure: {...}

# 第 3 层：证据材料层（来自 Researcher writer_packet）
writer_packet:
  safe_facts: [...]
  forbidden_claims: [...]
  useful_data: [...]
```

### 去 AI 味自动执行
Writer 节点内部自动调用去 AI 味 LLM（Sonnet 4.6），用独立 session 不污染 Writer 对话。

## 6. 模板系统

### 6.1 模板匹配优先级

1. **Director style 直接映射**（最高优先级）
   - `tech-minimal` / `tech-flat` / `cyberpunk` → `tech-digital.html`
   - `watercolor` / `chinese-ink` → `lifestyle.html`
2. **Keywords 子串匹配**
   - `"AI Agent"` 包含 `"AI"` → `tech-digital.html`
3. **Fallback** → `base.html`

### 6.2 深色/浅色模板适配

内联样式根据模板类型自动切换：

| 模板 | 背景 | 文字 | 标题 | 引用 |
|------|------|------|------|------|
| base.html | 白 | #333 | #1a1a1a | #07c160 边 |
| tech-digital.html | #0d1117 | #c9d1d9 | #1e90ff | #30363d 边 |

### 6.3 图片插入策略

使用 Director 的 `after_section` 精确匹配 h2 标题定位：

```
Director visual_plan:
  img_001: after_section="## 数据背后的魔幻现实"
  img_002: after_section="## 工具是杠杆，不是魔法"

Formatter 匹配:
  扫描 HTML 中的 <h2> 标签 → 建立 section_map
  → img_001 插入 "数据背后的魔幻现实" section 内
  → img_002 插入 "工具是杠杆，不是魔法" section 内
```

防碰撞：多张图位置相同时自动间隔。
图注：图片下方显示 Director 的 `purpose` 字段。

## 7. Director 配图管理

### 7.1 配图面板
Director 审核页面的配图方案面板：
- 每张配图显示缩略图、对应段落、作用说明
- 🔄 替换按钮 → 选新图片上传替换
- 🗑️ 删除按钮 → 删除配图（visual_plan + 文件）

### 7.2 聊天图片上传
- 📎 按钮上传图片 → 预览 → 输入指令 → 发送
- 支持"用这张图替换配图2"等自然语言操作

### 7.3 API
- `POST /api/runs/{id}/images/upload` — 上传/替换配图
- `DELETE /api/runs/{id}/placements/{pid}` — 删除配图位置

## 8. LLM 模型分配

| 角色 | 模型 | 用途 |
|------|------|------|
| Scout | anthropic/claude-sonnet-4-6 | 选题分析 |
| Researcher | anthropic/claude-sonnet-4-6 | 深度调研 |
| Writer | openai-codex/gpt-5.4 | 写作主模型 |
| De-AI Editor | anthropic/claude-sonnet-4-6 | 去 AI 味（独立 session） |
| Director | anthropic/claude-opus-4-6 | 配图方案 |
| Director Refine | dashscope/qwen3.5-plus | 配图细化/压缩 |
| 图像 prompt helper | anthropic/claude-sonnet-4-6 | prompt 翻译/压缩 |
| 意图判断 | dashscope/qwen3.5-flash | 审核对话是否有修改意图 |
| YAML 同步 | dashscope/qwen3.5-flash | 对话→YAML 更新 |
| 聊天节点 | 跟随节点配置 | 审核聊天用该节点同一个 model |

**关键设计**：Writer 写稿 + 审核聊天 + 审核改写使用同一个 model，保持风格一致。

## 9. Pipeline 节点（7 步）

```
scout → researcher → writer → director → image_gen → formatter → publisher
```

| 节点 | 交互 | 说明 |
|------|------|------|
| Scout | ✅ | 选题分析，输出新 YAML schema |
| Researcher | ✅ | 事实核查+素材包 |
| Writer | ✅ | 写作+自动去AI味，支持文章编辑 |
| Director | ✅ | 配图方案，支持图片上传/替换/删除 |
| Image Gen | ⚙️ | Pollinations.ai 自动生成 |
| Formatter | ✅ | 排版预览（手机/电脑/HTML 三视图） |
| Publisher | ⚙️ | 微信/小红书发布 |

## 10. 文件结构

```
plugins/content-pipeline/
├── SKILL.md                    # 技能入口
├── README.md                   # 快速开始
├── openclaw.plugin.yaml        # 插件清单
├── start.sh                    # 服务启动 + install-agent
├── docs/
│   ├── ARCHITECTURE.md         # 本文件
│   └── schema-writer-context.yaml
├── config/
│   ├── pipeline.yaml           # 模型 / gateway 配置
│   ├── template-mapping.yaml   # 模板匹配规则
│   └── styles/                 # 风格配置
├── prompts/
│   ├── scout.md
│   ├── researcher.md
│   ├── writer.md
│   ├── de-ai-engine.md
│   ├── art-director.md
│   └── art-director-refine.md
├── templates/
│   └── wechat/
│       ├── base.html
│       ├── tech-digital.html   # 深色科技模板
│       ├── lifestyle.html
│       ├── business-finance.html
│       ├── news-insight.html
│       └── education.html
├── scripts/
│   ├── nodes.py                # Pipeline 节点实现
│   ├── tools.py                # 搜索/LLM/工具函数
│   ├── formatter.py            # Markdown→HTML 排版
│   ├── publisher.py            # 微信/小红书发布
│   ├── hot_news.py             # 热搜聚合
│   ├── env_loader.py           # 环境变量加载
│   ├── image_engines/          # 图片生成引擎
│   └── web/                    # Web 控制台
│       ├── app.py
│       ├── events.py (SSE)
│       ├── run_manager.py
│       ├── routes/
│       │   ├── pages.py
│       │   ├── api.py
│       │   └── sse.py
│       └── templates/
└── output/
    └── runs/{run_id}/
        ├── state.yaml
        ├── topic.yaml
        ├── research.yaml
        ├── article_draft.md
        ├── article_edited.md
        ├── visual_plan.json
        ├── formatted.html
        ├── chat_scout.json
        ├── chat_researcher.json
        ├── chat_writer.json
        ├── chat_director.json
        ├── writer_context.yaml
        └── images/
            ├── img_001.jpg
            ├── img_002.jpg
            └── ...
```

**正式产物约定：** blank-agent 直接写入 `output/runs/{run_id}/`，不再使用 workspace 根下漂移的 `runs/...` 作为兼容路径。

## 11. 开发进度

| Phase | 内容 | 状态 |
|-------|------|------|
| 1 | 架构设计 + 模板库 | ✅ |
| 2 | 去AI味 Prompt | ✅ |
| 3 | LangGraph 驱动层 | ✅ |
| 4 | Scout + Researcher | ✅ |
| 5 | Writer + De-AI (合并) | ✅ |
| 6 | Director + ImageGen (Pollinations) | ✅ |
| 7 | Web 控制台 + E2E | ✅ |
| 8 | Formatter + Publisher stub | ✅ |
| 9 | Scout 新 schema + 三层消费 | ✅ |
| 10 | Per-node session + internal/visible | ✅ |
| 11 | 实时同步（对话→YAML/文章） | ✅ |
| 12 | 模板匹配修复 + 深色适配 | ✅ |
| 13 | 图文匹配（after_section 精确插入） | ✅ |
| 14 | Director 配图管理（上传/替换/删除） | ✅ |
| 15 | Publisher 真实发布 | 🔴 |
| 16 | Cron 定时调度 | 🔴 |


## 12. 插件化架构（v0.7.0 新增）

### 12.1 从 Skill 到 Plugin

| 维度 | Skill 模式 (v0.6) | Plugin 模式 (v0.7) |
|------|-------------------|-------------------|
| 位置 | `skills/content-pipeline/` | `plugins/content-pipeline/` |
| 启动 | AI 手动 `uvicorn` | `start.sh` 自动管理 |
| 清单 | `SKILL.md` only | `openclaw.plugin.yaml` + `SKILL.md` |
| 通知 | 无 | Discord 自动推送 |
| 健康检查 | 无 | `/api/health` |
| 工具注册 | 无 | `contentpipe_*` 5 个工具 |

### 12.2 服务管理

```
start.sh start    → nohup uvicorn → PID 文件 → 健康检查
start.sh stop     → pkill → 确认退出
start.sh restart  → stop + start
start.sh status   → PID + /api/health
start.sh logs     → tail -f /tmp/contentpipe.log
```

### 12.3 Discord 通知流

```
Pipeline 节点执行完成
    ↓
_execute_pipeline() 检测 review 暂停
    ↓
notify_review_needed(run_id, node_id) → httpx.post → Gateway message API
    ↓
Discord #图文生成 频道收到通知（含 Web UI 链接）
```

### 12.4 AI 工具桥接

```
用户: "帮我写一篇关于 AI 的文章"
    ↓
AI 匹配 SKILL.md → 读取工具定义
    ↓
exec: curl -X POST localhost:8765/api/runs -d '{"topic":"AI"}'
    ↓
Pipeline 启动 → Discord 通知 → Web UI 可审核
```
