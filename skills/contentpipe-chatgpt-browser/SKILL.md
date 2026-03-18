---
name: contentpipe-chatgpt-browser
description: 通过 OpenClaw 浏览器插件操作 ChatGPT 进行 DALL-E 图片生成与下载。ContentPipe image_gen 节点的核心 skill。
---

# ContentPipe ChatGPT Browser

通过 OpenClaw 浏览器插件（Chrome Relay + CDP）操作 ChatGPT，实现对话、图片生成、深度研究、图片下载等功能。

## 前提条件

- Chrome Relay 已连接（`browser action=tabs profile=chrome` 返回 tab 数据）
- 如未连接，运行 relay 激活脚本（全自动，无需手动操作）：
  ```bash
  cd ~/work/openclawWS/clawdbot_workspace/skills/browser-relay-activator
  bash scripts/connect.sh
  ```
- 需要已登录 ChatGPT Plus 账号
- URL: `https://chatgpt.com/`

## 账号信息

- **账号**: 孙世攀 (Plus)
- **当前默认模型**: ChatGPT 5.4 Thinking
- **URL**: `https://chatgpt.com/`

## 核心原则

**所有操作通过浏览器插件完成**，保持 Relay ON 状态：

| 操作类型 | 方法 |
|---------|------|
| 页面导航 | `browser(action=navigate)` |
| 读取页面 | `browser(action=snapshot)` |
| 截图 | `browser(action=screenshot)` |
| 点击按钮 | `browser(action=act, kind=click)` 或 `evaluate` |
| 输入文字 | `evaluate` + `contenteditable` 元素 |
| 上传文件 | `browser(action=upload, selector='input[type=file]')` |
| 下载图片 | `evaluate(fetch)` 取 URL + cookies → WSL `curl` 下载 |
| 执行 JS | `browser(action=act, kind=evaluate)` |

## 功能列表

### 1. 💬 普通对话

**输入方式**（必须用 evaluate，snapshot ref 不可靠）：
```javascript
// 聚焦输入框
(function(){ var t = document.querySelector('[contenteditable]'); if(t){t.focus(); return 'ok'} return 'not found'})()

// 输入文字
(function(){ var t = document.querySelector('[contenteditable]'); t.focus(); t.textContent='你的问题'; t.dispatchEvent(new Event('input',{bubbles:true})); return 'typed'})()
```

**发送方式**：
```javascript
// 点击发送按钮
(function(){ var btn = document.querySelector('button[aria-label="发送提示"]'); if(btn){btn.click(); return 'sent'} return 'not found'})()
```

**等待回复完成**：等发送按钮重新出现，或 "停止回复" 按钮消失。

### 2. 🎨 图片生成

**入口**: 导航到 `https://chatgpt.com/images` 或侧边栏「图片」

**图片页面特性**：
- 专用输入框："描述新图片"
- 20+ 风格模板（漫画风潮、繁花之躯、鎏金塑像、蜡笔画风、水墨画等）
- 历史图片库（"我的图片"）
- 每张图片有下载和分享按钮

**使用方法**：
1. `navigate` 到 `/images`
2. `evaluate` 输入 prompt → 点击发送
3. 等 15-30 秒 → `screenshot` 查看结果

### 3. 📥 图片下载（已验证方案）

**流程**：浏览器 fetch → 导出 cookies → WSL curl 下载

```javascript
// Step 1: 获取所有生成图片的 URL
(async function(){ var imgs = document.querySelectorAll('article img[src*="estuary"]'); var urls = []; var seen = {}; for(var i=0;i<imgs.length;i++){var s=imgs[i].src; if(!seen[s]){seen[s]=true; urls.push(s)}} return JSON.stringify(urls)})()

// Step 2: 获取 cookies
(function(){ return document.cookie })()
```

```bash
# Step 3: WSL curl 下载
curl -sS -o output.png \
  -H "Cookie: <cookies>" \
  "<image_url>" \
  -x http://172.27.112.1:7890  # 代理（如需要）
```

**⚠️ 不要用这些方式下载**（均被 CSP 阻止）：
- `<a download>` + data URL ❌
- `window.open(blobURL)` → 需手动保存 ⚠️
- base64 分块传输 → 浪费 context ⚠️

### 4. 🔍 深度研究（Deep Research）

**入口**: `https://chatgpt.com/deep-research` 或侧边栏「深度研究」

**页面元素**：
- 输入框："获取详细报告"
- 工具栏：深度研究、应用、站点
- 推荐主题（追踪体育经济、比较语言学习等）

**使用方法**：
1. `navigate` 到 `/deep-research`
2. 输入研究主题
3. 等待 3-10 分钟生成报告

### 5. 📁 文件上传

**⚠️ `browser(action=upload)` 在 relay 模式下不可靠**，推荐用 `evaluate` + DataTransfer API：

```javascript
// 方法 1（推荐）：直接注入 File 对象（小文件 < 1MB）
(function(){
  var inp = document.querySelector('input[type=file]:not([accept])');
  if(!inp) return 'no file input';
  var dt = new DataTransfer();
  var f = new File(['文件内容'], 'filename.txt', {type:'text/plain'});
  dt.items.add(f);
  inp.files = dt.files;
  inp.dispatchEvent(new Event('change', {bubbles:true}));
  return 'file set, files.length=' + inp.files.length;
})()
```

```javascript
// 方法 2：上传 Windows 本地文件（先拷贝到 Windows 路径）
// WSL: cp /path/to/file /mnt/c/Users/Administrator/Downloads/file.txt
// 然后用 browser(action=upload) + Windows 路径
browser action=upload profile=chrome targetId=<id> \
  selector="input[type=file]:not([accept])" \
  paths=["C:\\Users\\Administrator\\Downloads\\file.txt"]
// ⚠️ 需要额外 evaluate dispatch change 事件才能生效
```

**ChatGPT 有 3 个 file input**：
- `input[type=file]:not([accept])` — 通用文件（文档、代码等）
- `input[type=file][accept="image/*"]` ×2 — 仅图片

**支持格式**: PDF、图片、代码文件、文本、Office 等

### 6. 🧭 侧边栏导航

| 链接 | URL | 功能 |
|------|-----|------|
| 图片 | `/images` | 图片生成（DALL-E） |
| 应用 | `/apps` | 应用商店 |
| 深度研究 | `/deep-research` | 深度研究报告 |
| Codex | `/codex` | 代码执行环境 |
| GPT | `/gpts` | 自定义 GPT |
| 项目 | 按钮展开 | 项目管理 |

**导航方式**：优先用 `navigate` + 直接 URL，而非 `act(click)` 侧边栏链接。

### 7. 🔄 模型切换

当前模型: **ChatGPT 5.4 Thinking**

可用模型（顶部下拉）：
- **Auto** — 自动选择
- **Instant (5.3)** — 快速回复
- **Thinking (5.4)** — 推理模式（较慢但更准）
- **传统模型** → GPT-5.2 Instant / GPT-5.2 Thinking

## 故障排除

### Relay 断连（最常见问题）

ChatGPT 发送消息后 URL 变为 `/c/xxx`，可能导致 relay 断连。

**检测**：`browser(action=tabs)` 返回空 tabs。

**自动重连**：
```bash
cd ~/work/openclawWS/clawdbot_workspace/skills/browser-relay-activator
bash scripts/connect.sh  # ~5-8 秒完成
```

重连后必须重新 `tabs` 获取新 `targetId`。

### Toggle 行为注意

Relay 图标是 toggle 按钮：
- 已 ON → 点击变 OFF（detach）
- 已 OFF → 点击变 ON（attach）

`connect.sh` 内置 badge 颜色检测（红色=ON），会自动判断是否需要第二次点击。

### 输入框定位

**绝对不要**用 snapshot ref 操作 ChatGPT 输入框。必须用 `evaluate`：

```javascript
(function(){ var t = document.querySelector('[contenteditable]'); 
  if(t){t.focus(); t.textContent='内容'; t.dispatchEvent(new Event('input',{bubbles:true})); return 'ok'} 
  return 'not found'})()
```

## 测试记录

### 2026-03-16 全面测试（Relay 模式）

| 功能 | 状态 | 说明 |
|------|------|------|
| Relay 连接 | ✅ | 模板匹配 confidence 1.0，badge 验证通过 |
| 页面加载 | ✅ | chatgpt.com 正常 |
| 账号状态 | ✅ | Plus 账号（孙世攀） |
| 模型识别 | ✅ | ChatGPT 5.4 Thinking |
| 对话发送 | ✅ | evaluate + contenteditable + 发送按钮 |
| 图片生成 | ✅ | /images 页面，"A minimalist logo of a dragon breathing fire" 一次成功 |
| 图片下载 | ✅ | fetch + cookies + curl，242KB/265KB PNG 完整下载 |
| 深度研究 | ✅ | /deep-research 页面正常加载，含 4 个推荐主题 + 深度研究/应用/站点切换器 |
| 文件上传 | ✅ | evaluate + DataTransfer API 注入 File 对象，ChatGPT 成功读取并分析 |
| 侧边栏导航 | ✅ | navigate 直接 URL 方式稳定 |
| Relay 重连 | ✅ | connect.sh 一键重连，badge 颜色验证 |

### 已知限制

1. **snapshot ref 不可靠**：ChatGPT 的 aria ref 经常 timeout，必须用 evaluate
2. **Chrome 阻止自动下载**：`<a download>` 被 CSP 拦截，必须用 curl + cookies
3. **Thinking 模式延迟**：5.4 Thinking 回复 30-120 秒
4. **深度研究时间**：3-10 分钟，不要中途打断
5. **上传方式限制**：`browser(action=upload)` 在 relay 模式可能不触发 React 状态更新，优先用 evaluate + DataTransfer API 直接注入 File 对象
6. **大文件上传**：DataTransfer 方式不适合大文件（>1MB），大文件需拷贝到 Windows 路径后用 upload + change 事件
