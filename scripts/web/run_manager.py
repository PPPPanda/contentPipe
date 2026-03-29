"""
ContentPipe Run Manager — Run 状态文件操作

Web UI 通过此模块读写 Run 状态。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# 路径基于 Skill 根目录
SKILL_ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = SKILL_ROOT / "output"
CONFIG_DIR = SKILL_ROOT / "config"

# Pipeline 节点定义（精简版 — 导演交互完直接生图+排版）
PIPELINE_NODES = [
    {"id": "scout",           "label": "选题监控",   "icon": "🔍"},
    {"id": "researcher",      "label": "深度调研",   "icon": "📚"},
    {"id": "writer",          "label": "写作+润色",  "icon": "✍️"},
    {"id": "director",        "label": "AI 导演",    "icon": "🎬"},
    {"id": "image_gen",       "label": "图片生成",   "icon": "🖼️"},
    {"id": "formatter",       "label": "排版预览",   "icon": "📐"},
    {"id": "publisher",       "label": "发布",       "icon": "📤"},
]


def list_runs() -> list[dict]:
    runs_dir = OUTPUT_DIR / "runs"
    if not runs_dir.exists():
        return []
    runs = []
    for d in sorted(runs_dir.iterdir(), reverse=True):
        state_file = d / "state.yaml"
        if state_file.exists():
            try:
                state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    continue
                # 确保必需字段
                state.setdefault("run_id", d.name)
                state.setdefault("status", "unknown")
                state.setdefault("current_stage", "")
                state.setdefault("platform", "wechat")
                runs.append(_enrich_run(state))
            except Exception:
                pass
    return runs


def get_run(run_id: str) -> dict | None:
    state_file = OUTPUT_DIR / "runs" / run_id / "state.yaml"
    if not state_file.exists():
        return None
    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    return _enrich_run(state)


def create_run(platform: str = "wechat", topic: str = "", auto_approve: bool = False) -> dict:
    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    state = {
        "run_id": run_id,
        "status": "pending",
        "current_stage": "",
        "created_at": datetime.now().isoformat(),
        "platform": platform,
        "auto_approve": auto_approve,
    }
    if topic:
        state["topic"] = {"title": topic}
        state["user_topic"] = topic  # 保留原始输入供 scout 检测链接
    run_dir = OUTPUT_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _save_state(state)
    return _enrich_run(state)


def update_run_state(run_id: str, updates: dict) -> dict | None:
    raw = _load_raw_state(run_id)
    if not raw:
        return None
    raw.update(updates)
    _save_state(raw)
    return _enrich_run(raw)


def delete_run(run_id: str) -> bool:
    import shutil
    run_dir = OUTPUT_DIR / "runs" / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
        return True
    return False


# ── 审核聊天 ──────────────────────────────────────────────────

def get_chat_history(run_id: str, node_id: str = "") -> list[dict]:
    """读取节点 session 历史（每个节点独立 session，节点执行+审核聊天共享）

    返回完整历史（含 internal 消息）。
    用 get_chat_history_visible() 获取前端可展示的消息。
    """
    suffix = f"_{node_id}" if node_id else ""
    chat_file = OUTPUT_DIR / "runs" / run_id / f"chat{suffix}.json"
    if chat_file.exists():
        return json.loads(chat_file.read_text(encoding="utf-8"))
    return []


def get_chat_history_visible(run_id: str, node_id: str = "") -> list[dict]:
    """获取前端可展示的聊天消息（过滤掉 internal 标记的系统消息）"""
    history = get_chat_history(run_id, node_id)
    return [m for m in history if not m.get("internal")]


def save_chat_message(run_id: str, node_id: str, role: str, content: str,
                      tag: str = "", internal: bool = False,
                      attachments: list[dict[str, Any]] | None = None,
                      source: str = ""):
    """追加一条聊天消息到节点 session

    internal=True 的消息不会在前端审核对话框中显示，
    但会作为 LLM chat_history 传递（节点执行上下文、系统提示等）。

    source: 消息来源标记 ("web" | "discord" | "openclaw" | "system" | "")
    """
    suffix = f"_{node_id}" if node_id else ""
    chat_file = OUTPUT_DIR / "runs" / run_id / f"chat{suffix}.json"
    history = get_chat_history(run_id, node_id)
    msg: dict[str, Any] = {
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat(),
    }
    if node_id:
        msg["node"] = node_id
    if tag:
        msg["tag"] = tag
    if internal:
        msg["internal"] = True
    if attachments:
        msg["attachments"] = attachments
    if source:
        msg["source"] = source
    history.append(msg)
    chat_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def get_node_output(run_id: str, node_id: str) -> dict:
    """返回人类可读的节点输出摘要（用于 Web UI 展开面板）"""
    state = _load_raw_state(run_id)
    if not state:
        return {"error": "Run not found"}

    def _scout(s):
        # v2: 多话题候选
        scout_topics = s.get("scout_topics", [])
        selected_topic_id = s.get("selected_topic_id", "")
        t = s.get("topic", {})

        # 搜索执行记录
        search_log = s.get("search_execution_log", {})
        skills_called = search_log.get("skills_called", [])
        search_summary = ""
        if skills_called:
            skill_names = [sc.get("skill", "?") for sc in skills_called]
            total = search_log.get("total_sources_scanned", sum(sc.get("results_count", 0) for sc in skills_called))
            search_summary = f"调用了 {len(skill_names)} 个搜索 skill，共扫描 {total} 条结果"

        if scout_topics and len(scout_topics) >= 1:
            # v2 多话题模式（含单话题时也用此视图以展示完整信息）
            return {
                "_type": "scout_topics",
                "topics": scout_topics,
                "selected_topic_id": selected_topic_id or (scout_topics[0].get("topic_id", "") if scout_topics else ""),
                "search_summary": search_summary,
                "skills_called": skills_called,
                "reference_articles": s.get("reference_articles", []),
                "user_requirements": s.get("user_requirements", {}),
            }

        # v1 单话题 fallback
        wb = s.get("writer_brief", {})
        handoff = s.get("handoff_to_researcher", {})

        refs = t.get("direction_references", t.get("sources", []))
        ref_lines = [f"• {r.get('title', '?')}" for r in refs[:5]] if refs else []

        why = t.get("why_this_topic", [])
        why_text = "\n".join(f"• {w}" for w in why[:4]) if why else "—"

        must_cover = wb.get("must_cover", [])
        cover_text = "\n".join(f"• {m}" for m in must_cover[:5]) if must_cover else "—"

        vt_count = len(handoff.get("verification_targets", []))
        rq_count = len(handoff.get("research_questions", []))

        items = [
            {"label": "📰 选题", "value": t.get("title", "—")},
            {"label": "🎯 切入角度", "value": t.get("content_angle", t.get("suggested_angle", "—"))},
            {"label": "💡 核心结论", "value": t.get("proposed_thesis", "—")},
            {"label": "📝 摘要", "value": t.get("summary", "—")},
        ]

        if why != "—":
            items.append({"label": "❓ 为什么选这个", "value": why_text})
        if cover_text != "—":
            items.append({"label": "✅ Writer 必须覆盖", "value": cover_text})
        if vt_count or rq_count:
            items.append({"label": "🔍 交给 Researcher", "value": f"{vt_count} 条待核查事实, {rq_count} 个调研问题"})
        if ref_lines:
            items.append({"label": "📊 方向参考", "value": "\n".join(ref_lines)})
        if search_summary:
            items.append({"label": "🔍 搜索覆盖", "value": search_summary})

        if t.get("heat_score"):
            items.append({"label": "🔥 热度", "value": f"{t['heat_score']}/100"})
        if t.get("keywords"):
            items.append({"label": "🏷️ 关键词", "value": ", ".join(t["keywords"])})

        return {"_type": "cards", "items": items}

    def _researcher(s):
        r = s.get("research", {})
        wp = s.get("writer_packet", {})
        vr = s.get("verification_results", r.get("verification_results", []))

        # 新 schema 摘要
        summary = ""
        # 尝试从 verification_results 生成摘要
        if vr:
            vr_safe = [v for v in vr if isinstance(v, dict)]
            verified = sum(1 for v in vr_safe if v.get("status") == "verified")
            conflicted = sum(1 for v in vr_safe if v.get("status") == "conflicted")
            insufficient = sum(1 for v in vr_safe if v.get("status") == "insufficient_evidence")
            summary = f"核查 {len(vr_safe)} 条: ✅{verified} 已验证, ⚠️{conflicted} 有争议, ❓{insufficient} 证据不足"

        # 旧 schema fallback
        if not summary:
            summary = r.get("executive_summary", "") or r.get("summary", "") or "—"

        # safe_facts / forbidden
        safe = wp.get("safe_facts", [])
        safe_safe = [f for f in safe if f is not None]
        safe_text = "\n".join(f"✅ {f.get('item', f) if isinstance(f, dict) else f}" for f in safe_safe[:5]) if safe_safe else "—"

        forbidden = wp.get("forbidden_claims", [])
        forbidden_safe = [f for f in forbidden if f is not None]
        forbidden_text = "\n".join(f"🚫 {f}" for f in forbidden_safe[:5]) if forbidden_safe else "—"

        # insights
        insights = s.get("evidence_backed_insights", [])
        insights_safe = [i for i in insights if isinstance(i, dict)]
        insight_text = "\n".join(f"💡 {i.get('insight_text', '')[:80]}" for i in insights_safe[:3]) if insights_safe else "—"

        # open issues
        issues = s.get("open_issues", r.get("open_issues", []))
        issues_safe = [o for o in issues if isinstance(o, dict)]
        issue_text = "\n".join(f"⚠️ {o.get('description', '')[:80]}" for o in issues_safe[:3]) if issues_safe else "—"

        items = [
            {"label": "📋 核查摘要", "value": summary[:400]},
            {"label": "✅ 可安全使用的事实", "value": safe_text},
            {"label": "🚫 禁止写入的内容", "value": forbidden_text},
            {"label": "💡 分析角度", "value": insight_text},
        ]
        if issue_text != "—":
            items.append({"label": "⚠️ 未解决问题", "value": issue_text})

        # 兼容旧 schema
        key_findings = r.get("key_findings", [])
        if key_findings and not vr:
            findings_text = "\n".join(f"• {f}" if isinstance(f, str) else f"• {f.get('finding', '')}" for f in key_findings[:5])
            items.append({"label": "💡 核心发现", "value": findings_text})

        return {"_type": "cards", "items": items}

    def _writer(s):
        a = s.get("article", {})
        content = a.get("content", "")
        edited = s.get("article_edited", "")
        final = edited or content
        return {
            "_type": "cards",
            "items": [
                {"label": "📝 标题", "value": a.get("title", "—")},
                {"label": "📊 字数", "value": f"初稿 {len(content)} 字 → 润色后 {len(final)} 字"},
                {"label": "📖 开头", "value": final[:200] + "..." if len(final) > 200 else final},
            ]
        }

    def _de_ai(s):
        # 保留兼容性
        edited = s.get("article_edited", "")
        original = s.get("article", {}).get("content", "")
        return {
            "_type": "cards",
            "items": [
                {"label": "✏️ 编辑后字数", "value": f"{len(edited)} 字"},
                {"label": "📉 变化", "value": f"原文 {len(original)} 字 → 编辑后 {len(edited)} 字"},
                {"label": "📖 编辑后开头", "value": edited[:200] + "..." if len(edited) > 200 else edited},
            ]
        }

    def _director(s):
        vp = s.get("visual_plan", {})
        placements = vp.get("placements", [])
        generated = s.get("generated_images", [])
        run_id = s.get("run_id", "")

        # 构建每张配图的详细卡片
        placement_cards = []
        for i, p in enumerate(placements, 1):
            pid = p.get("id", f"img_{i:03d}")
            section = p.get("after_section", "").lstrip("#").strip()
            purpose = p.get("purpose", "")
            desc = p.get("description", "")[:120]
            aspect = p.get("aspect_ratio", "")
            size = p.get("size_hint", "")

            # 检查是否有已生成的图片
            has_img = any(g.get("placement_id") == pid and g.get("success") for g in generated)
            img_status = "✅ 已生成" if has_img else "⏳ 待生成"

            card_text = f"📍 段落: {section or '未指定'}\n"
            card_text += f"🎯 作用: {purpose}\n"
            card_text += f"📝 描述: {desc}\n"
            card_text += f"📐 比例: {aspect}  尺寸: {size}  {img_status}"

            placement_cards.append({"label": f"🖼️ 配图 {i} ({pid})", "value": card_text})

        cover = vp.get("cover", {})
        items = [
            {"label": "🎨 风格", "value": vp.get("style", "—")},
            {"label": "🌈 色调", "value": (vp.get("global_tone", "—"))[:120]},
            {"label": "🧷 封面设计", "value": (cover.get("description", "—"))[:140]},
            {"label": "🖼️ 配图数", "value": f"{len(placements)} 张"},
        ]
        items.extend(placement_cards)

        return {"_type": "cards", "items": items}

    def _review(s):
        return {
            "_type": "cards",
            "items": [
                {"label": "操作", "value": s.get("review_action", "待审核")},
                {"label": "反馈", "value": str(s.get("user_feedback", "—"))[:200]},
            ]
        }

    def _refine(s):
        candidates = s.get("image_candidates", [])
        lines = []
        for c in candidates[:8]:
            pid = c.get("placement_id", "?")
            opt = c.get("option", "?")
            prompt = c.get("prompt", "")[:60]
            lines.append(f"[{pid}] 方案{opt}: {prompt}")
        return {
            "_type": "cards",
            "items": [
                {"label": "🎯 候选数", "value": f"{len(candidates)} 个"},
                {"label": "📋 候选列表", "value": "\n".join(lines) if lines else "—"},
            ]
        }

    def _image_gen(s):
        imgs = s.get("generated_images", [])
        cover = s.get("generated_cover", {})
        ok = sum(1 for i in imgs if i.get("success"))
        items = [
            {"label": "🖼️ 正文配图", "value": f"{ok}/{len(imgs)} 张成功"},
            {"label": "🧷 封面图", "value": "✅ 已生成" if cover.get("success") else (cover.get("error", "⏳ 待生成")[:80] if isinstance(cover.get("error"), str) else "⏳ 待生成")},
        ]
        for img in imgs:
            pid = img.get("placement_id", "?")
            if img.get("success"):
                t = img.get("generation_time_ms", 0)
                eng = img.get("engine", "?")
                items.append({"label": f"✅ {pid}", "value": f"{eng} · {t/1000:.1f}s"})
            else:
                items.append({"label": f"❌ {pid}", "value": img.get("error", "失败")[:80]})
        return {"_type": "cards", "items": items}

    def _formatter(s):
        html_len = len(s.get("formatted_html", ""))
        imgs = s.get("generated_images", [])
        ok_imgs = sum(1 for i in imgs if i.get("success"))
        items = [
            {"label": "📐 排版", "value": f"{html_len} 字符" if html_len else "未生成"},
            {"label": "🖼️ 配图", "value": f"{ok_imgs} 张已嵌入"},
        ]
        title = s.get("article", {}).get("title", "")
        if title:
            items.insert(0, {"label": "📝 标题", "value": title})
        return {"_type": "cards", "items": items}

    def _publisher(s):
        r = s.get("publish_result", {})
        return {
            "_type": "cards",
            "items": [
                {"label": "📤 平台", "value": r.get("platform", "—")},
                {"label": "📋 状态", "value": r.get("status", "—")},
                {"label": "🔗 链接", "value": r.get("url", r.get("media_id", "—"))},
            ]
        }

    extractors = {
        "scout": _scout, "researcher": _researcher, "writer": _writer,
        "de_ai_editor": _de_ai, "director": _director,
        "image_gen": _image_gen,
        "formatter": _formatter, "publisher": _publisher,
    }
    fn = extractors.get(node_id)
    return fn(state) if fn else {"error": f"Unknown node: {node_id}"}


def get_node_input(run_id: str, node_id: str) -> dict:
    state = _load_raw_state(run_id)
    if not state:
        return {"error": "Run not found"}
    node_input_map = {
        "scout": lambda s: {"platform": s.get("platform", "wechat")},
        "researcher": lambda s: {"topic": s.get("topic", {})},
        "writer": lambda s: {"topic": s.get("topic", {}), "research_summary": (s.get("research", {}).get("executive_summary", ""))[:200]},
        "de_ai_editor": lambda s: {"article_title": s.get("article", {}).get("title", "")},
        "director": lambda s: {"article_title": s.get("article", {}).get("title", ""), "platform": s.get("platform", "")},
        "director_refine": lambda s: {"visual_plan": s.get("visual_plan", {})},
        "image_gen": lambda s: {"candidates_count": len(s.get("image_candidates", []))},
        "formatter": lambda s: {"selected_images": s.get("selected_images", {})},
        "publisher": lambda s: {"platform": s.get("platform", ""), "html_length": len(s.get("formatted_html", ""))},
    }
    extractor = node_input_map.get(node_id)
    return extractor(state) if extractor else {}


def get_run_artifact(run_id: str, filename: str) -> str | None:
    path = OUTPUT_DIR / "runs" / run_id / filename
    return path.read_text(encoding="utf-8") if path.exists() else None


def get_run_image_path(run_id: str, image_name: str) -> Path | None:
    images_dir = OUTPUT_DIR / "runs" / run_id / "images"
    path = images_dir / image_name
    if path.exists():
        return path

    # 兼容：前端常按 .jpg 请求，但用户上传可能是 .png/.webp 等
    req = Path(image_name)
    stem = req.stem
    for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        candidate = images_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return None


def get_dashboard_stats() -> dict:
    runs = list_runs()
    total = len(runs)
    running = sum(1 for r in runs if r.get("status") == "running")
    review = sum(1 for r in runs if r.get("status") == "review")
    completed = sum(1 for r in runs if r.get("status") == "completed")
    failed = sum(1 for r in runs if r.get("status") == "failed")
    pending_reviews = [r for r in runs if r.get("status") == "review"]
    return {
        "total": total, "running": running, "review": review,
        "completed": completed, "failed": failed,
        "pending_reviews": pending_reviews, "recent_runs": runs[:10],
    }


def load_settings() -> dict:
    """加载设置页使用的配置视图：base pipeline.yaml + local pipeline.local.yaml（merged）。"""
    base_path = CONFIG_DIR / "pipeline.yaml"
    config = yaml.safe_load(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    local_path = CONFIG_DIR / "pipeline.local.yaml"
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        config = _deep_merge_copy(config or {}, local)
    return config or {}


def save_settings(settings: dict) -> None:
    """设置页保存统一写入 pipeline.local.yaml，避免覆盖带注释的 base config。"""
    local_path = CONFIG_DIR / "pipeline.local.yaml"
    local_path.write_text(
        yaml.dump(settings, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _deep_merge(base: dict, override: dict) -> None:
    """递归合并 override 到 base，只更新有变化的字段。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _deep_merge_copy(base: dict, override: dict) -> dict:
    result = dict(base or {})
    for k, v in (override or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge_copy(result[k], v)
        else:
            result[k] = v
    return result


# ── 内部函数 ──────────────────────────────────────────────────

def _load_raw_state(run_id: str) -> dict | None:
    import fcntl
    state_file = OUTPUT_DIR / "runs" / run_id / "state.yaml"
    if not state_file.exists():
        return None
    lock_file = OUTPUT_DIR / "runs" / run_id / ".state.lock"
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_SH)  # 共享锁，允许并发读
        try:
            return yaml.safe_load(state_file.read_text(encoding="utf-8"))
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _save_state(state: dict) -> None:
    import fcntl
    run_id = state.get("run_id", "unknown")
    run_dir = OUTPUT_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state_file = run_dir / "state.yaml"
    content = yaml.dump(state, allow_unicode=True, default_flow_style=False)
    # 文件锁防止并发写入（pipeline 后台任务 vs API 请求）
    lock_file = run_dir / ".state.lock"
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            state_file.write_text(content, encoding="utf-8")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _enrich_run(state: dict) -> dict:
    current_stage = state.get("current_stage", "")
    status = state.get("status", "pending")
    node_ids = [n["id"] for n in PIPELINE_NODES]
    current_idx = node_ids.index(current_stage) if current_stage in node_ids else -1

    nodes_status = []
    for i, node in enumerate(PIPELINE_NODES):
        if i < current_idx:
            ns = "completed"
        elif i == current_idx:
            ns = "review" if status == "review" else ("running" if status == "running" else "completed")
        else:
            ns = "pending"
        nodes_status.append({**node, "status": ns})

    title = state.get("topic", {}).get("title", "") or state.get("article", {}).get("title", "") or "未命名"
    state["_title"] = title
    state["_nodes"] = nodes_status
    state["_current_idx"] = current_idx
    state["_progress"] = round((current_idx + 1) / len(PIPELINE_NODES) * 100) if current_idx >= 0 else 0
    return state
