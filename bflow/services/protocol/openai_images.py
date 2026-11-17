"""OpenAI Images 协议适配器。

覆盖两类端点：
- POST /v1/images/generations  文生图
- POST /v1/images/edits        带参考图编辑（multipart）

关键行为（全部来自 P0/P1 实测，见 spec/01-platform-behavior.md）：
- 请求体**始终携带**用户所选的 output_format，绝不偷改；
- 产物格式与尺寸一律按魔数解析，不信任上游声明；
- 上游 200 但产物格式/尺寸与请求不符时，**交付产物 + 产出警告**，
  绝不丢弃（上游已计费）；
- 上游明确拒绝时，直接报错，不重试、不换格式。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .base import (DEFAULT_TIMEOUT, MAX_ARTIFACT_BYTES, Artifact, UpstreamResult,
                   base_headers, detect_deviations, download, endpoint_url,
                   error_result, make_artifact, post_json, post_with_retry,
                   timeout_result)
from .errors import NormalizedError

# 兼容旧引用：其他模块从本模块取 Artifact / UpstreamResult / detect_deviations
__all__ = ["Artifact", "UpstreamResult", "detect_deviations", "generate", "edit"]


def _build_generation_payload(req: dict) -> dict:
    """组装文生图请求体。

    注意：output_format 只要用户选了就一定带上 —— 不因渠道可能不支持而省略
    （省略会导致渠道按默认格式出图，用户拿到与所选不符的结果却不知情）。
    """
    payload: dict[str, Any] = {
        "model": req["model"],
        "prompt": req.get("prompt", ""),
    }

    size = req.get("size")
    if size:
        payload["size"] = size

    fmt = (req.get("output_format") or "").lower()
    if fmt:
        payload["output_format"] = fmt

    # 压缩率只对 jpeg / webp 有意义；png 传了会被渠道以「压缩率 <100」拒绝
    compression = req.get("output_compression")
    if compression is not None and fmt in ("jpeg", "webp"):
        payload["output_compression"] = compression

    for key in ("background", "quality", "complexity", "n"):
        value = req.get(key)
        if value not in (None, "", "auto"):
            payload[key] = value

    # GIF 帧参数
    if fmt == "gif":
        if req.get("frame_count"):
            payload["frame_count"] = req["frame_count"]
        if req.get("frame_duration_ms"):
            payload["frame_duration_ms"] = req["frame_duration_ms"]

    # SVG 强制单张（上游要求 n=1）
    if fmt == "svg":
        payload["n"] = 1

    return payload


def _build_edit_fields(req: dict) -> tuple[dict, list[tuple[str, str, bytes]]]:
    """组装编辑请求的表单字段与文件部分。"""
    fields: dict[str, Any] = {
        "model": req["model"],
        "prompt": req.get("prompt", ""),
    }
    if req.get("size"):
        fields["size"] = req["size"]
    fmt = (req.get("output_format") or "").lower()
    if fmt:
        fields["output_format"] = fmt
    compression = req.get("output_compression")
    if compression is not None and fmt in ("jpeg", "webp"):
        fields["output_compression"] = str(compression)
    for key in ("background", "quality", "complexity"):
        value = req.get(key)
        if value not in (None, "", "auto"):
            fields[key] = str(value)
    if req.get("n"):
        fields["n"] = str(req["n"])
    if fmt == "svg":
        fields["n"] = "1"

    files: list[tuple[str, str, bytes]] = []

    def as_file(item, default_name: str) -> tuple[str, str, bytes] | None:
        """把一张上传图规范成 (字段名, 文件名, bytes) 三元组。

        generation.py 的 _load_reference_files / _validate_masks 产出的就是
        三元组（与 base.post_json 的 multipart 约定一致，ark/gemini 也按此
        用 item[2] 取内容）。历史上这里误用 item.filename / item.data ——
        tuple 没有这些属性，带参考图时必抛 AttributeError。保留对象格式
        兼容只是防御，正常链路一律走三元组。
        """
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            return (str(item[0] or default_name), str(item[1] or "image"), item[2])
        if hasattr(item, "filename") and hasattr(item, "data"):
            return (default_name, str(item.filename), item.data)
        return None

    # 参考图以同名字段多次提交（实测 CH-A 接受）
    for item in req.get("reference_images") or []:
        t = as_file(item, "image")
        if t:
            files.append(t)
    for item in req.get("masks") or []:
        t = as_file(item, "mask")
        if t:
            files.append(t)
    return fields, files


async def _extract_artifacts(data: dict, client: httpx.AsyncClient) -> list[Artifact]:
    """从上游响应中取回产物，统一按魔数解析。"""
    artifacts: list[Artifact] = []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return artifacts

    for item in items:
        if not isinstance(item, dict):
            continue
        raw: bytes = b""
        source_url = ""
        if item.get("b64_json"):
            try:
                import base64
                raw = base64.b64decode(item["b64_json"])
            except Exception:
                raw = b""
        elif item.get("url"):
            source_url = item["url"]
            try:
                raw = await download(client, source_url)
            except Exception:
                raw = b""
        artifact = make_artifact(raw, source_url)
        if artifact:
            artifacts.append(artifact)
    return artifacts


async def _post(client: httpx.AsyncClient, url: str, *,
                headers: dict, json_body: dict | None = None,
                fields: dict | None = None,
                files: list | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> UpstreamResult:
    import time
    started = time.time()
    try:
        resp = await post_with_retry(client, url, headers=headers,
                                     json_body=json_body, form=fields,
                                     files=files, timeout=timeout)
    except httpx.TimeoutException:
        return timeout_result(time.time() - started)
    except Exception as exc:
        from .base import connection_error
        return connection_error(exc, time.time() - started)

    elapsed = time.time() - started
    raw_text = resp.text or ""

    if resp.status_code != 200:
        return error_result(resp.status_code, raw_text, elapsed=elapsed,
                            requested_format=(json_body or fields or {}).get("output_format", ""),
                            requested_size=(json_body or fields or {}).get("size", ""))

    try:
        data = resp.json()
    except Exception:
        return UpstreamResult(
            ok=False, status=resp.status_code, raw_body=raw_text[:2000],
            elapsed=elapsed,
            error=NormalizedError(code="CHANNEL_INTERNAL_ERROR",
                                  message="渠道返回了无法解析的内容。",
                                  hint="请稍后重试，或更换渠道。", retryable=True))

    artifacts = await _extract_artifacts(data, client)
    usage = data.get("usage") if isinstance(data, dict) else None
    return UpstreamResult(
        ok=True, status=200, artifacts=artifacts,
        raw_body=raw_text[:2000],
        usage=usage if isinstance(usage, dict) else {},
        elapsed=elapsed)


async def generate(base_url: str, api_key: str, req: dict, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   extra_headers: dict | None = None,
                   config: dict | None = None) -> UpstreamResult:
    """文生图。"""
    url = endpoint_url(base_url, "images/generations")
    headers = base_headers(api_key, extra_headers)
    payload = _build_generation_payload(req)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        return await _post(client, url, headers=headers, json_body=payload, timeout=timeout)


async def edit(base_url: str, api_key: str, req: dict, *,
               timeout: float = DEFAULT_TIMEOUT,
               extra_headers: dict | None = None,
               config: dict | None = None) -> UpstreamResult:
    """带参考图编辑。"""
    url = endpoint_url(base_url, "images/edits")
    headers = base_headers(api_key, extra_headers)
    fields, files = _build_edit_fields(req)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        return await _post(client, url, headers=headers, fields=fields,
                           files=files, timeout=timeout)


def _endpoint_url(base_url: str, endpoint: str) -> str:
    return endpoint_url(base_url, endpoint)


def _headers(api_key: str, extra: dict | None = None) -> dict:
    return base_headers(api_key, extra)
