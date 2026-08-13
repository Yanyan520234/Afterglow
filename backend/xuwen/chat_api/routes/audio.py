"""/v1/audio/transcriptions：OpenAI 兼容的语音转文字端点。

IM 桥收到语音消息后，把音频文件 POST 到这里，返回 {"text": "..."}。
未配置 STT_API_URL 时返回 503（调用方降级为自然话术）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from xuwen.chat_api.state import AppState, get_state
from xuwen.chat_api.stt_client import SttClient, SttError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio"])


@router.post("/v1/audio/transcriptions")
async def transcribe_audio(
    file: UploadFile,
    state: AppState = Depends(get_state),
) -> dict[str, str]:
    settings = state.settings
    if not settings.stt_api_url:
        raise HTTPException(
            status_code=503,
            detail="未配置语音识别（STT_API_URL）。",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="语音内容为空")
    if len(raw) > settings.stt_max_bytes:
        mb = settings.stt_max_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"语音过大（>{mb:.0f}MB）",
        )

    filename = file.filename or "voice.mp3"
    mime = file.content_type or "audio/mpeg"

    client: SttClient | None = None
    try:
        client = SttClient(settings)
        text = await client.transcribe(raw, filename=filename, mime=mime)
    except SttError as e:
        logger.warning("STT 失败: %s", e.message)
        state.metrics.record("stt.transcribe", 0.0, error=e.code)
        raise HTTPException(status_code=502, detail=e.message) from e
    finally:
        if client is not None:
            await client.aclose()

    state.metrics.record("stt.transcribe", 0.0)
    return {"text": text}
