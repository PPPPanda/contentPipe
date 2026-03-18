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
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import jinja2
import yaml

from logutil import get_logger

logger = get_logger(__name__)


SKILL_DIR = Path(__file__).parent.parent
CONFIG_DIR = SKILL_DIR / "config"
TEMPLATES_DIR = SKILL_DIR / "templates"
DEFAULT_OUTPUT_BASE = Path(__file__).parent.parent.parent.parent / "work" / "content-pipeline" / "output" / "runs"


# ── Markdown → 微信 HTML ────────────────────────────────────

def markdown_to_wechat_html(md_text: str, platform: str = "wechat", template_name: str = "") -> str:
    """Markdown → 微信兼容内联样式 HTML（微信禁止 class/外部 CSS）"""
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
                import html as html_mod
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

    # in_list 不再需要关闭标签（已改用 <p> 模拟列表）
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
        }
    else:  # xhs
        return {
            "h2": 'style="font-size:17px;font-weight:700;color:#222;margin:16px 0 8px;"',
            "h3": 'style="font-size:15px;font-weight:700;color:#333;margin:12px 0 6px;"',
            "p":  'style="font-size:15px;color:#333;margin:8px 0;line-height:1.7;"',
            "blockquote": 'style="border-left:3px solid #ff2442;padding:6px 12px;color:#666;background:#fff5f5;margin:12px 0;"',
            "li": 'style="font-size:15px;color:#333;margin:3px 0;"',
            "strong": 'style="color:#ff2442;"',
        }


def _inline_format(text: str, styles: dict) -> str:
    """行内格式：加粗、斜体、行内代码、链接"""
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
    """在 HTML 中按 Director 的 after_section 精确插入图片

    优先使用 after_section（匹配 h2 标题）定位，
    fallback 到 after_paragraph（全局段落序号）。
    """
    import re as _re

    raw_lines = content_html.split("\n")
    blocks: list[str] = []
    current: list[str] = []
    close_tags = ("</p>", "</h2>", "</h3>", "</ul>", "</ol>", "</section>", "</blockquote>")

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

    # ── 建立 section 索引：h2 标题 → block 序号（section 最后一个段落的位置） ──
    section_map: dict[str, int] = {}  # normalized_title → section 最后一个 block 的 index
    current_section_title = ""
    for i, block in enumerate(blocks):
        # 检测 h2 标题
        h2_match = _re.search(r'<h2[^>]*>(.*?)</h2>', block, _re.DOTALL)
        if h2_match:
            title_text = _re.sub(r'<[^>]+>', '', h2_match.group(1)).strip()
            current_section_title = title_text
        # 每个 block 都更新当前 section 的"最后位置"
        if current_section_title:
            section_map[current_section_title] = i

    # ── 为每个 placement 计算精确插入位置 ──
    valid_placements = [
        (p, image_map.get(p["id"])) for p in placements if p.get("id") in image_map
    ]

    for placement, _ in valid_placements:
        after_section = placement.get("after_section", "")
        # 去掉 Markdown ## 前缀
        section_key = after_section.lstrip("#").strip()

        # 尝试精确匹配 section 标题
        matched_pos = None
        if section_key:
            # 精确匹配
            if section_key in section_map:
                matched_pos = section_map[section_key]
            else:
                # 模糊匹配：section_key 是标题的子串
                for title, pos in section_map.items():
                    if section_key in title or title in section_key:
                        matched_pos = pos
                        break

        if matched_pos is not None:
            # 在 section 内偏移 after_paragraph 个段落
            inner_offset = placement.get("after_paragraph", 0)
            # 找这个 section 的起始位置
            section_start = 0
            for title, pos in section_map.items():
                if title == (section_key if section_key in section_map else ""):
                    break
                section_start = pos + 1
            # 在 section 起始位置 + 偏移量处插入
            placement["_computed_pos"] = min(section_start + inner_offset, matched_pos)
        else:
            # Fallback: 用全局 after_paragraph
            placement["_computed_pos"] = placement.get("after_paragraph", 0)

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

    # Step 1: 匹配模板（先确定模板，因为内联样式依赖模板类型）
    director_style = visual_plan.get("style", "")
    template_name = match_template(platform, topic.get("keywords", []), director_style=director_style)

    # Step 2: Markdown → HTML（传入 template_name 让内联样式适配模板）
    content_html = markdown_to_wechat_html(article_edited, platform, template_name=template_name)

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
        )

    # 保存
    (output_dir / "formatted.html").write_text(html, encoding="utf-8")
    (output_dir / "content_body.html").write_text(content_html, encoding="utf-8")

    logger.info("Formatted: %s chars, %s images, template=%s", len(html), len(image_map), template_name)
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
