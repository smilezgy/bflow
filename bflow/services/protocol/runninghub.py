"""RunningHub 工作流协议适配器。

协议要点（依据公开文档）：
- 提交：`POST {base}/task/openapi/create`
  body `{"apiKey": "...", "workflowId": "...", "nodeInfoList": [...]}`
  → `{"data": {"taskId": "..."}}`
- 轮询：`POST {base}/task/openapi/outputs`
  body `{"apiKey": "...", "taskId": "..."}`
  → `{"data": {"outputs": [{"url": "..."}], "status": "SUCCESS"}}`
- 鉴权：API Key 放在请求体的 `apiKey` 字段（也接受 `Authorization: Bearer`）
- 节点参数：通过 `nodeInfoList` 传入，形如
  `[{"nodeId": "12", "fieldValues": {"text": "..."}}]`

与 OpenAI 协议的关键差异：
- 工作流 id 与提示词节点 id 都需要在渠道 config 里指定；
- **必须轮询**；
- 产物格式、尺寸由工作流决定 → 能力表标 `honors_request: false`。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .base import (DEFAULT_TIMEOUT, Artifact, UpstreamResult, base_headers,
                   connection_error, download, error_result, make_artifact,
                   success, timeout_result)
from .errors import NormalizedError

POLL_INTERVAL = 3.0
POLL_MAX_SECONDS = 900.0

DONE_STATES = ("success", "succeeded", "completed", "done")
FAIL_STATES = ("failed", "canceled", "cancelled", "expired", "error")


def _build_node_info(req: dict, config: dict | None) -> list[dict]:
    """构造 nodeInfoList。"""
    config = config or {}
    node_id = config.get("prompt_node")
    if not node_id:
        return []
    fields: dict = {"text": req.get("prompt") or ""}
    if config.get("negative_prompt"):
        fields["negative_text"] = config["negative_prompt"]
    for key in ("seed", "steps", "cfg", "width", "height", "batch_size"):
        if key in config:
            fields[key] = config[key]
    size = req.get("size") or ""
    if size and "x" in size and "width" not in fields:
        try:
            w, h = size.lower().split("x", 1)
            fields["width"] = int(w)
            fields["height"] = int(h)
        except Exception:
            pass
    return [{"nodeId": str(node_id), "fieldValues": fields}]


def _output_urls(payload: dict) -> list[str]:
    outputs = payload.get("outputs")
    urls: list[str] = []
    if isinstance(outputs, list):
        for item in outputs:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                for key in ("url", "imageUrl", "image"):
                    if item.get(key):
                        urls.append(str(item[key]))
                        break
    elif isinstance(outputs, dict):
        for value in outputs.values():
            if isinstance(value, str):
                urls.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        urls.append(item)
                    elif isinstance(item, dict) and item.get("url"):
                        urls.append(str(item["url"]))
    return urls


async def generate(base_url: str, api_key: str, req: dict, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   extra_headers: dict | None = None,
                   config: dict | None = None) -> UpstreamResult:
    started = time.time()
    config = config or {}
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError("渠道地址为空")

    workflow_id = config.get("workflow_id") or config.get("workflowId")
    if not workflow_id:
        return error_result(400, "未配置 workflow_id",
                            elapsed=time.time() - started,
                            code="INVALID_REQUEST",
                            message="该渠道尚未配置工作流 id。",
                            hint="请在渠道设置里填写 workflow_id。", retryable=False)

    headers = base_headers(api_key, extra_headers)
    body = {
        "apiKey": api_key,
        "workflowId": str(workflow_id),
        "nodeInfoList": _build_node_info(req, config),
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(f"{base}/task/openapi/create", headers=headers,
                                     json=body, timeout=timeout)
    except httpx.TimeoutException:
        return timeout_result(time.time() - started)
    except Exception as exc:
        return connection_error(exc, time.time() - started)

    raw_text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        err = data.get("error") if isinstance(data, dict) else None
        text = err.get("message") if isinstance(err, dict) else raw_text
        return error_result(resp.status_code, str(text or "提交失败"),
                            elapsed=time.time() - started)

    payload = data.get("data") if isinstance(data, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    task_id = payload.get("taskId") or payload.get("task_id") or ""
    if not task_id:
        return error_result(resp.status_code, "提交成功但未返回 taskId",
                            elapsed=time.time() - started)

    poll_body = {"apiKey": api_key, "taskId": task_id}
    deadline = started + max(timeout, POLL_MAX_SECONDS)
    polls = 0
    last_raw = raw_text
    status = ""
    out_payload: dict = {}

    while True:
        if status in DONE_STATES or status in FAIL_STATES:
            break
        if time.time() >= deadline:
            return UpstreamResult(
                ok=False, status=0, elapsed=time.time() - started,
                raw_body=last_raw[:2000],
                error=NormalizedError(
                    code="UPSTREAM_TIMEOUT",
                    message="RunningHub 任务轮询超时。",
                    hint="生成耗时过长，请稍后到任务中心查看。", retryable=True),
                meta={"protocol": "runninghub", "task_id": task_id, "polls": polls})

        polls += 1
        await asyncio.sleep(POLL_INTERVAL)
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                presp = await client.post(f"{base}/task/openapi/outputs",
                                          headers=headers, json=poll_body, timeout=60.0)
        except Exception:
            continue
        last_raw = presp.text or ""
        try:
            pdata = presp.json()
        except Exception:
            continue
        ppayload = pdata.get("data") if isinstance(pdata, dict) else None
        out_payload = ppayload if isinstance(ppayload, dict) else {}
        status = str(out_payload.get("status") or "").lower()

    if status in FAIL_STATES:
        err = out_payload.get("error") or ""
        text = err.get("message") if isinstance(err, dict) else str(err)
        return error_result(200, text or f"任务状态：{status}",
                            elapsed=time.time() - started,
                            code="CHANNEL_INTERNAL_ERROR",
                            message=f"RunningHub 任务失败（状态 {status}）。",
                            hint="请检查工作流与节点参数。", retryable=False)

    urls = _output_urls(out_payload)
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
                   meta={"protocol": "runninghub", "task_id": task_id, "polls": polls})
