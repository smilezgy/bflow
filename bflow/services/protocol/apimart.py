"""APIMart 异步任务协议适配器。

协议要点（依据公开文档）：
- 提交：`POST {base}/v1/tasks`
  → `{"data": {"id": "task_xxx", "status": "waiting"}}`
- 轮询：`GET {base}/v1/tasks/{id}`
  → `{"data": {"id","status","model","inputs","output","error","created_at","completed_at"}}`
- 状态：`waiting` → `processing` → `succeeded` / `failed` / `cancelled` / `expired`
- 产物：`output` 为字符串数组（图片 URL），失败时 `error` 给出原因
- 鉴权：请求头 `x-api-key: <key>`（部分部署同时接受 Bearer）

与 OpenAI 协议的关键差异：
- **必须轮询**（本适配器内置轮询与退避）；
- 参数按模型不同而不同，因此通过渠道 config 里的 `extra_body` 透传；
- 产物格式由模型决定，能力表需标注 `honors_request: false`。
"""

from __future__ import annotations

import time

import httpx

from .base import (DEFAULT_TIMEOUT, Artifact, UpstreamResult, base_headers,
                   connection_error, download, endpoint_url, error_result,
                   make_artifact, post_with_retry, success, timeout_result)
from .errors import NormalizedError

POLL_INTERVAL = 3.0
POLL_MAX_SECONDS = 900.0

DONE_STATES = ("succeeded", "success", "completed", "done")
FAIL_STATES = ("failed", "cancelled", "canceled", "expired", "error")


def _headers(api_key: str, extra: dict | None = None) -> dict:
    headers = base_headers(api_key, extra)
    # APIMart 用 x-api-key；同时保留 Bearer 兼容不同部署
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _build_body(req: dict, config: dict | None) -> dict:
    config = config or {}
    body = {
        "model": req.get("model") or "",
        "inputs": {"prompt": req.get("prompt") or ""},
    }
    # 尺寸/格式等参数按模型不同，通过 config 指定
    if config.get("size"):
        body["inputs"]["size"] = config["size"]
    elif req.get("size"):
        body["inputs"]["size"] = req["size"]
    if config.get("aspect_ratio"):
        body["inputs"]["aspect_ratio"] = config["aspect_ratio"]
    if config.get("num_images"):
        body["inputs"]["num_images"] = int(config["num_images"])
    elif req.get("count") and req["count"] > 1:
        body["inputs"]["num_images"] = int(req["count"])
    if config.get("output_format"):
        body["inputs"]["output_format"] = config["output_format"]
    elif req.get("output_format"):
        body["inputs"]["output_format"] = req["output_format"]

    extra = config.get("extra_body")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k == "inputs" and isinstance(v, dict):
                body["inputs"].update(v)
            else:
                body[k] = v
    return body


def _output_urls(payload: dict) -> list[str]:
    output = payload.get("output")
    urls: list[str] = []
    if isinstance(output, str):
        urls.append(output)
    elif isinstance(output, list):
        for item in output:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                for key in ("url", "image_url", "image"):
                    if item.get(key):
                        urls.append(str(item[key]))
                        break
    elif isinstance(output, dict):
        for key in ("images", "urls", "image_urls"):
            value = output.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        urls.append(item)
                    elif isinstance(item, dict) and item.get("url"):
                        urls.append(str(item["url"]))
                break
    return urls


async def generate(base_url: str, api_key: str, req: dict, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   extra_headers: dict | None = None,
                   config: dict | None = None) -> UpstreamResult:
    started = time.time()
    headers = _headers(api_key, extra_headers)
    body = _build_body(req, config)
    submit_url = endpoint_url(base_url, "tasks")

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
        text = flat.get("message") or flat.get("error") or raw_text
        return error_result(resp.status_code, str(text), elapsed=time.time() - started,
                            requested_format=req.get("output_format", ""),
                            requested_size=req.get("size", ""))

    payload = data.get("data") if isinstance(data, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    task_id = payload.get("id") or payload.get("task_id") or ""

    if not task_id:
        return error_result(resp.status_code, "提交成功但未返回任务 id",
                            elapsed=time.time() - started)

    poll_url = endpoint_url(base_url, f"tasks/{task_id}")
    deadline = started + max(timeout, POLL_MAX_SECONDS)
    polls = 0
    last_raw = raw_text
    status = str(payload.get("status") or "").lower()

    while True:
        if status in DONE_STATES or status in FAIL_STATES:
            break
        if time.time() >= deadline:
            return UpstreamResult(
                ok=False, status=0, elapsed=time.time() - started,
                raw_body=last_raw[:2000],
                error=NormalizedError(
                    code="UPSTREAM_TIMEOUT",
                    message="APIMart 任务轮询超时。",
                    hint="生成耗时过长，请稍后到任务中心查看。", retryable=True),
                meta={"protocol": "apimart", "task_id": task_id, "polls": polls})

        import asyncio
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
        ppayload = pdata.get("data") if isinstance(pdata, dict) else None
        if not isinstance(ppayload, dict):
            ppayload = {}
        status = str(ppayload.get("status") or "").lower()
        payload = ppayload

    if status in FAIL_STATES:
        err = payload.get("error") or {}
        text = err.get("message") if isinstance(err, dict) else str(err)
        return error_result(200, text or f"任务状态：{status}",
                            elapsed=time.time() - started,
                            code="CHANNEL_INTERNAL_ERROR",
                            message=f"APIMart 任务失败（状态 {status}）。",
                            hint="请检查提示词与该模型的可用性。", retryable=False)

    urls = _output_urls(payload)
    artifacts: list[Artifact] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for url in urls:
            try:
                raw = await download(client, url)
            except Exception:
                continue
            artifact = make_artifact(raw, url)
            if artifact:
                artifacts.append(artifact)

    return success(artifacts, status=200, raw_body=last_raw[:2000],
                   elapsed=time.time() - started,
                   meta={"protocol": "apimart", "task_id": task_id, "polls": polls})


async def edit(base_url: str, api_key: str, req: dict, *,
               timeout: float = DEFAULT_TIMEOUT,
               extra_headers: dict | None = None,
               config: dict | None = None) -> UpstreamResult:
    """APIMart 的编辑同样是任务，只是 inputs 不同。

    参考图通过 config 里的 `inputs` 覆盖传入（各模型字段名不统一）。
    """
    return await generate(base_url, api_key, req, timeout=timeout,
                          extra_headers=extra_headers, config=config)
