"""Bflow API 路由。

契约见 spec/03-api-contract.md。
所有响应走统一包络（ok / data / error / meta）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..config import APP_VERSION, settings
from ..db import query, query_one
from ..services import assets as assets_svc
from ..services import canvas as canvas_svc
from ..services import channels as channels_svc
from ..services import generation as gen_svc
from ..services.protocol.imaging import format_label
from .models import (AssetTagUpdate, CanvasCreate, CanvasRename, CanvasSave,
                     CapabilityUpdate, ChannelCreate, ChannelPatch, GenerateRequest,
                     ModelBindingUpdate, ModelFetchRequest, StorageSettings)
from .response import ApiError, fail, ok

router = APIRouter(prefix="/api")


# ================================================================== 渠道


@router.get("/channels")
async def list_channels():
    items = []
    for ch in channels_svc.list_channels():
        detail = channels_svc.channel_detail(ch["id"])
        if not detail:
            continue
        items.append(detail)
    return ok({"channels": items})


@router.post("/channels")
async def create_channel(body: ChannelCreate):
    if not body.base_url:
        raise ApiError("INVALID_REQUEST", "渠道地址不能为空。")
    ch = channels_svc.create_channel(body.model_dump())
    return ok(ch)


@router.patch("/channels/{channel_id}")
async def patch_channel(channel_id: str, body: ChannelPatch):
    patch = body.model_dump(exclude_none=True)
    capabilities = patch.pop("capabilities", None)
    capability_model = patch.pop("capability_model", "")

    ch = channels_svc.update_channel(channel_id, patch)
    if not ch:
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)

    if capabilities is not None:
        model = capability_model or (channels_svc.models_of(channel_id) or [""])[0]
        if not model:
            raise ApiError("INVALID_REQUEST", "请指定能力对应的模型。")
        channels_svc.set_capability(channel_id, model, capabilities, source="manual")

    return ok(channels_svc.channel_detail(channel_id))


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    if not channels_svc.delete_channel(channel_id):
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)
    return ok({"id": channel_id})


@router.put("/channels/{channel_id}/capability")
async def put_capability(channel_id: str, body: CapabilityUpdate):
    """人工覆盖能力表（实测见 spec/02 §「能力表覆盖」）。"""
    if not channels_svc.get_channel(channel_id):
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)
    channels_svc.set_capability(channel_id, body.model, body.capability, source="manual")
    return ok(channels_svc.channel_detail(channel_id))


# ================================================================== 模型绑定
#
# 一个渠道 = 一个接入点。聚合网关（newapi / sub2api / openrouter）的模型
# 列表混着多个厂商，各自协议不同 —— 所以协议绑在**模型**上，不是渠道上。


@router.get("/channels/{channel_id}/models")
async def list_channel_models(channel_id: str):
    """列出该渠道登记的模型及其协议绑定。"""
    if not channels_svc.get_channel(channel_id):
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)
    bindings = channels_svc.list_models(channel_id)
    return ok({
        "models": bindings,
        "default_protocol": channels_svc.get_channel(channel_id).get("protocol"),
    })


@router.put("/channels/{channel_id}/models")
async def put_channel_models(channel_id: str, body: ModelBindingUpdate):
    """批量设置模型的协议绑定。

    协议变更会重置该模型的能力表 —— 旧协议下实测出的结论对新协议不成立。
    """
    if not channels_svc.get_channel(channel_id):
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)
    for item in body.models:
        model = (item.model or "").strip()
        if not model:
            continue
        try:
            channels_svc.set_model_binding(
                channel_id, model, item.protocol or None,
                enabled=item.enabled, config=item.config or None)
        except ValueError as exc:
            raise ApiError("INVALID_REQUEST", str(exc))
    return ok({
        "models": channels_svc.list_models(channel_id),
        "channel": channels_svc.channel_detail(channel_id),
    })


@router.delete("/channels/{channel_id}/models/{model}")
async def delete_channel_model(channel_id: str, model: str):
    """移除一个模型绑定。"""
    if not channels_svc.get_channel(channel_id):
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)
    if not channels_svc.delete_model_binding(channel_id, model):
        raise ApiError("NOT_FOUND", "该渠道没有登记这个模型。", status=404)
    return ok({"id": channel_id, "model": model})


@router.post("/channels/{channel_id}/models/fetch")
async def fetch_channel_models(channel_id: str, body: ModelFetchRequest):
    """从上游拉取模型列表。

    聚合网关的模型目录是 OpenAI 兼容的（`GET /v1/models`），因此即使
    该网关下的模型分别走不同协议，拉目录这一步用哪个协议都能拿到全量列表。

    只列目录，不触发生图 —— 不产生生成费用。
    """
    ch = channels_svc.get_channel(channel_id)
    if not ch:
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)

    from ..services.protocol import registry

    protocol = body.protocol or ch.get("protocol") or "openai-images"

    secret = channels_svc.get_secret(
        query_one("SELECT * FROM channel WHERE id = ?", (channel_id,)))

    listing = await registry.list_upstream_models(
        protocol, ch["base_url"], secret,
        extra_headers=(ch.get("config") or {}).get("headers") or {},
        config=ch.get("config") or {})

    if not listing.ok:
        return fail("CHANNEL_UNAVAILABLE", listing.error or "拉取模型列表失败。",
                    status=502, hint=listing.hint or "",
                    retryable=True, detail={"raw": listing.raw_body[:300]})

    default_protocol = ch.get("protocol") or "openai-images"
    items = []
    for name in listing.models:
        suggested = (registry.suggest_protocol(name, default_protocol)
                     if body.auto_protocol else default_protocol)
        items.append({
            "model": name,
            "suggested_protocol": suggested,
            "suggested_protocol_label": registry.label(suggested),
        })

    # 直接登记：拉到的模型全部加上协议绑定
    if body.apply:
        for item in items:
            try:
                channels_svc.set_model_binding(
                    channel_id, item["model"], item["suggested_protocol"])
            except ValueError:
                continue

    return ok({
        "models": items,
        "count": len(items),
        "protocol_used": protocol,
        "applied": bool(body.apply),
        "channel": channels_svc.channel_detail(channel_id),
    })


@router.post("/channels/{channel_id}/verify")
async def verify_channel(channel_id: str):
    """触发一次实测以刷新能力表。

    会产生真实请求与费用，因此前端必须二次确认。

    验证用的是**该模型实际绑定的协议** —— 多协议渠道下，不同模型要走
    不同的协议，验证错协议会得到假结论。
    """
    ch = channels_svc.get_channel(channel_id)
    if not ch:
        raise ApiError("NOT_FOUND", "渠道不存在。", status=404)
    models = channels_svc.models_of(channel_id)
    if not models:
        models = [m["model"] for m in channels_svc.list_models(channel_id)]
    if not models:
        raise ApiError("INVALID_REQUEST", "该渠道尚无模型，请先添加。")
    model = models[0]

    secret = channels_svc.get_secret(
        query_one("SELECT * FROM channel WHERE id = ?", (channel_id,)))
    from ..services.protocol import registry

    protocol = channels_svc.protocol_for(channel_id, model)
    req = {"model": model, "prompt": "a flat vector icon of a red apple, simple shapes",
           "size": "1024x1024", "output_format": "png"}
    result = await registry.dispatch(
        protocol, ch["base_url"], secret, req,
        config=channels_svc.model_config_for(channel_id, model))

    if not result.ok:
        err = result.error
        return fail(err.code if err else "CHANNEL_INTERNAL_ERROR",
                    err.message if err else "验证失败。",
                    status=502,
                    hint=err.hint if err else "",
                    retryable=bool(err.retryable) if err else False,
                    detail={"raw": (err.raw or "")[:300]} if err else {})

    capability = channels_svc.capability_for(channel_id, model)
    capability = _merge_probe_result(capability, req, result)
    channels_svc.set_capability(channel_id, model, capability, source="probe",
                                verified_at=time.time())
    return ok(channels_svc.channel_detail(channel_id))


def _merge_probe_result(capability: dict, req: dict, result) -> dict:
    """把一次实测结果并入能力表。"""
    cap = dict(capability)
    formats = dict(cap.get("output_formats") or {})
    if result.artifacts:
        actual = result.artifacts[0].info.format
        fmt = (req.get("output_format") or "").lower()
        if fmt and actual:
            formats[fmt] = {"supported": True, "honors_request": actual == fmt}
    cap["output_formats"] = formats
    return cap


# ================================================================== 生图


@router.post("/images/generate")
async def generate_image(body: GenerateRequest):
    if not body.prompt.strip():
        raise ApiError("PROMPT_REQUIRED", "提示词不能为空。")

    req = gen_svc.GenerationRequest(
        prompt=body.prompt.strip(),
        channel_id=body.channel_id,
        model=body.model,
        size=body.size,
        count=body.count,
        output_format=body.output_format,
        output_compression=body.output_compression,
        background=body.background,
        quality=body.quality,
        complexity=body.complexity,
        frame_count=body.frame_count,
        frame_duration_ms=body.frame_duration_ms,
        reference_asset_ids=body.reference_images,
        mask_asset_ids=body.masks,
    )

    outcome = await gen_svc.generate(req)
    task_id = gen_svc.record_task(req, outcome)

    if not outcome.ok:
        err = outcome.error or {}
        return fail(err.get("code", "CHANNEL_INTERNAL_ERROR"),
                    err.get("message", "生成失败。"),
                    status=400 if err.get("code") not in
                    ("CHANNEL_UNAVAILABLE", "CHANNEL_INTERNAL_ERROR",
                     "UPSTREAM_TIMEOUT", "RATE_LIMITED") else 503,
                    hint=err.get("hint", ""),
                    retryable=err.get("retryable", False),
                    detail={"task_id": task_id, **(err.get("detail") or {})})

    return ok({
        "task_id": task_id,
        "images": outcome.images,
        "warnings": outcome.warnings,
    })


# ================================================================== 任务


@router.get("/tasks")
async def list_tasks(limit: int = Query(50, ge=1, le=200),
                     status: str = Query("")):
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query(f"SELECT * FROM task {clause} ORDER BY created_at DESC LIMIT ?",
                 params + [limit])
    items = [_task_to_dict(r) for r in rows]
    return ok({"tasks": items})


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    row = query_one("SELECT * FROM task WHERE id = ?", (task_id,))
    if not row:
        raise ApiError("NOT_FOUND", "任务不存在。", status=404)
    data = _task_to_dict(row)
    assets = query(
        "SELECT a.* FROM task_asset ta JOIN asset a ON a.id = ta.asset_id"
        " WHERE ta.task_id = ? AND ta.role = 'output' ORDER BY ta.position", (task_id,))
    data["images"] = [_asset_to_dict(a) for a in assets]
    return ok(data)


def _task_to_dict(row) -> dict:
    import json
    try:
        warnings = json.loads(row["warnings_json"] or "[]")
    except Exception:
        warnings = []
    try:
        request = json.loads(row["request_json"] or "{}")
    except Exception:
        request = {}
    return {
        "task_id": row["id"],
        "channel_id": row["channel_id"],
        "model": row["model"],
        "operation": row["operation"],
        "status": row["status"],
        "prompt": row["prompt"],
        "requested_size": row["requested_size"],
        "requested_format": row["requested_format"],
        "actual_size": row["actual_size"],
        "actual_format": row["actual_format"],
        "warnings": warnings,
        "error": ({"code": row["error_code"], "message": row["error_message"]}
                  if row["error_code"] else None),
        "billed": bool(row["billed"]),
        "progress": row["progress"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "request": request,
    }


# ================================================================== 资源


@router.post("/assets/upload")
async def upload_assets(files: list[UploadFile] = File(...),
                        purpose: str = Form("reference")):
    stored = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        kind = "input" if purpose in ("reference", "input") else (
            "mask" if purpose == "mask" else "library")
        stored.append(assets_svc.store_bytes(
            data, kind=kind, origin="upload").to_dict())
    if not stored:
        raise ApiError("INVALID_REQUEST", "没有收到有效文件。")
    return ok({"assets": stored})


@router.get("/assets")
async def list_assets(kind: str = Query(""), limit: int = Query(60, ge=1, le=300),
                      cursor: float | None = Query(None), tag: str = Query(""),
                      q: str = Query("")):
    items, next_cursor = assets_svc.list_assets(
        kind=kind or None, limit=limit, cursor=cursor, tag=tag or None, q=q or None)
    return ok({"assets": [_asset_to_dict(a) for a in items],
               "next_cursor": next_cursor})


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str):
    asset = assets_svc.get_asset(asset_id)
    if not asset:
        raise ApiError("NOT_FOUND", "资源不存在。", status=404)
    return ok(_asset_to_dict(asset))


@router.get("/assets/{asset_id}/raw")
async def get_asset_raw(asset_id: str):
    asset = assets_svc.get_asset(asset_id)
    if not asset:
        raise ApiError("NOT_FOUND", "资源不存在。", status=404)
    path = assets_svc.asset_path(asset)
    if not path.exists():
        raise ApiError("NOT_FOUND", "文件已丢失。", status=404)
    return FileResponse(path, media_type="application/octet-stream",
                        filename=path.name)


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str):
    if not assets_svc.delete_asset(asset_id):
        raise ApiError("NOT_FOUND", "资源不存在。", status=404)
    return ok({"id": asset_id})


@router.patch("/assets/{asset_id}/tags")
async def update_tags(asset_id: str, body: AssetTagUpdate):
    asset = assets_svc.get_asset(asset_id)
    if not asset:
        raise ApiError("NOT_FOUND", "资源不存在。", status=404)
    for tag in body.tags:
        assets_svc.add_tags(asset_id, [tag])
    return ok({"id": asset_id, "tags": body.tags})


@router.get("/tags")
async def list_tags():
    return ok({"tags": assets_svc.list_tags()})


def _asset_to_dict(asset) -> dict:
    d = asset.to_dict() if hasattr(asset, "to_dict") else dict(asset)
    d["tags"] = []
    return d


# ================================================================== 画布


@router.get("/canvases")
async def list_canvases():
    return ok({"canvases": canvas_svc.list_canvases()})


@router.post("/canvases")
async def create_canvas(body: CanvasCreate):
    return ok(canvas_svc.create_canvas(body.title))


@router.get("/canvases/{canvas_id}")
async def get_canvas(canvas_id: str):
    data = canvas_svc.get_canvas(canvas_id)
    if not data:
        raise ApiError("NOT_FOUND", "画布不存在。", status=404)
    return ok(data)


@router.put("/canvases/{canvas_id}")
async def save_canvas(canvas_id: str, body: CanvasSave):
    data = canvas_svc.update_canvas(canvas_id, body.model_dump())
    if not data:
        raise ApiError("NOT_FOUND", "画布不存在。", status=404)
    return ok(data)


@router.patch("/canvases/{canvas_id}")
async def rename_canvas(canvas_id: str, body: CanvasRename):
    data = canvas_svc.rename_canvas(canvas_id, body.title)
    if not data:
        raise ApiError("NOT_FOUND", "画布不存在。", status=404)
    return ok(data)


@router.delete("/canvases/{canvas_id}")
async def delete_canvas(canvas_id: str):
    if not canvas_svc.delete_canvas(canvas_id):
        raise ApiError("NOT_FOUND", "画布不存在。", status=404)
    return ok({"id": canvas_id})


# ================================================================== 配置


@router.get("/config")
async def get_config():
    from ..services import app_settings
    return ok({
        "version": APP_VERSION,
        "settings": app_settings.get_all(),
        "data_root": str(settings.data_root),
    })


@router.patch("/config")
async def patch_config(body: dict):
    from ..services import app_settings
    updated = app_settings.update_many(body)
    return ok(updated)


@router.get("/storage/settings")
async def get_storage_settings():
    return ok({"data_root": str(settings.data_root),
               "dirs": {
                   "input": str(settings.input_dir),
                   "output": str(settings.output_dir),
                   "canvases": str(settings.canvases_dir),
               }})


@router.patch("/storage/settings")
async def patch_storage_settings(body: StorageSettings):
    try:
        settings.set_data_root(body.data_root)
    except Exception as exc:
        raise ApiError("STORAGE_ERROR", f"无法切换数据目录：{exc}", status=500)
    from ..db import init_db
    init_db()
    return ok({"data_root": str(settings.data_root)})


@router.get("/app-info")
async def app_info():
    return ok({
        "version": APP_VERSION,
        "name": "Bflow",
        "update": {"channel": "none"},
    })


# ================================================================== 协议清单


@router.get("/protocols")
async def list_protocols():
    """可用协议清单 —— 渠道编辑界面用。"""
    from ..services.protocol import registry
    return ok({"protocols": registry.catalog()})
