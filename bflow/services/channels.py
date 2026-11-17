"""渠道服务：增删改查、能力表管理、凭据处理。

能力表的核心设计（spec/04-data-model.md §2.3）：
- 与渠道配置**分表**，因为来源、生命周期、更新频率都不同；
- `source` 区分 probe（实测）/ manual（人工覆盖）/ default（兜底）；
- `verified_at` 让前端能提示「能力数据已过期」。

实测背景：渠道能力会在数小时内变化（CH-D 的分组通道从有变无），
因此能力表必须**可被用户覆盖**，且不覆盖实测值的读取路径要清晰。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..db import execute, execute_many, query, query_one, transaction
from .credentials import decrypt, encrypt, mask
from .protocol import registry

# 兜底能力表：用户未验证渠道时使用，标注 source=default。
# 具体内容由协议决定（见 protocol/registry.py）—— OpenAI 协议能力最全，
# 工作流类协议只能声明「格式由工作流决定」。
DEFAULT_CAPABILITY: dict[str, Any] = registry.default_capability("openai-images")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ------------------------------------------------------------------ 凭据


def _put_credential(secret: str, kind: str = "bearer",
                    extra: dict | None = None) -> str:
    cid = new_id("cred")
    now = time.time()
    execute(
        "INSERT INTO api_credential (id, kind, secret_enc, extra_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (cid, kind, encrypt(secret), json.dumps(extra or {}, ensure_ascii=False), now, now),
    )
    return cid


def get_secret(channel_row) -> str:
    """取渠道的明文密钥（仅内部使用，接口层不暴露）。"""
    cred_id = channel_row["credential_id"] if channel_row else None
    if not cred_id:
        return ""
    row = query_one("SELECT secret_enc FROM api_credential WHERE id = ?", (cred_id,))
    if not row:
        return ""
    try:
        return decrypt(row["secret_enc"])
    except Exception:
        return ""


def _delete_credential(cred_id: str | None) -> None:
    if cred_id:
        execute("DELETE FROM api_credential WHERE id = ?", (cred_id,))


# ------------------------------------------------------------------ 渠道


def list_channels() -> list[dict]:
    rows = query("SELECT * FROM channel ORDER BY created_at ASC")
    return [_channel_to_dict(r) for r in rows]


def get_channel(channel_id: str) -> dict | None:
    row = query_one("SELECT * FROM channel WHERE id = ?", (channel_id,))
    return _channel_to_dict(row) if row else None


def create_channel(data: dict) -> dict:
    cid = data.get("id") or new_id("ch")
    now = time.time()
    secret = (data.get("api_key") or "").strip()
    cred_id = _put_credential(secret) if secret else None
    protocol = data.get("protocol") or "openai-images"
    execute(
        "INSERT INTO channel (id, display_name, protocol, base_url, enabled,"
        " config_json, credential_id, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (cid,
         data.get("display_name") or cid,
         protocol,
         (data.get("base_url") or "").rstrip("/"),
         1 if data.get("enabled", True) else 0,
         json.dumps(data.get("config") or {}, ensure_ascii=False),
         cred_id, now, now),
    )
    # 兜底能力表：按协议给不同的默认值
    fallback = registry.default_capability(protocol)
    for model in (data.get("models") or []):
        set_capability(cid, model, fallback, source="default")
    return get_channel(cid)  # type: ignore[return-value]


def update_channel(channel_id: str, patch: dict) -> dict | None:
    current = query_one("SELECT * FROM channel WHERE id = ?", (channel_id,))
    if not current:
        return None

    fields: list[str] = []
    values: list[Any] = []

    if "display_name" in patch and patch["display_name"]:
        fields.append("display_name = ?")
        values.append(patch["display_name"])
    if "protocol" in patch and patch["protocol"]:
        fields.append("protocol = ?")
        values.append(patch["protocol"])
    if "base_url" in patch and patch["base_url"]:
        fields.append("base_url = ?")
        values.append(patch["base_url"].rstrip("/"))
    if "enabled" in patch:
        fields.append("enabled = ?")
        values.append(1 if patch["enabled"] else 0)
    if "config" in patch:
        fields.append("config_json = ?")
        values.append(json.dumps(patch["config"] or {}, ensure_ascii=False))

    secret = (patch.get("api_key") or "").strip()
    if secret:
        # 换密钥：删旧凭据，建新凭据
        _delete_credential(current["credential_id"])
        fields.append("credential_id = ?")
        values.append(_put_credential(secret))

    fields.append("updated_at = ?")
    values.append(time.time())
    values.append(channel_id)
    execute(f"UPDATE channel SET {', '.join(fields)} WHERE id = ?", values)
    return get_channel(channel_id)


def delete_channel(channel_id: str) -> bool:
    row = query_one("SELECT * FROM channel WHERE id = ?", (channel_id,))
    if not row:
        return False
    _delete_credential(row["credential_id"])
    execute("DELETE FROM channel WHERE id = ?", (channel_id,))
    return True


def _channel_to_dict(row) -> dict:
    cfg = {}
    try:
        cfg = json.loads(row["config_json"] or "{}")
    except Exception:
        cfg = {}
    secret = get_secret(row)
    protocol = row["protocol"]
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "protocol": protocol,
        "protocol_label": registry.label(protocol),
        "protocol_known": registry.is_known(protocol),
        "base_url": row["base_url"],
        "enabled": bool(row["enabled"]),
        "config": cfg,
        "api_key": mask(secret),
        "has_credential": bool(secret),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ------------------------------------------------------------------ 能力表


def set_capability(channel_id: str, model: str, capability: dict,
                   source: str = "manual", verified_at: float | None = None) -> None:
    now = time.time()
    payload = json.dumps(capability, ensure_ascii=False)
    existing = query_one(
        "SELECT channel_id FROM channel_capability WHERE channel_id = ? AND model = ?",
        (channel_id, model))
    if existing:
        execute(
            "UPDATE channel_capability SET capability_json = ?, source = ?,"
            " verified_at = ? WHERE channel_id = ? AND model = ?",
            (payload, source, verified_at, channel_id, model))
    else:
        execute(
            "INSERT INTO channel_capability (channel_id, model, capability_json,"
            " source, verified_at) VALUES (?,?,?,?,?)",
            (channel_id, model, payload, source, verified_at or (now if source == "probe" else None)))


def get_capability(channel_id: str, model: str) -> dict:
    """取能力：优先该模型的能力表，没有则取该渠道任意一条，再没有用兜底。

    兜底表按**渠道协议**给 —— 工作流类协议的默认能力与 OpenAI 协议不同。
    """
    row = query_one(
        "SELECT * FROM channel_capability WHERE channel_id = ? AND model = ?",
        (channel_id, model))
    if not row:
        row = query_one(
            "SELECT * FROM channel_capability WHERE channel_id = ? LIMIT 1",
            (channel_id,))
    if not row:
        ch = query_one("SELECT protocol FROM channel WHERE id = ?", (channel_id,))
        protocol = ch["protocol"] if ch else "openai-images"
        return registry.default_capability(protocol)

    try:
        cap = json.loads(row["capability_json"])
    except Exception:
        cap = {}
    # 兜底补齐缺失字段，避免旧数据导致前端崩
    ch = query_one("SELECT protocol FROM channel WHERE id = ?", (channel_id,))
    protocol = ch["protocol"] if ch else "openai-images"
    for key, value in registry.default_capability(protocol).items():
        if key in ("source", "verified_at"):
            continue
        cap.setdefault(key, value)
    cap["source"] = row["source"]
    cap["verified_at"] = row["verified_at"]
    return cap


def list_capabilities(channel_id: str) -> list[dict]:
    rows = query("SELECT * FROM channel_capability WHERE channel_id = ?", (channel_id,))
    out = []
    for r in rows:
        try:
            cap = json.loads(r["capability_json"])
        except Exception:
            cap = {}
        out.append({"model": r["model"], "capability": cap,
                    "source": r["source"], "verified_at": r["verified_at"]})
    return out


def models_of(channel_id: str) -> list[str]:
    rows = query("SELECT model FROM channel_capability WHERE channel_id = ?", (channel_id,))
    return [r["model"] for r in rows]


def channel_detail(channel_id: str) -> dict | None:
    """渠道详情：含能力表与可信度信息。"""
    ch = get_channel(channel_id)
    if not ch:
        return None
    caps = list_capabilities(channel_id)
    ch["capabilities"] = caps
    ch["models"] = [c["model"] for c in caps]
    # 可信度：最近一次实测时间
    verified = [c["verified_at"] for c in caps if c["verified_at"]]
    ch["last_verified_at"] = max(verified) if verified else None
    ch["verified"] = bool(verified)
    return ch


def capability_for(channel_id: str, model: str) -> dict:
    """生图时用：取该渠道该模型的能力。"""
    return get_capability(channel_id, model)
