"""integration 集成测试共享 mock helper。

放置被多个集成测试文件复用的纯函数 mock（LLM/Embedding 端点响应构造等），
避免在各测试文件重复定义。fixture 仍留在各自测试文件中（配置各不相同）。
"""

from __future__ import annotations

import json

import httpx

# 与 test_chat_api.py 的 settings fixture 默认值保持一致：
# embedding_dim=8，向量维度 8，取值 [0.1 * (i+1)] * 8
EMBEDDING_DIM = 8


def embedding_response(req: httpx.Request) -> httpx.Response:
    """构造 OpenAI 兼容 embedding 端点响应（mock side_effect 用）。

    输入：body 含 {"input": [...]}；输出按 input 条数返回嵌入向量。
    """
    body = json.loads(req.read())
    n = len(body["input"])
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": i,
                    "embedding": [0.1 * (i + 1)] * EMBEDDING_DIM,
                }
                for i in range(n)
            ],
            "model": body.get("model", "Qwen3-Embedding-8B"),
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        },
    )


class NoopProactiveContextCache:
    """主动上下文缓存的空实现（探测用，不做任何事）。"""

    async def append_turn(self, **_: object) -> None:
        pass