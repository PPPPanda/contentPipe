"""
ContentPipe Web Console — FastAPI 主应用

启动:
  cd skills/content-pipeline/scripts && python -m web.app
  或:
  uvicorn web.app:app --reload --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from web.auth import AuthMiddleware

# 确保 scripts/ 在 sys.path
SCRIPTS_DIR = Path(__file__).parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from web.routes.pages import router as pages_router
from web.routes.api import router as api_router
from web.routes.sse import router as sse_router

# ── FastAPI 应用 ──────────────────────────────────────────────

app = FastAPI(
    title="ContentPipe Console",
    description="图文内容 Pipeline 主控台",
    version="0.7.0",
)
app.add_middleware(AuthMiddleware)

# 静态文件
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 生成产物（图片预览等）
SKILL_ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = SKILL_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

# 路由
app.include_router(pages_router)
app.include_router(api_router, prefix="/api")
app.include_router(sse_router)


# ── 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=8765,
        reload=True,
        reload_dirs=[str(SCRIPTS_DIR)],
    )
