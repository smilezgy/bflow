"""协议适配层公共设施。

各协议的差异只在「怎么发请求、产物从哪取」；而「产物怎么判定格式」、
「怎么把上游错误归一化」、「怎么检测偏差」是**所有协议共享**的规则，
因此集中在这里，避免每个适配器各写一份（那必然会出现不一致）。

核心规则不变（spec/01-platform-behavior.md §6.1）：
- 产物格式与尺寸一律按魔数判定，不信任上游声明；
- 产物与请求不符时**交付产物 + 产出警告**，绝不丢弃（上游已计费）；
- 上游明确拒绝时直接报错，不重试、不换格式。
"""

from __future__ import annotations

import time
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
    # 协议特有信息（异步任务 id、工作流 id、轮询次数等），供任务记录留痕
    meta: dict = field(default_factory=dict)


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


# ------------------------------------------------------------------ 工具


def make_artifact(raw: bytes, source_url: str = "") -> Artifact | None:
    """由原始字节构造产物；超限或为空返回 None。"""
    if not raw or len(raw) > MAX_ARTIFACT_BYTES:
        return None
    return Artifact(data=raw, info=parse_image(raw), source_url=source_url)


async def download(client: httpx.AsyncClient, url: str,
                   timeout: float = 120.0) -> bytes:
    """下载产物字节。"""
    resp = await client.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def error_result(status: int, body: str, *, elapsed: float = 0.0,
                 requested_format: str = "", requested_size: str = "",
                 code: str = "", message: str = "", hint: str = "",
                 retryable: bool = False) -> UpstreamResult:
    """由上游 (状态码, 报文) 构造失败结果。"""
    if code:
        err = NormalizedError(code=code, message=message, hint=hint,
                              retryable=retryable, raw=body[:600])
    else:
        err = normalize(status, body, requested_format=requested_format,
                        requested_size=requested_size, raw_error=body)
    return UpstreamResult(ok=False, status=status, error=err,
                          raw_body=body[:2000], elapsed=elapsed)


def timeout_result(elapsed: float) -> UpstreamResult:
    return UpstreamResult(
        ok=False, status=0, elapsed=elapsed,
        error=NormalizedError(code="UPSTREAM_TIMEOUT",
                              message="上游响应超时。",
                              hint="请稍后重试。", retryable=True))


def connection_error(exc: Exception, elapsed: float) -> UpstreamResult:
    return UpstreamResult(
        ok=False, status=0, elapsed=elapsed,
        error=NormalizedError(code="CHANNEL_INTERNAL_ERROR",
                              message=f"无法连接渠道：{type(exc).__name__}",
                              hint="请检查渠道地址与网络连通性。",
                              retryable=True))


def success(artifacts: list[Artifact], *, status: int = 200,
            raw_body: str = "", usage: dict | None = None,
            elapsed: float = 0.0, meta: dict | None = None) -> UpstreamResult:
    return UpstreamResult(ok=True, status=status, artifacts=artifacts,
                          raw_body=raw_body, usage=usage or {},
                          elapsed=elapsed, meta=meta or {})


def now() -> float:
    return time.time()


def base_headers(api_key: str, extra: dict | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "Bflow/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


def endpoint_url(base_url: str, path: str, *, versioned: bool = True,
                 version: str = "v1") -> str:
    """拼接端点地址。

    base 可能自带版本段（实测存在 base 已含 /v1 的配置），
    此时不再重复追加版本前缀。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError("渠道地址为空")
    if path.startswith("/"):
        path = path[1:]
    if not versioned:
        return f"{base}/{path}"
    tail = f"/{version}"
    if base.endswith(tail):
        return f"{base}/{path}"
    return f"{base}/{version}/{path}"


async def post_json(client: httpx.AsyncClient, url: str, *, headers: dict,
                    json_body: dict | None = None,
                    form: dict | None = None,
                    files: list | None = None,
                    timeout: float = DEFAULT_TIMEOUT):
    """统一的 POST：JSON / 表单 / multipart。"""
    if json_body is not None:
        return await client.post(url, headers=headers, json=json_body, timeout=timeout)
    if files:
        data = {k: str(v) for k, v in (form or {}).items()}
        multipart = [(name, (fname, content, "application/octet-stream"))
                     for name, fname, content in files]
        return await client.post(url, headers=headers, data=data,
                                 files=multipart, timeout=timeout)
    return await client.post(url, headers=headers, data=form or {}, timeout=timeout)


def as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return {}


# ------------------------------------------------------------------ 轮询


async def poll_until(fetch, is_done, *, interval: float = 2.0,
                     max_seconds: float = 600.0) -> tuple[Any, int, bool]:
    """轮询直到完成。

    fetch: async callable，返回本次的解析结果
    is_done: 判断结果是否已终态
    返回 (最后一次结果, 轮询次数, 是否超时)
    """
    import asyncio
    started = time.time()
    polls = 0
    last = None
    while time.time() - started < max_seconds:
        polls += 1
        last = await fetch()
        if is_done(last):
            return last, polls, False
        await asyncio.sleep(interval)
    return last, polls, True
