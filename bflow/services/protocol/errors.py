"""上游错误归一化。

各渠道的报错风格差异极大（英文 / 中文 / 结构不一 / 有的只有一句话），
实测样本见 spec/01-platform-behavior.md §0「通用错误报文样本」。

本模块把上游原文映射为 Bflow 的统一错误码，并给出**可执行**的中文提示。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class NormalizedError:
    """归一化后的错误。"""
    code: str
    message: str
    hint: str = ""
    retryable: bool = False
    detail: dict = field(default_factory=dict)
    raw: str = ""


# 错误码表见 spec/03-api-contract.md §0.3
CODE_INVALID_REQUEST = "INVALID_REQUEST"
CODE_FORMAT_NOT_SUPPORTED = "FORMAT_NOT_SUPPORTED"
CODE_SIZE_OUT_OF_RANGE = "SIZE_OUT_OF_RANGE"
CODE_CONTENT_REJECTED = "CONTENT_REJECTED"
CODE_CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"
CODE_CHANNEL_INTERNAL_ERROR = "CHANNEL_INTERNAL_ERROR"
CODE_UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_REFERENCE_TOO_MANY = "REFERENCE_TOO_MANY"
CODE_NO_REFERENCE_IMAGE = "NO_REFERENCE_IMAGE"

# 格式值 → 中文标签（复用 imaging 里的映射）
_FORMAT_NAMES = {"svg": "SVG", "gif": "GIF", "webp": "WebP",
                 "png": "PNG", "jpeg": "JPEG"}


def _fmt_name(value: str) -> str:
    return _FORMAT_NAMES.get(value.strip().lower(), value.upper())


# --- 报文特征识别 -------------------------------------------------------

# 「SVG output is not available in this key's group ...」
_RE_FORMAT_UNAVAILABLE = re.compile(
    r"\b(svg|gif|webp|jpeg|png)\s+(?:output|format|image)\b"
    r"[^.]{0,120}?\b(?:not\s+available|not\s+supported|unavailable|not\s+enabled)\b", re.I)

_RE_FORMAT_NOT_SUPPORTED = re.compile(
    r"\b(?:does\s+not\s+support|unsupported|not\s+supported|invalid)\b"
    r"[^.]{0,60}?\b(svg|gif|webp|jpeg|png)\b", re.I)

# 「SVG output could not be generated for this prompt ...」（内容限制）
_RE_CONTENT_REJECTED = re.compile(
    r"(svg|gif|webp)?\s*output\s+could\s+not\s+be\s+generated", re.I)

# 「Requested resolution is below the current minimum pixel budget.」
_RE_SIZE_BELOW = re.compile(
    r"(below\s+the\s+current\s+minimum|minimum\s+pixel\s+budget|"
    r"总像素必须在|像素下限)", re.I)

# 「Requested resolution exceeds the current pixel budget.」
_RE_SIZE_ABOVE = re.compile(
    r"(exceeds\s+the\s+current\s+pixel\s+budget|maximum\s+pixel\s+budget|"
    r"像素上限|总像素必须在)", re.I)

_RE_MODEL_NOT_FOUND = re.compile(
    r"(model_not_found|no\s+available\s+channel|无可用渠道|"
    r"模型.*(不存在|未开通|无通道))", re.I)

_RE_UPSTREAM_UNAVAILABLE = re.compile(
    r"(temporarily\s+unavailable|upstream_error|服务不可用|暂时不可用)", re.I)

_RE_INTERNAL = re.compile(
    r"(internal_server_error|服务内部错误|internal\s+error)", re.I)

_RE_TIMEOUT = re.compile(r"(timeout|timed\s+out|超时)", re.I)

_RE_RATE_LIMIT = re.compile(r"(rate.?limit|too\s+many\s+requests|限流|429)", re.I)

_RE_COMPRESSION = re.compile(
    r"compression\s+less\s+than\s+100|不支持\s*小于\s*100\s*的压缩率", re.I)

_RE_NO_CHANNEL_ROUTE = re.compile(r"(找不到模型|无可用通道|no\s+available\s+route)", re.I)


def normalize(status_code: int, body: str, *,
              requested_format: str = "",
              requested_size: str = "",
              raw_error: str = "") -> NormalizedError:
    """把上游 (状态码, 报文) 映射为统一错误。

    识别顺序按「特异性」从高到低：格式 → 内容 → 尺寸 → 通道 → 内部错误。
    """
    text = (body or "").strip()
    raw = raw_error or text

    # 1) 明确的通道/模型问题 —— 放最前，因为它常伴随 5xx
    if _RE_MODEL_NOT_FOUND.search(text) or _RE_NO_CHANNEL_ROUTE.search(text):
        return NormalizedError(
            code=CODE_CHANNEL_UNAVAILABLE,
            message="该渠道当前没有此模型的可用通道。",
            hint="可能是渠道侧分组未开通或通道临时下线，请稍后重试或更换渠道。",
            retryable=True,
            detail={"upstream_status": status_code},
            raw=raw[:600],
        )

    # 2) 格式不支持（400）
    if status_code in (400, 422):
        m = _RE_FORMAT_UNAVAILABLE.search(text) or _RE_FORMAT_NOT_SUPPORTED.search(text)
        if m:
            fmt = _fmt_name(m.group(1)) if m.groups() else _fmt_name(requested_format)
            return NormalizedError(
                code=CODE_FORMAT_NOT_SUPPORTED,
                message=f"该渠道不支持「{fmt}」输出格式。",
                hint=("建议：① 把输出格式改成 PNG 或 WebP；"
                      "② 或换一个支持该格式的渠道。"),
                retryable=False,
                detail={"format": fmt.lower()},
                raw=raw[:600],
            )

        # 内容限制
        if _RE_CONTENT_REJECTED.search(text):
            fmt = _fmt_name(requested_format) or "该格式"
            return NormalizedError(
                code=CODE_CONTENT_REJECTED,
                message=f"上游拒绝了本次 {fmt} 生成请求。",
                hint=("SVG 更适合图标、Logo、扁平插画这类内容；"
                      "写实照片建议改用 PNG，"
                      "或在提示词中加入「扁平矢量插画、简单形状」等描述。"),
                retryable=False,
                detail={"format": requested_format},
                raw=raw[:600],
            )

        # 尺寸越界
        if _RE_SIZE_BELOW.search(text) or _RE_SIZE_ABOVE.search(text):
            direction = "below" if _RE_SIZE_BELOW.search(text) else "above"
            if "总像素必须在" in text:
                # 渠道直接给出了区间（实测：总像素必须在 655360 到 8294400 之间）
                rng = re.search(r"总像素必须在\s*(\d+)\s*到\s*(\d+)\s*之间", text)
                if rng:
                    lo, hi = rng.group(1), rng.group(2)
                    return NormalizedError(
                        code=CODE_SIZE_OUT_OF_RANGE,
                        message=(f"尺寸 {requested_size or '所选'} 超出该渠道允许范围"
                                 f"（总像素需在 {lo} 到 {hi} 之间）。"),
                        hint="请调整尺寸后重试。",
                        retryable=False,
                        detail={"min_pixels": int(lo), "max_pixels": int(hi)},
                        raw=raw[:600],
                    )
            return NormalizedError(
                code=CODE_SIZE_OUT_OF_RANGE,
                message=(f"尺寸 {requested_size or '所选'} 超出该渠道允许范围"
                         + ("（低于下限）。" if direction == "below" else "（超��上限）。")),
                hint="请调整尺寸后重试。",
                retryable=False,
                detail={"direction": direction},
                raw=raw[:600],
            )

        # 压缩率冲突
        if _RE_COMPRESSION.search(text):
            return NormalizedError(
                code=CODE_INVALID_REQUEST,
                message="PNG 不接受小于 100 的压缩率。",
                hint="请把压缩质量调到 100，或改用 JPEG / WebP。",
                retryable=False,
                raw=raw[:600],
            )

        # 参考图数量超限
        if re.search(r"(too\s+many\s+images|最多\s*\d+\s*张|参考图.*超出)", text, re.I):
            return NormalizedError(
                code=CODE_REFERENCE_TOO_MANY,
                message="参考图数量超出该渠道允许的上限。",
                hint="请减少参考图后重试。",
                retryable=False,
                raw=raw[:600],
            )

        # 其余 400：参数不合法
        return NormalizedError(
            code=CODE_INVALID_REQUEST,
            message="该渠道拒绝了本次请求。",
            hint="请检查参数设置；原始报错见详情。",
            retryable=False,
            detail={"upstream_status": status_code},
            raw=raw[:600],
        )

    # 3) 429 限流
    if status_code == 429 or _RE_RATE_LIMIT.search(text):
        return NormalizedError(
            code=CODE_RATE_LIMITED,
            message="请求过于频繁，已被渠道限流。",
            hint="请稍候再试，或降低并发。",
            retryable=True,
            raw=raw[:600],
        )

    # 4) 5xx
    if status_code >= 500:
        if _RE_INTERNAL.search(text):
            return NormalizedError(
                code=CODE_CHANNEL_INTERNAL_ERROR,
                message="渠道内部错误。",
                hint="这是渠道侧的问题，可稍后重试；持续失败请更换渠道。",
                retryable=True,
                raw=raw[:600],
            )
        if _RE_UPSTREAM_UNAVAILABLE.search(text):
            return NormalizedError(
                code=CODE_CHANNEL_UNAVAILABLE,
                message="该渠道上游暂时不可用。",
                hint="请稍后重试，或更换渠道。",
                retryable=True,
                raw=raw[:600],
            )
        return NormalizedError(
            code=CODE_CHANNEL_INTERNAL_ERROR,
            message=f"渠道返回 {status_code}。",
            hint="这是渠道侧的问题，可稍后重试。",
            retryable=True,
            detail={"upstream_status": status_code},
            raw=raw[:600],
        )

    if _RE_TIMEOUT.search(text):
        return NormalizedError(
            code=CODE_UPSTREAM_TIMEOUT,
            message="上游响应超时。",
            hint="请稍后重试。",
            retryable=True,
            raw=raw[:600],
        )

    return NormalizedError(
        code=CODE_CHANNEL_INTERNAL_ERROR,
        message=f"渠道返回 {status_code}。",
        hint="请稍后重试，或更换渠道。",
        retryable=True,
        detail={"upstream_status": status_code},
        raw=raw[:600],
    )
