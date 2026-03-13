"""
ContentPipe — 简易 API 速率限制

基于 IP 的滑动窗口计数器，无外部依赖。
通过 CONTENTPIPE_RATE_LIMIT 环境变量控制（默认：60/min）。
设为 0 或空值表示不限制。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


def _parse_rate_limit() -> tuple[int, int]:
    """解析速率限制配置，格式: '{count}/{period}' 例如 '60/min' '100/hour'"""
    raw = os.environ.get("CONTENTPIPE_RATE_LIMIT", "60/min").strip()
    if not raw or raw == "0":
        return 0, 0

    try:
        count_str, period_str = raw.split("/", 1)
        count = int(count_str)
        periods = {"sec": 1, "min": 60, "hour": 3600, "day": 86400}
        period = periods.get(period_str, 60)
        return count, period
    except Exception:
        return 60, 60  # 默认 60/min


class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于 IP 的简易速率限制"""

    def __init__(self, app, max_requests: int = 0, window_seconds: int = 60):
        super().__init__(app)
        if max_requests == 0:
            limit, window = _parse_rate_limit()
            self.max_requests = limit
            self.window = window
        else:
            self.max_requests = max_requests
            self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        if self.max_requests <= 0:
            return await call_next(request)

        # 只限制 API 写入端点
        path = request.url.path
        if not path.startswith("/api/") or request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - self.window

        # 清理过期记录
        hits = self._hits[client_ip]
        self._hits[client_ip] = [t for t in hits if t > cutoff]
        hits = self._hits[client_ip]

        if len(hits) >= self.max_requests:
            retry_after = int(hits[0] - cutoff) + 1
            return JSONResponse(
                {"detail": "Rate limit exceeded", "retry_after": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        return await call_next(request)
