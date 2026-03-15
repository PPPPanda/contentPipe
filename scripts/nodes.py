"""
ContentPipe Nodes — LangGraph 节点实现

每个节点遵循统一模式：
  1. 读外部 prompt 文件
  2. 从 state 提取必要上下文（不传全部历史）
  3. 调 LLM
  4. 解析输出，写入 state
  5. 持久化中间产物到磁盘
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import re
import yaml

from gateway_auth import build_contentpipe_node_session_key
from logutil import get_logger
from state import ContentState
from tools import call_llm, load_pipeline_config
from validators import (
    ValidationResult,
    build_validation_retry_message,
    validate_de_ai_markdown,
    validate_image_candidates_json,
    validate_research_yaml,
    validate_topic_yaml,
    validate_visual_plan_json,
    validate_writer_markdown,
)

logger = get_logger(__name__)


def _node_session_key(state: ContentState, node_id: str, lane: str = "main") -> str:
    session_gen = state.get("_session_gen", {}) if isinstance(state.get("_session_gen", {}), dict) else {}
    generation = int(session_gen.get(node_id, 0) or 0)
    return build_contentpipe_node_session_key(state["run_id"], node_id, lane, generation)


def _strip_code_fence(text: str) -> str:
    """去掉 LLM 返回的 ```yaml ... ``` 或 ```json ... ``` 包裹"""
    text = text.strip()
    # 匹配 ```yaml\n...\n``` 或 ```json\n...\n``` 或 ```\n...\n```
    m = re.match(r'^```(?:yaml|json|markdown|md)?\s*\n(.*?)\n```\s*$', text, re.DOTALL)
    if m:
        return m.group(1)
    # fallback: LLM 有时输出 "yaml\n..." 不带 ```
    m2 = re.match(r'^(?:yaml|json)\s*\n(.+)$', text, re.DOTALL | re.IGNORECASE)
    if m2:
        return m2.group(1)
    return text


def _sanitize_subtitle(text: str) -> str:
    text = (text or '').strip()
    if not text:
        return ''
    text = _strip_code_fence(text)
    text = text.strip().strip('"').strip("'")
    banned_prefixes = [
        '这篇文章', '本文将', '本文会', '下面带你', '下面我们', '主力推荐',
        '用', '采用', '不装不端着', '从选题到发布', '文章将', '本篇文章',
    ]
    for prefix in banned_prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip('：:，,。 ')
    text = re.sub(r'\s+', ' ', text)
    return text[:120].strip('，,。；;：: ')


def _fallback_subtitle_from_article(article_text: str, title: str = '') -> str:
    lines = []
    for raw in (article_text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            continue
        if line.startswith('>'):
            continue
        lines.append(line)
    candidate = lines[0] if lines else ''
    candidate = re.sub(r'^(这篇文章|本文将|本文会|下面带你|下面我们)[：:，,。 ]*', '', candidate)
    candidate = candidate[:120].strip('，,。；;：: ')
    if candidate:
        return candidate
    return _sanitize_subtitle(title) or '一篇关于安装、配置与上手使用的实用教程'


def generate_article_subtitle(state: ContentState, article_text: str, title: str = '') -> str:
    """由 Writer 阶段显式生成发布摘要（读者可见 digest）。"""
    prompt = _read_prompt('writer-subtitle.md')
    context = f"文章标题: {title}\n\n--- 最终正文 ---\n{article_text}"
    cfg = load_pipeline_config().get('pipeline', {})
    try:
        result = call_llm(
            prompt,
            context,
            model='dashscope/qwen3.5-flash',
            max_tokens=400,
            response_format='json',
            system_prompt=prompt,
            gateway_session_key=_node_session_key(state, 'writer', f'subtitle-{uuid.uuid4().hex[:8]}'),
            gateway_agent_id=cfg.get('gateway_agent_id') if cfg.get('llm_mode') == 'gateway' else None,
        )
        data = json.loads(_strip_code_fence(result))
        subtitle = _sanitize_subtitle(str(data.get('subtitle', '') or ''))
        if subtitle:
            return subtitle
    except Exception:
        pass
    return _fallback_subtitle_from_article(article_text, title)

# ── 配置 ──────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "output"


def _read_prompt(name: str) -> str:
    """读取外部 prompt 文件"""
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def _save_artifact(run_id: str, filename: str, content: str) -> Path:
    """持久化中间产物到 output/runs/{run_id}/"""
    run_dir = OUTPUT_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def _save_state(state: ContentState) -> None:
    """持久化完整状态快照"""
    run_id = state.get("run_id", "unknown")
    _save_artifact(run_id, "state.yaml", yaml.dump(dict(state), allow_unicode=True, default_flow_style=False))


def _get_model(role: str) -> str | None:
    """获取角色对应的 LLM 模型（从 pipeline.yaml 的 llm_overrides 读取）"""
    config = load_pipeline_config()
    overrides = config.get("pipeline", {}).get("llm_overrides", {})
    return overrides.get(role)


# ── Per-Node Session 辅助 ─────────────────────────────────────

def _get_node_history(state: ContentState, node_id: str) -> list[dict]:
    """获取当前节点的 session history"""
    from web.run_manager import get_chat_history
    return get_chat_history(state["run_id"], node_id)


def _append_node_session(state: ContentState, node_id: str, role: str, content: str,
                         tag: str = "", internal: bool = False):
    """向节点 session 追加一条消息"""
    from web.run_manager import save_chat_message
    save_chat_message(state["run_id"], node_id, role, content, tag=tag, internal=internal)


def _call_llm_with_session(
    state: ContentState,
    node_id: str,
    prompt: str,
    context: str,
    model: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """调用 LLM 并将输入/输出写入节点 session。"""
    _append_node_session(state, node_id, "user", context, tag=f"{node_id}_exec", internal=True)

    history = _get_node_history(state, node_id)
    recent = [{"role": m["role"], "content": m["content"]} for m in history[:-1]][-20:]

    cfg = load_pipeline_config().get("pipeline", {})
    gateway_session_key = _node_session_key(state, node_id, "main")
    result = call_llm(
        prompt,
        context,
        model=model,
        max_tokens=max_tokens,
        chat_history=recent,
        system_prompt=prompt,
        gateway_session_key=gateway_session_key,
        gateway_agent_id=cfg.get("gateway_agent_id") if cfg.get("llm_mode") == "gateway" else None,
    )

    _append_node_session(state, node_id, "assistant", result, tag=f"{node_id}_exec", internal=True)
    return result


def _blank_agent_output_candidates(run_id: str, node_id: str, filename: str) -> list[Path]:
    return [OUTPUT_DIR / "runs" / run_id / filename]


def _call_llm_to_file_with_session(
    state: ContentState,
    node_id: str,
    prompt: str,
    context: str,
    *,
    model: str | None,
    output_filename: str,
    output_kind: str,
    max_tokens: int = 8192,
) -> tuple[str, str]:
    """让 blank-agent 把最终产物写入文件；返回 (agent_reply, file_content)。"""
    cfg = load_pipeline_config().get("pipeline", {})
    gateway_agent_id = cfg.get("gateway_agent_id")
    candidates = _blank_agent_output_candidates(state["run_id"], node_id, output_filename)
    official_target = candidates[0]
    for path in candidates:
        path.parent.mkdir(parents=True, exist_ok=True)
    if official_target.exists():
        official_target.unlink()

    write_instruction = (
        f"{context}\n\n"
        f"=== OUTPUT CONTRACT ===\n"
        f"Write the final {output_kind} to this exact absolute path using the write tool:\n"
        f"{official_target.as_posix()}\n"
        f"Do NOT write this output anywhere else. Do NOT use relative paths like runs/...\n"
        f"The file must contain ONLY the final {output_kind}.\n"
        f"Do not include explanations, self-checks, prefaces, markdown fences, or commentary outside the file.\n"
        f"After writing the file, you may reply briefly."
    )

    _append_node_session(state, node_id, "user", write_instruction, tag=f"{node_id}_exec", internal=True)
    history = _get_node_history(state, node_id)
    recent = [{"role": m["role"], "content": m["content"]} for m in history[:-1]][-20:]
    agent_reply = call_llm(
        prompt,
        write_instruction,
        model=model,
        max_tokens=max_tokens,
        chat_history=recent,
        system_prompt=prompt,
        gateway_session_key=_node_session_key(state, node_id, "main"),
        gateway_agent_id=gateway_agent_id,
    )
    _append_node_session(state, node_id, "assistant", agent_reply, tag=f"{node_id}_exec", internal=True)

    content = ""
    for path in candidates:
        if path.exists():
            content = path.read_text()
            break
    return agent_reply, content


def _call_llm_to_validated_file_with_session(
    state: ContentState,
    node_id: str,
    prompt: str,
    context: str,
    *,
    model: str | None,
    output_filename: str,
    output_kind: str,
    validator,
    max_tokens: int = 8192,
    max_attempts: int = 3,
) -> tuple[str, str, object]:
    last_reply = ""
    last_content = ""
    last_validation: ValidationResult | None = None

    for attempt in range(1, max_attempts + 1):
        agent_reply, content = _call_llm_to_file_with_session(
            state,
            node_id,
            prompt,
            context,
            model=model,
            output_filename=output_filename,
            output_kind=output_kind,
            max_tokens=max_tokens,
        )
        payload = (content or agent_reply).strip()
        validation = validator(payload)
        if validation.ok:
            normalized = validation.normalized_text or payload
            _save_artifact(state["run_id"], output_filename, normalized)
            return agent_reply, normalized, validation.parsed

        last_reply = agent_reply
        last_content = payload
        last_validation = validation
        logger.warning(
            "%s validation failed (attempt %s/%s): %s | %s",
            output_filename,
            attempt,
            max_attempts,
            validation.message,
            "; ".join(validation.details[:3]),
        )
        _append_node_session(
            state,
            node_id,
            "user",
            build_validation_retry_message(output_filename, output_kind, validation),
            tag=f"{node_id}_validation",
            internal=True,
        )

    if last_validation is not None:
        _save_artifact(
            state["run_id"],
            f"{Path(output_filename).stem}.validation-error.txt",
            build_validation_retry_message(output_filename, output_kind, last_validation)
            + "\n\n--- invalid content snapshot ---\n"
            + last_content[:4000],
        )
        raise ValueError(
            f"{output_filename} validation failed after {max_attempts} attempts: "
            f"{last_validation.message} | {'; '.join(last_validation.details[:5])}"
        )

    raise ValueError(f"{output_filename} validation failed without validator result")


# ── Agent 节点 ────────────────────────────────────────────────

def scout_node(state: ContentState) -> ContentState:
    """选题监控：多平台扫描热点 + 社交搜索，推荐选题"""
    prompt = _read_prompt("scout.md")
    platform = state.get("platform", "wechat")
    user_topic = state.get("user_topic", "")  # 用户指定的选题/参考链接

    # Skill-driven 模式：不再由 Python 预抓取公众号正文/搜索结果，改由 blank-agent 调用内置 skills
    reference_urls = list(state.get("reference_urls", []))
    urls_in_topic = re.findall(r'https?://\S+', user_topic)
    for raw_url in urls_in_topic:
        cleaned = raw_url.rstrip(",.;，。；）)]】」』")
        if cleaned and cleaned not in reference_urls:
            reference_urls.append(cleaned)

    search_query = user_topic if user_topic else "近期热门话题 AI 科技 2026"
    social_query = user_topic if user_topic else "AI 一人公司 Agent"

    context_parts = [
        f"目标平台: {platform}",
        f"账号领域: AI、科技、互联网、产品",
        "执行模式: skill-driven。请优先使用可见的 contentpipe-* skills 处理公众号链接、普通 URL、网络搜索与社交讨论检索。",
        "不要假装已经看到外部搜索结果；需要时请自行调用可用 skills / tools，再输出最终 YAML。",
    ]
    if user_topic:
        context_parts.append(f"\n--- 用户指定选题/参考 ---\n{user_topic}")
    if reference_urls:
        context_parts.append(f"\n--- 用户提供的参考链接（请自行读取/筛选） ---\n{json.dumps(reference_urls, ensure_ascii=False, indent=2)}")
    context_parts.append(f"\n--- 建议搜索主题 ---\n主搜索: {search_query}\n社交搜索: {social_query}")
    context = "\n".join(context_parts)

    result, topic_yaml, parsed = _call_llm_to_validated_file_with_session(
        state,
        "scout",
        prompt,
        context,
        model=_get_model("scout"),
        output_filename="topic.yaml",
        output_kind="YAML briefing document",
        validator=validate_topic_yaml,
        max_tokens=8192,
    )

    # 从新 schema 提取各部分
    topic = parsed.get("topic", {})
    # 兼容旧 schema：如果有 suggestions 列表，取第一个
    if not topic and "suggestions" in parsed:
        suggestions = parsed["suggestions"]
        topic = suggestions[0] if suggestions else {}

    # 新 schema 特有字段
    writer_brief = parsed.get("writer_brief", {})
    handoff = parsed.get("handoff_to_researcher", {})
    reference_articles = parsed.get("reference_articles", [])
    user_requirements = parsed.get("user_requirements", {})
    reference_index = parsed.get("reference_index", {})
    link_usage_policy = parsed.get("link_usage_policy", {})
    scout_summary = parsed.get("scout_process_summary", {})

    # 保存到 state（Researcher 和 Writer 会读取）
    state["topic"] = topic
    state["writer_brief"] = writer_brief
    state["handoff_to_researcher"] = handoff
    state["reference_articles"] = reference_articles
    state["user_requirements"] = user_requirements
    state["reference_index"] = reference_index
    state["link_usage_policy"] = link_usage_policy
    state["scout_process_summary"] = scout_summary

    # 执行元信息
    state["_node_context"] = state.get("_node_context", {})
    state["_node_context"]["scout"] = {
        "mode": "skill-driven",
        "reference_urls": reference_urls,
        "search_query": search_query,
        "social_query": social_query,
    }

    state["current_stage"] = "scout"
    # 保存完整 Scout YAML（含所有字段）
    _save_artifact(state["run_id"], "topic.yaml", topic_yaml or yaml.dump(parsed, allow_unicode=True, default_flow_style=False))
    _save_artifact(state["run_id"], "scout_raw.txt", result)
    _save_state(state)
    return state


def researcher_node(state: ContentState) -> ContentState:
    """深度调研：按 Scout 的 handoff 任务清单执行核查和调研"""
    prompt = _read_prompt("researcher.md")
    topic = state.get("topic", {})
    title = topic.get("title", "")
    keywords = topic.get("keywords", [])
    angle = topic.get("suggested_angle", topic.get("content_angle", ""))

    # Scout 新 schema 字段
    handoff = state.get("handoff_to_researcher", {})
    writer_brief = state.get("writer_brief", {})
    reference_articles = state.get("reference_articles", [])
    link_usage_policy = state.get("link_usage_policy", {})

    # Skill-driven 模式：Researcher 由 blank-agent 自主调用内置读取/搜索 skills
    verification_targets = handoff.get("verification_targets", [])
    sources = topic.get("sources", topic.get("direction_references", []))
    research_pool = handoff.get("research_reference_pool", [])
    all_urls = [s.get("url", "") if isinstance(s, dict) else str(s) for s in sources]
    all_urls += [r.get("url", "") for r in research_pool]
    all_urls += topic.get("reference_urls", [])
    all_urls = [u for u in all_urls if u]
    social_query = f"{title} {' '.join(keywords[:2]) if keywords else ''}".strip()

    context_parts = [
        f"选题: {title}",
        f"角度: {angle}",
        f"目标平台: {state.get('platform', 'wechat')}",
        "执行模式: skill-driven。请优先使用可见的 contentpipe-* skills 执行链接阅读、网页研究、社交搜索与事实核查，不要假装已经拿到了外部检索结果。",
    ]
    # Scout handoff 任务清单（核心输入）
    if handoff:
        context_parts.append(f"\n--- Scout 任务清单 (handoff_to_researcher) ---\n{yaml.dump(handoff, allow_unicode=True, default_flow_style=False)}")
    if writer_brief:
        context_parts.append(f"\n--- Writer Brief（了解最终需要什么） ---\n{yaml.dump(writer_brief, allow_unicode=True, default_flow_style=False)}")
    if link_usage_policy:
        context_parts.append(f"\n--- 链接使用策略 ---\n{yaml.dump(link_usage_policy, allow_unicode=True, default_flow_style=False)}")
    if verification_targets:
        context_parts.append(f"\n--- 待核查断言（请逐条自行查证） ---\n{yaml.dump(verification_targets, allow_unicode=True, default_flow_style=False)}")
    if research_pool:
        context_parts.append(f"\n--- 待深查种子链接（请自行读取/验证） ---\n{yaml.dump(research_pool, allow_unicode=True, default_flow_style=False)}")
    if all_urls:
        context_parts.append(f"\n--- 可用参考链接（请按需读取） ---\n{json.dumps(all_urls, ensure_ascii=False, indent=2)}")
    if sources:
        context_parts.append(f"\n--- 初始来源元数据 ---\n{json.dumps(sources, ensure_ascii=False, indent=2)}")
    if social_query:
        context_parts.append(f"\n--- 建议社交/社区检索主题 ---\n{social_query}")

    context = "\n".join(context_parts)

    result, research_yaml, parsed = _call_llm_to_validated_file_with_session(
        state,
        "researcher",
        prompt,
        context,
        model=_get_model("researcher"),
        output_filename="research.yaml",
        output_kind="YAML research packet",
        validator=validate_research_yaml,
        max_tokens=8192,
    )

    # 从新 schema 提取各部分
    research = parsed  # 完整保存
    verification_results = parsed.get("verification_results", [])
    writer_packet = parsed.get("writer_packet", {})
    topic_support = parsed.get("topic_support_materials", {})
    insights = parsed.get("evidence_backed_insights", [])
    open_issues = parsed.get("open_issues", [])
    source_registry = parsed.get("source_registry", [])

    # 兼容旧 schema
    if "research" in parsed and isinstance(parsed["research"], dict):
        research = parsed["research"]

    # 保存到 state（Writer 主要消费 writer_packet）
    state["research"] = research
    state["writer_packet"] = writer_packet
    state["verification_results"] = verification_results
    state["topic_support_materials"] = topic_support
    state["evidence_backed_insights"] = insights
    state["open_issues"] = open_issues

    # 执行元信息
    state["_node_context"] = state.get("_node_context", {})
    state["_node_context"]["researcher"] = {
        "mode": "skill-driven",
        "reference_url_count": len(all_urls),
        "verification_target_count": len(verification_targets),
        "verification_count": len(verification_results),
        "verified_count": sum(1 for v in verification_results if v.get("status") == "verified"),
        "sources_count": len(source_registry),
    }

    state["current_stage"] = "researcher"
    _save_artifact(state["run_id"], "research.yaml", research_yaml or yaml.dump(parsed, allow_unicode=True, default_flow_style=False))
    _save_artifact(state["run_id"], "researcher_raw.txt", result)
    _save_state(state)
    return state


def _collect_style_reference_urls(state: ContentState) -> list[str]:
    urls: list[str] = []
    for url in state.get("reference_urls", []) or []:
        if url and url not in urls:
            urls.append(url)
    topic = state.get("topic", {}) or {}
    for url in topic.get("reference_urls", []) or []:
        if url and url not in urls:
            urls.append(url)
    for ra in state.get("reference_articles", []) or []:
        if isinstance(ra, dict):
            url = str(ra.get("url", "")).strip()
            if url and url not in urls:
                urls.append(url)
    return urls


def _build_writer_context(state: ContentState) -> dict:
    """组装 writer_context — Writer 的完整写作上下文包

    三层结构：
    - 立题层 (topic): 写什么、为什么写、结论落哪
    - 执行层 (writer_brief + user_constraints): 怎么组织、覆盖什么、模仿什么风格
    - 证据材料层 (writer_packet + expandable_materials + promising_angles): 用什么事实/数据/案例
    """
    topic = state.get("topic", {})
    writer_brief = state.get("writer_brief", {})
    writer_packet = state.get("writer_packet", {})
    topic_support = state.get("topic_support_materials", {})
    insights = state.get("evidence_backed_insights", [])
    user_requirements = state.get("user_requirements", {})
    reference_articles = state.get("reference_articles", [])
    open_issues = state.get("open_issues", [])
    style_reference_urls = _collect_style_reference_urls(state)

    ctx: dict = {}

    # ── 立题层 ──
    ctx["topic"] = {
        "title": topic.get("title", ""),
        "summary": topic.get("summary", ""),
        "content_angle": topic.get("content_angle", topic.get("suggested_angle", "")),
        "proposed_thesis": topic.get("proposed_thesis", ""),
        "why_this_topic": topic.get("why_this_topic", []),
    }

    # ── 受众与风格 ──
    ctx["audience_and_style"] = {
        "platform": state.get("platform", "wechat"),
        "audience": user_requirements.get("audience", ""),
        "tone": user_requirements.get("tone", ""),
    }

    # ── 用户约束 ──
    ctx["user_constraints"] = {
        "required_keywords": user_requirements.get("required_keywords", []),
        "negative_keywords": user_requirements.get("negative_keywords", []),
        "hard_constraints": user_requirements.get("hard_constraints", []),
    }

    # ── 参考文章（模仿维度） ──
    if reference_articles:
        ctx["reference_articles"] = [
            {
                "ref_id": ra.get("ref_id", ""),
                "title": ra.get("title", ""),
                "imitate_dimensions": ra.get("extraction_focus", []),
                "do_not_copy": ra.get("do_not_copy", []),
            }
            for ra in reference_articles
        ]

    # ── 执行层 ──
    if writer_brief:
        ctx["writer_brief"] = writer_brief
    if style_reference_urls:
        ctx["style_reference_urls"] = style_reference_urls

    # ── 证据材料层 ──
    if writer_packet:
        ctx["writer_packet"] = writer_packet

    # ── 丰富层：可展开材料 ──
    expandable = {}
    if topic_support.get("definitions"):
        expandable["definitions"] = [
            {"term": d.get("term", ""), "definition": d.get("definition", ""), "writer_value": d.get("writer_value", "")}
            for d in topic_support["definitions"]
        ]
    if topic_support.get("comparisons"):
        expandable["comparisons"] = [
            {"axis": c.get("comparison_axis", ""), "summary": c.get("summary", "")}
            for c in topic_support["comparisons"]
        ]
    if topic_support.get("controversies"):
        expandable["controversies"] = [
            {"issue": ct.get("issue", ""), "viewpoints": ct.get("viewpoints", []), "writer_value": ct.get("writer_value", "")}
            for ct in topic_support["controversies"]
        ]
    if expandable:
        ctx["expandable_materials"] = expandable

    # ── 丰富层：分析角度 ──
    if insights:
        ctx["promising_angles"] = [
            {"insight": ins.get("insight_text", ""), "type": ins.get("insight_type", ""), "writer_usage": ins.get("writer_usage", "")}
            for ins in insights
        ]

    # ── 风险提示 ──
    if open_issues:
        ctx["open_issues"] = [
            {"description": oi.get("description", ""), "impact": oi.get("impact", "")}
            for oi in open_issues
        ]

    return ctx


def writer_node(state: ContentState) -> ContentState:
    """AI 写作：基于 writer_context（三层结构）生成文章"""
    prompt = _read_prompt("writer.md")
    research = state.get("research", {})

    # 组装 writer_context
    writer_context = _build_writer_context(state)

    # 保存 writer_context 供调试
    _save_artifact(state["run_id"], "writer_context.yaml", yaml.dump(writer_context, allow_unicode=True, default_flow_style=False))

    topic = state.get("topic", {})
    style_reference_urls = _collect_style_reference_urls(state)

    # 兼容旧 schema：如果没有新结构，fallback 到旧格式
    if not state.get("writer_packet") and not state.get("writer_brief"):
        context = "\n".join([
            f"选题: {topic.get('title', '')}",
            f"角度: {topic.get('suggested_angle', '')}",
            f"平台: {state.get('platform', 'wechat')}",
            f"\n--- 调研摘要 ---\n{research.get('executive_summary', '')}",
            f"\n--- 关键发现 ---\n{json.dumps(research.get('key_findings', []), ensure_ascii=False, indent=2)}",
            f"\n--- 数据点 ---\n{json.dumps(research.get('data_points', []), ensure_ascii=False, indent=2)}",
        ])
    else:
        context = f"以下是你的完整写作上下文包（writer_context），包含立题层、执行层、证据材料层三层信息。\n\n{yaml.dump(writer_context, allow_unicode=True, default_flow_style=False)}"
        if style_reference_urls:
            context += (
                "\n\n--- 风格参考链接（优先使用 contentpipe-style-reference 提炼，不要直接照抄） ---\n"
                + json.dumps(style_reference_urls, ensure_ascii=False, indent=2)
            )

    result, content, _ = _call_llm_to_validated_file_with_session(
        state,
        "writer",
        prompt,
        context,
        model=_get_model("writer"),
        output_filename="article_draft.md",
        output_kind="Markdown article正文",
        validator=validate_writer_markdown,
        max_tokens=8192,
    )

    content = (content or result).strip()
    article = {
        "title": state.get("topic", {}).get("title", ""),
        "content": content,
        "word_count": len(content),
    }

    state["article"] = article
    _save_artifact(state["run_id"], "article_draft.md", content)

    # ── 自动去 AI 味（用 Sonnet 4.6，独立 session）──
    logger.info("自动去 AI 味（Sonnet 4.6）...")
    de_ai_prompt = _read_prompt("de-ai-engine.md")
    de_ai_context_parts = [
        f"平台: {state.get('platform', 'wechat')}",
        f"话题分类: {', '.join(state.get('topic', {}).get('keywords', []))}",
        f"\n--- 原始文章 ---\n{content}",
    ]
    if style_reference_urls:
        de_ai_context_parts.append(
            "\n--- 风格参考链接（优先使用 contentpipe-style-reference 提炼，不要直接照抄） ---\n"
            + json.dumps(style_reference_urls, ensure_ascii=False, indent=2)
        )
    de_ai_context = "\n".join(de_ai_context_parts)

    # 去 AI 味用 Sonnet 4.6（文笔+对抗检测），独立 session 不混入 writer 对话
    de_ai_model = _get_model("de_ai_editor") or "anthropic/claude-sonnet-4-6"
    de_ai_reply, de_ai_file, _ = _call_llm_to_validated_file_with_session(
        state,
        "de_ai_editor",
        de_ai_prompt,
        de_ai_context,
        model=de_ai_model,
        output_filename="article_edited.md",
        output_kind="Markdown article正文（去AI味后）",
        validator=lambda text: validate_de_ai_markdown(text, content),
        max_tokens=8192,
    )

    de_ai_result = (de_ai_file or de_ai_reply).strip()
    subtitle = generate_article_subtitle(state, de_ai_result or content, article.get("title", ""))

    state["article_edited"] = de_ai_result
    if isinstance(state.get("article"), dict):
        state["article"]["subtitle"] = subtitle
        state["article"]["word_count"] = len(de_ai_result)
    _save_artifact(state["run_id"], "article_edited.md", de_ai_result)

    state["current_stage"] = "writer"
    _save_state(state)
    return state


def de_ai_editor_node(state: ContentState) -> ContentState:
    """去AI味编辑（已合并到 writer_node，保留兼容性）"""
    # 如果 writer 已经自动跑了去 AI 味，直接跳过
    if state.get("article_edited"):
        state["current_stage"] = "de_ai_editor"
        return state

    prompt = _read_prompt("de-ai-engine.md")
    article = state.get("article", {})

    style_reference_urls = _collect_style_reference_urls(state)
    context_parts = [
        f"平台: {state.get('platform', 'wechat')}",
        f"话题分类: {', '.join(state.get('topic', {}).get('keywords', []))}",
        f"\n--- 原始文章 ---\n{article.get('content', '')}",
    ]
    if style_reference_urls:
        context_parts.append(
            "\n--- 风格参考链接（优先使用 contentpipe-style-reference 提炼，不要直接照抄） ---\n"
            + json.dumps(style_reference_urls, ensure_ascii=False, indent=2)
        )
    context = "\n".join(context_parts)

    reply, file_content, _ = _call_llm_to_validated_file_with_session(
        state,
        "de_ai_editor",
        prompt,
        context,
        model=_get_model("de_ai_editor"),
        output_filename="article_edited.md",
        output_kind="Markdown article正文（去AI味后）",
        validator=lambda text: validate_de_ai_markdown(text, article.get("content", "")),
        max_tokens=8192,
    )

    result = (file_content or reply).strip()

    state["article_edited"] = result
    if isinstance(state.get("article"), dict):
        state["article"]["subtitle"] = generate_article_subtitle(state, result or article.get("content", ""), state.get("article", {}).get("title", ""))
        state["article"]["word_count"] = len(result)
    state["current_stage"] = "de_ai_editor"
    _save_artifact(state["run_id"], "article_edited.md", result)
    _save_state(state)
    return state


def director_node(state: ContentState) -> ContentState:
    """AI 导演（阶段一）：分析文章，输出配图决策"""
    prompt = _read_prompt("art-director.md")

    # 组装上下文：文章 + 风格库 + 可能的用户反馈
    article_content = state.get("article_edited", state.get("article", {}).get("content", ""))
    context_parts = [
        f"文章标题: {state.get('article', {}).get('title', '')}",
        f"平台: {state.get('platform', 'wechat')}",
        f"\n--- 文章正文 ---\n{article_content}",
    ]

    # 如果有用户反馈（阶段一循环），加入上下文
    feedback = state.get("user_feedback")
    if feedback and feedback.get("action") == "revise":
        context_parts.append(f"\n--- 用户修改意见 ---\n{json.dumps(feedback, ensure_ascii=False)}")
        prev_plan = state.get("visual_plan")
        if prev_plan:
            context_parts.append(f"\n--- 上一版配图方案 ---\n{json.dumps(prev_plan, ensure_ascii=False)}")

    context = "\n".join(context_parts)
    result, visual_plan_text, visual_plan = _call_llm_to_validated_file_with_session(
        state,
        "director",
        prompt,
        context,
        model=_get_model("director"),
        output_filename="visual_plan.json",
        output_kind="JSON visual plan",
        validator=validate_visual_plan_json,
        max_tokens=8192,
    )

    # 保存原始输出（调试用）
    _save_artifact(state["run_id"], "director_raw.txt", result)

    state["visual_plan"] = visual_plan
    state["current_stage"] = "director"
    state["review_action"] = ""  # 清空，等待人工审核
    state["user_feedback"] = {}
    _save_artifact(state["run_id"], "visual_plan.json", visual_plan_text or json.dumps(visual_plan, ensure_ascii=False, indent=2))
    _save_state(state)
    return state


def director_refine_node(state: ContentState) -> ContentState:
    """AI 导演（阶段二）：将每个配图描述细化为 3 个 prompt 变体"""
    prompt = _read_prompt("art-director-refine.md")

    visual_plan = state.get("visual_plan", {})
    context_parts = [
        f"全局风格: {visual_plan.get('style', '')}",
        f"全局基调: {visual_plan.get('global_tone', '')}",
        f"\n--- 确认的配图方案 ---\n{json.dumps(visual_plan.get('placements', []), ensure_ascii=False, indent=2)}",
    ]

    # 如果有阶段二用户反馈（图片不满意），加入上下文
    feedback = state.get("user_feedback")
    if feedback and feedback.get("action") == "revise":
        context_parts.append(f"\n--- 用户修改意见 ---\n{json.dumps(feedback, ensure_ascii=False)}")

    context = "\n".join(context_parts)
    expected_ids = [str(p.get("id", "")).strip() for p in visual_plan.get("placements", []) if str(p.get("id", "")).strip()]
    result, image_candidates_text, image_candidates = _call_llm_to_validated_file_with_session(
        state,
        "director_refine",
        prompt,
        context,
        model=_get_model("director_refine"),
        output_filename="image_candidates.json",
        output_kind="JSON image candidates array",
        validator=lambda text: validate_image_candidates_json(text, expected_ids=expected_ids),
        max_tokens=8192,
    )

    _save_artifact(state["run_id"], "director_refine_raw.txt", result)

    state["image_candidates"] = image_candidates
    state["current_stage"] = "director_refine"
    state["review_action"] = ""
    state["user_feedback"] = {}
    _save_artifact(state["run_id"], "image_candidates.json", image_candidates_text or json.dumps(image_candidates, ensure_ascii=False, indent=2))
    _save_state(state)
    return state


# ── 工具节点 ──────────────────────────────────────────────────

def image_gen_node(state: ContentState) -> ContentState:
    """
    图片生成：每个配图位置生成 1 张图片

    直接使用 Director 输出的 visual_plan.placements 中的 description 作为 prompt。
    不再有 3 选 1 —— Director 阶段已经和用户交互确认过配图方案。

    支持用户提供图片 URL（跳过生成）：
      visual_plan.placements[].user_image_url → 直接下载使用
    """
    from image_engines import create_engine_from_config

    visual_plan = state.get("visual_plan", {})
    placements = visual_plan.get("placements", [])
    generated = []
    run_id = state["run_id"]
    img_dir = OUTPUT_DIR / "runs" / run_id / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    existing_generated = {}
    for img in state.get("generated_images", []) or []:
        pid = img.get("placement_id", "")
        fp = img.get("file_path", "")
        if pid and img.get("success") and fp and os.path.exists(fp):
            existing_generated[pid] = img

    existing_cover = state.get("generated_cover", {}) if isinstance(state.get("generated_cover"), dict) else {}
    has_existing_cover = bool(existing_cover.get("success") and existing_cover.get("file_path") and os.path.exists(existing_cover.get("file_path")))

    placement_ids = [str(p.get("id", f"img_{i+1:03d}")) for i, p in enumerate(placements)]
    all_placements_ready = bool(placement_ids) and all(pid in existing_generated for pid in placement_ids)
    if has_existing_cover and all_placements_ready:
        logger.info("image_gen: all assets already provided, skip generation")
        generated = [existing_generated[pid] for pid in placement_ids]
        state["generated_images"] = generated
        state["generated_cover"] = existing_cover
        state["selected_images"] = {g["placement_id"]: "A" for g in generated if g.get("success")}
        state["current_stage"] = "image_gen"
        _save_artifact(run_id, "generated_images.json", json.dumps(generated, ensure_ascii=False, indent=2))
        _save_artifact(run_id, "generated_cover.json", json.dumps(existing_cover, ensure_ascii=False, indent=2))
        _save_state(state)
        return state

    engine = create_engine_from_config()
    logger.info("Image engine: %s", engine)

    # 宽高映射
    aspect_map = {
        "16:9": (1024, 576), "4:3": (1024, 768), "3:4": (768, 1024),
        "1:1": (1024, 1024), "9:16": (576, 1024), "2.35:1": (1410, 600),
    }

    # 先处理封面（优先复用用户已替换/已存在的 cover）
    cover = visual_plan.get("cover", {}) if isinstance(visual_plan, dict) else {}
    generated_cover = existing_cover if has_existing_cover else {}
    if has_existing_cover:
        logger.info("cover: reuse existing uploaded/generated cover")
    elif isinstance(cover, dict) and cover.get("user_image_url"):
        cover_path = img_dir / "cover.jpg"
        try:
            import httpx
            resp = httpx.get(cover.get("user_image_url"), timeout=120, follow_redirects=True)
            resp.raise_for_status()
            cover_path.write_bytes(resp.content)
            generated_cover = {
                "file_path": str(cover_path),
                "engine": "user_provided",
                "prompt_used": "",
                "generation_time_ms": 0,
                "success": True,
                "error": "",
            }
            logger.info("cover: user image saved")
        except Exception as e:
            generated_cover = {"success": False, "error": str(e)[:200], "file_path": "", "engine": "user_provided", "prompt_used": "", "generation_time_ms": 0}
            logger.warning("cover: download failed (%s), falling back to generation", e)
    if (not generated_cover or not generated_cover.get("success")) and isinstance(cover, dict) and cover.get("description"):
        cover_path = img_dir / "cover.jpg"
        cover_aspect = cover.get("aspect_ratio", "2.35:1")
        cover_width, cover_height = aspect_map.get(cover_aspect, (1410, 600))
        cover_prompt = ". ".join(
            part for part in [
                cover.get("description", ""),
                cover.get("purpose", ""),
                cover.get("style_notes", ""),
            ] if part
        )
        logger.info("cover: generating (%s chars)...", len(cover_prompt))
        cover_result = engine.generate(
            prompt=cover_prompt,
            width=cover_width,
            height=cover_height,
            seed=20260311,
            output_path=cover_path,
        )
        generated_cover = {
            "file_path": str(cover_result.file_path) if cover_result.success else "",
            "engine": cover_result.engine,
            "prompt_used": cover_result.prompt_used[:300],
            "generation_time_ms": cover_result.generation_time_ms,
            "success": cover_result.success,
            "error": cover_result.error,
        }
        if cover_result.success:
            logger.info("cover done (%sms)", cover_result.generation_time_ms)
        else:
            logger.error("cover failed: %s", cover_result.error)

    for i, placement in enumerate(placements):
        pid = placement.get("id", f"img_{i+1:03d}")
        desc = placement.get("description", "")
        purpose = placement.get("purpose", "")
        aspect = placement.get("size_hint", placement.get("aspect_ratio", "16:9"))
        width, height = aspect_map.get(aspect, (1024, 576))
        file_path = img_dir / f"{pid}.jpg"

        # 优先复用已上传/已存在图片
        if pid in existing_generated:
            generated.append(existing_generated[pid])
            logger.info("%s: reuse existing uploaded/generated image", pid)
            continue

        # 用户提供了图片 URL → 直接下载
        user_url = placement.get("user_image_url", "")
        if user_url:
            logger.info("%s: downloading user image...", pid)
            try:
                import httpx
                resp = httpx.get(user_url, timeout=120, follow_redirects=True)
                resp.raise_for_status()
                file_path.write_bytes(resp.content)
                generated.append({
                    "placement_id": pid, "file_path": str(file_path),
                    "engine": "user_provided", "success": True,
                    "generation_time_ms": 0, "error": "",
                })
                logger.info("%s: user image saved", pid)
                continue
            except Exception as e:
                logger.warning("%s: download failed (%s), falling back to generation", pid, e)

        # AI 生成
        prompt_text = f"{desc}. {purpose}" if purpose else desc
        logger.info("%s: generating (%s chars)...", pid, len(prompt_text))

        result = engine.generate(
            prompt=prompt_text,
            width=width,
            height=height,
            seed=42 + i,
            output_path=file_path,
        )

        generated.append({
            "placement_id": pid,
            "file_path": str(result.file_path) if result.success else "",
            "engine": result.engine,
            "prompt_used": result.prompt_used[:200],
            "generation_time_ms": result.generation_time_ms,
            "success": result.success,
            "error": result.error,
        })

        if result.success:
            logger.info("%s done (%sms)", pid, result.generation_time_ms)
        else:
            logger.error("%s failed: %s", pid, result.error)

    state["generated_images"] = generated
    state["generated_cover"] = generated_cover
    # 自动构建 selected_images（每个 placement 直接选中唯一的图）
    state["selected_images"] = {
        g["placement_id"]: "A"
        for g in generated if g["success"]
    }
    state["current_stage"] = "image_gen"
    _save_artifact(run_id, "generated_images.json", json.dumps(generated, ensure_ascii=False, indent=2))
    _save_artifact(run_id, "generated_cover.json", json.dumps(generated_cover, ensure_ascii=False, indent=2))
    _save_state(state)
    return state


def formatter_node(state: ContentState) -> ContentState:
    """
    排版：将文章 + 选中图片嵌入微信/小红书模板。

    统一复用 scripts/formatter.py 中的共享实现，避免 nodes.py 与 formatter.py
    各自维护一套模板匹配 / 图片插入逻辑而产生漂移。
    """
    import jinja2
    import formatter as cp_formatter

    article_content = state.get("article_edited") or state.get("article", {}).get("content", "")
    article = state.get("article", {})
    selected = state.get("selected_images", {})
    platform = state.get("platform", "wechat")
    visual_plan = state.get("visual_plan", {})
    run_id = state["run_id"]
    generated_cover = state.get("generated_cover", {})
    generated = state.get("generated_images", [])

    # ── Step 1: 先确定模板（要吃 director.style）──
    director_style = visual_plan.get("style", "") if isinstance(visual_plan, dict) else ""
    topic_keywords = state.get("topic", {}).get("keywords", []) or []
    template_name = cp_formatter.match_template(platform, topic_keywords, director_style=director_style)

    # ── Step 2: Markdown → HTML（模板感知）──
    content_html = cp_formatter.markdown_to_wechat_html(article_content or "", platform, template_name=template_name)

    # ── Step 3: 插图（共享定位算法）──
    placements = visual_plan.get("placements", []) if isinstance(visual_plan, dict) else []
    image_map = {}

    # 先尝试 selected_images 匹配；单候选模式下 option=None 也允许 fallback
    for pid, option in selected.items():
        matched = None
        for img in generated:
            if img.get("placement_id") == pid and img.get("option") == option and img.get("success", True) and img.get("file_path"):
                matched = img
                break
        if matched is None:
            for img in generated:
                if img.get("placement_id") == pid and img.get("success", True) and img.get("file_path"):
                    matched = img
                    break
        if matched:
            image_map[pid] = matched.get("file_path", "")

    if not image_map:
        for img in generated:
            if img.get("success", True) and img.get("placement_id") and img.get("file_path"):
                image_map[img["placement_id"]] = img["file_path"]

    if placements and image_map:
        content_html = cp_formatter.insert_images(content_html, placements, image_map, platform, run_id, template_name=template_name)

    # ── Step 4: 渲染完整 HTML ──
    template_path = PROJECT_ROOT / "templates" / platform / template_name
    if not template_path.exists():
        template_path = PROJECT_ROOT / "templates" / platform / "base.html"

    template_str = template_path.read_text(encoding="utf-8")
    config = load_pipeline_config()
    author = config.get("wechat", {}).get("author", "ContentPipe")

    cover_url = ""
    if isinstance(generated_cover, dict) and generated_cover.get("success") and generated_cover.get("file_path"):
        cover_url = f"/api/runs/{run_id}/images/{os.path.basename(generated_cover['file_path'])}"

    tpl = jinja2.Template(template_str)
    html = tpl.render(
        title=article.get("title", ""),
        subtitle=article.get("subtitle", ""),
        author=author,
        date=datetime.now().strftime("%Y-%m-%d"),
        lead=article.get("subtitle", ""),
        content=content_html,
        category=", ".join(topic_keywords[:2]),
        cover_url=cover_url,
    )

    state["formatted_html"] = html
    state["current_stage"] = "formatter"
    _save_artifact(run_id, "formatted.html", html)
    _save_artifact(run_id, "content_body.html", content_html)
    _save_state(state)
    logger.info("Formatted: %s chars, %s images inserted, template=%s", len(html), len(image_map), template_name)
    return state


def publisher_node(state: ContentState) -> ContentState:
    """
    发布：上传图片 + 创建草稿

    流程：
      1. 获取 access_token
      2. 上传配图到微信 CDN，替换本地路径为 CDN URL
      3. 创建草稿到微信公众号草稿箱
      4. 保存 media_id 供后续正式发布

    如果 WeChat 凭证未配置，跳过实际发布，仅保存 HTML 到本地。
    """
    from tools import wechat_get_token, wechat_upload_image, wechat_create_draft

    platform = state.get("platform", "wechat")
    run_id = state["run_id"]
    config = load_pipeline_config()

    if platform == "wechat":
        result = _publish_wechat(state, config)
    elif platform == "xhs":
        result = _publish_xhs(state, config)
    else:
        result = {"platform": platform, "status": "skipped", "error": f"Unknown platform: {platform}"}

    state["publish_result"] = result
    state["current_stage"] = "publisher"
    state["status"] = "failed" if result.get("status") == "failed" else "completed"
    _save_artifact(run_id, "publish_result.json", json.dumps(result, ensure_ascii=False, indent=2))
    _save_state(state)

    status = result.get("status", "?")
    logger.info("Published: platform=%s, status=%s", platform, status)
    if result.get("media_id"):
        logger.info("media_id: %s", result["media_id"])
    if result.get("error"):
        logger.warning("publish warning: %s", result["error"])

    return state


# ── 人工审核节点 ──────────────────────────────────────────────

def decision_review_node(state: ContentState) -> ContentState:
    """
    阶段一人工审核 — 配图决策审核

    LangGraph interrupt 在这里触发，等待 Web UI 用户操作。
    用户操作写入 state.review_action 和 state.user_feedback。
    """
    state["current_stage"] = "decision_review"
    state["status"] = "review"
    _save_state(state)
    # LangGraph interrupt_before 会在这里暂停
    # Web UI 读取 state → 展示 → 用户操作 → 写回 state
    return state


def image_select_node(state: ContentState) -> ContentState:
    """
    阶段二人工审核 — 图片选择

    用户在 Web UI 中为每个配图位置选择 A/B/C 之一。
    """
    state["current_stage"] = "image_select"
    state["status"] = "review"
    _save_state(state)
    return state


def final_review_node(state: ContentState) -> ContentState:
    """最终预览审核"""
    state["current_stage"] = "final_review"
    state["status"] = "review"
    _save_state(state)
    return state


# ── 路由函数 ──────────────────────────────────────────────────

def route_decision_review(state: ContentState) -> str:
    """阶段一路由：满意 → director_refine，不满意 → director"""
    if state.get("review_action") == "revise":
        return "director"
    return "director_refine"


def route_image_select(state: ContentState) -> str:
    """阶段二路由：全选完 → formatter，有不满意 → director_refine"""
    if state.get("review_action") == "revise":
        return "director_refine"
    return "formatter"


def route_final_review(state: ContentState) -> str:
    """最终审核路由：通过 → publisher，不通过 → writer"""
    if state.get("review_action") == "approve":
        return "publisher"
    return "writer"


# ── 辅助函数 ──────────────────────────────────────────────────

def _match_template(mapping: dict, platform: str, keywords: list[str]) -> str:
    """根据关键词匹配模板"""
    platform_config = mapping.get(platform, {})
    rules = platform_config.get("mapping", [])
    default = platform_config.get("default", "base.html")

    for rule in rules:
        rule_keywords = [k.lower() for k in rule.get("keywords", [])]
        for kw in keywords:
            if kw.lower() in rule_keywords:
                return rule["template"]
    return default


def _markdown_to_wechat_html(md_text: str, platform: str = "wechat") -> str:
    """
    Markdown → 微信兼容内联样式 HTML

    微信不支持 class/外部 CSS，所有样式必须 inline。
    """
    import re

    lines = md_text.strip().split("\n")
    html_parts = []
    in_list = False
    list_type = ""  # "ul" or "ol"
    in_blockquote = False

    # 平台样式配置
    if platform == "wechat":
        styles = {
            "h2": 'style="font-size:18px;font-weight:700;color:#1a1a1a;margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid #eee;"',
            "h3": 'style="font-size:16px;font-weight:700;color:#333;margin:20px 0 8px;"',
            "p":  'style="font-size:16px;color:#333;margin:12px 0;line-height:1.8;"',
            "blockquote": 'style="border-left:3px solid #07c160;padding:8px 14px;color:#666;background:#f7f7f7;margin:16px 0;border-radius:0 4px 4px 0;"',
            "li": 'style="font-size:16px;color:#333;margin:4px 0;line-height:1.8;"',
            "strong": 'style="color:#1a1a1a;"',
            "img": 'style="width:100%;border-radius:8px;margin:16px 0;display:block;"',
        }
    else:
        # 小红书样式（更紧凑）
        styles = {
            "h2": 'style="font-size:17px;font-weight:700;color:#222;margin:16px 0 8px;"',
            "h3": 'style="font-size:15px;font-weight:700;color:#333;margin:12px 0 6px;"',
            "p":  'style="font-size:15px;color:#333;margin:8px 0;line-height:1.7;"',
            "blockquote": 'style="border-left:3px solid #ff2442;padding:6px 12px;color:#666;background:#fff5f5;margin:12px 0;"',
            "li": 'style="font-size:15px;color:#333;margin:3px 0;"',
            "strong": 'style="color:#ff2442;"',
            "img": 'style="width:100%;border-radius:6px;margin:12px 0;display:block;"',
        }

    for line in lines:
        stripped = line.strip()

        # 空行
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
            text = stripped[3:].strip()
            html_parts.append(f'<h2 {styles["h2"]}>{_inline_format(text, styles)}</h2>')
            continue
        if stripped.startswith("### "):
            text = stripped[4:].strip()
            html_parts.append(f'<h3 {styles["h3"]}>{_inline_format(text, styles)}</h3>')
            continue
        if stripped.startswith("# "):
            # h1 通常是标题，模板已处理，跳过
            continue

        # 引用
        if stripped.startswith("> "):
            text = stripped[2:].strip()
            if not in_blockquote:
                html_parts.append(f'<section {styles["blockquote"]}>')
                in_blockquote = True
            html_parts.append(f'<p style="margin:4px 0;color:inherit;">{_inline_format(text, styles)}</p>')
            continue
        elif in_blockquote:
            html_parts.append("</section>")
            in_blockquote = False

        # 无序列表
        if stripped.startswith("- ") or stripped.startswith("* "):
            text = stripped[2:].strip()
            if not in_list:
                html_parts.append('<ul style="padding-left:20px;margin:12px 0;">')
                in_list = True
                list_type = "ul"
            elif list_type != "ul":
                html_parts.append(f"</{list_type}>")
                html_parts.append('<ul style="padding-left:20px;margin:12px 0;">')
                list_type = "ul"
            html_parts.append(f'<li {styles["li"]}>{_inline_format(text, styles)}</li>')
            continue
        elif in_list and not re.match(r"^\d+\.\s+", stripped):
            html_parts.append(f"</{list_type}>")
            in_list = False
            list_type = ""

        # 有序列表
        m = re.match(r"^(\d+)\.\s+(.+)", stripped)
        if m:
            text = m.group(2)
            if not in_list:
                html_parts.append('<ol style="padding-left:20px;margin:12px 0;">')
                in_list = True
                list_type = "ol"
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

    # 关闭未关闭的标签
    if in_list:
        html_parts.append(f"</{list_type}>")
    if in_blockquote:
        html_parts.append("</section>")

    return "\n".join(html_parts)


def _inline_format(text: str, styles: dict) -> str:
    """处理行内格式：加粗、斜体、行内代码、链接"""
    import re

    # 加粗
    text = re.sub(r"\*\*(.+?)\*\*", rf'<strong {styles["strong"]}>\1</strong>', text)
    # 斜体
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # 行内代码
    text = re.sub(
        r"`(.+?)`",
        r'<code style="background:#f0f0f0;padding:2px 6px;border-radius:3px;font-size:14px;color:#d63384;">\1</code>',
        text,
    )
    # 链接
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2" style="color:#576b95;text-decoration:none;">\1</a>', text)
    return text


def _insert_images_into_html(
    content_html: str,
    placements: list[dict],
    image_map: dict[str, str],
    platform: str,
    run_id: str,
) -> str:
    """
    在 HTML 内容中按 placement 指定的位置插入 <img> 标签

    策略：
      - 按 after_paragraph 倒序插入（避免序号偏移）
      - 图片路径暂用本地路径，发布时替换为 CDN URL
    """
    # 拆分成段落（按换行分割后，以闭合标签结尾的行为块边界）
    import re
    # 按换行分割，然后合并成块（每个闭合标签结尾为一个块）
    raw_lines = content_html.split("\n")
    blocks = []
    current = []
    close_tags = ("</p>", "</h2>", "</h3>", "</ul>", "</ol>", "</section>")
    for line in raw_lines:
        current.append(line)
        if any(line.rstrip().endswith(tag) for tag in close_tags):
            blocks.append("\n".join(current))
            current = []
    if current:
        remaining = "\n".join(current).strip()
        if remaining:
            blocks.append(remaining)

    # 按段落号倒序插入（防止序号偏移）
    sorted_placements = sorted(
        [(p, image_map.get(p["id"])) for p in placements if p.get("id") in image_map],
        key=lambda x: x[0].get("after_paragraph", 999),
        reverse=True,
    )

    img_style = (
        'style="width:100%;border-radius:8px;margin:16px 0;display:block;"'
        if platform == "wechat"
        else 'style="width:100%;border-radius:6px;margin:12px 0;display:block;"'
    )

    for placement, file_path in sorted_placements:
        para_idx = placement.get("after_paragraph", 0)
        pid = placement["id"]
        size_hint = placement.get("size_hint", "full_width")

        # 图片宽度
        width_style = ""
        if size_hint == "half":
            width_style = 'style="width:50%;margin:12px auto;display:block;border-radius:8px;"'
        elif size_hint == "thumbnail":
            width_style = 'style="width:30%;margin:8px auto;display:block;border-radius:6px;"'
        else:
            width_style = img_style

        # 构建 <img> 标签（本地路径用相对 URL）
        img_src = f"/api/runs/{run_id}/images/{pid}_{placement.get('_selected_option', 'A')}.png"
        # 备用：直接用文件路径
        if file_path and os.path.exists(file_path):
            # Web UI 预览时使用 API 路径
            filename = os.path.basename(file_path)
            img_src = f"/api/runs/{run_id}/images/{filename}"

        img_tag = f'\n<img src="{img_src}" alt="{placement.get("description", "")[:50]}" {width_style}>\n'

        # 插入到指定段落后
        insert_idx = min(para_idx, len(blocks))
        blocks.insert(insert_idx, img_tag)

    return "\n".join(blocks)


def _publish_wechat(state: dict, config: dict) -> dict:
    """微信公众号发布：上传图片 → 创建草稿"""
    from tools import wechat_get_token, wechat_upload_image, wechat_upload_permanent_image, wechat_create_draft

    wechat_config = config.get("wechat", {})
    app_id = os.getenv("WECHAT_APPID", "") or wechat_config.get("app_id", "")
    app_secret = os.getenv("WECHAT_SECRET", "") or wechat_config.get("app_secret", "")

    # 未配置微信凭证，仅本地保存
    if not app_id or not app_secret:
        logger.warning("微信 AppID/AppSecret 未配置，跳过实际发布")
        return {
            "platform": "wechat",
            "status": "local_only",
            "media_id": "",
            "url": "",
            "note": "微信凭证未配置，HTML 已保存到本地",
        }

    try:
        # 1. 获取 access_token
        token = wechat_get_token(app_id, app_secret)
        logger.info("WeChat token obtained")

        # 2. 上传配图到微信 CDN
        html = state.get("formatted_html", "")
        selected = state.get("selected_images", {})
        generated = state.get("generated_images", [])
        generated_cover = state.get("generated_cover", {})
        run_id = state["run_id"]

        cdn_replacements = {}
        if isinstance(generated_cover, dict) and generated_cover.get("success") and generated_cover.get("file_path"):
            cover_file = generated_cover["file_path"]
            if os.path.exists(cover_file):
                cover_cdn_url = wechat_upload_image(token, open(cover_file, "rb").read(), os.path.basename(cover_file))
                local_cover_url = f"/api/runs/{run_id}/images/{os.path.basename(cover_file)}"
                cdn_replacements[local_cover_url] = cover_cdn_url
                logger.info("Uploaded cover -> %s...", cover_cdn_url[:60])
        for pid, option in selected.items():
            matched = None
            for img in generated:
                if img.get("placement_id") == pid and img.get("option") == option:
                    matched = img
                    break
            if matched is None:
                for img in generated:
                    if img.get("placement_id") == pid and img.get("file_path"):
                        matched = img
                        break
            if matched:
                file_path = matched.get("file_path", "")
                if file_path and os.path.exists(file_path):
                    image_bytes = open(file_path, "rb").read()
                    cdn_url = wechat_upload_image(token, image_bytes, f"{pid}_{option}.png")
                    local_url = f"/api/runs/{run_id}/images/{os.path.basename(file_path)}"
                    cdn_replacements[local_url] = cdn_url
                    logger.info("Uploaded %s_%s -> %s...", pid, option, cdn_url[:60])

        # 替换 HTML 中的图片路径
        for local_url, cdn_url in cdn_replacements.items():
            html = html.replace(local_url, cdn_url)

        # 3. 选择并上传封面素材（P0: 先复用首张成功图；后续再接专门 cover 生成）
        cover_file_path = ""
        if isinstance(generated_cover, dict) and generated_cover.get("success") and generated_cover.get("file_path"):
            cover_file_path = generated_cover["file_path"]
        if not cover_file_path:
            for pid, option in selected.items():
                matched = None
                for img in generated:
                    if img.get("placement_id") == pid and img.get("option") == option and img.get("success") and img.get("file_path"):
                        matched = img
                        break
                if matched is None:
                    for img in generated:
                        if img.get("placement_id") == pid and img.get("success") and img.get("file_path"):
                            matched = img
                            break
                if matched:
                    cover_file_path = matched["file_path"]
                    break
        if not cover_file_path:
            for img in generated:
                if img.get("success") and img.get("file_path"):
                    cover_file_path = img["file_path"]
                    break

        if not cover_file_path or not os.path.exists(cover_file_path):
            raise RuntimeError("No successful generated image available for WeChat draft cover")

        with open(cover_file_path, "rb") as f:
            thumb_media_id = wechat_upload_permanent_image(token, f.read(), os.path.basename(cover_file_path))
        logger.info("WeChat cover uploaded -> thumb_media_id=%s", thumb_media_id)

        # 4. 创建草稿
        article = state.get("article", {})
        topic = state.get("topic", {})
        media_id = wechat_create_draft(token, {
            "title": article.get("title", ""),
            "content_html": html,
            "subtitle": article.get("subtitle", ""),
            "author": wechat_config.get("author", "ContentPipe"),
            "thumb_media_id": thumb_media_id,
        })

        return {
            "platform": "wechat",
            "status": "draft_saved",
            "media_id": media_id,
            "url": "",
            "images_uploaded": len(cdn_replacements),
            "thumb_media_id": thumb_media_id,
            "cover_source": os.path.basename(cover_file_path),
        }

    except Exception as e:
        logger.error("WeChat publish failed: %s", e)
        return {
            "platform": "wechat",
            "status": "failed",
            "media_id": "",
            "url": "",
            "error": str(e),
        }


def _publish_xhs(state: dict, config: dict) -> dict:
    """
    小红书发布（预留）

    小红书 API 非公开，需要通过浏览器自动化或第三方工具。
    当前仅保存为本地文件。
    """
    run_id = state["run_id"]
    article = state.get("article", {})

    # 保存小红书格式的内容
    xhs_content = {
        "title": article.get("title", ""),
        "content": state.get("article_edited", article.get("content", "")),
        "tags": article.get("tags", []),
        "images": [],
    }

    # 收集选中的图片路径
    selected = state.get("selected_images", {})
    generated = state.get("generated_images", [])
    for pid, option in selected.items():
        for img in generated:
            if img.get("placement_id") == pid and img.get("option") == option:
                xhs_content["images"].append(img.get("file_path", ""))
                break

    _save_artifact(run_id, "xhs_content.json", json.dumps(xhs_content, ensure_ascii=False, indent=2))

    return {
        "platform": "xhs",
        "status": "local_only",
        "media_id": "",
        "url": "",
        "note": "小红书 API 未接入，内容已保存到 xhs_content.json",
    }
