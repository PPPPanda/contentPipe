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
    in_blockquote = False

    styles = _get_platform_styles(platform, template_name)

    for line in lines:
        stripped = line.strip()

        # 空行：关闭当前块
        if not stripped:
            if in_list:
                html_parts.append(f"</{list_type}>")
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

        # 无序列表
        if stripped.startswith("- ") or stripped.startswith("* "):
            text = stripped[2:].strip()
            if not in_list:
                html_parts.append('<ul style="padding-left:20px;margin:12px 0;">')
                in_list, list_type = True, "ul"
            elif list_type != "ul":
                html_parts.append(f"</{list_type}>")
                html_parts.append('<ul style="padding-left:20px;margin:12px 0;">')
                list_type = "ul"
            html_parts.append(f'<li {styles["li"]}>{_inline_format(text, styles)}</li>')
            continue
        elif in_list and not re.match(r"^\d+\.\s+", stripped):
            html_parts.append(f"</{list_type}>")
            in_list, list_type = False, ""

        # 有序列表
        m = re.match(r"^(\d+)\.\s+(.+)", stripped)
        if m:
            text = m.group(2)
            if not in_list:
                html_parts.append('<ol style="padding-left:20px;margin:12px 0;">')
                in_list, list_type = True, "ol"
            elif list_type != "ol":
                html_parts.append(f"</{list_type}>")
                html_parts.append('<ol style="padding-left:20px;margin:12px 0;">')
                list_type = "ol"
            html_parts.append(f'<li {styles["li"]}>{_inline_format(text, styles)}</li>')
            continue

        # 分隔线
        if stripped in ("---", "***", "___"):
            html_parts.append('<section style="height:1px;background:#eee;margin:24px 0;"></section>')
            continue

        # 普通段落
        html_parts.append(f'<p {styles["p"]}>{_inline_format(stripped, styles)}</p>')

    if in_list:
        html_parts.append(f"</{list_type}>")
    if in_blockquote:
        html_parts.append("</section>")

    return "\n".join(html_parts)


def _get_platform_styles(platform: str, template_name: str = "") -> dict[str, str]:
    """根据平台和模板返回内联样式"""
    # 深色模板列表（需要浅色文字）
    DARK_TEMPLATES = {"tech-digital.html"}

    if platform == "wechat" and template_name in DARK_TEMPLATES:
        # 深色科技模板：浅色文字适配深背景
        return {
            "h2": 'style="font-size:18px;font-weight:700;color:#1e90ff;margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid #21262d;"',
            "h3": 'style="font-size:16px;font-weight:700;color:#58a6ff;margin:20px 0 8px;"',
            "p":  'style="font-size:16px;color:#c9d1d9;margin:12px 0;line-height:1.8;"',
            "blockquote": 'style="border-left:3px solid #30363d;padding:8px 14px;color:#8b949e;background:#161b22;margin:16px 0;border-radius:0 4px 4px 0;"',
            "li": 'style="font-size:16px;color:#c9d1d9;margin:4px 0;line-height:1.8;"',
            "strong": 'style="color:#ffffff;"',
        }
    elif platform == "wechat":
        return {
            "h2": 'style="font-size:18px;font-weight:700;color:#1a1a1a;margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid #eee;"',
            "h3": 'style="font-size:16px;font-weight:700;color:#333;margin:20px 0 8px;"',
            "p":  'style="font-size:16px;color:#333;margin:12px 0;line-height:1.8;"',
            "blockquote": 'style="border-left:3px solid #07c160;padding:8px 14px;color:#666;background:#f7f7f7;margin:16px 0;border-radius:0 4px 4px 0;"',
            "li": 'style="font-size:16px;color:#333;margin:4px 0;line-height:1.8;"',
            "strong": 'style="color:#1a1a1a;"',
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
