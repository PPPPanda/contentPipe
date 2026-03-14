# ContentPipe Architecture v0.7.2

> 最后更新: 2026-03-12
> 形态: OpenClaw Plugin（managed-service）

## 1. 设计哲学

**每一步都是一次对话**。传统 Pipeline 是批处理——输入进去、结果出来。ContentPipe 把每个节点变成一个可交互的 AI 角色，用户可以在任何节点暂停、讨论、调整方向、重新执行。

核心原则：
- **节点即角色**：每个 Pipeline 节点都有专属 AI 人格（选题策划、调研员、写手、导演）
- **信息瀑布**：上游节点的输出自动成为下游的输入，用户的讨论意见也随之传递
- **断点可恢复**：状态持久化到 YAML，任意节点可断点续跑
- **双模式**：人工审核（每步暂停）或全自动（一键到底）
- **逐轮提交仲裁**：每次节点运行（初始执行 / 审核追问 / 重试）后，Python 都会读回正式产物并做提交仲裁；MVP 阶段先做最小提交判定，后续再补深层校验

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
- blank-agent 的职责是承载节点的**正式产物修改**（初始执行 + 后续审核追问）
- 节点收到命令后先自行执行任务并尝试修改自己的正式产物文件
- Python 在**每一轮 LLM 运行后**读回文件，并先做最小提交判定；更深层的格式/规范校验和带行号反馈作为下一阶段增强
- Pipeline 下游节点以正式产物文件为准，不以聊天解释文字为准

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
├── chat_writer.json        ← Writer 执行 + 审核聊天（唯一作者人格）
├── chat_director.json      ← Director 执行 + 审核聊天
├── writer_last_exchange.json ← Writer 最近一次审核改稿调试记录（可选）
└── ...
```

典型 Gateway 路由键：

```text
x-openclaw-agent-id: contentpipe-blank
x-openclaw-session-key: contentpipe:{run_id}:{node_id}:main
```

节点的规范形态是：**一个节点 = 一个主 session = 一个主提示词 = 一个正式产物文件**。

```text
contentpipe:{run_id}:scout:main
contentpipe:{run_id}:researcher:main
contentpipe:{run_id}:writer:main
contentpipe:{run_id}:director:main
contentpipe:{run_id}:formatter:main
```

说明：
- 初始执行与后续审核追问共用同一个 node session / prompt，属于同一个连续 agent 人格
- 节点可以在审核阶段继续直接修改自己的正式产物文件
- 同一个节点的“继续修、重试、用户追问”都沿用这个连续 session
- 个别内部 helper lane（例如去 AI 味、图片细化）可以作为实现细节保留，但**不是架构主路径**，也不改变“单节点单主 session”的设计

### 3.2 消息分层：internal vs visible

```json
// internal=true: 节点执行数据（前端不显示，LLM 能看到）
{"role": "user", "content": "[热搜数据]...", "internal": true, "tag": "scout_exec"}
{"role": "assistant", "content": "```yaml\ntitle: ...", "internal": true, "tag": "scout_exec"}

// internal=false: 用户审核对话（前端显示）
{"role": "user", "content": "换个角度，从成本切入", "tag": "user_chat"}
{"role": "assistant", "content": "好的，从成本角度...", "tag": "user_chat"}
```

### 3.3 逐轮执行 / 最小提交 / 后续校验机制

无论是**节点初始执行**、**审核聊天追问**还是**手动重试**，都走同一个闭环：

```
收到命令 / 用户消息
  → 当前节点主 agent（同一 session / 同一 prompt）执行任务并修改正式产物
  → Python 读回正式产物文件
  → 先做最小提交判定（是否存在 / 是否可读 / 是否写到正确路径 / 是否有变化）
      ↓ 通过
    更新 state / 写 prev / 刷新 UI / 允许下游继续消费
      ↓ 失败
    反馈“未成功提交正式产物”给同一节点继续修
```

这意味着：
- **每次 LLM 运行一轮，都必须经过 Python 读回正式产物**
- 节点可以用追问方式持续修同一个文件
- 用户审核聊天本质上也是“继续驱动同一个节点修正式产物”

关键约束：
- **允许局部 edit，也允许整文件 write**；架构不强制修改方式
- **Python 不信 agent 的口头确认**，只信正式产物文件的实际结果
- **状态更新以正式产物为主**：必要时由 Python 从正式产物反推 state 对应字段
- **纯讨论可以不改文件**；只有正式产物实际发生变化，才算本轮修改成功

### 3.3.1 MVP 必做（当前阶段）

当前阶段先不做深层内容校验，Python 至少必须做：

1. **路径正确性**：节点只能修改自己的正式产物文件
2. **文件存在性**：本轮结束后目标文件必须存在
3. **文件可读性**：必须能以正确编码读回
4. **变更检测**：本轮文件内容 / hash / mtime 至少有一个发生变化
5. **state 回写**：由正式产物反推并刷新 state
6. **prev / diff**：对可比对节点保留上一版本，支持 diff 与回滚
7. **前端刷新**：只基于 Python 读回并接受的结果刷新 UI

### 3.3.2 可后置（下一阶段补）

以下内容校验先不作为 MVP 硬要求，但后续应逐步补齐：

- Scout / Researcher 的 YAML schema 校验
- Director 的 JSON schema 校验
- Writer 的 document contract 校验（例如正文纯净度、禁止解释性话语混入）
- 更细粒度的结构化错误反馈（行号 / 字段 / 失败原因）
- 校验失败后的自动重试链路

### 3.3.3 节点正式产物约定

| 节点 | 正式产物 | Python MVP 提交动作 |
|------|---------|---------------------|
| Scout | `topic.yaml` | 读回文件、确认变更、刷新 `topic`/`writer_brief`/`reference_articles` 等 state |
| Researcher | `research.yaml` | 读回文件、确认变更、刷新 `writer_packet` / 核查结果 |
| Writer | `article_edited.md`（或初稿阶段 `article_draft.md`） | 读回正文、确认变更、刷新正文 / diff / 预览 |
| Director | `visual_plan.json` | 读回文件、确认变更、刷新配图方案 |
| Formatter | `formatted.html` | 读回文件、确认变更、刷新 HTML 预览 |

这个协议的核心是：**节点的人格连续，但提交权交给 Python。**

### 3.3.4 当前实现状态（2026-03-12）

当前代码已经落地的主路径如下：

- **Scout / Researcher / Director / Formatter**
  - 审核聊天走同一个 node session
  - LLM 可直接修改本节点正式产物
  - Python 读回正式产物后做最小提交判定
  - 成功则刷新 state 与左侧 UI
- **Writer**
  - `writer:main` 为连续主 session
  - `writer-structure.md` 对应的结构 helper 每次 fresh session 运行
  - 结构 helper 负责把正式正文落到 `article_edited.md`
  - Writer 额外通过 `writer-subtitle.md` 生成面向发布的 `article.subtitle`（微信公众号草稿 digest）
  - Python 再读回正文、写 `.prev`、更新 `state.article_edited` / `state.article.subtitle`、刷新左侧 UI
- **Publisher**
  - Web pipeline 现在执行真实 `publisher_node`，不再只是 stub completed
  - 发布后会写入 `publish_result.json` 与 `state.publish_result`
  - 成功显示 `draft_saved`；失败显示 `failed`；未配置凭证时显示 `local_only`
- 旧的 `_sync_chat_to_state()` 二次 YAML 同步链路已经退役，不再作为主路径
- 旧的 `writer-extractor.md` 已移入 `prompts/deprecated/`，不再挂主链路

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

## 5. Writer：连续主 session + fresh 结构 LLM

Writer 仍然消费结构化的 `writer_context`，但在运行方式上比其他节点更特殊：

- **Writer 主 session 是连续的**：初稿、用户追问、审核聊天、重试，都走同一个 `writer:main` 连续 session
- **结构 LLM 是 fresh session**：每次单独启动一个新的 helper session，用来读取 Writer 主 session 里产出的正文，做结构化整理 / 反 AI 对抗 / 正式正文提取
- **Python 仍是最终仲裁者**：不管 Writer 主 session 还是结构 LLM 产出什么，最终都要经过 Python 读回文件、做提交仲裁、刷新 UI

Writer 的核心原则：
- **唯一作者人格**：同一个 Writer 主 session 负责持续写稿、追问、改稿；
- **主 session 连续**：用户追问和 retry 都沿用这个 session，不重新开新人格；
- **结构 helper 独立**：结构 LLM 每次 fresh session，不继承旧对话负担，只做“把正文整理成合规正式产物 + 把多余解释留给用户”这件事；
- **正式产物单一**：审核阶段唯一正式正文仍然是 `article_edited.md`；
- **提交由 Python 仲裁**：Python 读取正文、做最小提交判定、写 `.prev`、更新 state、驱动 diff/UI；正文内容契约校验后续再补。

Writer 不直接拼接零散字段，而是消费一份结构化的 `writer_context`：

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

### Writer 审核阶段闭环

审核聊天阶段：
1. 用户向 `writer:main` 追问/要求改稿
2. `writer:main` 在连续 session 中继续生成正文与说明
3. fresh 的结构 LLM 读取这轮正文，做结构化整理 / 反 AI 对抗 / 正文抽取
4. 结构 LLM 将正式正文写入 `article_edited.md`，把多余解释性话语留给用户侧
5. Python 读取 `article_edited.md` 并做最小提交判定（存在 / 可读 / 正确路径 / 有变化）
6. 通过后更新 `state.article_edited`、写 `.prev`、刷新左栏和 diff
7. 若失败，Python 把“未成功提交正式正文”的反馈再送回相应链路继续修

> 注：Writer 的正文内容契约校验（例如解释话语混入正文、结构污染、反 AI 纯净度）暂不作为 MVP 硬门槛，后续再补。

### 去 AI 味 / 结构化 helper
“去 AI 味”与“结构化整理”都可以由 Writer 的 helper LLM 承担；它们属于 Writer 节点内部实现，而不是新的用户交互人格。关键约束是：
- Writer 主 session 连续
- helper session 每次 fresh
- Python 永远负责最终校验与提交

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
| Scout | anthropic/claude-sonnet-4-6 | 选题分析 + 审核追问 |
| Researcher | anthropic/claude-sonnet-4-6 | 深度调研 + 审核追问 |
| Writer Main | openai-codex/gpt-5.4 | 连续主 session：写稿 + 审核聊天 + 改稿 |
| Writer Structure LLM | dashscope/qwen3.5-flash | fresh session：读取 Writer 正文，做结构整理 / 反 AI 对抗 / 正文落盘 |
| De-AI Editor | anthropic/claude-sonnet-4-6 | Writer 节点内部 polish（可并入结构 helper 链路） |
| Director | anthropic/claude-opus-4-6 | 配图方案 + 审核追问 |
| Director Refine | dashscope/qwen3.5-plus | 配图细化/压缩 |
| 图像 prompt helper | anthropic/claude-sonnet-4-6 | prompt 翻译/压缩 |
| Formatter | anthropic/claude-sonnet-4-6 | 排版预览 + 审核追问 |

**关键设计**：除 Writer 的“连续主 session + fresh 结构 helper”外，其余节点都走“单节点单主 session + 正式产物直接修改 + Python 逐轮提交仲裁”的统一闭环。

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
│   ├── writer-review.md
│   ├── writer-structure.md
│   ├── de-ai-engine.md
│   ├── art-director.md
│   ├── art-director-refine.md
│   └── deprecated/
│       └── writer-extractor.md
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
| 11 | 实时同步（对话→正式产物→Python 校验提交） | ✅ |
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
