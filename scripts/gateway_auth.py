from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_GATEWAY_TOKEN_CACHE: str | None = None


def get_gateway_token() -> str:
    global _GATEWAY_TOKEN_CACHE
    if _GATEWAY_TOKEN_CACHE is not None:
        return _GATEWAY_TOKEN_CACHE

    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if token:
        _GATEWAY_TOKEN_CACHE = token
        return token

    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    try:
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            token = str(cfg.get("gateway", {}).get("auth", {}).get("token", "")).strip()
    except Exception:
        token = ""

    _GATEWAY_TOKEN_CACHE = token
    return token


def build_gateway_headers(extra: dict[str, Any] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    token = get_gateway_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update({k: str(v) for k, v in extra.items()})
    return headers


def build_contentpipe_session_key(*parts: str) -> str:
    """构造稳定、可读的 Gateway session key。"""
    cleaned = []
    for part in parts:
        p = (part or "").strip().replace(" ", "-").replace(":", "-").replace("/", "-")
        if p:
            cleaned.append(p)
    return "contentpipe:" + ":".join(cleaned)


def build_contentpipe_node_session_key(run_id: str, node_id: str, lane: str = "main", generation: int = 0) -> str:
    """构造节点级 session key；generation 用于回退/重跑后切断旧上下文。"""
    parts = [run_id, node_id, lane]
    if generation > 0:
        parts.append(f"g{generation}")
    return build_contentpipe_session_key(*parts)
