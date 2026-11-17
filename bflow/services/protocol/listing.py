"""模型列表拉取。

为什么需要它：聚合网关（newapi / sub2api / openrouter 之类）把多个厂商的
模型聚在一个接入点下。它的模型列表里混着不同厂商的模型 —— gpt-image-* 要走
OpenAI 协议、gemini-*-image 要走 Gemini 协议、doubao-seedream-* 要走方舟协议。
用户需要先把上游的模型列表拉下来，再逐个指定协议。

拉取本身**不产生生成费用**（只是列目录），但会消耗一次 API 配额。

协议差异：
- OpenAI 兼容（newapi / sub2api / openrouter / 大多数 one-api 系）：
  `GET {base}/v1/models` → `{"data": [{"id": "..."}]}`
- Gemini：`GET {base}/v1beta/models` → `{"models": [{"name": "models/xxx"}]}`
- 方舟 / APIMart / 工作流类：没有统一的模型目录端点，或目录与生图无关，
  由用户手工填写。

因此本模块把「拉目录」和「选协议」分开：拉取只负责给出模型名，
协议由用户逐个指定（因为同一个模型在不同网关上的协议可能不同）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import base_headers, endpoint_url


@dataclass
class ModelListing:
    """一次模型目录拉取的结果。"""
    ok: bool
    models: list[str] = field(default_factory=list)
    status: int = 0
    error: str = ""
    hint: str = ""
    raw_body: str = ""
    elapsed: float = 0.0
    # 拉取时上游附带的部分元信息（owned_by 等），仅供展示
    meta: dict = field(default_factory=dict)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in items:
        name = (name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _parse_openai_listing(data: Any) -> list[str]:
    """解析 OpenAI 兼容的模型列表：{"data": [{"id": "..."}]}。"""
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        # 少数网关直接返回数组
        items = data if isinstance(data, list) else []
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if name:
                out.append(str(name))
    return out


def _parse_gemini_listing(data: Any) -> list[str]:
    """解析 Gemini 的模型列表：{"models": [{"name": "models/gemini-..."}]}。"""
    items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id") or ""
        if not name:
            continue
        # Gemini 返回 "models/gemini-2.5-flash" —— 去掉前缀，
        # 因为生图端点用的是不带前缀的模型名
        out.append(str(name).split("/")[-1] if "/" in str(name) else str(name))
    return out


async def fetch_models(base_url: str, api_key: str, *,
                       protocol: str = "openai-images",
                       extra_headers: dict | None = None,
                       config: dict | None = None,
                       timeout: float = 30.0) -> ModelListing:
    """拉取上游模型列表。

    protocol 决定用哪种方言解析响应 —— 同一个网关地址，按 openai 兼容
    方言解析通常就对（网关会把它代理的各家模型都列在 /v1/models 里）。
    """
    started = time.time()
    config = config or {}

    if protocol == "gemini":
        url = endpoint_url(base_url, "models", version="v1beta")
        headers = base_headers(api_key, extra_headers)
        # Gemini 用 x-goog-api-key 而不是 Authorization
        headers.pop("Authorization", None)
        if api_key:
            headers["x-goog-api-key"] = api_key
        parser = _parse_gemini_listing
    else:
        url = endpoint_url(base_url, "models", version="v1")
        headers = base_headers(api_key, extra_headers)
        parser = _parse_openai_listing

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return ModelListing(ok=False, status=0, elapsed=time.time() - started,
                            error="拉取模型列表超时。",
                            hint="请检查渠道地址是否可达，或稍后重试。")
    except Exception as exc:
        return ModelListing(ok=False, status=0, elapsed=time.time() - started,
                            error=f"无法连接渠道：{type(exc).__name__}",
                            hint="请检查渠道地址与网络连通性。")

    elapsed = time.time() - started
    raw_text = resp.text or ""

    if resp.status_code != 200:
        return ModelListing(
            ok=False, status=resp.status_code, elapsed=elapsed,
            raw_body=raw_text[:600],
            error=f"上游返回 {resp.status_code}。",
            hint=("请确认渠道地址正确、密钥有效，且该网关开放了模型列表端点。"
                  "若该渠道不支持拉取模型，可手工填写模型名。"))

    try:
        data = resp.json()
    except Exception:
        return ModelListing(ok=False, status=resp.status_code, elapsed=elapsed,
                            raw_body=raw_text[:600],
                            error="上游返回了无法解析的内容。",
                            hint="该端点可能不是模型列表接口，可手工填写模型名。")

    models = _dedupe(parser(data))
    if not models:
        return ModelListing(
            ok=False, status=resp.status_code, elapsed=elapsed,
            raw_body=raw_text[:600],
            error="上游没有返回任何模型。",
            hint="请确认该渠道的模型目录非空，或手工填写模型名。")

    return ModelListing(ok=True, models=models, status=resp.status_code,
                        elapsed=elapsed, raw_body=raw_text[:600])


def classify_model(model_name: str) -> str:
    """把上游模型粗分成 image / chat / video —— 只用于展示筛选。

    目录接口本身不返回用途，只能按模型名里的通用关键词猜测；
    这只是**筛选建议**，用户仍可在选择器里改归属，且不影响协议解析。
    """
    lc = (model_name or "").lower()
    video_keys = (
        "veo", "sora", "wan2", "wanx", "seedance", "kling", "hailuo",
        "video", "t2v-", "i2v-", "s2v",
    )
    if any(k in lc for k in video_keys):
        return "video"
    image_keys = (
        "banana", "image", "dalle", "dall-e", "imagen", "flux", "stable",
        "sdxl", "midjourney", "ideogram", "z-image", "qwen-image", "klein",
        "seedream", "text-to-image", "image-to-image",
    )
    if any(k in lc for k in image_keys):
        return "image"
    return "chat"


def guess_protocol(model_name: str, default: str = "openai-images") -> str:
    """按模型名猜协议 —— 只是**建议**，最终由用户确认。

    聚合网关里的模型名通常带厂商前缀或特征：
      gpt-image-* / dall-e-*   → OpenAI 协议
      gemini-*-image           → Gemini 协议
      doubao-* / seedream-*    → 方舟协议
      flux-* / sd-*            → 工作流协议（ComfyUI / RunningHub）
    """
    name = (model_name or "").lower()
    if "gemini" in name:
        return "gemini"
    if "doubao" in name or "seedream" in name or "seededit" in name:
        return "ark"
    if name.startswith("flux") or name.startswith("sd") or "-flux" in name:
        return "comfyui"
    return default
