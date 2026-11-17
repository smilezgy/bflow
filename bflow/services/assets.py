"""资源存储服务：落盘、去重、缩略图、清理。

关键设计（spec/04-data-model.md §4）：
- 产物格式以**魔数判定**后决定扩展名 —— 渠道声明的 mime 不可信；
- 文件名用短 ID + 真实扩展名，sha256 仅作去重索引；
- 先落盘（幂等）再入库，入库失败由孤儿清理兜底。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..db import execute, query, query_one
from .protocol.imaging import ImageInfo, parse_image

KIND_DIR = {
    "input": settings.input_dir,
    "output": settings.output_dir,
    "mask": settings.input_dir,
    "library": settings.output_dir,
}


@dataclass
class StoredAsset:
    id: str
    kind: str
    path: str
    format: str
    width: int
    height: int
    bytes: int
    sha256: str
    origin: str
    created_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "url": f"/api/assets/{self.id}",
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "origin": self.origin,
            "created_at": self.created_at,
        }


def _new_id(prefix: str = "as") -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def store_bytes(data: bytes, *, kind: str = "output", origin: str = "generate",
                asset_id: str | None = None) -> StoredAsset:
    """把字节流落盘并登记。格式由魔数判定。"""
    info: ImageInfo = parse_image(data)
    ext = info.format if info.format not in ("unknown", "") else "bin"
    aid = asset_id or _new_id()
    directory = KIND_DIR.get(kind, settings.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{aid}.{ext}"
    path = directory / filename
    path.write_bytes(data)

    rel = settings.relative_to_root(path)
    now = time.time()
    execute(
        "INSERT INTO asset (id, kind, path, format, width, height, bytes, sha256,"
        " origin, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, kind, rel, info.format, info.width, info.height,
         info.bytes, info.sha256, origin, now),
    )
    return StoredAsset(aid, kind, rel, info.format, info.width, info.height,
                       info.bytes, info.sha256, origin, now)


def get_asset(asset_id: str) -> StoredAsset | None:
    row = query_one("SELECT * FROM asset WHERE id = ?", (asset_id,))
    return _row_to_asset(row) if row else None


def get_asset_by_sha(sha: str) -> StoredAsset | None:
    row = query_one("SELECT * FROM asset WHERE sha256 = ? ORDER BY created_at ASC LIMIT 1",
                    (sha,))
    return _row_to_asset(row) if row else None


def _row_to_asset(row) -> StoredAsset:
    return StoredAsset(
        id=row["id"], kind=row["kind"], path=row["path"],
        format=row["format"], width=row["width"], height=row["height"],
        bytes=row["bytes"], sha256=row["sha256"] or "",
        origin=row["origin"], created_at=row["created_at"])


def asset_path(asset: StoredAsset) -> Path:
    return settings.absolute(asset.path)


def list_assets(*, kind: str | None = None, limit: int = 100,
                cursor: float | None = None, tag: str | None = None,
                q: str | None = None) -> tuple[list[StoredAsset], float | None]:
    """分页列出资源。返回 (列表, 下一页游标)。"""
    where = []
    params: list = []
    if kind:
        where.append("a.kind = ?")
        params.append(kind)
    if tag:
        where.append("EXISTS (SELECT 1 FROM asset_tag t WHERE t.asset_id = a.id AND t.tag = ?)")
        params.append(tag)
    if q:
        where.append("(a.id LIKE ? OR a.origin LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if cursor is not None:
        where.append("a.created_at < ?")
        params.append(cursor)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query(
        f"SELECT a.* FROM asset a {clause} ORDER BY a.created_at DESC LIMIT ?",
        params + [limit + 1])
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = rows[-1]["created_at"] if rows else None
    return [_row_to_asset(r) for r in rows], next_cursor


def delete_asset(asset_id: str) -> bool:
    asset = get_asset(asset_id)
    if not asset:
        return False
    path = asset_path(asset)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    execute("DELETE FROM asset WHERE id = ?", (asset_id,))
    return True


def add_tags(asset_id: str, tags: list[str]) -> None:
    for t in tags:
        t = (t or "").strip()
        if t:
            execute("INSERT OR IGNORE INTO asset_tag (asset_id, tag) VALUES (?,?)",
                    (asset_id, t))


def remove_tag(asset_id: str, tag: str) -> None:
    execute("DELETE FROM asset_tag WHERE asset_id = ? AND tag = ?", (asset_id, tag))


def list_tags() -> list[dict]:
    rows = query("SELECT tag, COUNT(*) AS n FROM asset_tag GROUP BY tag ORDER BY n DESC")
    return [{"tag": r["tag"], "count": r["n"]} for r in rows]


# ------------------------------------------------------------------ 缩略图


def make_thumbnail(asset: StoredAsset, width: int = 320) -> str | None:
    """生成 WebP 缩略图，返回相对路径。"""
    src = asset_path(asset)
    if not src.exists():
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
    out = settings.thumbs_dir / f"{asset.id}.webp"
    try:
        with Image.open(src) as im:
            im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
            ratio = width / max(1, im.width)
            h = max(1, int(im.height * ratio))
            im.thumbnail((width, h))
            im.save(out, "WEBP", quality=82)
    except Exception:
        return None
    return settings.relative_to_root(out)


# ------------------------------------------------------------------ 清理


def cleanup_orphans() -> int:
    """删除没有数据库记录的文件。返回清理数量。"""
    removed = 0
    known = {settings.absolute(r["path"]).resolve()
             for r in query("SELECT path FROM asset")}
    for directory in (settings.input_dir, settings.output_dir):
        if not directory.exists():
            continue
        for f in directory.iterdir():
            if not f.is_file():
                continue
            if f.resolve() in known:
                continue
            # 缩略图在 thumbs 目录，不参与
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def stats() -> dict:
    rows = query("SELECT kind, COUNT(*) AS n, SUM(bytes) AS total FROM asset GROUP BY kind")
    return {r["kind"]: {"count": r["n"], "bytes": r["total"] or 0} for r in rows}
