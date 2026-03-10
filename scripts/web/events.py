"""
ContentPipe Event Bus — Pipeline ↔ Web UI 实时通信

进程内 asyncio.Queue 实现的发布-订阅事件总线。
Pipeline 节点通过 publish() 发事件，SSE 端点通过 subscribe() 消费。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class PipelineEvent:
    """单条 Pipeline 事件"""
    type: str           # node_start / node_complete / node_error / review_needed / run_complete
    run_id: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> dict:
        return {
            "event": self.type,
            "data": json.dumps({
                "run_id": self.run_id,
                "timestamp": self.timestamp,
                **self.data,
            }, ensure_ascii=False),
        }


class PipelineEventBus:
    """进程内事件总线，支持多订阅者"""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._history: dict[str, list[PipelineEvent]] = {}
        self._max_history = 100

    async def publish(self, event: PipelineEvent) -> None:
        run_id = event.run_id
        if run_id not in self._history:
            self._history[run_id] = []
        self._history[run_id].append(event)
        if len(self._history[run_id]) > self._max_history:
            self._history[run_id] = self._history[run_id][-self._max_history:]

        for queue in self._subscribers.get(run_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def publish_sync(self, event: PipelineEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(event))
        except RuntimeError:
            pass

    async def subscribe(self, run_id: str, include_history: bool = True) -> AsyncIterator[PipelineEvent]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        if run_id not in self._subscribers:
            self._subscribers[run_id] = []
        self._subscribers[run_id].append(queue)

        try:
            if include_history and run_id in self._history:
                for event in self._history[run_id]:
                    yield event
            while True:
                event = await queue.get()
                yield event
        finally:
            if run_id in self._subscribers:
                try:
                    self._subscribers[run_id].remove(queue)
                except ValueError:
                    pass
                if not self._subscribers[run_id]:
                    del self._subscribers[run_id]

    def get_history(self, run_id: str) -> list[PipelineEvent]:
        return self._history.get(run_id, [])

    def clear(self, run_id: str) -> None:
        self._history.pop(run_id, None)


# ── 全局单例 ──────────────────────────────────────────────────
event_bus = PipelineEventBus()


# ── 便捷函数 ──────────────────────────────────────────────────

def emit_node_start(run_id: str, node: str) -> None:
    event_bus.publish_sync(PipelineEvent(type="node_start", run_id=run_id, data={"node": node}))

def emit_node_complete(run_id: str, node: str, duration_ms: int = 0, summary: str = "") -> None:
    event_bus.publish_sync(PipelineEvent(type="node_complete", run_id=run_id, data={"node": node, "duration_ms": duration_ms, "summary": summary}))

def emit_node_error(run_id: str, node: str, error: str) -> None:
    event_bus.publish_sync(PipelineEvent(type="node_error", run_id=run_id, data={"node": node, "error": error}))

def emit_review_needed(run_id: str, node: str, review_type: str) -> None:
    event_bus.publish_sync(PipelineEvent(type="review_needed", run_id=run_id, data={"node": node, "review_type": review_type}))

def emit_run_complete(run_id: str, total_time_ms: int = 0) -> None:
    event_bus.publish_sync(PipelineEvent(type="run_complete", run_id=run_id, data={"status": "completed", "total_time_ms": total_time_ms}))
