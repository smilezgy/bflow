"""Gemini（Google Generative Language）图像生成适配器。

协议要点（依据公开文档，v1beta）：
- 端点：`POST {base}/v1beta/models/{model}:generateContent`
- 请求体用 `contents[].parts[]` 承载文本与图片：
  文本 → `{"text": "..."}`；图片 → `{"inlineData": {"mimeType","data"}}`（base64）
- 产物在 `candidates[].content.parts[].inlineData`（同样是 base64）
- 尺寸用 `generationConfig.imageConfig.aspectRatio` 表达，**不是**像素值
- 报错：`{"error": {"code": 400, "message": "...", "status": "INVALID_ARGUMENT"}}`

与 OpenAI 协议的关键差异：
- 不接受 `output_format` / `quality` / `complexity` / `background`；
  输出格式由模型固定（通常 PNG），因此能力表里 `honors_request` 需如实标注；
- 参考图数量有限（部分模型仅支持 1 张），能力表里 `max_reference_images` 要小。

本模块只做协议翻译；格式/尺寸判定与偏差检测复用 base 模块。
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from .base import (DEFAULT_TIMEOUT, Artifact, UpstreamResult, base_headers,
                   connection_error, endpoint_url, error_result, make_artifact,
                   success, timeout_result)

# 像素尺寸 → Gemini 宽高比
_ASPECT_BY_SIZE = {
    "512x512": "1:1", "768x768": "1:1", "896x896": "1:1",
    "1024x1024": "1:1", "2048x2048": "1:1",
    "1536x1024": "3:2", "1024x1536": "2:3",
    "1792x1024": "16:9", "1024x1792": "9:16",
}


def _aspect_ratio(size: str) -> str:
    return _ASPECT_BY_SIZE.get((size or "").lower(), "1:1")


def _mime_of(filename: str, data: bytes) -> str:
    head = data[:8]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if (filename or "").lower().endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def _build_parts(req: dict) -> list[dict]:
    """组装 parts：文本在前，参考图随后。"""
    parts: list[dict] = []
    prompt = (req.get("prompt") or "").strip()
    if prompt:
        parts.append({"text": prompt})

    for item in req.get("reference_images") or []:
        data = item[2] if isinstance(item, (list, tuple)) else item
        name = ""
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            name = item[1]
        if not data:
            continue
        parts.append({
            "inlineData": {
                "mimeType": _mime_of(name, data),
                "data": base64.b64encode(data).decode("ascii"),
            }
        })
    return parts


def _build_body(req: dict, config: dict | None) -> dict:
    config = config or {}
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": _build_parts(req)}],
    }
    gen_config: dict[str, Any] = {"responseModalities": ["IMAGE"]}
    aspect = config.get("aspect_ratio") or _aspect_ratio(req.get("size", ""))
    image_config: dict[str, Any] = {}
    if aspect:
        image_config["aspectRatio"] = aspect
    # 支持通过 config 覆盖尺寸表达（部分模型支持 imageSize）
    if config.get("image_size"):
        image_config["imageSize"] = config["image_size"]
    if image_config:
        gen_config["imageConfig"] = image_config
    body["generationConfig"] = gen_config

    # 渠道自定义参数（透传，不做校验 —— 上游会明确报错）
    extra = config.get("extra_body")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in ("contents", "generationConfig"):
                body[k] = v
    return body


def _extract(data: dict) -> list[Artifact]:
    artifacts: list[Artifact] = []
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        return artifacts
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        content = cand.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            raw_b64 = inline.get("data") or ""
            if not raw_b64:
                continue
            try:
                raw = base64.b64decode(raw_b64)
            except Exception:
                continue
            artifact = make_artifact(raw)
            if artifact:
                artifacts.append(artifact)
    return artifacts


def _blocked_reason(data: dict) -> str:
    """Gemini 会用 finishReason 表达「被安全策略拦截」。"""
    candidates = data.get("candidates") or []
    for cand in candidates:
        if isinstance(cand, dict):
            reason = cand.get("finishReason") or cand.get("finish_reason")
            if reason and str(reason).upper() not in ("STOP", "MAX_TOKENS"):
                return str(reason)
    return ""


async def generate(base_url: str, api_key: str, req: dict, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   extra_headers: dict | None = None,
                   config: dict | None = None) -> UpstreamResult:
    started = time.time()
    model = req.get("model") or ""
    url = endpoint_url(base_url, f"models/{model}:generateContent", version="v1beta")
    headers = base_headers(api_key, extra_headers)
    body = _build_body(req, config)

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException:
        return timeout_result(time.time() - started)
    except Exception as exc:
        return connection_error(exc, time.time() - started)

    elapsed = time.time() - started
    raw_text = resp.text or ""

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        # Gemini 的 error 结构是嵌套的，交给通用归一化之前先摊平
        err = data.get("error") if isinstance(data, dict) else None
        flat = err if isinstance(err, dict) else {}
        text = raw_text
        if flat.get("message"):
            text = f"{flat.get('message')} {flat.get('status', '')}".strip()
        return error_result(resp.status_code, text, elapsed=elapsed,
                            requested_format=req.get("output_format", ""),
                            requested_size=req.get("size", ""))

    reason = _blocked_reason(data)
    if reason:
        from .base import NormalizedError
        return UpstreamResult(
            ok=False, status=resp.status_code, elapsed=elapsed,
            raw_body=raw_text[:2000],
            error=NormalizedError(
                code="CONTENT_REJECTED",
                message=f"上游拒绝了本次生成（finishReason={reason}）。",
                hint="提示词可能触发了内容安全策略，请调整描述后重试。",
                retryable=False))

    artifacts = _extract(data)
    if not artifacts:
        from .base import NormalizedError
        return UpstreamResult(
            ok=False, status=resp.status_code, elapsed=elapsed,
            raw_body=raw_text[:2000],
            error=NormalizedError(
                code="CHANNEL_INTERNAL_ERROR",
                message="上游返回成功但没有产物。",
                hint="该模型可能不支持图像输出，请换一个模型或渠道。",
                retryable=True))

    usage = data.get("usageMetadata") or {}
    return success(artifacts, status=resp.status_code, raw_body=raw_text[:2000],
                   usage=usage if isinstance(usage, dict) else {},
                   elapsed=elapsed,
                   meta={"protocol": "gemini", "model": model})


async def edit(base_url: str, api_key: str, req: dict, *,
               timeout: float = DEFAULT_TIMEOUT,
               extra_headers: dict | None = None,
               config: dict | None = None) -> UpstreamResult:
    """Gemini 的「编辑」就是带参考图的生成 —— 同一端点。"""
    return await generate(base_url, api_key, req, timeout=timeout,
                          extra_headers=extra_headers, config=config)
