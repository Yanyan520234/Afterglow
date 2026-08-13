"""语音识别客户端：调用 OpenAI 兼容 audio/transcriptions 端点把语音转文字。

适用场景：IM 桥接收到语音消息后，先把语音转成文字，再以文本注入对话。
端点未配置（stt_api_url 为空）时 transcribe 返回空字符串，调用方自行降级。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from xuwen.config import Settings
from xuwen.core.errors import XuwenError
from xuwen.ingestion.embedder import _resolve_endpoint

logger = logging.getLogger(__name__)


class SttError(XuwenError):
    """STT 调用失败。"""

    code = "xuwen.stt"
    http_status = 502


class _RetryableSttError(SttError):
    pass


class SttClient:
    """OpenAI 兼容的语音转文字客户端。"""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.settings = settings
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        )
        self._url = _resolve_endpoint(
            str(settings.stt_api_url), "/audio/transcriptions"
        )
        self._headers = {
            "Authorization": f"Bearer {settings.stt_api_key.get_secret_value()}",
        }

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def __aenter__(self) -> SttClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def transcribe(
        self, audio: bytes, *, filename: str = "voice.mp3", mime: str = "audio/mpeg"
    ) -> str:
        """把一段语音转成文字；失败时抛 SttError（调用方自行降级）。"""
        if not self.settings.stt_api_url:
            raise SttError("未配置 STT_API_URL")
        if not audio:
            raise SttError("语音内容为空")

        files = {
            "file": (Path(filename).name, audio, mime),
        }
        data: dict[str, object] = {"model": self.settings.stt_model}

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1.0, min=1.0, max=10.0),
            retry=retry_if_exception_type((httpx.HTTPError, _RetryableSttError)),
            reraise=True,
        ):
            with attempt:
                return await self._call_once(files, data)
        raise SttError("STT 重试退出（不应到达）")

    async def _call_once(
        self, files: dict[str, tuple[str, bytes, str]], data: dict[str, object]
    ) -> str:
        try:
            resp = await self._client.post(
                self._url, headers=self._headers, files=files, data=data
            )
        except httpx.HTTPError as e:
            raise _RetryableSttError(f"STT 网络错误：{type(e).__name__}") from e

        if resp.status_code in (429,) or 500 <= resp.status_code < 600:
            logger.warning("STT 上游 %d: %s", resp.status_code, resp.text[:500])
            raise _RetryableSttError(
                f"STT 暂时不可用（HTTP {resp.status_code}）",
                detail={"status": resp.status_code},
            )
        if resp.status_code >= 400:
            logger.error("STT 上游 %d: %s", resp.status_code, resp.text[:500])
            raise SttError(
                f"STT 客户端错误（HTTP {resp.status_code}），请检查 STT_API_URL / STT_API_KEY / STT_MODEL（详情见日志）",
                detail={"status": resp.status_code},
            )

        try:
            data_out = resp.json()
        except ValueError as e:
            raise SttError("STT 返回非 JSON 响应") from e

        text = str(data_out.get("text") or "").strip()
        if not text:
            raise SttError("STT 返回空文本")
        return text
