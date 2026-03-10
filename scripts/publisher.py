#!/usr/bin/env python3
"""
ContentPipe Publisher — 微信公众号/小红书发布

独立可执行脚本。

用法:
  python3 publisher.py --run-id run_xxx --platform wechat
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
import yaml


DEFAULT_OUTPUT_BASE = Path(__file__).parent.parent.parent.parent / "work" / "content-pipeline" / "output" / "runs"


# ── 微信 API ─────────────────────────────────────────────────

def wechat_get_token(appid: str, secret: str) -> str:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}")
        data = resp.json()
        if "access_token" in data:
            return data["access_token"]
        raise RuntimeError(f"WeChat token error: {data}")


def wechat_upload_image(token: str, image_bytes: bytes, filename: str = "image.png") -> str:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"https://api.weixin.qq.com/cgi-bin/media/uploadimg?access_token={token}",
            files={"media": (filename, image_bytes, "image/png")},
        )
        data = resp.json()
        if "url" in data:
            return data["url"]
        raise RuntimeError(f"WeChat upload error: {data}")


def wechat_create_draft(token: str, article: dict) -> str:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={token}",
            json={"articles": [{
                "title": article.get("title", ""),
                "content": article.get("content_html", ""),
                "digest": article.get("subtitle", ""),
                "author": article.get("author", "ContentPipe"),
                "thumb_media_id": article.get("thumb_media_id", ""),
            }]},
        )
        data = resp.json()
        if "media_id" in data:
            return data["media_id"]
        raise RuntimeError(f"WeChat draft error: {data}")


# ── 发布逻辑 ─────────────────────────────────────────────────

def publish_wechat(output_dir: Path) -> dict:
    state = yaml.safe_load((output_dir / "state.yaml").read_text(encoding="utf-8"))
    article = state.get("article", {})
    run_id = state.get("run_id", "")

    appid = os.environ.get("WECHAT_APPID", "")
    secret = os.environ.get("WECHAT_SECRET", "")

    if not appid or not secret:
        print("⚠️  WECHAT_APPID/WECHAT_SECRET 未配置，跳过发布")
        return {"platform": "wechat", "status": "local_only", "note": "凭证未配置"}

    html_path = output_dir / "formatted.html"
    if not html_path.exists():
        return {"platform": "wechat", "status": "failed", "error": "formatted.html not found"}
    html = html_path.read_text(encoding="utf-8")

    try:
        token = wechat_get_token(appid, secret)
        print(f"🔑 WeChat token obtained")

        # 上传图片到 CDN
        selected = state.get("selected_images", {})
        generated = state.get("generated_images", [])
        for pid, option in selected.items():
            for img in generated:
                if img.get("placement_id") == pid and img.get("option") == option:
                    fpath = img.get("file_path", "")
                    if fpath and os.path.exists(fpath):
                        cdn_url = wechat_upload_image(token, open(fpath, "rb").read(), f"{pid}.png")
                        local_url = f"/api/runs/{run_id}/images/{os.path.basename(fpath)}"
                        html = html.replace(local_url, cdn_url)
                        print(f"☁️  {pid} → {cdn_url[:50]}...")
                    break

        media_id = wechat_create_draft(token, {
            "title": article.get("title", ""),
            "content_html": html,
            "subtitle": article.get("subtitle", ""),
        })
        print(f"✅ Draft created: {media_id}")
        return {"platform": "wechat", "status": "draft", "media_id": media_id}

    except Exception as e:
        print(f"❌ {e}")
        return {"platform": "wechat", "status": "failed", "error": str(e)}


def publish_xhs(output_dir: Path) -> dict:
    """小红书：保存内容到 JSON（待浏览器自动化接入）"""
    state = yaml.safe_load((output_dir / "state.yaml").read_text(encoding="utf-8"))
    article = state.get("article", {})

    content = {
        "title": article.get("title", ""),
        "content": state.get("article_edited", article.get("content", "")),
        "tags": article.get("tags", []),
        "images": [img.get("file_path", "") for img in state.get("generated_images", []) if img.get("success")],
    }
    (output_dir / "xhs_content.json").write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    print("📝 XHS content saved to xhs_content.json")
    return {"platform": "xhs", "status": "local_only"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ContentPipe Publisher")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--platform", default="wechat")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_BASE / args.run_id

    if args.platform == "wechat":
        result = publish_wechat(output_dir)
    elif args.platform == "xhs":
        result = publish_xhs(output_dir)
    else:
        result = {"error": f"Unknown platform: {args.platform}"}

    (output_dir / "publish_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
