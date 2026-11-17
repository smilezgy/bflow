"""渠道服务：增删改查、能力表管理、凭据处理、模型协议绑定。

能力表的核心设计（spec/04-data-model.md §2.3）：
- 与渠道配置**分表**，因为来源、生命周期、更新频率都不同；
- `source` 区分 probe（实测）/ manual（人工覆盖）/ default（兜底）；
- `verified_at` 让前端能提示「能力数据已过期」。

实测背景：渠道能力会在数小时内变化（CH-D 的分组通道从有变无），
因此能力表必须**可被用户覆盖**，且不覆盖实测值的读取路径要清晰。

模型协议绑定（spec/04-data-model.md §2.4）：
一个渠道 = 一个接入点（base_url + 凭据）。聚合网关（newapi / sub2api /
openrouter 之类）的模型列表会混着多个厂商 —— gpt-image-* 走 OpenAI 协议、
gemini-*-image 走 Gemini 协议、doubao-seedream-* 走方舟协议。因此协议
从渠道级下沉到模型级：

- `channel.protocol`      → **默认协议**，模型未单独指定时的兜底；
- `channel_model.protocol` → 该模型实际使用的协议。
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
        model = (model or "").strip()
        if not model:
            continue
        # 登记模型 → 协议绑定（模型未指定协议时继承渠道默认协议）
        set_model_binding(cid, model, protocol)
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
    # 空模型名会污染能力表（前端会渲染出一个空模型选项），直接拒绝。
    model = (model or "").strip()
    if not model:
        return
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


def _merge_defaults(channel_id: str, cap: dict, model: str = "") -> dict:
    """用协议默认能力补齐缺失字段。

    缺字段不能丢 —— 前端按 `output_formats` 渲染格式下拉、按 `quality_levels`
    渲染档位，缺了就会退化成"只显示 PNG"或只显示 auto。

    默认能力按**该模型的实际协议**给（多协议渠道下，不同模型的默认能力不同）：
    工作流类协议只能声明"格式由工作流决定"，而 OpenAI 协议能力最全。
    """
    protocol = protocol_for(channel_id, model) if model else _channel_protocol(channel_id)
    merged = dict(cap) if isinstance(cap, dict) else {}
    for key, value in registry.default_capability(protocol).items():
        if key in ("source", "verified_at"):
            continue
        merged.setdefault(key, value)
    return merged


def _channel_protocol(channel_id: str) -> str:
    ch = query_one("SELECT protocol FROM channel WHERE id = ?", (channel_id,))
    return ch["protocol"] if ch else "openai-images"


def get_capability(channel_id: str, model: str) -> dict:
    """取能力：优先该模型的能力表，没有则取同协议的其他模型，再没有用兜底。

    兜底表按**该模型实际使用的协议**给 —— 多协议渠道下不同模型的默认能力
    不同，工作流类协议与 OpenAI 协议不能混用。

    注意：跨模型继承能力表时**必须同协议**。否则 Gemini 模型会继承到
    OpenAI 模型的实测结论（比如"支持 svg"），前置校验就会放行一个
    该协议根本不接受的格式。
    """
    want_protocol = protocol_for(channel_id, model)
    row = query_one(
        "SELECT * FROM channel_capability WHERE channel_id = ? AND model = ?",
        (channel_id, model))
    if not row:
        # 同协议的其他模型的能力表才可继承
        for other in list_models(channel_id):
            if other["model"] == model or other["protocol"] != want_protocol:
                continue
            row = query_one(
                "SELECT * FROM channel_capability WHERE channel_id = ? AND model = ?",
                (channel_id, other["model"]))
            if row:
                break
    if not row:
        return registry.default_capability(want_protocol)

    try:
        cap = json.loads(row["capability_json"])
    except Exception:
        cap = {}
    # 兜底补齐缺失字段，避免旧数据导致前端崩
    cap = _merge_defaults(channel_id, cap, model)
    cap["source"] = row["source"]
    cap["verified_at"] = row["verified_at"]
    return cap


def list_capabilities(channel_id: str) -> list[dict]:
    """列出该渠道所有模型的能力表。

    每条都要补齐协议默认能力 —— 前端按 `output_formats` 渲染格式下拉，
    若返回裸 JSON（历史上手工存过空表），前端会退化成"只显示 PNG"。

    覆盖**注册表里的全部模型**：新绑定的模型还没有能力表记录，但前端
    仍需要按它的协议渲染可用选项（Gemini 只有 PNG/JPEG、尺寸固定）。
    """
    rows = query("SELECT * FROM channel_capability WHERE channel_id = ?", (channel_id,))
    out = []
    seen: set[str] = set()

    for r in rows:
        model = (r["model"] or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            cap = json.loads(r["capability_json"])
        except Exception:
            cap = {}
        if not isinstance(cap, dict):
            cap = {}
        merged = _merge_defaults(channel_id, cap, model)
        merged["source"] = r["source"]
        merged["verified_at"] = r["verified_at"]
        out.append({"model": model, "capability": merged,
                    "source": r["source"], "verified_at": r["verified_at"]})

    # 注册表里有、但还没建能力表的模型 —— 按其协议给默认能力
    for binding in list_models(channel_id):
        model = binding["model"]
        if model in seen:
            continue
        seen.add(model)
        merged = registry.default_capability(binding["protocol"])
        merged["source"] = "default"
        merged["verified_at"] = None
        out.append({"model": model, "capability": merged,
                    "source": "default", "verified_at": None})

    out.sort(key=lambda x: x["model"])
    return out


def models_of(channel_id: str) -> list[str]:
    rows = query("SELECT model FROM channel_capability WHERE channel_id = ?", (channel_id,))
    return [r["model"] for r in rows if (r["model"] or "").strip()]


# ------------------------------------------------------------------ 模型绑定
#
# channel_model 是**模型注册表**：一个渠道有哪些模型、各自用哪个协议。
# channel_capability 仍按 (channel_id, model) 存能力表 —— 同一模型在同一
# 渠道下只有一个生效协议，所以能力表不会歧义。协议变更时重置该模型的能力表，
# 否则旧协议下实测出的结论会被误用到新协议上。


def list_models(channel_id: str) -> list[dict]:
    """列出该渠道登记的模型及其协议绑定。"""
    rows = query(
        "SELECT * FROM channel_model WHERE channel_id = ? ORDER BY model", (channel_id,))
    out = []
    for r in rows:
        try:
            cfg = json.loads(r["config_json"] or "{}")
        except Exception:
            cfg = {}
        out.append({
            "model": r["model"],
            "protocol": r["protocol"],
            "protocol_label": registry.label(r["protocol"]),
            "enabled": bool(r["enabled"]),
            "config": cfg if isinstance(cfg, dict) else {},
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return out


def get_model_binding(channel_id: str, model: str) -> dict | None:
    row = query_one(
        "SELECT * FROM channel_model WHERE channel_id = ? AND model = ?",
        (channel_id, model))
    if not row:
        return None
    try:
        cfg = json.loads(row["config_json"] or "{}")
    except Exception:
        cfg = {}
    return {
        "model": row["model"],
        "protocol": row["protocol"],
        "enabled": bool(row["enabled"]),
        "config": cfg if isinstance(cfg, dict) else {},
    }


def protocol_for(channel_id: str, model: str) -> str:
    """该渠道该模型实际使用的协议。

    优先级：模型绑定 > 渠道默认协议 > openai-images。
    这是多协议渠道的核心：同一个接入点下，不同模型走不同协议。
    """
    binding = get_model_binding(channel_id, model)
    if binding and binding.get("protocol"):
        return binding["protocol"]
    ch = query_one("SELECT protocol FROM channel WHERE id = ?", (channel_id,))
    if ch and ch["protocol"]:
        return ch["protocol"]
    return "openai-images"


def model_config_for(channel_id: str, model: str) -> dict:
    """模型级配置：与渠道级配置合并（模型级覆盖渠道级）。"""
    ch = query_one("SELECT config_json FROM channel WHERE id = ?", (channel_id,))
    merged: dict[str, Any] = {}
    if ch:
        try:
            base = json.loads(ch["config_json"] or "{}")
            if isinstance(base, dict):
                merged.update(base)
        except Exception:
            pass
    binding = get_model_binding(channel_id, model)
    if binding:
        merged.update(binding.get("config") or {})
    return merged


def set_model_binding(channel_id: str, model: str, protocol: str | None = None,
                      *, enabled: bool | None = None,
                      config: dict | None = None) -> dict:
    """登记或更新一个模型的协议绑定。

    协议变更会**重置该模型的能力表** —— 旧协议下实测出的结论
    （支持哪些格式、尺寸策略、能否编辑）对新协议不成立，留着会误导
    前置校验（spec/03 §2.3）。
    """
    model = (model or "").strip()
    if not model:
        raise ValueError("模型名不能为空")

    existing = get_model_binding(channel_id, model)
    now = time.time()

    old_protocol = existing["protocol"] if existing else None
    new_protocol = (protocol or old_protocol or "").strip()
    if not new_protocol:
        ch = query_one("SELECT protocol FROM channel WHERE id = ?", (channel_id,))
        new_protocol = ch["protocol"] if ch else "openai-images"

    if not registry.is_known(new_protocol):
        raise ValueError(f"未知协议：{new_protocol}")

    if enabled is None:
        enabled = existing["enabled"] if existing else True

    if config is None:
        cfg = existing["config"] if existing else {}
    else:
        cfg = config or {}

    if existing:
        execute(
            "UPDATE channel_model SET protocol = ?, enabled = ?, config_json = ?,"
            " updated_at = ? WHERE channel_id = ? AND model = ?",
            (new_protocol, 1 if enabled else 0,
             json.dumps(cfg, ensure_ascii=False), now, channel_id, model))
    else:
        execute(
            "INSERT INTO channel_model (channel_id, model, protocol, enabled,"
            " config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (channel_id, model, new_protocol, 1 if enabled else 0,
             json.dumps(cfg, ensure_ascii=False), now, now))

    # 协议变了 → 该模型的能力表作废，重新按新协议给默认值
    if old_protocol and old_protocol != new_protocol:
        set_capability(channel_id, model,
                       registry.default_capability(new_protocol),
                       source="default")

    return get_model_binding(channel_id, model) or {}


def delete_model_binding(channel_id: str, model: str) -> bool:
    """移除一个模型（同时清掉它的能力表）。"""
    if not get_model_binding(channel_id, model):
        return False
    execute("DELETE FROM channel_model WHERE channel_id = ? AND model = ?",
            (channel_id, model))
    execute("DELETE FROM channel_capability WHERE channel_id = ? AND model = ?",
            (channel_id, model))
    return True


def channel_detail(channel_id: str) -> dict | None:
    """渠道详情：含能力表、模型绑定与可信度信息。"""
    ch = get_channel(channel_id)
    if not ch:
        return None
    caps = list_capabilities(channel_id)
    ch["capabilities"] = caps
    ch["models"] = [c["model"] for c in caps if (c.get("model") or "").strip()]

    # 模型注册表（含协议绑定）—— 模型清单以它为准
    bindings = list_models(channel_id)
    ch["model_bindings"] = bindings
    ch["registered_models"] = [b["model"] for b in bindings]
    # 渠道级 models 字段取并集：注册表有就优先，否则回落到能力表
    merged = list(ch["registered_models"])
    for m in ch["models"]:
        if m not in merged:
            merged.append(m)
    ch["models"] = merged
    ch["protocols_in_use"] = sorted({b["protocol"] for b in bindings})

    # 可信度：最近一次实测时间
    verified = [c["verified_at"] for c in caps if c["verified_at"]]
    ch["last_verified_at"] = max(verified) if verified else None
    ch["verified"] = bool(verified)
    return ch


def capability_for(channel_id: str, model: str) -> dict:
    """生图时用：取该渠道该模型的能力（默认值按该模型的实际协议给）。"""
    return get_capability(channel_id, model)
