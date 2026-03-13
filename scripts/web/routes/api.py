"""
ContentPipe Plugin — REST API 路由

提供 JSON API 供 Web UI、Discord 按钮、AI 工具调用。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from logutil import get_logger

from web.run_manager import (
    list_runs, get_run, create_run, update_run_state, delete_run,
    get_node_output, get_node_input, get_run_image_path,
    get_run_artifact, load_settings, save_settings,
    PIPELINE_NODES, _load_raw_state, _save_state,
)
from web.events import event_bus, emit_node_start, emit_node_complete, emit_run_complete


def _node_session_key(state: dict, run_id: str, node_id: str, lane: str = "main") -> str:
    """Delegates to nodes._node_session_key (single source of truth)."""
    from nodes import _node_session_key as _nsk
    if "run_id" not in state:
        state["run_id"] = run_id
    return _nsk(state, node_id, lane)


def _read_prompt_text(name: str) -> str:
    prompts_dir = Path(__file__).parent.parent.parent.parent / "prompts"
    path = prompts_dir / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def _extract_tag_block(text: str, tag: str) -> str:
    import re as _re
    m = _re.search(rf"<{tag}>(.*?)</{tag}>", text or "", _re.S | _re.I)
    return (m.group(1).strip() if m else "")


def _node_official_artifact_path(run_id: str, node_id: str) -> Path | None:
    run_dir = Path(__file__).parent.parent.parent.parent / "output" / "runs" / run_id
    mapping = {
        "scout": run_dir / "topic.yaml",
        "researcher": run_dir / "research.yaml",
        "writer": run_dir / "article_edited.md",
        "director": run_dir / "visual_plan.json",
        "formatter": run_dir / "formatted.html",
    }
    return mapping.get(node_id)


def _apply_artifact_to_state_minimally(state: dict, node_id: str, artifact_text: str) -> None:
    import yaml as _yaml

    if node_id == "scout":
        parsed = _yaml.safe_load(artifact_text) or {}
        if not isinstance(parsed, dict):
            raise ValueError("topic.yaml top-level must be a mapping")
        state["topic"] = parsed.get("topic", {}) or {}
        state["writer_brief"] = parsed.get("writer_brief", {}) or {}
        state["handoff_to_researcher"] = parsed.get("handoff_to_researcher", {}) or {}
        state["reference_articles"] = parsed.get("reference_articles", []) or []
        state["user_requirements"] = parsed.get("user_requirements", {}) or {}
        state["reference_index"] = parsed.get("reference_index", {}) or {}
        state["link_usage_policy"] = parsed.get("link_usage_policy", {}) or {}
        state["scout_process_summary"] = parsed.get("scout_process_summary", {}) or {}
        state["current_stage"] = "scout"
        return

    if node_id == "researcher":
        parsed = _yaml.safe_load(artifact_text) or {}
        if not isinstance(parsed, dict):
            raise ValueError("research.yaml top-level must be a mapping")
        state["research"] = parsed
        state["writer_packet"] = parsed.get("writer_packet", {}) or {}
        state["verification_results"] = parsed.get("verification_results", []) or []
        state["topic_support_materials"] = parsed.get("topic_support_materials", {}) or {}
        state["evidence_backed_insights"] = parsed.get("evidence_backed_insights", []) or []
        state["open_issues"] = parsed.get("open_issues", []) or []
        state["current_stage"] = "researcher"
        return

    if node_id == "director":
        parsed = json.loads(artifact_text or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("visual_plan.json top-level must be an object")
        state["visual_plan"] = parsed
        state["current_stage"] = "director"
        return

    if node_id == "formatter":
        state["formatted_html"] = artifact_text
        state["current_stage"] = "formatter"
        return


async def _handle_artifact_review_chat(run_id: str, state: dict, node_id: str, user_msg: str, gateway_agent_id: str | None) -> tuple[str, bool]:
    from tools import call_llm
    from web.run_manager import get_chat_history, save_chat_message, _save_state
    from nodes import _get_model

    artifact_path = _node_official_artifact_path(run_id, node_id)
    if not artifact_path:
        raise ValueError(f"node {node_id} has no official artifact path")

    before_text = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else ""
    system_prompt = _build_node_chat_prompt(node_id, state) + f"""

## 正式产物约束
- 你的正式产物路径是：{artifact_path.as_posix()}
- 当用户明确要求修改当前节点结果时，你可以直接使用 edit 或 write 修改这个正式产物
- 不要修改其他节点的文件
- 不要口头谎报“已更新”；Python 会在你回复后读回文件并决定本轮是否提交成功
"""

    current_artifact_block = before_text[:12000] if before_text else "(当前正式产物为空)"
    user_input = f"""## 当前正式产物
{current_artifact_block}

## 用户本轮消息
{user_msg}

如果用户这轮要求修改当前节点的正式产物，请直接修改该文件；如果只是讨论，可只回复。"""

    full_history = get_chat_history(run_id, node_id)
    recent = [{"role": m["role"], "content": m["content"]} for m in full_history[-20:]]
    chat_model = _get_model(node_id) or "dashscope/qwen3.5-plus"

    loop = asyncio.get_event_loop()
    ai_reply = await loop.run_in_executor(
        None,
        lambda: call_llm(
            system_prompt,
            user_input,
            model=chat_model,
            chat_history=recent,
            gateway_session_key=_node_session_key(state, run_id, node_id, "main"),
            gateway_agent_id=gateway_agent_id,
        )
    )

    after_text = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else ""
    state_updated = after_text != before_text and bool(after_text.strip())
    if not state_updated:
        save_chat_message(run_id, node_id, "assistant", ai_reply, tag="user_chat")
        return ai_reply, False

    try:
        # Save .prev for diff support
        if before_text:
            from nodes import _save_artifact
            _save_artifact(run_id, f"{artifact_path.name}.prev", before_text)
        _apply_artifact_to_state_minimally(state, node_id, after_text)
        _save_state(state)
        logger.info("Artifact review commit accepted for %s", node_id)
        save_chat_message(run_id, node_id, "assistant", ai_reply, tag="user_chat")
        return ai_reply, True
    except Exception as e:
        try:
            artifact_path.write_text(before_text, encoding="utf-8")
        except Exception:
            pass
        logger.warning("Artifact review commit rejected for %s: %s", node_id, e)
        repaired_reply = (ai_reply + f"\n\n（本轮文件修改未被系统接受：{str(e)[:120]}）").strip()
        save_chat_message(run_id, node_id, "assistant", repaired_reply, tag="user_chat")
        return repaired_reply, False


async def _run_writer_structure_helper(run_id: str, state: dict, raw_output: str, current_article: str, gateway_agent_id: str | None) -> dict:
    from tools import call_llm
    import uuid

    prompt = _read_prompt_text("writer-structure.md")
    article_path = Path(__file__).parent.parent.parent.parent / "output" / "runs" / run_id / "article_edited.md"
    context = f"""## 当前正式正文
{current_article}

## Writer 主 session 原始输出
{raw_output}

## 正式正文文件路径
{article_path.as_posix()}
"""

    try:
        loop = asyncio.get_event_loop()
        helper_text = await loop.run_in_executor(
            None,
            lambda: call_llm(
                prompt,
                context,
                model="dashscope/qwen3.5-flash",
                response_format="json",
                max_tokens=1200,
                gateway_session_key=_node_session_key(state, run_id, "writer", f"structure-{uuid.uuid4().hex[:8]}"),
                gateway_agent_id=gateway_agent_id,
            )
        )
        return json.loads(helper_text)
    except Exception:
        return {
            "reply_visible": _extract_tag_block(raw_output, "reply") or raw_output[:240].strip(),
            "should_update_article": bool(_extract_tag_block(raw_output, "article_full")),
            "change_summary": _extract_tag_block(raw_output, "change_summary"),
        }


async def _handle_writer_review_chat(run_id: str, state: dict, user_msg: str, gateway_agent_id: str | None) -> tuple[str, bool, str]:
    from tools import call_llm
    from web.run_manager import get_chat_history, save_chat_message, _save_state
    from nodes import _get_model, _save_artifact

    article_path = Path(__file__).parent.parent.parent.parent / "output" / "runs" / run_id / "article_edited.md"
    if article_path.exists():
        current_article = article_path.read_text(encoding="utf-8").strip()
    else:
        current_article = (state.get("article_edited", "") or state.get("article", {}).get("content", "")).strip()
    article_title = state.get("article", {}).get("title", "") or state.get("topic", {}).get("title", "")

    system_prompt = _read_prompt_text("writer-review.md") + f"""

## 当前状态摘要
- 文章标题: {article_title}
- 当前正式正文长度: {len(current_article)} 字
- 当前正文开头: {current_article[:200]}
"""

    writer_input = f"""## 当前正式正文
{current_article}

## 用户本轮消息
{user_msg}

请按约定输出 `<reply>`，并在需要修改正文时额外输出 `<change_summary>` 与 `<article_full>`。
"""

    full_history = [m for m in get_chat_history(run_id, "writer") if m.get("tag") != "writer_main_raw"]
    recent = [{"role": m["role"], "content": m["content"]} for m in full_history[-20:]]
    writer_model = _get_model("writer") or "openai-codex/gpt-5.4"

    loop = asyncio.get_event_loop()
    raw_output = await loop.run_in_executor(
        None,
        lambda: call_llm(
            system_prompt,
            writer_input,
            model=writer_model,
            chat_history=recent,
            max_tokens=8192,
            gateway_session_key=_node_session_key(state, run_id, "writer", "main"),
            gateway_agent_id=gateway_agent_id,
        )
    )

    save_chat_message(run_id, "writer", "assistant", raw_output, tag="writer_main_raw", internal=True)

    helper = await _run_writer_structure_helper(run_id, state, raw_output, current_article, gateway_agent_id)
    reply_visible = (helper.get("reply_visible") or _extract_tag_block(raw_output, "reply") or "我按你的要求处理了，左侧正文会同步更新。").strip()
    change_summary = (helper.get("change_summary") or _extract_tag_block(raw_output, "change_summary") or "").strip()
    should_update = bool(helper.get("should_update_article"))

    after_article = article_path.read_text(encoding="utf-8").strip() if article_path.exists() else ""
    state_updated = bool(after_article) and after_article != current_article

    if state_updated:
        if current_article:
            _save_artifact(run_id, "article_edited.md.prev", current_article)
        state["article_edited"] = after_article
        if "article" in state and isinstance(state["article"], dict):
            state["article"]["word_count"] = len(after_article)
        _save_state(state)
    elif should_update:
        reply_visible = (reply_visible + "\n\n（本轮正文没有成功提交到正式产物文件，系统未接受这次修改。）").strip()

    _save_artifact(
        run_id,
        "writer_last_exchange.json",
        json.dumps(
            {
                "raw_output": raw_output,
                "reply_visible": reply_visible,
                "change_summary": change_summary,
                "should_update_article": should_update,
                "state_updated": state_updated,
                "_debug": {
                    "current_article_len": len(current_article),
                    "after_article_len": len(after_article),
                    "writer_model": writer_model,
                    "helper_mode": "fresh-structure-session",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
    )

    return reply_visible, state_updated, change_summary


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

    # Save previous version for diff
    current_article = state.get("article_edited", "")
    if current_article and current_article != content:
        _save_artifact(run_id, "article_edited.md.prev", current_article)

    # 更新 article_edited（下游节点读这个）
    state["article_edited"] = content
    # 同步更新 article 的 word_count
    if "article" in state and isinstance(state["article"], dict):
        state["article"]["word_count"] = len(content)
    _save_state(state)
    _save_artifact(run_id, "article_edited.md", content)
    return {"ok": True, "word_count": len(content)}


@router.get("/runs/{run_id}/diff")
async def api_get_diff(run_id: str, node: str | None = None):
    """获取节点改动 diff（当前 vs 上一版本）。

    ?node=scout  → topic.yaml diff
    ?node=writer → article_edited.md diff (default)
    ?node=researcher → research.yaml diff
    ?node=director → visual_plan.json diff
    ?node=formatter → formatted.html diff
    """
    import difflib

    run_dir = Path(__file__).parent.parent.parent.parent / "output" / "runs" / run_id

    # 确定要 diff 的文件
    if node and node != "writer":
        artifact_path = _node_official_artifact_path(run_id, node)
        if not artifact_path:
            return JSONResponse({"error": f"未知节点: {node}"}, status_code=400)
        current_path = artifact_path
        prev_path = run_dir / f"{artifact_path.name}.prev"
        draft_path = None
    else:
        # 默认: writer article
        current_path = run_dir / "article_edited.md"
        prev_path = run_dir / "article_edited.md.prev"
        draft_path = run_dir / "article_draft.md"

    if not current_path.exists():
        return JSONResponse({"error": f"当前产物不存在: {current_path.name}"}, status_code=404)

    # 优先用 .prev，fallback 用 draft（仅 writer）
    if prev_path.exists():
        base_path = prev_path
        from_label = "上一版本"
    elif draft_path and draft_path.exists():
        base_path = draft_path
        from_label = "初稿"
    else:
        return JSONResponse({"diff": None, "has_diff": False, "message": "暂无历史版本"})

    current = current_path.read_text(encoding="utf-8").splitlines(keepends=True)
    previous = base_path.read_text(encoding="utf-8").splitlines(keepends=True)

    diff = difflib.unified_diff(
        previous, current,
        fromfile=from_label,
        tofile="当前版本",
        lineterm=""
    )
    diff_text = "\n".join(diff)
    return {"diff": diff_text, "has_diff": bool(diff_text.strip())}


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

    # 重定向到 Run 详情页（HTMX 兼容）
    wants_html = not request.headers.get("content-type", "").startswith("application/json")
    if request.headers.get("HX-Request"):
        # HTMX 请求：返回带 HX-Redirect 的响应
        return HTMLResponse(
            "",
            headers={"HX-Redirect": f"/runs/{run_id}"},
        )
    # 普通表单提交：meta refresh
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0;url=/runs/{run_id}">',
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
    attachments = body.get("attachments", []) or []
    if not user_msg and not attachments:
        raise HTTPException(status_code=400, detail="Empty message")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    node_id = node_id or raw.get("current_stage", "")

    from web.run_manager import get_chat_history, save_chat_message
    from tools import call_llm, load_pipeline_config

    history = get_chat_history(run_id, node_id)
    display_msg = user_msg or "(附图片)"
    save_chat_message(run_id, node_id, "user", display_msg, attachments=attachments)

    # ── skill-driven 提示：检测 URL / 搜索意图，但不在 Python 里预抓取内容 ──
    import re as _re
    skill_hints = []

    urls = []
    for url in _re.findall(r'https?://\S+', user_msg):
        cleaned = url.rstrip(",.;，。；）)]】」』")
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    if urls:
        skill_hints.append(
            "[技能提示] 用户消息中包含参考链接。请优先使用可见的 contentpipe-wechat-reader / contentpipe-url-reader / contentpipe-style-reference 自行读取和提炼，不要假装系统已经替你抓好了正文。\n"
            + json.dumps(urls[:5], ensure_ascii=False, indent=2)
        )

    if attachments:
        attachment_hint = [
            {
                'type': a.get('type', 'image'),
                'path': a.get('path', ''),
                'filename': a.get('filename', ''),
                'mime': a.get('mime', ''),
                'purpose': a.get('purpose', 'chat_reference'),
            }
            for a in attachments[:5]
        ]
        skill_hints.append(
            "[技能提示] 用户附了图片附件。请把这些附件当作视觉/风格/内容参考，按需使用可见的 contentpipe-style-reference 或相关 skills 进行分析，不要忽略附件。\n"
            + json.dumps(attachment_hint, ensure_ascii=False, indent=2)
        )

    search_triggers = _re.search(r'(?:搜一下|查一查|帮我搜|search for|look up|搜索)\s*[：:]?\s*(.+)', user_msg, _re.I)
    if search_triggers:
        query = search_triggers.group(1).strip()[:100]
        skill_hints.append(
            "[技能提示] 用户明确提出了搜索需求。请优先使用可见的 contentpipe-web-research / contentpipe-social-research 自主完成检索。\n"
            f"建议查询: {query}"
        )

    if skill_hints:
        save_chat_message(
            run_id,
            node_id,
            "user",
            "[skill-driven hints]\n" + "\n\n".join(skill_hints),
            tag="skill_hint",
            internal=True,
        )
        base_msg = user_msg or "(附图片)"
        user_msg = base_msg + "\n\n" + "\n\n".join(skill_hints)

    gateway_agent_id = load_pipeline_config().get("pipeline", {}).get("gateway_agent_id")

    # Writer: 连续主 session + fresh 结构 helper
    if node_id == "writer":
        try:
            ai_reply, state_updated, change_summary = await _handle_writer_review_chat(
                run_id,
                raw,
                user_msg,
                gateway_agent_id,
            )
        except Exception as e:
            ai_reply = f"[AI 回复失败: {str(e)[:100]}]"
            state_updated = False
            change_summary = ""

        save_chat_message(run_id, node_id, "assistant", ai_reply, tag="user_chat")
        return {
            "role": "assistant",
            "content": ai_reply,
            "state_updated": state_updated,
            "change_summary": change_summary,
        }

    # 结构化/预览节点：同 session 直接改正式产物，Python 读回后提交
    if node_id in {"scout", "researcher", "director", "formatter"}:
        try:
            ai_reply, state_updated = await _handle_artifact_review_chat(
                run_id,
                raw,
                node_id,
                user_msg,
                gateway_agent_id,
            )
        except Exception as e:
            ai_reply = f"[AI 回复失败: {str(e)[:100]}]"
            state_updated = False
        return {
            "role": "assistant",
            "content": ai_reply,
            "state_updated": state_updated,
        }

    # 构建节点专属 system prompt
    system_prompt = _build_node_chat_prompt(node_id, raw)

    # 使用该节点的完整 history（含 internal，LLM 需要完整上下文）
    full_history = get_chat_history(run_id, node_id)
    recent = [{"role": m["role"], "content": m["content"]} for m in full_history[-20:]]

    # 审核聊天用该节点配置的同一个 model（保持写作风格一致）
    from nodes import _get_model
    chat_model = _get_model(node_id) or "dashscope/qwen3.5-plus"

    try:
        loop = asyncio.get_event_loop()
        ai_reply = await loop.run_in_executor(
            None,
            lambda: call_llm(
                system_prompt,
                user_msg,
                model=chat_model,
                chat_history=recent,
                gateway_session_key=_node_session_key(raw, run_id, node_id, "main"),
                gateway_agent_id=gateway_agent_id,
            )
        )
    except Exception as e:
        ai_reply = f"[AI 回复失败: {str(e)[:100]}]"

    # AI 回复：前端可见（internal=False）
    save_chat_message(run_id, node_id, "assistant", ai_reply, tag="user_chat")

    return {"role": "assistant", "content": ai_reply, "state_updated": False}


def _rollback_to_review_node(raw: dict, run_id: str, node_id: str) -> tuple[dict, str]:
    """丢弃当前节点成果与 session，回退到上一个可审核节点继续聊天修改。"""
    interactive_nodes = ["scout", "researcher", "writer", "director", "formatter"]
    if node_id not in interactive_nodes:
        raise HTTPException(status_code=400, detail=f"Node does not support rollback: {node_id}")

    idx = interactive_nodes.index(node_id)
    if idx == 0:
        raise HTTPException(status_code=400, detail="Current node has no previous review node")

    prev_node = interactive_nodes[idx - 1]

    run_dir = Path(__file__).parent.parent.parent / "output" / "runs" / run_id
    chat_file = run_dir / f"chat_{node_id}.json"
    if chat_file.exists():
        chat_file.unlink()

    session_gen = raw.get("_session_gen") if isinstance(raw.get("_session_gen"), dict) else {}
    session_gen[node_id] = int(session_gen.get(node_id, 0) or 0) + 1
    if node_id == "writer":
        session_gen["de_ai_editor"] = int(session_gen.get("de_ai_editor", 0) or 0) + 1
    raw["_session_gen"] = session_gen

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

    raw["current_stage"] = prev_node
    raw["status"] = "review"
    raw["_node_done"] = True
    raw["review_action"] = ""
    raw.pop("user_feedback", None)
    _save_state(raw)
    return raw, prev_node


@router.post("/runs/{run_id}/nodes/{node_id}/rerun")
async def api_rerun_node(request: Request, run_id: str, node_id: str, background_tasks: BackgroundTasks):
    wants_html = not request.headers.get("content-type", "").startswith("application/json")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    _, prev_node = _rollback_to_review_node(raw, run_id, node_id)

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


@router.post("/runs/{run_id}/rollback/image-gen-to-director")
async def api_rollback_image_gen_to_director(run_id: str):
    """从图片生成阶段回退到 AI 导演审核态。"""
    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    run_dir = Path(__file__).parent.parent.parent / "output" / "runs" / run_id
    raw["current_stage"] = "director"
    raw["status"] = "review"
    raw["_node_done"] = True
    raw["review_action"] = ""
    raw.pop("user_feedback", None)
    raw.pop("generated_images", None)
    raw.pop("generated_cover", None)
    raw.pop("selected_images", None)

    for name in ["generated_images.json", "generated_cover.json"]:
        path = run_dir / name
        if path.exists():
            path.unlink()
    images_dir = run_dir / "images"
    if images_dir.exists():
        import shutil
        shutil.rmtree(images_dir, ignore_errors=True)

    _save_state(raw)
    return {
        "ok": True,
        "message": "Rolled back from image_gen to director review",
        "redirect_to": f"/runs/{run_id}/review?node=director",
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
        lines = ["\n执行上下文摘要:"]
        if ctx.get("mode"):
            lines.append(f"- 模式: {ctx['mode']}")
        if ctx.get("reference_urls"):
            lines.append(f"- 参考链接: {len(ctx['reference_urls'])} 个")
        if ctx.get("reference_url_count") is not None:
            lines.append(f"- 参考链接: {ctx['reference_url_count']} 个")
        if ctx.get("search_query"):
            lines.append(f"- 主搜索主题: {ctx['search_query']}")
        if ctx.get("social_query"):
            lines.append(f"- 社交搜索主题: {ctx['social_query']}")
        if ctx.get("verification_target_count") is not None:
            lines.append(f"- 待核查断言: {ctx['verification_target_count']} 条")
        if ctx.get("verification_count") is not None:
            lines.append(f"- 已产出核查项: {ctx['verification_count']} 条")
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
- 如果用户给了文章链接或网页链接，请优先使用可见的 contentpipe-* skills 自行读取
- 如果用户说"搜一下 XXX"，请优先使用可见的 research skills 自主检索，不要假装系统已经把结果附上来
- 用户可以让你换一个选题方向、修改角度、调整 Writer 要求
- 讨论目标读者、传播性、差异化角度
- 当用户明确要求修改当前节点结果时，你可以直接修改本节点的正式产物文件
- 不要修改其他节点文件
- 不要口头谎报“已更新”；是否提交成功以 Python 读回正式产物后的结果为准

保持专业但不啰嗦，用中文回复。""",

        "researcher": f"""你是**深度调研 AI**。你刚完成了选题的调研。

选题: {topic.get('title', '')}
调研摘要: {research.get('executive_summary', research.get('summary', ''))[:300]}
{exec_ctx}

你的职责:
- 帮用户判断数据是否充足、信源是否可靠
- 如果用户给了链接或搜索指令，请优先使用可见的 contentpipe-* skills 自主阅读和检索
- 讨论哪些论点需要更强的数据支撑
- 建议补充哪些案例、数据、专家观点
- 分析竞品文章的调研深度
- 当用户明确要求修改当前节点结果时，你可以直接修改本节点的正式产物文件
- 不要修改其他节点文件，也不要谎报“已写入 YAML”

保持严谨，用中文回复。""",

        "writer": f"""你是“微信公众号主笔”子 agent。你刚完成了文章写作和终稿润色。

标题: {article.get('title', '')}
初稿字数: {article.get('word_count', 0)}
润色后字数: {len(article_edited)}
润色后开头: {(article_edited or article.get('content', ''))[:200]}

你的目标是和用户一起把这篇文章打磨成“愿意被读完、被转发、被划线”的公众号文章。

你的职责:
- 讨论文章整体质量、结构、节奏和推进感
- 判断标题是否有信息量和张力，但不过度标题党
- 判断开头是否足够快地把读者拉进来
- 找出仍然偏空、偏套话、偏 AI 味的段落
- 给出更自然、更像成熟作者的表达建议
- 讨论语气、风格、结构、收尾的调整方向
- 如果用户贴了风格参考链接，请优先使用 contentpipe-style-reference 提炼风格，再做调整，不要模仿具体作者

注意：
- 用户看到的是润色后的最终文章，用户可以直接编辑文章内容。
- 保持克制、清醒、有判断，不卖弄，不喊口号。
- 不编造事实，不建议加入没有依据的数据或引用。

用中文回复，风格自然，别写成审稿报告。""",

        "director": f"""你是**视觉导演 AI**。你刚完成了配图方案设计。

风格: {visual_plan.get('style', '')}
色调: {visual_plan.get('global_tone', '')[:100]}
配图数: {len(visual_plan.get('placements', []))}

你的职责:
- 讨论配图风格是否与文章调性匹配
- 分析每张配图的位置和目的
- 建议更好的视觉方向
- 讨论图片风格的一致性
- 当用户明确要求修改当前节点结果时，你可以直接修改本节点的正式产物文件
- 不要修改其他节点文件，也不要谎报“已写入 JSON/YAML”

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
- 当用户明确要求修改当前排版结果时，你可以直接修改本节点的正式产物文件
- 不要修改其他节点文件，也不要谎报“已写入 HTML”

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
    purpose = form.get("purpose", "placement" if placement_id else "chat")

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
    if purpose == "cover":
        filename = f"cover{suffix}"
        # 清理旧封面文件（不同扩展名）
        for ext in ALLOWED_IMAGE_EXTS:
            old = images_dir / f"cover{ext}"
            if old.exists():
                old.unlink()
    else:
        filename = f"{placement_id}{suffix}"
    filepath = images_dir / filename
    content = await image.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Image too large (max 20MB)")
    filepath.write_bytes(content)

    if purpose == "placement":
        # 更新 state 中的 generated_images
        generated = state.get("generated_images", [])
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
    elif purpose == "cover":
        state["generated_cover"] = {
            "success": True,
            "file_path": str(filepath),
            "engine": "user_upload",
            "prompt_used": "",
            "generation_time_ms": 0,
            "error": "",
        }
        _save_state(state)

    return {
        "ok": True,
        "filename": filename,
        "path": str(filepath),
        "placement_id": placement_id,
        "purpose": purpose,
        "mime": content_type or f"image/{suffix.lstrip('.')}"
    }


@router.post("/runs/{run_id}/placements/{pid}/caption")
async def api_save_placement_caption(run_id: str, pid: str, body: dict):
    """保存某个配图位置的 caption（读者可见图注）。"""
    from web.run_manager import _load_raw_state, _save_state
    from nodes import _save_artifact

    if not SAFE_PID_RE.match(pid):
        raise HTTPException(status_code=400, detail="Invalid placement id")

    state = _load_raw_state(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    caption = str((body or {}).get("caption", "")).strip()
    vp = state.get("visual_plan", {})
    prev_vp_text = json.dumps(vp, ensure_ascii=False, indent=2)
    placements = vp.get("placements", []) if isinstance(vp, dict) else []
    found = False
    for p in placements:
        if p.get("id") == pid:
            p["caption"] = caption
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Placement not found")

    # 保存上一版供 diff 使用
    _save_artifact(run_id, "visual_plan.json.prev", prev_vp_text)
    state["visual_plan"] = vp
    _save_state(state)
    _save_artifact(run_id, "visual_plan.json", json.dumps(vp, ensure_ascii=False, indent=2))
    return {"ok": True, "placement_id": pid, "caption": caption}


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

    # Gateway 配置
    pipeline = settings.setdefault("pipeline", {})
    if form.get("gateway_url"):
        pipeline["gateway_url"] = str(form["gateway_url"]).strip().rstrip("/")
    if form.get("llm_mode"):
        pipeline["llm_mode"] = form["llm_mode"]
    if form.get("gateway_agent_id"):
        pipeline["gateway_agent_id"] = form["gateway_agent_id"]

    # 默认模型
    if form.get("default_model"):
        pipeline["default_llm"] = form["default_model"]
    if form.get("image_engine"):
        pipeline["image_engine"] = form["image_engine"]

    # LLM overrides
    overrides = pipeline.setdefault("llm_overrides", {})
    for role in ["scout", "researcher", "writer", "de_ai_editor", "director"]:
        val = str(form.get(f"model_{role}", "")).strip()
        if val:
            overrides[role] = val
        elif role in overrides:
            del overrides[role]  # 清空 = 使用默认

    # WeChat config（凭证只走环境变量，不落配置文件）
    wechat = settings.setdefault("wechat", {})
    if form.get("wechat_author"):
        wechat["author"] = form["wechat_author"]

    save_settings(settings)

    # 通知频道 → 写入 .env.local
    notify_channel = str(form.get("notify_channel", "")).strip()
    env_local_path = Path(__file__).parent.parent.parent.parent / ".env.local"
    _update_env_local(env_local_path, "CONTENTPIPE_NOTIFY_CHANNEL", notify_channel)

    return HTMLResponse(
        '<div class="text-green-400 text-sm mt-2">✅ 设置已保存（通知频道需重启生效）</div>',
    )


# ── Setup Wizard API ─────────────────────────────────────────


@router.post("/setup/test-gateway")
async def api_setup_test_gateway(request: Request):
    """测试 Gateway 连接，返回模型数量和频道数量。"""
    import httpx

    body = await request.json()
    gateway_url = (body.get("gateway_url") or "").strip().rstrip("/")
    if not gateway_url:
        return JSONResponse({"ok": False, "error": "请输入 Gateway 地址"})

    from gateway_auth import get_gateway_token

    token = get_gateway_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    model_count = 0
    channel_count = 0

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 1. 健康检查
            health_resp = await client.get(f"{gateway_url}/health", headers=headers)
            if health_resp.status_code != 200:
                return JSONResponse({"ok": False, "error": f"Gateway 返回 HTTP {health_resp.status_code}"})

            # 2. 模型列表 — 通过 CLI
            try:
                result = subprocess.run(
                    ["openclaw", "models", "list", "--json"],
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, "NO_COLOR": "1"},
                )
                if result.returncode == 0:
                    import json as _json
                    raw = _json.loads(result.stdout)
                    raw_models = raw.get("models", raw) if isinstance(raw, dict) else raw
                    model_count = len(raw_models) if isinstance(raw_models, list) else 0
            except Exception:
                model_count = -1  # 无法获取但不阻止

            # 3. 频道 — 从 openclaw.json 检测已配置的 providers
            try:
                cfg_path = Path.home() / ".openclaw" / "openclaw.json"
                if cfg_path.exists():
                    import json as _json
                    cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                    ch_cfg = cfg.get("channels", {})
                    channel_count = sum(1 for p in ch_cfg if ch_cfg[p].get("accounts") or ch_cfg[p].get("token"))
            except Exception:
                channel_count = 0

    except httpx.ConnectError:
        return JSONResponse({"ok": False, "error": f"无法连接 {gateway_url}，请检查 Gateway 是否已启动"})
    except httpx.TimeoutException:
        return JSONResponse({"ok": False, "error": "连接超时"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

    return JSONResponse({
        "ok": True,
        "model_count": model_count,
        "channel_count": channel_count,
    })


@router.get("/setup/discover")
async def api_setup_discover(gateway_url: str = "http://localhost:18789"):
    """发现 Gateway 的模型和频道列表，供 Setup 向导前端填充下拉框。"""
    import httpx

    gateway_url = gateway_url.strip().rstrip("/")
    from gateway_auth import get_gateway_token
    token = get_gateway_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    models = []
    channels = []

    # 模型列表 — 通过 CLI 获取
    try:
        result = subprocess.run(
            ["openclaw", "models", "list", "--json"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if result.returncode == 0:
            import json as _json
            raw = _json.loads(result.stdout)
            raw_models = raw.get("models", raw) if isinstance(raw, dict) else raw
            if isinstance(raw_models, list):
                for m in raw_models:
                    mid = m.get("key") or m.get("model") or m.get("id") or m.get("name", "")
                    if mid:
                        name = m.get("name", mid)
                        tags = m.get("tags", [])
                        configured = "configured" in tags if isinstance(tags, list) else "configured" in str(tags)
                        prefix = "✅" if configured else "⬜"
                        ctx = m.get("contextWindow", 0)
                        ctx_label = f" ({ctx // 1024}k)" if ctx > 0 else ""
                        models.append({"id": mid, "label": f"{prefix} {name}{ctx_label}"})
    except Exception:
        pass

    # 如果 CLI 失败，fallback: 尝试 Gateway REST API
    if not models:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{gateway_url}/api/models", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_models = data.get("models", data) if isinstance(data, dict) else data
                    if isinstance(raw_models, list):
                        for m in raw_models:
                            mid = m.get("key") or m.get("model") or m.get("id") or m.get("name", "")
                            if mid:
                                name = m.get("name", mid)
                                ctx = m.get("contextWindow", 0)
                                ctx_label = f" ({ctx // 1024}k)" if ctx > 0 else ""
                                models.append({"id": mid, "label": f"✅ {name}{ctx_label}"})
        except Exception:
            pass

    # 最终 fallback: 常用模型列表
    if not models:
        models = [
            {"id": "dashscope/qwen3.5-plus", "label": "dashscope/qwen3.5-plus"},
            {"id": "anthropic-sonnet/claude-sonnet-4-6", "label": "anthropic-sonnet/claude-sonnet-4-6"},
            {"id": "anthropic/claude-opus-4-6", "label": "anthropic/claude-opus-4-6"},
            {"id": "openai-codex/gpt-5.4", "label": "openai-codex/gpt-5.4"},
            {"id": "dashscope/glm-5", "label": "dashscope/glm-5"},
            {"id": "dashscope/kimi-k2.5", "label": "dashscope/kimi-k2.5"},
        ]

    # 频道列表 — 从 openclaw.json 读可用 channel providers，
    # 然后通过 Discord Bot Token 获取 guild channel list
    try:
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if cfg_path.exists():
            import json as _json
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
            discord_cfg = cfg.get("channels", {}).get("discord", {})

            # 获取 Discord bot token
            bot_token = ""
            accounts = discord_cfg.get("accounts", {})
            for acc_id, acc in accounts.items():
                t = acc.get("token", "")
                if t:
                    bot_token = t
                    break
            if not bot_token:
                bot_token = discord_cfg.get("token", "")

            if bot_token:
                async with httpx.AsyncClient(timeout=10) as client:
                    # 获取 bot 的 guilds
                    guilds_resp = await client.get(
                        "https://discord.com/api/v10/users/@me/guilds",
                        headers={"Authorization": f"Bot {bot_token}"},
                    )
                    if guilds_resp.status_code == 200:
                        guilds = guilds_resp.json()
                        for guild in guilds[:5]:  # 最多 5 个 guild
                            guild_id = guild.get("id", "")
                            guild_name = guild.get("name", "")
                            ch_resp = await client.get(
                                f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                                headers={"Authorization": f"Bot {bot_token}"},
                            )
                            if ch_resp.status_code == 200:
                                for ch in ch_resp.json():
                                    if ch.get("type") == 0:  # 文字频道
                                        channels.append({
                                            "id": ch["id"],
                                            "label": f"💬 [{guild_name}] #{ch.get('name', '')}",
                                            "provider": "discord",
                                        })
    except Exception:
        pass

    return JSONResponse({"models": models, "channels": channels})


@router.post("/setup/save")
async def api_setup_save(request: Request):
    """保存 Setup 向导配置。"""
    body = await request.json()
    settings = load_settings()
    pipeline = settings.setdefault("pipeline", {})

    # Gateway
    if body.get("gateway_url"):
        pipeline["gateway_url"] = body["gateway_url"].strip().rstrip("/")
    if body.get("llm_mode"):
        pipeline["llm_mode"] = body["llm_mode"]

    # 模型
    if body.get("default_model"):
        pipeline["default_llm"] = body["default_model"]

    # 角色覆盖
    overrides = pipeline.setdefault("llm_overrides", {})
    for role in ["scout", "researcher", "writer", "de_ai_editor", "director"]:
        val = body.get(f"model_{role}", "").strip()
        if val:
            overrides[role] = val

    # 图片引擎
    if body.get("image_engine"):
        pipeline["image_engine"] = body["image_engine"]

    # 微信作者
    wechat = settings.setdefault("wechat", {})
    if body.get("wechat_author"):
        wechat["author"] = body["wechat_author"]

    save_settings(settings)

    # 通知频道 → 写入 .env.local
    notify_channel = body.get("notify_channel", "").strip()
    env_local_path = Path(__file__).parent.parent.parent.parent / ".env.local"
    _update_env_local(env_local_path, "CONTENTPIPE_NOTIFY_CHANNEL", notify_channel)

    # 端口
    port = body.get("port", "").strip()
    if port:
        _update_env_local(env_local_path, "CONTENTPIPE_PORT", port)

    # 标记设置完成
    setup_flag = Path(__file__).parent.parent.parent.parent / "config" / ".setup_done"
    setup_flag.parent.mkdir(parents=True, exist_ok=True)
    setup_flag.write_text("configured\n")

    return JSONResponse({"ok": True})


def _update_env_local(env_path: Path, key: str, value: str):
    """更新 .env.local 中的指定键值，保留其他行。"""
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                if value:
                    lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found and value:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


# ── 内部函数 ──────────────────────────────────────────────────

async def _execute_pipeline(run_id: str):
    """
    后台执行 Pipeline — 调用真实 LLM 节点

    节点顺序: scout → researcher → writer → director → image_gen → formatter → publisher

    交互节点（人工模式暂停）: scout, researcher, writer, director, formatter
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
