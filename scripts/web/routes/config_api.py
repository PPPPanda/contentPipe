"""
ContentPipe — 配置管理 API

供 OpenClaw LLM 通过 AI 工具远程配置项目。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"
PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"


def _load_config() -> dict:
    """加载 pipeline.yaml + pipeline.local.yaml（合并）"""
    base_path = CONFIG_DIR / "pipeline.yaml"
    base = yaml.safe_load(base_path.read_text(encoding="utf-8")) if base_path.exists() else {}
    local_path = CONFIG_DIR / "pipeline.local.yaml"
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        base = _deep_merge(base or {}, local)
    return base or {}


def _save_local_config(config: dict) -> None:
    """写入 pipeline.local.yaml（用户个性化配置）"""
    local_path = CONFIG_DIR / "pipeline.local.yaml"
    local_path.write_text(
        yaml.dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ── 完整配置 ──────────────────────────────────────────────────

@router.get("/config")
async def api_get_config():
    """读取当前完整配置"""
    config = _load_config()
    pipeline = config.get("pipeline", {})
    return {
        "gateway_url": pipeline.get("gateway_url", "") or os.environ.get("OPENCLAW_GATEWAY_URL", ""),
        "llm_mode": pipeline.get("llm_mode", "gateway"),
        "default_llm": pipeline.get("default_llm", ""),
        "gateway_agent_id": pipeline.get("gateway_agent_id", "contentpipe-blank"),
        "llm_overrides": pipeline.get("llm_overrides", {}),
        "image_engine": pipeline.get("image_engine", "auto"),
        "notify_channel": os.environ.get("CONTENTPIPE_NOTIFY_CHANNEL", ""),
        "public_base_url": os.environ.get("CONTENTPIPE_PUBLIC_BASE_URL", ""),
        "wechat_author": config.get("wechat", {}).get("author", ""),
        "scout": config.get("scout", {}),
    }


@router.patch("/config")
async def api_patch_config(request: Request):
    """部分更新配置（deep merge 到 pipeline.local.yaml）

    示例请求:
    ```json
    {"default_llm": "dashscope/qwen3.5-plus", "llm_overrides": {"writer": "openai-codex/gpt-5.4"}}
    ```
    """
    body = await request.json()

    # 读取现有 local config
    local_path = CONFIG_DIR / "pipeline.local.yaml"
    local = {}
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}

    pipeline = local.setdefault("pipeline", {})

    # 映射：顶层 key → pipeline.yaml 结构
    simple_keys = {
        "default_llm", "gateway_url", "llm_mode", "gateway_agent_id", "image_engine",
    }
    for key in simple_keys:
        if key in body:
            pipeline[key] = body[key]

    if "llm_overrides" in body:
        existing = pipeline.get("llm_overrides", {})
        if isinstance(body["llm_overrides"], dict):
            existing.update(body["llm_overrides"])
            pipeline["llm_overrides"] = existing

    if "wechat_author" in body:
        local.setdefault("wechat", {})["author"] = body["wechat_author"]

    if "scout" in body and isinstance(body["scout"], dict):
        local["scout"] = _deep_merge(local.get("scout", {}), body["scout"])

    _save_local_config(local)

    # 更新环境变量（立即生效，不需要重启）
    if "notify_channel" in body:
        os.environ["CONTENTPIPE_NOTIFY_CHANNEL"] = str(body["notify_channel"])
    if "public_base_url" in body:
        os.environ["CONTENTPIPE_PUBLIC_BASE_URL"] = str(body["public_base_url"])

    return {"ok": True, "message": "Config updated", "updated_keys": list(body.keys())}


# ── 模型配置 ──────────────────────────────────────────────────

@router.get("/config/models")
async def api_get_models():
    """列出各角色当前使用的模型"""
    config = _load_config()
    pipeline = config.get("pipeline", {})
    default = pipeline.get("default_llm", "")
    overrides = pipeline.get("llm_overrides", {})

    roles = ["scout", "researcher", "writer", "de_ai_editor", "director", "director_refine"]
    models = {}
    for role in roles:
        models[role] = overrides.get(role, "") or default or "(未配置)"

    return {
        "default_llm": default,
        "roles": models,
        "overrides": overrides,
    }


@router.put("/config/models")
async def api_set_models(request: Request):
    """设置模型配置

    示例:
    ```json
    {
      "default_llm": "dashscope/qwen3.5-plus",
      "overrides": {
        "writer": "openai-codex/gpt-5.4",
        "director": "anthropic/claude-opus-4-6"
      }
    }
    ```
    或者全部使用默认:
    ```json
    {"default_llm": "dashscope/qwen3.5-plus", "overrides": {}}
    ```
    """
    body = await request.json()

    local_path = CONFIG_DIR / "pipeline.local.yaml"
    local = {}
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}

    pipeline = local.setdefault("pipeline", {})

    if "default_llm" in body:
        pipeline["default_llm"] = body["default_llm"]
    if "overrides" in body:
        pipeline["llm_overrides"] = body["overrides"]

    _save_local_config(local)
    return {"ok": True, "message": "Models updated"}


# ── 通知频道 ──────────────────────────────────────────────────

@router.get("/config/notify")
async def api_get_notify():
    """获取通知配置"""
    return {
        "notify_channel": os.environ.get("CONTENTPIPE_NOTIFY_CHANNEL", ""),
        "discord_bot_token_set": bool(os.environ.get("DISCORD_BOT_TOKEN", "").strip()
                                      or _has_openclaw_discord_token()),
        "public_base_url": os.environ.get("CONTENTPIPE_PUBLIC_BASE_URL", ""),
    }


@router.put("/config/notify")
async def api_set_notify(request: Request):
    """设置通知频道

    ```json
    {"notify_channel": "1480223789626294466", "public_base_url": "https://my-server:8765"}
    ```
    """
    body = await request.json()
    updated = []

    if "notify_channel" in body:
        os.environ["CONTENTPIPE_NOTIFY_CHANNEL"] = str(body["notify_channel"])
        _update_env_local("CONTENTPIPE_NOTIFY_CHANNEL", str(body["notify_channel"]))
        updated.append("notify_channel")

    if "public_base_url" in body:
        os.environ["CONTENTPIPE_PUBLIC_BASE_URL"] = str(body["public_base_url"])
        _update_env_local("CONTENTPIPE_PUBLIC_BASE_URL", str(body["public_base_url"]))
        updated.append("public_base_url")

    return {"ok": True, "updated": updated}


# ── 图片引擎 ──────────────────────────────────────────────────

@router.get("/config/image-engine")
async def api_get_image_engine():
    """获取图片引擎配置"""
    from image_engines.engine_factory import list_engines
    config = _load_config()
    current = config.get("pipeline", {}).get("image_engine", "auto")
    engines = list_engines()
    return {
        "current": current,
        "available": engines,
    }


@router.put("/config/image-engine")
async def api_set_image_engine(request: Request):
    """设置图片引擎

    ```json
    {"engine": "dall-e-3"}
    ```
    可选: "auto", "pollinations", "dall-e-3", "dashscope", "browser:jimeng"
    """
    body = await request.json()
    engine = body.get("engine", "auto")

    local_path = CONFIG_DIR / "pipeline.local.yaml"
    local = {}
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}

    local.setdefault("pipeline", {})["image_engine"] = engine
    _save_local_config(local)
    return {"ok": True, "engine": engine}


# ── Prompt 管理 ──────────────────────────────────────────────

@router.get("/config/prompts")
async def api_list_prompts():
    """列出所有 prompt 文件"""
    prompts = []
    for f in sorted(PROMPTS_DIR.glob("*.md")):
        content = f.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        first_line = lines[0].strip().lstrip("#").strip() if lines else ""
        prompts.append({
            "name": f.name,
            "title": first_line,
            "size": len(content),
            "lines": len(lines),
        })
    return {"prompts": prompts}


@router.get("/config/prompts/{name}")
async def api_get_prompt(name: str):
    """读取指定 prompt 全文"""
    path = PROMPTS_DIR / name
    if not path.exists() or not path.suffix == ".md":
        raise HTTPException(status_code=404, detail=f"Prompt not found: {name}")
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@router.put("/config/prompts/{name}")
async def api_set_prompt(name: str, request: Request):
    """更新 prompt 内容"""
    body = await request.json()
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    path = PROMPTS_DIR / name
    if not name.endswith(".md"):
        raise HTTPException(status_code=400, detail="Prompt name must end with .md")

    # 保存 .prev 备份
    if path.exists():
        prev_path = PROMPTS_DIR / f"{name}.prev"
        prev_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    path.write_text(content, encoding="utf-8")
    return {"ok": True, "name": name, "size": len(content)}


# ── 辅助函数 ──────────────────────────────────────────────────

def _has_openclaw_discord_token() -> bool:
    try:
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if cfg_path.exists():
            import json
            cfg = json.loads(cfg_path.read_text())
            return bool(cfg.get("channels", {}).get("discord", {}).get("token", ""))
    except Exception:
        pass
    return False


def _update_env_local(key: str, value: str) -> None:
    """更新 .env.local 中的指定变量"""
    env_path = Path(__file__).parent.parent.parent.parent / ".env.local"
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
