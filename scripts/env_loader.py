"""
从 OpenClaw 配置自动加载 API keys 到环境变量

Pipeline 启动时调用 load_keys_from_openclaw()，
自动把 OpenClaw 的 provider keys 映射到标准环境变量。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


OPENCLAW_CONFIG_PATHS = [
    Path.home() / ".clawdbot" / "openclaw.json",
    Path.home() / ".openclaw" / "openclaw.json",
]


def load_keys_from_openclaw() -> dict[str, str]:
    """
    从 OpenClaw 配置读取 provider API keys，设置为环境变量。

    映射:
      dashscope.apiKey → DASHSCOPE_API_KEY + DASHSCOPE_BASE_URL
      anthropic-sonnet.apiKey → ANTHROPIC_API_KEY
      minimax.apiKey → MINIMAX_API_KEY

    返回: {env_var: value} 所有设置的环境变量
    """
    config_path = None
    for p in OPENCLAW_CONFIG_PATHS:
        if p.exists():
            config_path = p
            break

    if not config_path:
        return {}

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    providers = config.get("models", {}).get("providers", {})
    keys_set: dict[str, str] = {}

    # DashScope
    ds = providers.get("dashscope", {})
    if ds.get("apiKey") and not os.environ.get("DASHSCOPE_API_KEY"):
        os.environ["DASHSCOPE_API_KEY"] = ds["apiKey"]
        keys_set["DASHSCOPE_API_KEY"] = ds["apiKey"][:12] + "..."
    if ds.get("baseUrl") and not os.environ.get("DASHSCOPE_BASE_URL"):
        os.environ["DASHSCOPE_BASE_URL"] = ds["baseUrl"]
        keys_set["DASHSCOPE_BASE_URL"] = ds["baseUrl"]

    # Anthropic (sonnet provider)
    anth = providers.get("anthropic-sonnet", {})
    if anth.get("apiKey") and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = anth["apiKey"]
        keys_set["ANTHROPIC_API_KEY"] = anth["apiKey"][:12] + "..."

    # Anthropic base URL (如果有自定义)
    if anth.get("baseUrl") and anth["baseUrl"] != "https://api.anthropic.com":
        os.environ["ANTHROPIC_BASE_URL"] = anth["baseUrl"]
        keys_set["ANTHROPIC_BASE_URL"] = anth["baseUrl"]

    # MiniMax
    mm = providers.get("minimax", {})
    if mm.get("apiKey") and not os.environ.get("MINIMAX_API_KEY"):
        os.environ["MINIMAX_API_KEY"] = mm["apiKey"]
        keys_set["MINIMAX_API_KEY"] = mm["apiKey"][:12] + "..."

    # Brave Search API
    web_search = config.get("tools", {}).get("web", {}).get("search", {})
    if web_search.get("apiKey") and not os.environ.get("BRAVE_API_KEY"):
        os.environ["BRAVE_API_KEY"] = web_search["apiKey"]
        keys_set["BRAVE_API_KEY"] = web_search["apiKey"][:12] + "..."

    return keys_set


if __name__ == "__main__":
    keys = load_keys_from_openclaw()
    if keys:
        print(f"✅ 从 OpenClaw 加载了 {len(keys)} 个 API keys:")
        for k, v in keys.items():
            print(f"  {k} = {v}")
    else:
        print("❌ 未找到 OpenClaw 配置或无可用 keys")
