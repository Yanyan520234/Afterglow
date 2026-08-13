#!/usr/bin/env python3
"""按磁盘现存图片重建 stickers/index.json（不重跑历史导入、不加回已删文件）。

背景：用户直接在数据目录里删了无用图片（642 → 436），连同 index.json 一起丢了。
全量重跑 import_stickers_from_history.py 会把删掉的文件加回来，所以这里只对
「现存文件」重建索引：names/tags/描述与最初导入保持一致（确定性还原），
映射不到的现存文件用兜底名 h{sha[:16]}（会报告出来，可后续改名）。

用法（在 backend 目录，venv）：
    python scripts/rebuild_sticker_index.py [--qq-data-root <nt_data 根>] [--imports-glob imports/*.json] [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.import_stickers_from_history import (
    _data_url_for,
    _iter_image_refs,
    _make_name,
)

DEFAULT_QQ_DATA_ROOT = r"C:\Users\yang\Documents\Tencent Files\2903132650\nt_qq\nt_data"
DEFAULT_SELF_UIN = "2903132650"

_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qq-data-root", default=DEFAULT_QQ_DATA_ROOT)
    ap.add_argument("--imports-glob", default="imports/*.json")
    ap.add_argument("--self-uin", default=DEFAULT_SELF_UIN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from xuwen.chat_api.sticker_store import StickerStore
    from xuwen.config import Settings

    settings = Settings()
    store = StickerStore(settings)
    store_dir = settings.sticker_data_dir

    existing: dict[str, Path] = {}
    for p in store_dir.glob("*"):
        if p.is_file() and p.suffix.lower() in _IMG_EXT and len(p.stem) == 64:
            try:
                bytes.fromhex(p.stem)
            except ValueError:
                continue
            existing.setdefault(p.stem.lower(), p)
    print(f"现存图片文件：{len(existing)} 张（{store_dir}）")

    root = Path(args.qq_data_root)
    imports = sorted(glob.glob(args.imports_glob))
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

    name_index: dict[str, Path] = {}
    for sub in ("Pic", "Emoji"):
        base = root / sub
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    name_index.setdefault(p.name.lower(), p)
    print(f"历史引用 {len(seen_fn)} 个文件名，QQ 原图索引 {len(name_index)} 个")

    sha_meta: dict[str, tuple[Path, str, str, list[str]]] = {}
    for fn, uin in seen_fn.items():
        src = name_index.get(fn.lower())
        if src is None:
            continue
        blob = _data_url_for(src)
        if blob is None:
            continue
        data_url, raw = blob
        sha = hashlib.sha256(raw).hexdigest().lower()
        if sha in sha_meta:
            continue
        label = "阳" if uin == args.self_uin else "对方"
        category = "表情包" if "Emoji" in str(src) else "图片"
        tags = ["历史", label]
        if category == "表情包":
            tags.append("表情包")
        name = _make_name(src.stem, sha)
        sha_meta[sha] = (src, name, f"{label}在聊天里发的{category}", tags)

    hit = miss = fallback = 0
    if args.dry_run:
        for sha, path in existing.items():
            if sha in sha_meta:
                hit += 1
            else:
                miss += 1
                print(f"[dry-run 兜底] {path.name}（历史映射缺失）")
        print(f"dry-run：命中 {hit}，兜底 {miss}，未写入。")
        return 0

    for sha, path in existing.items():
        meta = sha_meta.get(sha)
        if meta is None:
            fallback += 1
            print(f"[兜底] {path.name} 无历史映射 → h{sha[:16]}（可后续改名）")
            raw = path.read_bytes()

            ext = path.suffix.lower()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}[ext.lstrip(".")]
            data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
            try:
                store.add(name="h" + sha[:16], description="历史聊天里的图",
                          data_url=data_url, owner="shared", tags=["历史"])
            except Exception as e:
                print(f"跳过 {path.name}: {e}")
            continue
        src, name, desc, tags = meta
        if store.get(name) is not None:
            continue
        blob = _data_url_for(src)
        if blob is None:
            continue
        try:
            store.add(name=name, description=desc, data_url=blob[0],
                      owner="shared", tags=tags)
            hit += 1
        except Exception as e:
            print(f"跳过 {path.name}: {e}")

    print(f"\n完成：index 重建 {len(store.list_all())} 张（命中 {hit}，兜底 {fallback}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
