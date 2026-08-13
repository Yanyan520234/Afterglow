#!/usr/bin/env python3
"""手动导入图片目录为表情包。

用法（在 backend 目录，venv）：
    python scripts/import_stickers.py <图片目录> [--description <说明>] [--tags 手动,精选] [--owner shared] [--dry-run]

- 扫描目录（含子目录）里的 png/jpg/jpeg/gif/webp/bmp
- name=文件名前缀（非法字符去掉），重复 name/sha 自动跳过
- description 默认「用户手动添加的表情包」
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xuwen.chat_api.sticker_store import _NAME_RE, StickerError, StickerStore

_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _data_url(path: Path) -> str | None:
    mime = _EXT_MIME.get(path.suffix.lower())
    if not mime:
        return None
    raw = path.read_bytes()
    if not raw:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _make_name(stem: str, sha: str) -> str:
    n = re.sub(r"[^\w一-鿿\-]", "", stem)[:32]
    if not n or not _NAME_RE.fullmatch(n):
        n = "h" + sha[:16]
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", help="图片目录")
    ap.add_argument("--description", default="用户手动添加的表情包")
    ap.add_argument("--tags", default="手动", help="逗号分隔")
    ap.add_argument("--owner", default="shared", choices=("shared", "ai", "self"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from xuwen.config import Settings

    settings = Settings()
    store = StickerStore(settings)
    src_dir = Path(args.dir)
    if not src_dir.is_dir():
        print(f"目录不存在：{src_dir}")
        return 1

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    files = [p for p in src_dir.rglob("*") if p.suffix.lower() in _EXT_MIME]
    if not files:
        print(f"{src_dir} 下没有图片文件")
        return 1

    added = skipped = over = 0
    seen_sha: set[str] = set()
    for p in files:
        try:
            data_url = _data_url(p)
            if data_url is None:
                skipped += 1
                continue
            raw = _extract_raw(data_url)
            if len(raw) > settings.sticker_max_image_bytes:
                over += 1
                print(f"跳过 {p.name}: 超过 {settings.sticker_max_image_bytes / (1024*1024):.1f}MB")
                continue
            sha = hashlib.sha256(raw).hexdigest()
            if sha in seen_sha:
                skipped += 1
                continue
            seen_sha.add(sha)
            name = _make_name(p.stem, sha)
            if store.get(name) is not None:
                skipped += 1
                print(f"跳过 {p.name}: name 已存在 ({name})")
                continue
            if args.dry_run:
                added += 1
                print(f"[dry] {name} <- {p}")
                continue
            store.add(name=name, description=args.description.strip(),
                      data_url=data_url, owner=args.owner, tags=tags)
            added += 1
            print(f"[+] {name} <- {p}")
        except (StickerError, OSError) as e:
            skipped += 1
            print(f"跳过 {p.name}: {e}")

    print(f"\n完成：新增 {added}，跳过 {skipped}，超限 {over}，库内共 {len(store.list_all())} 张")
    return 0


def _extract_raw(data_url: str) -> bytes:
    m = re.match(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", data_url.strip())
    if not m:
        raise ValueError("bad data url")
    return base64.b64decode(m.group(1))


if __name__ == "__main__":
    raise SystemExit(main())
