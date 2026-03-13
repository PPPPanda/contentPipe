"""
ContentPipe — 系统状态 API

诊断、统计、测试。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Request

from gateway_auth import build_gateway_headers

router = APIRouter()

OUTPUT_DIR = Path(__file__).parent.parent.parent.parent / "output"


@router.get("/system/status")
async def api_system_status():
    """系统状态全景"""
    from tools import load_pipeline_config
    config = load_pipeline_config()
    pipeline = config.get("pipeline", {})

    gateway_url = pipeline.get("gateway_url", "") or os.environ.get("OPENCLAW_GATEWAY_URL", "")
    gateway_ok = False
    gateway_latency_ms = 0

    if gateway_url:
        try:
            t0 = time.time()
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{gateway_url}/v1/models", headers=build_gateway_headers())
                gateway_ok = r.status_code == 200
                gateway_latency_ms = int((time.time() - t0) * 1000)
        except Exception:
            pass

    # Run 统计
    runs_dir = OUTPUT_DIR / "runs"
    total_runs = 0
    active_runs = 0
    completed_runs = 0
    if runs_dir.exists():
        for d in runs_dir.iterdir():
            if d.is_dir() and d.name.startswith("run_"):
                total_runs += 1
                state_file = d / "state.yaml"
                if state_file.exists():
                    try:
                        s = yaml.safe_load(state_file.read_text(encoding="utf-8"))
                        status = s.get("status", "")
                        if status == "completed":
                            completed_runs += 1
                        elif status in ("running", "review", "pending"):
                            active_runs += 1
                    except Exception:
                        pass

    # 通知配置
    notify_channel = os.environ.get("CONTENTPIPE_NOTIFY_CHANNEL", "")
    from web.notify import _get_discord_bot_token
    discord_configured = bool(notify_channel and _get_discord_bot_token())

    return {
        "gateway": {
            "url": gateway_url,
            "connected": gateway_ok,
            "latency_ms": gateway_latency_ms,
        },
        "llm_mode": pipeline.get("llm_mode", "gateway"),
        "default_model": pipeline.get("default_llm", ""),
        "runs": {
            "total": total_runs,
            "active": active_runs,
            "completed": completed_runs,
        },
        "notifications": {
            "discord_configured": discord_configured,
            "notify_channel": notify_channel,
        },
        "auth_enabled": bool(os.environ.get("CONTENTPIPE_AUTH_TOKEN", "")),
        "version": "0.8.1",
    }


@router.get("/system/engines")
async def api_system_engines():
    """列出图片引擎及其可用状态"""
    from image_engines.engine_factory import list_engines
    from tools import load_pipeline_config
    config = load_pipeline_config()
    current = config.get("pipeline", {}).get("image_engine", "auto")
    return {
        "current": current,
        "engines": list_engines(),
    }


@router.post("/system/test-llm")
async def api_test_llm(request: Request):
    """测试 LLM 调用

    ```json
    {"model": "dashscope/qwen3.5-plus", "prompt": "说'OK'"}
    ```
    """
    body = await request.json()
    model = body.get("model", "")
    prompt = body.get("prompt", "Reply with exactly: OK")

    from tools import call_llm
    t0 = time.time()
    try:
        reply = call_llm(prompt, "", model=model or None, max_tokens=50)
        latency_ms = int((time.time() - t0) * 1000)
        return {
            "ok": True,
            "model": model,
            "reply": reply[:200],
            "latency_ms": latency_ms,
        }
    except Exception as e:
        return {
            "ok": False,
            "model": model,
            "error": str(e)[:300],
            "latency_ms": int((time.time() - t0) * 1000),
        }


@router.post("/system/test-notify")
async def api_test_notify():
    """发送测试通知到 Discord"""
    from web.notify import notify_discord
    ok = await notify_discord("🧪 ContentPipe 通知测试 — 配置正常")
    return {"ok": ok, "channel": os.environ.get("CONTENTPIPE_NOTIFY_CHANNEL", "")}


@router.get("/system/logs")
async def api_system_logs(limit: int = 50, level: str = ""):
    """获取最近日志"""
    log_path = Path("/tmp/contentpipe.log")
    if not log_path.exists():
        return {"logs": [], "count": 0}

    lines = log_path.read_text(encoding="utf-8", errors="replace").strip().split("\n")
    if level:
        lines = [l for l in lines if level.upper() in l]
    recent = lines[-limit:]
    return {"logs": recent, "count": len(recent), "total": len(lines)}
