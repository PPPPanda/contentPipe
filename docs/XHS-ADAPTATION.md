# ContentPipe 小红书适配设计

> 状态：设计文档 / 未全部实现
> 目标：在复用现有 ContentPipe pipeline 的前提下，补齐小红书（XHS / Xiaohongshu）内容生成、改写与发布链路。

---

## 1. 背景

ContentPipe 当前主链路以微信公众号长文为中心：
- Scout → Researcher → Writer → Director → Image Gen → Formatter → Publisher
- Writer 人格与输出结构偏向公众号长文
- Director / Formatter / Publisher 已有部分 `xhs` 痕迹，但整体还未形成真正可用的小红书工作流

当前代码现状：
- `platform: wechat / xhs` 已存在
- `run_new.html` 已支持选择 `xhs`
- `art-director.md` 已出现公众号/小红书比例差异
- `formatter.py` 有 `xhs` 分支
- `_publish_xhs()` 目前仅 `local_only`，还不是真正发布

因此，本设计的目标不是从零造一套“小红书新系统”，而是：

> **基于现有 pipeline 做平台分支收敛**，让 ContentPipe 能同时支持公众号长文与小红书笔记，并支持“公众号文章 → 小红书风格改写”的复用链路。

---

## 2. 核心结论

### 2.1 不是两套 pipeline，而是一条 pipeline + 平台分支

推荐架构：
- Scout / Researcher 前两节点共用
- Writer / Director / Formatter / Publisher 做平台化分支
- 允许从现有微信公众号 run 一键派生一个小红书改写 run

### 2.2 Writer 是平台适配的核心节点

公众号写作与小红书写作不是“长短变化”，而是：
- 信息组织方式不同
- 标题策略不同
- 读者预期不同
- 节奏、段落、CTA、标签、首评逻辑都不同

因此必须新增：
- `prompts/writer-xhs.md`

而不是让现有 `writer.md` 兼容一切。

### 2.3 小红书发布不是简单的 HTML 渲染问题

公众号发布侧重点是：
- HTML / 图片 CDN / 草稿箱

小红书发布侧重点是：
- 笔记文案
- 封面与多页图文卡片
- 标签
- 首评
- 登录态与浏览器自动化 / MCP

所以 Publisher-XHS 应视为一条独立发布链，但仍复用前面的内容生成与审校流程。

---

## 3. 设计原则

1. **共用前两节点**：不要重复实现选题与调研
2. **平台人格后置**：平台差异主要在 Writer 之后放大
3. **保留文件产物协议**：继续用文件作为正式产物与审校锚点
4. **先 local publish package，后真正发布**：先把 `xhs_content.json` 做强，再接自动发布
5. **支持派生 run**：从公众号 run 一键派生小红书 run，而不是覆盖原正文
6. **一条主线，多平台消费**：Research 输出要支持不同平台的二次消费方式

---

## 4. 平台差异总表

| 维度 | 微信公众号 | 小红书 |
|------|------------|--------|
| 内容形态 | 中长文 / 结构完整 | 笔记 / 清单 / 经验帖 / 多图卡片 |
| 标题策略 | 信息量 + 判断力 | 强结果 / 强利益点 / 强场景 |
| 开头 | 张力 + 问题引入 | 第一屏就给结果 |
| 正文节奏 | 可较长，强调论证 | 短段落，高信息密度 |
| 图片逻辑 | 封面 + 文中配图 | 封面首图 + 多页图卡 |
| 发布产物 | HTML + 草稿箱 | 笔记文案 + 标签 + 首评 + 图集 |
| CTA | 结尾思考 / 在看 | 收藏 / 评论 / 私信 / 抄作业 |

---

## 5. 节点设计

## 5.1 Scout（共用）

Scout 继续共用，但要显式引入平台策略。

### 新增输出建议

在 `topic.yaml` 中增加：

```yaml
platform_strategy:
  platform: xhs
  target_format: note
  hook_style: 痛点开场 / 结果开场 / 清单开场
  title_constraints:
    max_chars: 20
    style: 强结果 / 强场景 / 强利益点
  content_constraints:
    short_paragraphs: true
    emotional_density: medium
    actionability: high
```

### 作用
- 同一个选题，在微信公众号与小红书下会有不同立题形式
- 后续 Writer / Director 直接消费这个平台策略，而不是硬编码平台规则

---

## 5.2 Researcher（共用）

Researcher 继续共用，但输出要分平台消费包。

### 当前问题
Researcher 主要输出：
- `safe_facts`
- `cautious_points`
- `forbidden_claims`
- `writer_packet`

这些偏公众号长文消费。

### 新增输出建议

```yaml
platform_packet:
  wechat:
    longform_safe_facts: [...]
  xhs:
    note_safe_points: [...]
    checklist_points: [...]
    title_hooks: [...]
    comment_cta_candidates: [...]
```

### 作用
- 微信：更适合长文论证
- 小红书：更适合卡片化信息点、清单化表达、收藏向内容

---

## 5.3 Writer（平台分支核心）

## 5.3.1 微信公众号 Writer（保留现状）
- `prompts/writer.md`
- 人格：微信公众号主笔
- 适合中长文、结构完整、观点推进

## 5.3.2 小红书 Writer（新增）
- `prompts/writer-xhs.md`
- 人格：懂平台节奏的经验帖作者 / 笔记作者

### 小红书 Writer 的核心要求
- 第一屏直接给结果
- 强标题意识
- 短段落、高密度
- 更多列表 / emoji / checklist / “照着做”感
- 少宏大分析，多可执行经验
- 允许带“我怎么做 / 我踩过什么坑 / 你直接抄作业”的语气

### 输出结构建议
统一扩展 `article`：

```python
article = {
  "title": "...",
  "subtitle": "...",
  "platform": "xhs",
  "content": "...",
  "word_count": 680,
  "tags": ["#AI工具", "#内容创作"],
  "cover_text": "...",
  "first_comment": "..."
}
```

说明：
- `subtitle`：平台卡片摘要 / 发布摘要
- `tags`：小红书发布时需要
- `cover_text`：封面卡片常用
- `first_comment`：小红书强实用字段

---

## 5.4 Director（平台化视觉规划）

### 微信公众号 Director
- 目标：封面 + 文中配图
- 当前结构：`cover + placements`

### 小红书 Director
小红书的重点不是“文中插图”，而是：
- 封面首图
- 多页图文卡片
- 每页一句信息点 / 核心结论 / checklist

### 建议扩展 `visual_plan`

```json
{
  "platform": "xhs",
  "style": "...",
  "cover": {...},
  "slides": [
    {"id": "slide_001", "role": "cover", "text": "...", "description": "..."},
    {"id": "slide_002", "role": "problem", "text": "...", "description": "..."},
    {"id": "slide_003", "role": "steps", "text": "...", "description": "..."}
  ]
}
```

### 兼容策略
- `wechat` 继续使用 `placements`
- `xhs` 新增 `slides`
- Frontend 根据 `platform` 渲染不同面板

---

## 5.5 Image Gen（图集化）

### 微信
- 生成封面图 + 文中配图

### 小红书
- 生成封面图
- 生成 2~9 页图卡

### 产物建议
- `images/cover.png`
- `images/slide_001.png`
- `images/slide_002.png`
- ...

---

## 5.6 Formatter（真正的平台分叉点）

## 微信 Formatter
保持现状：
- 输出 `formatted.html`

## 小红书 Formatter
不能只输出 HTML，要输出：

### A. 文案包
- `xhs_note.md`
- `xhs_note.txt`

### B. 图卡包
- `xhs_slides.json`
- `images/slide_*.png`

### C. 发布包（核心）
- `xhs_content.json`

建议升级为：

```json
{
  "title": "...",
  "content": "...",
  "tags": ["#AI工具", "#内容创作"],
  "cover_text": "...",
  "first_comment": "...",
  "images": ["images/cover.png", "images/slide_001.png"],
  "platform_meta": {
    "style": "经验贴",
    "word_count": 620
  }
}
```

---

## 5.7 Publisher（XHS 分阶段接入）

### 当前状态
`_publish_xhs()` 目前只做：
- 保存 `xhs_content.json`
- 返回 `local_only`

### 建议阶段

## Phase 1
- 先保留 `local_only`
- 但把 `xhs_content.json` 结构做完整
- 支持用户手动拿去发布

## Phase 2
接入真正发布链：
1. `xiaohongshu-mcp`
2. 或浏览器自动化（登录态复用）
3. 保留 fallback

### 推荐优先级
优先尝试：
- `xiaohongshu-mcp`
- `xhs-toolkit`
- 浏览器自动化方案

这些社区方案普遍说明：
- 小红书正式发布更依赖登录态复用
- 不适合幻想有稳定官方公开 API
- “预览后发布”是更稳的产品路径

---

## 6. 一键“公众号 → 小红书”改写

这是最值得做的功能之一。

## 6.1 不建议直接覆盖原正文
应做成：
- 从现有 `wechat` run 派生一个新的 `xhs` run

## 6.2 新增 run 派生模式

建议增加：

```yaml
source_run_id: run_20260314_xxx
transform_mode: wechat_to_xhs
```

### 支持两种模式

## A. 完整重构模式（默认）
- Scout 读取来源文章
- 重构为小红书立题
- Researcher 整理平台化材料
- Writer-XHS 重写

## B. 快速改写模式
- 跳过 Scout / Researcher
- 直接用已有 `article_edited.md` 进入 `writer-xhs`

### 推荐默认
用 **A. 完整重构模式**，因为小红书不是简单缩短公众号正文，而是平台重构。

---

## 7. UI / API 设计建议

## 7.1 新建 Run 页面
继续保留：
- `platform = wechat / xhs`

## 7.2 Run 详情页
新增按钮：
- `转为小红书版本`

点击后：
- 选择是否复用 research
- 选择是否复用图片
- 选择快速改写 / 完整重构

## 7.3 API 建议
新增：

```text
POST /api/runs/{id}/adapt
```

请求示例：

```json
{
  "target_platform": "xhs",
  "mode": "wechat_to_xhs",
  "reuse_research": true,
  "reuse_images": false,
  "fast": false
}
```

返回：

```json
{
  "ok": true,
  "new_run_id": "run_...",
  "source_run_id": "run_..."
}
```

---

## 8. 新增配置建议

建议新增 `platform_profiles/xhs.yaml` 或等价配置层：

```yaml
xhs:
  title_max_chars: 20
  body_soft_max_chars: 1000
  preferred_paragraph_len: short
  prefer_listicles: true
  require_tags: true
  allow_first_comment: true
  visual_mode: carousel
```

作用：
- Writer / Director / Formatter / Publisher 共用一套平台规则
- 避免在多个 prompt / 多个节点里硬编码

---

## 9. 实施顺序（推荐）

## Phase 1 — 写作与产物打通（MVP）
1. 新增 `writer-xhs.md`
2. 扩展 `article` 结构（tags / cover_text / first_comment）
3. 新增 `formatter-xhs`
4. 升级 `xhs_content.json`

## Phase 2 — 改写入口
5. 新增 `转为小红书版本`
6. 新增 `POST /api/runs/{id}/adapt`
7. 派生新 run，不覆盖原文章

## Phase 3 — 真正发布
8. 对接 `xiaohongshu-mcp` / 浏览器自动化
9. 登录态 / 风控 / 预览发布
10. 小红书发布结果写入 `publish_result`

---

## 10. MVP 建议

如果按最短路径先上线：

### 推荐 MVP
- Scout / Researcher 共用
- Writer-XHS 新增
- Formatter-XHS 新增
- `转为小红书版` 按钮新增
- Publisher-XHS 先增强为 `xhs_content.json` 完整包
- 真正发布后接

这个版本已经可以让用户：

> **一键把公众号文章改写成小红书版，并导出可发布素材包。**

---

## 11. 社区 / 竞品参考信号（非规范来源）

以下项目提供了有价值的模式参考：

1. `xhs_ai_publisher`
   - 登录态复用
   - 预览后发布
   - 模板系统
   - 本地用户/环境隔离

2. `xiaohongshu-mcp`
   - 登录/搜索/推荐/图文发布
   - 明确平台规则：标题长度、正文字数、登录态约束

3. `AI-Media2Doc`
   - 一份源材料 → 多平台风格改写
   - 证明“公众号 → 小红书”式平台改写是合理的产品方向

这些信号共同说明：
- 小红书发布链更适合“登录态 + 浏览器/MCP”方案
- 多平台风格改写是可行且高价值的功能
- 平台差异主要集中在 Writer / Director / Formatter / Publisher，不必重造前两节点

---

## 12. 最终判断

ContentPipe 做小红书适配的最优路线是：

> **一条共享前链（Scout / Researcher） + 平台化后链（Writer / Director / Formatter / Publisher） + 派生 run 改写入口**

这比造一条完全独立的小红书 pipeline 更省维护，也更符合你现有架构。
