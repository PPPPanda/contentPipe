"""
ContentPipe — 多平台通知集成

通过 OpenClaw Gateway 的 `openclaw message send` CLI 向任意已接入的频道推送 Pipeline 事件。
支持 Discord / 飞书 / KOOK / 企业微信等所有 OpenClaw 已接入平台。

配置格式（CONTENTPIPE_NOTIFY_CHANNEL 环境变量或 Web 设置页）:
  "<platform>:<target>"  — 推荐格式，明确指定平台和目标
    例: "feishu:oc_306c296f6726924d578289ecd911fbe9"
         "discord:1480223789626294466"
         "kook:3968623244573582"

  "<target>"             — 兼容旧格式（纯 target ID），默认走 discord
    例: "1480223789626294466" → discord

所有通知最终通过 `openclaw message send --channel <platform> --target <target>` 发送，
由 OpenClaw Gateway 负责平台路由和认证，ContentPipe 不感知具体平台 API。
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from logutil import get_logger

# 动态读取配置 — 支持运行时修改，无需重启
def _get_notify_channel() -> str:
    return os.environ.get("CONTENTPIPE_NOTIFY_CHANNEL", "") or _read_config_val("notify_channel", "")

def _get_public_base_url() -> str:
    return os.environ.get("CONTENTPIPE_PUBLIC_BASE_URL", "http://localhost:8765").rstrip("/")

def _read_config_val(key: str, default: str = "") -> str:
    """从 pipeline.yaml 读取配置值（轻量级，每次调用时读）"""
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent.parent / "config" / "pipeline.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            return str(cfg.get("pipeline", {}).get(key, default))
    except Exception:
        pass
    return default

logger = get_logger(__name__)


def _parse_channel_target(raw: str) -> Tuple[str, str]:
    """解析 'platform:target' 格式的通知频道配置。

    Returns:
        (platform, target) 元组

    Examples:
        "feishu:oc_xxx"           → ("feishu", "oc_xxx")
        "discord:1480223789626"   → ("discord", "1480223789626")
        "kook:123456"             → ("kook", "123456")
        "1480223789626"           → ("discord", "1480223789626")  # 兼容旧格式
        ""                        → ("", "")
    """
    if not raw:
        return ("", "")

    # 支持的平台名（与 openclaw message send --channel 参数一致）
    KNOWN_PLATFORMS = {
        "discord", "feishu", "kook", "telegram", "wecom",
        "slack", "whatsapp", "signal", "line", "googlechat",
        "irc", "imessage", "msteams", "mattermost",
    }

    if ":" in raw:
        platform, target = raw.split(":", 1)
        if platform.lower() in KNOWN_PLATFORMS:
            return (platform.lower(), target)

    # 兼容旧格式：纯 target ID，默认 discord
    return ("discord", raw)

# 节点 emoji
NODE_EMOJI = {
    "scout": "🔍",
    "researcher": "📚",
    "writer": "✍️",
    "de_ai_editor": "✏️",
    "director": "🎬",
    "director_refine": "🎨",
    "image_gen": "🖼️",
    "formatter": "📐",
    "publisher": "📤",
}


# ── 产物摘要生成器（纯 Python 解析，不走 LLM）──────────────────────

def _build_node_summary(run_id: str, node: str, state: Dict[str, Any]) -> str:
    """从 state / 正式产物文件生成人类可读摘要。

    每个节点的正式产物格式不同，直接解析文件或 state 字段。
    返回多行字符串，用于嵌入通知消息。
    """
    builders = {
        "scout": _summary_scout,
        "researcher": _summary_researcher,
        "writer": _summary_writer,
        "de_ai_editor": _summary_writer,  # 复用 writer 摘要
        "director": _summary_director,
        "formatter": _summary_formatter,
    }
    builder = builders.get(node)
    if not builder:
        # fallback: 只显示标题
        title = state.get("topic", {}).get("title", "")
        return f"标题: {title}" if title else ""
    try:
        return builder(run_id, state)
    except Exception as e:
        logger.warning("Summary build failed for %s/%s: %s", run_id, node, e)
        title = state.get("topic", {}).get("title", "")
        return f"标题: {title}" if title else ""


def _runs_dir() -> Path:
    return Path(__file__).parent.parent.parent / "output" / "runs"


def _summary_scout(run_id: str, state: Dict[str, Any]) -> str:
    """Scout 摘要: 标题、角度、参考文章数、关键词"""
    lines: List[str] = []
    # 优先从产物文件读取
    topic_path = _runs_dir() / run_id / "topic.yaml"
    topic_data = state.get("topic", {})
    if topic_path.exists():
        try:
            import yaml
            topic_data = yaml.safe_load(topic_path.read_text(encoding="utf-8")) or {}
            topic_data = topic_data.get("topic", topic_data)
        except Exception:
            pass

    title = topic_data.get("title", "")
    if title:
        lines.append(f"📌 标题: {title}")

    angle = topic_data.get("content_angle", "")
    if angle:
        lines.append(f"🎯 角度: {angle}")

    # 参考文章数
    refs = state.get("reference_articles") or []
    if not refs:
        # 从 topic.yaml 中读
        raw = {}
        if topic_path.exists():
            try:
                import yaml
                raw = yaml.safe_load(topic_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        refs = raw.get("reference_articles") or raw.get("reference_index", {}).get("all_links", [])
    if refs:
        lines.append(f"📎 参考文章: {len(refs)} 篇")

    # 关键词
    keywords = topic_data.get("required_keywords", []) or topic_data.get("keywords", [])
    if not keywords:
        ur = state.get("user_requirements", {}) if isinstance(state, dict) else {}
        keywords = ur.get("required_keywords", [])
    if keywords:
        lines.append(f"🏷️ 关键词: {', '.join(keywords[:6])}")

    return "\n".join(lines)


def _summary_researcher(run_id: str, state: Dict[str, Any]) -> str:
    """Researcher 摘要: 核查结果数、研究问题数、引用来源数"""
    lines: List[str] = []
    research_path = _runs_dir() / run_id / "research.yaml"
    data: Dict[str, Any] = {}
    if research_path.exists():
        try:
            import yaml
            data = yaml.safe_load(research_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass

    title = state.get("topic", {}).get("title", "")
    if title:
        lines.append(f"📌 标题: {title}")

    verifications = data.get("verification_results", [])
    if verifications:
        verified = sum(1 for v in verifications if v.get("status") == "verified")
        lines.append(f"✅ 核查结果: {verified}/{len(verifications)} 条已验证")

    findings = data.get("research_findings", [])
    if findings:
        lines.append(f"🔬 研究发现: {len(findings)} 条")

    # 统计独立来源数
    sources_set: set = set()
    for v in verifications:
        for s in v.get("sources", []):
            url = s.get("url", "")
            if url:
                sources_set.add(url)
    for f in findings:
        for s in f.get("sources", []):
            url = s.get("url", "")
            if url:
                sources_set.add(url)
    if sources_set:
        lines.append(f"📚 引用来源: {len(sources_set)} 个")

    return "\n".join(lines)


def _summary_writer(run_id: str, state: Dict[str, Any]) -> str:
    """Writer 摘要: 标题、字数、段落数"""
    lines: List[str] = []
    title = state.get("topic", {}).get("title", "")
    if title:
        lines.append(f"📌 标题: {title}")

    # 读取正式正文
    for fname in ("article_edited.md", "article_draft.md"):
        article_path = _runs_dir() / run_id / fname
        if article_path.exists():
            try:
                text = article_path.read_text(encoding="utf-8")
                char_count = len(text.strip())
                # 段落数 = 非空行中以 ## 开头的数量
                sections = [l for l in text.splitlines() if l.strip().startswith("## ")]
                lines.append(f"📝 字数: {char_count} 字")
                if sections:
                    lines.append(f"📑 章节: {len(sections)} 节")
                break
            except Exception:
                pass

    return "\n".join(lines)


def _summary_director(run_id: str, state: Dict[str, Any]) -> str:
    """Director 摘要: 配图数量、封面、风格"""
    lines: List[str] = []
    vp_path = _runs_dir() / run_id / "visual_plan.json"
    data: Dict[str, Any] = {}
    if vp_path.exists():
        try:
            data = json.loads(vp_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    title = state.get("topic", {}).get("title", "")
    if title:
        lines.append(f"📌 标题: {title}")

    style = data.get("style", "")
    if style:
        lines.append(f"🎨 风格: {style}")

    cover = data.get("cover", {})
    if cover.get("title"):
        lines.append(f"🖼️ 封面: {cover['title']}")

    placements = data.get("placements", [])
    if placements:
        lines.append(f"📸 配图: {len(placements)} 张")

    return "\n".join(lines)


def _summary_formatter(run_id: str, state: Dict[str, Any]) -> str:
    """Formatter 摘要: HTML 大小、图片数"""
    lines: List[str] = []
    title = state.get("topic", {}).get("title", "")
    if title:
        lines.append(f"📌 标题: {title}")

    html_path = _runs_dir() / run_id / "formatted.html"
    if html_path.exists():
        size_kb = html_path.stat().st_size / 1024
        lines.append(f"📐 HTML: {size_kb:.1f} KB")

    images_dir = _runs_dir() / run_id / "images"
    if images_dir.exists():
        img_count = len([f for f in images_dir.iterdir() if f.suffix in (".png", ".jpg", ".jpeg", ".webp")])
        if img_count:
            lines.append(f"🖼️ 图片: {img_count} 张")

    return "\n".join(lines)



async def notify_discord(
    message: str,
    *,
    channel: str = "",
    run_id: Optional[str] = None,
    node: Optional[str] = None,
    buttons: bool = False,
):
    """向配置的通知频道发送消息。

    统一通过 `openclaw message send` CLI 发送，支持所有已接入平台。
    函数名保留 notify_discord 以兼容现有调用方。

    Args:
        message: 消息内容
        channel: "platform:target" 格式（空则从配置/env 动态读取）
        run_id: 关联的 Run ID
        node: 当前节点（用于 emoji）
        buttons: 是否添加审核按钮（预留）
    """
    raw = channel or _get_notify_channel()
    if not raw:
        return False

    platform, target = _parse_channel_target(raw)
    if not platform or not target:
        logger.warning("Notify skipped: invalid channel config '%s'", raw)
        return False

    return await _send_via_cli(message, platform, target)


async def _send_via_cli(message: str, platform: str, target: str) -> bool:
    """通过 openclaw message send CLI 发送通知。"""
    import asyncio
    import subprocess as _sp

    cmd = [
        "openclaw", "message", "send",
        "--channel", platform,
        "--target", target,
        "-m", message,
        "--json",
    ]

    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _sp.run(cmd, capture_output=True, text=True, timeout=15),
        )
        if proc.returncode == 0:
            logger.info("Notify (%s → %s): ok", platform, target[:20])
            return True
        else:
            logger.warning("Notify failed (%s → %s): rc=%d stderr=%s",
                           platform, target[:20], proc.returncode, proc.stderr[:200])
            return False
    except Exception as e:
        logger.warning("Notify error (%s → %s): %s", platform, target[:20], e)
        return False


async def notify_node_complete(run_id: str, node: str, title: str = "", summary: str = ""):
    """节点执行完成通知"""
    emoji = NODE_EMOJI.get(node, "📌")
    msg = f"{emoji} **{node}** 完成"
    if title:
        msg += f"\n> {title}"
    if summary:
        msg += f"\n{summary[:200]}"
    msg += f"\n🔗 审核: {_get_public_base_url()}/runs/{run_id}/review?node={node}"
    await notify_discord(msg, run_id=run_id, node=node)


async def notify_review_needed(
    run_id: str,
    node: str,
    output_summary: str = "",
    state: Optional[Dict[str, Any]] = None,
):
    """需要人工审核通知（内嵌结构化摘要 + 审核指引）。

    Args:
        run_id: Run ID
        node: 当前节点 ID
        output_summary: 旧的纯文本摘要（兼容 fallback）
        state: Pipeline state dict（有则生成结构化摘要）
    """
    emoji = NODE_EMOJI.get(node, "📌")
    lines = [
        f"⏸️ **{emoji} {node} 等待审核**  `[REVIEW]`",
        f"`run_id: {run_id}` · `node: {node}`",
    ]

    # 结构化摘要（纯 Python 解析产物文件）
    if state:
        summary = _build_node_summary(run_id, node, state)
        if summary:
            lines.append("")
            for sl in summary.splitlines():
                lines.append(sl)
    elif output_summary:
        lines.append(f"> {output_summary[:300]}")

    lines.append("")
    lines.append(f"💬 直接回复审核意见 → `contentpipe_chat({run_id})`")
    lines.append(f"✅ 说「通过/OK」→ `contentpipe_approve({run_id})`")
    lines.append(f"🔗 网页审核: {_get_public_base_url()}/runs/{run_id}/review?node={node}")
    msg = "\n".join(lines)
    await notify_discord(msg, run_id=run_id, node=node, buttons=True)


async def notify_run_complete(run_id: str, title: str = ""):
    """Pipeline 运行完成通知"""
    msg = f"✅ **Pipeline 完成**"
    if title:
        msg += f": {title}"
    msg += f"\n📱 预览: {_get_public_base_url()}/runs/{run_id}/preview"
    await notify_discord(msg, run_id=run_id)


async def notify_run_failed(run_id: str, error: str = ""):
    """Pipeline 运行失败通知"""
    msg = f"❌ **Pipeline 失败**"
    if error:
        msg += f"\n```{error[:200]}```"
    msg += f"\n🔗 {_get_public_base_url()}/runs/{run_id}"
    await notify_discord(msg, run_id=run_id)
