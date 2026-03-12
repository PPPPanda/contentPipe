"""
ContentPipe Web UI — 页面路由

服务端渲染 Jinja2 模板，返回完整 HTML 页面。
"""

from __future__ import annotations

from pathlib import Path
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web.auth import AUTH_COOKIE, get_auth_token, hash_token, is_auth_enabled
from web.run_manager import (
    list_runs, get_run, get_dashboard_stats,
    get_node_output, get_node_input,
    get_run_artifact, load_settings,
    PIPELINE_NODES,
)

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if not is_auth_enabled():
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "next": next,
        "page": "login",
        "auth_enabled": True,
    })


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...), next: str = Form("/")):
    token = get_auth_token()
    if not token or password != token:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "next": next,
            "page": "login",
            "auth_enabled": True,
            "error": "口令不正确",
        }, status_code=401)
    response = RedirectResponse(url=next or "/", status_code=303)
    response.set_cookie(AUTH_COOKIE, hash_token(token), httponly=True, samesite="lax")
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE)
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard 总览"""
    stats = get_dashboard_stats()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "page": "dashboard",
    })


@router.get("/runs", response_class=HTMLResponse)
async def run_list(request: Request):
    """Run 列表"""
    runs = list_runs()
    return templates.TemplateResponse("run_list.html", {
        "request": request,
        "runs": runs,
        "page": "runs",
    })


@router.get("/runs/new", response_class=HTMLResponse)
async def new_run_form(request: Request):
    """新建 Run 表单"""
    return templates.TemplateResponse("run_new.html", {
        "request": request,
        "page": "runs",
    })


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str):
    """Run 详情页"""
    run = get_run(run_id)
    if not run:
        return HTMLResponse("<h1>Run not found</h1>", status_code=404)
    env_ready = bool(os.getenv("WECHAT_APPID")) and bool(os.getenv("WECHAT_SECRET"))
    return templates.TemplateResponse("run_detail.html", {
        "request": request,
        "run": run,
        "nodes": PIPELINE_NODES,
        "page": "runs",
        "wechat_publish_ready": env_ready,
    })


@router.get("/runs/{run_id}/review", response_class=HTMLResponse)
async def review_page(request: Request, run_id: str, node: str | None = None):
    """通用节点交互页 — 每个节点都可以讨论+审批

    ?node=xxx 可打开任意已完成节点的历史聊天和输出。
    不带参数则打开当前 stage。
    """
    run = get_run(run_id)
    if not run:
        return HTMLResponse("<h1>Run not found</h1>", status_code=404)

    # 指定节点 or 当前节点
    stage = node or run.get("current_stage", "")
    # 是否只读模式（查看已完成节点的历史，不能 approve/revise）
    is_readonly = (node is not None and node != run.get("current_stage", ""))
    node_output = get_node_output(run_id, stage)

    # 节点元信息
    node_labels = {n["id"]: n for n in PIPELINE_NODES}
    node_info = node_labels.get(stage, {"id": stage, "label": stage, "icon": "⚙️"})

    # 上一个“可聊天/可审核”的节点（用于回退）
    interactive_nodes = ["scout", "researcher", "writer", "director", "formatter"]
    prev_review_node = None
    if stage in interactive_nodes:
        idx = interactive_nodes.index(stage)
        if idx > 0:
            prev_review_node = interactive_nodes[idx - 1]

    return templates.TemplateResponse("review_node.html", {
        "request": request,
        "run": run,
        "stage": stage,
        "node_info": node_info,
        "node_output": node_output,
        "is_readonly": is_readonly,
        "prev_review_node": prev_review_node,
        "prev_review_info": node_labels.get(prev_review_node) if prev_review_node else None,
        "page": "runs",
    })


@router.get("/runs/{run_id}/preview", response_class=HTMLResponse)
async def preview_page(request: Request, run_id: str):
    """文章预览页"""
    run = get_run(run_id)
    if not run:
        return HTMLResponse("<h1>Run not found</h1>", status_code=404)
    html_content = get_run_artifact(run_id, "formatted.html") or "<p>排版尚未完成</p>"
    return templates.TemplateResponse("preview.html", {
        "request": request,
        "run": run,
        "html_content": html_content,
        "page": "runs",
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """设置页"""
    settings = load_settings()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": settings,
        "page": "settings",
    })
