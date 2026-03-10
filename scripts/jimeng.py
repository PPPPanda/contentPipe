"""
即梦 (Jimeng) 浏览器自动化 — 通过 OpenClaw Browser Relay 生成图片

用法:
    from jimeng import generate_images
    urls = generate_images("一只猫咪在看书", tab_id="633824CAA4F7CEFB0C523AFB972A9E08")

依赖: OpenClaw Gateway + Windows Chrome Browser Relay + 即梦 tab 已打开并 attach
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from logutil import get_logger

GATEWAY_URL = "http://localhost:18789"
JIMENG_TAB_ID = "633824CAA4F7CEFB0C523AFB972A9E08"  # 默认即梦 tab

logger = get_logger(__name__)


def _browser_action(action: str, **kwargs) -> dict:
    """调用 OpenClaw Gateway 的 browser tool"""
    payload = {"action": action, "profile": "chrome", **kwargs}
    with httpx.Client(timeout=60) as client:
        resp = client.post(f"{GATEWAY_URL}/api/tools/browser", json=payload)
        resp.raise_for_status()
        return resp.json()


def _evaluate(js: str, tab_id: str = JIMENG_TAB_ID) -> Any:
    """在即梦 tab 执行 JavaScript"""
    result = _browser_action("act", targetId=tab_id, kind="evaluate", fn=js)
    return result.get("result")


def generate_images(
    prompt: str,
    tab_id: str = JIMENG_TAB_ID,
    timeout_sec: int = 30,
    poll_interval: float = 3.0,
) -> list[str]:
    """
    在即梦网页端生成图片

    Args:
        prompt: 图片描述（建议 < 150 字，太长会失败）
        tab_id: 即梦 tab 的 targetId
        timeout_sec: 等待生成完成的超时秒数
        poll_interval: 轮询间隔秒数

    Returns:
        生成的图片 URL 列表（CDN 签名 URL，需从浏览器 context 访问）
    """
    # 1. 填入 prompt（React textarea 需要 native setter）
    fill_js = f'''() => {{
        const ta = document.querySelector('textarea');
        if (!ta) return 'no textarea';
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value'
        ).set;
        setter.call(ta, {json.dumps(prompt)});
        ta.dispatchEvent(new Event('input', {{bubbles: true}}));
        ta.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'filled';
    }}'''
    fill_result = _evaluate(fill_js, tab_id)
    if fill_result != "filled":
        raise RuntimeError(f"Failed to fill prompt: {fill_result}")

    # 2. 记录当前图片数量（用于判断新图片出现）
    count_js = '''() => {
        return document.querySelectorAll('img[src*="dreamina"]').length;
    }'''
    before_count = _evaluate(count_js, tab_id) or 0

    # 3. 点击生成按钮
    submit_js = '''() => {
        const btns = Array.from(document.querySelectorAll('button.lv-btn-primary'));
        const submit = btns.find(b => b.className.includes('circle') && b.getBoundingClientRect().x > 700);
        if (submit) { submit.click(); return 'clicked'; }
        return 'no button';
    }'''
    click_result = _evaluate(submit_js, tab_id)
    if click_result != "clicked":
        raise RuntimeError(f"Failed to click submit: {click_result}")

    # 4. 轮询等待新图片出现
    deadline = time.time() + timeout_sec
    new_urls = []

    while time.time() < deadline:
        time.sleep(poll_interval)

        check_js = f'''() => {{
            const imgs = Array.from(document.querySelectorAll('img[src*="dreamina"]'));
            const current = imgs.length;
            if (current <= {before_count}) return JSON.stringify({{done: false, count: current}});
            // 获取最新一组（最前面的 4 张）
            const latest = imgs.filter(i => {{
                const r = i.getBoundingClientRect();
                return r.width > 80 && r.y > 0;
            }}).slice(0, 4);
            const urls = latest.map(i => i.src);
            return JSON.stringify({{done: true, count: current, urls: urls}});
        }}'''
        result = _evaluate(check_js, tab_id)
        try:
            data = json.loads(result)
            if data.get("done"):
                new_urls = data.get("urls", [])
                break
        except (json.JSONDecodeError, TypeError):
            continue

    if not new_urls:
        # 检查是否失败
        fail_js = '''() => {
            const el = Array.from(document.querySelectorAll('*')).find(
                e => e.textContent?.includes('生成失败')
                && e.getBoundingClientRect().width > 0
                && e.children.length <= 3
            );
            return el ? '生成失败' : '超时';
        }'''
        fail_reason = _evaluate(fail_js, tab_id) or "超时"
        raise RuntimeError(f"即梦生成未完成: {fail_reason}")

    return new_urls


def get_highres_url(thumbnail_url: str, size: int = 2048) -> str:
    """将缩略图 URL 转为高清 URL"""
    import re
    return re.sub(r'aigc_resize:\d+:\d+', f'aigc_resize:{size}:{size}', thumbnail_url)


if __name__ == "__main__":
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else "一只可爱的卡通猫咪在花园里玩耍"
    logger.info("Prompt: %s", prompt)
    try:
        urls = generate_images(prompt)
        logger.info("生成 %s 张图片", len(urls))
        for i, url in enumerate(urls):
            logger.info("[%s] %s...", i+1, url[:100])
    except Exception as e:
        logger.error("%s", e)
