"""
LLM 浏览器引擎 — 通过 LLM Session + chatgpt-browser Skill 生成图片

核心思路：
  Python 只负责「准备参数 + 检查结果」，
  浏览器操作全部交给 LLM（通过 sessions_spawn 起 contentpipe-blank agent session）。

  1. Python 准备：prompt、尺寸比例、保存路径、文件名
  2. 启动 LLM session：带 chatgpt-browser skill，让 LLM 自己操控浏览器
  3. Python 检查：文件是否存在、大小是否合理、图片比例是否正确
  4. 通过 → 下一张；失败 → 重试

优势：
  - DOM 结构变了 → 改 SKILL.md，不动 Python
  - LLM 可以自适应处理意外弹窗、验证码等
  - 隔离性好：每张图一个独立 session
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import ImageEngine, ImageResult

logger = logging.getLogger(__name__)

# LLM session 超时（秒）
DEFAULT_TIMEOUT = 480  # 8 分钟，留够生成+下载时间
MAX_RETRIES = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _pid_is_live(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="ignore")
        for line in status.splitlines():
            if line.startswith("State:"):
                return "Z" not in line
        return True
    except FileNotFoundError:
        return False
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return False


class LLMBrowserEngine(ImageEngine):
    """
    LLM 驱动的浏览器图片生成引擎。

    每张图片启动一个独立的 LLM session (contentpipe-blank agent)，
    通过 chatgpt-browser skill 让 LLM 自己操控 Chrome 生成并下载图片。
    """

    engine_name = "llm-browser"
    mode = "browser"

    def __init__(
        self,
        site: str = "chatgpt",
        agent_id: str = "contentpipe-blank",
        timeout: int = DEFAULT_TIMEOUT,
        gateway_url: str | None = None,
    ):
        self.site = site
        self.agent_id = agent_id
        self.timeout = timeout
        self.gateway_url = gateway_url or os.environ.get(
            "OPENCLAW_GATEWAY_URL", "http://localhost:18789"
        )

    def _build_audit_context(self, output_path: Path, prompt: str, width: int, height: int) -> dict:
        run_id = ""
        placement_id = output_path.stem
        parts = list(output_path.parts)
        try:
            runs_idx = parts.index("runs")
            run_id = parts[runs_idx + 1]
        except Exception:
            pass
        audit_dir = output_path.parent.parent / "image_sessions"
        audit_dir.mkdir(parents=True, exist_ok=True)
        stem = placement_id or output_path.stem or f"img_{int(time.time())}"
        return {
            "run_id": run_id,
            "placement_id": stem,
            "output_path": str(output_path),
            "audit_dir": audit_dir,
            "audit_json_path": audit_dir / f"{stem}.audit.json",
            "trace_jsonl_path": audit_dir / f"{stem}.trace.jsonl",
            "agent_task_path": audit_dir / f"{stem}.agent.txt",
            "stdout_path": audit_dir / f"{stem}.stdout.log",
            "stderr_path": audit_dir / f"{stem}.stderr.log",
            "selection_json_path": audit_dir / f"{stem}.selection.json",
            "proc_json_path": audit_dir / f"{stem}.proc.json",
            "prompt_sha256": _sha256_text(prompt),
            "requested_width": width,
            "requested_height": height,
        }

    def _append_trace(self, audit: dict, event: str, **payload) -> None:
        path = audit.get("trace_jsonl_path")
        if not path:
            return
        record = {"ts": _now_iso(), "event": event, **payload}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or "", encoding="utf-8")

    def _write_proc_record(self, audit: dict, **payload) -> None:
        path = audit.get("proc_json_path")
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        try:
            if path.exists():
                current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        current.update(payload)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    def _terminate_process_group(self, pid: int | None, *, audit: dict | None = None, reason: str = "") -> None:
        if not pid:
            return
        try:
            pgid = os.getpgid(pid)
        except Exception:
            pgid = None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    os.kill(pid, sig)
            except ProcessLookupError:
                break
            except Exception:
                pass
            time.sleep(0.8)
            if not _pid_is_live(pid):
                break
        if audit:
            self._append_trace(audit, "process_group_killed", pid=pid, pgid=pgid, reason=reason)
            self._write_proc_record(audit, pid=pid, pgid=pgid, status="killed", kill_reason=reason, ended_at=_now_iso())

    @staticmethod
    def _read_json_file(path: Path) -> dict | None:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return None

    def _extract_agent_audit(self, text: str) -> dict | None:
        raw = text or ""
        m = re.search(r"AUDIT_JSON\s*:\s*", raw)
        if not m:
            return None
        tail = raw[m.end():].lstrip()
        try:
            obj, _ = json.JSONDecoder().raw_decode(tail)
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
        return None

    def _collect_text_blobs(self, obj) -> list[str]:
        blobs: list[str] = []
        if isinstance(obj, str):
            blobs.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                blobs.extend(self._collect_text_blobs(v))
        elif isinstance(obj, list):
            for v in obj:
                blobs.extend(self._collect_text_blobs(v))
        return blobs

    @staticmethod
    def _parse_cli_json(output: str) -> dict:
        raw = (output or "").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            pass
        json_start = -1
        for i, ch in enumerate(raw):
            if ch in ("{", "["):
                json_start = i
                break
        if json_start >= 0:
            try:
                return json.loads(raw[json_start:])
            except Exception:
                pass
        return {"raw": raw}

    def _browser_cli(self, args: list[str], timeout: int = 30, retries: int = 0, retry_delay: float = 2.0) -> dict:
        cmd = ["openclaw", "browser", *args, "--json", "--browser-profile", "chrome"]
        last_error = ""
        attempts = max(1, retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                parsed = self._parse_cli_json(result.stdout)
                if result.returncode == 0:
                    return parsed
                last_error = (result.stderr or "").strip() or f"browser cli failed: {' '.join(args)}"
                if parsed and not parsed.get("error"):
                    return parsed
            except subprocess.TimeoutExpired:
                last_error = f"browser cli timeout after {timeout}s: {' '.join(args)}"
            except Exception as e:
                last_error = str(e)
            if attempt < attempts:
                time.sleep(retry_delay)
        raise RuntimeError(last_error or f"browser cli failed: {' '.join(args)}")

    def _find_browser_target(self, conversation_url: str | None = None) -> str | None:
        tabs_result = self._browser_cli(["tabs"], timeout=20, retries=2, retry_delay=2.0)
        tabs = tabs_result.get("tabs", []) if isinstance(tabs_result, dict) else []
        if conversation_url:
            for tab in tabs:
                if tab.get("url") == conversation_url:
                    return tab.get("targetId")
        for tab in tabs:
            if "chatgpt.com/" in (tab.get("url") or ""):
                return tab.get("targetId")
        return None

    def _verify_download_matches_browser(self, output_path: Path, selection: dict, audit: dict | None = None) -> dict:
        selection_url = selection.get("selected_image_current_src") or selection.get("selected_image_url")
        selected_file_id = selection.get("selected_image_file_id")
        conversation_url = selection.get("conversation_url") or selection.get("page_url") or ""
        if not selection_url or not selected_file_id:
            return {"ok": False, "reason": "selection proof missing selected_image_current_src or selected_image_file_id"}

        target_id = self._find_browser_target(conversation_url)
        if not target_id:
            return {"ok": False, "reason": "unable to find browser target for conversation"}

        safe_url = json.dumps(selection_url)
        safe_file_id = json.dumps(selected_file_id)
        fn = f"""(() => (async () => {{
  const selectedUrl = {safe_url};
  const selectedFileId = {safe_file_id};
  const imgs = Array.from(document.querySelectorAll('img[src*=\"estuary\"], img[src*=\"oaiusercontent\"], img[src*=\"backend-api/estuary\"]'));
  const uniqueFileIds = [...new Set(imgs.map(img => {{
    const src = img.currentSrc || img.src || '';
    const m = src.match(/id=([^&]+)/);
    return m ? m[1] : null;
  }}).filter(Boolean))];
  const inDom = uniqueFileIds.includes(selectedFileId);
  const r = await fetch(selectedUrl, {{ credentials: 'include' }});
  const buf = await r.arrayBuffer();
  const hash = await crypto.subtle.digest('SHA-256', buf);
  const sha256 = Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2,'0')).join('');
  return JSON.stringify({{
    ok: true,
    inDom,
    uniqueFileIds,
    byteLength: buf.byteLength,
    status: r.status,
    contentType: r.headers.get('content-type'),
    sha256
  }});
}})())()"""
        eval_result = self._browser_cli(
            ["evaluate", "--target-id", target_id, "--fn", fn],
            timeout=120,
            retries=2,
            retry_delay=3.0,
        )
        raw = eval_result.get("result") or eval_result.get("raw") or eval_result.get("value")
        if not raw:
            return {"ok": False, "reason": "browser evaluate returned no result"}
        try:
            browser_check = json.loads(raw)
        except Exception as e:
            return {"ok": False, "reason": f"failed to parse browser verify result: {e}"}

        local_sha = _sha256_file(output_path)
        browser_check["local_sha256"] = local_sha
        browser_check["selected_file_id"] = selected_file_id
        browser_check["selected_image_current_src"] = selection_url
        browser_check["conversation_url"] = conversation_url
        browser_check["ok"] = bool(browser_check.get("inDom")) and browser_check.get("sha256") == local_sha
        if audit:
            self._append_trace(audit, "browser_verify", **{k: v for k, v in browser_check.items() if k != "selected_image_current_src"})
        if not browser_check.get("inDom"):
            browser_check["reason"] = "selected file_id not present in current DOM"
        elif browser_check.get("sha256") != local_sha:
            browser_check["reason"] = "browser fetched bytes do not match local file"
        return browser_check

    def _finalize_audit(self, audit: dict, *, session_id: str, status: str,
                        task: str, spawn_result: dict | None, output_path: Path,
                        elapsed_ms: int, check: dict, error: str = "") -> None:
        stdout_text = (spawn_result or {}).get("stdout", "") or ""
        stderr_text = (spawn_result or {}).get("stderr", "") or ""
        parsed = (spawn_result or {}).get("parsed")
        agent_audit = None
        if stdout_text:
            agent_audit = self._extract_agent_audit(stdout_text)
        if agent_audit is None and parsed is not None:
            for blob in self._collect_text_blobs(parsed):
                agent_audit = self._extract_agent_audit(blob)
                if agent_audit:
                    break

        file_meta = {
            "path": str(output_path),
            "exists": output_path.exists(),
            "bytes": output_path.stat().st_size if output_path.exists() else 0,
            "sha256": _sha256_file(output_path) if output_path.exists() else "",
            "mime": f"image/{check.get('format','')}" if check.get("ok") else "",
            "width": 0,
            "height": 0,
        }
        dims = self._get_image_dimensions(output_path) if output_path.exists() else None
        if dims:
            file_meta["width"], file_meta["height"] = dims

        summary = {
            "run_id": audit.get("run_id", ""),
            "placement_id": audit.get("placement_id", ""),
            "engine": f"llm-browser:{self.site}",
            "agent_id": self.agent_id,
            "session_id": session_id,
            "status": status,
            "started_at": audit.get("started_at", ""),
            "ended_at": _now_iso(),
            "duration_ms": elapsed_ms,
            "prompt": {
                "text": task,
                "sha256": _sha256_text(task),
            },
            "browser": {
                "profile": "chrome",
                "site": self.site,
            },
            "agent_audit": agent_audit or {},
            "spawn": {
                "status": (spawn_result or {}).get("status", ""),
                "returncode": (spawn_result or {}).get("returncode"),
            },
            "output_file": file_meta,
            "validation": check,
            "error": error,
            "artifacts": {
                "agent_task_path": str(audit.get("agent_task_path")),
                "stdout_path": str(audit.get("stdout_path")),
                "stderr_path": str(audit.get("stderr_path")),
                "trace_path": str(audit.get("trace_jsonl_path")),
                "selection_json_path": str(audit.get("selection_json_path")),
                "proc_json_path": str(audit.get("proc_json_path")),
            },
        }
        (audit["audit_json_path"]).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

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
        """通过 LLM session 生成一张图片"""
        start = time.time()
        output_path = Path(output_path) if output_path else Path(f"/tmp/llm_gen_{int(time.time())}.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audit = self._build_audit_context(output_path, prompt, width, height)
        audit["started_at"] = _now_iso()
        self._append_trace(audit, "generate_start", output_path=str(output_path), width=width, height=height)

        # 构建比例说明
        ratio_hint = self._get_ratio_hint(width, height)

        # 构建 LLM 任务指令
        task = self._build_task(prompt, ratio_hint, width, height, str(output_path), str(audit["selection_json_path"]))
        self._write_text(audit["agent_task_path"], task)
        self._append_trace(audit, "agent_task_written", path=str(audit["agent_task_path"]))

        logger.info("llm_browser[%s]: spawning session for %s (%dx%d)",
                     self.site, output_path.name, width, height)

        spawn_result = None
        cancel_check = kwargs.get("cancel_check")
        try:
            # 通过 openclaw CLI 启动 session
            spawn_result = self._spawn_session(task, audit=audit, cancel_check=cancel_check)

            if spawn_result is None:
                raise RuntimeError("LLM session spawn failed or timed out")
            if (spawn_result or {}).get("status") == "cancelled":
                raise RuntimeError((spawn_result or {}).get("cancel_reason") or "generation cancelled")

            self._write_text(audit["stdout_path"], (spawn_result or {}).get("stdout", "") or "")
            self._write_text(audit["stderr_path"], (spawn_result or {}).get("stderr", "") or "")
            self._append_trace(
                audit,
                "agent_turn_completed",
                session_id=(spawn_result or {}).get("session_id", ""),
                status=(spawn_result or {}).get("status", ""),
                returncode=(spawn_result or {}).get("returncode"),
            )

            # 等待文件落盘（agent 的 browser/exec 操作可能异步完成）
            elapsed = int((time.time() - start) * 1000)
            check = self._wait_for_file(
                output_path,
                width,
                height,
                max_wait=120,
                audit=audit,
                cancel_check=cancel_check,
                spawn_result=spawn_result,
            )

            if check["ok"]:
                selection = self._read_json_file(audit["selection_json_path"])
                if not selection:
                    raise RuntimeError(f"selection proof not found: {audit['selection_json_path']}")

                verify = self._verify_download_matches_browser(output_path, selection, audit=audit)
                check["selection"] = {
                    "selected_image_file_id": selection.get("selected_image_file_id"),
                    "conversation_url": selection.get("conversation_url") or selection.get("page_url"),
                }
                check["browser_verify"] = {k: v for k, v in verify.items() if k != "selected_image_current_src"}
                if not verify.get("ok"):
                    raise RuntimeError(f"browser verification failed: {verify.get('reason', 'unknown reason')}")

                logger.info("llm_browser[%s]: success, file=%s size=%d",
                            self.site, output_path.name, check["size"])
                self._finalize_audit(
                    audit,
                    session_id=(spawn_result or {}).get("session_id", ""),
                    status="success",
                    task=task,
                    spawn_result=spawn_result,
                    output_path=output_path,
                    elapsed_ms=elapsed,
                    check=check,
                )
                return ImageResult(
                    success=True,
                    file_path=str(output_path),
                    engine=f"llm-browser:{self.site}",
                    prompt_used=prompt,
                    seed_used=seed,
                    generation_time_ms=elapsed,
                    width=width,
                    height=height,
                    metadata={
                        "site": self.site,
                        "agent": self.agent_id,
                        "session_id": (spawn_result or {}).get("session_id", ""),
                        "audit_path": str(audit["audit_json_path"]),
                        "trace_path": str(audit["trace_jsonl_path"]),
                        "selection_json_path": str(audit["selection_json_path"]),
                    },
                )
            else:
                raise RuntimeError(f"Image check failed: {check['reason']}")

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error("llm_browser[%s]: failed: %s", self.site, e)
            self._append_trace(audit, "generate_failed", error=str(e))
            self._finalize_audit(
                audit,
                session_id=(spawn_result or {}).get("session_id", "") if spawn_result else "",
                status="failed",
                task=task,
                spawn_result=spawn_result,
                output_path=output_path,
                elapsed_ms=elapsed,
                check={"ok": False, "reason": str(e)},
                error=str(e),
            )
            return ImageResult(
                success=False,
                engine=f"llm-browser:{self.site}",
                prompt_used=prompt,
                generation_time_ms=elapsed,
                error=str(e),
                metadata={
                    "site": self.site,
                    "agent": self.agent_id,
                    "audit_path": str(audit["audit_json_path"]),
                    "trace_path": str(audit["trace_jsonl_path"]),
                },
            )

    def is_available(self) -> bool:
        """检查 gateway 是否可达"""
        try:
            result = subprocess.run(
                ["openclaw", "status"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    # ── 内部方法 ──────────────────────────────────────────

    def _build_task(
        self,
        prompt: str,
        ratio_hint: str,
        width: int,
        height: int,
        output_path: str,
        selection_json_path: str,
    ) -> str:
        """构建给 LLM 的任务指令"""
        return f"""使用 contentpipe-chatgpt-browser 技能，在 ChatGPT 上生成一张图片并下载到本地。

## 任务要求

1. **先阅读 contentpipe-chatgpt-browser 的 SKILL.md**，严格按照技能文档操作
2. 导航到 ChatGPT 图片生成页面（/images）
3. 输入以下提示词生成图片：

```
{prompt}

Image size: {ratio_hint}, {width}x{height} pixels
```

4. 等待图片生成完成
5. **下载原图**（不是截图！）到以下路径：

```
{output_path}
```

6. **必须把你最终选中的图片证据写入下面这个本地 JSON 文件**（这是内部校验用，允许写完整带 sig 的 URL）：

```
{selection_json_path}
```

证据文件必须是合法 JSON，且至少包含：

```json
{{
  "page_url": "当前页面 URL",
  "conversation_url": "当前对话 URL",
  "selection_strategy": "你实际使用的选择策略",
  "selected_image_file_id": "file_xxx",
  "selected_image_current_src": "完整 currentSrc，必须包含 sig 参数",
  "selected_image_natural_width": 1536,
  "selected_image_natural_height": 1024,
  "selected_image_alt": "图片 alt（如有）",
  "download_button_seen": true
}}
```

必须使用 write 工具把这个 JSON 写到**准确路径**，不能只在回复里口头说明。

## 下载方法（参考 SKILL.md）

- 使用 evaluate 提取图片的真实 URL（oaiusercontent / estuary 域名）
- 获取 cookies
- 使用 curl 下载原图到指定路径
- 确保下载的是完整的 PNG/JPG 图片文件（不是 HTML 页面）

## 完成标准

- 文件已保存到 `{output_path}`
- 文件大小 > 10KB（确认是真实图片，不是错误页面）
- 完成后输出：DONE

## 审计输出（必须追加在 DONE 后面）

在 `DONE` 之后，继续输出以下结构化块（不要用 markdown code fence）：

AUDIT_JSON:
{{
  "page_url": "当前页面 URL（如 /images 或 /c/...）",
  "conversation_url": "当前对话 URL（如有）",
  "selection_strategy": "你如何锁定当前这轮生成图片，例如 latest-image-block",
  "download_button_seen": true,
  "image_count_in_selected_block": 1,
  "selected_image_url_domain": "仅域名或路径前缀，不要输出完整签名 URL",
  "selected_image_file_id": "如能提取 file_xxx 则填，否则 null",
  "selected_image_natural_width": 1536,
  "selected_image_natural_height": 1024,
  "cookie_names": ["oai-did", "_puid", "oai-sc"],
  "download_method": "curl",
  "notes": "可选备注",
  "warnings": []
}}

注意：
- **不要输出完整 cookies**
- **不要输出完整带 sig 的图片 URL**
- 只输出可审计但不泄密的信息"""

    def _spawn_session(self, task: str, *, audit: dict | None = None, cancel_check=None) -> dict | None:
        """通过 openclaw agent CLI 启动 LLM agent turn。"""
        session_id = f"contentpipe-img-{int(time.time())}"
        cmd = [
            "openclaw", "agent",
            "--agent", self.agent_id,
            "--session-id", session_id,
            "--message", task,
            "--timeout", str(self.timeout),
            "--json",
        ]

        logger.info("llm_browser: running agent turn (session=%s, timeout=%ds)", session_id, self.timeout)
        deadline = time.time() + self.timeout + 60
        proc = None
        stdout = ""
        stderr = ""
        pid = None
        pgid = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(Path.home()),
                start_new_session=True,
            )
            pid = proc.pid
            try:
                pgid = os.getpgid(pid)
            except Exception:
                pgid = None
            if audit:
                self._append_trace(audit, "agent_turn_spawned", session_id=session_id, pid=pid, pgid=pgid)
                self._write_proc_record(audit, session_id=session_id, pid=pid, pgid=pgid, status="running", started_at=_now_iso())

            while True:
                if cancel_check:
                    cancel_reason = cancel_check()
                    if cancel_reason:
                        logger.info("llm_browser: cancelling agent turn (session=%s): %s", session_id, cancel_reason)
                        self._terminate_process_group(pid, audit=audit, reason=cancel_reason)
                        try:
                            stdout, stderr = proc.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            stdout, stderr = "", ""
                        return {
                            "status": "cancelled",
                            "session_id": session_id,
                            "returncode": proc.returncode,
                            "stdout": (stdout or "").strip(),
                            "stderr": (stderr or "").strip(),
                            "parsed": None,
                            "pid": pid,
                            "pgid": pgid,
                            "cancel_reason": cancel_reason,
                        }

                remaining = max(0.5, min(2.0, deadline - time.time()))
                if time.time() >= deadline:
                    logger.warning("llm_browser: agent turn timed out after %ds (session=%s)", self.timeout + 60, session_id)
                    self._terminate_process_group(pid, audit=audit, reason="timeout")
                    try:
                        stdout, stderr = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        stdout, stderr = "", ""
                    return {
                        "status": "timeout",
                        "session_id": session_id,
                        "returncode": proc.returncode,
                        "stdout": (stdout or "").strip(),
                        "stderr": (stderr or "").strip(),
                        "parsed": None,
                        "pid": pid,
                        "pgid": pgid,
                    }

                try:
                    stdout, stderr = proc.communicate(timeout=remaining)
                    break
                except subprocess.TimeoutExpired:
                    continue

            stdout = stdout.strip() if stdout else ""
            stderr = stderr.strip() if stderr else ""
            stdout_lines = [
                line for line in stdout.split("\n")
                if not any(line.startswith(p) for p in [
                    "[plugins]", "[gateway]", "[agent]", "[session]",
                    "[channel", "🦞", "WARN ", "  WARN", "  Fix:",
                ])
            ]
            clean_stdout = "\n".join(stdout_lines).strip()
            parsed = None
            try:
                parsed = json.loads(clean_stdout)
            except (json.JSONDecodeError, ValueError):
                parsed = None

            status = "completed" if proc.returncode == 0 else "error"
            if proc.returncode == 0:
                logger.info("llm_browser: agent turn completed (session=%s)", session_id)
            else:
                logger.warning("llm_browser: agent turn failed (rc=%s, session=%s): %s", proc.returncode, session_id, stderr[:300])

            if audit:
                self._write_proc_record(audit, session_id=session_id, pid=pid, pgid=pgid, status=status, returncode=proc.returncode, ended_at=_now_iso())

            return {
                "status": status,
                "session_id": session_id,
                "returncode": proc.returncode,
                "stdout": clean_stdout,
                "stderr": stderr,
                "parsed": parsed,
                "pid": pid,
                "pgid": pgid,
            }
        except Exception:
            if proc and proc.poll() is None:
                self._terminate_process_group(pid, audit=audit, reason="spawn_exception")
            raise

    def _wait_for_file(self, output_path: Path, width: int, height: int, max_wait: int = 120, audit: dict | None = None, cancel_check=None, spawn_result: dict | None = None) -> dict:
        """等待文件出现并通过检查。

        Agent turn 返回后，浏览器/exec 操作可能仍在异步执行（DALL-E 生成 + curl 下载）。
        轮询等待文件出现，最多等 max_wait 秒。
        """
        import time as _time

        # 先立即检查一次
        check = self._check_result(output_path, width, height)
        if check["ok"]:
            if audit:
                self._append_trace(audit, "file_ready_immediate", size=check.get("size", 0), format=check.get("format", ""))
            return check

        logger.info("llm_browser: file not ready yet, polling up to %ds for %s", max_wait, output_path.name)
        if audit:
            self._append_trace(audit, "file_poll_start", max_wait=max_wait)
        poll_interval = 5  # 每 5 秒检查一次
        waited = 0
        while waited < max_wait:
            if cancel_check:
                cancel_reason = cancel_check()
                if cancel_reason:
                    self._terminate_process_group((spawn_result or {}).get("pid"), audit=audit, reason=cancel_reason)
                    return {"ok": False, "reason": f"cancelled: {cancel_reason}"}
            _time.sleep(poll_interval)
            waited += poll_interval
            check = self._check_result(output_path, width, height)
            if check["ok"]:
                logger.info("llm_browser: file appeared after %ds polling for %s", waited, output_path.name)
                if audit:
                    self._append_trace(audit, "file_ready_after_poll", waited_seconds=waited, size=check.get("size", 0), format=check.get("format", ""))
                return check
            # 如果文件存在但太小，可能还在下载中
            if output_path.exists():
                size = output_path.stat().st_size
                if size > 0:
                    logger.info("llm_browser: file exists but %d bytes (waiting for complete download)", size)
                    if audit:
                        self._append_trace(audit, "file_exists_waiting", waited_seconds=waited, size=size)

        # 最终检查
        final = self._check_result(output_path, width, height)
        if not final["ok"]:
            logger.warning("llm_browser: file still not ready after %ds for %s: %s", max_wait, output_path.name, final.get("reason"))
            if audit:
                self._append_trace(audit, "file_poll_failed", waited_seconds=max_wait, reason=final.get("reason", "unknown"))
        return final

    def _check_result(self, output_path: Path, width: int, height: int) -> dict:
        """检查生成结果

        检查项：
        1. 文件是否存在
        2. 文件大小是否合理（>10KB，排除错误页面）
        3. 是否是有效图片（尝试读取头部 magic bytes）
        """
        if not output_path.exists():
            return {"ok": False, "reason": "file not found"}

        size = output_path.stat().st_size
        if size < 10 * 1024:  # < 10KB
            return {"ok": False, "reason": f"file too small ({size} bytes), likely not a real image"}

        # 检查 magic bytes
        with open(output_path, "rb") as f:
            header = f.read(16)

        # PNG: 89 50 4E 47
        # JPEG: FF D8 FF
        # WebP: 52 49 46 46 ... 57 45 42 50
        is_png = header[:4] == b'\x89PNG'
        is_jpeg = header[:3] == b'\xff\xd8\xff'
        is_webp = header[:4] == b'RIFF' and header[8:12] == b'WEBP'

        if not (is_png or is_jpeg or is_webp):
            return {"ok": False, "reason": "file is not a valid image (bad magic bytes)",
                    "header_hex": header[:8].hex()}

        # 可选：检查图片实际尺寸（需要 Pillow）
        actual_dims = self._get_image_dimensions(output_path)
        if actual_dims:
            aw, ah = actual_dims
            # 允许 ±20% 的尺寸偏差（DALL-E 可能不精确匹配请求尺寸）
            w_ratio = aw / width if width else 1
            h_ratio = ah / height if height else 1
            if w_ratio < 0.5 or w_ratio > 2.0 or h_ratio < 0.5 or h_ratio > 2.0:
                logger.warning("llm_browser: image dimensions %dx%d differ significantly from "
                               "requested %dx%d", aw, ah, width, height)
                # 不作为失败，只是警告

        return {"ok": True, "size": size, "format": "png" if is_png else "jpeg" if is_jpeg else "webp"}

    @staticmethod
    def _get_image_dimensions(path: Path) -> tuple[int, int] | None:
        """尝试获取图片实际尺寸"""
        try:
            from PIL import Image
            with Image.open(path) as img:
                return img.size
        except Exception:
            return None

    @staticmethod
    def _get_ratio_hint(width: int, height: int) -> str:
        """生成比例描述"""
        if width == height:
            return "1:1 square aspect ratio"
        elif width > height:
            r = width / height
            if abs(r - 16 / 9) < 0.1:
                return "16:9 landscape aspect ratio"
            elif abs(r - 3 / 2) < 0.1:
                return "3:2 landscape aspect ratio"
            elif abs(r - 4 / 3) < 0.1:
                return "4:3 landscape aspect ratio"
            elif abs(r - 2.35) < 0.15:
                return "2.35:1 cinematic widescreen aspect ratio"
            else:
                return f"{width}x{height} landscape"
        else:
            r = height / width
            if abs(r - 16 / 9) < 0.1:
                return "9:16 portrait aspect ratio"
            elif abs(r - 3 / 2) < 0.1:
                return "2:3 portrait aspect ratio"
            elif abs(r - 4 / 3) < 0.1:
                return "3:4 portrait aspect ratio"
            else:
                return f"{width}x{height} portrait"
