"""
ContentPipe State — LangGraph 状态定义

所有阶段间数据通过 ContentState 传递。
每个节点只读取需要的字段，写入自己的输出字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from typing_extensions import TypedDict


# ── 阶段输出类型 ──────────────────────────────────────────────

class TopicSuggestion(TypedDict, total=False):
    title: str
    keywords: list[str]
    heat_score: int
    relevance_score: int
    suggested_angle: str
    sources: list[dict]


class ResearchBrief(TypedDict, total=False):
    executive_summary: str
    key_findings: list[dict]
    data_points: list[dict]
    outline_suggestion: list[str]
    raw_sources: list[dict]


class ArticleDraft(TypedDict, total=False):
    title: str
    subtitle: str
    platform: str
    content: str
    word_count: int
    tags: list[str]


class ImagePlacement(TypedDict, total=False):
    id: str
    after_section: str
    after_paragraph: int
    type: str                    # illustration / infographic / photo / diagram
    description: str             # 详细画面描述（几十到上百字）
    purpose: str                 # 在文章中的叙事作用
    aspect_ratio: str
    size_hint: str               # full_width / half / thumbnail


class VisualPlan(TypedDict, total=False):
    style: str
    global_tone: str
    placements: list[ImagePlacement]


class ImageCandidate(TypedDict, total=False):
    option: str                  # A / B / C
    concept: str                 # 创意角度简述
    prompt: str
    negative_prompt: str


class ImagePlacementCandidates(TypedDict, total=False):
    id: str
    original_description: str
    candidates: list[ImageCandidate]
    recommended: str             # A / B / C
    aspect_ratio: str
    seed_base: int


class GeneratedImage(TypedDict, total=False):
    placement_id: str
    option: str                  # A / B / C
    file_path: str
    engine: str
    seed_used: int
    generation_time_ms: int


class PublishResult(TypedDict, total=False):
    platform: str
    status: str                  # draft / published / failed
    media_id: str
    url: str


# ── 用户反馈 ──────────────────────────────────────────────────

class UserFeedback(TypedDict, total=False):
    action: Literal["approve", "revise"]
    instructions: list[dict]     # 逐条修改意见
    global_note: str             # 全局修改意见


# ── 主状态 ────────────────────────────────────────────────────

class ContentState(TypedDict, total=False):
    """LangGraph 全局状态，所有节点共享"""

    # ── 运行元数据 ──
    run_id: str
    status: str                  # pending / running / review / completed / failed
    current_stage: str
    created_at: str
    platform: str                # wechat / xhs

    # ── Scout 输出 ──
    topic: TopicSuggestion

    # ── Researcher 输出 ──
    research: ResearchBrief

    # ── Writer 输出 ──
    article: ArticleDraft

    # ── De-AI Editor 输出 ──
    article_edited: str

    # ── Director 输出（阶段一：配图决策） ──
    visual_plan: VisualPlan

    # ── 人工审核（阶段一） ──
    review_action: str           # approve / revise
    user_feedback: UserFeedback

    # ── Director Refine 输出（阶段二：3 prompt 变体） ──
    image_candidates: list[ImagePlacementCandidates]

    # ── ImageGen 输出 ──
    generated_images: list[GeneratedImage]

    # ── 用户图片选择 ──
    selected_images: dict        # {img_001: "A", img_002: "C", ...}

    # ── Formatter 输出 ──
    formatted_html: str

    # ── Publisher 输出 ──
    publish_result: PublishResult

    # ── 错误追踪 ──
    error: str
    error_stage: str
