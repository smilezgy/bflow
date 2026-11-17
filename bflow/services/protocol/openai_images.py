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
from dataclasses import dataclass, field
from typing import Any

import httpx

from .errors import NormalizedError, normalize
from .imaging import ImageInfo, format_label, parse_image

DEFAULT_TIMEOUT = 300.0
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


@dataclass
class Artifact:
    """一份产物。"""
    data: bytes
    info: ImageInfo
    source_url: str = ""


@dataclass
class UpstreamResult:
    """一次上游调用的完整结果。"""
    ok: bool
    status: int
    artifacts: list[Artifact] = field(default_factory=list)
    error: NormalizedError | None = None
    raw_body: str = ""
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0


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
    for item in req.get("reference_images") or []:
        # 参考图以同名字段多次提交（实测 CH-A 接受）
        files.append(("image", item.filename, item.data))
    for item in req.get("masks") or []:
        files.append(("mask", item.filename, item.data))
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
                resp = await client.get(source_url, timeout=120.0)
                resp.raise_for_status()
                raw = resp.content
            except Exception:
                raw = b""
        if not raw:
            continue
        if len(raw) > MAX_ARTIFACT_BYTES:
            continue
        artifacts.append(Artifact(data=raw, info=parse_image(raw),
                                  source_url=source_url))
    return artifacts


async def _post(client: httpx.AsyncClient, url: str, *,
                headers: dict, json_body: dict | None = None,
                fields: dict | None = None,
                files: list | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> UpstreamResult:
    import time
    started = time.time()
    try:
        if json_body is not None:
            resp = await client.post(url, headers=headers, json=json_body, timeout=timeout)
        else:
            # multipart：字段与文件分开传；值统一转字符串
            data = {k: str(v) for k, v in (fields or {}).items()}
            multipart = [(name, (fname, content, "application/octet-stream"))
                         for name, fname, content in (files or [])]
            resp = await client.post(url, headers=headers, data=data,
                                     files=multipart or None, timeout=timeout)
    except httpx.TimeoutException:
        return UpstreamResult(
            ok=False, status=0, elapsed=time.time() - started,
            error=NormalizedError(code="UPSTREAM_TIMEOUT",
                                  message="上游响应超时。",
                                  hint="请稍后重试。", retryable=True))
    except Exception as exc:
        return UpstreamResult(
            ok=False, status=0, elapsed=time.time() - started,
            error=NormalizedError(code="CHANNEL_INTERNAL_ERROR",
                                  message=f"无法连接渠道：{type(exc).__name__}",
                                  hint="请检查渠道地址与网络连通性。",
                                  retryable=True))

    elapsed = time.time() - started
    raw_text = resp.text or ""

    if resp.status_code != 200:
        err = normalize(resp.status_code, raw_text,
                        requested_format=(json_body or fields or {}).get("output_format", ""),
                        requested_size=(json_body or fields or {}).get("size", ""),
                        raw_error=raw_text)
        return UpstreamResult(ok=False, status=resp.status_code, error=err,
                              raw_body=raw_text[:2000], elapsed=elapsed)

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
                   extra_headers: dict | None = None) -> UpstreamResult:
    """文生图。"""
    url = _endpoint_url(base_url, "images/generations")
    headers = _headers(api_key, extra_headers)
    payload = _build_generation_payload(req)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        return await _post(client, url, headers=headers, json_body=payload, timeout=timeout)


async def edit(base_url: str, api_key: str, req: dict, *,
               timeout: float = DEFAULT_TIMEOUT,
               extra_headers: dict | None = None) -> UpstreamResult:
    """带参考图编辑。"""
    url = _endpoint_url(base_url, "images/edits")
    headers = _headers(api_key, extra_headers)
    fields, files = _build_edit_fields(req)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        return await _post(client, url, headers=headers, fields=fields,
                           files=files, timeout=timeout)


def _endpoint_url(base_url: str, endpoint: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError("渠道地址为空")
    # base 可能自带 /v1（实测存在这种配置）
    if base.endswith("/v1"):
        return f"{base}/{endpoint}"
    return f"{base}/v1/{endpoint}"


def _headers(api_key: str, extra: dict | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "Bflow/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


# ------------------------------------------------------------------ 偏差检测


def detect_deviations(req: dict, artifacts: list[Artifact]) -> list[dict]:
    """检测产物与请求的偏差，产出警告列表。

    这是 Bflow 的核心差异化能力（spec/05-ui-ia.md §3.3）：
    产物有效，但与用户选择不同 —— 必须让用户知道。
    """
    warnings: list[dict] = []
    if not artifacts:
        return warnings

    requested_format = (req.get("output_format") or "").lower()
    requested_size = (req.get("size") or "").lower()

    info = artifacts[0].info
    actual_format = info.format
    actual_size = info.size_label

    if requested_format and actual_format and actual_format != requested_format:
        warnings.append({
            "code": "FORMAT_NOT_HONORED",
            "message": (f"你选择了 {format_label(requested_format)} 格式，"
                        f"但上游实际返回的是 {format_label(actual_format)}。"),
            "hint": f"如需 {format_label(requested_format)}，请换一个支持该格式的渠道。",
            "actual": {"format": actual_format},
        })

    if requested_size and actual_size and actual_size != requested_size:
        warnings.append({
            "code": "SIZE_NORMALIZED",
            "message": f"你请求了 {requested_size}，该渠道输出为 {actual_size}。",
            "hint": "产物尺寸由渠道决定，如需精确尺寸请更换渠道或调整请求。",
            "actual": {"width": info.width, "height": info.height},
        })

    return warnings
