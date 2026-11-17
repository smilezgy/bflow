"""火山方舟（Ark / Doubao-Seedream）图像生成适配器。

协议要点（依据公开文档）：
- 提交：`POST {base}/api/v3/images/generations`
  → 立即返回 `{"data": {"task_id": "..."}}`，**不直接返回图片**
- 轮询：`GET {base}/api/v3/images/tasks/{task_id}`
  → `{"status": "queued|running|succeeded|failed", "response": {"data": [...]}, "usage": {...}}`
- 产物在 `response.data[]`，每项含 `url` 或 `b64_json`，以及 `size`、`model` 等信息
- 错误：提交时 4xx 直接给 `{"error": {...}}`；任务失败时在
  `{"status": "failed", "error": {"message": ...}}`

与 OpenAI 协议的关键差异：
- **必须轮询**，因此本适配器内置轮询循环（含上限与退避）；
- `size` 语义不同：方舟用像素串或 `2K` / `4K` 等级（由 `sequential_image_generation`
  与模型决定），不认 OpenAI 的 `1024x1024` 全集；
- 不接受 `output_format` —— 产物格式由模型决定（通常 PNG/JPEG），
  因此能力表必须如实标注 `honors_request: false`。

因此本模块只做协议翻译；格式/尺寸判定与偏差检测复用 base 模块。
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx

from .base import (DEFAULT_TIMEOUT, Artifact, UpstreamResult, base_headers,
                   connection_error, error_result, make_artifact,
                   post_with_retry, success, timeout_result)
from .errors import NormalizedError

# 轮询参数
POLL_INTERVAL = 2.0
POLL_MAX_SECONDS = 600.0
TERMINAL_OK = ("succeeded", "success", "completed", "done")
TERMINAL_FAIL = ("failed", "cancelled", "canceled", "expired")


def _ark_base(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError("渠道地址为空")
    if "/api/v3" in base:
        return base
    return f"{base}/api/v3"


def _url(base_url: str, path: str) -> str:
    return f"{_ark_base(base_url)}/{path.lstrip('/')}"


def _build_body(req: dict, config: dict | None) -> dict:
    config = config or {}
    body: dict[str, Any] = {
        "model": req.get("model") or "",
        "prompt": req.get("prompt") or "",
    }

    # 尺寸：方舟接受像素串或 2K/4K 等级
    size = (config.get("size") or req.get("size") or "").strip()
    if size:
        body["size"] = size

    # 方舟的响应格式是 url / b64_json
    body["response_format"] = config.get("response_format") or "url"

    # 水印开关（渠道级配置）
    if "watermark" in config:
        body["watermark"] = bool(config["watermark"])

    # 参考图：方舟用 image 字段传图片 URL 或 base64
    images: list[str] = []
    for item in req.get("reference_images") or []:
        data = item[2] if isinstance(item, (list, tuple)) else item
        if not data:
            continue
        images.append("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
    if images:
        body["image"] = images[:int(config.get("max_reference_images") or 10)]

    # 遮罩：方舟用 mask 字段（base64 PNG）
    masks = req.get("masks") or []
    if masks:
        first = masks[0]
        data = first[2] if isinstance(first, (list, tuple)) else first
        if data:
            body["mask"] = "data:image/png;base64," + base64.b64encode(data).decode("ascii")

    # 透传自定义字段（如 sequential_image_generation / guidance_scale）
    extra = config.get("extra_body")
    if isinstance(extra, dict):
        for k, v in extra.items():
            body.setdefault(k, v)

    return body


def _sync_download(url: str, timeout: float = 120.0) -> bytes:
    """产物下载走独立连接，避免与轮询共用连接池造成阻塞。"""
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _extract_artifacts_sync(payload: list) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw = b""
        source_url = ""
        if item.get("b64_json"):
            try:
                raw = base64.b64decode(item["b64_json"])
            except Exception:
                raw = b""
        elif item.get("url"):
            source_url = item["url"]
            try:
                raw = _sync_download(source_url)
            except Exception:
                raw = b""
        artifact = make_artifact(raw, source_url)
        if artifact:
            artifacts.append(artifact)
    return artifacts


async def generate(base_url: str, api_key: str, req: dict, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   extra_headers: dict | None = None,
                   config: dict | None = None) -> UpstreamResult:
    started = time.time()
    headers = base_headers(api_key, extra_headers)
    body = _build_body(req, config)
    submit_url = _url(base_url, "images/generations")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await post_with_retry(client, submit_url, headers=headers,
                                         json_body=body, timeout=timeout)
    except httpx.TimeoutException:
        return timeout_result(time.time() - started)
    except Exception as exc:
        return connection_error(exc, time.time() - started)

    raw_text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code not in (200, 201):
        err = data.get("error") if isinstance(data, dict) else None
        flat = err if isinstance(err, dict) else {}
        text = flat.get("message") or raw_text
        return error_result(resp.status_code, text, elapsed=time.time() - started,
                            requested_format=req.get("output_format", ""),
                            requested_size=req.get("size", ""))

    task_id = ""
    if isinstance(data, dict):
        payload = data.get("data") or {}
        if isinstance(payload, dict):
            task_id = payload.get("task_id") or ""

    if not task_id:
        # 有些部署直接同步返回产物
        payload = (data.get("data") if isinstance(data, dict) else None)
        if isinstance(payload, list):
            artifacts = _extract_artifacts_sync(payload)
            usage = data.get("usage") or {}
            return success(artifacts, status=resp.status_code,
                           raw_body=raw_text[:2000],
                           usage=usage if isinstance(usage, dict) else {},
                           elapsed=time.time() - started,
                           meta={"protocol": "ark", "sync": True})
        return error_result(resp.status_code, "提交成功但未返回任务 id",
                            elapsed=time.time() - started)

    # 轮询
    poll_url = _url(base_url, f"images/tasks/{task_id}")
    deadline = started + max(timeout, POLL_MAX_SECONDS)
    polls = 0
    last_raw = raw_text

    while time.time() < deadline:
        polls += 1
        await asyncio.sleep(POLL_INTERVAL)
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                presp = await client.get(poll_url, headers=headers, timeout=60.0)
        except Exception:
            continue

        last_raw = presp.text or ""
        try:
            pdata = presp.json()
        except Exception:
            continue

        if presp.status_code != 200:
            err = pdata.get("error") if isinstance(pdata, dict) else None
            flat = err if isinstance(err, dict) else {}
            return error_result(presp.status_code, flat.get("message") or last_raw,
                                elapsed=time.time() - started)

        status = str((pdata or {}).get("status") or "").lower()
        if status in TERMINAL_FAIL:
            err = pdata.get("error") if isinstance(pdata, dict) else None
            flat = err if isinstance(err, dict) else {}
            return error_result(200, flat.get("message") or f"任务状态：{status}",
                                elapsed=time.time() - started,
                                code="CHANNEL_INTERNAL_ERROR",
                                message=f"方舟任务失败（状态 {status}）。",
                                hint="请检查提示词与该模型的可用性。",
                                retryable=False)

        if status in TERMINAL_OK:
            response = pdata.get("response") or {}
            items = response.get("data") if isinstance(response, dict) else None
            items = items if isinstance(items, list) else []
            artifacts = _extract_artifacts_sync(items)
            usage = pdata.get("usage") or {}
            return success(artifacts, status=200, raw_body=last_raw[:2000],
                           usage=usage if isinstance(usage, dict) else {},
                           elapsed=time.time() - started,
                           meta={"protocol": "ark", "task_id": task_id, "polls": polls})

    return UpstreamResult(
        ok=False, status=0, elapsed=time.time() - started, raw_body=last_raw[:2000],
        error=NormalizedError(
            code="UPSTREAM_TIMEOUT",
            message="方舟任务轮询超时。",
            hint="生成耗时过长，请稍后到任务中心查看。", retryable=True),
        meta={"protocol": "ark", "task_id": task_id, "polls": polls})


async def edit(base_url: str, api_key: str, req: dict, *,
               timeout: float = DEFAULT_TIMEOUT,
               extra_headers: dict | None = None,
               config: dict | None = None) -> UpstreamResult:
    """方舟的编辑与生成走同一端点（用 image / mask 字段表达输入）。"""
    return await generate(base_url, api_key, req, timeout=timeout,
                          extra_headers=extra_headers, config=config)
