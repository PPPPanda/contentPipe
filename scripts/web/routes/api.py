"""
ContentPipe Plugin — REST API 路由

提供 JSON API 供 Web UI、Discord 按钮、AI 工具调用。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from gateway_auth import build_contentpipe_session_key
from logutil import get_logger

from web.run_manager import (
    list_runs, get_run, create_run, update_run_state, delete_run,
    get_node_output, get_node_input, get_run_image_path,
    get_run_artifact, load_settings, save_settings,
    PIPELINE_NODES, _load_raw_state, _save_state,
)
from web.events import event_bus, emit_node_start, emit_node_complete, emit_run_complete

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_PID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
logger = get_logger(__name__)


# ── 健康检查 + 插件信息 ──────────────────────────────────────

@router.get("/health")
async def api_health():
    """健康检查端点（插件清单中引用）"""
    from web.run_manager import list_runs
    runs = list_runs()
    active = [r for r in runs if r.get("status") in ("running", "review")]
    return {
        "status": "healthy",
        "plugin": "content-pipeline",
        "version": "0.7.0",
        "total_runs": len(runs),
        "active_runs": len(active),
    }


@router.get("/info")
async def api_plugin_info():
    """插件元信息"""
    return {
        "id": "content-pipeline",
        "name": "ContentPipe",
        "version": "0.7.0",
        "description": "AI 图文内容生产线",
        "web_ui": True,
        "discord_notify": True,
    }


# ── Pipeline 管理 ─────────────────────────────────────────────

@router.get("/runs")
async def api_list_runs():
    """列出所有 Run（JSON）"""
    runs = list_runs()
    return [
        {
            "run_id": r["run_id"],
            "status": r["status"],
            "current_stage": r.get("current_stage", ""),
            "platform": r.get("platform", ""),
            "title": r.get("_title", ""),
            "progress": r.get("_progress", 0),
            "created_at": r.get("created_at", ""),
        }
        for r in runs
    ]


@router.get("/runs/sidebar", response_class=HTMLResponse)
async def api_runs_sidebar():
    """侧边栏最近运行（HTML 片段供 HTMX）"""
    runs = list_runs()[:8]
    status_icons = {"completed": "✅", "running": "🔵", "review": "⏸", "failed": "❌", "pending": "⏳", "unknown": "❓", "cancelled": "🚫"}
    items = []
    for r in runs:
        icon = status_icons.get(r["status"], "❓")
        title = (r.get("_title", "") or r["run_id"])[:20]
        items.append(
            f'<a href="/runs/{r["run_id"]}" class="nav-item flex items-center gap-2 px-3 py-1.5 rounded text-xs text-slate-400 hover:text-white">'
            f'<span>{icon}</span> {title}'
            f'</a>'
        )
    return "\n".join(items) if items else '<p class="px-3 py-2 text-xs text-slate-600">暂无运行</p>'


@router.post("/runs")
async def api_create_run(request: Request, background_tasks: BackgroundTasks):
    """新建 Run"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    platform = body.get("platform", "wechat")
    topic = body.get("topic", "")
    auto_approve = body.get("auto_approve", False)

    run = create_run(platform=platform, topic=topic, auto_approve=auto_approve)
    return run


@router.get("/runs/{run_id}")
async def api_get_run(run_id: str):
    """获取 Run 详情"""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.delete("/runs/{run_id}")
async def api_delete_run(run_id: str):
    """删除 Run"""
    if delete_run(run_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Run not found")


@router.post("/runs/{run_id}/delete")
async def api_delete_run_post(run_id: str):
    """删除 Run (POST 兼容，供 HTMX 使用)"""
    if delete_run(run_id):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Run not found")


@router.post("/runs/{run_id}/start")
async def api_start_run(run_id: str, background_tasks: BackgroundTasks):
    """启动/恢复 Pipeline"""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    background_tasks.add_task(_execute_pipeline, run_id)
    return {"ok": True, "message": "Pipeline started"}


@router.post("/runs/{run_id}/cancel")
async def api_cancel_run(run_id: str):
    """取消 Run"""
    run = update_run_state(run_id, {"status": "cancelled"})
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"ok": True}


@router.get("/runs/{run_id}/article")
async def api_get_article(run_id: str):
    """获取当前文章内容"""
    from web.run_manager import _load_raw_state
    state = _load_raw_state(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")
    content = state.get("article_edited", "")
    if not content:
        content = state.get("article", {}).get("content", "") if isinstance(state.get("article"), dict) else ""
    return {"content": content, "word_count": len(content)}


@router.post("/runs/{run_id}/article")
async def api_save_article(run_id: str, body: dict):
    """保存用户编辑的文章（以用户改的为准）"""
    from web.run_manager import _load_raw_state, _save_state
    from nodes import _save_artifact
    content = body.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="Content is empty")

    state = _load_raw_state(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    # 更新 article_edited（下游节点读这个）
    state["article_edited"] = content
    # 同步更新 article 的 word_count
    if "article" in state and isinstance(state["article"], dict):
        state["article"]["word_count"] = len(content)
    _save_state(state)
    _save_artifact(run_id, "article_edited.md", content)
    return {"ok": True, "word_count": len(content)}


# ── 节点数据 ──────────────────────────────────────────────────

@router.get("/runs/{run_id}/nodes/{node_id}/output", response_class=HTMLResponse)
async def api_node_output(request: Request, run_id: str, node_id: str):
    """获取节点输出（返回 HTML 片段供 HTMX 嵌入）"""
    output = get_node_output(run_id, node_id)
    return templates.TemplateResponse("partials/node_output.html", {
        "request": request,
        "node_id": node_id,
        "output": output,
    })


@router.get("/runs/{run_id}/nodes/{node_id}/input", response_class=HTMLResponse)
async def api_node_input(request: Request, run_id: str, node_id: str):
    """获取节点输入"""
    input_data = get_node_input(run_id, node_id)
    return templates.TemplateResponse("partials/node_output.html", {
        "request": request,
        "node_id": node_id,
        "output": input_data,
    })


# ── 人工审核 ──────────────────────────────────────────────────

@router.post("/runs/{run_id}/auto-skip")
async def api_auto_skip(request: Request, run_id: str):
    """切换单个节点的自动跳过（不暂停审核）"""
    data = await request.json()
    node_id = data.get("node_id", "")
    skip = data.get("skip", False)

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    auto_skip = raw.setdefault("auto_skip_nodes", {})
    auto_skip[node_id] = bool(skip)
    _save_state(raw)
    return {"ok": True, "node_id": node_id, "skip": skip}


@router.post("/runs/{run_id}/review")
async def api_submit_review(request: Request, run_id: str, background_tasks: BackgroundTasks):
    """提交审核结果"""
    form = await request.form()
    action = form.get("action", "approve")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    if action == "approve":
        raw["review_action"] = "approve"
        raw["user_feedback"] = {}

        # ── 审核对话同步：将聊天讨论的结论写回 state ──
        current_node = raw.get("current_stage", "")
        if current_node:
            try:
                await _sync_chat_to_state(run_id, current_node, raw)
            except Exception as e:
                logger.warning("Chat sync failed for %s: %s", current_node, e)
    elif action == "revise":
        feedback = form.get("feedback", "")
        raw["review_action"] = "revise"
        raw["user_feedback"] = {"action": "revise", "global_note": feedback}
    elif action == "select":
        # 图片选择
        selections = {}
        for key, value in form.items():
            if key.startswith("select_"):
                pid = key.replace("select_", "")
                selections[pid] = value
        raw["selected_images"] = selections
        raw["review_action"] = "approve"

    _save_state(raw)

    # 恢复 Pipeline 执行
    background_tasks.add_task(_execute_pipeline, run_id)

    # 重定向到 Run 详情页
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0;url=/runs/{run_id}">',
        headers={"HX-Redirect": f"/runs/{run_id}"},
    )


# ── 预览 ──────────────────────────────────────────────────────

# ── 审核聊天 ──────────────────────────────────────────────────

@router.get("/runs/{run_id}/chat/history")
async def api_chat_history(run_id: str, node: str = ""):
    """获取节点聊天历史（仅返回前端可见消息）"""
    from web.run_manager import get_chat_history_visible
    raw = _load_raw_state(run_id)
    node = node or (raw.get("current_stage", "") if raw else "")
    messages = get_chat_history_visible(run_id, node)
    return messages


@router.post("/runs/{run_id}/chat")
async def api_chat(request: Request, run_id: str):
    """节点聊天 — 用户与当前节点 AI 对话"""
    body = await request.json()
    user_msg = body.get("message", "").strip()
    node_id = body.get("node", "")
    if not user_msg:
        raise HTTPException(status_code=400, detail="Empty message")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    node_id = node_id or raw.get("current_stage", "")

    from web.run_manager import get_chat_history, save_chat_message
    from tools import call_llm, load_pipeline_config

    history = get_chat_history(run_id, node_id)
    save_chat_message(run_id, node_id, "user", user_msg)

    # ── 自动工具调用：检测消息中的 URL 和搜索意图 ──
    import re as _re
    from tools import is_wechat_url, fetch_wechat_article, search_web, search_social, fetch_url

    enriched_parts = []

    # 1) 微信链接自动提取
    wechat_urls = _re.findall(r'https?://mp\.weixin\.qq\.com/s/\S+', user_msg)
    for url in wechat_urls:
        url = url.rstrip(",.;，。；")
        if is_wechat_url(url):
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda u=url: fetch_wechat_article(u))
                if result.get("success"):
                    enriched_parts.append(f"[已读取微信文章: {result.get('title','')}]\n{result.get('content','')[:3000]}")
            except Exception:
                pass

    # 2) 其他 URL 自动抓取
    other_urls = _re.findall(r'https?://(?!mp\.weixin\.qq\.com)\S+', user_msg)
    for url in other_urls[:3]:  # 最多 3 个
        url = url.rstrip(",.;，。；")
        try:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, lambda u=url: fetch_url(u, max_chars=3000))
            if text:
                enriched_parts.append(f"[已读取网页: {url[:60]}]\n{text[:2000]}")
        except Exception:
            pass

    # 3) 搜索意图检测 — 用户说"搜一下/查一查/帮我搜"
    search_triggers = _re.search(r'(?:搜一下|查一查|帮我搜|search for|look up|搜索)\s*[：:]?\s*(.+)', user_msg, _re.I)
    if search_triggers:
        query = search_triggers.group(1).strip()[:60]
        try:
            loop = asyncio.get_event_loop()
            web_res = await loop.run_in_executor(None, lambda q=query: search_web(q, count=5))
            social_res = await loop.run_in_executor(None, lambda q=query: search_social(q, platforms=["twitter", "xiaohongshu"]))
            if web_res:
                enriched_parts.append(f"[网络搜索: {query}]\n" + "\n".join(
                    f"- {r['title']}: {r.get('description','')[:80]}" for r in web_res[:5]
                ))
            for plat, items in social_res.items():
                if items:
                    enriched_parts.append(f"[{plat} 搜索: {query}]\n" + "\n".join(
                        f"- {r.get('title','')[:60]} ({r.get('author','')})" for r in items[:5]
                    ))
        except Exception:
            pass

    if enriched_parts:
        user_msg = user_msg + "\n\n" + "\n\n".join(enriched_parts)

    # 工具结果写入 session（internal=True，前端不可见，但 LLM 能看到）
    if enriched_parts:
        save_chat_message(run_id, node_id, "user",
                          "[自动工具结果]\n" + "\n\n".join(enriched_parts),
                          tag="auto_tool", internal=True)

    # 构建节点专属 system prompt
    system_prompt = _build_node_chat_prompt(node_id, raw)

    # 使用该节点的完整 history（含 internal，LLM 需要完整上下文）
    full_history = get_chat_history(run_id, node_id)
    recent = [{"role": m["role"], "content": m["content"]} for m in full_history[-20:]]

    # 审核聊天用该节点配置的同一个 model（保持写作风格一致）
    from nodes import _get_model
    chat_model = _get_model(node_id) or "dashscope/qwen3.5-plus"
    gateway_agent_id = load_pipeline_config().get("pipeline", {}).get("gateway_agent_id")

    try:
        loop = asyncio.get_event_loop()
        ai_reply = await loop.run_in_executor(
            None,
            lambda: call_llm(
                system_prompt,
                user_msg,
                model=chat_model,
                chat_history=recent,
                system_prompt=system_prompt,
                gateway_session_key=build_contentpipe_session_key(run_id, node_id, "main"),
                gateway_agent_id=gateway_agent_id,
            )
        )
    except Exception as e:
        ai_reply = f"[AI 回复失败: {str(e)[:100]}]"

    # AI 回复：前端可见（internal=False）
    save_chat_message(run_id, node_id, "assistant", ai_reply, tag="user_chat")

    # ── 每次 AI 回复后：检查是否需要同步 ──
    state_updated = False
    try:
        import copy
        import hashlib
        old_snapshot = {
            "topic": copy.deepcopy(raw.get("topic", {})),
            "visual_plan": copy.deepcopy(raw.get("visual_plan", {})),
            "article_edited": raw.get("article_edited", ""),
            "writer_brief": copy.deepcopy(raw.get("writer_brief", {})),
            "writer_packet": copy.deepcopy(raw.get("writer_packet", {})),
        }
        old_hash = hashlib.sha256(
            json.dumps(old_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        await _sync_chat_to_state(run_id, node_id, raw)
        # 重新读取 state（_sync 可能写了磁盘）
        refreshed = _load_raw_state(run_id) or raw
        new_snapshot = {
            "topic": refreshed.get("topic", {}),
            "visual_plan": refreshed.get("visual_plan", {}),
            "article_edited": refreshed.get("article_edited", ""),
            "writer_brief": refreshed.get("writer_brief", {}),
            "writer_packet": refreshed.get("writer_packet", {}),
        }
        new_hash = hashlib.sha256(
            json.dumps(new_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        state_updated = old_hash != new_hash
        if state_updated:
            logger.info("State updated after chat sync for %s", node_id)
    except Exception as e:
        logger.warning("Post-reply sync failed: %s", e)

    return {"role": "assistant", "content": ai_reply, "state_updated": state_updated}


@router.post("/runs/{run_id}/nodes/{node_id}/rerun")
async def api_rerun_node(request: Request, run_id: str, node_id: str, background_tasks: BackgroundTasks):
    """丢弃当前节点成果与 session，回退到上一个可审核节点继续聊天修改。"""
    wants_html = not request.headers.get("content-type", "").startswith("application/json")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    interactive_nodes = ["scout", "researcher", "writer", "director", "formatter"]
    if node_id not in interactive_nodes:
        raise HTTPException(status_code=400, detail=f"Node does not support rollback: {node_id}")

    idx = interactive_nodes.index(node_id)
    if idx == 0:
        raise HTTPException(status_code=400, detail="Current node has no previous review node")

    prev_node = interactive_nodes[idx - 1]

    # 1) 丢弃当前节点 session
    run_dir = Path(__file__).parent.parent.parent / "output" / "runs" / run_id
    chat_file = run_dir / f"chat_{node_id}.json"
    if chat_file.exists():
        chat_file.unlink()

    # 2) 丢弃当前节点的 state / 产物（只清当前节点，不动上一个节点）
    state_cleanup = {
        "scout": ["topic", "writer_brief", "handoff_to_researcher", "reference_articles", "user_requirements", "reference_index", "link_usage_policy", "scout_process_summary"],
        "researcher": ["research", "writer_packet", "verification_results", "evidence_backed_insights", "open_issues"],
        "writer": ["article", "article_edited", "writer_context"],
        "director": ["visual_plan", "image_candidates", "selected_images"],
        "formatter": ["formatted_html"],
    }
    artifact_cleanup = {
        "scout": ["topic.yaml", "scout_raw.txt"],
        "researcher": ["research.yaml", "researcher_raw.txt"],
        "writer": ["writer_context.yaml", "article_draft.md", "article_edited.md"],
        "director": ["director_raw.txt", "visual_plan.json", "director_refine_raw.txt", "image_candidates.json"],
        "formatter": ["formatted.html", "content_body.html"],
    }

    for key in state_cleanup.get(node_id, []):
        raw.pop(key, None)

    # formatter 回退到 director 时，保留 visual_plan，但清空 formatter 成果；
    # director 回退到 writer 时，也清掉已经生成/选择的图片，避免后续误用旧图。
    if node_id == "director":
        raw.pop("generated_images", None)
        images_dir = run_dir / "images"
        if images_dir.exists():
            import shutil
            shutil.rmtree(images_dir, ignore_errors=True)

    for filename in artifact_cleanup.get(node_id, []):
        path = run_dir / filename
        if path.exists():
            path.unlink()

    # 3) 回退到上一个可审核节点，保留其已有状态与聊天，继续聊天修改
    raw["current_stage"] = prev_node
    raw["status"] = "review"
    raw["_node_done"] = True  # 审批时跳过重新执行上一个节点，直接往后跑
    raw["review_action"] = ""
    raw.pop("user_feedback", None)
    _save_state(raw)

    redirect_to = f"/runs/{run_id}/review?node={prev_node}"
    if wants_html:
        return HTMLResponse(
            f'<meta http-equiv="refresh" content="0;url={redirect_to}">',
            headers={"HX-Redirect": redirect_to},
        )

    return {
        "ok": True,
        "message": f"Discarded {node_id} and rolled back to {prev_node}",
        "redirect_to": redirect_to,
        "prev_node": prev_node,
    }


def _build_node_chat_prompt(node_id: str, state: dict) -> str:
    """为每个节点生成专属聊天 system prompt（含执行 context）"""
    topic = state.get("topic", {})
    article = state.get("article", {})
    article_edited = state.get("article_edited", "")
    visual_plan = state.get("visual_plan", {})
    research = state.get("research", {})
    node_ctx = state.get("_node_context", {}).get(node_id, {})

    # 格式化节点执行 context 摘要
    def _fmt_ctx(ctx: dict) -> str:
        if not ctx:
            return ""
        lines = ["\n执行时获取的数据源:"]
        if ctx.get("hotnews_summary"):
            lines.append(f"- 热搜: {', '.join(f'{k}({v}条)' for k, v in ctx['hotnews_summary'].items() if v)}")
        if ctx.get("web_results_count"):
            lines.append(f"- 网络搜索: {ctx['web_results_count']} 条结果")
        if ctx.get("social_results"):
            lines.append(f"- 社交平台: {', '.join(f'{k}({v}条)' for k, v in ctx['social_results'].items() if v)}")
        if ctx.get("wechat_refs"):
            lines.append(f"- 微信文章: {', '.join(r['title'] for r in ctx['wechat_refs'])}")
        if ctx.get("perplexity_available"):
            lines.append("- Perplexity 深度搜索: ✅")
        return "\n".join(lines)

    exec_ctx = _fmt_ctx(node_ctx)

    prompts = {
        "scout": f"""你是**选题策划 AI**。你刚刚完成了选题分析。

选题结果:
- 标题: {topic.get('title', '未定')}
- 角度: {topic.get('content_angle', topic.get('suggested_angle', '未定'))}
- 核心结论: {topic.get('proposed_thesis', '未定')}
- 摘要: {topic.get('summary', '')[:150]}
{exec_ctx}

Writer Brief:
- 核心信息: {state.get('writer_brief', {}).get('core_message', '未定')}
- 必须覆盖: {', '.join(state.get('writer_brief', {}).get('must_cover', [])[:3])}

交给 Researcher:
- {len(state.get('handoff_to_researcher', {}).get('verification_targets', []))} 条待核查事实
- {len(state.get('handoff_to_researcher', {}).get('research_questions', []))} 个调研问题

你的职责:
- 帮用户分析这个选题的可行性和潜力
- 如果用户给了文章链接，你能直接看到提取的正文内容
- 如果用户说"搜一下 XXX"，系统会自动搜索并把结果附在消息里
- 用户可以让你换一个选题方向、修改角度、调整 Writer 要求
- 讨论目标读者、传播性、差异化角度
- **重要**：用户在对话中提出的任何修改，会在点击「继续」时自动同步到 YAML

保持专业但不啰嗦，用中文回复。""",

        "researcher": f"""你是**深度调研 AI**。你刚完成了选题的调研。

选题: {topic.get('title', '')}
调研摘要: {research.get('executive_summary', research.get('summary', ''))[:300]}
{exec_ctx}

你的职责:
- 帮用户判断数据是否充足、信源是否可靠
- 讨论哪些论点需要更强的数据支撑
- 建议补充哪些案例、数据、专家观点
- 分析竞品文章的调研深度

保持严谨，用中文回复。""",

        "writer": f"""你是**写作 AI**。你刚完成了文章写作和润色（包括去 AI 味处理）。

标题: {article.get('title', '')}
初稿字数: {article.get('word_count', 0)}
润色后字数: {len(article_edited)}
润色后开头: {(article_edited or article.get('content', ''))[:200]}

你的职责:
- 讨论文章整体质量、结构、节奏
- 分析标题的吸引力
- 讨论开头是否能抓住读者
- 指出仍然读起来像 AI 生成的部分
- 建议更自然的表达方式
- 讨论语气、风格调整方向

注意：用户看到的是润色后的最终文章，用户可以直接编辑文章内容。
保持有文笔的专业，用中文回复。""",

        "director": f"""你是**视觉导演 AI**。你刚完成了配图方案设计。

风格: {visual_plan.get('style', '')}
色调: {visual_plan.get('global_tone', '')[:100]}
配图数: {len(visual_plan.get('placements', []))}

你的职责:
- 讨论配图风格是否与文章调性匹配
- 分析每张配图的位置和目的
- 建议更好的视觉方向
- 讨论图片风格的一致性

保持有审美品味，用中文回复。""",

        "formatter": f"""你是**排版预览 AI**。用户正在查看最终排版效果（文章+配图）。

文章标题: {article.get('title', '')}
配图数量: {len(visual_plan.get('placements', []))}
生成图片: {sum(1 for g in state.get('generated_images', []) if g.get('success'))} 张成功

你的职责:
- 帮用户检查排版是否美观
- 讨论配图位置是否合理
- 如果用户想换某张图，记录下来
- 讨论标题/副标题的呈现
- 确认微信公众号兼容性

保持简洁，用中文回复。""",
    }

    return prompts.get(node_id, f"你是 ContentPipe 的 {node_id} 阶段助手。帮用户审核当前阶段的输出。用中文回复。")


# ── 预览 ──────────────────────────────────────────────────────

@router.get("/runs/{run_id}/preview/html")
async def api_preview_html(run_id: str):
    """获取排版后的 HTML"""
    html = get_run_artifact(run_id, "formatted.html")
    if not html:
        return HTMLResponse("<p>排版尚未完成</p>")
    return HTMLResponse(html)


@router.get("/runs/{run_id}/images/{image_name}")
async def api_get_image(run_id: str, image_name: str):
    """获取生成的图片"""
    if not SAFE_NAME_RE.match(image_name):
        raise HTTPException(status_code=400, detail="Invalid image name")
    path = get_run_image_path(run_id, image_name)
    if not path:
        raise HTTPException(status_code=404, detail="Image not found")
    media_type = "image/png"
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif suffix == ".webp":
        media_type = "image/webp"
    elif suffix == ".gif":
        media_type = "image/gif"
    return FileResponse(path, media_type=media_type)


# ── 配置 ──────────────────────────────────────────────────────

# ── 配图操作 API ──────────────────────────────────────────────

@router.post("/runs/{run_id}/images/upload")
async def api_upload_image(run_id: str, request: Request):
    """上传图片（用于替换配图或添加新配图）"""
    from web.run_manager import _load_raw_state, _save_state
    from nodes import _save_artifact
    import shutil

    form = await request.form()
    image = form.get("image")
    message = form.get("message", "")
    placement_id = form.get("placement_id", "")

    if not image:
        raise HTTPException(status_code=400, detail="No image file")

    state = _load_raw_state(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    # 轻量安全校验：不影响正常图片替换体验
    original_name = getattr(image, "filename", "") or "upload.jpg"
    suffix = Path(original_name).suffix.lower() or ".jpg"
    content_type = getattr(image, "content_type", "") or ""
    if suffix not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {suffix}")
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    # 确定保存路径
    from web.run_manager import OUTPUT_DIR
    images_dir = OUTPUT_DIR / "runs" / run_id / "images"
    images_dir.mkdir(exist_ok=True)

    # 如果指定了 placement_id，替换该配图
    if not placement_id:
        existing = list(images_dir.glob("img_*"))
        idx = len(existing) + 1
        placement_id = f"img_{idx:03d}"
    elif not SAFE_PID_RE.match(placement_id):
        raise HTTPException(status_code=400, detail="Invalid placement id")

    # 保存文件
    filename = f"{placement_id}{suffix}"
    filepath = images_dir / filename
    content = await image.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Image too large (max 20MB)")
    filepath.write_bytes(content)

    # 更新 state 中的 generated_images
    generated = state.get("generated_images", [])
    # 移除该 placement_id 的旧记录
    generated = [g for g in generated if g.get("placement_id") != placement_id]
    generated.append({
        "placement_id": placement_id,
        "success": True,
        "file_path": str(filepath),
        "engine": "user_upload",
        "option": None,
    })
    state["generated_images"] = generated
    _save_state(state)

    return {"ok": True, "filename": filename, "path": str(filepath), "placement_id": placement_id}


@router.delete("/runs/{run_id}/placements/{pid}")
async def api_delete_placement(run_id: str, pid: str):
    """删除一个配图位置"""
    from web.run_manager import _load_raw_state, _save_state

    if not SAFE_PID_RE.match(pid):
        raise HTTPException(status_code=400, detail="Invalid placement id")

    state = _load_raw_state(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    # 从 visual_plan 中移除
    vp = state.get("visual_plan", {})
    placements = vp.get("placements", [])
    vp["placements"] = [p for p in placements if p.get("id") != pid]
    state["visual_plan"] = vp

    # 从 generated_images 中移除
    generated = state.get("generated_images", [])
    state["generated_images"] = [g for g in generated if g.get("placement_id") != pid]

    _save_state(state)

    # 删除图片文件
    from web.run_manager import OUTPUT_DIR
    images_dir = OUTPUT_DIR / "runs" / run_id / "images"
    for ext in ["jpg", "png", "jpeg", "webp"]:
        f = images_dir / f"{pid}.{ext}"
        if f.exists():
            f.unlink()

    return {"ok": True, "deleted": pid}


@router.get("/settings")
async def api_get_settings():
    """获取配置"""
    return load_settings()


@router.put("/settings")
async def api_update_settings(request: Request):
    """更新配置"""
    settings = await request.json()
    save_settings(settings)
    return {"ok": True}


@router.post("/settings")
async def api_update_settings_form(request: Request):
    """更新配置（表单提交）"""
    form = await request.form()
    settings = load_settings()

    # 更新 pipeline 配置
    pipeline = settings.setdefault("pipeline", {})
    if form.get("default_model"):
        pipeline["default_model"] = form["default_model"]
    if form.get("image_engine"):
        pipeline["image_engine"] = form["image_engine"]

    # LLM overrides
    overrides = pipeline.setdefault("llm_overrides", {})
    for role in ["scout", "researcher", "writer", "de_ai_editor", "director"]:
        val = form.get(f"model_{role}")
        if val:
            overrides[role] = val

    # WeChat config
    wechat = settings.setdefault("wechat", {})
    if form.get("wechat_app_id"):
        wechat["app_id"] = form["wechat_app_id"]
    if form.get("wechat_app_secret"):
        wechat["app_secret"] = form["wechat_app_secret"]
    if form.get("wechat_author"):
        wechat["author"] = form["wechat_author"]

    save_settings(settings)
    return HTMLResponse(
        '<div class="text-green-400 text-sm mt-2">✅ 设置已保存</div>',
    )


# ── 审核对话同步 ──────────────────────────────────────────────

async def _sync_chat_to_state(run_id: str, node_id: str, state: dict):
    """每次 AI 回复后，检查最新对话是否包含修改意图，有则同步回 state。

    两步策略：
    1. 快速判断（看最新一轮 user+assistant）——是否有修改意图？
    2. 如果有，完整同步（把当前 YAML + 最近对话喂给 LLM 更新）
    """
    from web.run_manager import get_chat_history, _save_state
    from nodes import _save_artifact
    from tools import call_llm, load_pipeline_config
    import yaml as _yaml

    # 节点字段映射 — YAML 类节点
    NODE_STATE_FIELDS = {
        "scout": ["topic", "writer_brief", "handoff_to_researcher", "reference_articles", "user_requirements"],
        "researcher": ["writer_packet", "verification_results", "evidence_backed_insights", "open_issues"],
        "director": ["visual_plan"],
    }
    # 文章类节点 — 同步的是长文本，不是 YAML
    ARTICLE_NODES = {"writer"}

    is_yaml_node = node_id in NODE_STATE_FIELDS
    is_article_node = node_id in ARTICLE_NODES
    if not is_yaml_node and not is_article_node:
        return

    history = get_chat_history(run_id, node_id)
    visible = [m for m in history if not m.get("internal")]
    if len(visible) < 2:
        return

    gateway_agent_id = load_pipeline_config().get("pipeline", {}).get("gateway_agent_id")

    # 取最新一轮对话
    last_user = ""
    last_ai = ""
    for m in reversed(visible):
        if m["role"] == "assistant" and not last_ai:
            last_ai = m["content"][:300]
        elif m["role"] == "user" and not last_user:
            last_user = m["content"][:300]
        if last_user and last_ai:
            break

    if not last_user:
        return

    # ── Step 1: 快速判断是否有修改意图 ──
    loop = asyncio.get_event_loop()

    if is_article_node:
        judge_q = (
            f"问题: 这轮对话中，用户是否明确要求修改文章内容、结构、标题、"
            f"段落、措辞、开头、结尾等？（纯讨论/提问/夸奖 = NO）"
        )
    else:
        judge_q = (
            f"问题: 这轮对话中，用户是否明确要求修改选题方向、角度、标题、要求、"
            f"核查目标、写作指导、配图方案等内容？（纯闲聊/提问/确认 = NO）"
        )

    judge_result = await loop.run_in_executor(
        None,
        lambda: call_llm(
            "你是意图分类器。只输出 YES 或 NO，不要输出任何其他内容。",
            f"用户消息: {last_user}\nAI回复: {last_ai}\n\n{judge_q}\n只输出 YES 或 NO：",
            model="dashscope/qwen3.5-flash",
            max_tokens=10,
            gateway_session_key=build_contentpipe_session_key(run_id, node_id, "judge"),
            gateway_agent_id=gateway_agent_id,
        )
    )

    if "YES" not in judge_result.upper():
        return

    logger.info("Chat sync triggered for %s (modification detected)", node_id)

    recent_chat = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content'][:200]}"
        for m in visible[-6:]
    )

    # ── Step 2a: 文章节点 — 同步文章内容 ──
    if is_article_node:
        article_edited = state.get("article_edited", "")
        if not article_edited:
            article_edited = state.get("article", {}).get("content", "")

        # 文章改写用 Writer 同一个 model（保持风格一致）
        from nodes import _get_model
        writer_model = _get_model("writer") or "dashscope/qwen3.5-plus"
        sync_result = await loop.run_in_executor(
            None,
            lambda: call_llm(
                "你是文章编辑助手。根据用户要求修改文章，只输出修改后的完整 Markdown 文章，不要输出任何解释。",
                f"""用户在审核对话中要求修改文章。请按要求修改。

## 规则
1. 只修改用户明确要求改的部分
2. 保持文章整体结构和其他内容不变
3. 输出完整的修改后文章（Markdown 格式）
4. 不要输出任何解释文字，只输出文章

## 当前文章
{article_edited}

## 最近对话（包含修改要求）
{recent_chat}

输出修改后的完整文章：""",
                model=writer_model,
                max_tokens=8192,
                gateway_session_key=build_contentpipe_session_key(run_id, node_id, "article-sync"),
                gateway_agent_id=gateway_agent_id,
            )
        )

        try:
            from nodes import _strip_code_fence
            new_article = _strip_code_fence(sync_result).strip()
            if len(new_article) > 200:  # 基本合理性检查
                state["article_edited"] = new_article
                _save_state(state)
                _save_artifact(run_id, "article_edited.md", new_article)
                logger.info("Article sync complete (%s chars)", len(new_article))
        except Exception as e:
            logger.warning("Article sync failed: %s", e)
        return

    # ── Step 2b: YAML 节点 — 同步结构化数据 ──
    fields = NODE_STATE_FIELDS[node_id]
    current_data = {}
    for f in fields:
        val = state.get(f)
        if val:
            current_data[f] = val

    sync_result = await loop.run_in_executor(
        None,
        lambda: call_llm(
            "你是数据同步助手，只输出 YAML，不要输出任何其他内容。",
            f"""用户在「{node_id}」节点审核对话中提出了修改。请根据对话更新 YAML。

## 规则
1. 只修改对话中明确要求改的部分
2. 没提到的字段保持原样不动
3. 输出完整 YAML（不是 diff）
4. 只输出 YAML

## 当前数据
```yaml
{_yaml.dump(current_data, allow_unicode=True, default_flow_style=False)}
```

## 最近对话
{recent_chat}

输出更新后的完整 YAML：""",
            model="dashscope/qwen3.5-flash",
            gateway_session_key=build_contentpipe_session_key(run_id, node_id, "yaml-sync"),
            gateway_agent_id=gateway_agent_id,
        )
    )

    try:
        from nodes import _strip_code_fence
        updated = _yaml.safe_load(_strip_code_fence(sync_result))
        if not isinstance(updated, dict):
            return

        changed = False
        for f in fields:
            if f in updated and updated[f] != state.get(f):
                state[f] = updated[f]
                changed = True
                logger.info("Chat sync updated state[%s]", f)

        if changed:
            _save_state(state)
            if node_id == "scout":
                _save_artifact(run_id, "topic.yaml",
                    _yaml.dump({f: state[f] for f in fields if f in state},
                               allow_unicode=True, default_flow_style=False))
            elif node_id == "researcher":
                _save_artifact(run_id, "research.yaml",
                    _yaml.dump({f: state[f] for f in fields if f in state},
                               allow_unicode=True, default_flow_style=False))
            logger.info("Chat sync complete for %s", node_id)
    except Exception as e:
        logger.warning("Chat sync parse failed: %s", e)


# ── 内部函数 ──────────────────────────────────────────────────

async def _execute_pipeline(run_id: str):
    """
    后台执行 Pipeline — 调用真实 LLM 节点

    节点顺序: scout → researcher → writer → de_ai_editor → director
    → image_gen → formatter → publisher

    交互节点（人工模式暂停）: scout, researcher, writer, de_ai_editor, director, formatter
    自动节点: image_gen（每个配图位置生成 1 张）, publisher（发布）
    """
    import sys
    import time
    from pathlib import Path

    # 确保 scripts/ 在 sys.path（给 nodes/tools import 用）
    scripts_dir = Path(__file__).parent.parent.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from web.events import (
        emit_node_start, emit_node_complete, emit_node_error,
        emit_run_complete, emit_review_needed,
    )
    from web.run_manager import _load_raw_state, _save_state, PIPELINE_NODES

    # 真实节点函数映射
    from nodes import (
        scout_node, researcher_node, writer_node,
        director_node,
        image_gen_node, formatter_node,
    )

    NODE_FUNCTIONS = {
        "scout": scout_node,
        "researcher": researcher_node,
        "writer": writer_node,
        "director": director_node,
        "image_gen": image_gen_node,
        "formatter": formatter_node,
    }

    # 可交互节点 — 执行完后暂停让用户讨论
    # Writer 已内置去 AI 味（自动执行），用户在 Writer 节点审核最终文章
    INTERACTIVE_NODES = {
        "scout", "researcher", "writer",
        "director", "formatter",
    }

    # 跳过节点
    STUB_NODES = {"publisher"}

    state = _load_raw_state(run_id)
    if not state:
        emit_node_error(run_id, "pipeline", "Run not found")
        return

    state["status"] = "running"
    _save_state(state)

    node_ids = [n["id"] for n in PIPELINE_NODES]
    start_idx = 0

    current = state.get("current_stage", "")
    if current in node_ids:
        start_idx = node_ids.index(current)
        # 如果当前节点已 completed（从 review 恢复），跳到下一个
        if state.get("_node_done"):
            start_idx += 1
            state.pop("_node_done", None)

    for i in range(start_idx, len(node_ids)):
        node_id = node_ids[i]
        state["current_stage"] = node_id
        _save_state(state)
        emit_node_start(run_id, node_id)

        # ── 真实 LLM 节点 ──
        if node_id in NODE_FUNCTIONS:
            node_fn = NODE_FUNCTIONS[node_id]
            t0 = time.time()
            try:
                state = await asyncio.get_event_loop().run_in_executor(
                    None, node_fn, state
                )
                duration_ms = int((time.time() - t0) * 1000)
                summary = ""
                if node_id == "scout":
                    summary = state.get("topic", {}).get("title", "")[:40]
                elif node_id == "writer":
                    summary = f"{state.get('article', {}).get('word_count', 0)} 字"
                elif node_id == "image_gen":
                    imgs = state.get("generated_images", [])
                    ok = sum(1 for g in imgs if g.get("success"))
                    summary = f"{ok}/{len(imgs)} 张"
                elif node_id == "formatter":
                    summary = f"{len(state.get('formatted_html', ''))} 字符"
                emit_node_complete(run_id, node_id, duration_ms=duration_ms, summary=summary)
            except Exception as e:
                emit_node_error(run_id, node_id, str(e)[:200])
                state["status"] = "failed"
                _save_state(state)
                return

            # ── 交互暂停 ──
            if node_id in INTERACTIVE_NODES:
                # 检查：全局 auto_approve 或 per-node auto_skip
                global_auto = state.get("auto_approve", False)
                node_skip = state.get("auto_skip_nodes", {}).get(node_id, False)
                if not global_auto and not node_skip:
                    state["status"] = "review"
                    state["_node_done"] = True
                    _save_state(state)
                    emit_review_needed(run_id, node_id, node_id)
                    # Discord 通知
                    try:
                        from web.notify import notify_review_needed as _discord_notify
                        title = state.get("topic", {}).get("title", "")
                        await _discord_notify(run_id, node_id, title[:200])
                    except Exception:
                        pass
                    return  # 暂停，等用户 approve
            continue

        # ── Stub 节点 ──
        if node_id in STUB_NODES:
            await asyncio.sleep(0.3)
            state["current_stage"] = node_id
            _save_state(state)
            emit_node_complete(run_id, node_id, duration_ms=100, summary="auto")
            continue

        # ── 未知节点 ──
        emit_node_complete(run_id, node_id, duration_ms=0, summary="skip")

    state["status"] = "completed"
    _save_state(state)
    emit_run_complete(run_id)
    # Discord 完成通知
    try:
        from web.notify import notify_run_complete as _discord_complete
        title = state.get("topic", {}).get("title", "")
        await _discord_complete(run_id, title)
    except Exception:
        pass
