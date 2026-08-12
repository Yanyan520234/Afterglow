"""共享对话编排层（P1 重构：S2 纯搬迁骨架）。

目标：把 chat.py / responses.py 两条平行流水线的公共部分收敛到这里，
最终形成「共享管道 + 薄适配层」。本文件在 S2 阶段只做**纯搬迁**——
迁入跨路由复用的纯工具函数与数据类，路由端通过 re-export 保持对外兼容，
行为零变化（由 tests/integration/test_protocol_parity.py 兜底）。

原则：
- 零 HTTP 概念：不 import FastAPI / StreamingResponse；错误用类型化异常表达。
- 编排不搬实例：turn_coordinator 仍由 AppState 持有，本层通过引用使用。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from xuwen.chat_api.llm_client import GenerationParams
from xuwen.chat_api.state import AppState
from xuwen.memory.writer import WritebackTurn

# ---------------------------------------------------------------------------
# S2 已迁入：跨路由纯工具（原地迁移，路由下阶段再切换引用）
# ---------------------------------------------------------------------------


def format_sse(payload: dict[str, Any]) -> bytes:
    """打包一条 SSE data 帧（OpenAI SSE 协议）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def compact_debug_text(text: str, limit: int) -> str:
    """压缩调试文本：合并空白、超限截尾补省略号。"""
    compact = " ".join(text.split()).replace(",", "，")
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


class NopTurnContext:
    """无编排的占位上下文（responses 路由用）。

    在 chat 走 turn_coordinator 编排、responses 不编排的差异里，
    共享层对这两种情况都透传一个统一外观，避免调用方写 if/else。
    """

    def __init__(self, snapshot: Any | None = None) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> Any | None:
        return self._snapshot

    def combined_text(self) -> str:
        if self._snapshot is not None and hasattr(self._snapshot, "combined_text"):
            return self._snapshot.combined_text()
        return ""

    def combined_image_shas(self) -> list[str]:
        if self._snapshot is not None and hasattr(self._snapshot, "combined_image_shas"):
            return self._snapshot.combined_image_shas()
        return []

    def combined_image_urls(self) -> list[str]:
        if self._snapshot is not None and hasattr(self._snapshot, "combined_image_urls"):
            return self._snapshot.combined_image_urls()
        return []


# ---------------------------------------------------------------------------
# 编排外观（skeleton）：begin_turn 统一入口
# ---------------------------------------------------------------------------

# 强引用 set：防止任务对象被 GC 提前中断（asyncio.create_task 只留弱引用）。
_ACTIVE_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


async def begin_turn(
    state: AppState,
    *,
    caller_id: str,
    message_id: str | None,
    text: str,
    image_shas: list[str],
    image_urls: list[str],
) -> NopTurnContext:
    """开启一轮对话编排。

    - chat 路由：调用 state.turn_coordinator.begin_turn 做输入合并 / 取消上一轮
    - responses / 其他路由：返回 NopTurnContext（无编排）
    后续阶段（S3）将把 run_layer_a / decide_policy / complete 移入此处。
    """
    coordinator = getattr(state, "turn_coordinator", None)
    if coordinator is None or not caller_id:
        return NopTurnContext()
    snapshot = await coordinator.begin_turn(
        caller_id=caller_id,
        message_id=message_id,
        text=text,
        image_shas=image_shas,
        image_urls=image_urls,
    )
    return NopTurnContext(snapshot)


# ---------------------------------------------------------------------------
# re-export：保持对既有调用方的兼容（反向迁移前先提供同名符号）
# ---------------------------------------------------------------------------

# 保留 GenerationParams / WritebackTurn 的引用（骨架阶段尚未使用，
# 但后续 S3 complete() 用，先导入保持接口稳定）
__all__ = [
    "GenerationParams",
    "NopTurnContext",
    "WritebackTurn",
    "begin_turn",
    "compact_debug_text",
    "format_sse",
]