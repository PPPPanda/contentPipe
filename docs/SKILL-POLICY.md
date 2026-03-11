# ContentPipe Skill Policy

> 最后更新：2026-03-11
> 目标：把 ContentPipe 的能力增强统一收口到 **插件内置 skills**，避免运行时漂移、双源依赖和安全失控。

## 1. 单一真源

ContentPipe 相关 skill 只允许放在：

```text
plugins/content-pipeline/skills/
```

不采用以下双轨方案作为正式依赖：
- `~/.openclaw/skills` 中手工安装的 ContentPipe 专用 skill
- 运行时临时从外部拉取 skill 并直接进入生产链路
- 同一个 ContentPipe capability 同时存在“内置版 + 外置版”两套实现

原则：**仓库版本 = skill 版本 = 插件发布版本**。

## 2. 为什么必须内置

### 2.1 版本一致
- `v0.7.x` 必须对应一组确定的 skills
- 回滚插件版本时，skill 也必须一起回滚
- 文档、README、安装命令、agent allowlist 必须与实际能力一致

### 2.2 可部署
- 外部用户 clone 仓库后，不应再额外猜测该装哪些 ContentPipe skill
- `./start.sh install-agent` 只负责配置 agent，不负责下载不确定来源的运行时 skill

### 2.3 可审计
- 每个 skill 都进入 git
- 每个 skill 都可 code review
- 每个 skill 的 prompt 变化、脚本变化、引用变化都有提交记录

## 3. Skill 安全规则

### 3.1 默认最小暴露
每个 agent 只允许看到它实际需要的 skill。
不得让 `contentpipe-blank` 无限制暴露全局所有 skill。

通过 agent `skills` allowlist 收口，例如：

```json
[
  "contentpipe-wechat-reader",
  "contentpipe-url-reader",
  "contentpipe-web-research",
  "contentpipe-social-research",
  "contentpipe-wechat-draft-publisher"
]
```

### 3.2 技能分层
ContentPipe skill 分三层：

1. **输入增强类**
   - 公众号链接阅读
   - 通用 URL 抽取
   - 搜索 / 社交搜索
2. **生成辅助类**
   - 风格参考读取
   - 素材整合
   - 引用整理
3. **发布类**
   - 微信草稿箱
   - 封面/素材上传
   - 后续正式 publish

### 3.3 禁止隐式外部副作用
skill 不得在未明确要求的情况下：
- 发送公开消息
- 自动正式发布文章
- 修改 OpenClaw 全局配置
- 安装未知来源依赖
- 访问未声明的外部服务

### 3.4 允许的确定性外部动作
以下动作可作为明确职责存在，但必须在 skill 文档里写明：
- 读取网页 / 公众号文章
- 发起搜索
- 上传微信素材
- 新增公众号草稿

### 3.5 输出必须落到正式路径
skill 驱动的结构化产物必须写入：

```text
plugins/content-pipeline/output/runs/<run_id>/
```

不允许再写：
- workspace 根下漂移的 `runs/...`
- agentDir
- 临时不可追踪目录

### 3.6 结构化产物必须过 validator
skill 负责写文件，但**不负责绕过校验**。
所有结构化输出依然必须经过 Python validator：
- `topic.yaml`
- `research.yaml`
- `visual_plan.json`
- `image_candidates.json`
- 后续 `article_draft.md` / `article_edited.md`

## 4. Skill 编写规范

### 4.1 小而专
每个 skill 只解决一个明确问题：
- 一个输入增强 skill
- 一个发布 skill
- 一个特定平台能力 skill

不要做“大一统超 skill”。

### 4.2 只保留 ContentPipe 真正需要的上下文
skill 必须短、专、低污染：
- 只写实际工作流
- 不写产品营销文案
- 不写无关背景故事
- 不把大段参考资料塞进 `SKILL.md`

### 4.3 以文件 contract 为中心
skill 的说明必须优先描述：
- 输入来源是什么
- 输出文件写到哪里
- 输出格式是什么
- 失败时如何反馈给 session

### 4.4 禁止绕过流程
skill 不得擅自：
- 跳过审核节点
- 跳过 validator
- 把中间解释文字当成正式产物
- 直接把失败吞掉并伪装成成功

## 5. 吸收外部优秀 skill 的方式

允许吸收优秀 skill，但只能按以下流程进行：

1. 调研外部 skill / repo
2. 评估安全、依赖、许可证、维护状态
3. vendor 到 `plugins/content-pipeline/skills/`
4. 按 ContentPipe contract 重构
5. 补文档、测试、agent allowlist
6. 跟版本一起发布

**不允许**把外部 skill 直接作为线上生产依赖。

## 6. 安装与发布规则

安装时：
- skill 由插件仓库自带，不另开第二套来源
- `install-agent` 只负责给 agent 写入 skill allowlist 和环境对齐

发布时：
- skill 更新必须进入 changelog / release notes（若影响能力边界或安装方式）
- 若 skill 变更影响 agent routing / install 流程，README 与 ARCHITECTURE 必须同步更新

## 7. 当前决策

当前正式决策为：

> **ContentPipe 所有需要的 skills 都内置在插件仓库中，随版本一起发布；不再采用“仓库内置 + 运行时外装”双轨制。**
