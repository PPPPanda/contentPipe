#!/usr/bin/env python3
"""
ContentPipe Formatter — Markdown → 微信/小红书兼容 HTML

独立可执行脚本，不依赖 LLM 或 OpenClaw。

用法:
  python3 formatter.py --run-id run_xxx --output-dir /path/to/output
  python3 formatter.py --run-id run_xxx  # 默认 output-dir
"""

from __future__ import annotations

import argparse
from typing import Any
import html as html_mod
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import jinja2
import yaml

from logutil import get_logger
from tools import call_llm, load_pipeline_config, resolve_role_model

logger = get_logger(__name__)


SKILL_DIR = Path(__file__).parent.parent
CONFIG_DIR = SKILL_DIR / "config"
TEMPLATES_DIR = SKILL_DIR / "templates"
DEFAULT_OUTPUT_BASE = Path(__file__).parent.parent.parent.parent / "work" / "content-pipeline" / "output" / "runs"


# ── Markdown → 微信 HTML ────────────────────────────────────

SECTION_HEADING_RE = re.compile(r"^[一二三四五六七八九十]+、.+")
ORPHAN_BULLET_RE = re.compile(r"^[●•·]$")
ORPHAN_NUMBER_RE = re.compile(r"^(\d+)[\.、]$")
FORMAT_PATCH_STYLES = {"strong"}
TERM_PATCH_STYLES = {"risk", "accent"}


def _normalize_text_spacing(text: str) -> str:
    text = str(text or "").replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r"\s+([，。！？：；、）》】）])", r"\1", text)
    text = re.sub(r"([（《【“‘])\s+", r"\1", text)
    text = re.sub(r"([，。！？：；、])\s+(?=[\u4e00-\u9fff])", r"\1", text)
    return text.strip()


def _looks_like_section_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if SECTION_HEADING_RE.match(stripped):
        return True
    if re.match(r"^(结语|结尾|总结)[:：].+", stripped):
        return True
    return False


def _preprocess_markdown(md_text: str) -> str:
    lines = str(md_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized: list[str] = []
    in_code_block = False
    i = 0

    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            normalized.append(stripped)
            i += 1
            continue

        if in_code_block:
            normalized.append(raw)
            i += 1
            continue

        if not stripped:
            if normalized and normalized[-1] != "":
                normalized.append("")
            i += 1
            continue

        next_idx = i + 1
        while next_idx < len(lines) and not lines[next_idx].strip():
            next_idx += 1
        next_line = lines[next_idx].strip() if next_idx < len(lines) else ""

        if ORPHAN_BULLET_RE.match(stripped) and next_line and not re.match(r"^[-*]\s+|^\d+[\.、]\s+|^#+\s|^>\s", next_line):
            normalized.append(f"- {_normalize_text_spacing(next_line)}")
            i = next_idx + 1
            continue

        m = ORPHAN_NUMBER_RE.match(stripped)
        if m and next_line and not re.match(r"^[-*]\s+|^\d+[\.、]\s+|^#+\s|^>\s", next_line):
            normalized.append(f"{m.group(1)}. {_normalize_text_spacing(next_line)}")
            i = next_idx + 1
            continue

        cleaned = _normalize_text_spacing(stripped)
        if _looks_like_section_heading(cleaned):
            normalized.append(f"## {cleaned}")
        else:
            normalized.append(cleaned)
        i += 1

    compacted: list[str] = []
    for line in normalized:
        if line == "" and compacted and compacted[-1] == "":
            continue
        compacted.append(line)

    return "\n".join(compacted).strip()


def _build_format_line_map(md_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(md_text.split("\n"), 1):
        stripped = line.strip()
        if not stripped:
            continue
        kind = "paragraph"
        if stripped.startswith("## "):
            kind = "h2"
        elif stripped.startswith("### "):
            kind = "h3"
        elif stripped.startswith("- ") or stripped.startswith("* "):
            kind = "ul"
        elif re.match(r"^\d+[\.、]\s+", stripped):
            kind = "ol"
        elif stripped.startswith("> "):
            kind = "blockquote"
        rows.append({"line_no": idx, "kind": kind, "text": stripped[:500]})
    return rows


def _validate_format_patch(md_text: str, patch_obj: dict) -> tuple[list[dict], list[dict]]:
    line_map = {row["line_no"]: row for row in _build_format_line_map(md_text)}
    line_styles = patch_obj.get("line_styles", []) or []
    term_styles = patch_obj.get("term_styles", []) or []
    if not isinstance(line_styles, list) or not isinstance(term_styles, list):
        raise ValueError("format patch fields line_styles/term_styles must be arrays")

    valid_line_styles: list[dict] = []
    for item in line_styles[:10]:
        if not isinstance(item, dict):
            continue
        line_no = int(item.get("line_no", 0) or 0)
        style = str(item.get("style", "") or "").strip()
        if style not in FORMAT_PATCH_STYLES or line_no not in line_map:
            continue
        kind = line_map[line_no]["kind"]
        if style == "strong" and kind not in {"paragraph", "blockquote"}:
            continue
        valid_line_styles.append({"line_no": line_no, "style": style})

    valid_term_styles: list[dict] = []
    for item in term_styles[:24]:
        if not isinstance(item, dict):
            continue
        line_no = int(item.get("line_no", 0) or 0)
        term = str(item.get("term", "") or "").strip()
        style = str(item.get("style", "") or "").strip()
        if line_no not in line_map or style not in TERM_PATCH_STYLES:
            continue
        if not term or len(term) > 40:
            continue
        if term not in line_map[line_no]["text"]:
            continue
        valid_term_styles.append({"line_no": line_no, "term": term, "style": style})

    return valid_line_styles, valid_term_styles


def _apply_term_markers(text: str, terms: list[dict]) -> str:
    out = text
    for item in sorted(terms, key=lambda x: len(x["term"]), reverse=True):
        term = item["term"]
        marker = "RISK" if item["style"] == "risk" else "ACCENT"
        if f"[[{marker}:{term}]]" in out:
            continue
        out = out.replace(term, f"[[{marker}:{term}]]", 1)
    return out


def _apply_format_patch(md_text: str, line_styles: list[dict], term_styles: list[dict]) -> str:
    by_line_terms: dict[int, list[dict]] = {}
    by_line_style: dict[int, str] = {}
    for item in line_styles:
        by_line_style[item["line_no"]] = item["style"]
    for item in term_styles:
        by_line_terms.setdefault(item["line_no"], []).append(item)

    output_lines: list[str] = []
    for idx, raw in enumerate(md_text.split("\n"), 1):
        line = raw
        stripped = line.strip()
        if not stripped:
            output_lines.append(line)
            continue
        if idx in by_line_terms:
            line = _apply_term_markers(line, by_line_terms[idx])
            stripped = line.strip()
        style = by_line_style.get(idx)
        if style == "blockquote" and not stripped.startswith("> "):
            line = "> " + stripped
        elif style == "strong":
            base = stripped
            if base.startswith("> "):
                payload = base[2:].strip()
                if not payload.startswith("**"):
                    line = "> **" + payload + "**"
            elif not base.startswith("**"):
                line = "**" + base + "**"
        output_lines.append(line)
    return "\n".join(output_lines)


def _suggest_format_patch(run_id: str, md_text: str, platform: str, template_name: str, state: dict | None = None) -> tuple[str, dict]:
    line_map = _build_format_line_map(md_text)
    if not line_map:
        return md_text, {"applied": False, "reason": "empty"}

    cfg = load_pipeline_config()
    model = resolve_role_model("formatter", config=cfg) or resolve_role_model("director_refine", config=cfg) or resolve_role_model("writer", config=cfg)
    if not model:
        return md_text, {"applied": False, "reason": "no-model"}

    topic_title = ((state or {}).get("topic") or {}).get("title", "")
    article_title = ((state or {}).get("article") or {}).get("title", "")
    system_prompt = """你是微信公众号排版编辑，只负责提出结构化的轻量强调建议，不改正文事实。\n
目标：\n1. 找出适合轻量强调的术语或风险词；\n2. 如有必要，可把极少数短句做 strong 强调；\n3. 只输出 JSON，不要输出正文。\n
约束：\n- 只能修改给定行号中的格式，不能改写正文内容。\n- 不负责决定哪些句子做引用框；引用框由 Python 规则处理。\n- 术语强调要克制，整篇不要超过 24 处。\n- strong 强调最多 3 处。\n- 返回 schema: {reply, line_styles:[{line_no,style}], term_styles:[{line_no,term,style}]}\n"""
    user_input = json.dumps({
        "run_id": run_id,
        "platform": platform,
        "template": template_name,
        "topic_title": topic_title,
        "article_title": article_title,
        "lines": line_map,
    }, ensure_ascii=False, indent=2)
    raw = call_llm(
        system_prompt,
        user_input,
        model=model,
        response_format="json",
        chat_history=[],
        system_prompt=system_prompt,
        gateway_agent_id="contentpipe-blank",
        gateway_session_key=f"contentpipe:{run_id}:formatter-refine:main",
    )
    parsed = json.loads(raw)
    valid_line_styles, valid_term_styles = _validate_format_patch(md_text, parsed)
    patched = _apply_format_patch(md_text, valid_line_styles, valid_term_styles)
    meta = {
        "applied": bool(valid_line_styles or valid_term_styles),
        "model": model,
        "reply": parsed.get("reply", ""),
        "line_styles": valid_line_styles,
        "term_styles": valid_term_styles,
    }
    return patched, meta


def markdown_to_wechat_html(md_text: str, platform: str = "wechat", template_name: str = "") -> str:
    """Markdown → 微信兼容内联样式 HTML（微信禁止 class/外部 CSS）"""
    md_text = _preprocess_markdown(md_text)
    lines = md_text.strip().split("\n")
    html_parts: list[str] = []
    in_list = False
    list_type = ""
    list_counter = 0
    in_blockquote = False
    in_code_block = False
    code_block_lines: list[str] = []
    code_block_lang = ""

    styles = _get_platform_styles(platform, template_name)

    for line in lines:
        stripped = line.strip()

        # 代码块处理（```...```）
        if stripped.startswith("```"):
            if not in_code_block:
                # 进入代码块
                in_code_block = True
                code_block_lang = stripped[3:].strip()
                code_block_lines = []
                # 关闭之前未关闭的列表/引用
                if in_list:
                    in_list, list_type = False, ""
                if in_blockquote:
                    html_parts.append("</section>")
                    in_blockquote = False
            else:
                # 退出代码块 → 生成 HTML
                # 微信编辑器会吞掉 <pre> 内的 \n，必须用 <br> 强制换行
                escaped_lines = []
                for cl in code_block_lines:
                    # 转义 HTML 特殊字符，空格转 &nbsp; 保留缩进
                    esc = html_mod.escape(cl)
                    esc = esc.replace("  ", " &nbsp;")  # 每两个空格保留一个 nbsp
                    escaped_lines.append(esc)
                escaped = "<br>".join(escaped_lines)
                html_parts.append(
                    '<section style="background:#f6f8fa;border-radius:8px;padding:14px 16px;'
                    'margin:12px 0;overflow-x:auto;border:1px solid #e1e4e8;">'
                    f'<p style="margin:0;font-family:Menlo,Consolas,\'Courier New\',monospace;'
                    f'font-size:13px;line-height:1.6;color:#24292e;">{escaped}</p></section>'
                )
                in_code_block = False
                code_block_lines = []
                code_block_lang = ""
            continue

        if in_code_block:
            code_block_lines.append(line)  # 保留原始缩进
            continue

        # 空行：关闭当前块
        if not stripped:
            if in_list:
                in_list = False
                list_type = ""
            if in_blockquote:
                html_parts.append("</section>")
                in_blockquote = False
            continue

        # 标题
        if stripped.startswith("## "):
            html_parts.append(f'<h2 {styles["h2"]}>{_inline_format(stripped[3:].strip(), styles)}</h2>')
            continue
        if stripped.startswith("### "):
            html_parts.append(f'<h3 {styles["h3"]}>{_inline_format(stripped[4:].strip(), styles)}</h3>')
            continue
        if stripped.startswith("# "):
            continue  # h1 由模板处理

        # 引用
        if stripped.startswith("> "):
            if not in_blockquote:
                html_parts.append(f'<section {styles["blockquote"]}>')
                in_blockquote = True
            html_parts.append(f'<p style="margin:4px 0;color:inherit;">{_inline_format(stripped[2:].strip(), styles)}</p>')
            continue
        elif in_blockquote:
            html_parts.append("</section>")
            in_blockquote = False

        # 无序列表 — 用 <p> 模拟（微信编辑器 <ul>/<li> 内 <strong> 会断行）
        if stripped.startswith("- ") or stripped.startswith("* "):
            text = stripped[2:].strip()
            if not in_list:
                in_list, list_type, list_counter = True, "ul", 0
            elif list_type != "ul":
                in_list, list_type, list_counter = True, "ul", 0
            html_parts.append(
                f'<p style="font-size:16px;color:#333;margin:4px 0;line-height:1.8;'
                f'padding-left:1.5em;text-indent:-1.2em;">'
                f'<span style="color:#999;margin-right:4px;">•</span>'
                f'{_inline_format(text, styles)}</p>'
            )
            continue
        elif in_list and not re.match(r"^\d+\.\s+", stripped):
            in_list, list_type = False, ""

        # 有序列表 — 用 <p> 模拟
        m = re.match(r"^(\d+)\.\s+(.+)", stripped)
        if m:
            text = m.group(2)
            if not in_list:
                in_list, list_type, list_counter = True, "ol", 0
            elif list_type != "ol":
                in_list, list_type, list_counter = True, "ol", 0
            list_counter += 1
            html_parts.append(
                f'<p style="font-size:16px;color:#333;margin:4px 0;line-height:1.8;'
                f'padding-left:1.5em;text-indent:-1.2em;">'
                f'<span style="color:#999;margin-right:4px;">{list_counter}.</span>'
                f'{_inline_format(text, styles)}</p>'
            )
            continue

        # 分隔线
        if stripped in ("---", "***", "___"):
            html_parts.append('<section style="height:1px;background:#eee;margin:24px 0;"></section>')
            continue

        # 普通段落
        html_parts.append(f'<p {styles["p"]}>{_inline_format(stripped, styles)}</p>')

    # 容错：未关闭的代码块
    if in_code_block and code_block_lines:
        escaped_lines = []
        for cl in code_block_lines:
            esc = html_mod.escape(cl)
            esc = esc.replace("  ", " &nbsp;")
            escaped_lines.append(esc)
        escaped = "<br>".join(escaped_lines)
        html_parts.append(
            '<section style="background:#f6f8fa;border-radius:8px;padding:14px 16px;'
            'margin:12px 0;overflow-x:auto;border:1px solid #e1e4e8;">'
            f'<p style="margin:0;font-family:Menlo,Consolas,\'Courier New\',monospace;'
            f'font-size:13px;line-height:1.6;color:#24292e;">{escaped}</p></section>'
        )
    if in_blockquote:
        html_parts.append("</section>")

    return "\n".join(html_parts)


def _get_platform_styles(platform: str, template_name: str = "") -> dict[str, str]:
    """根据平台和模板返回内联样式。微信公众号模板统一走 dark-mode safe 白底正文方案。"""
    if platform == "wechat":
        domain_styles = {
            "wechat-tech-": {"accent": "#1e90ff", "soft": "#eef6ff"},
            "wechat-government-": {"accent": "#c0392b", "soft": "#fdf2f2"},
            "wechat-office-": {"accent": "#4b5563", "soft": "#f7f7f8"},
            "wechat-marketing-": {"accent": "#e67e22", "soft": "#fff6ed"},
            "wechat-finance-": {"accent": "#2c3e50", "soft": "#f7f9fb"},
            "wechat-academic-": {"accent": "#5b5fc7", "soft": "#f4f3ff"},
            "wechat-game-": {"accent": "#7c3aed", "soft": "#f7f2ff"},
            "wechat-education-": {"accent": "#6c5ce7", "soft": "#f4f0ff"},
            "wechat-medical-": {"accent": "#0f766e", "soft": "#eefcf8"},
            "wechat-legal-": {"accent": "#1f3a5f", "soft": "#f4f7fb"},
            "wechat-travel-": {"accent": "#0ea5a4", "soft": "#eefdfd"},
            # 兼容旧模板
            "tech-digital": {"accent": "#1e90ff", "soft": "#eef6ff"},
            "business-finance": {"accent": "#2c3e50", "soft": "#f7f9fb"},
            "news-insight": {"accent": "#e94560", "soft": "#fafafa"},
            "lifestyle": {"accent": "#ff6b6b", "soft": "#fff8f5"},
            "education": {"accent": "#6c5ce7", "soft": "#f4f0ff"},
        }
        accent = "#07c160"
        soft = "#f7f7f7"
        for prefix, style in domain_styles.items():
            if template_name.startswith(prefix) or template_name == f"{prefix}.html":
                accent = style["accent"]
                soft = style["soft"]
                break
        return {
            "h2": f'style="font-size:18px;font-weight:700;color:{accent};margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid #ececec;"',
            "h3": f'style="font-size:16px;font-weight:700;color:#222;margin:20px 0 8px;"',
            "p":  'style="font-size:16px;color:#333;margin:12px 0;line-height:1.8;"',
            "blockquote": f'style="border-left:3px solid {accent};padding:8px 14px;color:#666;background:{soft};border:1px solid #ececec;margin:16px 0;border-radius:0 6px 6px 0;"',
            "li": 'style="font-size:16px;color:#333;margin:4px 0;line-height:1.8;"',
            "strong": 'style="color:#1f2937;"',
            "accent_color": accent,
            "risk_color": "#d9485f",
        }
    else:  # xhs
        return {
            "h2": 'style="font-size:17px;font-weight:700;color:#222;margin:16px 0 8px;"',
            "h3": 'style="font-size:15px;font-weight:700;color:#333;margin:12px 0 6px;"',
            "p":  'style="font-size:15px;color:#333;margin:8px 0;line-height:1.7;"',
            "blockquote": 'style="border-left:3px solid #ff2442;padding:6px 12px;color:#666;background:#fff5f5;margin:12px 0;"',
            "li": 'style="font-size:15px;color:#333;margin:3px 0;"',
            "strong": 'style="color:#ff2442;"',
            "accent_color": "#ff2442",
            "risk_color": "#d9485f",
        }


def _inline_format(text: str, styles: dict) -> str:
    """行内格式：加粗、斜体、行内代码、链接 + LLM 建议的强调 marker"""
    text = _normalize_text_spacing(text)

    text = re.sub(
        r"\[\[RISK:(.+?)\]\]",
        lambda m: f'<span style="color:{styles.get("risk_color", "#d9485f")};font-weight:700;">{m.group(1)}</span>',
        text,
    )
    text = re.sub(
        r"\[\[ACCENT:(.+?)\]\]",
        lambda m: f'<span style="color:{styles.get("accent_color", "#1e90ff")};font-weight:700;">{m.group(1)}</span>',
        text,
    )

    text = re.sub(r"\*\*(.+?)\*\*", rf'<strong {styles["strong"]}>\1</strong>', text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"`(.+?)`", r'<code style="background:#f0f0f0;padding:2px 6px;border-radius:3px;font-size:14px;color:#d63384;">\1</code>', text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2" style="color:#576b95;text-decoration:none;">\1</a>', text)
    # 微信公众号兼容：单字母 <strong>X</strong> 后紧跟字母会被编辑器断行
    # 修复：将 <strong>X</strong>word 合并为 <strong>Xword</strong>
    text = re.sub(
        r'<strong([^>]*)>(\w{1,2})</strong>(\w+)',
        r'<strong\1>\2\3</strong>',
        text,
    )
    return text


# ── 图片插入 ─────────────────────────────────────────────────

def insert_images(content_html: str, placements: list, image_map: dict, platform: str, run_id: str,
                   template_name: str = "") -> str:
    """在 HTML 中按 Director 的 after_section 精确插入图片。

    优先使用 after_section（匹配 h2 标题）定位，
    fallback 到 after_paragraph（全局段落序号）。

    注意：
    - section 匹配会做标题规范化（去引号/标点/空白），仅用于定位，不影响最终显示
    - after_paragraph 表示“该 section 第 N 段后”，不会把 h2 标题本身算作第 1 段
    """
    import re as _re

    def _normalize_heading(text: str) -> str:
        text = _re.sub(r'<[^>]+>', '', str(text or '')).strip()
        text = text.lstrip('#').strip()
        text = text.lower()
        # 去掉标点、引号、空白；仅用于内部匹配，不改正文显示
        return _re.sub(r'[\W_]+', '', text, flags=_re.UNICODE)

    raw_lines = content_html.split("\n")
    blocks: list[str] = []
    current: list[str] = []
    close_tags = ("</p>", "</h2>", "</h3>", "</section>", "</blockquote>")

    for line in raw_lines:
        current.append(line)
        if any(line.rstrip().endswith(tag) for tag in close_tags):
            blocks.append("\n".join(current))
            current = []
    if current:
        remaining = "\n".join(current).strip()
        if remaining:
            blocks.append(remaining)

    # 深色模板用浅色图注
    DARK_TEMPLATES = {"tech-digital.html"}
    is_dark = template_name in DARK_TEMPLATES
    caption_color = "#8b949e" if is_dark else "#999"

    img_style = (
        'style="width:100%;border-radius:8px;margin:16px 0;display:block;"'
        if platform == "wechat" else
        'style="width:100%;border-radius:6px;margin:12px 0;display:block;"'
    )

    # ── 建立 section 索引：规范化标题 → section 元数据 ──
    sections: list[dict] = []
    current_section: dict | None = None
    global_paragraph_positions: list[int] = []

    for i, block in enumerate(blocks):
        stripped = block.strip()
        h2_match = _re.search(r'<h2[^>]*>(.*?)</h2>', block, _re.DOTALL)
        if h2_match:
            title_text = _re.sub(r'<[^>]+>', '', h2_match.group(1)).strip()
            current_section = {
                "title": title_text,
                "norm": _normalize_heading(title_text),
                "heading_idx": i,
                "paragraph_positions": [],
                "last_content_idx": i,
            }
            sections.append(current_section)
            continue

        is_paragraph = bool(_re.search(r'^\s*<p\b', stripped))
        is_separator = bool(_re.search(r'^\s*<section\b', stripped))

        if is_paragraph:
            global_paragraph_positions.append(i)

        if current_section is not None:
            if is_paragraph:
                current_section["paragraph_positions"].append(i)
                current_section["last_content_idx"] = i
            elif not is_separator and stripped:
                current_section["last_content_idx"] = i

    # ── 为每个 placement 计算精确插入位置 ──
    valid_placements = [
        (p, image_map.get(p["id"])) for p in placements if p.get("id") in image_map
    ]

    for placement, _ in valid_placements:
        after_section = placement.get("after_section", "")
        section_key = after_section.lstrip("#").strip()
        section_norm = _normalize_heading(section_key)
        matched_section = None

        if section_norm:
            for sec in sections:
                if sec["norm"] == section_norm:
                    matched_section = sec
                    break
            if matched_section is None:
                for sec in sections:
                    if section_norm and (section_norm in sec["norm"] or sec["norm"] in section_norm):
                        matched_section = sec
                        break

        inner_offset = int(placement.get("after_paragraph", 0) or 0)
        if matched_section is not None:
            para_positions = matched_section.get("paragraph_positions", [])
            if para_positions:
                if inner_offset <= 0:
                    placement["_computed_pos"] = para_positions[0]
                elif inner_offset <= len(para_positions):
                    placement["_computed_pos"] = para_positions[inner_offset - 1] + 1
                else:
                    placement["_computed_pos"] = matched_section.get("last_content_idx", para_positions[-1]) + 1
            else:
                placement["_computed_pos"] = matched_section.get("heading_idx", 0) + 1
        else:
            # Fallback: 用全局 after_paragraph（按全文段落序号，而不是 block 序号）
            if global_paragraph_positions:
                if inner_offset <= 0:
                    placement["_computed_pos"] = global_paragraph_positions[0]
                elif inner_offset <= len(global_paragraph_positions):
                    placement["_computed_pos"] = global_paragraph_positions[inner_offset - 1] + 1
                else:
                    placement["_computed_pos"] = global_paragraph_positions[-1] + 1
            else:
                placement["_computed_pos"] = min(inner_offset, len(blocks))

    # 防碰撞：如果多张图位置相同，自动间隔 1
    positions_used = set()
    for placement, _ in valid_placements:
        pos = placement["_computed_pos"]
        while pos in positions_used:
            pos += 1
        placement["_computed_pos"] = pos
        positions_used.add(pos)

    # 按位置倒序插入
    sorted_placements = sorted(valid_placements, key=lambda x: x[0].get("_computed_pos", 0), reverse=True)

    for placement, file_path in sorted_placements:
        para_idx = placement.get("_computed_pos", 0)
        pid = placement["id"]
        size = placement.get("size_hint", "full_width")
        desc = placement.get("description", "")[:60]
        purpose = placement.get("purpose", "")

        if size == "half":
            style = 'style="width:50%;margin:12px auto;display:block;border-radius:8px;"'
        elif size == "thumbnail":
            style = 'style="width:30%;margin:8px auto;display:block;border-radius:6px;"'
        else:
            style = img_style

        if file_path and os.path.exists(file_path):
            filename = os.path.basename(file_path)
            img_src = f"/api/runs/{run_id}/images/{filename}"
        else:
            img_src = f"/api/runs/{run_id}/images/{pid}.png"

        # 图注：只渲染给读者看的 caption，不渲染内部 purpose
        caption_html = ""
        caption = str(placement.get("caption", "")).strip()
        if caption:
            caption_html = f'\n<p style="text-align:center;font-size:12px;color:{caption_color};margin:4px 0 16px;">{caption}</p>'

        img_tag = f'\n<img src="{img_src}" alt="{desc}" {style}>{caption_html}\n'
        blocks.insert(min(para_idx, len(blocks)), img_tag)

    return "\n".join(blocks)


# ── 模板匹配 ─────────────────────────────────────────────────

def match_template(platform: str, keywords: list[str], director_style: str = "") -> str:
    """根据 Director style + 关键词匹配排版模板。

    优先级:
    1. YAML 中配置的 style exact / aliases / prefixes
    2. keywords 子串匹配
    3. fallback 到 default
    """
    mapping_path = CONFIG_DIR / "template-mapping.yaml"
    if not mapping_path.exists():
        return "base.html"

    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    platform_config = mapping.get(platform, {})
    rules = platform_config.get("mapping", [])
    default = platform_config.get("default", "base.html")
    styles_cfg = platform_config.get("styles", {}) if isinstance(platform_config, dict) else {}
    exact_map = styles_cfg.get("exact", {}) if isinstance(styles_cfg, dict) else {}
    alias_map = styles_cfg.get("aliases", {}) if isinstance(styles_cfg, dict) else {}
    prefix_map = styles_cfg.get("prefixes", {}) if isinstance(styles_cfg, dict) else {}

    # ── 1. Director style 直接映射（来自 YAML 配置）──
    if director_style:
        style_lower = director_style.lower().strip()
        tpl = None
        if style_lower in exact_map:
            tpl = exact_map[style_lower]
        elif style_lower in alias_map:
            tpl = alias_map[style_lower]
        else:
            for prefix, candidate_tpl in prefix_map.items():
                if style_lower.startswith(str(prefix).lower().strip()):
                    tpl = candidate_tpl
                    break
        if tpl:
            tpl_path = TEMPLATES_DIR / platform / tpl
            if tpl_path.exists():
                return tpl

    # ── 2. keywords 子串匹配（"AI Agent" 匹配 "AI"）──
    for rule in rules:
        rule_keywords = [k.lower() for k in rule.get("keywords", [])]
        for kw in keywords:
            kw_lower = kw.lower()
            # 精确匹配
            if kw_lower in rule_keywords:
                return rule["template"]
            # 子串匹配：keyword 包含 rule_keyword
            for rk in rule_keywords:
                if rk in kw_lower:
                    return rule["template"]
    return default


# ── 主函数 ────────────────────────────────────────────────────

def format_article(run_id: str, output_dir: Path, platform: str = "wechat") -> str:
    """完整排版流程：读取产物 → 转换 → 模板渲染 → 输出 HTML"""

    # 读取产物
    article_edited = (output_dir / "article_edited.md").read_text(encoding="utf-8") if (output_dir / "article_edited.md").exists() else ""
    if not article_edited and (output_dir / "article_draft.md").exists():
        article_edited = (output_dir / "article_draft.md").read_text(encoding="utf-8")

    state_path = output_dir / "state.yaml"
    state = yaml.safe_load(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    topic = state.get("topic", {})
    article = state.get("article", {})
    visual_plan = state.get("visual_plan", {})
    selected = state.get("selected_images", {})
    generated = state.get("generated_images", [])
    generated_cover = state.get("generated_cover", {}) or {}

    cover_url = ""
    cover_file = generated_cover.get("file_path", "") if isinstance(generated_cover, dict) else ""
    if cover_file:
        cover_name = Path(cover_file).name
        cover_url = f"/api/runs/{run_id}/images/{cover_name}"

    # Step 1: 匹配模板（先确定模板，因为内联样式依赖模板类型）
    director_style = visual_plan.get("style", "")
    template_name = match_template(platform, topic.get("keywords", []), director_style=director_style)

    # Step 2: 规则清洗 + LLM 格式建议 patch（结构化）→ HTML
    prepared_md = _preprocess_markdown(article_edited)
    format_patch_meta = {"applied": False, "reason": "skipped"}
    try:
        patched_md, format_patch_meta = _suggest_format_patch(run_id, prepared_md, platform, template_name, state=state)
        prepared_md = patched_md
    except Exception as e:
        format_patch_meta = {"applied": False, "reason": f"llm-patch-failed: {e}"}
        logger.warning("formatter style patch failed for %s: %s", run_id, e)
    content_html = markdown_to_wechat_html(prepared_md, platform, template_name=template_name)

    # Step 3: 插入图片
    placements = visual_plan.get("placements", [])
    image_map = {}

    # 先尝试 selected_images 匹配（多候选模式）
    for pid, option in selected.items():
        for img in generated:
            if img.get("placement_id") == pid and img.get("option") == option and img.get("success", True):
                image_map[pid] = img.get("file_path", "")
                break

    # Fallback: 如果 option 匹配失败（单候选模式，option=None），直接按 placement_id 匹配
    if not image_map:
        for img in generated:
            if img.get("success", True) and img.get("placement_id") and img.get("file_path"):
                image_map[img["placement_id"]] = img["file_path"]

    if placements and image_map:
        content_html = insert_images(content_html, placements, image_map, platform, run_id, template_name=template_name)
    template_path = TEMPLATES_DIR / platform / template_name
    if not template_path.exists():
        template_path = TEMPLATES_DIR / platform / "base.html"
    if not template_path.exists():
        # 无模板，返回纯内容
        html = content_html
    else:
        tpl = jinja2.Template(template_path.read_text(encoding="utf-8"))
        html = tpl.render(
            title=article.get("title", topic.get("title", "")),
            subtitle=article.get("subtitle", ""),
            author="ContentPipe",
            date=datetime.now().strftime("%Y-%m-%d"),
            lead=article.get("subtitle", ""),
            content=content_html,
            category=", ".join(topic.get("keywords", [])[:2]),
            cover_url=cover_url,
        )

    # 保存
    (output_dir / "formatted.html").write_text(html, encoding="utf-8")
    (output_dir / "content_body.html").write_text(content_html, encoding="utf-8")
    (output_dir / "formatter_input_prepared.md").write_text(prepared_md, encoding="utf-8")
    (output_dir / "formatter_style_patch_last.json").write_text(
        json.dumps(format_patch_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("Formatted: %s chars, %s images, template=%s, style_patch=%s", len(html), len(image_map), template_name, format_patch_meta.get("applied"))
    return html


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ContentPipe Formatter")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--platform", default="wechat")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = DEFAULT_OUTPUT_BASE / args.run_id

    if not output_dir.exists():
        logger.error("Output dir not found: %s", output_dir)
        sys.exit(1)

    format_article(args.run_id, output_dir, args.platform)
