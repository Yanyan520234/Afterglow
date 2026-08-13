"""STT 客户端 + 语音注入单测。"""

from __future__ import annotations

import httpx
import pytest
import respx

from xuwen.chat_api.stt_client import SttClient, SttError
from xuwen.config import Settings


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        embedding_dim=8,
        lance_db_path=tmp_path / "lancedb",
        persona_data_dir=tmp_path / "persona",
        stt_api_url="https://stt.test/v1",
        stt_api_key="sk-test",  # type: ignore[arg-type]
        stt_model="whisper-1",
    )


@pytest.mark.asyncio
async def test_stt_transcribes_ok(settings: Settings):
    async with httpx.AsyncClient() as raw:
        client = SttClient(settings, client=raw)
        with respx.mock(base_url="https://stt.test/v1") as router:
            router.post("/audio/transcriptions").mock(
                return_value=httpx.Response(200, json={"text": "明天一起吃饭吗"})
            )
            text = await client.transcribe(b"fake-audio", filename="voice.silk")
    assert text == "明天一起吃饭吗"


@pytest.mark.asyncio
async def test_stt_constructor_handles_url_with_suffix():
    settings = Settings(
        embedding_dim=8,
        stt_api_url="https://stt.test/v1/audio/transcriptions",
        stt_api_key="sk-x",  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient() as raw:
        client = SttClient(settings, client=raw)
        assert client._url == "https://stt.test/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_stt_propagates_4xx_via_exception(settings: Settings):
    async with httpx.AsyncClient() as raw:
        client = SttClient(settings, client=raw)
        with respx.mock(base_url="https://stt.test/v1") as router:
            router.post("/audio/transcriptions").mock(
                return_value=httpx.Response(401, text="invalid key")
            )
            with pytest.raises(SttError):
                await client.transcribe(b"fake-audio")


@pytest.mark.asyncio
async def test_stt_retries_on_5xx(settings: Settings):
    async with httpx.AsyncClient() as raw:
        client = SttClient(settings, client=raw)
        seq = iter(
            [
                httpx.Response(500, text="boom"),
                httpx.Response(200, json={"text": "好的"}),
            ]
        )
        with respx.mock(base_url="https://stt.test/v1") as router:
            router.post("/audio/transcriptions").mock(side_effect=lambda req: next(seq))
            text = await client.transcribe(b"fake-audio")
    assert text == "好的"


@pytest.mark.asyncio
async def test_stt_empty_result_raises(settings: Settings):
    async with httpx.AsyncClient() as raw:
        client = SttClient(settings, client=raw)
        with respx.mock(base_url="https://stt.test/v1") as router:
            router.post("/audio/transcriptions").mock(
                return_value=httpx.Response(200, json={"text": "  "})
            )
            with pytest.raises(SttError):
                await client.transcribe(b"fake-audio")


@pytest.mark.asyncio
async def test_stt_raises_when_not_configured():
    settings = Settings(embedding_dim=8, stt_api_url="")
    async with httpx.AsyncClient() as raw:
        client = SttClient(settings, client=raw)
        with pytest.raises(SttError, match="STT_API_URL"):
            await client.transcribe(b"fake-audio")


def test_voice_text_schema_field():
    from xuwen.chat_api.schemas import ChatCompletionRequest

    req = ChatCompletionRequest(
        messages=[{"role": "user", "content": "语音内容"}],
        voice_text="明天一起吃饭吗",
    )
    assert req.voice_text == "明天一起吃饭吗"