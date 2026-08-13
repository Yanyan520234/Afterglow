"""/v1/chat/completions：OpenAI 兼容的对话端点。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from xuwen.chat_api import turn_service
from xuwen.chat_api.chat_pipeline import (
    available_sticker_names,
    build_sticker_retry_hint,
    detect_privacy_request,
    effective_silence_sentinel,
    extract_life_events,
    extract_need_owner_hints,
    extract_schedule_hints,
    extract_send_image_hints,
    fallback_for_rejected_sticker,
    is_ai_silence_signal,
    looks_like_sticker_only_intent,
    rule_fallback_need_owner,
    schedule_life_events,
)
from xuwen.chat_api.image_store import ImageError, save_data_url
from xuwen.chat_api.llm_client import GenerationParams
from xuwen.chat_api.output_filter import AssistantOutputFilter, sanitize_assistant_text
from xuwen.chat_api.proactive_activity import record_proactive_user_activity
from xuwen.chat_api.schedule_extractor import extract_schedule_tasks
from xuwen.chat_api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ImagePart,
    ImageUrlPayload,
    PolicyHint,
    ScheduleTask,
    TextPart,
    Usage,
)
from xuwen.chat_api.schemas import (
    ChatMessage as APIChatMessage,
)
from xuwen.chat_api.state import AppState, get_state
from xuwen.chat_api.turn_coordinator import TurnSnapshot
from xuwen.chat_api.vision_client import VisionClient
from xuwen.chat_api.web_fetch import render_url_context, resolve_fetch_urls
from xuwen.chat_api.web_search import render_web_context, should_search_web
from xuwen.companion.response_policy import (
    ResponseDecision,
)
from xuwen.config import Settings
from xuwen.core.errors import RetrievalError, RetrievalTimeout, XuwenError
from xuwen.core.models import HistoryImageChunk
from xuwen.core.time import local_now
from xuwen.ingestion.image_importer import _usable_image_description
from xuwen.memory.writer import WritebackTurn
from xuwen.persona.prompt import ChatMessage as PromptMessage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

# 后台任务引用容器：asyncio 只保留弱引用，任务对象可能被 GC 提前回收中断执行。
# 用模块级 set 强引用任务，完成后自动丢弃；避免 history_images 异步写入偶发丢失。
_ACTIVE_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


class ChatTurnCancelRequest(BaseModel):
    caller_id: str = Field(..., min_length=1)
    message_ids: list[str] = Field(default_factory=list)


class ChatTurnCancelResponse(BaseModel):
    discarded: int
    cancelled_active: bool
    remaining: int


@router.post("/v1/chat/turns/cancel", response_model=ChatTurnCancelResponse)
async def cancel_chat_turn(
    req: ChatTurnCancelRequest,
    state: AppState = Depends(get_state),
) -> ChatTurnCancelResponse:
    """用户明确停止生成：取消当前 active turn，并丢弃待合并输入。"""
    coordinator = getattr(state, "turn_coordinator", None)
    if coordinator is None:
        return ChatTurnCancelResponse(discarded=0, cancelled_active=False, remaining=0)
    result = await coordinator.discard(
        caller_id=req.caller_id,
        message_ids=req.message_ids,
    )
    return ChatTurnCancelResponse(**result)


def _spawn_history_image_persist(
    state: AppState,
    req: ChatCompletionRequest,
    image_shas: list[str],
    vlm_descriptions: list[str],
    trace_id: str,
) -> None:
    """后台把图片 VLM 描述写入 history_images 长期记忆。

    conversation_id 形如 qq:{user_id}，从中解析 sender_uid 用于溯源。
    embedding 是 API 调用，放后台任务做，不阻塞本轮回复；失败仅 log。
    """

    async def _persist() -> None:
        try:
            sender_uid = ""
            if req.conversation_id and req.conversation_id.startswith("qq:"):
                sender_uid = req.conversation_id[3:]
            friend_name = state.settings.friend_name or "对方"
            now = local_now()
            chunks: list[HistoryImageChunk] = []
            for i, (sha, desc) in enumerate(zip(image_shas, vlm_descriptions, strict=True)):
                # 失败/占位描述（识别失败/超时/无描述）不写入长期记忆
                if not _usable_image_description(desc):
                    continue
                chunks.append(
                    HistoryImageChunk(
                        chunk_id=f"live-img-{uuid.uuid4().hex[:16]}",
                        message_id=req.client_message_id or "",
                        session_id=req.conversation_id or "",
                        seq=i,
                        timestamp_ms=int(now.timestamp() * 1000),
                        sender_uid=sender_uid,
                        sender_name=friend_name,
                        sender_role="friend",
                        image_sha=sha,
                        image_name="",
                        mime="",
                        size=0,
                        description=desc,
                        vision_model=state.settings.vision_model,
                        source="human_original_image",
                        trust_level=0.8,
                    )
                )
            if not chunks:
                return
            texts = [c.description for c in chunks]
            embeddings = await state.embedder.embed_texts(texts)
            emb_map = {c.chunk_id: vec for c, vec in zip(chunks, embeddings, strict=True)}
            await state.store.upsert_history_image_chunks(chunks, emb_map)
            state.metrics.record(
                "history.images", 0.0, detail=f"trace={trace_id},chunks={len(chunks)}"
            )
        except Exception:
            logger.warning("history_images 异步写入失败", exc_info=True)

    try:
        task = asyncio.create_task(_persist())
        _ACTIVE_BACKGROUND_TASKS.add(task)
        task.add_done_callback(_ACTIVE_BACKGROUND_TASKS.discard)
    except RuntimeError:
        # 事件循环关闭等极端情况，静默丢弃
        pass


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    state: AppState = Depends(get_state),
) -> StreamingResponse | ChatCompletionResponse:
    trace_id = str(getattr(request.state, "request_id", "") or "")
    user_messages = [m for m in req.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="messages 中至少要有一条 role=user")
    last_user = user_messages[-1]

    image_shas: list[str] = []
    vlm_descriptions: list[str] = []
    images_in_last = last_user.image_urls()
    current_user_text = last_user.text_only().strip()

    turn_snapshot: TurnSnapshot | None = None

    if images_in_last:
        if not state.settings.vision_enabled:
            raise HTTPException(
                status_code=400,
                detail="未启用视觉理解。请在后端 .env 设置 VISION_ENABLED=true。",
            )
        for url in images_in_last:
            try:
                ref = save_data_url(url, state.settings)
            except ImageError as e:
                raise HTTPException(status_code=400, detail=e.message) from e
            image_shas.append(ref.sha)
        if not state.settings.chat_model_supports_vision:
            if (
                not state.settings.vision_api_url
                or not state.settings.vision_api_key.get_secret_value()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="主模型不支持视觉，且 VISION_API_URL / VISION_API_KEY 未配置。",
                )

    if req.caller_id:
        coordinator = getattr(state, "turn_coordinator", None)
        if coordinator is not None:
            turn_snapshot = await coordinator.begin_turn(
                caller_id=req.caller_id,
                message_id=req.client_message_id,
                text=current_user_text,
                image_shas=image_shas,
                image_urls=images_in_last,
            )

    if current_user_text or images_in_last:
        await record_proactive_user_activity(
            state,
            req.conversation_id,
            req.caller_id,
        )

    recent: list[PromptMessage] = [
        PromptMessage(role=m.role, content=m.text_only())
        for m in req.messages[:-1]
        if m.role in {"user", "assistant"}
    ]

    if turn_snapshot is not None:
        coordinator = getattr(state, "turn_coordinator", None)
        if coordinator is not None:
            await coordinator.update_pending_input(
                turn_snapshot,
                text=current_user_text,
                image_shas=image_shas,
                image_urls=images_in_last,
            )
        if await _turn_was_cancelled(state, turn_snapshot):
            model_name = state.settings.chat_model
            if req.stream:
                return StreamingResponse(
                    _stream_cancelled(model_name=model_name, trace_id=trace_id),
                    media_type="text/event-stream",
                )
            return _cancelled_response(model_name=model_name, trace_id=trace_id)
        current_user_text = turn_snapshot.combined_text() or current_user_text
        image_shas = turn_snapshot.combined_image_shas()
        images_in_last = turn_snapshot.combined_image_urls()

    if images_in_last and not state.settings.chat_model_supports_vision:
        try:
            async with VisionClient(state.settings) as vc:
                # 预算 = 单张预算 × 张数：动图单张就要 ~20s，整组共享 30s 会让
                # 动图或多图场景误超时。每张给足时间，静态图快不受拖累。
                per_image_budget = max(5.0, state.settings.vision_timeout_seconds)
                group_budget = per_image_budget * len(images_in_last)
                vlm_descriptions = await asyncio.wait_for(
                    vc.describe_images(images_in_last),
                    timeout=group_budget,
                )
        except TimeoutError:
            logger.warning(
                "VLM 描图超时（预算 %.1fs × %d 张），使用占位描述",
                state.settings.vision_timeout_seconds,
                len(images_in_last),
            )
            state.metrics.record("vision.describe", 0.0, error="TimeoutError")
            vlm_descriptions = ["[图片：识别超时]"] * len(images_in_last)
        except Exception:
            logger.warning("VLM 描图失败，使用占位描述", exc_info=True)
            state.metrics.record("vision.describe", 0.0, error="Exception")
            vlm_descriptions = ["[图片：识别失败]"] * len(images_in_last)
    if vlm_descriptions:
        desc_block = "\n".join(
            f"（对方发来一张图：{d}）" for d in vlm_descriptions
        )
        current_user_text = (current_user_text + "\n" + desc_block).strip()
        # 后台异步把图片描述写入 history_images 长期记忆（不阻塞本轮回复）
        if image_shas and len(image_shas) == len(vlm_descriptions):
            _spawn_history_image_persist(state, req, image_shas, vlm_descriptions, trace_id)

    if req.voice_text and req.voice_text.strip():
        voice = req.voice_text.strip()
        frame = f"（对方发来一段语音：{voice}）"
        current_user_text = (
            (current_user_text + "\n" + frame).strip() if current_user_text else frame
        )

    retrieval_query = current_user_text if current_user_text else "（用户发了一张图片）"

    # Layer A（共享管道）：检索 / 关系记忆 / life 并发。检索超时/失败抛类型化异常，
    # 本路由（chat）catch → 504/503（不变量②）；responses 路由 catch → 降级。
    try:
        layer_a = await turn_service.run_layer_a(
            state,
            route="chat",
            retrieval_query=retrieval_query,
            current_user_text=current_user_text,
            recent=recent,
            conversation_id=req.conversation_id,
            trace_id=trace_id,
        )
    except RetrievalTimeout as e:
        logger.warning("检索超时，停止本轮聊天：%s", e.message)
        state.metrics.record("retrieval", 0.0, error="TimeoutError")
        raise HTTPException(
            status_code=504,
            detail=(
                f"记忆检索超时（>{state.settings.retrieval_timeout_seconds:g}s）。"
                "本轮已停止：请先检查 Embedding/向量模型连通性。"
            ),
        ) from None
    except RetrievalError as e:
        logger.warning("检索失败，停止本轮聊天：%s", e.message)
        state.metrics.record("retrieval", 0.0, error=type(e).__name__)
        raise HTTPException(
            status_code=503,
            detail=f"记忆检索失败，本轮已停止：{e.message}",
        ) from e
    retrieved = layer_a.retrieved
    relationship_block = layer_a.relationship_block
    life = layer_a.life

    async def _web_search_or_skip() -> str:
        web_should_search = should_search_web(current_user_text)
        if state.web_search is None or not web_should_search:
            _record_web_search_skipped(
                state,
                trace_id=trace_id,
                query=current_user_text,
                should_search=web_should_search,
            )
            return ""
        try:
            results = await state.web_search.search(
                current_user_text,
                trace_id=trace_id,
                metrics=state.metrics,
            )
            return render_web_context(results)
        except Exception:
            logger.warning("web_search 调用失败", exc_info=True)
            return ""

    async def _resolve_urls_or_skip() -> list[str]:
        if state.web_fetch is None:
            return []
        try:
            return await resolve_fetch_urls(
                current_user_text,
                llm=state.life_llm,
                model=state.settings.resolved_life_model,
                limit=state.settings.web_fetch_max_urls,
                trace_id=trace_id,
                metrics=state.metrics,
            )
        except Exception:
            logger.warning("resolve_fetch_urls 失败", exc_info=True)
            return []

    # 模型名固定使用后端配置的 CHAT_MODEL；req.model 接受但忽略
    model_name = state.settings.chat_model

    # fetch_many 依赖 fetch_urls；定义函数体，等 silence 决策确认后再启动
    async def _fetch_many_or_skip(urls: list[str]) -> str:
        if not urls or state.web_fetch is None:
            _record_web_fetch_skipped(state, trace_id=trace_id, urls=urls)
            return ""
        try:
            url_results = await state.web_fetch.fetch_many(
                urls,
                trace_id=trace_id,
                metrics=state.metrics,
            )
            return render_url_context(url_results)
        except Exception:
            logger.warning("fetch_many 失败", exc_info=True)
            return ""

    response_decision, policy_hint = await turn_service.decide_policy(
        state,
        route="chat",
        current_user_text=current_user_text,
        has_images=bool(images_in_last),
        retrieved=retrieved,
        life=life,
        relationship_context=relationship_block,
        recent=recent,
        trace_id=trace_id,
    )

    # 隐私类请求（要本人声音 / 要本人照片 / 要隐私信息）：触及本人隐私，
    # 规则层强制静默——对对方完全不发可见消息，只把提醒带出给 owner（桥接转发）定夺。
    # 放 web/url 调用之前，避免隐私请求还把消息发到搜索/URL 解析端。
    privacy_hints = detect_privacy_request(current_user_text)
    if privacy_hints:
        if req.conversation_id and (current_user_text or image_shas):
            await state.writeback.enqueue_turn(
                WritebackTurn(
                    conversation_id=req.conversation_id,
                    user_text=current_user_text,
                    assistant_text="",
                    user_image_shas=image_shas,
                )
            )
        await state.proactive_context_cache.append_turn(
            caller_id=req.caller_id,
            conversation_id=req.conversation_id,
            user_text=current_user_text,
            assistant_text="",
        )
        state.metrics.record(
            "chat.silenced.privacy",
            0.0,
            detail=f"trace={trace_id},hints={','.join(privacy_hints)}",
        )
        await _ack_turn(state, turn_snapshot)
        if req.stream:
            return StreamingResponse(
                _stream_silenced(
                    settings=state.settings,
                    model_name=model_name,
                    trace_id=trace_id,
                    policy=policy_hint,
                ),
                media_type="text/event-stream",
            )
        return ChatCompletionResponse(
            model=model_name,
            choices=[
                Choice(
                    index=0,
                    message=APIChatMessage(
                        role="assistant",
                        content=state.settings.silence_response_sentinel,
                    ),
                    finish_reason=state.settings.silence_finish_reason,
                )
            ],
            usage=Usage(),
            trace_id=trace_id,
            policy=policy_hint,
            need_owner=privacy_hints,
        )

    # silence 短路：放在 web/url 调用之前，避免用户说"别说话"还把消息发到搜索 / URL 解析端
    if not response_decision.should_reply:
        if await _turn_was_cancelled(state, turn_snapshot):
            if req.stream:
                return StreamingResponse(
                    _stream_cancelled(model_name=model_name, trace_id=trace_id),
                    media_type="text/event-stream",
                )
            return _cancelled_response(model_name=model_name, trace_id=trace_id)
        if req.conversation_id and (current_user_text or image_shas):
            await state.writeback.enqueue_turn(
                WritebackTurn(
                    conversation_id=req.conversation_id,
                    user_text=current_user_text,
                    assistant_text="",
                    user_image_shas=image_shas,
                )
            )
        await state.proactive_context_cache.append_turn(
            caller_id=req.caller_id,
            conversation_id=req.conversation_id,
            user_text=current_user_text,
            assistant_text="",
        )
        state.metrics.record(
            "chat.silenced",
            0.0,
            detail=f"trace={trace_id},{response_decision.metric_detail()}",
        )
        await _ack_turn(state, turn_snapshot)
        if req.stream:
            return StreamingResponse(
                _stream_silenced(
                    settings=state.settings,
                    model_name=model_name,
                    trace_id=trace_id,
                    policy=policy_hint,
                ),
                media_type="text/event-stream",
            )
        return ChatCompletionResponse(
            model=model_name,
            choices=[
                Choice(
                    index=0,
                    message=APIChatMessage(
                        role="assistant",
                        content=state.settings.silence_response_sentinel,
                    ),
                    finish_reason=state.settings.silence_finish_reason,
                )
            ],
            usage=Usage(),
            trace_id=trace_id,
            policy=policy_hint,
        )

    # Layer B：decision 确认要回复后才并发跑 Web Search + URL Resolve。
    # fetch_many 依赖 fetch_urls，依旧 fire-and-forget 让它在 prompt 组装 / LLM 调用前完成。
    web_context, fetch_urls = await asyncio.gather(
        _web_search_or_skip(),
        _resolve_urls_or_skip(),
    )
    fetch_many_task: asyncio.Task[str] = asyncio.create_task(_fetch_many_or_skip(fetch_urls))

    persona_card = await turn_service.build_persona_card(
        state,
        life=life,
        relationship_context=relationship_block,
        style_query=current_user_text,
        decision=response_decision,
        include_schedule_hint=True,  # 仅本路由（不变量③）
    )
    # 等 Layer B 起的 fetch_many 跑完。如果 prompt 组装已盖住 fetch RTT，这里 await 接近 0ms。
    url_context = await fetch_many_task

    messages = await turn_service.build_messages(
        state,
        persona_card=persona_card,
        retrieved=retrieved,
        recent=recent,
        current_user_message=current_user_text or "（图片）",
        web_context=web_context,
        url_context=url_context,
        images=images_in_last or None,
    )

    params = GenerationParams(
        temperature=req.temperature,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
        presence_penalty=req.presence_penalty,
        frequency_penalty=req.frequency_penalty,
    )

    if await _turn_was_cancelled(state, turn_snapshot):
        if req.stream:
            return StreamingResponse(
                _stream_cancelled(model_name=model_name, trace_id=trace_id),
                media_type="text/event-stream",
            )
        return _cancelled_response(model_name=model_name, trace_id=trace_id)

    # 真流式：仅当 RESPONSE_STREAMING_ENABLED=true 时启用。
    # 否则即使客户端传 stream=true 也走非流式路径，最后再包装成 SSE 单 chunk 发出
    # （Afterglow 模拟"真人发消息"，不应该逐字蹦）。
    if req.stream and state.settings.response_streaming_enabled:
        return StreamingResponse(
            _stream_response(
                state=state,
                messages=messages,
                params=params,
                model_name=model_name,
                conversation_id=req.conversation_id,
                caller_id=req.caller_id,
                user_text=current_user_text,
                image_shas=image_shas,
                trace_id=trace_id,
                policy=policy_hint,
                decision=response_decision,
                turn_snapshot=turn_snapshot,
            ),
            media_type="text/event-stream",
        )

    _llm_start = time.perf_counter()
    try:
        raw_assistant_text = await state.llm.complete_chat(
            messages,
            params,
            model=model_name,
            trace_id=trace_id,
            stage="chat.complete",
            metrics=state.metrics,
        )
        life_extraction = extract_life_events(raw_assistant_text)
        stripped = life_extraction.text
        accepted_life_events = life_extraction.events
        valid_names = available_sticker_names(state.settings)
        assistant_text = sanitize_assistant_text(
            stripped,
            valid_sticker_names=valid_names,
        )
        # AI 自主沉默：主模型严格输出 sentinel → 转沉默路径。
        # unsafe 等硬边界场景由 is_ai_silence_signal 内部守卫，不会进入这里。
        ai_silenced = is_ai_silence_signal(
            assistant_text,
            sentinel=effective_silence_sentinel(state.settings),
            decision=response_decision,
        )
        if ai_silenced:
            state.metrics.record(
                "chat.silenced.ai",
                0.0,
                detail=f"trace={trace_id},{response_decision.metric_detail()}",
            )
        # 模型整段只发了不存在的 sticker → sanitize 后空。
        # 先尝试让主模型重新生成一次（带明确提示），失败再退回 reply_mode-aware 短句。
        if (
            not ai_silenced
            and assistant_text in {"嗯", ""}
            and looks_like_sticker_only_intent(stripped)
        ):
            retried = False
            if state.settings.sticker_reject_retry and valid_names is not None:
                hint = build_sticker_retry_hint(stripped, valid_names)
                retry_messages = [
                    *messages,
                    {"role": "system", "content": hint},
                ]
                try:
                    retry_raw = await state.llm.complete_chat(
                        retry_messages,
                        params,
                        model=model_name,
                        trace_id=trace_id,
                        stage="chat.complete.sticker_retry",
                        metrics=state.metrics,
                    )
                    retry_extraction = extract_life_events(retry_raw)
                    retry_stripped = retry_extraction.text
                    retry_text = sanitize_assistant_text(
                        retry_stripped,
                        valid_sticker_names=valid_names,
                    )
                    if retry_text and retry_text != "嗯" and not looks_like_sticker_only_intent(
                        retry_stripped
                    ):
                        assistant_text = retry_text
                        accepted_life_events = retry_extraction.events
                        retried = True
                        state.metrics.record(
                            "chat.sticker.retry_ok",
                            0.0,
                            detail=f"trace={trace_id},mode={response_decision.reply_mode}",
                        )
                except Exception:
                    logger.warning("sticker retry 失败，回退到短句兜底", exc_info=True)
            if not retried:
                accepted_life_events = ()
                assistant_text = (
                    fallback_for_rejected_sticker(response_decision.reply_mode)
                    or assistant_text
                )
                state.metrics.record(
                    "chat.sticker.rejected",
                    0.0,
                    detail=f"trace={trace_id},mode={response_decision.reply_mode}",
                )
        state.metrics.record(
            "llm.complete",
            (time.perf_counter() - _llm_start) * 1000,
            detail=model_name,
        )
    except XuwenError as e:
        state.metrics.record(
            "llm.complete",
            (time.perf_counter() - _llm_start) * 1000,
            error=e.code,
        )
        await _ack_turn(state, turn_snapshot)
        raise

    if await _turn_was_cancelled(state, turn_snapshot):
        return _cancelled_response(model_name=model_name, trace_id=trace_id)

    if not ai_silenced:
        schedule_life_events(
            accepted_life_events,
            state.life,
            enabled=state.settings.life_marker_update_enabled,
            llm=state.life_llm,
            model=state.settings.resolved_life_model,
            apply_lock=state.life_apply_lock,
            pending_tasks=state.pending_life_tasks,
            assistant_text=assistant_text,
            current_user_text=current_user_text,
            trace_id=trace_id,
            metrics=state.metrics,
        )

    if req.conversation_id:
        await state.writeback.enqueue_turn(
            WritebackTurn(
                conversation_id=req.conversation_id,
                user_text=current_user_text,
                # 沉默时写空 assistant_text，保持与规则层 silence 短路一致：
                # 历史里不留 sentinel 文本，避免后续检索把 [silent] 当成真人风格。
                assistant_text="" if ai_silenced else assistant_text,
                user_image_shas=image_shas,
            )
        )
        await _remember_relationship_turn(
            state,
            conversation_id=req.conversation_id,
            decision=response_decision,
            trace_id=trace_id,
        )
    await state.proactive_context_cache.append_turn(
        caller_id=req.caller_id,
        conversation_id=req.conversation_id,
        user_text=current_user_text,
        assistant_text="" if ai_silenced else assistant_text,
    )
    await _ack_turn(state, turn_snapshot)
    # Feature #9：从主模型原始输出抽 <schedule-hint>，调用小模型解析为 ScheduleTask。
    # 失败/未启用时为 None；不影响正常回复链路。
    schedule_tasks_field = None
    schedule_hints = extract_schedule_hints(
        raw_assistant_text,
        max_hints=state.settings.schedule_max_hints_per_turn,
    )
    schedule_reason = "ok"
    schedule_start = time.perf_counter()
    if ai_silenced:
        schedule_reason = "ai_silenced"
    elif not state.settings.schedule_extract_enabled:
        schedule_reason = "disabled"
    elif not schedule_hints:
        schedule_reason = "no_hints_from_main_model"
    else:
        tasks = await extract_schedule_tasks(
            schedule_hints,
            llm=state.schedule_extractor_llm,
            settings=state.settings,
            now=local_now(state.settings.app_timezone),
            trace_id=trace_id,
            metrics=state.metrics,
        )
        schedule_tasks_field = tasks or None
        if not tasks:
            schedule_reason = "extractor_returned_empty"
    _record_schedule_debug(
        state,
        trace_id=trace_id,
        enabled=state.settings.schedule_extract_enabled,
        stream=req.stream,
        reason=schedule_reason,
        hints=schedule_hints,
        task_count=len(schedule_tasks_field or []),
        latency_ms=(time.perf_counter() - schedule_start) * 1000,
    )

    # 需本人处理的现实邀约/请求：从主模型原始输出抽 <need-owner> 块，
    # 已由 sanitize 从正文剥离；这里提取内容回传给调用方转发提醒本人。
    # 兜底：模型没输出标记但用户消息命中邀约关键词时，用规则补一个通用提醒，
    # 避免漏掉需要本人决定/履约的现实邀约（模型对协议遵守不稳定时尤其重要）。
    need_owner_field: list[str] | None = None
    if not ai_silenced:
        owner_hints = extract_need_owner_hints(
            raw_assistant_text,
            max_hints=5,
        )
        if not owner_hints:
            owner_hints = rule_fallback_need_owner(current_user_text)
        if owner_hints:
            need_owner_field = owner_hints
    state.metrics.record(
        "chat.need_owner",
        0.0,
        detail=f"trace={trace_id},count={len(need_owner_field or [])}",
    )

    # AI 想发图：<send-image> 协议块 → 透传给桥接从本地图库检索发送
    send_image_field: list[str] | None = None
    if not ai_silenced:
        img_hints = extract_send_image_hints(raw_assistant_text, max_hints=3)
        if img_hints:
            send_image_field = img_hints
    state.metrics.record(
        "chat.send_image",
        0.0,
        detail=f"trace={trace_id},count={len(send_image_field or [])}",
    )

    # 假流式：客户端传 stream=true 但后端配置不启用真流式 → 把完整内容包装成
    # 单个 content chunk + 收尾，按 OpenAI SSE 协议返回，客户端无感。
    if req.stream:
        return StreamingResponse(
            _pseudo_stream_chunks(
                model_name=model_name,
                trace_id=trace_id,
                assistant_text=assistant_text,
                policy=policy_hint,
                schedule_tasks=schedule_tasks_field,
                need_owner=need_owner_field,
                send_image=send_image_field,
                finish_reason=(
                    state.settings.silence_finish_reason if ai_silenced else "stop"
                ),
                silenced=ai_silenced,
            ),
            media_type="text/event-stream",
        )

    return ChatCompletionResponse(
        model=model_name,
        choices=[
            Choice(
                index=0,
                message=APIChatMessage(role="assistant", content=assistant_text),
                finish_reason=(
                    state.settings.silence_finish_reason if ai_silenced else "stop"
                ),
            )
        ],
        usage=Usage(),
        trace_id=trace_id,
        policy=policy_hint,
        schedule_tasks=schedule_tasks_field,
        need_owner=need_owner_field,
        send_image=send_image_field,
    )


async def _pseudo_stream_chunks(
    *,
    model_name: str,
    trace_id: str,
    assistant_text: str,
    policy: PolicyHint,
    schedule_tasks: list[ScheduleTask] | None = None,
    need_owner: list[str] | None = None,
    send_image: list[str] | None = None,
    finish_reason: str = "stop",
    silenced: bool = False,
) -> AsyncIterator[bytes]:
    """OpenAI SSE 协议包装：把已经生成好的完整 assistant_text 作为单个 content chunk
    发出，再发 finish + [DONE]。等同非流式行为，但符合 stream=true 客户端协议预期。"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def _chunk(
        delta: dict[str, Any],
        finish: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "trace_id": trace_id,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish},
            ],
        }
        if extra:
            payload.update(extra)
        return payload

    yield _format_sse(
        _chunk({"role": "assistant"}, extra={"policy": policy.model_dump()})
    )
    if assistant_text and not silenced:
        yield _format_sse(_chunk({"content": assistant_text}))
    final = _chunk({}, finish=finish_reason)
    final["policy"] = policy.model_dump()
    if silenced:
        final["silenced"] = True
    if schedule_tasks:
        final["schedule_tasks"] = [t.model_dump() for t in schedule_tasks]
    if need_owner:
        final["need_owner"] = need_owner
    if send_image:
        final["send_image"] = send_image
    yield _format_sse(final)
    yield b"data: [DONE]\n\n"


async def _stream_response(
    *,
    state: AppState,
    messages: list[dict[str, Any]],
    params: GenerationParams,
    model_name: str,
    conversation_id: str | None,
    caller_id: str | None,
    user_text: str,
    image_shas: list[str],
    trace_id: str,
    policy: PolicyHint,
    decision: ResponseDecision,
    turn_snapshot: TurnSnapshot | None = None,
) -> AsyncIterator[bytes]:
    """OpenAI SSE 格式生成 chunk；收尾块带 policy 字段。"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    buffer: list[str] = []
    output_filter = AssistantOutputFilter(
        valid_sticker_names=available_sticker_names(state.settings),
    )

    def _chunk_dict(
        delta: dict[str, Any],
        finish: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "trace_id": trace_id,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish,
                }
            ],
        }
        if extra:
            payload.update(extra)
        return payload

    if await _turn_was_cancelled(state, turn_snapshot):
        async for chunk in _stream_cancelled(model_name=model_name, trace_id=trace_id):
            yield chunk
        return

    yield _format_sse(
        _chunk_dict({"role": "assistant"}, extra={"policy": policy.model_dump()})
    )

    _stream_start = time.perf_counter()
    tail = ""
    try:
        async for piece in state.llm.stream_chat(
            messages,
            params,
            model=model_name,
            trace_id=trace_id,
            stage="chat.stream",
            metrics=state.metrics,
        ):
            if await _turn_was_cancelled(state, turn_snapshot):
                yield _format_sse(_chunk_dict({}, finish="cancelled"))
                yield b"data: [DONE]\n\n"
                return
            filtered = output_filter.feed(piece)
            if not filtered:
                continue
            buffer.append(filtered)
            yield _format_sse(_chunk_dict({"content": filtered}))
        tail = output_filter.flush()
        state.metrics.record(
            "llm.stream",
            (time.perf_counter() - _stream_start) * 1000,
            detail=f"{model_name},chars={sum(len(p) for p in buffer) + len(tail)}",
        )
    except XuwenError as e:
        state.metrics.record(
            "llm.stream",
            (time.perf_counter() - _stream_start) * 1000,
            error=e.code,
        )
        yield _format_sse(
            {
                "error": {
                    "code": e.code,
                    "message": e.message,
                    "trace_id": trace_id,
                }
            }
        )
        yield b"data: [DONE]\n\n"
        await _ack_turn(state, turn_snapshot)
        return

    raw_full = output_filter.raw_text()
    full_text = "".join(buffer) + tail
    ai_silenced = is_ai_silence_signal(
        full_text,
        sentinel=effective_silence_sentinel(state.settings),
        decision=decision,
    )
    if ai_silenced:
        tail = ""
        state.metrics.record(
            "chat.silenced.ai",
            0.0,
            detail=f"trace={trace_id},{decision.metric_detail()},stream",
        )
    elif tail:
        buffer.append(tail)
        yield _format_sse(_chunk_dict({"content": tail}))

    # 流式补救：模型整条只发了不存在的 sticker → 所有 chunk 都被剥离 →
    # 用户什么也没看到。这里在 finish chunk 之前补发一段 fallback delta。
    full_text = "".join(buffer)
    if not full_text.strip() and not ai_silenced and looks_like_sticker_only_intent(raw_full):
        fallback = fallback_for_rejected_sticker(policy.reply_mode)
        if fallback:
            buffer.append(fallback)
            yield _format_sse(_chunk_dict({"content": fallback}))
            state.metrics.record(
                "chat.sticker.rejected",
                0.0,
                detail=f"trace={trace_id},mode={policy.reply_mode},stream",
            )

    # AI 自主沉默：累积完整 buffer == sentinel → finish_reason 改 silenced，
    # 写历史时 assistant_text 置空（与规则层 silence 短路保持一致）。
    full_text = "".join(buffer)
    finish_reason = state.settings.silence_finish_reason if ai_silenced else "stop"

    # Feature #9 Finding 1：真流式同样要把 schedule_tasks 放进收尾 chunk，
    # 与假流式 / 非流式 schema 一致。失败/未启用时 None，不影响协议兼容。
    final_extra: dict[str, Any] = {"policy": policy.model_dump()}
    if ai_silenced:
        final_extra["silenced"] = True
    schedule_hints = extract_schedule_hints(
        raw_full,
        max_hints=state.settings.schedule_max_hints_per_turn,
    )
    schedule_reason = "ok"
    schedule_start = time.perf_counter()
    if ai_silenced:
        schedule_reason = "ai_silenced"
    elif not state.settings.schedule_extract_enabled:
        schedule_reason = "disabled"
    elif not schedule_hints:
        schedule_reason = "no_hints_from_main_model"
    else:
        stream_tasks = await extract_schedule_tasks(
            schedule_hints,
            llm=state.schedule_extractor_llm,
            settings=state.settings,
            now=local_now(state.settings.app_timezone),
            trace_id=trace_id,
            metrics=state.metrics,
        )
        if stream_tasks:
            final_extra["schedule_tasks"] = [t.model_dump() for t in stream_tasks]
        else:
            schedule_reason = "extractor_returned_empty"
    _record_schedule_debug(
        state,
        trace_id=trace_id,
        enabled=state.settings.schedule_extract_enabled,
        stream=True,
        reason=schedule_reason,
        hints=schedule_hints,
        task_count=len(final_extra.get("schedule_tasks") or []),
        latency_ms=(time.perf_counter() - schedule_start) * 1000,
    )

    # 需本人处理的现实邀约/请求：真流式同样放进收尾 chunk，与假流式/非流式一致。
    if not ai_silenced:
        owner_hints = extract_need_owner_hints(raw_full, max_hints=5)
        if owner_hints:
            final_extra["need_owner"] = owner_hints
    state.metrics.record(
        "chat.need_owner",
        0.0,
        detail=f"trace={trace_id},count={len(final_extra.get('need_owner') or [])}",
    )
    # 发图：真流式同样把 <send-image> 关键词放进收尾 chunk。
    if not ai_silenced:
        img_hints = extract_send_image_hints(raw_full, max_hints=3)
        if img_hints:
            final_extra["send_image"] = img_hints
    state.metrics.record(
        "chat.send_image",
        0.0,
        detail=f"trace={trace_id},count={len(final_extra.get('send_image') or [])}",
    )

    if await _turn_was_cancelled(state, turn_snapshot):
        yield _format_sse(_chunk_dict({}, finish="cancelled"))
        yield b"data: [DONE]\n\n"
        return

    yield _format_sse(_chunk_dict({}, finish=finish_reason, extra=final_extra))
    yield b"data: [DONE]\n\n"

    if await _turn_was_cancelled(state, turn_snapshot):
        return

    # 流结束后只把完整、最终采用的事件交给 Life 模型；不阻塞已发出的回复。
    life_extraction = extract_life_events(raw_full)
    schedule_life_events(
        life_extraction.events if not ai_silenced else (),
        state.life,
        enabled=state.settings.life_marker_update_enabled,
        llm=state.life_llm,
        model=state.settings.resolved_life_model,
        apply_lock=state.life_apply_lock,
        pending_tasks=state.pending_life_tasks,
        assistant_text=full_text,
        current_user_text=user_text,
        trace_id=trace_id,
        metrics=state.metrics,
    )

    assistant_text = "" if ai_silenced else full_text
    if conversation_id and (assistant_text or ai_silenced):
        await state.writeback.enqueue_turn(
            WritebackTurn(
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
                user_image_shas=image_shas,
            )
        )
        await _remember_relationship_turn(
            state,
            conversation_id=conversation_id,
            decision=decision,
            trace_id=trace_id,
        )
    await state.proactive_context_cache.append_turn(
        caller_id=caller_id,
        conversation_id=conversation_id,
        user_text=user_text,
        assistant_text=assistant_text,
    )
    await _ack_turn(state, turn_snapshot)


async def _stream_silenced(
    *,
    settings: Settings,
    model_name: str,
    trace_id: str,
    policy: PolicyHint,
) -> AsyncIterator[bytes]:
    """决策层选择不回复时按 OpenAI SSE 协议返回最小响应。"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def _chunk(
        delta: dict[str, Any],
        finish: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "trace_id": trace_id,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish,
                }
            ],
        }
        if extra:
            payload.update(extra)
        return payload

    yield _format_sse(
        _chunk({"role": "assistant"}, extra={"policy": policy.model_dump()})
    )
    final = _chunk({}, finish=settings.silence_finish_reason)
    final["policy"] = policy.model_dump()
    final["silenced"] = True
    yield _format_sse(final)
    yield b"data: [DONE]\n\n"


async def _turn_was_cancelled(
    state: AppState,
    turn_snapshot: TurnSnapshot | None,
) -> bool:
    if turn_snapshot is None:
        return False
    if turn_snapshot.cancel_event.is_set():
        return True
    coordinator = getattr(state, "turn_coordinator", None)
    if coordinator is None:
        return False
    return not await coordinator.is_current(turn_snapshot)


async def _ack_turn(state: AppState, turn_snapshot: TurnSnapshot | None) -> None:
    if turn_snapshot is None:
        return
    coordinator = getattr(state, "turn_coordinator", None)
    if coordinator is None:
        return
    await coordinator.ack(turn_snapshot)


async def _remember_relationship_turn(
    state: AppState,
    *,
    conversation_id: str,
    decision: ResponseDecision,
    trace_id: str,
) -> None:
    try:
        await state.relationship_memory.remember_turn(
            conversation_id=conversation_id,
            entry=decision.relationship_memory,
        )
    except Exception:
        logger.warning("关系记忆写入失败，已忽略：trace=%s", trace_id, exc_info=True)
        state.metrics.record("relationship.remember", 0.0, error="Exception")


def _cancelled_response(*, model_name: str, trace_id: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model=model_name,
        choices=[
            Choice(
                index=0,
                message=APIChatMessage(role="assistant", content=""),
                finish_reason="cancelled",
            )
        ],
        usage=Usage(),
        trace_id=trace_id,
    )


async def _stream_cancelled(
    *,
    model_name: str,
    trace_id: str,
) -> AsyncIterator[bytes]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "trace_id": trace_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    yield _format_sse(_chunk({"role": "assistant"}))
    yield _format_sse(_chunk({}, finish="cancelled"))
    yield b"data: [DONE]\n\n"


def _format_sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _record_schedule_debug(
    state: AppState,
    *,
    trace_id: str,
    enabled: bool,
    stream: bool,
    reason: str,
    hints: list[str],
    task_count: int,
    latency_ms: float,
) -> None:
    hint_preview = "|".join(_compact_debug_text(h, 80) for h in hints[:3])
    state.metrics.record(
        "schedule.extract",
        latency_ms,
        detail=(
            f"trace={trace_id},enabled={enabled},stream={stream},"
            f"reason={reason},hints={len(hints)},tasks={task_count},"
            f"model={state.settings.resolved_schedule_model or ''},"
            f"hint_preview={hint_preview}"
        ),
    )


def _compact_debug_text(text: str, limit: int) -> str:
    compact = " ".join(text.split()).replace(",", "，")
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _record_web_search_skipped(
    state: AppState,
    *,
    trace_id: str,
    query: str,
    should_search: bool,
) -> None:
    if not state.settings.web_access_enabled:
        reason = "web_access_disabled"
    elif state.web_search is None:
        reason = "web_search_client_inactive"
    elif not should_search:
        reason = "trigger_not_matched"
    else:
        reason = "unknown"
    state.metrics.record(
        "web.search.skipped",
        0.0,
        detail=(
            f"reason={reason},trace={trace_id},"
            f"should_search={str(should_search).lower()},query_chars={len(query)}"
        ),
    )


def _record_web_fetch_skipped(
    state: AppState,
    *,
    trace_id: str,
    urls: list[str],
) -> None:
    if not state.settings.web_access_enabled:
        reason = "web_access_disabled"
    elif not state.settings.web_fetch_enabled:
        reason = "web_fetch_disabled"
    elif state.web_fetch is None:
        reason = "web_fetch_client_inactive"
    elif not urls:
        reason = "no_url"
    else:
        reason = "unknown"
    state.metrics.record(
        "web.fetch.skipped",
        0.0,
        detail=f"reason={reason},trace={trace_id},url_count={len(urls)}",
    )


_unused: tuple[type, ...] = (ChatMessage, ImagePart, ImageUrlPayload, TextPart)
