"""协议注册表。

渠道的 `protocol` 字段决定用哪个适配器。新增协议只需：
1. 在本文件注册元信息（标签、默认能力表、支持的操作）；
2. 在适配器模块里实现 `generate` / `edit` 两个协程。

这样 `services/generation.py` 与 API 层都不需要为每个协议写分支 ——
协议差异被收敛在适配器内部。

关于默认能力表：每个协议的「天然能力」不同。
例如 ComfyUI 的产物格式由工作流决定，因此 `honors_request` 必须是 false；
Gemini 不接受 `output_format`，只有 PNG；方舟的尺寸语义是像素串或 2K/4K 等级。
把这些如实写进默认能力表，前端才能正确地显示选项与提示。
"""

from __future__ import annotations

from typing import Any

from . import apimart, ark, comfyui, gemini, openai_images, runninghub

# 通用尺寸边界（实测跨渠道一致，属模型层约束）
PIXEL_BOUNDS = {"min_pixels": 655360, "max_pixels": 8294400, "max_edge": 3840}


def _fmt(supported: bool, honors: bool) -> dict:
    return {"supported": supported, "honors_request": honors}


def _base_capability(**overrides: Any) -> dict:
    cap: dict[str, Any] = {
        "output_formats": {
            "png": _fmt(True, True),
            "jpeg": _fmt(True, True),
        },
        "size_policy": "reject",
        "size_bounds": dict(PIXEL_BOUNDS),
        "quality_levels": ["auto"],
        "complexity_levels": ["auto"],
        "backgrounds": ["auto"],
        "transparent_formats": ["png", "webp"],
        "compression_by_format": {"png": None, "jpeg": [0, 100], "webp": [0, 100]},
        "ignores_unknown_params": True,
        "edits": {
            "supported": False,
            "max_reference_images": 0,
            "supports_mask": False,
            "validates_mask_content": False,
            "accepts_input_fidelity": False,
        },
        "notes": [],
    }
    cap.update(overrides)
    return cap


PROTOCOLS: dict[str, dict[str, Any]] = {
    "openai-images": {
        "label": "OpenAI Images",
        "adapter": openai_images,
        "supports_edit": True,
        "hint": "兼容 OpenAI Images 协议（/v1/images/generations 与 /v1/images/edits）。",
        "capability": _base_capability(
            output_formats={
                "png": _fmt(True, True), "jpeg": _fmt(True, True),
                "webp": _fmt(True, True), "svg": _fmt(True, True),
                "gif": _fmt(True, True),
            },
            quality_levels=["auto", "low", "medium", "high", "xhigh", "max"],
            complexity_levels=["auto", "low", "medium", "high"],
            backgrounds=["auto", "transparent", "opaque"],
            edits={"supported": True, "max_reference_images": 3,
                   "supports_mask": True, "validates_mask_content": False,
                   "accepts_input_fidelity": True},
        ),
    },
    "gemini": {
        "label": "Gemini（Google）",
        "adapter": gemini,
        "supports_edit": True,
        "hint": "Google Generative Language 协议；尺寸用宽高比表达，产物格式由模型固定。",
        "capability": _base_capability(
            output_formats={"png": _fmt(True, True), "jpeg": _fmt(True, False)},
            size_policy="fixed",
            backgrounds=["auto"],
            transparent_formats=["png"],
            edits={"supported": True, "max_reference_images": 1,
                   "supports_mask": False, "validates_mask_content": False,
                   "accepts_input_fidelity": False},
            notes=["Gemini 不接受 output_format / quality / complexity 参数。"],
        ),
    },
    "ark": {
        "label": "火山方舟（Ark）",
        "adapter": ark,
        "supports_edit": True,
        "hint": "提交后轮询任务；尺寸支持像素串或 2K/4K 等级；参考图以 base64 传入。",
        "capability": _base_capability(
            output_formats={"png": _fmt(True, False), "jpeg": _fmt(True, False)},
            backgrounds=["auto"],
            transparent_formats=[],
            edits={"supported": True, "max_reference_images": 10,
                   "supports_mask": True, "validates_mask_content": False,
                   "accepts_input_fidelity": False},
            notes=["方舟为异步任务协议，生成期间会持续轮询。"],
        ),
    },
    "apimart": {
        "label": "APIMart",
        "adapter": apimart,
        "supports_edit": True,
        "hint": "异步任务协议；参数按模型不同，通过渠道配置透传。",
        "capability": _base_capability(
            output_formats={"png": _fmt(True, False), "jpeg": _fmt(True, False)},
            backgrounds=["auto"],
            transparent_formats=[],
            edits={"supported": True, "max_reference_images": 4,
                   "supports_mask": False, "validates_mask_content": False,
                   "accepts_input_fidelity": False},
            notes=["APIMart 为异步任务协议，生成期间会持续轮询。"],
        ),
    },
    "comfyui": {
        "label": "ComfyUI 工作流",
        "adapter": comfyui,
        "supports_edit": True,
        "hint": "需要提供工作流 JSON 模板；格式与尺寸由工作流决定。",
        "capability": _base_capability(
            output_formats={"png": _fmt(True, False), "jpeg": _fmt(True, False),
                            "webp": _fmt(True, False)},
            size_policy="fixed",
            backgrounds=["auto"],
            transparent_formats=[],
            edits={"supported": True, "max_reference_images": 4,
                   "supports_mask": False, "validates_mask_content": False,
                   "accepts_input_fidelity": False},
            notes=["ComfyUI 的产物格式与尺寸由工作流决定，请求参数只影响工作流输入。"],
        ),
    },
    "runninghub": {
        "label": "RunningHub 工作流",
        "adapter": runninghub,
        "supports_edit": True,
        "hint": "需要提供 workflow_id 与提示词节点 id；格式与尺寸由工作流决定。",
        "capability": _base_capability(
            output_formats={"png": _fmt(True, False), "jpeg": _fmt(True, False),
                            "webp": _fmt(True, False)},
            size_policy="fixed",
            backgrounds=["auto"],
            transparent_formats=[],
            edits={"supported": True, "max_reference_images": 4,
                   "supports_mask": False, "validates_mask_content": False,
                   "accepts_input_fidelity": False},
            notes=["RunningHub 为异步任务协议，生成期间会持续轮询。"],
        ),
    },
}

DEFAULT_PROTOCOL = "openai-images"


def is_known(protocol: str) -> bool:
    return (protocol or "") in PROTOCOLS


def label(protocol: str) -> str:
    meta = PROTOCOLS.get(protocol or "")
    return meta["label"] if meta else (protocol or DEFAULT_PROTOCOL)


def adapter(protocol: str):
    """取适配器模块；未知协议回落到默认协议。"""
    meta = PROTOCOLS.get(protocol or "")
    if not meta:
        meta = PROTOCOLS[DEFAULT_PROTOCOL]
    return meta["adapter"]


def default_capability(protocol: str) -> dict:
    """该协议的默认能力表（用户未实测时使用）。"""
    meta = PROTOCOLS.get(protocol or "") or PROTOCOLS[DEFAULT_PROTOCOL]
    cap = dict(meta["capability"])
    cap["source"] = "default"
    cap["verified_at"] = None
    return cap


def supports_edit(protocol: str) -> bool:
    meta = PROTOCOLS.get(protocol or "")
    return bool(meta["supports_edit"]) if meta else True


def catalog() -> list[dict]:
    """给前端用的协议清单。"""
    out = []
    for key, meta in PROTOCOLS.items():
        out.append({
            "id": key,
            "label": meta["label"],
            "hint": meta.get("hint", ""),
            "supports_edit": meta["supports_edit"],
        })
    return out


async def dispatch(protocol: str, base_url: str, api_key: str, req: dict, *,
                   timeout: float = 300.0, extra_headers: dict | None = None,
                   config: dict | None = None, with_reference: bool = False):
    """按协议分发一次生成请求。

    with_reference 表示带参考图 —— 决定走 generate 还是 edit。
    """
    mod = adapter(protocol)
    if with_reference and hasattr(mod, "edit"):
        return await mod.edit(base_url, api_key, req, timeout=timeout,
                              extra_headers=extra_headers, config=config)
    return await mod.generate(base_url, api_key, req, timeout=timeout,
                              extra_headers=extra_headers, config=config)
