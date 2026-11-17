"""画布服务：节点、连线、工程文件持久化。

设计（spec/04-data-model.md §2.8）：
- 数据库存**索引**（列表、检索、缩略图）；
- 完整画布内容存**工程文件** <data_root>/canvases/<id>.bflow.json；
- 保存时整份 JSON 原子替换（写临时文件 → os.replace），数据库索引最后更新。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import execute, query, query_one

CANVAS_SUFFIX = ".bflow.json"


def new_id(prefix: str = "cv") -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _canvas_path(canvas_id: str) -> Path:
    settings.canvases_dir.mkdir(parents=True, exist_ok=True)
    return settings.canvases_dir / f"{canvas_id}{CANVAS_SUFFIX}"


def list_canvases() -> list[dict]:
    rows = query("SELECT * FROM canvas ORDER BY updated_at DESC")
    return [_row_to_dict(r) for r in rows]


def get_canvas(canvas_id: str) -> dict | None:
    row = query_one("SELECT * FROM canvas WHERE id = ?", (canvas_id,))
    if not row:
        return None
    data = _read_file(row["path"])
    meta = _row_to_dict(row)
    meta["nodes"] = data.get("nodes", [])
    meta["edges"] = data.get("edges", [])
    meta["meta"] = data.get("meta", {})
    return meta


def _read_file(rel_path: str) -> dict:
    path = settings.absolute(rel_path)
    if not path.exists():
        return {"nodes": [], "edges": [], "meta": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"nodes": [], "edges": [], "meta": {}}


def _write_file(canvas_id: str, data: dict) -> str:
    """原子写入工程文件。"""
    path = _canvas_path(canvas_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return settings.relative_to_root(path)


def create_canvas(title: str = "未命名画布") -> dict:
    cid = new_id()
    now = time.time()
    data = {"nodes": [], "edges": [], "meta": {"version": 1, "created_at": now}}
    rel = _write_file(cid, data)
    execute(
        "INSERT INTO canvas (id, title, path, thumbnail_id, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)", (cid, title, rel, None, now, now))
    return get_canvas(cid)  # type: ignore[return-value]


def update_canvas(canvas_id: str, payload: dict) -> dict | None:
    """整体保存画布（节点 + 连线 + 元信息）。"""
    row = query_one("SELECT * FROM canvas WHERE id = ?", (canvas_id,))
    if not row:
        return None

    now = time.time()
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    meta = payload.get("meta") or {}
    title = payload.get("title") or row["title"]

    data = {"nodes": nodes, "edges": edges,
            "meta": {**meta, "updated_at": now}}
    rel = _write_file(canvas_id, data)

    # 数据库索引重建
    execute("DELETE FROM canvas_node WHERE canvas_id = ?", (canvas_id,))
    execute("DELETE FROM canvas_edge WHERE canvas_id = ?", (canvas_id,))

    # 先落节点，收集有效节点 id；悬空连线直接丢弃（不因为一条坏边导致整次保存失败）
    valid_node_ids: set[str] = set()
    for idx, node in enumerate(nodes):
        nid = node.get("id") or new_id("nd")
        valid_node_ids.add(nid)
        execute(
            "INSERT OR REPLACE INTO canvas_node (id, canvas_id, type, x, y, width,"
            " height, z_index, data_json, asset_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, canvas_id, node.get("type") or "image",
             float(node.get("x") or 0), float(node.get("y") or 0),
             float(node.get("width") or 320), float(node.get("height") or 320),
             int(node.get("z_index") or idx),
             json.dumps(node.get("data") or {}, ensure_ascii=False),
             node.get("asset_id"), now, now),
        )
    for edge in edges:
        source = edge.get("source_id") or ""
        target = edge.get("target_id") or ""
        if source not in valid_node_ids or target not in valid_node_ids:
            continue          # 悬空连线：丢弃
        eid = edge.get("id") or new_id("eg")
        execute(
            "INSERT OR REPLACE INTO canvas_edge (id, canvas_id, source_id, target_id,"
            " label, data_json) VALUES (?,?,?,?,?,?)",
            (eid, canvas_id, source, target, edge.get("label"),
             json.dumps(edge.get("data") or {}, ensure_ascii=False)),
        )
    execute("UPDATE canvas SET title = ?, path = ?, updated_at = ? WHERE id = ?",
            (title, rel, now, canvas_id))
    return get_canvas(canvas_id)


def rename_canvas(canvas_id: str, title: str) -> dict | None:
    row = query_one("SELECT * FROM canvas WHERE id = ?", (canvas_id,))
    if not row:
        return None
    execute("UPDATE canvas SET title = ?, updated_at = ? WHERE id = ?",
            (title, time.time(), canvas_id))
    return get_canvas(canvas_id)


def delete_canvas(canvas_id: str) -> bool:
    row = query_one("SELECT * FROM canvas WHERE id = ?", (canvas_id,))
    if not row:
        return False
    path = settings.absolute(row["path"])
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    execute("DELETE FROM canvas WHERE id = ?", (canvas_id,))
    return True


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "thumbnail_id": row["thumbnail_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def export_canvas(canvas_id: str) -> dict:
    """导出画布内容（不含二进制资源）。"""
    canvas = get_canvas(canvas_id)
    if not canvas:
        return {}
    return {
        "id": canvas["id"],
        "title": canvas["title"],
        "nodes": canvas["nodes"],
        "edges": canvas["edges"],
        "meta": canvas["meta"],
    }
