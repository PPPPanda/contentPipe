"""
ContentPipe Plugin — REST API 路由

提供 JSON API 供 Web UI、Discord 按钮、AI 工具调用。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from cli_utils import parse_cli_json
from logutil import get_logger

from web.run_manager import (
    list_runs, get_run, create_run, update_run_state, delete_run,
    get_node_output, get_node_input, get_run_image_path,
    get_run_artifact, load_settings, save_settings,
    PIPELINE_NODES, _load_raw_state, _save_state,
)
from web.events import (
    event_bus, emit_node_start, emit_node_complete, emit_run_complete,
    emit_chat_message, emit_approved, emit_rejected, emit_rolled_back,
    emit_review_needed,
)


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
        # v2 多话题
        topics_list = parsed.get("topics", [])
        if topics_list and isinstance(topics_list, list):
            state["scout_topics"] = topics_list
            # 重新选中：保留当前 selected_topic_id，若已无效则选第一个
            sel_id = state.get("selected_topic_id", "")
            valid_ids = [t.get("topic_id") for t in topics_list]
            if sel_id not in valid_ids:
                sel_id = valid_ids[0] if valid_ids else ""
                state["selected_topic_id"] = sel_id
            # 用选中的话题更新顶级字段
            chosen = next((t for t in topics_list if t.get("topic_id") == sel_id), topics_list[0] if topics_list else {})
            state["topic"] = chosen
            state["writer_brief"] = chosen.get("writer_brief", {}) or {}
            state["handoff_to_researcher"] = chosen.get("handoff_to_researcher", {}) or {}
        else:
            # v1 兼容
            state["topic"] = parsed.get("topic", {}) or {}
            state["writer_brief"] = parsed.get("writer_brief", {}) or {}
            state["handoff_to_researcher"] = parsed.get("handoff_to_researcher", {}) or {}
        state["reference_articles"] = parsed.get("reference_articles", []) or []
        state["user_requirements"] = parsed.get("user_requirements", {}) or {}
        state["reference_index"] = parsed.get("reference_index", {}) or {}
        state["link_usage_policy"] = parsed.get("link_usage_policy", {}) or {}
        state["scout_process_summary"] = parsed.get("scout_process_summary", {}) or {}
        state["search_execution_log"] = parsed.get("search_execution_log", {}) or {}
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
    from nodes import _get_model, _save_artifact, generate_article_subtitle

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
            state["article"]["subtitle"] = generate_article_subtitle(state, after_article, article_title)
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
        "version": "0.8.1",
        "total_runs": len(runs),
        "active_runs": len(active),
    }


@router.get("/info")
async def api_plugin_info():
    """插件元信息"""
    return {
        "id": "content-pipeline",
        "name": "ContentPipe",
        "version": "0.8.1",
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
    from nodes import _save_artifact, generate_article_subtitle
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
    # 同步更新 article 的 word_count + subtitle
    if "article" in state and isinstance(state["article"], dict):
        state["article"]["word_count"] = len(content)
        article_title = state.get("article", {}).get("title", "") or state.get("topic", {}).get("title", "")
        state["article"]["subtitle"] = generate_article_subtitle(state, content, article_title)
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


# ── Scout 话题选择 ──────────────────────────────────────────

@router.post("/runs/{run_id}/select-topic")
async def api_select_topic(request: Request, run_id: str):
    """用户在 Scout 审核阶段选择话题候选"""
    data = await request.json()
    topic_id = data.get("topic_id", "")
    if not topic_id:
        raise HTTPException(status_code=400, detail="topic_id is required")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    scout_topics = raw.get("scout_topics", [])
    chosen = next((t for t in scout_topics if t.get("topic_id") == topic_id), None)
    if not chosen:
        raise HTTPException(status_code=404, detail=f"Topic {topic_id} not found in scout_topics")

    # 更新 state：选中的话题写入 topic / writer_brief / handoff
    raw["selected_topic_id"] = topic_id
    raw["topic"] = chosen
    raw["writer_brief"] = chosen.get("writer_brief", {})
    raw["handoff_to_researcher"] = chosen.get("handoff_to_researcher", {})

    # 话题特有关键词覆盖全局 user_requirements（防止其他话题的关键词污染 Writer）
    ur = raw.get("user_requirements", {})
    if chosen.get("required_keywords"):
        ur["required_keywords"] = chosen["required_keywords"]
    elif "required_keywords" in ur:
        # 话题没有独立关键词 → 清空全局的，避免混入
        ur.pop("required_keywords", None)
    if chosen.get("preferred_keywords"):
        ur["preferred_keywords"] = chosen["preferred_keywords"]
    elif "preferred_keywords" in ur:
        ur.pop("preferred_keywords", None)
    raw["user_requirements"] = ur

    _save_state(raw)

    logger.info("Topic selected: %s for run %s", topic_id, run_id)
    return {"ok": True, "selected_topic_id": topic_id, "title": chosen.get("title", "")}


_review_locks: dict[str, float] = {}  # run_id → timestamp of last approve

@router.post("/runs/{run_id}/review")
async def api_submit_review(request: Request, run_id: str, background_tasks: BackgroundTasks):
    """提交审核结果（幂等：10 秒内重复 approve 返回 302 不重跑 pipeline）"""
    import time as _time
    form = await request.form()
    action = form.get("action", "approve")

    # 防重复提交：同一 run 10 秒内的第二次 approve 直接跳过
    if action in ("approve", "select"):
        now = _time.time()
        last = _review_locks.get(run_id, 0)
        if now - last < 10:
            logger.warning("Duplicate approve for %s (%.1fs ago), skipping", run_id, now - last)
            return HTMLResponse(
                f'<meta http-equiv="refresh" content="0;url=/runs/{run_id}">',
            )
        _review_locks[run_id] = now

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

    # SSE 事件 + Discord 反向推送
    current_node = raw.get("current_stage", "")
    if action in ("approve", "select"):
        emit_approved(run_id, current_node, source="web")
        try:
            from web.notify import notify_discord
            asyncio.ensure_future(notify_discord(
                f"✅ **{current_node}** 已在网页端通过",
                run_id=run_id, node=current_node,
            ))
        except Exception:
            pass
    elif action == "revise":
        emit_rejected(run_id, current_node, reason=feedback if action == "revise" else "", source="web")
        try:
            from web.notify import notify_discord
            asyncio.ensure_future(notify_discord(
                f"🔄 **{current_node}** 被驳回（网页端）\n> {feedback[:200]}",
                run_id=run_id, node=current_node,
            ))
        except Exception:
            pass

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
    source = body.get("source", "web")  # web | discord | openclaw
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
    save_chat_message(run_id, node_id, "user", display_msg, attachments=attachments, source=source)
    emit_chat_message(run_id, node_id, "user", display_msg, source=source)

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

    run_dir = Path(__file__).parent.parent.parent.parent / "output" / "runs" / run_id
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

    run_dir = Path(__file__).parent.parent.parent.parent / "output" / "runs" / run_id

    # 杀掉残留的 image agent 进程
    _kill_image_agent_processes(run_id)

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


def _kill_image_agent_processes(run_id: str):
    """杀掉与指定 run 关联的 image agent 子进程。"""
    import subprocess
    try:
        # 查找 contentpipe-img 相关的 openclaw agent 进程
        result = subprocess.run(
            ["pgrep", "-af", f"contentpipe-img.*{run_id[:20]}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                pid = line.split()[0]
                try:
                    subprocess.run(["kill", pid], timeout=5)
                    logger.info("Killed image agent process: pid=%s", pid)
                except Exception:
                    pass
        # 也查找通用的 contentpipe-img session
        result2 = subprocess.run(
            ["pgrep", "-af", "contentpipe-img"],
            capture_output=True, text=True, timeout=5,
        )
        if result2.stdout.strip():
            for line in result2.stdout.strip().split("\n"):
                pid = line.split()[0]
                try:
                    subprocess.run(["kill", pid], timeout=5)
                    logger.info("Killed stale image agent process: pid=%s", pid)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Failed to kill image agent processes: %s", e)


# ── JSON API：reject / rollback（供 OpenClaw 工具调用）─────────

@router.post("/runs/{run_id}/reject")
async def api_reject_node(request: Request, run_id: str, background_tasks: BackgroundTasks):
    """驳回当前节点，带反馈重新执行。

    JSON body: {"reason": "...", "source": "openclaw"}
    """
    body = await request.json()
    reason = body.get("reason", "")
    source = body.get("source", "api")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    if raw.get("status") != "review":
        raise HTTPException(status_code=400, detail=f"Run not in review status (current: {raw.get('status')})")

    node_id = raw.get("current_stage", "")
    raw["review_action"] = "revise"
    raw["user_feedback"] = {"action": "revise", "global_note": reason, "source": source}
    _save_state(raw)

    emit_rejected(run_id, node_id, reason=reason, source=source)

    # 恢复 Pipeline 执行（带反馈重新执行当前节点）
    background_tasks.add_task(_execute_pipeline, run_id)

    # Discord 反向推送
    try:
        from web.notify import notify_discord
        import asyncio
        asyncio.ensure_future(notify_discord(
            f"🔄 **{node_id}** 被驳回（{source}）\n> {reason[:200]}",
            run_id=run_id, node=node_id,
        ))
    except Exception:
        pass

    return {
        "ok": True,
        "node": node_id,
        "action": "revise",
        "message": f"{node_id} rejected with feedback, re-executing",
    }


@router.post("/runs/{run_id}/rollback")
async def api_rollback_node(request: Request, run_id: str):
    """回退到指定节点。

    JSON body: {"target_node": "writer", "reason": "...", "source": "openclaw"}
    """
    body = await request.json()
    target_node = body.get("target_node", "")
    reason = body.get("reason", "")

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Run not found")

    current_node = raw.get("current_stage", "")
    interactive_nodes = ["scout", "researcher", "writer", "director", "formatter"]

    if target_node not in interactive_nodes:
        raise HTTPException(status_code=400, detail=f"Invalid target node: {target_node}")

    if target_node == current_node:
        raise HTTPException(status_code=400, detail=f"Already at {target_node}")

    # 确保 target 在 current 之前
    cur_idx = interactive_nodes.index(current_node) if current_node in interactive_nodes else len(interactive_nodes)
    tgt_idx = interactive_nodes.index(target_node)
    if tgt_idx >= cur_idx:
        raise HTTPException(status_code=400, detail=f"Cannot rollback forward: {current_node} → {target_node}")

    # 清理从 target+1 到 current 的所有节点
    nodes_to_clear = interactive_nodes[tgt_idx + 1: cur_idx + 1]
    run_dir = Path(__file__).parent.parent.parent.parent / "output" / "runs" / run_id

    state_cleanup = {
        "scout": ["topic", "writer_brief", "handoff_to_researcher", "reference_articles",
                   "user_requirements", "reference_index", "link_usage_policy", "scout_process_summary"],
        "researcher": ["research", "writer_packet", "verification_results",
                        "evidence_backed_insights", "open_issues"],
        "writer": ["article", "article_edited", "writer_context"],
        "director": ["visual_plan", "image_candidates", "selected_images",
                      "generated_images", "generated_cover"],
        "formatter": ["formatted_html"],
    }
    artifact_cleanup = {
        "scout": ["topic.yaml", "scout_raw.txt"],
        "researcher": ["research.yaml", "researcher_raw.txt"],
        "writer": ["writer_context.yaml", "article_draft.md", "article_edited.md"],
        "director": ["director_raw.txt", "visual_plan.json", "director_refine_raw.txt",
                      "image_candidates.json", "generated_images.json", "generated_cover.json"],
        "formatter": ["formatted.html", "content_body.html"],
    }

    cleared: list[str] = []
    session_gen = raw.get("_session_gen") if isinstance(raw.get("_session_gen"), dict) else {}
    for node in nodes_to_clear:
        # 清理 state 字段
        for key in state_cleanup.get(node, []):
            raw.pop(key, None)
        # 清理文件
        for fname in artifact_cleanup.get(node, []):
            p = run_dir / fname
            if p.exists():
                p.unlink()
        # 清理 chat 文件
        chat_file = run_dir / f"chat_{node}.json"
        if chat_file.exists():
            chat_file.unlink()
        # 提升 session generation
        session_gen[node] = int(session_gen.get(node, 0) or 0) + 1
        cleared.append(node)

    # director → 清理图片目录
    if "director" in nodes_to_clear:
        images_dir = run_dir / "images"
        if images_dir.exists():
            import shutil
            shutil.rmtree(images_dir, ignore_errors=True)

    raw["_session_gen"] = session_gen
    raw["current_stage"] = target_node
    raw["status"] = "review"
    raw["_node_done"] = True
    raw["review_action"] = ""
    raw.pop("user_feedback", None)
    if reason:
        raw["_rollback_reason"] = reason
    _save_state(raw)

    emit_rolled_back(run_id, current_node, target_node, source=body.get("source", "api"))

    # Discord 反向推送
    try:
        from web.notify import notify_discord
        import asyncio
        reason_text = f"\n> {reason[:200]}" if reason else ""
        asyncio.ensure_future(notify_discord(
            f"⏪ 已回退: **{current_node}** → **{target_node}**{reason_text}",
            run_id=run_id, node=target_node,
        ))
    except Exception:
        pass

    return {
        "ok": True,
        "rolled_back_to": target_node,
        "cleared_nodes": cleared,
        "message": f"Rolled back to {target_node}, cleared: {', '.join(cleared)}",
    }


# ── 聊天 prompt 构建 ─────────────────────────────────────────

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
        # 清理旧配图文件（不同扩展名），避免 img_001.jpg / img_001.png 并存造成预览混淆
        for ext in ALLOWED_IMAGE_EXTS:
            old = images_dir / f"{placement_id}{ext}"
            if old.exists():
                old.unlink()
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

    # 如果当前在 formatter / publisher，替换图片后立刻重算 HTML，避免页面左栏和 iframe 预览不一致
    rerendered = False
    current_stage = state.get("current_stage", "")
    if current_stage in {"formatter", "publisher"}:
        try:
            from formatter import format_article
            from web.run_manager import OUTPUT_DIR as _OUTPUT_DIR
            html = format_article(run_id, _OUTPUT_DIR / "runs" / run_id, state.get("platform", "wechat"))
            state = _load_raw_state(run_id) or state
            state["formatted_html"] = html
            _save_state(state)
            rerendered = True
        except Exception as e:
            logger.warning("upload image: formatter rerender failed for %s: %s", run_id, e)

    return {
        "ok": True,
        "filename": filename,
        "path": str(filepath),
        "placement_id": placement_id,
        "purpose": purpose,
        "mime": content_type or f"image/{suffix.lstrip('.')}",
        "rerendered": rerendered,
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
    """更新配置（JSON）。微信凭据写入 .env.local，不落 pipeline.yaml。"""
    settings = await request.json()

    # 敏感项拆出，避免写入 pipeline.yaml
    wechat_appid = str(settings.pop("wechat_appid", "") or "").strip()
    wechat_secret = str(settings.pop("wechat_secret", "") or "").strip()

    save_settings(settings)

    env_local_path = Path(__file__).parent.parent.parent.parent / ".env.local"
    if wechat_appid:
        _update_env_local(env_local_path, "WECHAT_APPID", wechat_appid)
    if wechat_secret:
        _update_env_local(env_local_path, "WECHAT_SECRET", wechat_secret)

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

    # WeChat config（作者名写配置；凭证只走 .env.local，不落 pipeline.yaml）
    wechat = settings.setdefault("wechat", {})
    if form.get("wechat_author"):
        wechat["author"] = form["wechat_author"]

    save_settings(settings)

    # 环境变量 → 写入 .env.local，并同步当前进程环境
    env_local_path = Path(__file__).parent.parent.parent.parent / ".env.local"

    notify_channel = str(form.get("notify_channel", "")).strip()
    _update_env_local(env_local_path, "CONTENTPIPE_NOTIFY_CHANNEL", notify_channel)

    wechat_appid = str(form.get("wechat_appid", "")).strip()
    if wechat_appid:
        _update_env_local(env_local_path, "WECHAT_APPID", wechat_appid)

    wechat_secret = str(form.get("wechat_secret", "")).strip()
    if wechat_secret:
        _update_env_local(env_local_path, "WECHAT_SECRET", wechat_secret)

    return HTMLResponse(
        '<div class="text-green-400 text-sm mt-2">✅ 设置已保存（微信凭据已写入 .env.local）</div>',
    )


# ── Setup Wizard API ─────────────────────────────────────────


def _discover_model_keys_local() -> list[str]:
    try:
        result = subprocess.run(
            ["openclaw", "models", "list", "--json"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if result.returncode != 0:
            return []
        raw = parse_cli_json(result.stdout)
        raw_models = raw.get("models", raw) if isinstance(raw, dict) else raw
        if not isinstance(raw_models, list):
            return []
        keys = []
        for m in raw_models:
            mid = m.get("key") or m.get("model") or m.get("id") or m.get("name", "")
            if mid:
                keys.append(str(mid))
        return keys
    except Exception:
        return []


def _setup_preflight_agent_id() -> str:
    settings = load_settings()
    return settings.get("pipeline", {}).get("gateway_agent_id", "contentpipe-blank") or "contentpipe-blank"


def _is_probably_local_gateway(gateway_url: str) -> bool:
    try:
        host = (urlparse(gateway_url).hostname or "").strip().lower()
    except Exception:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback
    except Exception:
        return False


async def _run_setup_preflight(gateway_url: str) -> dict[str, Any]:
    import httpx
    from gateway_auth import build_gateway_headers

    checks: list[dict[str, Any]] = []
    agent_id = _setup_preflight_agent_id()
    skills_dir = (Path(__file__).parent.parent.parent.parent / "skills").resolve()

    # P0-1: blank agent exists
    agent_exists = False
    agents_error = ""
    try:
        result = subprocess.run(
            ["openclaw", "config", "get", "agents.list", "--json"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if result.returncode == 0:
            items = parse_cli_json(result.stdout)
            agent_exists = any(item.get("id") == agent_id for item in items)
        else:
            agents_error = (result.stderr or result.stdout or "openclaw config get agents.list failed")[:200]
    except Exception as e:
        agents_error = str(e)[:200]
    checks.append({
        "id": "agent_exists",
        "label": f"blank agent 已存在（{agent_id}）",
        "ok": agent_exists,
        "detail": "已找到 agent 配置" if agent_exists else (agents_error or "未找到 contentpipe-blank，请先执行 ./start.sh install-agent"),
    })

    # P0-2: gateway_agent_id can be routed
    route_ok = False
    route_detail = ""
    probe_models = _discover_model_keys_local()
    probe_model = probe_models[0] if probe_models else ""
    if not probe_model:
        route_detail = "未找到可用模型，无法做路由探测"
    else:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{gateway_url}/v1/chat/completions",
                    headers=build_gateway_headers({"X-OpenClaw-Agent-Id": agent_id}),
                    json={
                        "model": probe_model,
                        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                        "max_tokens": 8,
                    },
                )
                route_ok = resp.status_code == 200
                route_detail = f"HTTP {resp.status_code} · model={probe_model}"
                if not route_ok:
                    route_detail = f"{route_detail} · {(resp.text or '')[:160]}"
        except Exception as e:
            route_detail = str(e)[:200]
    checks.append({
        "id": "agent_route",
        "label": f"Gateway 可路由到 agent（{agent_id}）",
        "ok": route_ok,
        "detail": route_detail or "路由探测失败",
    })

    # P0-3: skills.load.extraDirs contains ContentPipe skills path
    extra_dirs_ok = False
    extra_dirs_detail = ""
    extra_dirs: list[str] = []
    try:
        result = subprocess.run(
            ["openclaw", "config", "get", "skills.load.extraDirs", "--json"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if result.returncode == 0:
            loaded = parse_cli_json(result.stdout)
            if isinstance(loaded, list):
                extra_dirs = [str(Path(p).resolve()) for p in loaded if p]
                extra_dirs_ok = str(skills_dir) in extra_dirs
                extra_dirs_detail = f"已注册 {len(extra_dirs)} 个目录"
            else:
                extra_dirs_detail = "skills.load.extraDirs 不是数组"
        else:
            extra_dirs_detail = (result.stderr or result.stdout or "读取 skills.load.extraDirs 失败")[:200]
    except Exception as e:
        extra_dirs_detail = str(e)[:200]
    checks.append({
        "id": "skills_extra_dirs",
        "label": "skills.load.extraDirs 已包含 ContentPipe skills 目录",
        "ok": extra_dirs_ok,
        "detail": extra_dirs_detail if extra_dirs_ok else (extra_dirs_detail or f"缺少 {skills_dir}"),
    })

    # P0-4: path exists on gateway host (strict for local gateways; remote gateway = unverifiable warning)
    local_gateway = _is_probably_local_gateway(gateway_url)
    skills_path_exists = skills_dir.exists() and skills_dir.is_dir()
    path_ok = skills_path_exists if local_gateway else True
    path_detail = ""
    if local_gateway:
        path_detail = f"本机路径: {skills_dir}" if skills_path_exists else f"本机路径不存在: {skills_dir}"
    else:
        path_detail = f"远端 Gateway（{gateway_url}）无法直接验证宿主机路径；请确认 Gateway 所在机器也有此目录"
    checks.append({
        "id": "skills_path_exists",
        "label": "ContentPipe skills 路径在 Gateway 宿主上存在",
        "ok": path_ok,
        "detail": path_detail,
        "warning": not local_gateway,
    })

    blocking_checks = [c for c in checks if not c.get("warning")]
    preflight_ok = all(c["ok"] for c in blocking_checks)
    return {
        "ok": preflight_ok,
        "agent_id": agent_id,
        "skills_dir": str(skills_dir),
        "checks": checks,
    }


@router.post("/setup/test-gateway")
async def api_setup_test_gateway(request: Request):
    """测试 Gateway 连接，并执行 P0 安装预检。"""
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
                model_count = len(_discover_model_keys_local())
            except Exception:
                model_count = -1

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

    preflight = await _run_setup_preflight(gateway_url)

    return JSONResponse({
        "ok": True,
        "ready": preflight["ok"],
        "model_count": model_count,
        "channel_count": channel_count,
        "preflight": preflight,
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
            raw = parse_cli_json(result.stdout)
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

    # 频道列表 — 从 openclaw.json 读所有 channel providers，自动发现可用频道
    try:
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if cfg_path.exists():
            import json as _json
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
            all_channels_cfg = cfg.get("channels", {})
            proxy_url = all_channels_cfg.get("discord", {}).get("proxy", "")

            async with httpx.AsyncClient(
                timeout=10,
                proxy=proxy_url if proxy_url else None,
            ) as client:
                # ── Discord ──
                discord_cfg = all_channels_cfg.get("discord", {})
                if discord_cfg.get("enabled", False):
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
                        try:
                            guilds_resp = await client.get(
                                "https://discord.com/api/v10/users/@me/guilds",
                                headers={"Authorization": f"Bot {bot_token}"},
                            )
                            if guilds_resp.status_code == 200:
                                for guild in guilds_resp.json()[:5]:
                                    guild_id = guild.get("id", "")
                                    guild_name = guild.get("name", "")
                                    ch_resp = await client.get(
                                        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                                        headers={"Authorization": f"Bot {bot_token}"},
                                    )
                                    if ch_resp.status_code == 200:
                                        for ch in ch_resp.json():
                                            if ch.get("type") == 0:
                                                channels.append({
                                                    "id": ch["id"],
                                                    "label": f"💬 Discord [{guild_name}] #{ch.get('name', '')}",
                                                    "provider": "discord",
                                                })
                        except Exception:
                            pass

                # ── 飞书 (Feishu) ──
                feishu_cfg = all_channels_cfg.get("feishu", {})
                if feishu_cfg.get("enabled", False):
                    app_id = feishu_cfg.get("appId", "")
                    app_secret = feishu_cfg.get("appSecret", "")
                    domain = feishu_cfg.get("domain", "feishu")
                    base_url = "https://open.feishu.cn" if domain == "feishu" else "https://open.larksuite.com"

                    if app_id and app_secret:
                        try:
                            # 获取 tenant_access_token
                            token_resp = await client.post(
                                f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
                                json={"app_id": app_id, "app_secret": app_secret},
                            )
                            if token_resp.status_code == 200:
                                tenant_token = token_resp.json().get("tenant_access_token", "")
                                if tenant_token:
                                    # 获取 bot 所在的群列表
                                    chats_resp = await client.get(
                                        f"{base_url}/open-apis/im/v1/chats?page_size=50",
                                        headers={"Authorization": f"Bearer {tenant_token}"},
                                    )
                                    if chats_resp.status_code == 200:
                                        items = chats_resp.json().get("data", {}).get("items", [])
                                        for chat in items:
                                            chat_id = chat.get("chat_id", "")
                                            chat_name = chat.get("name", "未命名群")
                                            chat_type = chat.get("chat_type", "")
                                            icon = "👤" if chat_type == "p2p" else "👥"
                                            channels.append({
                                                "id": chat_id,
                                                "label": f"{icon} 飞书 {chat_name}",
                                                "provider": "feishu",
                                            })
                        except Exception:
                            pass

                # ── KOOK ──
                kook_cfg = all_channels_cfg.get("kook", {})
                if kook_cfg.get("enabled", False):
                    kook_token = kook_cfg.get("token", "")
                    if kook_token:
                        try:
                            guilds_resp = await client.get(
                                "https://www.kookapp.cn/api/v3/guild/list",
                                headers={"Authorization": f"Bot {kook_token}"},
                            )
                            if guilds_resp.status_code == 200:
                                guild_items = guilds_resp.json().get("data", {}).get("items", [])
                                for guild in guild_items[:5]:
                                    guild_id = guild.get("id", "")
                                    guild_name = guild.get("name", "")
                                    ch_resp = await client.get(
                                        f"https://www.kookapp.cn/api/v3/channel/list?guild_id={guild_id}",
                                        headers={"Authorization": f"Bot {kook_token}"},
                                    )
                                    if ch_resp.status_code == 200:
                                        ch_items = ch_resp.json().get("data", {}).get("items", [])
                                        for ch in ch_items:
                                            if ch.get("type") == 1:  # 文字频道
                                                channels.append({
                                                    "id": ch.get("id", ""),
                                                    "label": f"💬 KOOK [{guild_name}] #{ch.get('name', '')}",
                                                    "provider": "kook",
                                                })
                        except Exception:
                            pass

                # ── 企业微信 (WeCom) ──
                wecom_cfg = all_channels_cfg.get("wecom", {})
                if wecom_cfg.get("enabled", False):
                    # 企业微信主要通过 webhook 发送，不支持频道发现
                    # 但可以列出已配置的 agentId
                    agent_id = wecom_cfg.get("agentId", "")
                    if agent_id:
                        channels.append({
                            "id": f"wecom:{agent_id}",
                            "label": f"🏢 企业微信 应用 #{agent_id}",
                            "provider": "wecom",
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


@router.post("/restart")
async def api_restart():
    """重启 ContentPipe 服务（通过 start.sh restart）。"""
    import asyncio
    start_script = Path(__file__).parent.parent.parent.parent / "start.sh"
    if not start_script.exists():
        return JSONResponse({"ok": False, "error": "start.sh not found"}, status_code=500)

    async def _do_restart():
        await asyncio.sleep(0.5)  # 先让 response 发出去
        subprocess.Popen(
            [str(start_script), "restart"],
            cwd=str(start_script.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    asyncio.get_event_loop().create_task(_do_restart())
    return JSONResponse({"ok": True, "message": "restarting"})


def _update_env_local(env_path: Path, key: str, value: str):
    """更新 .env.local 中的指定键值，保留其他行，并同步当前进程环境。"""
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
                if value:
                    lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found and value:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


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

    from web.events import emit_node_start, emit_node_complete, emit_node_error
    from web.run_manager import _load_raw_state, _save_state, PIPELINE_NODES

    # 真实节点函数映射
    from nodes import (
        scout_node, researcher_node, writer_node,
        director_node,
        image_gen_node, formatter_node, publisher_node,
    )

    NODE_FUNCTIONS = {
        "scout": scout_node,
        "researcher": researcher_node,
        "writer": writer_node,
        "director": director_node,
        "image_gen": image_gen_node,
        "formatter": formatter_node,
        "publisher": publisher_node,
    }

    # 可交互节点 — 执行完后暂停让用户讨论
    # Writer 已内置去 AI 味（自动执行），用户在 Writer 节点审核最终文章
    INTERACTIVE_NODES = {
        "scout", "researcher", "writer",
        "director", "formatter",
    }

    # 跳过节点
    STUB_NODES = set()

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
                elif node_id == "publisher":
                    pr = state.get("publish_result", {}) if isinstance(state.get("publish_result"), dict) else {}
                    summary = pr.get("status", "?")

                if node_id == "publisher" and state.get("status") == "failed":
                    err = (state.get("publish_result", {}) or {}).get("error", "publisher failed")
                    emit_node_error(run_id, node_id, str(err)[:200])
                    _save_state(state)
                    return

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
                    # Discord 通知（含结构化产物摘要）
                    try:
                        from web.notify import notify_review_needed as _discord_notify
                        await _discord_notify(run_id, node_id, state=state)
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

    if state.get("status") != "failed":
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
