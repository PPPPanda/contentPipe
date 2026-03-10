"""
DashScope 文生图引擎 — API 模式

通过阿里 DashScope API 调用文生图模型（通义万相 / Flux 等）。
国内可用，无需翻墙。
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import httpx

from .base import ImageEngine, ImageResult


class DashScopeEngine(ImageEngine):
    engine_name = "dashscope"
    mode = "api"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "wanx-v1",
    ):
        self.api_key = api_key or os.environ.get("DASHSCOPE_IMAGE_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
        self.model = model
        self.base_url = "https://dashscope.aliyuncs.com/api/v1"

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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",  # 异步模式
        }

        body = {
            "model": self.model,
            "input": {
                "prompt": prompt,
                "negative_prompt": negative_prompt,
            },
            "parameters": {
                "size": f"{width}*{height}",
                "n": 1,
            },
        }
        if seed is not None:
            body["parameters"]["seed"] = seed

        try:
            with httpx.Client(timeout=120) as client:
                # 1. 提交任务
                resp = client.post(
                    f"{self.base_url}/services/aigc/text2image/image-synthesis",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                task_data = resp.json()
                task_id = task_data["output"]["task_id"]

                # 2. 轮询等待结果
                check_headers = {"Authorization": f"Bearer {self.api_key}"}
                for _ in range(60):  # 最多等 60 秒
                    time.sleep(1)
                    check_resp = client.get(
                        f"{self.base_url}/tasks/{task_id}",
                        headers=check_headers,
                    )
                    check_data = check_resp.json()
                    status = check_data["output"]["task_status"]

                    if status == "SUCCEEDED":
                        results = check_data["output"]["results"]
                        image_url = results[0]["url"]

                        # 3. 下载图片
                        img_resp = client.get(image_url)
                        image_bytes = img_resp.content

                        output_path = Path(output_path)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(image_bytes)

                        elapsed = int((time.time() - start) * 1000)
                        return ImageResult(
                            success=True,
                            file_path=str(output_path),
                            engine=self.engine_name,
                            prompt_used=prompt,
                            seed_used=seed,
                            generation_time_ms=elapsed,
                            width=width,
                            height=height,
                            metadata={"model": self.model, "task_id": task_id},
                        )

                    elif status == "FAILED":
                        error_msg = check_data["output"].get("message", "Task failed")
                        raise RuntimeError(error_msg)

                raise TimeoutError("Image generation timed out (60s)")

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
