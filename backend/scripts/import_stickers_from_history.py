#!/usr/bin/env python3
"""从历史聊天导出里提取 阳 与 对方 发过的图片/表情包，导入 StickerStore。

用法（在 backend 目录，venv）：
    python scripts/import_stickers_from_history.py [--qq-data-root <nt_data 根>] [--describe] [--dry-run]

- 解析 imports/*.json（Afterglow Chat v1），收集所有 image 资源（含发送者 uin）
- 在 QQ 数据目录（nt_data/Pic、nt_data/Emoji，含 Ori 原图）按文件名（忽略大小写）定位原图
- 导入 StickerStore：name=QQ 文件名前缀，description=发送者+来源（可选 VLM 短描述），tags=历史/阳/对方/表情包
- --describe 会用 VISION_MODEL 逐张生成语义描述（更利于 AI 挑选，但慢）
- --dry-run 只统计不写入
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xuwen.chat_api.sticker_store import _NAME_RE, StickerError, StickerStore

DEFAULT_QQ_DATA_ROOT = r"C:\Users\yang\Documents\Tencent Files\2903132650\nt_qq\nt_data"
DEFAULT_SELF_UIN = "2903132650"

_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_ADDED = "added"
_SKIPPED_DUP = "skipped_dup"
_SKIPPED = "skipped"


def _iter_image_refs(payload: dict) -> list[tuple[str, str]]:
    """提取消息里所有图片资源 → (filename, sender_uin)。"""
    refs: list[tuple[str, str]] = []
    for m in payload.get("messages") or []:
        sender = m.get("sender") or {}
        uin = str(sender.get("uin") or "")
        for res in (m.get("content") or {}).get("resources") or []:
            if isinstance(res, dict) and res.get("type") == "image":
                fn = str(res.get("filename") or "").strip()
                if fn:
                    refs.append((fn, uin))
    return refs


def _make_name(fn_stem: str, sha: str) -> str:
    n = re.sub(r"[^\w一-鿿\-]", "", fn_stem)[:16]
    if not n or not _NAME_RE.fullmatch(n):
        n = "h" + sha[:16]
    return n


def _data_url_for(path: Path) -> tuple[str, bytes] | None:
    mime = _EXT_MIME.get(path.suffix.lower())
    if not mime:
        return None
    raw = path.read_bytes()
    if not raw:
        return None
    return (f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", raw)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qq-data-root", default=DEFAULT_QQ_DATA_ROOT)
    ap.add_argument("--imports-glob", default="imports/*.json")
    ap.add_argument("--self-uin", default=DEFAULT_SELF_UIN)
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from xuwen.config import Settings

    settings = Settings()
    store = StickerStore(settings)
    root = Path(args.qq_data_root)
    if not root.is_dir():
        print(f"QQ 数据目录不存在：{root}")
        return 1

    imports = sorted(glob.glob(args.imports_glob))
    if not imports:
        print(f"没有找到导入文件：{args.imports_glob}")
        return 1

    refs: list[tuple[str, str]] = []
    for f in imports:
        try:
            payload = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"跳过 {f}: {e}")
            continue
        refs.extend(_iter_image_refs(payload))

    seen_fn: dict[str, str] = {}
    for fn, uin in refs:
        seen_fn.setdefault(fn, uin)
    n_self = sum(1 for u in seen_fn.values() if u == args.self_uin)
    print(f"图片引用 {len(refs)} 条，去重后 {len(seen_fn)} 个文件名"
          f"（阳={n_self}，对方={len(seen_fn) - n_self}）")

    name_index: dict[str, Path] = {}
    for sub in ("Pic", "Emoji"):
        base = root / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file():
                name_index.setdefault(p.name.lower(), p)
    print(f"本地原图索引 {len(name_index)} 个文件")

    pending: list[tuple[Path, str, str, str, list[str]]] = []
    missing: list[str] = []
    over_size = undecodable = 0
    counts = {_ADDED: 0, _SKIPPED_DUP: 0, _SKIPPED: 0}
    seen_sha: set[str] = set()

    for fn, uin in seen_fn.items():
        src = name_index.get(fn.lower())
        if src is None:
            missing.append(fn)
            continue
        blob = _data_url_for(src)
        if blob is None:
            undecodable += 1
            continue
        data_url, raw = blob
        if len(raw) > settings.sticker_max_image_bytes:
            over_size += 1
            continue

        sha = hashlib.sha256(raw).hexdigest()
        if sha in seen_sha:
            counts[_SKIPPED_DUP] += 1
            continue
        seen_sha.add(sha)

        label = "阳" if uin == args.self_uin else "对方"
        tags = ["历史", label]
        category = "表情包" if "Emoji" in str(src) else "图片"
        if "Emoji" in str(src):
            tags.append("表情包")
        name = _make_name(src.stem, sha)
        if store.get(name) is not None:
            counts[_SKIPPED_DUP] += 1
            continue
        counts[_ADDED] += 1
        pending.append((src, name, f"{label}在聊天里发的{category}", data_url, tags))

    print(f"可导入 {counts[_ADDED]} 张（内容去重跳过 {counts[_SKIPPED_DUP]}，"
          f"找不到原图 {len(missing)}，超限 {over_size}，无法解码 {undecodable}）")
    if args.dry_run:
        print("dry-run 未写入。")
        return 0

    if args.describe:
        from xuwen.chat_api.vision_client import VisionClient

        vision = VisionClient(settings)
        print(f"VLM 描述 {len(pending)} 张…")
        done = 0
        for src, name, desc, data_url, tags in pending:
            try:
                texts = await vision.describe_images([data_url])
                if texts and texts[0]:
                    desc = texts[0]
            except Exception:
                pass
            try:
                store.add(name=name, description=desc, data_url=data_url,
                          owner="shared", tags=tags)
            except (StickerError, KeyError) as e:
                print(f"跳过 {src.name}: {e}")
                continue
            done += 1
            print(f"[{done}/{len(pending)}] {name} <- {src.name} : {desc}")
    else:
        done = 0
        for src, name, desc, data_url, tags in pending:
            try:
                store.add(name=name, description=desc, data_url=data_url,
                          owner="shared", tags=tags)
            except (StickerError, KeyError) as e:
                print(f"跳过 {src.name}: {e}")
                continue
            done += 1
            print(f"[{done}/{len(pending)}] {name} <- {src.name}")

    print(f"\n完成：新增 {done} 张，库内共 {len(store.list_all())} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
