"""环境变量 / .env.local 辅助工具。"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

import httpx

PLUGIN_DIR = Path(__file__).parent.parent.parent
ENV_LOCAL_PATH = PLUGIN_DIR / ".env.local"


def read_env_local(path: Path | None = None) -> dict[str, str]:
    """读取 .env.local（简单 KEY=VALUE 解析，忽略注释/空行）。"""
    env_path = path or ENV_LOCAL_PATH
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def get_env_value(key: str, default: str = "") -> str:
    """优先读当前进程环境，fallback 到 .env.local。"""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    return read_env_local().get(key, default)


def is_env_configured(key: str) -> bool:
    return bool(get_env_value(key, "").strip())


def masked_if_configured(key: str) -> str:
    return "*****" if is_env_configured(key) else ""


async def detect_public_ip() -> tuple[str | None, str | None]:
    """探测当前服务进程的出口 IP。使用多个 plain-text 服务，返回首个合法 IP。"""
    urls = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://api.ip.sb/ip",
    ]

    timeout = httpx.Timeout(3.0, connect=2.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                text = (resp.text or "").strip()
                if not text:
                    continue
                ipaddress.ip_address(text)
                return text, None
            except Exception:
                continue

    return None, "未能自动探测出口 IP。你可以直接和 OpenClaw 的 LLM 对话问：当前机器出口 IP 是多少？"
