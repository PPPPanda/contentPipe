"""
ContentPipe Tools — 外部工具调用封装

每个工具封装为独立函数，供节点调用。
生产环境替换 TODO 占位实现。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import yaml

from cli_utils import parse_cli_json
from gateway_auth import build_gateway_headers
from logutil import get_logger

# ── 自动加载 API keys ────────────────────────────────────────

from env_loader import load_keys_from_openclaw
_loaded_keys = load_keys_from_openclaw()

logger = get_logger(__name__)

# ── 配置 ──────────────────────────────────────────────────────

CONFIG_DIR = Path(__file__).parent.parent / "config"


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 覆盖 base"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_pipeline_config() -> dict:
    """加载 pipeline.yaml + pipeline.local.yaml（本地覆盖）"""
    config = {}
    config_path = CONFIG_DIR / "pipeline.yaml"
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # 本地覆盖（不提交到 git）
    local_path = CONFIG_DIR / "pipeline.local.yaml"
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        config = _deep_merge(config, local)

    return config


# ── LLM 调用 ─────────────────────────────────────────────────

def call_llm(
    prompt: str,
    context: str,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    response_format: str | None = None,
    chat_history: list[dict] | None = None,
    system_prompt: str | None = None,
    gateway_session_key: str | None = None,
    gateway_agent_id: str | None = None,
) -> str:
    """
    调用 LLM。

    约定：
    - 当 `context` 非空时：`prompt` 视为 system/instruction，`context` 视为当前 user message
    - 当 `context` 为空时：`prompt` 视为当前 user message
    """
    config = load_pipeline_config()
    pipeline_config = config.get("pipeline", {})
    model = model or pipeline_config.get("default_llm", "anthropic/claude-sonnet-4-6")
    llm_mode = pipeline_config.get("llm_mode", "gateway")

    user_prompt = context if context else prompt
    effective_system_prompt = system_prompt if system_prompt is not None else (prompt if context else None)

    if llm_mode == "gateway":
        return _call_via_gateway(
            model=model,
            prompt=user_prompt,
            system_prompt=effective_system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            gateway_url=pipeline_config.get("gateway_url", "http://localhost:18789"),
            chat_history=chat_history,
            gateway_session_key=gateway_session_key,
            gateway_agent_id=gateway_agent_id,
            timeout_seconds=int(pipeline_config.get("gateway_timeout_seconds", 1800)),
        )

    if "/" in model:
        provider, model_name = model.split("/", 1)
    else:
        provider, model_name = "openai", model

    if provider in ("openai", "dashscope"):
        return _call_openai_compatible(
            model_name=model_name,
            prompt=user_prompt,
            system_prompt=effective_system_prompt,
            provider=provider,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            chat_history=chat_history,
        )
    elif provider == "anthropic":
        return _call_anthropic(
            model_name=model_name,
            prompt=user_prompt,
            system_prompt=effective_system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            chat_history=chat_history,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")


def build_gateway_openai_compat_target(
    requested_model: str,
    gateway_agent_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    """将旧的 provider/model 调用转换为新 Gateway agent-first 协议。"""
    requested_model = (requested_model or "").strip()
    extra_headers: dict[str, str] = {}

    if gateway_agent_id:
        extra_headers["X-OpenClaw-Agent-Id"] = gateway_agent_id

    lowered = requested_model.lower()
    is_agent_target = (
        lowered == "openclaw"
        or lowered == "openclaw/default"
        or lowered.startswith("openclaw/")
        or lowered.startswith("openclaw:")
        or lowered.startswith("agent:")
    )

    if is_agent_target:
        compat_model = requested_model
    else:
        compat_model = f"openclaw/{gateway_agent_id}" if gateway_agent_id else "openclaw/default"
        if requested_model:
            extra_headers["X-OpenClaw-Model"] = requested_model

    return compat_model, extra_headers


def _call_via_gateway(
    model: str,
    prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    response_format: str | None = None,
    gateway_url: str = "http://localhost:18789",
    chat_history: list[dict] | None = None,
    system_prompt: str | None = None,
    gateway_session_key: str | None = None,
    gateway_agent_id: str | None = None,
    timeout_seconds: int = 1800,
) -> str:
    """通过 OpenClaw Gateway 调用 LLM。"""
    compat_model, compat_headers = build_gateway_openai_compat_target(model, gateway_agent_id)
    extra_headers = dict(compat_headers)
    if gateway_session_key:
        extra_headers["X-OpenClaw-Session-Key"] = gateway_session_key
    headers = build_gateway_headers(extra_headers or None)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if chat_history:
        for msg in chat_history:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {
        "model": compat_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if response_format == "json":
        body["response_format"] = {"type": "json_object"}

    with httpx.Client(timeout=timeout_seconds) as client:
        resp = client.post(f"{gateway_url}/v1/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def _call_openai_compatible(
    model_name: str,
    prompt: str,
    provider: str = "openai",
    temperature: float = 0.7,
    max_tokens: int = 4096,
    response_format: str | None = None,
    chat_history: list[dict] | None = None,
    system_prompt: str | None = None,
) -> str:
    """OpenAI 兼容格式调用（OpenAI / DashScope）"""
    if provider == "dashscope":
        base_url = os.environ.get("DASHSCOPE_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1")
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    else:
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        api_key = os.environ.get("OPENAI_API_KEY", "")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if chat_history:
        for msg in chat_history:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if response_format == "json":
        body["response_format"] = {"type": "json_object"}

    proxy = None if provider == "dashscope" else os.environ.get("HTTPS_PROXY")
    with httpx.Client(timeout=120, proxy=proxy) as client:
        resp = client.post(f"{base_url}/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def _call_anthropic(
    model_name: str,
    prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    system_prompt: str | None = None,
    chat_history: list[dict] | None = None,
) -> str:
    """Anthropic Claude 调用"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    messages = []
    if chat_history:
        for msg in chat_history:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {
        "model": model_name,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system_prompt:
        body["system"] = system_prompt

    with httpx.Client(timeout=120) as client:
        resp = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


# ── 搜索工具 ─────────────────────────────────────────────────

def search_web(query: str, count: int = 10) -> list[dict]:
    """
    网络搜索（Brave Search）

    返回: [{title, url, description}, ...]
    """
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        # Fallback: 返回空结果
        return []

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    params = {"q": query, "count": count}

    with httpx.Client(timeout=30) as client:
        resp = client.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            })
        return results


def search_perplexity(query: str, model: str = "sonar") -> str:
    """
    Perplexity 深度搜索

    返回: 搜索结果文本（含引用）
    """
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        return ""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
    }

    with httpx.Client(timeout=60) as client:
        resp = client.post("https://api.perplexity.ai/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def fetch_url(url: str, max_chars: int = 10000) -> str:
    """抓取 URL 内容（纯文本）"""
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        text = resp.text[:max_chars]
        return text


def fetch_wechat_article(url: str) -> dict:
    """
    提取微信公众号文章正文（Playwright headless）。
    首次调用自动安装 playwright + chromium。
    返回 {success, title, author, publish_time, content, word_count, error}
    """
    # 复用 wechat-article-reader skill 的逻辑
    import sys
    skill_scripts = str(Path(__file__).parent.parent.parent / "wechat-article-reader" / "scripts")
    if skill_scripts not in sys.path:
        sys.path.insert(0, skill_scripts)
    try:
        from fetch_article import fetch_wechat_article as _fetch
        return _fetch(url)
    except ImportError:
        return {"success": False, "error": "wechat-article-reader skill 未安装", "content": ""}


def is_wechat_url(url: str) -> bool:
    """判断是否为微信公众号文章链接"""
    return "mp.weixin.qq.com" in url


# ── 社交平台搜索（agent-reach） ──────────────────────────────

def search_social(query: str, platforms: list[str] | None = None) -> dict[str, list[dict]]:
    """
    通过 agent-reach 工具链搜索多个社交平台。

    platforms 可选: twitter, xiaohongshu, bilibili, douyin, youtube, github
    默认搜索: twitter, xiaohongshu

    返回: {platform: [{title, url, content, engagement}, ...]}
    """
    if platforms is None:
        platforms = ["twitter", "xiaohongshu"]

    results: dict[str, list[dict]] = {}

    for plat in platforms:
        try:
            items = _search_platform(plat, query)
            if items:
                results[plat] = items
        except Exception as e:
            logger.warning("%s 搜索失败: %s", plat, e)
            results[plat] = []

    return results


def _search_platform(platform: str, query: str) -> list[dict]:
    """单平台搜索"""
    import subprocess
    # mcporter 需要在 workspace 根目录找配置
    _WORKSPACE_ROOT = str(Path(__file__).parent.parent.parent.parent)

    if platform == "twitter":
        # xreach search — 需要 auth token 才能搜索
        try:
            r = subprocess.run(
                ["xreach", "search", query, "--json", "-n", "10"],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0 and r.stdout.strip():
                # 检查是否是认证失败
                if "Not authenticated" in r.stdout or "Not authenticated" in r.stderr:
                    logger.warning("Twitter 未认证，跳过")
                    return []
                data = parse_cli_json(r.stdout)
                items = data if isinstance(data, list) else data.get("tweets", data.get("results", []))
                return [
                    {
                        "title": t.get("text", "")[:100],
                        "url": t.get("url", t.get("link", "")),
                        "content": t.get("text", "")[:300],
                        "author": t.get("author", t.get("user", {}).get("name", "")),
                        "engagement": t.get("likes", 0) + t.get("retweets", 0),
                    }
                    for t in items[:10]
                ]
            elif "Not authenticated" in (r.stderr or ""):
                logger.warning("Twitter 未认证，跳过")
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return []

    elif platform == "xiaohongshu":
        # mcporter call xiaohongshu.search_feeds
        try:
            r = subprocess.run(
                ["mcporter", "call", f'xiaohongshu.search_feeds(keyword: "{query}")'],
                capture_output=True, text=True, timeout=180, cwd=_WORKSPACE_ROOT,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = parse_cli_json(r.stdout)
                feeds = data.get("feeds", data.get("items", []))
                if isinstance(feeds, list):
                    return [
                        {
                            "title": f.get("noteCard", {}).get("displayTitle", "")[:100],
                            "url": f"https://www.xiaohongshu.com/explore/{f.get('id', '')}",
                            "content": f.get("noteCard", {}).get("desc", "")[:300],
                            "author": f.get("noteCard", {}).get("user", {}).get("nickname", ""),
                            "engagement": int(f.get("noteCard", {}).get("interactInfo", {}).get("likedCount", 0) or 0),
                        }
                        for f in feeds[:10]
                    ]
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return []

    elif platform == "bilibili":
        # Jina Reader + Bilibili 搜索
        try:
            encoded = query.replace(" ", "+")
            url = f"https://search.bilibili.com/all?keyword={encoded}"
            with httpx.Client(timeout=180, proxy=None) as client:
                resp = client.get(
                    f"https://r.jina.ai/{url}",
                    headers={"Accept": "text/markdown", "User-Agent": "agent-reach/1.0"},
                )
                if resp.status_code == 200:
                    text = resp.text[:3000]
                    # 从 markdown 提取标题+链接
                    import re
                    links = re.findall(r'\[([^\]]+)\]\((https?://www\.bilibili\.com/video/[^\)]+)\)', text)
                    return [
                        {"title": title[:100], "url": url, "content": "", "engagement": 0}
                        for title, url in links[:10]
                    ]
        except Exception:
            pass
        return []

    elif platform == "douyin":
        # mcporter call douyin
        try:
            r = subprocess.run(
                ["mcporter", "call", f'douyin.search_douyin_videos(keyword: "{query}")'],
                capture_output=True, text=True, timeout=180, cwd=_WORKSPACE_ROOT,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = parse_cli_json(r.stdout)
                items = data if isinstance(data, list) else data.get("videos", [])
                return [
                    {
                        "title": v.get("title", "")[:100],
                        "url": v.get("share_url", v.get("url", "")),
                        "content": v.get("desc", "")[:300],
                        "author": v.get("author", ""),
                        "engagement": v.get("digg_count", 0),
                    }
                    for v in (items[:10] if isinstance(items, list) else [])
                ]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return []

    elif platform == "youtube":
        # yt-dlp ytsearch
        try:
            r = subprocess.run(
                ["yt-dlp", "--dump-json", f"ytsearch5:{query}"],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0 and r.stdout.strip():
                items = []
                for line in r.stdout.strip().split("\n"):
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                return [
                    {
                        "title": v.get("title", "")[:100],
                        "url": v.get("webpage_url", ""),
                        "content": v.get("description", "")[:300],
                        "author": v.get("uploader", ""),
                        "engagement": v.get("view_count", 0),
                    }
                    for v in items[:5]
                ]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return []

    elif platform == "github":
        try:
            r = subprocess.run(
                ["gh", "search", "repos", query, "--sort", "stars", "--limit", "5", "--json",
                 "name,url,description,stargazersCount"],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0 and r.stdout.strip():
                items = parse_cli_json(r.stdout)
                return [
                    {
                        "title": repo.get("name", ""),
                        "url": repo.get("url", ""),
                        "content": repo.get("description", "")[:300],
                        "engagement": repo.get("stargazersCount", 0),
                    }
                    for repo in items[:5]
                ]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return []

    return []


# ── 热搜工具 ─────────────────────────────────────────────────

def fetch_hotnews() -> dict[str, list[dict]]:
    """
    获取多平台热搜

    返回: {platform: [{title, url, heat}, ...]}

    数据源优先级:
    1. 百度热搜 (最稳定，直接 API)
    2. 微博热搜 (weibo.com/ajax/side/hotSearch)
    3. 知乎热榜 (API)
    4. Jina Reader 抓取 tophub.today 综合热搜
    """
    results: dict[str, list[dict]] = {
        "weibo": [],
        "zhihu": [],
        "baidu": [],
        "aggregated": [],
    }

    headers_common = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    # ── 百度热搜 (最稳定) ──
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://top.baidu.com/api/board?platform=wise&tab=realtime",
                headers=headers_common,
            )
            if resp.status_code == 200:
                data = resp.json()
                cards = data.get("data", {}).get("cards", [])
                if cards:
                    # 百度 API 三层嵌套: cards[0]["content"][0]["content"] → items
                    inner = cards[0].get("content", [])
                    items = inner
                    # 解开嵌套层
                    while items and isinstance(items[0], dict) and "content" in items[0] and isinstance(items[0]["content"], list):
                        items = items[0]["content"]

                    for item in items[:30]:
                        if isinstance(item, dict) and item.get("word"):
                            results["baidu"].append({
                                "title": item["word"],
                                "url": item.get("url", f"https://www.baidu.com/s?wd={item['word']}"),
                                "heat": item.get("hotScore", item.get("index", 0)),
                            })
    except Exception as e:
        logger.warning("百度热搜失败: %s", e)

    # ── 微博热搜 ──
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://weibo.com/ajax/side/hotSearch",
                headers=headers_common,
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in (data.get("data", {}).get("realtime", []) or [])[:20]:
                    if item.get("word"):
                        results["weibo"].append({
                            "title": item["word"],
                            "url": f"https://s.weibo.com/weibo?q=%23{item['word']}%23",
                            "heat": item.get("num", 0),
                            "label": item.get("label_name", ""),
                        })
    except Exception as e:
        logger.warning("微博热搜失败: %s", e)

    # ── 知乎热榜 ──
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total",
                headers=headers_common,
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in (data.get("data", []) or [])[:20]:
                    target = item.get("target", {})
                    if target.get("title"):
                        heat_text = item.get("detail_text", "0")
                        # 解析 "1234 万热度" 格式
                        heat = 0
                        try:
                            heat = int("".join(c for c in heat_text if c.isdigit()) or "0")
                        except ValueError:
                            pass
                        results["zhihu"].append({
                            "title": target["title"],
                            "url": f"https://www.zhihu.com/question/{target.get('id', '')}",
                            "heat": heat,
                            "excerpt": target.get("excerpt", "")[:100],
                        })
    except Exception as e:
        logger.warning("知乎热榜失败: %s", e)

    # ── Jina Reader 兜底 (抓取综合热搜) ──
    if not any(results[k] for k in ["weibo", "baidu", "zhihu"]):
        logger.info("主要热搜源均失败，尝试 Jina Reader 兜底...")
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    "https://r.jina.ai/https://top.baidu.com/board?tab=realtime",
                    headers={"Accept": "text/markdown", "User-Agent": "agent-reach/1.0"},
                )
                if resp.status_code == 200:
                    import re
                    lines = resp.text.split("\n")
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith(("#", "!", "[", "|", "---")):
                            # 尝试提取热搜条目
                            match = re.match(r"^\d+[.、]\s*(.+)", line)
                            if match:
                                results["aggregated"].append({
                                    "title": match.group(1).strip()[:80],
                                    "url": "",
                                    "heat": 0,
                                    "platform": "jina_fallback",
                                })
        except Exception as e:
            logger.warning("Jina 兜底也失败: %s", e)

    # ── 统计 ──
    total = sum(len(v) for v in results.values())
    sources = []
    for k, v in results.items():
        if v:
            sources.append(f"{k} {len(v)}")
    logger.info("热搜抓取: %s 条 (%s)", total, ", ".join(sources) or "全部失败")

    return results


# ── 图片生成工具 ──────────────────────────────────────────────

def generate_image(
    prompt: str,
    negative_prompt: str = "",
    size: str = "1792x1024",
    engine: str = "dall-e-3",
    seed: int | None = None,
) -> bytes:
    """
    生成图片

    返回: 图片 bytes

    TODO: 接入实际图片生成 API
    - DALL-E 3: OpenAI API
    - Stable Diffusion: 本地或 API
    - 魔搭: MCP 协议
    """
    if engine == "dall-e-3":
        return _generate_dalle3(prompt, size)
    else:
        raise ValueError(f"Unknown engine: {engine}")


def _generate_dalle3(prompt: str, size: str = "1792x1024") -> bytes:
    """DALL-E 3 图片生成"""
    api_key = os.environ.get("OPENAI_API_KEY", "")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": "dall-e-3",
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "b64_json",
    }

    with httpx.Client(timeout=120) as client:
        resp = client.post("https://api.openai.com/v1/images/generations", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        import base64
        return base64.b64decode(data["data"][0]["b64_json"])


# ── 发布工具 ──────────────────────────────────────────────────

def wechat_get_token(appid: str, secret: str) -> str:
    """获取微信公众号 access_token"""
    url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"
    with httpx.Client(timeout=10) as client:
        resp = client.get(url)
        data = resp.json()
        if "access_token" in data:
            return data["access_token"]
        raise RuntimeError(f"WeChat token error: {data}")


def wechat_upload_image(access_token: str, image_bytes: bytes, filename: str = "image.png") -> str:
    """上传图片到微信 CDN，返回 URL"""
    url = f"https://api.weixin.qq.com/cgi-bin/media/uploadimg?access_token={access_token}"
    files = {"media": (filename, image_bytes, "image/png")}
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, files=files)
        data = resp.json()
        if "url" in data:
            return data["url"]
        raise RuntimeError(f"WeChat upload error: {data}")


def wechat_upload_permanent_image(access_token: str, image_bytes: bytes, filename: str = "cover.png") -> str:
    """上传永久图片素材，返回 media_id（用于 thumb_media_id）"""
    url = f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={access_token}&type=image"
    files = {"media": (filename, image_bytes, "image/png")}
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, files=files)
        data = resp.json()
        if "media_id" in data:
            return data["media_id"]
        raise RuntimeError(f"WeChat permanent image upload error: {data}")


def wechat_create_draft(access_token: str, article: dict) -> str:
    """创建微信公众号草稿，返回 media_id"""
    url = f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={access_token}"
    body = {
        "articles": [
            {
                "title": article.get("title", ""),
                "content": article.get("content_html", ""),
                "digest": article.get("subtitle", ""),
                "author": article.get("author", "ContentPipe"),
                "thumb_media_id": article.get("thumb_media_id", ""),
                "content_source_url": "",
                "need_open_comment": 0,
            }
        ]
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=body)
        data = resp.json()
        if "media_id" in data:
            return data["media_id"]
        raise RuntimeError(f"WeChat draft error: {data}")
