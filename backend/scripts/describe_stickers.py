"""一次性脚本：用免费 VLM 给表情包生成一句话语义描述，写回 index.json。

用法（在 backend 目录）：
  .venv\\Scripts\\python.exe scripts\\describe_stickers.py            # 只描述进提示词的 top-N（sticker_max_for_ai）
  .venv\\Scripts\\python.exe scripts\\describe_stickers.py --all      # 全部表情包
  .venv\\Scripts\\python.exe scripts\\describe_stickers.py --dry-run  # 只打印会处理哪些，不调用 API

- 首选免费模型 Qwen/Qwen2.5-VL-7B-Instruct（SiliconFlow 9B 以下永久免费）；
  若返回 400/404（模型不可用）自动全局回退到 .env 的 VISION_MODEL（付费，只兜底）。
- 并发 ~6，429/5xx/网络错误指数退避重试；单张仍失败记占位，不中断，可重跑补齐。
- 写回前备份 index.json；只改 description，name/sha/tags/owner 不动。
"""
import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.path.insert(0, str(Path.cwd()))

from xuwen.chat_api.sticker_store import StickerStore
from xuwen.config import Settings
from xuwen.ingestion.embedder import _resolve_endpoint

FREE_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
DESCRIBE_PROMPT = (
    "用一句简短自然的中文描述这张表情包的画面内容和情绪/梗，"
    "供 AI 在聊天里挑选应景表情包时使用。只输出描述本身，"
    "例如『金毛狗斜眼嫌弃』『猫猫捂脸尴尬』；不要引号、不要解释、不要输出多余文字。"
)
CONCURRENCY = 6
MAX_ATTEMPTS = 4


@dataclass
class Job:
    name: str
    filename: str
    desc: str = ""


@dataclass
class _State:
    settings: Settings
    free_model_failed: bool = False
    jobs: list[Job] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    done: int = 0


def _data_url(path: Path, ext: str) -> str:
    import base64

    raw = path.read_bytes()
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(
        ext, ext
    )
    return f"data:image/{mime};base64,{base64.b64encode(raw).decode()}"


async def _describe_one(client: httpx.AsyncClient, st: _State, url: str, filename: str) -> str:
    base = str(st.settings.vision_api_url)
    endpoint = _resolve_endpoint(base, "/chat/completions")
    headers = {
        "Authorization": f"Bearer {st.settings.vision_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": FREE_MODEL if not st.free_model_failed else st.settings.vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": DESCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ],
        "stream": False,
        "max_tokens": 120,
        "temperature": 0.3,
    }
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = await client.post(endpoint, headers=headers, json=payload)
        except httpx.HTTPError as e:
            last_exc = e
            await asyncio.sleep(2 ** (attempt - 1))
            continue
        if resp.status_code in (400, 404) and payload["model"] == FREE_MODEL:
            st.free_model_failed = True
            payload["model"] = st.settings.vision_model
            continue
        if resp.status_code in (429,) or 500 <= resp.status_code < 600:
            await asyncio.sleep(2 ** attempt)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"VLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not text:
            raise RuntimeError("VLM 返回空描述")
        return text
    raise RuntimeError(f"重试 {MAX_ATTEMPTS} 次仍失败: {last_exc!r} 最后请求 {filename}")


async def worker(
    client: httpx.AsyncClient, st: _State, q: asyncio.Queue[Job], sem: asyncio.Semaphore
) -> None:
    while True:
        job = await q.get()
        if job is None:
            q.task_done()
            return
        try:
            async with sem:
                data_url = _data_url(st.settings.sticker_data_dir / job.filename, job.filename.rsplit(".", 1)[-1])
                job.desc = await _describe_one(client, st, data_url, job.filename)
        except Exception as e:
            job.desc = f"[描述失败] {e}"
            st.failed.append(job.name)
        st.done += 1
        print(f"[{st.done}/{len(st.jobs)}] {job.name}: {job.desc[:60]}")
        q.task_done()


async def run(st: _State) -> None:
    q: asyncio.Queue[Job] = asyncio.Queue()
    for j in st.jobs:
        q.put_nowait(j)
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
        workers = [asyncio.create_task(worker(client, st, q, sem)) for _ in range(CONCURRENCY)]
        await q.join()
        for _ in workers:
            await q.put(None)
        await asyncio.gather(*workers)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="描述全部表情包（默认只描述进提示词的 top-N）")
    parser.add_argument("--dry-run", action="store_true", help="只打印目标，不调用 API")
    args = parser.parse_args()

    settings = Settings()
    store = StickerStore(settings)
    index_path = store._index_path
    if not index_path.exists():
        print(f"index.json 不存在: {index_path}")
        return
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        print("index.json 结构异常（应为列表）")
        return
    by_name = {e.get("name"): e for e in raw if isinstance(e, dict) and e.get("name")}

    if args.all:
        names = list(by_name)
    else:
        names = [s.name for s in store.available_for_ai()]
        print(f"（默认只处理进提示词的 {len(names)} 张；可用 --all 处理全部）")

    st = _State(settings=settings)
    for n in names:
        entry = by_name.get(n)
        if not entry:
            continue
        filename = f"{entry.get('sha')}.{entry.get('extension')}"
        if not (settings.sticker_data_dir / filename).exists():
            print(f"跳过缺失文件: {filename}")
            continue
        st.jobs.append(Job(name=n, filename=filename))

    print(f"目标 {len(st.jobs)} 张，免费模型 {FREE_MODEL}")
    if args.dry_run:
        for j in st.jobs:
            print(" -", j.name, j.filename)
        return

    backup = index_path.with_suffix(".json.bak_" + time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(index_path, backup)
    print("已备份:", backup.name)

    t0 = time.time()
    asyncio.run(run(st))
    print(f"完成，用时 {time.time() - t0:.1f}s；失败 {len(st.failed)} 张")

    new_desc = {j.name: j.desc for j in st.jobs}
    for e in raw:
        if isinstance(e, dict) and e.get("name") in new_desc:
            e["description"] = new_desc[e["name"]]
    tmp = index_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(index_path)
    print(f"已写回 {len(new_desc)} 条描述 -> index.json")

    if st.failed:
        print("以下描述失败（可重跑补齐）:")
        for n in st.failed:
            print(" -", n)


if __name__ == "__main__":
    main()