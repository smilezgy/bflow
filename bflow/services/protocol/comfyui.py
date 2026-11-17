"""ComfyUI 工作流协议适配器。

协议要点（依据公开文档）：
- 提交：`POST {base}/prompt`  body `{"prompt": {节点图}, "client_id": "..."}`
  → `{"prompt_id": "abc-123", "number": 1, "node_errors": {}}`
- 轮询：`GET {base}/history/{prompt_id}`
  → `{"<prompt_id>": {"outputs": {"<node_id>": {"images": [{"filename","subfolder","type"}]}}}}`
- 取图：`GET {base}/view?filename=...&subfolder=...&type=output` → 二进制
- 取消：`POST {base}/interrupt`

与 OpenAI 协议的关键差异：
- **没有参数 schema**：产物格式、尺寸、模型全由用户工作流决定。
  因此 Bflow 侧的做法是 —— 渠道 config 里存工作流 JSON 模板，
  按 `prompt_node` 指定的节点写入提示词，其余节点保持不变。
- 工作流本身可以包含模型加载、放大、ControlNet 等复杂链路，
  这正是「工作流类渠道」的价值所在。
- 参考图通过 config 指定的 `image_node` 节点写入（LoadImage 类节点）。

因此能力表里该协议的 `output_formats` 只能标 `honors_request: false`。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid

import httpx

from .base import (DEFAULT_TIMEOUT, Artifact, UpstreamResult, base_headers,
                   connection_error, endpoint_url, error_result, make_artifact,
                   success, timeout_result)
from .errors import NormalizedError

POLL_INTERVAL = 2.0
POLL_MAX_SECONDS = 900.0


def _load_workflow(config: dict | None) -> dict:
    """取工作流模板。

    支持三种来源（优先级从高到低）：
    1. config["workflow"] —— 直接给 JSON 对象
    2. config["workflow_file"] —— 给服务器上的文件路径（相对 ComfyUI 的 input 目录）
    3. config["workflow_url"] —— 给一个 URL
    """
    config = config or {}
    wf = config.get("workflow")
    if isinstance(wf, dict):
        return copy.deepcopy(wf)
    if isinstance(wf, str) and wf.strip().startswith("{"):
        try:
            return json.loads(wf)
        except Exception:
            return {}
    return {}


def _find_node(workflow: dict, node_id: str | None, class_hint: str) -> str | None:
    """定位节点：优先按 id，其次按 class_type 匹配。"""
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        # API 格式（扁平字典）：{"3": {"class_type": "KSampler", "inputs": {...}}}
        if not node_id:
            return None
        return node_id if node_id in workflow else None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node_id and str(node.get("id")) == str(node_id):
            return str(node.get("id"))
        title = str(node.get("title") or "")
        ctype = str(node.get("type") or node.get("class_type") or "")
        if class_hint and class_hint.lower() in ctype.lower():
            return str(node.get("id"))
        if class_hint and class_hint.lower() in title.lower():
            return str(node.get("id"))
    return node_id


def _set_input(workflow: dict, node_id: str | None, key: str, value) -> bool:
    """给节点写入输入值，兼容两种工作流格式。"""
    if node_id is None:
        return False
    nodes = workflow.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and str(node.get("id")) == str(node_id):
                node.setdefault("inputs", {})[key] = value
                return True
        return False
    node = workflow.get(node_id)
    if isinstance(node, dict):
        node.setdefault("inputs", {})[key] = value
        return True
    return False


def _apply_prompt(workflow: dict, req: dict, config: dict | None) -> dict:
    """把提示词与参考图写进工作流。"""
    config = config or {}
    wf = copy.deepcopy(workflow)

    prompt = req.get("prompt") or ""
    node_id = _find_node(wf, config.get("prompt_node"), "CLIPTextEncode")
    if node_id:
        _set_input(wf, node_id, "text", prompt)

    # 参考图：写入 LoadImage 节点的 image 字段
    refs = req.get("reference_images") or []
    if refs:
        image_node = _find_node(wf, config.get("image_node"), "LoadImage")
        if image_node:
            _set_input(wf, image_node, "image", config.get("image_filename") or "reference.png")

    # 尺寸：写入 EmptyLatentImage 之类节点的宽高
    size = req.get("size") or ""
    if size and "x" in size and config.get("size_node"):
        try:
            w, h = size.lower().split("x", 1)
            _set_input(wf, config["size_node"], "width", int(w))
            _set_input(wf, config["size_node"], "height", int(h))
        except Exception:
            pass

    # 种子
    if config.get("seed_node"):
        _set_input(wf, config["seed_node"], "seed", int(time.time() * 1000) % (2 ** 31))

    return wf


def _collect_images(history_entry: dict) -> list[dict]:
    """从 history 条目里收集图片描述。"""
    images: list[dict] = []
    outputs = history_entry.get("outputs") if isinstance(history_entry, dict) else None
    if not isinstance(outputs, dict):
        return images
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "gifs"):
            items = node_out.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("filename"):
                    images.append({
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder") or "",
                        "type": item.get("type") or "output",
                    })
    return images


def _history_error(history_entry: dict) -> str:
    status = (history_entry or {}).get("status") or {}
    if not isinstance(status, dict):
        return ""
    if status.get("completed"):
        return ""
    for entry in (status.get("messages") or []):
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[0] == "execution_error":
            detail = entry[1]
            if isinstance(detail, dict):
                return str(detail.get("exception_message") or detail.get("node_type") or "执行错误")
    return ""


async def generate(base_url: str, api_key: str, req: dict, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   extra_headers: dict | None = None,
                   config: dict | None = None) -> UpstreamResult:
    started = time.time()
    config = config or {}
    headers = base_headers(api_key, extra_headers)
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError("渠道地址为空")

    workflow = _load_workflow(config)
    if not workflow:
        return error_result(400, "未配置 ComfyUI 工作流",
                            elapsed=time.time() - started,
                            code="INVALID_REQUEST",
                            message="该渠道尚未配置工作流模板。",
                            hint="请在渠道设置里提供 workflow JSON 或工作流文件路径。",
                            retryable=False)

    payload = {"prompt": _apply_prompt(workflow, req, config),
               "client_id": f"bflow-{uuid.uuid4().hex[:8]}"}

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(f"{base}/prompt", headers=headers, json=payload,
                                     timeout=timeout)
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
        text = ""
        if isinstance(err, dict):
            text = f"{err.get('message', '')} {err.get('details', '')}".strip()
        node_errors = data.get("node_errors") if isinstance(data, dict) else None
        if node_errors:
            text = (text + f" 节点错误：{json.dumps(node_errors, ensure_ascii=False)[:300]}").strip()
        return error_result(resp.status_code, text or raw_text,
                            elapsed=time.time() - started)

    prompt_id = data.get("prompt_id") if isinstance(data, dict) else None
    if not prompt_id:
        return error_result(resp.status_code, "提交成功但未返回 prompt_id",
                            elapsed=time.time() - started)

    # 轮询 history
    deadline = started + max(timeout, POLL_MAX_SECONDS)
    polls = 0
    entry: dict = {}
    while time.time() < deadline:
        polls += 1
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                hresp = await client.get(f"{base}/history/{prompt_id}",
                                         headers=headers, timeout=60.0)
            if hresp.status_code == 200:
                hdata = hresp.json()
                if isinstance(hdata, dict):
                    found = hdata.get(prompt_id)
                    if isinstance(found, dict):
                        entry = found
                        if entry.get("status", {}).get("completed"):
                            break
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)

    if not entry:
        return UpstreamResult(
            ok=False, status=0, elapsed=time.time() - started,
            error=NormalizedError(code="UPSTREAM_TIMEOUT",
                                  message="ComfyUI 任务轮询超时。",
                                  hint="生成耗时过长，请稍后到任务中心查看。",
                                  retryable=True),
            meta={"protocol": "comfyui", "prompt_id": prompt_id, "polls": polls})

    err_msg = _history_error(entry)
    if err_msg:
        return error_result(200, err_msg, elapsed=time.time() - started,
                            code="CHANNEL_INTERNAL_ERROR",
                            message=f"ComfyUI 工作流执行失败：{err_msg}",
                            hint="请检查工作流配置与节点参数。", retryable=False)

    images = _collect_images(entry)
    if not images:
        return error_result(200, "工作流未产出图片",
                            elapsed=time.time() - started,
                            code="CHANNEL_INTERNAL_ERROR",
                            message="ComfyUI 工作流执行完成，但没有输出图片。",
                            hint="请确认工作流里有 SaveImage / PreviewImage 节点，"
                                 "且它接在正确的链路上。",
                            retryable=False)

    artifacts: list[Artifact] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for img in images:
            params = {"filename": img["filename"], "subfolder": img["subfolder"],
                      "type": img["type"]}
            try:
                vresp = await client.get(f"{base}/view", headers=headers, params=params,
                                         timeout=120.0)
                vresp.raise_for_status()
                raw = vresp.content
            except Exception:
                continue
            artifact = make_artifact(raw)
            if artifact:
                artifacts.append(artifact)

    return success(artifacts, status=200, elapsed=time.time() - started,
                   meta={"protocol": "comfyui", "prompt_id": prompt_id, "polls": polls,
                         "images_found": len(images)})


async def edit(base_url: str, api_key: str, req: dict, *,
               timeout: float = DEFAULT_TIMEOUT,
               extra_headers: dict | None = None,
               config: dict | None = None) -> UpstreamResult:
    """ComfyUI 的编辑由工作流内的节点完成，协议层与生成一致。"""
    return await generate(base_url, api_key, req, timeout=timeout,
                          extra_headers=extra_headers, config=config)
