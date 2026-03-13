"""
ContentPipe — Discord 通知集成

通过 OpenClaw Gateway 的 message API 向 Discord 频道推送 Pipeline 事件。
"""

from __future__ import annotations

import os
import json
import httpx
from typing import Optional

from gateway_auth import build_gateway_headers
from logutil import get_logger

# 动态读取配置 — 支持运行时修改，无需重启
def _get_gateway_url() -> str:
    return os.environ.get("OPENCLAW_GATEWAY_URL", "") or _read_config_val("gateway_url", "http://localhost:18789")

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


async def notify_discord(
    message: str,
    *,
    channel: str = "",
    run_id: Optional[str] = None,
    node: Optional[str] = None,
    buttons: bool = False,
):
    """向 Discord 发送通知。

    Args:
        message: 消息内容
        channel: 目标频道 ID（空则从配置/env 动态读取）
        run_id: 关联的 Run ID（用于按钮回调）
        node: 当前节点（用于 emoji）
        buttons: 是否添加审核按钮
    """
    if not channel:
        channel = _get_notify_channel()
    if not channel:
        return False
    gateway_url = _get_gateway_url()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            payload = {
                "action": "send",
                "channel": "discord",
                "target": f"channel:{channel}",
                "message": message,
            }

            resp = await client.post(
                f"{gateway_url}/api/message",
                json=payload,
                headers=build_gateway_headers(),
            )
            return resp.status_code == 200
    except Exception as e:
        logger.warning("Discord notify failed: %s", e)
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


async def notify_review_needed(run_id: str, node: str, output_summary: str = ""):
    """需要人工审核通知（内嵌审核指引，agent 自动识别进入桥接模式）"""
    emoji = NODE_EMOJI.get(node, "📌")
    lines = [
        f"⏸️ **{emoji} {node} 等待审核**  `[REVIEW]`",
        f"`run_id: {run_id}` · `node: {node}`",
    ]
    if output_summary:
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
