"""
ContentPipe — 产物管理 API

读取/修改 run 产物文件，上传封面/配图。
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse

from web.run_manager import _load_raw_state, _save_state

router = APIRouter()

OUTPUT_DIR = Path(__file__).parent.parent.parent.parent / "output"


def _run_dir(run_id: str) -> Path:
    d = OUTPUT_DIR / "runs" / run_id
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return d


# ── 产物列表 ──────────────────────────────────────────────────

@router.get("/runs/{run_id}/artifacts")
async def api_list_artifacts(run_id: str):
    """列出 run 的所有产物文件"""
    d = _run_dir(run_id)
    artifacts = []
    for f in sorted(d.iterdir()):
        if f.name.startswith("."):
            continue
        if f.is_dir():
            # 图片目录
            if f.name == "images":
                for img in sorted(f.iterdir()):
                    artifacts.append({
                        "name": f"images/{img.name}",
                        "size": img.stat().st_size,
                        "type": "image",
                    })
            continue
        ext = f.suffix.lower()
        ftype = {
            ".yaml": "yaml", ".yml": "yaml", ".json": "json",
            ".md": "markdown", ".html": "html", ".txt": "text",
            ".png": "image", ".jpg": "image", ".jpeg": "image",
        }.get(ext, "binary")
        artifacts.append({
            "name": f.name,
            "size": f.stat().st_size,
            "type": ftype,
        })
    return {"run_id": run_id, "artifacts": artifacts, "count": len(artifacts)}


@router.get("/runs/{run_id}/artifacts/{filename:path}")
async def api_get_artifact(run_id: str, filename: str):
    """读取产物文件内容"""
    d = _run_dir(run_id)
    path = d / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Artifact not found: {filename}")

    # 图片返回文件
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return FileResponse(path)

    content = path.read_text(encoding="utf-8")

    # 尝试解析结构化数据
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            parsed = yaml.safe_load(content)
            return {"name": filename, "type": "yaml", "content": content, "parsed": parsed}
        except Exception:
            pass

    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(content)
            return {"name": filename, "type": "json", "content": content, "parsed": parsed}
        except Exception:
            pass

    return {"name": filename, "type": path.suffix.lstrip("."), "content": content}


@router.put("/runs/{run_id}/artifacts/{filename:path}")
async def api_put_artifact(run_id: str, filename: str, request: Request):
    """写入/修改产物文件

    ```json
    {"content": "文件内容..."}
    ```
    """
    d = _run_dir(run_id)
    body = await request.json()
    content = body.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="Content required")

    path = d / filename
    # 安全检查：不允许路径穿越
    if ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # 保存 .prev
    if path.exists():
        prev = d / f"{filename}.prev"
        prev.parent.mkdir(parents=True, exist_ok=True)
        prev.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "name": filename, "size": len(content)}


# ── 导演阶段：封面/配图管理 ──────────────────────────────────

@router.get("/runs/{run_id}/visual-plan")
async def api_get_visual_plan(run_id: str):
    """获取导演视觉方案（封面 + 配图规划）"""
    d = _run_dir(run_id)
    vp_path = d / "visual_plan.json"
    if not vp_path.exists():
        return {"run_id": run_id, "visual_plan": None, "has_images": False}

    vp = json.loads(vp_path.read_text(encoding="utf-8"))

    # 检查已生成的图片
    images_dir = d / "images"
    existing_images = []
    if images_dir.exists():
        existing_images = [f.name for f in sorted(images_dir.iterdir()) if f.is_file()]

    return {
        "run_id": run_id,
        "visual_plan": vp,
        "existing_images": existing_images,
        "has_images": len(existing_images) > 0,
    }


@router.put("/runs/{run_id}/visual-plan")
async def api_set_visual_plan(run_id: str, request: Request):
    """直接设置/修改视觉方案

    ```json
    {
      "style": "tech-digital",
      "cover": {"title": "...", "description": "..."},
      "placements": [{"id": "img_001", "after_section": "...", "description": "..."}]
    }
    ```
    """
    d = _run_dir(run_id)
    body = await request.json()

    vp_path = d / "visual_plan.json"
    # 备份
    if vp_path.exists():
        (d / "visual_plan.json.prev").write_text(vp_path.read_text(encoding="utf-8"), encoding="utf-8")

    vp_path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    # 同步到 state
    raw = _load_raw_state(run_id)
    if raw:
        raw["visual_plan"] = body
        _save_state(raw)

    return {"ok": True, "message": "Visual plan updated"}


@router.post("/runs/{run_id}/images/upload-cover")
async def api_upload_cover(run_id: str, request: Request):
    """上传封面图片

    支持两种方式：
    1. multipart/form-data: file 字段
    2. JSON: {"image": "data:image/png;base64,..."}  或  {"image": "base64string", "filename": "cover.png"}
    """
    d = _run_dir(run_id)
    images_dir = d / "images"
    images_dir.mkdir(exist_ok=True)

    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(status_code=400, detail="No file uploaded")
        data = await file.read()
        ext = Path(file.filename).suffix or ".png"
        dest = images_dir / f"cover{ext}"
        dest.write_bytes(data)
    else:
        body = await request.json()
        image_data = body.get("image", "")
        if not image_data:
            raise HTTPException(status_code=400, detail="No image data")

        # 解析 data URI 或纯 base64
        if image_data.startswith("data:"):
            header, b64 = image_data.split(",", 1)
            ext = ".png"
            if "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "webp" in header:
                ext = ".webp"
        else:
            b64 = image_data
            ext = body.get("ext", ".png")

        data = base64.b64decode(b64)
        dest = images_dir / f"cover{ext}"
        dest.write_bytes(data)

    # 更新 generated_cover.json
    cover_meta = d / "generated_cover.json"
    cover_meta.write_text(json.dumps({
        "source": "upload",
        "filename": dest.name,
        "size": len(data),
    }, ensure_ascii=False), encoding="utf-8")

    return {"ok": True, "filename": dest.name, "size": len(data), "path": f"images/{dest.name}"}


@router.post("/runs/{run_id}/images/upload-placement")
async def api_upload_placement_image(run_id: str, request: Request):
    """上传配图（指定 placement ID）

    JSON:
    ```json
    {"placement_id": "img_001", "image": "data:image/png;base64,..."}
    ```

    或 multipart: file + placement_id 字段
    """
    d = _run_dir(run_id)
    images_dir = d / "images"
    images_dir.mkdir(exist_ok=True)

    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        form = await request.form()
        file = form.get("file")
        pid = form.get("placement_id", "img_001")
        if not file:
            raise HTTPException(status_code=400, detail="No file uploaded")
        data = await file.read()
        ext = Path(file.filename).suffix or ".png"
        dest = images_dir / f"{pid}{ext}"
        dest.write_bytes(data)
    else:
        body = await request.json()
        pid = body.get("placement_id", "img_001")
        image_data = body.get("image", "")
        if not image_data:
            raise HTTPException(status_code=400, detail="No image data")

        if image_data.startswith("data:"):
            header, b64 = image_data.split(",", 1)
            ext = ".png"
            if "jpeg" in header or "jpg" in header:
                ext = ".jpg"
        else:
            b64 = image_data
            ext = body.get("ext", ".png")

        data = base64.b64decode(b64)
        dest = images_dir / f"{pid}{ext}"
        dest.write_bytes(data)

    # 更新 generated_images.json
    gi_path = d / "generated_images.json"
    gi_list: list = []
    if gi_path.exists():
        try:
            raw = json.loads(gi_path.read_text(encoding="utf-8"))
            gi_list = raw if isinstance(raw, list) else []
        except Exception:
            pass

    # 更新或追加
    found = False
    for item in gi_list:
        if isinstance(item, dict) and item.get("placement_id") == pid:
            item.update({"source": "upload", "file_path": str(dest), "filename": dest.name, "success": True, "error": ""})
            found = True
            break
    if not found:
        gi_list.append({"placement_id": pid, "source": "upload", "file_path": str(dest), "filename": dest.name, "success": True})

    gi_path.write_text(json.dumps(gi_list, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"ok": True, "placement_id": pid, "filename": dest.name, "size": len(data)}


# ── Run 增强操作 ─────────────────────────────────────────────

@router.post("/runs/{run_id}/clone")
async def api_clone_run(run_id: str, request: Request):
    """克隆一个 run（可修改主题）

    ```json
    {"new_topic": "新的主题（可选）"}
    ```
    """
    d = _run_dir(run_id)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}

    from datetime import datetime
    new_run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    new_dir = OUTPUT_DIR / "runs" / new_run_id

    # 只复制 topic.yaml 和 state 骨架
    new_dir.mkdir(parents=True, exist_ok=True)

    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404)

    # 新 state
    new_state = {
        "run_id": new_run_id,
        "status": "pending",
        "current_stage": "",
        "platform": raw.get("platform", "wechat"),
        "auto_approve": raw.get("auto_approve", False),
        "user_topic": body.get("new_topic", raw.get("user_topic", "")),
        "topic": {"title": body.get("new_topic", raw.get("topic", {}).get("title", ""))},
        "cloned_from": run_id,
    }
    _save_state(new_state)

    return {"ok": True, "new_run_id": new_run_id, "cloned_from": run_id}


@router.get("/runs/{run_id}/timeline")
async def api_timeline(run_id: str):
    """获取 run 的执行时间线"""
    d = _run_dir(run_id)
    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404)

    # 从 chat 文件推断时间线
    timeline = []
    for node in ["scout", "researcher", "writer", "de_ai_editor", "director", "formatter", "publisher"]:
        chat_file = d / f"chat_{node}.json"
        if chat_file.exists():
            try:
                msgs = json.loads(chat_file.read_text(encoding="utf-8"))
                if msgs:
                    first_ts = msgs[0].get("timestamp", "")
                    last_ts = msgs[-1].get("timestamp", "")
                    timeline.append({
                        "node": node,
                        "started": first_ts,
                        "ended": last_ts,
                        "messages": len(msgs),
                    })
            except Exception:
                pass

    return {
        "run_id": run_id,
        "status": raw.get("status", ""),
        "current_stage": raw.get("current_stage", ""),
        "timeline": timeline,
    }


@router.post("/runs/{run_id}/auto-approve")
async def api_auto_approve(run_id: str, request: Request):
    """开启/关闭全自动模式

    ```json
    {"enabled": true}
    ```
    """
    body = await request.json()
    raw = _load_raw_state(run_id)
    if not raw:
        raise HTTPException(status_code=404)

    raw["auto_approve"] = body.get("enabled", True)
    _save_state(raw)

    return {"ok": True, "auto_approve": raw["auto_approve"]}
