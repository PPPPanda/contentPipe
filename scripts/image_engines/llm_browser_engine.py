"""
LLM 浏览器引擎 — 通过 LLM Session + chatgpt-browser Skill 生成图片

核心思路：
  Python 只负责「准备参数 + 检查结果」，
  浏览器操作全部交给 LLM（通过 sessions_spawn 起 contentpipe-blank agent session）。

  1. Python 准备：prompt、尺寸比例、保存路径、文件名
  2. 启动 LLM session：带 chatgpt-browser skill，让 LLM 自己操控浏览器
  3. Python 检查：文件是否存在、大小是否合理、图片比例是否正确
  4. 通过 → 下一张；失败 → 重试

优势：
  - DOM 结构变了 → 改 SKILL.md，不动 Python
  - LLM 可以自适应处理意外弹窗、验证码等
  - 隔离性好：每张图一个独立 session
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from .base import ImageEngine, ImageResult

logger = logging.getLogger(__name__)

# LLM session 超时（秒）
DEFAULT_TIMEOUT = 300  # 5 分钟，留够生成+下载时间
MAX_RETRIES = 2


class LLMBrowserEngine(ImageEngine):
    """
    LLM 驱动的浏览器图片生成引擎。

    每张图片启动一个独立的 LLM session (contentpipe-blank agent)，
    通过 chatgpt-browser skill 让 LLM 自己操控 Chrome 生成并下载图片。
    """

    engine_name = "llm-browser"
    mode = "browser"

    def __init__(
        self,
        site: str = "chatgpt",
        agent_id: str = "contentpipe-blank",
        timeout: int = DEFAULT_TIMEOUT,
        gateway_url: str | None = None,
    ):
        self.site = site
        self.agent_id = agent_id
        self.timeout = timeout
        self.gateway_url = gateway_url or os.environ.get(
            "OPENCLAW_GATEWAY_URL", "http://localhost:18789"
        )

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
        """通过 LLM session 生成一张图片"""
        start = time.time()
        output_path = Path(output_path) if output_path else Path(f"/tmp/llm_gen_{int(time.time())}.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 构建比例说明
        ratio_hint = self._get_ratio_hint(width, height)

        # 构建 LLM 任务指令
        task = self._build_task(prompt, ratio_hint, width, height, str(output_path))

        logger.info("llm_browser[%s]: spawning session for %s (%dx%d)",
                     self.site, output_path.name, width, height)

        try:
            # 通过 openclaw CLI 启动 session
            result = self._spawn_session(task)

            if result is None:
                raise RuntimeError("LLM session spawn failed or timed out")

            # 检查结果
            elapsed = int((time.time() - start) * 1000)
            check = self._check_result(output_path, width, height)

            if check["ok"]:
                logger.info("llm_browser[%s]: success, file=%s size=%d",
                            self.site, output_path.name, check["size"])
                return ImageResult(
                    success=True,
                    file_path=str(output_path),
                    engine=f"llm-browser:{self.site}",
                    prompt_used=prompt,
                    seed_used=seed,
                    generation_time_ms=elapsed,
                    width=width,
                    height=height,
                    metadata={"site": self.site, "agent": self.agent_id},
                )
            else:
                raise RuntimeError(f"Image check failed: {check['reason']}")

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error("llm_browser[%s]: failed: %s", self.site, e)
            return ImageResult(
                success=False,
                engine=f"llm-browser:{self.site}",
                prompt_used=prompt,
                generation_time_ms=elapsed,
                error=str(e),
            )

    def is_available(self) -> bool:
        """检查 gateway 是否可达"""
        try:
            result = subprocess.run(
                ["openclaw", "status"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    # ── 内部方法 ──────────────────────────────────────────

    def _build_task(
        self,
        prompt: str,
        ratio_hint: str,
        width: int,
        height: int,
        output_path: str,
    ) -> str:
        """构建给 LLM 的任务指令"""
        return f"""使用 chatgpt-browser 技能，在 ChatGPT 上生成一张图片并下载到本地。

## 任务要求

1. **先阅读 chatgpt-browser 的 SKILL.md**，严格按照技能文档操作
2. 导航到 ChatGPT 图片生成页面（/images）
3. 输入以下提示词生成图片：

```
{prompt}

Image size: {ratio_hint}, {width}x{height} pixels
```

4. 等待图片生成完成
5. **下载原图**（不是截图！）到以下路径：

```
{output_path}
```

## 下载方法（参考 SKILL.md）

- 使用 evaluate 提取图片的真实 URL（oaiusercontent / estuary 域名）
- 获取 cookies
- 使用 curl 下载原图到指定路径
- 确保下载的是完整的 PNG/JPG 图片文件（不是 HTML 页面）

## 完成标准

- 文件已保存到 `{output_path}`
- 文件大小 > 10KB（确认是真实图片，不是错误页面）
- 完成后输出：DONE"""

    def _spawn_session(self, task: str) -> dict | None:
        """通过 openclaw agent CLI 启动 LLM agent turn。

        使用 `openclaw agent` 命令，指定 agent id 和消息。
        Agent 拥有完整的 tool use 能力（browser、exec 等），
        会根据 chatgpt-browser skill 自主操控浏览器。

        每次调用创建独立 session（通过唯一 session-id）。
        """
        session_id = f"contentpipe-img-{int(time.time())}"
        cmd = [
            "openclaw", "agent",
            "--agent", self.agent_id,
            "--session-id", session_id,
            "--message", task,
            "--timeout", str(self.timeout),
            "--json",
        ]

        logger.info("llm_browser: running agent turn (session=%s, timeout=%ds)",
                     session_id, self.timeout)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout + 60,  # CLI 超时比 agent 超时多 60s
                cwd=str(Path.home()),
            )

            stdout = result.stdout.strip() if result.stdout else ""
            stderr = result.stderr.strip() if result.stderr else ""

            # 过滤 CLI 噪音（[plugins]、🦞 等行）
            stdout_lines = [
                line for line in stdout.split("\n")
                if not any(line.startswith(p) for p in [
                    "[plugins]", "[gateway]", "[agent]", "[session]",
                    "[channel", "🦞", "WARN ", "  WARN", "  Fix:",
                ])
            ]
            clean_stdout = "\n".join(stdout_lines).strip()

            if result.returncode == 0:
                logger.info("llm_browser: agent turn completed (session=%s)", session_id)
                try:
                    return json.loads(clean_stdout)
                except (json.JSONDecodeError, ValueError):
                    return {"status": "completed", "session_id": session_id,
                            "stdout": clean_stdout[:500]}
            else:
                logger.warning("llm_browser: agent turn failed (rc=%d, session=%s): %s",
                               result.returncode, session_id, stderr[:300])
                # 即使 CLI 返回非零，图片可能已下载成功
                return {"status": "error", "session_id": session_id,
                        "stderr": stderr[:300]}

        except subprocess.TimeoutExpired:
            logger.warning("llm_browser: agent turn timed out after %ds (session=%s)",
                           self.timeout + 60, session_id)
            # 超时但图片可能已经下载了
            return {"status": "timeout", "session_id": session_id}

    def _check_result(self, output_path: Path, width: int, height: int) -> dict:
        """检查生成结果

        检查项：
        1. 文件是否存在
        2. 文件大小是否合理（>10KB，排除错误页面）
        3. 是否是有效图片（尝试读取头部 magic bytes）
        """
        if not output_path.exists():
            return {"ok": False, "reason": "file not found"}

        size = output_path.stat().st_size
        if size < 10 * 1024:  # < 10KB
            return {"ok": False, "reason": f"file too small ({size} bytes), likely not a real image"}

        # 检查 magic bytes
        with open(output_path, "rb") as f:
            header = f.read(16)

        # PNG: 89 50 4E 47
        # JPEG: FF D8 FF
        # WebP: 52 49 46 46 ... 57 45 42 50
        is_png = header[:4] == b'\x89PNG'
        is_jpeg = header[:3] == b'\xff\xd8\xff'
        is_webp = header[:4] == b'RIFF' and header[8:12] == b'WEBP'

        if not (is_png or is_jpeg or is_webp):
            return {"ok": False, "reason": "file is not a valid image (bad magic bytes)",
                    "header_hex": header[:8].hex()}

        # 可选：检查图片实际尺寸（需要 Pillow）
        actual_dims = self._get_image_dimensions(output_path)
        if actual_dims:
            aw, ah = actual_dims
            # 允许 ±20% 的尺寸偏差（DALL-E 可能不精确匹配请求尺寸）
            w_ratio = aw / width if width else 1
            h_ratio = ah / height if height else 1
            if w_ratio < 0.5 or w_ratio > 2.0 or h_ratio < 0.5 or h_ratio > 2.0:
                logger.warning("llm_browser: image dimensions %dx%d differ significantly from "
                               "requested %dx%d", aw, ah, width, height)
                # 不作为失败，只是警告

        return {"ok": True, "size": size, "format": "png" if is_png else "jpeg" if is_jpeg else "webp"}

    @staticmethod
    def _get_image_dimensions(path: Path) -> tuple[int, int] | None:
        """尝试获取图片实际尺寸"""
        try:
            from PIL import Image
            with Image.open(path) as img:
                return img.size
        except Exception:
            return None

    @staticmethod
    def _get_ratio_hint(width: int, height: int) -> str:
        """生成比例描述"""
        if width == height:
            return "1:1 square aspect ratio"
        elif width > height:
            r = width / height
            if abs(r - 16 / 9) < 0.1:
                return "16:9 landscape aspect ratio"
            elif abs(r - 3 / 2) < 0.1:
                return "3:2 landscape aspect ratio"
            elif abs(r - 4 / 3) < 0.1:
                return "4:3 landscape aspect ratio"
            elif abs(r - 2.35) < 0.15:
                return "2.35:1 cinematic widescreen aspect ratio"
            else:
                return f"{width}x{height} landscape"
        else:
            r = height / width
            if abs(r - 16 / 9) < 0.1:
                return "9:16 portrait aspect ratio"
            elif abs(r - 3 / 2) < 0.1:
                return "2:3 portrait aspect ratio"
            elif abs(r - 4 / 3) < 0.1:
                return "3:4 portrait aspect ratio"
            else:
                return f"{width}x{height} portrait"
