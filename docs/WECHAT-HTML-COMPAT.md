# 微信公众号 HTML 兼容性指南

> 最后更新：2026-03-18
>
> 本文档记录微信公众号编辑器的 HTML 渲染陷阱和 ContentPipe formatter 的应对方案。
> 每次踩坑后更新，避免重复犯错。

---

## 核心原则

微信公众号编辑器**禁止 `<style>`、`<class>`、外部 CSS**，所有样式必须内联 `style="..."`。
此外，编辑器对部分 HTML 标签的渲染行为与浏览器标准不同，需要特别处理。

---

## 已知问题与修复

### 1. `<ul>/<li>` + `<strong>` 断行问题

**现象**：列表项内只加粗部分文字（如 `<li><strong>C</strong>apacity and Role</li>`），
微信编辑器会把 `<strong>` 当成块级元素渲染，导致加粗文字和后续内容断行，
列表项之间出现空行/空 bullet。

**根因**：微信编辑器对 `<li>` 内嵌的 `<strong>` 标签渲染异常，
尤其是 `<strong>` 只包含 1-2 个字符时更明显。

**修复方案**（`formatter.py`）：
- **彻底弃用 `<ul>/<li>/<ol>` 原生列表标签**
- 改用 `<p>` + `padding-left` + `text-indent`（悬挂缩进）模拟列表
- 无序列表：`<span>•</span>` 作为 bullet
- 有序列表：`<span>1.</span>` 作为序号
- 同时对单字母加粗做后处理合并：`<strong>C</strong>apacity` → `<strong>Capacity</strong>`

**示例**：
```html
<!-- ❌ 微信不兼容 -->
<ul>
  <li><strong>C</strong>apacity and Role</li>
</ul>

<!-- ✅ 微信兼容 -->
<p style="padding-left:1.5em;text-indent:-1.2em;">
  <span style="color:#999;margin-right:4px;">•</span>
  <strong>Capacity</strong> and Role
</p>
```

**Commit**: `656c5be`, `fe453c2`

---

### 2. 代码块（` ``` `）不渲染

**现象**：代码块内容换行丢失，多行代码挤成一行。

**根因**：
1. 初版：`markdown_to_wechat_html()` 没有代码块状态机
2. 二版：用 `<pre>` + `\n` 换行，但微信编辑器会**吞掉 `<pre>` 内的 `\n`**，
   替换成空格/`&nbsp;`，`white-space: pre-wrap` 不生效

**修复方案**（`formatter.py`）：
- 新增 `in_code_block` 状态机
- 检测到 ` ``` ` 开始行 → 收集后续行
- 检测到 ` ``` ` 结束行 → 用 `<section><p>` 渲染（**不用 `<pre>`**）
- **换行用 `<br>` 替代 `\n`**（微信不会吞 `<br>`）
- **缩进空格用 `&nbsp;` 保留**
- 代码内容用 `html.escape()` 转义
- 样式：浅灰背景（`#f6f8fa`）、monospace 字体、圆角边框

**示例**：
```html
<section style="background:#f6f8fa;border-radius:8px;padding:14px 16px;
  margin:12px 0;overflow-x:auto;border:1px solid #e1e4e8;">
  <p style="margin:0;font-family:Menlo,Consolas,'Courier New',monospace;
    font-size:13px;line-height:1.6;color:#24292e;">
    第一行代码<br>
    &nbsp;&nbsp;缩进的第二行<br>
    第三行代码</p>
</section>
```

**⚠️ 关键教训**：微信编辑器里 `<pre>` 的 `white-space` 属性**完全不生效**，
必须用 `<br>` 显式换行 + `&nbsp;` 显式缩进。

**Commit**: `9f2d2cf`, `b97c335`

---

### 3. 单字母加粗断行

**现象**：markdown `**C**apacity` 转 HTML 后变成 `<strong>C</strong>apacity`，
微信编辑器把只含 1-2 字符的 `<strong>` 当成独立块元素，C 和 apacity 分两行显示。

**根因**：微信编辑器对短内容 `<strong>` 的块级渲染行为。

**修复方案**（`formatter.py` `_inline_format()`）：
- 后处理正则：`<strong>X</strong>word` → `<strong>Xword</strong>`
- 正则：`r'<strong([^>]*)>(\w{1,2})</strong>(\w+)'`
- 只匹配 1-2 个 word 字符后紧跟 word 字符，不影响正常加粗

**Commit**: `fe453c2`

---

## 安全标签清单

以下标签在微信公众号编辑器中**可安全使用**：

| 标签 | 用途 | 注意事项 |
|------|------|----------|
| `<section>` | 容器/区块 | 替代 `<div>`（微信可能过滤 `<div>`） |
| `<p>` | 段落 | 最稳定的文本容器 |
| `<span>` | 行内样式 | 用于颜色、字号等局部样式 |
| `<strong>` | 加粗 | 避免只包含 1-2 个字符 |
| `<em>` | 斜体 | 正常使用 |
| `<h2>/<h3>` | 标题 | 正常使用 |
| `<img>` | 图片 | 必须内联 style |
| `<a>` | 链接 | 微信会过滤部分属性 |
| `<pre>` | 代码块 | ⚠️ 微信会吞掉 `\n`，建议用 `<p>` + `<br>` 替代 |
| `<code>` | 行内代码 | 正常使用 |
| `<br>` | 换行 | 正常使用 |

以下标签在微信公众号**有兼容问题**：

| 标签 | 问题 |
|------|------|
| `<ul>/<ol>/<li>` | 内嵌 `<strong>` 断行，列表项间出现空行 |
| `<div>` | 可能被过滤或转换 |
| `<table>` | 渲染不稳定，建议用图片替代 |
| `<iframe>` | 完全禁止 |
| `<script>/<style>` | 完全禁止 |

---

## 测试方法

1. ContentPipe 预览页（`/runs/{run_id}/preview`）与微信编辑器渲染**不完全一致**
2. 最终验证必须在**微信公众号编辑器**中粘贴 HTML 后检查
3. 重点检查区域：列表、代码块、加粗、嵌套样式
4. 如发现新的兼容性问题，更新本文档和 `formatter.py`
