"""
引擎工厂 — 根据配置创建图片生成引擎

支持：
  - 按名称创建: create_engine("pollinations")
  - 按配置创建: create_engine_from_config()
  - 自动降级: Pollinations(默认) → DALL-E 3 → DashScope → 浏览器

优先级说明:
  Pollinations 是免费的，作为默认首选。
  付费 API (DALL-E 3 / DashScope) 质量更高但需要 key。
  浏览器模式(即梦) 质量最好但最慢且不稳定。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .base import ImageEngine
from .api_pollinations import PollinationsEngine
from .api_dalle import DallE3Engine
from .api_dashscope import DashScopeEngine
from .browser_engine import BrowserEngine, SITE_CONFIGS


# 引擎注册表
ENGINE_REGISTRY: dict[str, type[ImageEngine]] = {
    "pollinations": PollinationsEngine,
    "dall-e-3": DallE3Engine,
    "dashscope": DashScopeEngine,
}

# 浏览器引擎名称映射
BROWSER_SITES = list(SITE_CONFIGS.keys())


def create_engine(name: str, **kwargs) -> ImageEngine:
    """
    按名称创建引擎

    Args:
        name: 引擎名称
            API: "pollinations", "dall-e-3", "dashscope"
            浏览器: "browser:jimeng", "browser:tongyi"
    """
    if name.startswith("browser:"):
        site = name.split(":", 1)[1]
        return BrowserEngine(site=site, **kwargs)

    if name == "browser":
        return BrowserEngine(site="jimeng", **kwargs)

    if name in ENGINE_REGISTRY:
        return ENGINE_REGISTRY[name](**kwargs)

    raise ValueError(
        f"Unknown engine: {name}. "
        f"Available: {list(ENGINE_REGISTRY.keys()) + [f'browser:{s}' for s in BROWSER_SITES]}"
    )


def create_engine_from_config(config: dict | None = None) -> ImageEngine:
    """
    从 pipeline.yaml 配置创建引擎

    配置示例:
      pipeline:
        image_engine: "pollinations"     # 免费默认
        image_engine: "dall-e-3"         # 付费高质量
        image_engine: "browser:jimeng"   # 浏览器自动化
        image_engine: "auto"             # 自动选择
    """
    if config is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "pipeline.yaml"
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        else:
            config = {}

    engine_name = config.get("pipeline", {}).get("image_engine", "auto")

    if engine_name == "auto":
        return _auto_select_engine()

    return create_engine(engine_name)


def _auto_select_engine() -> ImageEngine:
    """
    自动选择可用引擎

    优先级: Pollinations(免费) → DALL-E 3 → DashScope → 浏览器(即梦)
    """
    # 优先级 1: Pollinations（永远可用）
    return PollinationsEngine()


def list_engines() -> list[dict]:
    """列出所有可用引擎及其状态"""
    engines = []

    for name, cls in ENGINE_REGISTRY.items():
        instance = cls()
        engines.append({
            "name": name,
            "mode": "api",
            "available": instance.is_available(),
        })

    for site_name in SITE_CONFIGS:
        engines.append({
            "name": f"browser:{site_name}",
            "mode": "browser",
            "available": None,
        })

    return engines
