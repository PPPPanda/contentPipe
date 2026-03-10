"""
浏览器代理引擎 — 通过 OpenClaw Browser Relay 控制网页端图片生成工具

适用场景：
  - 无 API 的工具（Midjourney Web / Leonardo / Ideogram / 在线免费工具）
  - 需要登录态的服务
  - 国内无法直连的 API（通过浏览器翻墙）

原理：
  1. 通过 OpenClaw browser tool 控制 Chrome
  2. 导航到目标图片生成网站
  3. 在输入框填入 prompt
  4. 点击生成按钮
  5. 等待图片出现
  6. 截图或下载图片

支持的网站（预置配置）：
  - Midjourney (web)
  - Leonardo.ai
  - Ideogram.ai
  - 通义万相 (tongyi.aliyun.com)
  - 即梦 (jimeng.jianying.com)
  - 可灵 (klingai.kuaishou.com)

也支持自定义网站配置。
"""

from __future__ import annotations

import os
import time
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass, field

from gateway_auth import build_gateway_headers
from .base import ImageEngine, ImageResult


@dataclass
class BrowserSiteConfig:
    """浏览器自动化网站配置"""
    name: str
    url: str

    # CSS 选择器
    prompt_input_selector: str          # 提示词输入框
    generate_button_selector: str       # 生成按钮
    image_result_selector: str          # 生成结果图片
    download_button_selector: str = ""  # 下载按钮（可选）

    # 等待时间
    generation_timeout_ms: int = 60000  # 生成超时
    load_wait_ms: int = 3000            # 页面加载等待

    # 额外操作
    negative_prompt_selector: str = ""  # 反向提示词输入框
    size_selector: str = ""             # 尺寸选择器
    pre_actions: list[dict] = field(default_factory=list)  # 生成前的额外操作


# 预置网站配置
SITE_CONFIGS: dict[str, BrowserSiteConfig] = {
    "tongyi": BrowserSiteConfig(
        name="通义万相",
        url="https://tongyi.aliyun.com/wanxiang/creation",
        prompt_input_selector="textarea[placeholder*='描述']",
        generate_button_selector="button[class*='generate'], button:has-text('生成')",
        image_result_selector="img[class*='result'], img[class*='generated']",
        generation_timeout_ms=30000,
    ),
    "jimeng": BrowserSiteConfig(
        name="即梦AI",
        url="https://jimeng.jianying.com/ai-tool/image/generate",
        prompt_input_selector="textarea",
        generate_button_selector="button:has-text('生成')",
        image_result_selector="img[class*='result']",
        generation_timeout_ms=30000,
    ),
    "ideogram": BrowserSiteConfig(
        name="Ideogram",
        url="https://ideogram.ai/t/create",
        prompt_input_selector="textarea[placeholder*='Describe']",
        generate_button_selector="button:has-text('Generate')",
        image_result_selector="img[class*='generated'], img[alt*='Generated']",
        generation_timeout_ms=60000,
    ),
    "leonardo": BrowserSiteConfig(
        name="Leonardo.ai",
        url="https://app.leonardo.ai/ai-generations",
        prompt_input_selector="textarea[placeholder*='prompt']",
        generate_button_selector="button:has-text('Generate')",
        image_result_selector="img[class*='generated']",
        generation_timeout_ms=60000,
    ),
}


class BrowserEngine(ImageEngine):
    """
    浏览器代理图片生成引擎

    通过 OpenClaw 的 browser tool 控制 Chrome 扩展。
    需要：
      1. Chrome 安装 OpenClaw Browser Relay 扩展
      2. 目标网站已登录
      3. OpenClaw Gateway 运行中

    用法：
      engine = BrowserEngine(site="tongyi")
      # 或自定义配置
      engine = BrowserEngine(site_config=BrowserSiteConfig(...))
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
        self.gateway_url = gateway_url or os.environ.get("OPENCLAW_GATEWAY_URL", "http://localhost:18789")

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
        """
        通过浏览器生成图片

        流程：
          1. 打开目标网站
          2. 在输入框填入 prompt
          3. 点击生成
          4. 等待图片出现
          5. 截图保存
        """
        start = time.time()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        config = self.site_config

        try:
            # Step 1: 导航到目标网站
            self._browser_action("navigate", url=config.url)
            time.sleep(config.load_wait_ms / 1000)

            # Step 2: 执行预置操作（如关闭弹窗等）
            for action in config.pre_actions:
                self._browser_action(**action)

            # Step 3: 填入 prompt
            self._browser_action(
                "act",
                kind="fill",
                selector=config.prompt_input_selector,
                text=prompt,
            )

            # Step 4: 填入 negative prompt（如果有）
            if negative_prompt and config.negative_prompt_selector:
                self._browser_action(
                    "act",
                    kind="fill",
                    selector=config.negative_prompt_selector,
                    text=negative_prompt,
                )

            # Step 5: 点击生成
            self._browser_action(
                "act",
                kind="click",
                selector=config.generate_button_selector,
            )

            # Step 6: 等待图片出现
            self._browser_action(
                "act",
                kind="wait",
                selector=config.image_result_selector,
                timeoutMs=config.generation_timeout_ms,
            )

            # Step 7: 截图保存
            screenshot_result = self._browser_screenshot(output_path)

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
                metadata={"site": config.name, "url": config.url},
            )

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return ImageResult(
                success=False,
                engine=f"browser:{config.name}",
                prompt_used=prompt,
                generation_time_ms=elapsed,
                error=str(e),
            )

    def is_available(self) -> bool:
        """检查浏览器连接是否可用"""
        try:
            # 简单检查 gateway 是否响应
            import httpx
            resp = httpx.get(f"{self.gateway_url}/api/status", timeout=5, headers=build_gateway_headers())
            return resp.status_code == 200
        except Exception:
            return False

    def _browser_action(self, action: str, **kwargs) -> dict:
        """
        调用 OpenClaw browser tool

        这里用 HTTP API 直接调用 Gateway 的 browser 工具。
        生产环境可替换为 OpenClaw SDK 调用。
        """
        # 方案 A: 通过 OpenClaw Gateway HTTP API
        import httpx

        payload = {
            "action": action,
            "profile": self.profile,
            **kwargs,
        }

        with httpx.Client(timeout=120) as client:
            resp = client.post(
                f"{self.gateway_url}/api/tools/browser",
                json=payload,
                headers=build_gateway_headers(),
            )
            resp.raise_for_status()
            return resp.json()

    def _browser_screenshot(self, output_path: Path) -> dict:
        """截取当前页面截图"""
        result = self._browser_action(
            "screenshot",
            type="png",
            fullPage=False,
        )
        # 结果中应包含 base64 图片数据
        if "data" in result:
            import base64
            image_bytes = base64.b64decode(result["data"])
            output_path.write_bytes(image_bytes)
        return result

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
            "timeout_ms": config.generation_timeout_ms,
        }
