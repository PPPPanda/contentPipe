from __future__ import annotations

import hashlib
import os
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

AUTH_ENV = "CONTENTPIPE_AUTH_TOKEN"
AUTH_COOKIE = "contentpipe_auth"
AUTH_HEADER = "x-contentpipe-token"

PUBLIC_PATH_PREFIXES = (
    "/static",
    "/api/health",
    "/api/info",
    "/login",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
)


def get_auth_token() -> str:
    return os.environ.get(AUTH_ENV, "").strip()


def is_auth_enabled() -> bool:
    return bool(get_auth_token())


def hash_token(token: str) -> str:
    return hashlib.sha256(f"contentpipe:{token}".encode("utf-8")).hexdigest()


def is_authenticated_request(request: Request) -> bool:
    token = get_auth_token()
    if not token:
        return True

    cookie_val = request.cookies.get(AUTH_COOKIE, "")
    if cookie_val and cookie_val == hash_token(token):
        return True

    header_val = request.headers.get(AUTH_HEADER, "") or request.headers.get("authorization", "")
    if header_val.lower().startswith("bearer "):
        header_val = header_val[7:].strip()
    if header_val and header_val == token:
        return True

    return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not is_auth_enabled():
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        if is_authenticated_request(request):
            return await call_next(request)

        if path.startswith("/api/") or request.headers.get("hx-request") == "true":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)
