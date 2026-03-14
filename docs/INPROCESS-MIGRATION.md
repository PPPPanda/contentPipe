# ContentPipe In-Process Plugin Migration Plan

> 状态：Phase 0 / Scaffold started
> 目标：把 ContentPipe 从 managed-service Python app 迁移为 OpenClaw in-process plugin

## 0. 结论先行

当前 OpenClaw `api.runtime` **没有直接暴露 LLM 调用接口**，只有：
- `config`
- `system`
- `media`
- `tts`
- `stt`
- `tools`
- `events`
- `state`
- `subagent`
- `channel`

因此，想做到“进程内插件 + 直接 `api.runtime` 调 LLM”，需要二选一：

1. **上游增强 OpenClaw**：新增 `api.runtime.llm`（理想终态）
2. **过渡方案**：in-process plugin 内先通过 `api.runtime.subagent.run()` 驱动会话执行，再逐步替换现有 HTTP Gateway client

本次迁移先按 **方案 2（过渡方案）** 起骨架，避免空谈。

---

## 1. 目标架构

```text
ContentPipe In-Process Plugin (TypeScript)
├─ plugin-inprocess/
│  ├─ package.json
│  ├─ openclaw.plugin.json
│  ├─ index.ts
│  └─ src/
│     ├─ plugin.ts
│     ├─ config.ts
│     ├─ runtime-bridge.ts
│     └─ web-routes.ts
├─ Python managed-service（现网主线，暂保留）
└─ docs/INPROCESS-MIGRATION.md
```

### 最终想达到
- ContentPipe 不再自己维护 Gateway URL / Bearer Token / `/v1/chat/completions`
- 改为进程内插件注册工具 / HTTP routes / 服务
- LLM 调用统一走 OpenClaw runtime bridge
- Web UI 最终也迁入插件 HTTP routes 或嵌入式前端服务

---

## 2. 分阶段计划

## Phase 0 — Scaffold（本次开始）
- [x] 建立 in-process plugin 迁移文档
- [x] 建立 `plugin-inprocess/` 骨架
- [x] 插件注册最小健康路由
- [x] 运行时桥接接口占位（显式说明当前无 `runtime.llm`）
- [x] 输出迁移 TODO 清单

## Phase 1 — Runtime Bridge
- [ ] 把 config/paths/env 读取迁到 TS 插件侧
- [ ] 用 `api.runtime.subagent.run()` 做最小 LLM 调用适配器
- [ ] 验证单轮“给 prompt → 拿回复”闭环
- [ ] 定义 run/session key 映射策略

## Phase 2 — Tool Surface
- [ ] 把 `contentpipe_create/status/list/...` 工具迁入 in-process plugin
- [ ] 保持 API 契约兼容现有前端
- [ ] 建立 TS 侧 run state 读写层

## Phase 3 — Node Execution
- [ ] 迁移 Scout / Researcher / Writer 节点执行主链
- [ ] 迁移 artifact commit 仲裁逻辑
- [ ] 保留文件产物协议（topic.yaml / research.yaml / article_edited.md ...）

## Phase 4 — Web Console
- [ ] 把 FastAPI 管理台逐步迁到插件 HTTP routes
- [ ] 或最少把 Web 后端逻辑从 Python 迁到 TS，前端模板可后迁

## Phase 5 — Cutover
- [ ] 切换主 manifest 到 in-process plugin
- [ ] 移除 managed-service 依赖主链
- [ ] 保留 Python 仅作离线工具 / 迁移兼容层（若还需要）

---

## 3. 当前 blocker / 风险

## 3.1 最大 blocker
OpenClaw 当前 `api.runtime` **没有 `llm` helper**。

因此：
- 不能直接 `api.runtime.llm.chat(...)`
- 需要过渡性桥接层
- 或上游给 OpenClaw 增 runtime.llm

## 3.2 迁移风险
- Python 节点逻辑资产较多（`scripts/nodes.py`）
- Web 控制台是 FastAPI/Jinja2，不是轻量页面
- 直接一次性切换会中断现网能力

## 3.3 为什么仍值得做
- 消除云端 `gateway_url/header/token/localhost` 脆弱性
- 消除 blank-agent 安装/路径共享类坑
- 让 ContentPipe 真正成为 OpenClaw 原生扩展

---

## 4. 本次改动范围

本次只做 **Phase 0 scaffold**：
- 不切掉现有 Python managed-service
- 不改现网入口
- 先让 in-process plugin 代码骨架进入仓库
- 后续逐步替换能力

---

## 5. 迁移判断标准

认为可切主线前，至少满足：
- [ ] in-process 插件能独立完成最小 LLM 调用
- [ ] 能注册并执行 `contentpipe_create/status` 工具
- [ ] 能完成 Scout→Writer 的最小 E2E
- [ ] 现有 Web UI 至少能正常读取新状态层
