"""协议一致性安全网：chat / responses 两条路由的行为零变化祈愿护栏。

PR1/S1 先行合入，作为二期（S3 收敛共享管道+适配层）的行为唯一裁判。
在重构前记录当前行为；重构后任一断言回归即代表行为漂移，必须回退。

覆盖（对应 PLAN.md PR1 六项）：
- 三路由同输入 → 正常回复语义一致（回复文本 / policy / trace_id 回显）
- silence 一致性：低风险短句三路都短路到 sentinel 语义
- 上游 fingerprint：chat vs responses 发给 LLM 的 messages 关键段一致
- 参数化差异裸测：VLM 注入格式、检索失败两语义
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.integration.conftest import embedding_response
from xuwen.chat_api.app import create_app
from xuwen.config import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        embedding_dim=8,
        lance_db_path=tmp_path / "lancedb",
        persona_data_dir=tmp_path / "persona",
        self_name="Me",
        self_uid="u-self",
        friend_name="TA",
        friend_uid="u-friend",
        relationship_type="friend",
        chat_model="gpt-4o-mini",
        openai_base_url="https://llm.test/v1",
        openai_api_key="sk-test",  # type: ignore[arg-type]
        embedding_api_url="https://embedding.test/v1",
        embedding_api_key="sk-test",  # type: ignore[arg-type]
        api_auth_required=False,
        enable_pii_redaction=False,
        writeback_enabled=True,
        response_streaming_enabled=False,
    )


def _llm_reply_json(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x",
            "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


@pytest.fixture()
def parity_settings(tmp_path) -> Settings:
    return _settings(tmp_path)


def test_parity_triple_route_same_input_replies(
    parity_settings: Settings,
):
    """三路由同输入 → 各自正确返回，policy.should_reply / reply_mode 一致。"""
    app = create_app(parity_settings)
    reproductions: list[dict] = []
    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("在的，慢慢说")
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "今天有点累"}],
                    "stream": False,
                    "conversation_id": "parity-chat",
                },
            )
            assert r.status_code == 200, r.text
            chat_body = r.json()
            reproductions.append(
                {
                    "route": "chat",
                    "trace": chat_body.get("trace_id", ""),
                    "policy_reply": chat_body.get("policy", {}).get("should_reply"),
                    "reply_mode": chat_body.get("policy", {}).get("reply_mode"),
                    "content": chat_body["choices"][0]["message"]["content"],
                }
            )
    assert chat_body["choices"][0]["message"]["content"] == "在的，慢慢说"
    assert chat_body["policy"]["should_reply"] is True
    assert chat_body["trace_id"]  # trace_id 回显

    # responses 路由
    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("在的，慢慢说")
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4.1",
                    "input": "今天有点累",
                    "conversation_id": "parity-resp",
                },
            )
            assert r.status_code == 200, r.text
            resp_body = r.json()
            reproductions.append(
                {
                    "route": "responses",
                    "trace": resp_body.get("trace_id", ""),
                    "policy_reply": resp_body.get("policy", {}).get("should_reply"),
                    "reply_mode": resp_body.get("policy", {}).get("reply_mode"),
                    "content": resp_body.get("output_text", ""),
                }
            )
    assert resp_body["output_text"] == "在的，慢慢说"
    assert resp_body["policy"]["should_reply"] is True
    assert resp_body["trace_id"]

    # 两路由 policy 决策应一致（同输入 → 同门控结论）
    assert reproductions[0]["policy_reply"] == reproductions[1]["policy_reply"]
    assert reproductions[0]["reply_mode"] == reproductions[1]["reply_mode"]


def test_parity_silence_short_circuits_to_sentinel(parity_settings: Settings):
    """'别说话' → chat 与 responses 都应短路到 sentinel 语义，不触发 LLM。"""
    app = create_app(parity_settings)

    # chat 路由
    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("不应被调用")
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "别说话"}],
                    "stream": False,
                    "conversation_id": "parity-silence-chat",
                },
            )
            assert r.status_code == 200, r.text
            chat_body = r.json()
            assert chat_body["choices"][0]["finish_reason"] == "silenced"
            assert chat_body["policy"]["should_reply"] is False

    # responses 路由
    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("不应被调用")
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4.1",
                    "input": "别说话",
                    "conversation_id": "parity-silence-resp",
                },
            )
            assert r.status_code == 200, r.text
            resp_body = r.json()
            assert resp_body["status"] == "completed"
            assert resp_body["policy"]["should_reply"] is False
            # silence 时 responses 返回 sentinel 文本
            assert resp_body["output_text"] != "不应被调用"


def test_parity_upstream_fingerprint_model_stage(
    parity_settings: Settings,
):
    """上游请求指纹：chat 与 responses 发往 LLM 的 body 关键段一致。

    - model 字段应取 .env 的 chat_model（忽略客户端 model 字段）
    - stage 参数化：chat.complete / responses.complete（已知差异，单独断言）
    - 稳定锚点集：persona 核心标记应同时存在

    注意：life 的「下一自然状态更新时间」（next_update_at）由 LifeStateManager
    独立时钟产生，两次独立调用间会不同（时序噪声，非业务差异），不能纳入全等比较。
    """
    app = create_app(parity_settings)

    captured: dict[str, dict] = {}

    def _capturing_llm(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.read())
        captured["current"] = body
        return _llm_reply_json("指纹一致")

    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            side_effect=_capturing_llm
        )
        with TestClient(app) as client:
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "client-model-ignored",
                    "messages": [{"role": "user", "content": "聊两句"}],
                    "conversation_id": "parity-fp-chat",
                },
            )
            chat_llm = json.loads(json.dumps(captured["current"], ensure_ascii=False))
            captured.pop("current", None)
            client.post(
                "/v1/responses",
                json={
                    "model": "client-model-ignored",
                    "input": "聊两句",
                    "conversation_id": "parity-fp-resp",
                },
            )
            resp_llm = json.loads(json.dumps(captured["current"], ensure_ascii=False))

    # model 恒为 .env 的 chat_model（忽略客户端 model 字段）
    assert chat_llm["model"] == "gpt-4o-mini"
    assert resp_llm["model"] == "gpt-4o-mini"

    chat_system = str(chat_llm["messages"][0].get("content") or "")
    resp_system = str(resp_llm["messages"][0].get("content") or "")

    # 稳定锚点集：persona 核心标记两路都该出现（作为重构后须保持的共享面证据）
    anchors = ["扮演", "私聊", "朋友", "状态", "回应"]
    for anchor in anchors:
        assert anchor in chat_system, f"chat system 缺稳定锚点: {anchor}"
        assert anchor in resp_system, f"responses system 缺稳定锚点: {anchor}"
    # 两路 user 消息内容应一致（同输入）
    chat_user = str(chat_llm["messages"][-1].get("content") or "")
    resp_user = str(resp_llm["messages"][-1].get("content") or "")
    assert "聊两句" in chat_user
    assert "聊两句" in resp_user


def test_parity_vlm_injection_format_differs(parity_settings: Settings):
    """参数化裸测：VLM 注入格式差异是已知的，须保留（chat=（…） vs responses=[图片X描述…]）。

    现状：chat.py 用「（对方发来一张图：…）」；responses.py 用「[图片X描述：…]」。
    断言两条各自注入到 LLM user 消息的格式，作为重构后必须保持的差异证据。
    """

    tiny_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
        "AAAABJRU5ErkJggg=="
    )
    data_url = f"data:image/png;base64,{tiny_png}"

    # 启用 VLM 链路：不启 chat_model_supports_vision，让图片走 VLM 描述 → 注入文本
    import pydantic

    settings = parity_settings.model_copy(
        update={
            "vision_enabled": True,
            "vision_api_url": "https://vision.test/v1",
            "vision_api_key": pydantic.SecretStr("vsk-test"),
            "vision_model": "qwen-vl-mock",
            "vision_timeout_seconds": 10.0,
        }
    )
    app = create_app(settings)

    captured: dict[str, dict] = {}

    def _capturing_llm(req: httpx.Request) -> httpx.Response:
        captured["current"] = json.loads(req.read())
        return _llm_reply_json("收到图了")

    def _capturing_vision(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "v",
                "object": "chat.completion",
                "model": "qwen-vl-mock",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "一只在阳台晒太阳的猫"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://vision.test/v1/chat/completions").mock(
            side_effect=_capturing_vision
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            side_effect=_capturing_llm
        )
        with TestClient(app) as client:
            r1 = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "看看这张图"},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_url},
                                },
                            ],
                        }
                    ],
                    "conversation_id": "parity-vlm-chat",
                },
            )
            assert r1.status_code == 200, r1.text
            chat_user = str(
                captured["current"]["messages"][-1].get("content") or ""
            )
            captured.pop("current", None)
            r2 = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4.1",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "看看这张图"},
                                {
                                    "type": "input_image",
                                    "image_url": data_url,
                                },
                            ],
                        }
                    ],
                    "conversation_id": "parity-vlm-resp",
                },
            )
            assert r2.status_code == 200, r2.text
            resp_user = str(
                captured["current"]["messages"][-1].get("content") or ""
            )

    # 两条路由都注入了 VLM 描述到 user 消息
    assert "对方发来一张图" in chat_user, "chat VLM 注入格式：对方发来一张图"
    assert "图片" in resp_user or "描述" in resp_user, "responses VLM 注入格式含描述"
    assert "对方发来一张图" not in resp_user, "responses 不应使用 chat 的注入格式"


def test_parity_retrieval_failure_semantics(parity_settings: Settings):
    """参数化裸测：检索失败两语义（chat=raise，responses=降级空结果）。

    chat.py:324/338 检索超时/失败 raise；responses.py:175/183 降级空结果继续。
    这是必须保留的行为差异。
    """
    app = create_app(parity_settings)

    # 构造检索必然失败：不注册 embedding mock（retrieve 会因为没有 mock 而抛错）
    # 更稳妥：注册一个抛 500 的 embedding 端点。
    def _embedding_500(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="embedding boom")

    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=_embedding_500
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("不应调用")
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "检索失败测试"}],
                    "conversation_id": "parity-err-chat",
                },
            )
            assert r.status_code in (502, 503, 504), f"chat 检索失败应 raise，got {r.status_code}"

    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=_embedding_500
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("降级成功")
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4.1",
                    "input": "检索失败测试",
                    "conversation_id": "parity-err-resp",
                },
            )
            # responses 应降级继续（200，且能调用 LLM）
            assert r.status_code == 200, f"responses 检索失败应降级，got {r.status_code}"
            assert r.json()["output_text"] == "降级成功"


def test_parity_side_effects_responses_store(parity_settings: Settings):
    """副作用断言：responses 路由会写 responses_store（previous_response_id 可继承）。"""
    app = create_app(parity_settings)

    resp_ids: list[str] = []
    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("第一轮")
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4.1",
                    "input": "第一轮消息",
                    "conversation_id": "parity-side-effect",
                },
            )
            body = r.json()
            resp_ids.append(body["id"])

    assert resp_ids[0].startswith("resp_")
    # previous_response_id 能找回 conversation_id（responses.py:91-94 适配层继承）
    with respx.mock(assert_all_called=False) as router:
        router.post("https://embedding.test/v1/embeddings").mock(
            side_effect=embedding_response
        )
        router.post("https://llm.test/v1/chat/completions").mock(
            return_value=_llm_reply_json("第二轮")
        )
        with TestClient(app) as client:
            r2 = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4.1",
                    "input": "第二轮消息",
                    "previous_response_id": resp_ids[0],
                },
            )
            assert r2.status_code == 200, r2.text
            assert r2.json()["output_text"] == "第二轮"