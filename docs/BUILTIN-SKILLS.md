# ContentPipe Built-in Skills Plan

> 最后更新：2026-03-11
> 状态：规划中（将逐步内置到 `plugins/content-pipeline/skills/`）

## 1. 设计目标

ContentPipe 的 blank-agent 需要“有列表可查”的能力，但不能因此重新变成高污染大工作区 agent。

因此采用：

- **skills 内置随版本发布**
- **agent 只暴露 allowlist 中需要的 skills**
- **Python pipeline 只保留编排/校验/持久化/发布确定性步骤**

## 2. 内置 skills

### 2.1 已内置（当前仓库已存在）

#### `contentpipe-wechat-reader`
用途：
- 识别公众号文章链接
- 抽取标题、作者、正文、字数
- 供 Scout / Researcher / 聊天窗口引用

#### `contentpipe-url-reader`
用途：
- 读取非微信 URL 的可读正文
- 输出结构化摘要或原文片段
- 供参考文章、背景材料、补充链接使用

#### `contentpipe-web-research`
用途：
- 面向 Scout / Researcher 的通用网络搜索
- 返回结构化结果，避免聊天式搜索污染正式产物

### 2.2 规划中（后续逐步内置）

#### `contentpipe-social-research`
用途：
- 搜索 Twitter/X、小红书、B站等社交平台讨论
- 返回平台、标题、链接、摘要、互动信息

### B. 生成辅助类

#### `contentpipe-style-reference`
用途：
- 读取用户贴入的风格参考链接
- 提取写作风格和结构模式
- 供 Writer / De-AI 聊天窗口使用

### C. 发布类

#### `contentpipe-wechat-draft-publisher`
用途：
- 上传正文图片
- 上传封面永久素材
- 创建微信公众号草稿
- 保存 `media_id` / `thumb_media_id`

#### `contentpipe-wechat-freepublish`
用途：
- 后续正式 publish
- 轮询或接收回调
- 管理 `publish_id` / 发布状态

## 3. 节点到 skill 的推荐映射

### Scout
- `contentpipe-wechat-reader`
- `contentpipe-url-reader`
- `contentpipe-web-research`
- `contentpipe-social-research`

### Researcher
- `contentpipe-wechat-reader`
- `contentpipe-url-reader`
- `contentpipe-web-research`
- `contentpipe-social-research`

### Writer
- `contentpipe-style-reference`
- （必要时）`contentpipe-url-reader`

### De-AI
- `contentpipe-style-reference`

### Publisher
- `contentpipe-wechat-draft-publisher`
- 后续增加 `contentpipe-wechat-freepublish`

## 4. blank-agent 暴露原则

`contentpipe-blank` 不应该默认看到全部 OpenClaw skill。
应该只看到 ContentPipe 必需的内置 skills。

推荐 allowlist：

```json
[
  "contentpipe-wechat-reader",
  "contentpipe-url-reader",
  "contentpipe-web-research",
  "contentpipe-social-research",
  "contentpipe-style-reference",
  "contentpipe-wechat-draft-publisher"
]
```

## 5. 与 Python pipeline 的边界

### Skill 负责
- 阅读
- 搜索
- 外部资料抓取
- 发布动作说明与流程知识

### Python pipeline 负责
- state 读写
- 文件落盘
- validator
- 重试编排
- UI 刷新
- 运行时产物管理

## 6. 当前迁移顺序

1. 在插件 manifest 中声明 `skills/`
2. 建立 `plugins/content-pipeline/skills/`
3. 先实现 `contentpipe-wechat-reader`
4. 再实现 `contentpipe-web-research`
5. 修改 `install-agent` 为写入 agent skills allowlist
6. 逐步把 Scout / Researcher 从 Python 搜索主导迁到 skill 暴露主导
