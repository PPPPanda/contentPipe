from __future__ import annotations

import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    parsed: Any = None
    normalized_text: str = ""


Validator = Callable[[str], ValidationResult]


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r'^```(?:yaml|yml|json|markdown|md)?\s*\n(.*?)\n```\s*$', text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.match(r'^(?:yaml|yml|json)\s*\n(.+)$', text, re.DOTALL | re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return text


def _yaml_error_details(exc: Exception) -> list[str]:
    details: list[str] = []
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        details.append(f"line {mark.line + 1}, column {mark.column + 1}")
    problem = getattr(exc, "problem", None)
    if problem:
        details.append(str(problem))
    context = getattr(exc, "context", None)
    if context:
        details.append(str(context))
    if not details:
        details.append(str(exc))
    return details


def _try_repair_truncated_yaml(text: str) -> str | None:
    """尝试修复 LLM 输出被截断的 YAML。

    常见情况：字符串在引号内被截断，或列表项缺少结尾。
    策略：从末尾逐行删除直到 YAML 可以 parse。
    """
    lines = text.rstrip().split("\n")
    if len(lines) < 5:
        return None

    # 尝试从末尾删 1~20 行
    for trim in range(1, min(21, len(lines) - 3)):
        candidate = "\n".join(lines[:-trim]).rstrip()
        # 补全可能未闭合的引号
        open_quotes = candidate.count('"') % 2
        if open_quotes:
            candidate += '"'
        try:
            parsed = yaml.safe_load(candidate)
            if isinstance(parsed, dict) and len(parsed) >= 3:
                return candidate + "\n"
        except Exception:
            continue
    return None


def _json_error_details(exc: json.JSONDecodeError, text: str) -> list[str]:
    details = [f"line {exc.lineno}, column {exc.colno}: {exc.msg}"]
    lines = text.splitlines()
    if 1 <= exc.lineno <= len(lines):
        line = lines[exc.lineno - 1]
        pointer = " " * max(exc.colno - 1, 0) + "^"
        details.append(line[:240])
        details.append(pointer[:240])
        if line.count('"') % 2 == 1:
            details.append("possible unescaped double quote in string")
    return details


def _ensure_mapping(value: Any, field: str, details: list[str]) -> None:
    if not isinstance(value, dict):
        details.append(f"{field} must be a mapping/object")


def _ensure_list(value: Any, field: str, details: list[str]) -> None:
    if not isinstance(value, list):
        details.append(f"{field} must be a list")


def validate_topic_yaml(text: str) -> ValidationResult:
    raw = _strip_code_fence(text)
    try:
        parsed = yaml.safe_load(raw)
    except Exception as exc:
        repaired = _try_repair_truncated_yaml(raw)
        if repaired:
            try:
                parsed = yaml.safe_load(repaired)
                raw = repaired
            except Exception:
                return ValidationResult(ok=False, message="topic.yaml is not valid YAML (repair failed)", details=_yaml_error_details(exc))
        else:
            return ValidationResult(ok=False, message="topic.yaml is not valid YAML", details=_yaml_error_details(exc))

    details: list[str] = []
    if not isinstance(parsed, dict):
        return ValidationResult(ok=False, message="topic.yaml top-level must be a mapping", details=[f"got {type(parsed).__name__}"])

    topic = parsed.get("topic")
    writer_brief = parsed.get("writer_brief")
    handoff = parsed.get("handoff_to_researcher")

    if topic is None:
        details.append("missing required top-level key: topic")
    else:
        _ensure_mapping(topic, "topic", details)
        if isinstance(topic, dict) and not str(topic.get("title", "")).strip():
            details.append("topic.title is required and must be non-empty")

    if writer_brief is None:
        details.append("missing required top-level key: writer_brief")
    elif not isinstance(writer_brief, dict):
        details.append("writer_brief must be a mapping/object")

    if handoff is None:
        details.append("missing required top-level key: handoff_to_researcher")
    elif not isinstance(handoff, dict):
        details.append("handoff_to_researcher must be a mapping/object")

    if details:
        return ValidationResult(ok=False, message="topic.yaml failed schema checks", details=details)

    normalized = raw.strip() + "\n"
    return ValidationResult(ok=True, parsed=parsed, normalized_text=normalized)


def validate_research_yaml(text: str) -> ValidationResult:
    raw = _strip_code_fence(text)
    try:
        parsed = yaml.safe_load(raw)
    except Exception as exc:
        # 尝试自动修复截断的 YAML
        repaired = _try_repair_truncated_yaml(raw)
        if repaired:
            try:
                parsed = yaml.safe_load(repaired)
                raw = repaired  # 用修复后的版本继续校验
            except Exception:
                return ValidationResult(ok=False, message="research.yaml is not valid YAML (repair failed)", details=_yaml_error_details(exc))
        else:
            return ValidationResult(ok=False, message="research.yaml is not valid YAML", details=_yaml_error_details(exc))

    details: list[str] = []
    if not isinstance(parsed, dict):
        return ValidationResult(ok=False, message="research.yaml top-level must be a mapping", details=[f"got {type(parsed).__name__}"])

    has_new_schema = any(k in parsed for k in (
        "verification_results",
        "writer_packet",
        "topic_support_materials",
        "evidence_backed_insights",
        "open_issues",
        "source_registry",
    ))
    has_old_schema = isinstance(parsed.get("research"), dict)
    if not has_new_schema and not has_old_schema:
        details.append("expected new research packet keys or legacy 'research' mapping")

    if "verification_results" in parsed:
        _ensure_list(parsed.get("verification_results"), "verification_results", details)
    if "writer_packet" in parsed and not isinstance(parsed.get("writer_packet"), dict):
        details.append("writer_packet must be a mapping/object")
    if "topic_support_materials" in parsed and not isinstance(parsed.get("topic_support_materials"), dict):
        details.append("topic_support_materials must be a mapping/object")
    if "evidence_backed_insights" in parsed:
        _ensure_list(parsed.get("evidence_backed_insights"), "evidence_backed_insights", details)
    if "open_issues" in parsed:
        _ensure_list(parsed.get("open_issues"), "open_issues", details)
    if "source_registry" in parsed:
        _ensure_list(parsed.get("source_registry"), "source_registry", details)

    if details:
        return ValidationResult(ok=False, message="research.yaml failed schema checks", details=details)

    normalized = raw.strip() + "\n"
    return ValidationResult(ok=True, parsed=parsed, normalized_text=normalized)


def _allowed_visual_styles() -> set[str]:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "template-mapping.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        exact = ((cfg.get("wechat") or {}).get("styles") or {}).get("exact") or {}
        return {str(k).strip() for k in exact.keys() if str(k).strip()}
    except Exception:
        return {"tech-digital", "business-finance", "news-insight", "lifestyle", "education"}


ALLOWED_VISUAL_STYLES = _allowed_visual_styles()


def validate_visual_plan_json(text: str) -> ValidationResult:
    raw = _strip_code_fence(text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ValidationResult(ok=False, message="visual_plan.json is not valid JSON", details=_json_error_details(exc, raw))

    details: list[str] = []
    if not isinstance(parsed, dict):
        return ValidationResult(ok=False, message="visual_plan.json top-level must be an object", details=[f"got {type(parsed).__name__}"])

    style = parsed.get("style")
    style_variant = parsed.get("style_variant")
    global_tone = parsed.get("global_tone")
    cover = parsed.get("cover")
    placements = parsed.get("placements")

    if not isinstance(style, str) or not style.strip():
        details.append("style must be a non-empty string")
    elif style.strip() not in ALLOWED_VISUAL_STYLES:
        details.append(f"style must be one of: {', '.join(sorted(ALLOWED_VISUAL_STYLES))}")
    if not isinstance(style_variant, str) or not style_variant.strip():
        details.append("style_variant must be a non-empty string")
    if not isinstance(global_tone, str) or not global_tone.strip():
        details.append("global_tone must be a non-empty string")
    if not isinstance(cover, dict):
        details.append("cover must be an object")
    else:
        for field in ("title", "description", "purpose", "aspect_ratio", "style_notes"):
            value = cover.get(field)
            if not isinstance(value, str) or not value.strip():
                details.append(f"cover.{field} must be a non-empty string")
    if not isinstance(placements, list) or not placements:
        details.append("placements must be a non-empty list")
    else:
        seen_ids: set[str] = set()
        for idx, placement in enumerate(placements, start=1):
            prefix = f"placements[{idx}]"
            if not isinstance(placement, dict):
                details.append(f"{prefix} must be an object")
                continue
            pid = str(placement.get("id", "")).strip()
            if not pid:
                details.append(f"{prefix}.id is required")
            elif pid in seen_ids:
                details.append(f"duplicate placement id: {pid}")
            else:
                seen_ids.add(pid)
            for field in ("after_section", "type", "description", "purpose", "caption"):
                value = placement.get(field)
                if not isinstance(value, str) or not value.strip():
                    details.append(f"{prefix}.{field} must be a non-empty string")
            if "after_paragraph" in placement and not isinstance(placement.get("after_paragraph"), int):
                details.append(f"{prefix}.after_paragraph must be an integer when present")

    if details:
        return ValidationResult(ok=False, message="visual_plan.json failed schema checks", details=details)

    normalized = json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"
    return ValidationResult(ok=True, parsed=parsed, normalized_text=normalized)


def validate_image_candidates_json(text: str, expected_ids: list[str] | None = None) -> ValidationResult:
    raw = _strip_code_fence(text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ValidationResult(ok=False, message="image_candidates.json is not valid JSON", details=_json_error_details(exc, raw))

    details: list[str] = []
    if not isinstance(parsed, list):
        return ValidationResult(ok=False, message="image_candidates.json top-level must be an array", details=[f"got {type(parsed).__name__}"])
    if not parsed:
        details.append("top-level array must not be empty")
    else:
        ids_seen: set[str] = set()
        for idx, item in enumerate(parsed, start=1):
            prefix = f"image_candidates[{idx}]"
            if not isinstance(item, dict):
                details.append(f"{prefix} must be an object")
                continue
            pid = str(item.get("id", "")).strip()
            if not pid:
                details.append(f"{prefix}.id is required")
            elif pid in ids_seen:
                details.append(f"duplicate image candidate id: {pid}")
            else:
                ids_seen.add(pid)

            if not isinstance(item.get("original_description"), str) or not item.get("original_description", "").strip():
                details.append(f"{prefix}.original_description must be a non-empty string")

            candidates = item.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                details.append(f"{prefix}.candidates must be a non-empty list")
            else:
                options_seen: set[str] = set()
                for cidx, candidate in enumerate(candidates, start=1):
                    cp = f"{prefix}.candidates[{cidx}]"
                    if not isinstance(candidate, dict):
                        details.append(f"{cp} must be an object")
                        continue
                    option = str(candidate.get("option", "")).strip()
                    if option not in {"A", "B", "C"}:
                        details.append(f"{cp}.option must be one of A/B/C")
                    elif option in options_seen:
                        details.append(f"duplicate option {option} in {prefix}.candidates")
                    else:
                        options_seen.add(option)
                    for field in ("concept", "prompt", "negative_prompt"):
                        value = candidate.get(field)
                        if not isinstance(value, str) or not value.strip():
                            details.append(f"{cp}.{field} must be a non-empty string")

                recommended = str(item.get("recommended", "")).strip()
                if recommended not in {"A", "B", "C"}:
                    details.append(f"{prefix}.recommended must be one of A/B/C")
                elif recommended and recommended not in {c.get('option') for c in candidates if isinstance(c, dict)}:
                    details.append(f"{prefix}.recommended must match one of its candidates")

            if "aspect_ratio" in item and (not isinstance(item.get("aspect_ratio"), str) or not item.get("aspect_ratio", "").strip()):
                details.append(f"{prefix}.aspect_ratio must be a non-empty string when present")
            if "seed_base" in item and not isinstance(item.get("seed_base"), int):
                details.append(f"{prefix}.seed_base must be an integer when present")

        if expected_ids:
            missing = [pid for pid in expected_ids if pid not in ids_seen]
            if missing:
                details.append(f"missing candidate groups for placement ids: {', '.join(missing)}")

    if details:
        return ValidationResult(ok=False, message="image_candidates.json failed schema checks", details=details)

    normalized = json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"
    return ValidationResult(ok=True, parsed=parsed, normalized_text=normalized)


def validate_writer_markdown(text: str, min_chars: int = 1200) -> ValidationResult:
    raw = _strip_code_fence(text).strip()
    details: list[str] = []
    bad_markers = [
        "我看到你提供了",
        "以下是",
        "我将",
        "我来为你",
        "根据你的要求",
        "下面是文章",
        "已为你生成",
        "这篇文章将",
    ]

    if len(raw) < min_chars:
        details.append(f"content too short: {len(raw)} chars (< {min_chars})")

    head = raw[:400]
    for marker in bad_markers:
        if marker in head:
            details.append(f"meta marker near opening: {marker}")

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if len(paragraphs) < 4:
        details.append(f"too few paragraphs: {len(paragraphs)}")

    heading_count = sum(1 for line in raw.splitlines() if line.strip().startswith("## "))
    if heading_count < 2:
        details.append(f"too few section headings: {heading_count}")

    if details:
        return ValidationResult(ok=False, message="article_draft.md failed quality checks", details=details)
    return ValidationResult(ok=True, parsed=raw, normalized_text=raw + "\n")


def validate_de_ai_markdown(text: str, original_text: str) -> ValidationResult:
    raw = _strip_code_fence(text).strip()
    details: list[str] = []
    bad_markers = [
        "自检清单",
        "改写完成",
        "结构粉碎",
        "风格拟态",
        "我来根据",
        "以下是",
        "主要改动",
        "处理如下",
        "去AI味",
        "润色后的版本",
    ]

    if len(raw) < 800:
        details.append(f"content too short: {len(raw)} chars (< 800)")

    for marker in bad_markers:
        if marker in raw:
            details.append(f"bad marker present: {marker}")

    original = (original_text or "").strip()
    if original:
        ratio = len(raw) / max(len(original), 1)
        if ratio < 0.6 or ratio > 1.4:
            details.append(f"length ratio out of range: {ratio:.2f}x")

    if details:
        return ValidationResult(ok=False, message="article_edited.md failed quality checks", details=details)
    return ValidationResult(ok=True, parsed=raw, normalized_text=raw + "\n")


def build_validation_retry_message(filename: str, output_kind: str, result: ValidationResult) -> str:
    lines = [
        f"你刚写出的 {filename} 未通过校验。",
        f"问题: {result.message}",
    ]
    if result.details:
        lines.append("详细错误:")
        lines.extend(f"- {detail}" for detail in result.details[:10])
    lines.extend([
        "",
        "请修复并重新写同一个文件：",
        f"- 文件内容必须是合法的 {output_kind}",
        "- 不要输出解释文字、前言、后记、markdown code fence",
        "- 不要用空对象、空数组、默认占位值敷衍",
        "- 保留原始任务语义，只修复格式/结构问题",
    ])
    return "\n".join(lines)
