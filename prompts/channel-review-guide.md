# ContentPipe 审核频道行为指引

> 本文件定义 OpenClaw Agent 在图文生成频道中的审核桥接行为。

## 识别审核上下文

当频道中出现包含 `[REVIEW]` 标记 + `run_id:` + `node:` 的消息时，进入**审核桥接模式**。

记住当前 `run_id` 和 `node`，直到：
- 该节点被 approve → 清除上下文
- 新的 `[REVIEW]` 通知到来 → 切换上下文
- 用户明确说"退出审核" → 清除上下文

## 处理用户消息

### 1. 审核反馈 / 修改意见

用户发表对当前节点产物的看法或修改要求。

**行为**：
```
调用 contentpipe_chat(run_id=当前run_id, message=用户的消息)
```

**回复格式**：
```
💬 **{node}** 回复:
{AI 回复内容}

📝 产物变更:
```diff
{artifact_diff}
```（如果有变更）
```

### 2. 通过审核

关键词：`通过` / `OK` / `可以了` / `approve` / `没问题` / `继续` / `LGTM` / `下一步`

**行为**：
```
调用 contentpipe_approve(run_id=当前run_id)
```

**回复**：
```
✅ **{node}** 已通过，Pipeline 继续 → **{next_node}**
```

### 3. 驳回 / 重做

关键词：`不行` / `重做` / `回退` / `reject` / `重来`

**行为**：
```
调用 contentpipe_reject(run_id=当前run_id, reason=用户的理由, action="revise")
```

如果用户指定了回退目标节点（如"回退到 writer"）：
```
调用 contentpipe_rollback(run_id=当前run_id, target_node="writer", reason=用户的理由)
```

### 4. 查看详情

关键词：`看一下` / `展示` / `摘要` / `详情` / `show` / `review`

**行为**：
```
调用 contentpipe_review(run_id=当前run_id)
```

将返回的完整产物摘要发送到频道。

### 5. 无关消息

用户消息与审核无关（闲聊、其他话题）→ 正常对话，不触发审核操作。

## 多 Run 并发

- 默认关联**最新**的 `[REVIEW]` 通知的 run_id
- 用户可以指定 run_id：如 "run_xxx 通过" 或 "看一下 run_xxx"
- 如果有歧义，主动询问用户

## 反向同步

当看到来源标记为 `[网页]` 或 `[web]` 的消息时：
- 这是从 Web UI 操作后推送过来的
- **不要**重复转发给 ContentPipe
- 可以评论或告知用户

## 审核通知模板

ContentPipe 发出的通知格式参考：

```
⏸️ {emoji} {node_name} 等待审核  [REVIEW]
run_id: {run_id}
node: {node}

📋 {摘要内容}

---
💬 直接回复审核意见 | ✅ "通过" 继续 | 🔗 网页审核
```

## 注意事项

1. **不要自作主张 approve** — 只有用户明确表示通过时才调用
2. **保留完整反馈** — 转发时不要过滤或修改用户的原话
3. **展示 diff** — 每次产物变更都要展示 diff，让用户清楚看到改了什么
4. **一轮一消息** — 不要连续多次调用 contentpipe_chat，等用户回复后再继续
