#!/usr/bin/env python3
"""
热搜抓取 — 基于 Agent Reach 工具链

数据源优先级:
  1. 百度热搜 (直接 API，最稳定)
  2. Twitter/X (xreach CLI，全球科技趋势)
  3. 微博热搜 (直接 API)
  4. 知乎热榜 (直接 API)
  5. Jina Reader 兜底 (任意热榜页面)

用法:
  python3 hot_news.py                    # 全部源
  python3 hot_news.py --top 10           # 每源取前10
  python3 hot_news.py --sources baidu,twitter  # 指定源
  python3 hot_news.py --keywords "AI,科技"     # 额外 Twitter 搜索
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

from logutil import get_logger

logger = get_logger(__name__)

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

ALL_SOURCES = ["baidu", "twitter", "weibo", "zhihu", "tophub_weibo", "tophub_zhihu"]


# ── 百度热搜 (直接 API) ─────────────────────────────────────

def fetch_baidu(top: int = 30) -> list[dict]:
    try:
        with httpx.Client(timeout=10, proxy=None) as client:
            resp = client.get("https://top.baidu.com/api/board?platform=wise&tab=realtime", headers=HEADERS)
            if resp.status_code != 200:
                return []
            data = resp.json()
            cards = data.get("data", {}).get("cards", [])
            if not cards:
                return []
            items = cards[0].get("content", [])
            while items and isinstance(items[0], dict) and "content" in items[0] and isinstance(items[0]["content"], list):
                items = items[0]["content"]
            return [
                {"title": it["word"], "url": it.get("url", ""), "heat": it.get("hotScore", i), "platform": "baidu"}
                for i, it in enumerate(items[:top]) if isinstance(it, dict) and it.get("word")
            ]
    except Exception as e:
        logger.warning("百度: %s", e)
        return []


# ── Twitter/X (xreach CLI from agent-reach) ──────────────────

def fetch_twitter(top: int = 15, keywords: list[str] | None = None) -> list[dict]:
    """用 xreach CLI 搜索 Twitter 趋势"""
    results: list[dict] = []

    # 搜索中文科技热点
    queries = keywords or ["AI trending", "科技 热门"]
    for query in queries[:3]:
        try:
            proc = subprocess.run(
                ["xreach", "search", query, "--json", "-n", str(min(top, 10))],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                continue

            tweets = json.loads(proc.stdout) if proc.stdout.strip() else []
            if isinstance(tweets, dict):
                tweets = tweets.get("tweets", tweets.get("data", [tweets]))
            if not isinstance(tweets, list):
                continue

            for tweet in tweets[:top]:
                text = tweet.get("text", tweet.get("full_text", ""))
                if not text:
                    continue
                # 截取前 80 字作为标题
                title = text[:80].replace("\n", " ").strip()
                if len(text) > 80:
                    title += "..."
                results.append({
                    "title": title,
                    "url": tweet.get("url", tweet.get("tweet_url", "")),
                    "heat": tweet.get("like_count", tweet.get("favorite_count", 0)),
                    "platform": "twitter",
                    "author": tweet.get("user", {}).get("screen_name", tweet.get("username", "")),
                    "retweets": tweet.get("retweet_count", 0),
                })
        except FileNotFoundError:
            logger.warning("xreach 未安装，跳过 Twitter")
            return results
        except subprocess.TimeoutExpired:
            logger.warning("xreach 超时: %s", query)
        except Exception as e:
            logger.warning("Twitter (%s): %s", query, e)

    # 去重
    seen = set()
    deduped = []
    for r in results:
        key = r["title"][:40]
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped[:top]


# ── 微博热搜 (直接 API) ─────────────────────────────────────

def fetch_weibo(top: int = 20) -> list[dict]:
    try:
        with httpx.Client(timeout=10, proxy=None) as client:
            resp = client.get("https://weibo.com/ajax/side/hotSearch", headers=HEADERS)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [
                {
                    "title": it["word"],
                    "url": f"https://s.weibo.com/weibo?q=%23{it['word']}%23",
                    "heat": it.get("num", 0),
                    "platform": "weibo",
                    "label": it.get("label_name", ""),
                }
                for it in (data.get("data", {}).get("realtime", []) or [])[:top]
                if it.get("word")
            ]
    except Exception as e:
        logger.warning("微博: %s", e)
        return []


# ── 知乎热榜 (直接 API) ─────────────────────────────────────

def fetch_zhihu(top: int = 20) -> list[dict]:
    try:
        with httpx.Client(timeout=10, proxy=None) as client:
            resp = client.get("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total", headers=HEADERS)
            if resp.status_code != 200:
                return []
            results = []
            for item in (resp.json().get("data", []) or [])[:top]:
                target = item.get("target", {})
                if not target.get("title"):
                    continue
                heat = 0
                try:
                    heat = int("".join(c for c in item.get("detail_text", "0") if c.isdigit()) or "0")
                except ValueError:
                    pass
                results.append({
                    "title": target["title"],
                    "url": f"https://www.zhihu.com/question/{target.get('id', '')}",
                    "heat": heat,
                    "platform": "zhihu",
                    "excerpt": target.get("excerpt", "")[:100],
                })
            return results
    except Exception as e:
        logger.warning("知乎: %s", e)
        return []


# ── Jina Reader 兜底 ─────────────────────────────────────────

def fetch_via_jina(url: str, platform: str = "jina") -> list[dict]:
    """通过 Jina Reader 抓取任意热榜页面"""
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"https://r.jina.ai/{url}",
                headers={"Accept": "text/markdown", "User-Agent": "agent-reach/1.0"},
            )
            if resp.status_code != 200:
                return []
            import re
            results = []
            for line in resp.text.split("\n"):
                line = line.strip()
                m = re.match(r"^\d+[.、]\s*(.+)", line)
                if m:
                    results.append({
                        "title": m.group(1).strip()[:80],
                        "url": "",
                        "heat": 0,
                        "platform": platform,
                    })
            return results
    except Exception as e:
        logger.warning("Jina (%s): %s", url, e)
        return []


# ── 主函数 ────────────────────────────────────────────────────

def fetch_tophub(board_id: str, platform_name: str, top: int = 30) -> list[dict]:
    """通过 tophub.today + Jina Reader 抓取热榜（微博/知乎/抖音等通用）"""
    import re as _re
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"https://r.jina.ai/https://tophub.today/n/{board_id}",
                headers={"Accept": "text/markdown", "User-Agent": "agent-reach/1.0"},
            )
            if resp.status_code != 200:
                return []
            results = []
            for line in resp.text.split("\n"):
                line = line.strip()
                # 格式 A: 1.[标题](url)103万
                m = _re.match(r"^\d+\.\[(.+?)\]\((https?://[^\s)]+)\)(\d+万)?", line)
                # 格式 B (知乎): 1.![图片][标题](url)\n578 万热度
                if not m:
                    m = _re.match(r"^\d+\.(?:!\[.*?\]\(.*?\))?\[(.+?)\]\((https?://[^\s)]+)\)", line)
                if m:
                    title = m.group(1)
                    url = m.group(2)
                    heat_str = m.group(3) if m.lastindex and m.lastindex >= 3 else ""
                    heat = int((heat_str or "").replace("万", "")) * 10000 if heat_str else 0
                    results.append({
                        "title": title,
                        "url": url,
                        "heat": heat,
                        "platform": platform_name,
                    })
                    if len(results) >= top:
                        break
                # 热度单独一行: "578 万热度"
                elif results and _re.match(r"^(\d+)\s*万热度", line):
                    hm = _re.match(r"^(\d+)\s*万热度", line)
                    results[-1]["heat"] = int(hm.group(1)) * 10000
            return results
    except Exception as e:
        logger.warning("tophub (%s): %s", platform_name, e)
        return []


# tophub.today 的板块 ID
TOPHUB_BOARDS = {
    "tophub_weibo": ("KqndgxeLl9", "weibo"),       # 微博热搜
    "tophub_zhihu": ("mproPpoq6O", "zhihu"),       # 知乎热榜
    "tophub_douyin": ("DpQvNABoNE", "douyin"),      # 抖音热点
    "tophub_bilibili": ("74KjzEJeO5", "bilibili"),  # B站热门
}


FETCHERS = {
    "baidu": lambda top, kw: fetch_baidu(top),
    "twitter": lambda top, kw: fetch_twitter(top, kw),
    "weibo": lambda top, kw: fetch_weibo(top),
    "zhihu": lambda top, kw: fetch_zhihu(top),
    "tophub_weibo": lambda top, kw: fetch_tophub(*TOPHUB_BOARDS["tophub_weibo"], top),
    "tophub_zhihu": lambda top, kw: fetch_tophub(*TOPHUB_BOARDS["tophub_zhihu"], top),
    "tophub_douyin": lambda top, kw: fetch_tophub(*TOPHUB_BOARDS["tophub_douyin"], top),
    "tophub_bilibili": lambda top, kw: fetch_tophub(*TOPHUB_BOARDS["tophub_bilibili"], top),
}


def fetch_all(
    top: int = 20,
    sources: list[str] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, list[dict]]:
    sources = sources or ALL_SOURCES
    results: dict[str, list[dict]] = {}

    for src in sources:
        fetcher = FETCHERS.get(src)
        if fetcher:
            results[src] = fetcher(top, keywords)

    total = sum(len(v) for v in results.values())
    parts = [f"{k} {len(v)}" for k, v in results.items() if v]
    logger.info("热搜: %s 条 (%s)", total, ", ".join(parts) or "全部失败")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="热搜抓取 (Agent Reach)")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--sources", default=None, help="逗号分隔: baidu,twitter,weibo,zhihu")
    parser.add_argument("--keywords", default=None, help="Twitter 搜索关键词，逗号分隔")
    args = parser.parse_args()

    sources = args.sources.split(",") if args.sources else None
    keywords = args.keywords.split(",") if args.keywords else None

    data = fetch_all(args.top, sources, keywords)
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
