"""生图服务：能力表应用、协议调用、偏差检测、任务落库。

流程：
1. 载入渠道与能力表；
2. 按能力表做**前置校验**（格式不支持、参考图超限、mask 非法 → 直接报错，不发请求）；
3. 调协议层；
4. 落盘产物 → 记录偏差警告 → 写任务记录。

关键原则（spec/03-api-contract.md §2.3）：
- 不降级格式、不篡改尺寸、不丢弃产物、不隐藏偏差。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..db import execute, query_one
from . import assets as assets_svc
from . import channels as channels_svc
from .protocol import openai_images
from .protocol.imaging import format_label


@dataclass
class GenerationRequest:
    prompt: str
    channel_id: str
    model: str
    size: str = "1024x1024"
    count: int = 1
    output_format: str = "png"
    output_compression: int | None = None
    background: str = ""
    quality: str = ""
    complexity: str = ""
    frame_count: int | None = None
    frame_duration_ms: int | None = None
    reference_asset_ids: list[str] = field(default_factory=list)
    mask_asset_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt, "channel_id": self.channel_id, "model": self.model,
            "size": self.size, "count": self.count,
            "output_format": self.output_format,
            "output_compression": self.output_compression,
            "background": self.background, "quality": self.quality,
            "complexity": self.complexity,
            "frame_count": self.frame_count,
            "frame_duration_ms": self.frame_duration_ms,
            "reference_images": self.reference_asset_ids,
            "masks": self.mask_asset_ids,
        }


@dataclass
class GenerationOutcome:
    ok: bool
    task_id: str = ""
    images: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    error: dict | None = None
    billed: bool = False
    elapsed: float = 0.0


class ValidationError(Exception):
    """前置校验失败 —— 不发请求。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def validate_request(req: GenerationRequest, capability: dict) -> None:
    """按能力表做前置校验。

    这些检查是**我们**的职责：渠道侧要么静默忽略（用户不知情），
    要么用含糊的报文报错。能在本地拦下的，就不要浪费一次计费请求。
    """
    fmt = (req.output_format or "").lower()
    formats = capability.get("output_formats") or {}

    if fmt and formats:
        info = formats.get(fmt)
        if info is not None and not info.get("supported", False):
            raise ValidationError(
                "FORMAT_NOT_SUPPORTED",
                f"该渠道不支持「{format_label(fmt)}」输出格式。",
                "建议：① 把输出格式改成 PNG 或 WebP；② 或换一个支持该格式的渠道。")

    # 透明背景需要支持 alpha 的格式
    if (req.background or "").lower() == "transparent":
        allowed = capability.get("transparent_formats") or ["png", "webp"]
        if fmt and fmt not in allowed:
            raise ValidationError(
                "INVALID_REQUEST",
                f"{format_label(fmt)} 不支持透明背景。",
                f"请把输出格式改为 {' 或 '.join(format_label(f) for f in allowed)}。")

    # PNG 不接受小于 100 的压缩率（实测会报 400）
    if fmt == "png" and req.output_compression is not None and req.output_compression < 100:
        raise ValidationError(
            "INVALID_REQUEST",
            "PNG 不接受小于 100 的压缩率。",
            "请把压缩质量调到 100，或改用 JPEG / WebP。")

    # 参考图数量上限
    edits_cap = capability.get("edits") or {}
    max_refs = int(edits_cap.get("max_reference_images") or 0)
    if req.reference_asset_ids:
        if not edits_cap.get("supported", False):
            raise ValidationError(
                "INVALID_REQUEST",
                "该渠道不支持参考图编辑。",
                "请换一个支持编辑的渠道。")
        if max_refs and len(req.reference_asset_ids) > max_refs:
            raise ValidationError(
                "REFERENCE_TOO_MANY",
                f"该渠道最多支持 {max_refs} 张参考图，本次提交了 {len(req.reference_asset_ids)} 张。",
                "请减少参考图后重试。")

    # 质量 / 复杂度档位
    if req.quality and req.quality != "auto":
        levels = capability.get("quality_levels") or []
        if levels and req.quality not in levels:
            raise ValidationError(
                "INVALID_REQUEST",
                f"该渠道不支持质量档「{req.quality}」。",
                f"可用档位：{', '.join(levels)}。")
    if req.complexity and req.complexity != "auto":
        levels = capability.get("complexity_levels") or []
        if levels and req.complexity not in levels:
            raise ValidationError(
                "INVALID_REQUEST",
                f"该渠道不支持复杂度「{req.complexity}」。",
                f"可用档位：{', '.join(levels)}。")

    # SVG 必须单张
    if fmt == "svg" and req.count > 1:
        raise ValidationError(
            "INVALID_REQUEST", "SVG 输出仅支持单张。", "请把数量改为 1。")


def _load_reference_files(asset_ids: list[str]) -> list[tuple[str, str, bytes]]:
    """载入参考图文件。"""
    out = []
    for aid in asset_ids:
        asset = assets_svc.get_asset(aid)
        if not asset:
            continue
        path = assets_svc.asset_path(asset)
        if not path.exists():
            continue
        out.append(("image", path.name, path.read_bytes()))
    return out


def _validate_masks(asset_ids: list[str], main_asset_id: str | None) -> list[tuple[str, str, bytes]]:
    """校验 mask —— 上游不校验，必须我们自己做（见 spec/01 §4.4）。

    检查：可解码、PNG、含 alpha、尺寸与主图一致。
    """
    out = []
    main_info = None
    if main_asset_id:
        main = assets_svc.get_asset(main_asset_id)
        if main:
            main_info = (main.width, main.height)

    for aid in asset_ids:
        asset = assets_svc.get_asset(aid)
        if not asset:
            raise ValidationError("INVALID_REQUEST", "遮罩资源不存在。", "请重新上传遮罩。")
        path = assets_svc.asset_path(asset)
        if not path.exists():
            raise ValidationError("INVALID_REQUEST", "遮罩文件缺失。", "请重新上传遮罩。")
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValidationError(
                "INVALID_REQUEST", "遮罩须为 PNG 格式。",
                "请导出为 PNG 后重试。")
        try:
            from PIL import Image
            with Image.open(path) as im:
                if im.mode not in ("RGBA", "LA", "PA"):
                    raise ValidationError(
                        "INVALID_REQUEST", "遮罩必须包含透明通道（alpha）。",
                        "请使用带 alpha 的 PNG。")
                w, h = im.size
        except ValidationError:
            raise
        except Exception:
            raise ValidationError(
                "INVALID_REQUEST", "遮罩文件无法识别为图片。", "请重新上传有效的 PNG。")
        if main_info and (w, h) != main_info:
            raise ValidationError(
                "INVALID_REQUEST",
                f"遮罩尺寸 {w}x{h} 与主图 {main_info[0]}x{main_info[1]} 不一致。",
                "请把遮罩调整为与主图相同的尺寸。")
        out.append(("mask", path.name, data))
    return out


async def generate(req: GenerationRequest) -> GenerationOutcome:
    """执行一次生成。"""
    started = time.time()

    channel = channels_svc.get_channel(req.channel_id)
    if not channel:
        return GenerationOutcome(
            ok=False,
            error={"code": "NOT_FOUND", "message": "渠道不存在。",
                   "hint": "请重新选择渠道。", "retryable": False})
    if not channel.get("enabled", True):
        return GenerationOutcome(
            ok=False,
            error={"code": "INVALID_REQUEST", "message": "该渠道已停用。",
                   "hint": "请在渠道设置中启用。", "retryable": False})

    capability = channels_svc.capability_for(req.channel_id, req.model)

    # 前置校验（不发请求，不计费）
    try:
        validate_request(req, capability)
    except ValidationError as exc:
        return GenerationOutcome(
            ok=False,
            error={"code": exc.code, "message": exc.message,
                   "hint": exc.hint, "retryable": False})

    api_key = channels_svc.get_secret(
        query_one("SELECT * FROM channel WHERE id = ?", (req.channel_id,)))
    extra_headers = (channel.get("config") or {}).get("headers") or {}

    # 组装上游请求
    upstream_req: dict = {
        "model": req.model,
        "prompt": req.prompt,
        "size": req.size,
        "output_format": req.output_format,
        "output_compression": req.output_compression,
        "background": req.background,
        "quality": req.quality,
        "complexity": req.complexity,
        "frame_count": req.frame_count,
        "frame_duration_ms": req.frame_duration_ms,
        "n": req.count,
    }

    has_refs = bool(req.reference_asset_ids)
    try:
        if has_refs:
            upstream_req["reference_images"] = _load_reference_files(req.reference_asset_ids)
            main_id = req.reference_asset_ids[0] if req.reference_asset_ids else None
            upstream_req["masks"] = _validate_masks(req.mask_asset_ids, main_id)
            result = await openai_images.edit(
                channel["base_url"], api_key, upstream_req,
                extra_headers=extra_headers)
        else:
            result = await openai_images.generate(
                channel["base_url"], api_key, upstream_req,
                extra_headers=extra_headers)
    except Exception as exc:
        return GenerationOutcome(
            ok=False,
            error={"code": "CHANNEL_INTERNAL_ERROR",
                   "message": f"调用渠道失败：{type(exc).__name__}",
                   "hint": "请检查渠道配置后重试。", "retryable": True})

    if not result.ok:
        err = result.error
        return GenerationOutcome(
            ok=False,
            error={"code": err.code if err else "CHANNEL_INTERNAL_ERROR",
                   "message": err.message if err else "渠道调用失败。",
                   "hint": err.hint if err else "",
                   "retryable": bool(err.retryable) if err else False,
                   "detail": {"raw": (err.raw or "")[:400]} if err else {}},
            billed=False,
            elapsed=time.time() - started)

    # 落盘产物
    images: list[dict] = []
    for artifact in result.artifacts:
        stored = assets_svc.store_bytes(
            artifact.data, kind="output", origin="generate")
        images.append({
            "id": stored.id,
            "url": f"/api/assets/{stored.id}",
            "width": stored.width,
            "height": stored.height,
            "format": stored.format,
            "bytes": stored.bytes,
            "requested_format": req.output_format,
            "requested_size": req.size,
        })

    warnings = openai_images.detect_deviations(upstream_req, result.artifacts)

    return GenerationOutcome(
        ok=True,
        images=images,
        warnings=warnings,
        billed=True,
        elapsed=time.time() - started)


def record_task(req: GenerationRequest, outcome: GenerationOutcome,
                operation: str = "generate") -> str:
    """把一次生成写入任务表，返回 task_id。"""
    task_id = channels_svc.new_id("task")
    now = time.time()
    status = "succeeded" if outcome.ok else "failed"
    error = outcome.error or {}
    actual_format = outcome.images[0]["format"] if outcome.images else None
    actual_size = (f"{outcome.images[0]['width']}x{outcome.images[0]['height']}"
                   if outcome.images and outcome.images[0].get("width") else None)
    execute(
        "INSERT INTO task (id, channel_id, model, operation, status, prompt,"
        " request_json, requested_size, requested_format, actual_size, actual_format,"
        " error_code, error_message, warnings_json, progress, billed,"
        " created_at, started_at, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, req.channel_id, req.model, operation, status, req.prompt,
         json.dumps(req.to_dict(), ensure_ascii=False),
         req.size, req.output_format, actual_size, actual_format,
         error.get("code"), error.get("message"),
         json.dumps(outcome.warnings, ensure_ascii=False),
         1.0 if outcome.ok else 0.0, 1 if outcome.billed else 0,
         now, now, now),
    )
    for idx, img in enumerate(outcome.images):
        execute(
            "INSERT OR IGNORE INTO task_asset (task_id, asset_id, role, position)"
            " VALUES (?,?,?,?)", (task_id, img["id"], "output", idx))
    return task_id
