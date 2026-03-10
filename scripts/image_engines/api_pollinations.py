"""
Pollinations.ai 图片生成引擎

免费 API，零注册，GET 请求直接返回图片。
优先级最低（免费 fallback），但最简单可靠。

API: GET https://image.pollinations.ai/prompt/{prompt}?width=W&height=H&model=flux&seed=S&nologo=true

限制:
  - 免费 tier 可能排队 (2s~90s)
  - 输出尺寸可能被缩小
  - 中文 prompt 效果一般，内部自动翻译为英文
  - 无 negative prompt 支持
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .base import ImageEngine, ImageResult
from logutil import get_logger

logger = get_logger(__name__)


class PollinationsEngine(ImageEngine):
    """Pollinations.ai 免费图片生成"""

    engine_name = "pollinations"
    mode = "api"

    def __init__(
        self,
        model: str = "flux",
        timeout: int = 120,
        translate_to_en: bool = True,
        max_retries: int = 2,
    ):
        self.model = model
        self.timeout = timeout
        self.translate_to_en = translate_to_en
        self.max_retries = max_retries

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
        t0 = time.time()

        # 如果 prompt 包含中文，翻译为英文描述
        if self.translate_to_en and _has_chinese(prompt):
            prompt = _translate_prompt(prompt)

        # Pollinations 对长 prompt 返回 500 — 精简到 ~200 字符
        if len(prompt) > 200:
            prompt = _shorten_prompt(prompt)

        # 构建 URL
        encoded_prompt = quote(prompt, safe="")
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        params = {
            "width": width,
            "height": height,
            "model": self.model,
            "nologo": "true",
            "enhance": "true",  # 让 AI 优化 prompt
        }
        if seed is not None:
            params["seed"] = seed

        output_path = Path(output_path) if output_path else Path(f"/tmp/pollinations_{int(time.time())}.jpg")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        last_error = ""
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=self.timeout, proxy=None) as client:
                    resp = client.get(url, params=params, follow_redirects=True)
                    resp.raise_for_status()

                # 验证是图片（不是错误页）
                ct = resp.headers.get("content-type", "")
                if "image" not in ct and len(resp.content) < 1000:
                    last_error = f"Not an image: {ct}, size={len(resp.content)}"
                    continue

                output_path.write_bytes(resp.content)
                duration_ms = int((time.time() - t0) * 1000)

                return ImageResult(
                    success=True,
                    file_path=str(output_path),
                    engine="pollinations",
                    prompt_used=prompt,
                    seed_used=seed,
                    generation_time_ms=duration_ms,
                    width=width,
                    height=height,
                    metadata={
                        "model": self.model,
                        "actual_size": len(resp.content),
                        "content_type": ct,
                        "attempts": attempt + 1,
                    },
                )

            except httpx.TimeoutException:
                last_error = f"Timeout after {self.timeout}s (attempt {attempt+1})"
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code} (attempt {attempt+1})"
                # 500 可能是排队问题，重试
                if e.response.status_code >= 500:
                    import time as _time
                    _time.sleep(3)
                    continue
                break  # 4xx 不重试
            except Exception as e:
                last_error = str(e)[:200]
                break

        return ImageResult(
            success=False,
            engine="pollinations",
            prompt_used=prompt,
            generation_time_ms=int((time.time() - t0) * 1000),
            error=last_error,
        )

    def is_available(self) -> bool:
        """Pollinations 永远可用（免费无 key）"""
        return True


def _shorten_prompt(prompt: str, max_len: int = 180) -> str:
    """
    精简长 prompt。先尝试 LLM 摘要，失败则硬截断。
    Pollinations 对超过 ~200 字符的 prompt 经常返回 500。
    """
    try:
        from tools import call_llm
        result = call_llm(
            prompt="Condense this image generation prompt into under 100 words. "
                   "Keep the most important visual elements: subject, style, mood, colors. "
                   "Output ONLY the condensed prompt.",
            context=prompt,
            model="anthropic/claude-sonnet-4-6",
            temperature=0.2,
            max_tokens=150,
        )
        short = result.strip().strip('"').strip("'")
        if short and len(short) > 10:
            return short[:max_len]
    except Exception:
        pass
    return prompt[:max_len]


def _has_chinese(text: str) -> bool:
    """检测文本是否包含中文字符"""
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def _translate_prompt(chinese_prompt: str) -> str:
    """
    将中文 prompt 翻译为英文。

    策略: 用 LLM 翻译。如果 LLM 不可用，用简单的关键词映射。
    """
    try:
        from tools import call_llm
        result = call_llm(
            prompt="You are a prompt translator. Translate the following Chinese image generation prompt into English. "
                   "Output ONLY the English prompt, nothing else. Keep it concise (under 60 words). "
                   "Preserve the artistic style, mood, and composition details.",
            context=chinese_prompt,
            model="anthropic/claude-sonnet-4-6",
            temperature=0.3,
            max_tokens=200,
        )
        translated = result.strip().strip('"').strip("'")
        if translated and len(translated) > 10:
            return translated
    except Exception as e:
        logger.warning("Prompt translation failed: %s", e)

    # Fallback: 原样返回（Pollinations 也能处理部分中文）
    return chinese_prompt
