"""共享对话编排层（P1 重构：S2 纯搬迁骨架 → S3 共享管道收敛）。

目标：把 chat.py / responses.py 两条平行流水线的公共部分收敛到这里，
最终形成「共享管道 + 薄适配层」。

原则（已定稿，不可破坏）：
- 零 HTTP 概念：不 import FastAPI / StreamingResponse；错误用类型化异常表达。
- 检索错误：共享层只抛类型化异常（RetrievalError / RetrievalTimeout），空结果不抛；
  差异（chat=504/503 vs responses=降级）全落在适配层 catch。
- 编排不搬实例：turn_coordinator 仍由 AppState 持有，本层通过引用使用。
- VLM 注入格式、VLM 预算、presence/frequency、schedule 提取、SSE 双线均为适配层差异，不回收到本层。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from xuwen.chat_api.companion_prompt import (
    build_persona_card_with_companion_context,
    empty_retrieval_result,
    render_life_memory_context_from_recent,
)
from xuwen.chat_api.llm_client import GenerationParams
from xuwen.chat_api.state import AppState
from xuwen.chat_api.sticker_store import StickerStore, render_sticker_block_for_prompt
from xuwen.companion.life import LifeSnapshot
from xuwen.companion.response_policy import (
    ResponseDecision,
    decide_response_policy,
    refine_decision_with_llm,
)
from xuwen.core.errors import RetrievalError, RetrievalTimeout
from xuwen.core.models import RetrievalQuery, RetrievalResult
from xuwen.memory.writer import WritebackTurn
from xuwen.persona.prompt import build_chat_messages

logger = logging.getLogger(__name__)

# 关系记忆渲染超时（两路由一致，收敛为共享常量）
_RELATIONSHIP_CONTEXT_TIMEOUT_SECONDS = 5.0

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
# 共享管道（S3）：Layer A 预决策 + 互动策略决策
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LayerA:
    """Layer A 预决策结果：检索 / 关系记忆 / life 并发产物。

    检索错误不吞——适配层需要区分 chat（504/503 raise）与 responses（降级）。
    本类只承载成功路径的产物；检索抛错（RetrievalError/RetrievalTimeout）时
    由调用方 catch。
    """

    retrieved: RetrievalResult
    relationship_block: str
    life: LifeSnapshot


async def run_layer_a(
    state: AppState,
    *,
    route: str,
    retrieval_query: str,
    current_user_text: str,
    recent: list[Any],
    conversation_id: str | None,
    trace_id: str,
    retrieval_fail_open: bool = False,
    relationship_graceful_on_exception: bool = False,
) -> LayerA:
    """Layer A：并发跑「检索 / 关系记忆 / life」，供后续决策使用。

    - **检索**：wait_for 包 retriever.retrieve。
      `retrieval_fail_open=False`（chat）：超时 → RetrievalTimeout，失败 → RetrievalError
      （均不吞，冒泡给适配层 → 504/503）。
      `retrieval_fail_open=True`（responses）：失败/超时 → 降级空结果继续（不变量②）。
      fail_open 时关系记忆/life 仍正常并发计算，与原 responses gather 语义一致。
      空结果 = 正常返回，不抛。
    - **关系记忆**：wait_for 包 render_context；超时→降级空串。
      `relationship_graceful_on_exception=True` 时（responses）非 Timeout 异常也降级；
      False 时（chat）非 Timeout 异常向上抛（保持 chat 原语义）。
    - **life**：wait_for 包 decide_for_turn；超时 → snapshot（两路由一致）。
    - `route`（"chat"/"responses"）仅用于 metric 前缀与 life trigger 的参数化。
    """
    _retrieval_start = time.perf_counter()

    async def _retrieve() -> RetrievalResult:
        try:
            result = await asyncio.wait_for(
                state.retriever.retrieve(
                    RetrievalQuery(
                        query_text=retrieval_query,
                        conversation_id=conversation_id,
                    ),
                    metrics=state.metrics,
                    trace_id=trace_id,
                ),
                timeout=state.settings.retrieval_timeout_seconds,
            )
            state.metrics.record(
                "retrieval",
                (time.perf_counter() - _retrieval_start) * 1000,
                detail=f"final={len(result.fused)}",
            )
            return result
        except RetrievalError as e:
            if not retrieval_fail_open:
                raise
            logger.warning("检索失败，降级到无 RAG 模式：%s", e.message)
            state.metrics.record("retrieval", 0.0, error=type(e).__name__)
            return empty_retrieval_result()
        except TimeoutError:
            if not retrieval_fail_open:
                raise RetrievalTimeout(
                    f"记忆检索超时（>{state.settings.retrieval_timeout_seconds:g}s）"
                ) from None
            logger.warning(
                "检索超时 %.1fs，降级到无 RAG 模式",
                state.settings.retrieval_timeout_seconds,
            )
            state.metrics.record("retrieval", 0.0, error="TimeoutError")
            return empty_retrieval_result()

    async def _relationship_context() -> str:
        try:
            return await asyncio.wait_for(
                state.relationship_memory.render_context(
                    retrieval_query,
                    include_relevant=False,
                    metrics=state.metrics,
                    trace_id=trace_id,
                ),
                timeout=_RELATIONSHIP_CONTEXT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "关系记忆渲染超时 %.1fs，降级为空上下文",
                _RELATIONSHIP_CONTEXT_TIMEOUT_SECONDS,
            )
            state.metrics.record(
                f"{'responses.' if route == 'responses' else ''}relationship.context",
                0.0,
                error="TimeoutError",
            )
            return ""
        except Exception:
            if not relationship_graceful_on_exception:
                raise
            logger.warning(
                "关系记忆渲染失败，降级为空上下文", exc_info=True
            )
            state.metrics.record(
                f"{'responses.' if route == 'responses' else ''}relationship.context",
                0.0,
                error="Exception",
            )
            return ""

    life_markdown = state.relationship_memory.load_markdown()

    async def _life() -> LifeSnapshot:
        async with state.life_apply_lock:
            try:
                return await asyncio.wait_for(
                    state.life.decide_for_turn(
                        llm=state.life_llm,
                        model=state.settings.resolved_life_model,
                        current_user_text=current_user_text,
                        recent=recent,
                        relationship_context=life_markdown,
                        memory_context=render_life_memory_context_from_recent(
                            recent, state.settings
                        ),
                        trigger=route,
                        trace_id=trace_id,
                        metrics=state.metrics,
                    ),
                    timeout=state.settings.life_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "life 决策超时 %.1fs，沿用当前 snapshot",
                    state.settings.life_timeout_seconds,
                )
                state.metrics.record(
                    f"{'responses.' if route == 'responses' else ''}life.decide",
                    0.0,
                    error="TimeoutError",
                )
                return state.life.snapshot()

    retrieved, relationship_block, life = await asyncio.gather(
        _retrieve(),
        _relationship_context(),
        _life(),
    )
    return LayerA(
        retrieved=retrieved,
        relationship_block=relationship_block,
        life=life,
    )


async def decide_policy(
    state: AppState,
    *,
    route: str,
    current_user_text: str,
    has_images: bool,
    retrieved: RetrievalResult,
    life: LifeSnapshot,
    relationship_context: str,
    recent: list[Any],
    trace_id: str,
) -> tuple[ResponseDecision, Any]:
    """互动策略决策：规则层 +（可选）LLM refine，返回 (decision, policy_hint)。

    差异说明：metric 前缀按 route 参数化（chat 用 `response.policy`，responses 相同前缀；
    refine 超时按 route 记 metrics）。policy_hint 结构由 build_policy_hint 统一产。
    """
    decision = decide_response_policy(
        current_user_text=current_user_text,
        has_images=has_images,
        retrieved=retrieved,
        life=life,
        relationship_context=relationship_context,
        recent=recent,
    )
    state.metrics.record(
        "response.policy",
        0.0,
        detail=f"trace={trace_id},{decision.metric_detail()}",
    )
    if state.settings.response_policy_model_enabled:
        try:
            decision = await asyncio.wait_for(
                refine_decision_with_llm(
                    base=decision,
                    llm=state.response_policy_llm,
                    model=state.settings.resolved_response_policy_model,
                    settings=state.settings,
                    current_user_text=current_user_text,
                    recent=recent,
                    life=life,
                    relationship_context=relationship_context,
                    has_images=has_images,
                    trace_id=trace_id,
                    metrics=state.metrics,
                ),
                timeout=state.settings.response_policy_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "互动策略小模型超时 %.1fs，沿用规则层决策",
                state.settings.response_policy_timeout_seconds,
            )
            state.metrics.record(
                "response.policy.refined", 0.0, error="TimeoutError"
            )
        state.metrics.record(
            "response.policy.refined",
            0.0,
            detail=f"trace={trace_id},{decision.metric_detail()}",
        )
    from xuwen.chat_api.chat_pipeline import (
        build_policy_hint,
        effective_reply_delay_seconds,
    )

    reply_delay_seconds = effective_reply_delay_seconds(
        life=life,
        decision=decision,
        settings=state.settings,
    )
    policy_hint = build_policy_hint(
        decision,
        reply_delay_seconds=reply_delay_seconds,
        reply_delay_reason=life.reply_delay_reason,
    )
    return decision, policy_hint


async def build_persona_card(
    state: AppState,
    *,
    life: LifeSnapshot,
    relationship_context: str,
    style_query: str,
    decision: ResponseDecision,
    include_schedule_hint: bool = False,
) -> str:
    """组装 persona 卡片。`include_schedule_hint` 仅 chat 传 True（不变量③）。"""
    from xuwen.chat_api.chat_pipeline import effective_silence_sentinel

    return build_persona_card_with_companion_context(
        settings=state.settings,
        life=life,
        relationship_context=relationship_context,
        style_query=style_query,
        response_policy_context=decision.render_prompt_block(
            silence_sentinel=effective_silence_sentinel(state.settings),
        ),
        include_schedule_hint=include_schedule_hint,
    )


async def build_messages(
    state: AppState,
    *,
    persona_card: str,
    retrieved: RetrievalResult,
    recent: list[Any],
    current_user_message: str,
    web_context: str = "",
    url_context: str = "",
    images: list[str] | None = None,
) -> list[dict[str, Any]]:
    """组装发往 LLM 的 messages（persona + 检索 + 历史 + 当前输入 + web/url + 多模态图）。

    `images` 非空且主模型支持视觉时，把最后一条 user 消息扩成多模态 content。
    """
    messages = build_chat_messages(
        settings=state.settings,
        persona_card=persona_card,
        retrieved=retrieved,
        recent=recent,
        current_user_message=current_user_message or "（图片）",
        web_context=web_context,
        url_context=url_context,
        sticker_block=render_sticker_block_for_prompt(
            StickerStore(state.settings).available_for_ai()
        ),
    )
    if (
        images
        and state.settings.chat_model_supports_vision
        and messages
        and messages[-1]["role"] == "user"
    ):
        text_for_user: Any = messages[-1]["content"]
        if isinstance(text_for_user, str):
            multimodal_content: list[dict[str, Any]] = [
                {"type": "text", "text": text_for_user},
            ]
            for url in images:
                multimodal_content.append(
                    {"type": "image_url", "image_url": {"url": url}}
                )
            messages[-1] = {"role": "user", "content": multimodal_content}
    return messages


# ---------------------------------------------------------------------------
# re-export：保持对既有调用方的兼容（反向迁移前先提供同名符号）
# ---------------------------------------------------------------------------

# 保留 GenerationParams / WritebackTurn 的引用（骨架阶段尚未使用，
# 但后续 S3 complete() 用，先导入保持接口稳定）
__all__ = [
    "GenerationParams",
    "LayerA",
    "NopTurnContext",
    "WritebackTurn",
    "begin_turn",
    "build_messages",
    "build_persona_card",
    "compact_debug_text",
    "decide_policy",
    "format_sse",
    "run_layer_a",
]