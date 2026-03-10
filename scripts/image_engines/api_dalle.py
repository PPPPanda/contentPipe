"""
DALL-E 3 引擎 — API 模式

通过 OpenAI API 直接调用 DALL-E 3 生成图片。
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import httpx

from .base import ImageEngine, ImageResult


class DallE3Engine(ImageEngine):
    engine_name = "dall-e-3"
    mode = "api"

    # DALL-E 3 支持的尺寸
    SUPPORTED_SIZES = {
        (1024, 1024): "1024x1024",
        (1792, 1024): "1792x1024",   # 横版
        (1024, 1792): "1024x1792",   # 竖版
    }

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

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
        start = time.time()

        # 映射到 DALL-E 支持的尺寸
        size_str = self._map_size(width, height)

        # DALL-E 3 不支持 negative_prompt，拼到 prompt 末尾
        full_prompt = prompt
        if negative_prompt:
            full_prompt += f"\n\nAvoid: {negative_prompt}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        body = {
            "model": "dall-e-3",
            "prompt": full_prompt,
            "n": 1,
            "size": size_str,
            "quality": kwargs.get("quality", "standard"),  # "standard" or "hd"
            "response_format": "b64_json",
        }

        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(f"{self.base_url}/images/generations", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()

            image_bytes = base64.b64decode(data["data"][0]["b64_json"])
            revised_prompt = data["data"][0].get("revised_prompt", prompt)

            # 保存到文件
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(image_bytes)

            elapsed = int((time.time() - start) * 1000)

            return ImageResult(
                success=True,
                file_path=str(output_path),
                engine=self.engine_name,
                prompt_used=revised_prompt,
                seed_used=seed,
                generation_time_ms=elapsed,
                width=int(size_str.split("x")[0]),
                height=int(size_str.split("x")[1]),
                metadata={"revised_prompt": revised_prompt, "quality": body["quality"]},
            )

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            return ImageResult(
                success=False,
                engine=self.engine_name,
                prompt_used=prompt,
                generation_time_ms=elapsed,
                error=str(e),
            )

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _map_size(self, width: int, height: int) -> str:
        """将任意尺寸映射到 DALL-E 3 支持的最近尺寸"""
        ratio = width / height
        if ratio > 1.4:
            return "1792x1024"  # 横版
        elif ratio < 0.7:
            return "1024x1792"  # 竖版
        else:
            return "1024x1024"  # 方形
