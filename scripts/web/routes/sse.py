"""
ContentPipe Web UI — SSE 实时推送

HTMX 通过 hx-ext="sse" + sse-connect="/sse/{run_id}" 连接。
每个事件触发对应的 sse-swap 更新 DOM 片段。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from web.events import event_bus
from web.run_manager import get_run, PIPELINE_NODES

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/api/runs/{run_id}/events")
async def sse_json_endpoint(request: Request, run_id: str):
    """JSON SSE 端点：推送 Pipeline 事件（供 OpenClaw Agent / 外部客户端订阅）

    每个事件的 data 是 JSON 对象：
    event: node_complete
    data: {"run_id":"...","node":"scout","duration_ms":12000}
    """
    async def event_generator():
        try:
            async for event in event_bus.subscribe(run_id):
                yield event.to_sse()
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


@router.get("/sse/{run_id}")
async def sse_endpoint(request: Request, run_id: str):
    """
    SSE 端点：推送 Pipeline 事件

    HTMX 用法:
      <div hx-ext="sse" sse-connect="/sse/run_003">
        <div sse-swap="node_complete">...</div>
      </div>

    每个事件的 data 是一段 HTML 片段，HTMX 直接 swap 到 DOM。
    """
    async def event_generator():
        try:
            async for event in event_bus.subscribe(run_id):
                # 根据事件类型生成 HTML 片段
                html = _render_event_html(request, event.type, run_id, event.data)
                yield {
                    "event": event.type,
                    "data": html,
                }
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


def _render_event_html(request: Request, event_type: str, run_id: str, data: dict) -> str:
    """将事件转为 HTML 片段（供 HTMX sse-swap 使用）"""

    if event_type == "node_start":
        node_id = data.get("node", "")
        node = _find_node(node_id)
        return f'''<div class="flex items-center gap-2 text-blue-400">
            <span class="animate-spin">⏳</span>
            <span>{node["icon"]} {node["label"]} 执行中...</span>
        </div>'''

    elif event_type == "node_complete":
        node_id = data.get("node", "")
        node = _find_node(node_id)
        duration = data.get("duration_ms", 0)
        duration_str = f"{duration // 1000}s" if duration else ""
        summary = data.get("summary", "")
        # 返回完整的进度条更新
        run = get_run(run_id)
        if run:
            return _render_progress_bar(run)
        return f'''<div class="flex items-center gap-2 text-green-400">
            <span>✅</span>
            <span>{node["icon"]} {node["label"]} 完成 {duration_str}</span>
            <span class="text-gray-500 text-sm">{summary}</span>
        </div>'''

    elif event_type == "node_error":
        node_id = data.get("node", "")
        node = _find_node(node_id)
        error = data.get("error", "")
        return f'''<div class="flex items-center gap-2 text-red-400">
            <span>❌</span>
            <span>{node["icon"]} {node["label"]} 失败</span>
            <span class="text-gray-500 text-sm">{error[:80]}</span>
        </div>'''

    elif event_type == "review_needed":
        review_type = data.get("review_type", "")
        label = "配图决策审核" if review_type == "decision" else "图片选择" if review_type == "image" else "最终审核"
        return f'''<div class="flex items-center gap-2 text-yellow-400 p-3 bg-yellow-900/20 rounded-lg">
            <span>⏸️</span>
            <span>{label} — 需要人工操作</span>
            <a href="/runs/{run_id}/review"
               class="ml-auto px-3 py-1 bg-yellow-600 text-white rounded hover:bg-yellow-500 text-sm">
               去审核 →
            </a>
        </div>'''

    elif event_type == "run_complete":
        total_ms = data.get("total_time_ms", 0)
        total_str = f"{total_ms // 60000}min" if total_ms else ""
        return f'''<div class="flex items-center gap-2 text-green-400 p-3 bg-green-900/20 rounded-lg">
            <span>🎉</span>
            <span>Pipeline 完成！{total_str}</span>
            <a href="/runs/{run_id}/preview"
               class="ml-auto px-3 py-1 bg-green-600 text-white rounded hover:bg-green-500 text-sm">
               查看预览 →
            </a>
        </div>'''

    return f'<div class="text-gray-500">{event_type}: {json.dumps(data)}</div>'


def _find_node(node_id: str) -> dict:
    """查找节点信息"""
    for node in PIPELINE_NODES:
        if node["id"] == node_id:
            return node
    return {"id": node_id, "label": node_id, "icon": "⚙️"}


def _render_progress_bar(run: dict) -> str:
    """渲染进度条 HTML"""
    nodes = run.get("_nodes", [])
    parts = []
    for node in nodes:
        status = node["status"]
        if status == "completed":
            icon = "✅"
            cls = "text-green-400"
        elif status in ("running", "review"):
            icon = "⏳" if status == "running" else "⏸️"
            cls = "text-yellow-400"
        else:
            icon = "○"
            cls = "text-gray-600"
        parts.append(f'<div class="flex flex-col items-center gap-1 {cls}">'
                      f'<span class="text-lg">{icon}</span>'
                      f'<span class="text-xs">{node["label"]}</span>'
                      f'</div>')

    return f'''<div class="flex items-center gap-1 justify-between">
        {"".join(parts)}
    </div>'''
