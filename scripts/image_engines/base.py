"""
图片生成引擎基类

所有引擎（API / 浏览器）实现统一接口。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ImageResult:
    """图片生成结果"""
    success: bool
    file_path: str = ""
    engine: str = ""
    prompt_used: str = ""
    seed_used: int | None = None
    generation_time_ms: int = 0
    width: int = 0
    height: int = 0
    error: str = ""
    metadata: dict = field(default_factory=dict)


class ImageEngine(ABC):
    """图片生成引擎抽象基类"""

    engine_name: str = "base"
    mode: str = "api"  # "api" or "browser"

    @abstractmethod
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
        生成一张图片

        Args:
            prompt: 正向提示词
            negative_prompt: 反向提示词
            width: 宽度
            height: 高度
            seed: 随机种子（可选）
            output_path: 输出文件路径

        Returns:
            ImageResult
        """
        ...

    def generate_batch(
        self,
        prompts: list[dict],
        output_dir: str | Path,
    ) -> list[ImageResult]:
        """
        批量生成图片

        Args:
            prompts: [{"id": "img_001_A", "prompt": "...", "negative_prompt": "...", "width": 1024, "height": 1024, "seed": 42}]
            output_dir: 输出目录

        Returns:
            [ImageResult, ...]
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results = []

        for item in prompts:
            img_id = item.get("id", f"img_{len(results)}")
            output_path = output_dir / f"{img_id}.png"

            result = self.generate(
                prompt=item.get("prompt", ""),
                negative_prompt=item.get("negative_prompt", ""),
                width=item.get("width", 1024),
                height=item.get("height", 1024),
                seed=item.get("seed"),
                output_path=output_path,
                **{k: v for k, v in item.items() if k not in ("id", "prompt", "negative_prompt", "width", "height", "seed")},
            )
            result.metadata["id"] = img_id
            results.append(result)

        return results

    @abstractmethod
    def is_available(self) -> bool:
        """检查引擎是否可用（API key / 浏览器连接）"""
        ...

    def __repr__(self):
        return f"<{self.__class__.__name__} mode={self.mode}>"
