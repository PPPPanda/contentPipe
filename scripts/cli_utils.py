"""
CLI 输出安全解析工具

openclaw CLI 的 stdout 可能被插件日志污染（如 [plugins] 行），
导致 json.loads() 报 Extra data 错误。

本模块提供安全的 JSON 提取函数，过滤非 JSON 行后解析。
"""

from __future__ import annotations

import json
import re
from typing import Any


# 已知的 CLI 日志前缀（会污染 stdout）
_NOISE_PREFIXES = (
    "[plugins]",
    "[gateway]",
    "[agent]",
    "[session]",
    "[channel",
    "🦞",
    "WARN ",
    "  WARN",
    "  Fix:",
)


def parse_cli_json(text: str) -> Any:
    """安全解析 CLI stdout 中的 JSON。

    策略:
    1. 先尝试直接解析（最快路径）
    2. 失败则过滤已知噪音行后重试
    3. 再失败则尝试提取第一个 JSON 对象/数组

    Args:
        text: CLI 的 stdout 输出

    Returns:
        解析后的 Python 对象

    Raises:
        json.JSONDecodeError: 无法提取有效 JSON
    """
    text = text.strip()
    if not text:
        raise json.JSONDecodeError("Empty input", text, 0)

    # 1. 快速路径：直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 过滤噪音行
    clean_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _NOISE_PREFIXES):
            continue
        # 跳过纯文本行（不以 JSON 字符开头且不在 JSON 块内）
        clean_lines.append(line)

    if clean_lines:
        clean = "\n".join(clean_lines)
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            pass

    # 3. 提取第一个 JSON 对象或数组（花括号/方括号匹配）
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start < 0:
            continue
        # 从末尾找对应的结束符
        end = text.rfind(end_char)
        if end <= start:
            continue
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError(
        f"No valid JSON found in CLI output ({len(text)} chars)",
        text[:100],
        0,
    )
