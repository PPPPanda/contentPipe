"""
浏览器代理引擎 v2 — 通过 OpenClaw Browser Relay 控制网页端图片生成工具

v2 改进（参考 chatgpt-browser skill 实测经验）:
  - 支持 evaluate 方式输入（contenteditable 元素，如 ChatGPT）
  - 支持 evaluate 方式发送（绕过 aria-ref 超时问题）
  - 支持真实图片下载（不只是截图）
  - 内置 relay 断连检测 + 自动重连
  - 站点配置更灵活：自定义 step 序列

适用场景：
  - 无 API 的工具（ChatGPT DALL-E / Midjourney Web / Leonardo / Ideogram）
  - 需要登录态的服务
  - 国内无法直连的 API（通过浏览器翻墙）

支持的网站（预置配置）：
  - ChatGPT (chatgpt.com) — DALL-E 图片生成 ⭐ 新增
  - 通义万相 (tongyi.aliyun.com)
  - 即梦 (jimeng.jianying.com)
  - Ideogram (ideogram.ai)
  - Leonardo.ai
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Literal

from .base import ImageEngine, ImageResult

logger = logging.getLogger(__name__)


# ── 站点配置 ────────────────────────────────────────────────


@dataclass
class BrowserSiteConfig:
    """浏览器自动化网站配置"""
    name: str
    url: str

    # ─ 输入方式 ─
    input_method: Literal["fill", "evaluate"] = "fill"
    # fill: 标准 CSS selector 填充（适用于 input/textarea）
    # evaluate: JS evaluate 注入（适用于 contenteditable，如 ChatGPT）

    prompt_input_selector: str = ""          # fill 模式用
    prompt_evaluate_fn: str = ""             # evaluate 模式用（JS 函数模板，{prompt} 占位符）

    # ─ 发送方式 ─
    send_method: Literal["click", "evaluate"] = "click"
    generate_button_selector: str = ""       # click 模式用
    send_evaluate_fn: str = ""               # evaluate 模式用

    # ─ 结果提取 ─
    result_method: Literal["screenshot", "download", "evaluate"] = "screenshot"
    image_result_selector: str = ""          # screenshot/download 模式用（等待此元素出现）
    image_download_evaluate_fn: str = ""     # evaluate 模式：返回图片 URL 数组的 JS
    image_download_cookies: bool = False     # 是否需要 cookies 才能下载图片

    # ─ 等待时间 ─
    generation_timeout_ms: int = 60000       # 生成超时
    load_wait_ms: int = 3000                 # 页面加载等待
    post_send_wait_ms: int = 0               # 发送后额外等待（如 ChatGPT 需要 30-60s）

    # ─ 其他 ─
    negative_prompt_selector: str = ""
    pre_actions: list[dict] = field(default_factory=list)
    reconnect_on_navigate: bool = False      # 发送后 URL 变化导致 relay 断连
    completion_check_fn: str = ""            # 检测生成是否完成的 JS（返回 true/false）

    # ─ 下载代理 ─
    download_proxy: str = ""                 # curl 下载时的代理（如 http://172.27.112.1:7890）


# ── 预置站点 ────────────────────────────────────────────────


SITE_CONFIGS: dict[str, BrowserSiteConfig] = {

    # ⭐ ChatGPT — DALL-E 图片生成（参考 chatgpt-browser skill 实测）
    # 使用 /images 专用页面（有风格模板、历史图片库、下载按钮）
    "chatgpt": BrowserSiteConfig(
        name="ChatGPT",
        url="https://chatgpt.com/images",

        input_method="evaluate",
        # /images 页面: #prompt-textarea 或 contenteditable 输入框
        prompt_evaluate_fn=(
            "(function(){{ var el = document.querySelector('#prompt-textarea, [contenteditable=\"true\"]');"
            " if(!el) return 'input not found';"
            " el.focus();"
            " document.execCommand('insertText', false, '{prompt}');"
            " return el.textContent }})()"
        ),

        send_method="evaluate",
        send_evaluate_fn=(
            "(function(){ var btn = document.querySelector("
            "'button[data-testid=\"send-button\"], button[aria-label=\"发送提示\"],"
            " button[aria-label=\"Send\"]');"
            " if(btn){ btn.click(); return 'sent' } return 'not found' })()"
        ),

        result_method="download",
        image_result_selector="img[src*='oaiusercontent'], img[src*='estuary'], article img",
        # 提取生成图片的原图 URL（覆盖 /images 和对话页面多种 selector）
        image_download_evaluate_fn=(
            "(function(){ var imgs = document.querySelectorAll("
            "'img[src*=\"oaiusercontent\"], img[src*=\"estuary\"],"
            " img[src*=\"dalle\"], img[src*=\"openai\"]');"
            " var urls = []; var seen = {};"
            " for(var i=0;i<imgs.length;i++){ var s=imgs[i].src;"
            " if(s && !seen[s] && !s.startsWith('data:') && s.includes('http'))"
            " { seen[s]=true; urls.push(s) } }"
            " return JSON.stringify(urls) })()"
        ),
        image_download_cookies=True,

        generation_timeout_ms=90000,
        load_wait_ms=3000,
        post_send_wait_ms=45000,       # ChatGPT 图片生成需要 30-60s
        reconnect_on_navigate=True,     # 发送后 URL 可能变为 /c/xxx

        # 完成检测: 停止按钮消失 = 生成完毕
        completion_check_fn=(
            "(function(){ return !document.querySelector("
            "'[data-testid=\"stop-button\"], button[aria-label*=\"停止\"],"
            " button[aria-label*=\"Stop\"]') })()"
        ),
    ),

    # 通义万相
    "tongyi": BrowserSiteConfig(
        name="通义万相",
        url="https://tongyi.aliyun.com/wanxiang/creation",
        input_method="fill",
        prompt_input_selector="textarea[placeholder*='描述']",
        send_method="click",
        generate_button_selector="button[class*='generate'], button:has-text('生成')",
        result_method="screenshot",
        image_result_selector="img[class*='result'], img[class*='generated']",
        generation_timeout_ms=30000,
    ),

    # 即梦 AI
    "jimeng": BrowserSiteConfig(
        name="即梦AI",
        url="https://jimeng.jianying.com/ai-tool/image/generate",
        input_method="fill",
        prompt_input_selector="textarea",
        send_method="click",
        generate_button_selector="button:has-text('生成')",
        result_method="screenshot",
        image_result_selector="img[class*='result']",
        generation_timeout_ms=30000,
    ),

    # Ideogram
    "ideogram": BrowserSiteConfig(
        name="Ideogram",
        url="https://ideogram.ai/t/create",
        input_method="fill",
        prompt_input_selector="textarea[placeholder*='Describe']",
        send_method="click",
        generate_button_selector="button:has-text('Generate')",
        result_method="screenshot",
        image_result_selector="img[class*='generated'], img[alt*='Generated']",
        generation_timeout_ms=60000,
    ),

    # Leonardo.ai
    "leonardo": BrowserSiteConfig(
        name="Leonardo.ai",
        url="https://app.leonardo.ai/ai-generations",
        input_method="fill",
        prompt_input_selector="textarea[placeholder*='prompt']",
        send_method="click",
        generate_button_selector="button:has-text('Generate')",
        result_method="screenshot",
        image_result_selector="img[class*='generated']",
        generation_timeout_ms=60000,
    ),
}


# ── 引擎实现 ────────────────────────────────────────────────


class BrowserEngine(ImageEngine):
    """
    浏览器代理图片生成引擎 v2

    通过 OpenClaw 的 browser tool (Chrome Relay) 控制浏览器。
    支持两种输入模式、两种发送模式、三种结果提取模式。

    需要：
      1. Chrome 安装 OpenClaw Browser Relay 扩展
      2. 目标网站已登录
      3. OpenClaw Gateway 运行中
    """

    engine_name = "browser"
    mode = "browser"

    def __init__(
        self,
        site: str | None = None,
        site_config: BrowserSiteConfig | None = None,
        profile: str = "chrome",
        gateway_url: str | None = None,
    ):
        if site_config:
            self.site_config = site_config
        elif site and site in SITE_CONFIGS:
            self.site_config = SITE_CONFIGS[site]
        else:
            available = ", ".join(SITE_CONFIGS.keys())
            raise ValueError(f"Unknown site: {site}. Available: {available}")

        self.profile = profile
        self.gateway_url = gateway_url or os.environ.get(
            "OPENCLAW_GATEWAY_URL", "http://localhost:18789"
        )
        self._target_id: str | None = None

    # ── 主生成流程 ──────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = 1024,
        height: int = 1024,
        seed: int | None = None,
        output_path: str | Path = "",
        **kwargs,
    ) -> ImageResult:
        """生成图片 — 完整流程"""
        start = time.time()
        output_path = Path(output_path) if output_path else Path(f"/tmp/browser_gen_{int(time.time())}.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        config = self.site_config

        try:
            # Step 1: 确保浏览器连接 + 找到/创建目标 tab
            self._ensure_tab(config.url)
            time.sleep(config.load_wait_ms / 1000)
            logger.info("browser_engine[%s]: page loaded, target=%s", config.name, self._target_id)

            # Step 2: 执行预置操作（关闭弹窗等）
            for action in config.pre_actions:
                self._browser_action(**action)

            # Step 3: 输入 prompt（附加尺寸比例说明）
            full_prompt = self._build_prompt_with_size(prompt, width, height)
            self._input_prompt(full_prompt)

            # Step 4: 输入 negative prompt
            if negative_prompt and config.negative_prompt_selector:
                self._browser_action(
                    "act", kind="fill",
                    selector=config.negative_prompt_selector,
                    text=negative_prompt,
                )

            # Step 5: 发送 / 点击生成
            self._send_generate()

            # Step 6: 等待生成完成
            self._wait_for_completion(config)

            # Step 7: 提取结果
            image_bytes = self._extract_result(output_path)
            if image_bytes:
                output_path.write_bytes(image_bytes)

            elapsed = int((time.time() - start) * 1000)
            return ImageResult(
                success=True,
                file_path=str(output_path),
                engine=f"browser:{config.name}",
                prompt_used=prompt,
                seed_used=seed,
                generation_time_ms=elapsed,
                width=width,
                height=height,
                metadata={"site": config.name, "url": config.url, "target_id": self._target_id},
            )

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error("browser_engine[%s]: generation failed: %s", config.name, e, exc_info=True)
            return ImageResult(
                success=False,
                engine=f"browser:{config.name}",
                prompt_used=prompt,
                generation_time_ms=elapsed,
                error=str(e),
            )

    # ── 各步骤实现 ──────────────────────────────────────────

    def _ensure_tab(self, url: str):
        """确保有可用的 tab 连接到目标网站。

        流程:
        1. 查看现有 tabs，找匹配的
        2. tabs 为空 → 说明 relay 未连接，尝试激活 relay
        3. relay 激活后重新查找
        4. 仍然没有 → 尝试 navigate 打开
        5. 全部失败 → 抛异常
        """
        tabs = self._get_tabs()
        domain = re.sub(r'https?://', '', url).split('/')[0]

        # 找已有的匹配 tab
        for tab in tabs:
            if domain in tab.get("url", ""):
                self._target_id = tab["targetId"]
                logger.info("browser_engine: reusing existing tab %s", self._target_id)
                return

        # tabs 为空 → relay 可能未连接，先尝试激活
        if not tabs:
            logger.info("browser_engine: no tabs found, attempting relay activation...")
            self._activate_relay()
            # 激活后重新获取
            tabs = self._get_tabs()
            for tab in tabs:
                if domain in tab.get("url", ""):
                    self._target_id = tab["targetId"]
                    logger.info("browser_engine: found tab after relay activation: %s", self._target_id)
                    return
            # relay 激活了但没有目标页面，尝试导航
            if tabs:
                self._target_id = tabs[0]["targetId"]
                logger.info("browser_engine: navigating existing tab %s to %s", self._target_id, url)
                nav_result = self._browser_action("navigate", url=url)
                if nav_result.get("ok"):
                    return

        # 有 tabs 但没有匹配的 → 尝试 navigate 打开新页面
        if tabs:
            result = self._browser_action("open", url=url)
            if result.get("ok") and result.get("targetId"):
                self._target_id = result["targetId"]
                return
            # fallback: 重新获取 tabs
            time.sleep(3)
            tabs = self._get_tabs()
            for tab in tabs:
                if domain in tab.get("url", ""):
                    self._target_id = tab["targetId"]
                    return

        raise RuntimeError(f"Failed to open tab for {url}")

    def _activate_relay(self):
        """尝试激活 Chrome Relay（运行 connect.sh 脚本）"""
        try:
            import subprocess as _sp
            script_path = Path(__file__).parent.parent.parent / "skills" / "browser-relay-activator" / "scripts" / "connect.sh"
            if not script_path.exists():
                logger.warning("browser_engine: relay activator script not found: %s", script_path)
                return False

            logger.info("browser_engine: running relay activator: %s", script_path)
            result = _sp.run(
                ["bash", str(script_path)],
                capture_output=True, text=True, timeout=60,
                cwd=str(script_path.parent.parent),
            )
            if result.returncode == 0:
                logger.info("browser_engine: relay activated successfully")
                time.sleep(3)  # 等 relay 稳定
                return True
            else:
                logger.warning("browser_engine: relay activation failed (rc=%d): %s",
                               result.returncode, result.stderr[:200] if result.stderr else "")
                return False
        except Exception as e:
            logger.warning("browser_engine: relay activation error: %s", e)
            return False

    @staticmethod
    def _build_prompt_with_size(prompt: str, width: int, height: int) -> str:
        """在 prompt 末尾追加图片尺寸/比例说明。

        ChatGPT/DALL-E 支持通过自然语言指定比例，如：
        - "16:9 横版" / "9:16 竖版" / "1:1 方形"
        - "宽 1200px 高 800px"
        """
        if width == height:
            ratio_hint = "1:1 square aspect ratio"
        elif width > height:
            # 常见横版比例
            r = width / height
            if abs(r - 16 / 9) < 0.1:
                ratio_hint = "16:9 landscape aspect ratio"
            elif abs(r - 3 / 2) < 0.1:
                ratio_hint = "3:2 landscape aspect ratio"
            elif abs(r - 4 / 3) < 0.1:
                ratio_hint = "4:3 landscape aspect ratio"
            else:
                ratio_hint = f"{width}x{height} landscape aspect ratio"
        else:
            r = height / width
            if abs(r - 16 / 9) < 0.1:
                ratio_hint = "9:16 portrait aspect ratio"
            elif abs(r - 3 / 2) < 0.1:
                ratio_hint = "2:3 portrait aspect ratio"
            elif abs(r - 4 / 3) < 0.1:
                ratio_hint = "3:4 portrait aspect ratio"
            else:
                ratio_hint = f"{width}x{height} portrait aspect ratio"

        return f"{prompt}\n\nImage size: {ratio_hint}, {width}x{height} pixels"

    def _input_prompt(self, prompt: str):
        """输入提示词"""
        config = self.site_config
        safe_prompt = prompt.replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")

        if config.input_method == "evaluate":
            fn = config.prompt_evaluate_fn.format(prompt=safe_prompt)
            result = self._browser_evaluate(fn)
            logger.info("browser_engine: evaluate input result: %s", result)
        else:
            self._browser_action(
                "act", kind="fill",
                selector=config.prompt_input_selector,
                text=prompt,
            )

    def _send_generate(self):
        """点击发送/生成按钮"""
        config = self.site_config

        if config.send_method == "evaluate":
            result = self._browser_evaluate(config.send_evaluate_fn)
            logger.info("browser_engine: evaluate send result: %s", result)
            if result and "not found" in str(result).lower():
                raise RuntimeError("Send button not found")
        else:
            self._browser_action(
                "act", kind="click",
                selector=config.generate_button_selector,
            )

    def _wait_for_completion(self, config: BrowserSiteConfig):
        """等待图片生成完成"""
        # 固定等待（如 ChatGPT 需要较长时间）
        if config.post_send_wait_ms > 0:
            wait_s = config.post_send_wait_ms / 1000
            logger.info("browser_engine: waiting %.1fs for generation...", wait_s)
            time.sleep(wait_s)

        # 如果发送后 URL 变化导致 relay 断连，尝试重连
        if config.reconnect_on_navigate:
            self._reconnect_relay()

        # 轮询检测完成状态
        if config.completion_check_fn:
            deadline = time.time() + config.generation_timeout_ms / 1000
            while time.time() < deadline:
                try:
                    result = self._browser_evaluate(config.completion_check_fn)
                    if result and str(result).lower() in ("true", "1"):
                        logger.info("browser_engine: generation completed")
                        return
                except Exception:
                    pass
                time.sleep(3)
            logger.warning("browser_engine: completion check timed out, proceeding anyway")

        # 无 completion_check，等待图片元素出现
        elif config.image_result_selector:
            try:
                self._browser_action(
                    "act", kind="wait",
                    selector=config.image_result_selector,
                    timeoutMs=config.generation_timeout_ms,
                )
            except Exception as e:
                logger.warning("browser_engine: wait for result element failed: %s", e)

    def _extract_result(self, output_path: Path) -> bytes | None:
        """提取生成的图片"""
        config = self.site_config

        if config.result_method == "download":
            return self._download_image(output_path)
        elif config.result_method == "evaluate":
            return self._evaluate_extract_image()
        else:
            return self._screenshot_result(output_path)

    # ── 图片下载（新增，参考 chatgpt-browser skill） ─────────

    def _download_image(self, output_path: Path) -> bytes | None:
        """从页面提取真实图片 URL 并下载"""
        config = self.site_config

        # Step 1: 用 evaluate 获取图片 URL
        if config.image_download_evaluate_fn:
            raw = self._browser_evaluate(config.image_download_evaluate_fn)
            try:
                urls = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                logger.warning("browser_engine: failed to parse image URLs: %s", raw)
                # fallback 到截图
                return self._screenshot_result(output_path)
        else:
            logger.warning("browser_engine: no download evaluate fn, falling back to screenshot")
            return self._screenshot_result(output_path)

        if not urls:
            logger.warning("browser_engine: no image URLs found, falling back to screenshot")
            return self._screenshot_result(output_path)

        # Step 2: 下载第一张图片
        image_url = urls[0] if isinstance(urls, list) else urls
        logger.info("browser_engine: downloading image from %s", image_url[:100])

        # 获取 cookies（如果需要）
        cookies = ""
        if config.image_download_cookies:
            try:
                cookies = self._browser_evaluate("(function(){ return document.cookie })()")
            except Exception:
                pass

        # 尝试用 httpx 下载
        import httpx
        headers = {}
        if cookies:
            headers["Cookie"] = cookies

        try:
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                resp = client.get(image_url, headers=headers)
                resp.raise_for_status()
                return resp.content
        except Exception as e:
            logger.warning("browser_engine: httpx download failed: %s, trying curl", e)

        # Fallback: curl 下载（支持代理）
        try:
            import subprocess
            cmd = ["curl", "-sS", "-L", "-o", str(output_path)]
            if cookies:
                cmd += ["-H", f"Cookie: {cookies}"]
            if config.download_proxy:
                cmd += ["-x", config.download_proxy]
            cmd.append(image_url)

            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode == 0 and output_path.exists():
                return output_path.read_bytes()
        except Exception as e:
            logger.warning("browser_engine: curl download failed: %s", e)

        # Final fallback: 截图
        logger.warning("browser_engine: all download methods failed, falling back to screenshot")
        return self._screenshot_result(output_path)

    # ── Relay 重连 ──────────────────────────────────────────

    def _reconnect_relay(self):
        """
        执行过程中 relay 断连时尝试重连。

        触发场景：
        - 发送后 ChatGPT URL 从 / 变为 /c/xxx，relay targetId 失效
        - Chrome 扩展意外断开
        - 长时间等待后连接超时

        策略：
        1. 先测试当前连接（可能没断）
        2. 重新获取 tabs，找到新 targetId
        3. tabs 为空 → 调 _activate_relay() 运行 connect.sh 重连
        4. 重连后导航回目标页面
        """
        # 先测试当前连接
        try:
            result = self._browser_evaluate("document.title")
            if result:
                logger.info("browser_engine: relay still connected, title=%s", result)
                return
        except Exception:
            pass

        logger.info("browser_engine: relay disconnected, attempting reconnect...")

        config = self.site_config
        domain = re.sub(r'https?://', '', config.url).split('/')[0]

        # 重新获取 tabs（relay 可能还在，只是 targetId 变了）
        tabs = self._get_tabs()

        if tabs:
            for tab in tabs:
                tab_url = tab.get("url", "")
                if domain in tab_url:
                    old_id = self._target_id
                    self._target_id = tab["targetId"]
                    logger.info("browser_engine: found tab %s -> %s (url: %s)",
                                old_id, self._target_id, tab_url)
                    try:
                        result = self._browser_evaluate("document.title")
                        if result:
                            logger.info("browser_engine: reconnected successfully")
                            return
                    except Exception:
                        pass

        # tabs 为空或没找到匹配 → relay 完全断了，重新激活
        if not tabs or not any(domain in t.get("url", "") for t in tabs):
            logger.info("browser_engine: relay fully disconnected, activating relay...")
            activated = self._activate_relay()
            if activated:
                tabs = self._get_tabs()
                # 激活后找匹配的 tab
                for tab in tabs:
                    if domain in tab.get("url", ""):
                        self._target_id = tab["targetId"]
                        logger.info("browser_engine: reconnected via relay activation, target=%s", self._target_id)
                        return
                # 有 tab 但没匹配 → 导航到目标页面
                if tabs:
                    self._target_id = tabs[0]["targetId"]
                    logger.info("browser_engine: navigating to %s after relay activation", config.url)
                    self._browser_action("navigate", url=config.url)
                    time.sleep(3)
                    return

        logger.warning("browser_engine: reconnect failed, continuing with best effort")

    # ── 底层浏览器操作 ──────────────────────────────────────

    def _get_tabs(self) -> list[dict]:
        """获取当前 Chrome tab 列表"""
        result = self._browser_action("tabs")
        return result.get("tabs", [])

    def _browser_evaluate(self, fn: str) -> str | None:
        """执行 JS evaluate 并返回结果"""
        result = self._browser_action("act", kind="evaluate", fn=fn)
        # CLI 返回格式可能不同
        return result.get("result") or result.get("value") or result.get("raw")

    def _screenshot_result(self, output_path: Path) -> bytes | None:
        """截取当前页面截图"""
        result = self._browser_action("screenshot", type="png", fullPage=False)

        # CLI 模式：返回本地文件路径
        if result.get("path"):
            src = Path(result["path"])
            if src.exists():
                image_bytes = src.read_bytes()
                output_path.write_bytes(image_bytes)
                logger.info("browser_engine: screenshot saved %s (%d KB)", output_path, len(image_bytes) // 1024)
                return image_bytes

        # API 模式：返回 base64 data
        if result.get("data"):
            return base64.b64decode(result["data"])
        if result.get("buffer"):
            return base64.b64decode(result["buffer"])

        logger.warning("browser_engine: screenshot returned no data: %s", list(result.keys()))
        return None

    def _browser_action(self, action: str, **kwargs) -> dict:
        """
        调用 OpenClaw browser tool

        使用 `openclaw browser` CLI（走 Gateway WebSocket 内部通道）。
        不走 HTTP API（Gateway 没有暴露 /api/tools/browser 端点）。
        """
        import subprocess as _sp

        cmd = ["openclaw", "browser", action, "--json", "--browser-profile", self.profile]

        # 添加 targetId
        target_id = kwargs.pop("targetId", None) or (
            self._target_id if action not in ("tabs", "status", "open") else None
        )

        # 按 action 类型组装 CLI 参数
        if action == "tabs":
            pass  # 无额外参数

        elif action == "status":
            pass

        elif action == "navigate":
            url = kwargs.get("url", "")
            if target_id:
                cmd += ["--target-id", target_id]
            if url:
                cmd += [url]  # positional arg

        elif action == "open":
            url = kwargs.get("url", "")
            if url:
                cmd += [url]

        elif action == "screenshot":
            if target_id:
                cmd += [target_id]  # positional arg
            img_type = kwargs.get("type", "png")
            cmd += ["--type", img_type]

        elif action == "snapshot":
            if target_id:
                cmd += ["--target-id", target_id]
            if kwargs.get("compact"):
                cmd += ["--compact"]
            if kwargs.get("selector"):
                cmd += ["--selector", kwargs["selector"]]

        elif action == "act":
            # act 子命令需要特殊处理
            kind = kwargs.get("kind", "")
            if kind == "evaluate":
                cmd = ["openclaw", "browser", "evaluate", "--json", "--browser-profile", self.profile]
                if target_id:
                    cmd += ["--target-id", target_id]
                fn = kwargs.get("fn", "")
                cmd += ["--fn", fn]
            elif kind == "click":
                cmd = ["openclaw", "browser", "click", "--json", "--browser-profile", self.profile]
                if target_id:
                    cmd += ["--target-id", target_id]
                if kwargs.get("ref"):
                    cmd += [kwargs["ref"]]  # positional ref arg
                elif kwargs.get("selector"):
                    cmd += ["--selector", kwargs["selector"]]
            elif kind == "fill":
                # `openclaw browser type <ref> <text>` — 但 fill by selector 不直接支持
                # 用 evaluate 代替
                selector = kwargs.get("selector", "")
                text = kwargs.get("text", "").replace("'", "\\'")
                fill_fn = (
                    f"(function(){{ var el = document.querySelector('{selector}');"
                    f" if(!el) return 'not found';"
                    f" el.focus(); el.value = '{text}';"
                    f" el.dispatchEvent(new Event('input', {{bubbles:true}}));"
                    f" return 'filled' }})()"
                )
                cmd = ["openclaw", "browser", "evaluate", "--json", "--browser-profile", self.profile]
                if target_id:
                    cmd += ["--target-id", target_id]
                cmd += ["--fn", fill_fn]
            elif kind == "wait":
                # wait 没有直接 CLI，用 eval 轮询
                return self._poll_wait(
                    kwargs.get("selector", ""),
                    kwargs.get("timeoutMs", 30000),
                )
            else:
                logger.warning("browser_action: unsupported act kind=%s", kind)
                return {}
        else:
            logger.warning("browser_action: unknown action=%s", action)
            return {}

        # 执行 CLI
        timeout_s = max(kwargs.get("timeoutMs", 30000) / 1000, 30)
        logger.debug("browser_cli: %s", " ".join(cmd[:8]) + "...")

        try:
            result = _sp.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            output = result.stdout.strip()

            # 解析 JSON 输出（可能是多行 JSON）
            # 先尝试整段解析
            try:
                return json.loads(output)
            except (json.JSONDecodeError, ValueError):
                pass

            # 尝试找 JSON 块（从第一个 { 或 [ 到最后一个 } 或 ]）
            json_start = -1
            for i, ch in enumerate(output):
                if ch in ('{', '['):
                    json_start = i
                    break
            if json_start >= 0:
                try:
                    return json.loads(output[json_start:])
                except (json.JSONDecodeError, ValueError):
                    pass

            # 非 JSON 输出，包装返回
            if result.returncode == 0:
                return {"ok": True, "raw": output}
            else:
                stderr = result.stderr.strip()
                logger.error("browser_cli failed: rc=%d stdout=%s stderr=%s", result.returncode, output[:200], stderr[:200])
                return {"ok": False, "error": stderr or output}

        except _sp.TimeoutExpired:
            logger.error("browser_cli timeout: %s", " ".join(cmd[:6]))
            raise RuntimeError(f"Browser CLI timeout: {action}")
        except Exception as e:
            logger.error("browser_cli error: %s", e)
            raise

    def _poll_wait(self, selector: str, timeout_ms: int) -> dict:
        """轮询等待元素出现（CLI 没有 wait 子命令）"""
        deadline = time.time() + timeout_ms / 1000
        check_fn = f"(function(){{ return !!document.querySelector('{selector}') }})()"
        while time.time() < deadline:
            result = self._browser_action("act", kind="evaluate", fn=check_fn)
            if str(result.get("result", "")).lower() in ("true", "1"):
                return {"ok": True}
            time.sleep(3)
        return {"ok": False, "error": f"wait timeout: {selector}"}

    # ── 可用性检查 ──────────────────────────────────────────

    def is_available(self) -> bool:
        """检查浏览器 relay 是否连接"""
        try:
            result = self._browser_action("tabs")
            tabs = result.get("tabs", [])
            return len(tabs) > 0
        except Exception:
            return False

    @classmethod
    def list_sites(cls) -> list[str]:
        """列出所有预置网站"""
        return list(SITE_CONFIGS.keys())

    @classmethod
    def get_site_info(cls, site: str) -> dict:
        """获取预置网站信息"""
        config = SITE_CONFIGS.get(site)
        if not config:
            return {}
        return {
            "name": config.name,
            "url": config.url,
            "input_method": config.input_method,
            "result_method": config.result_method,
            "timeout_ms": config.generation_timeout_ms,
        }
