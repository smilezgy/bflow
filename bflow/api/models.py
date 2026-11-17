"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="提示词")
    channel_id: str
    model: str
    size: str = "1024x1024"
    count: int = Field(1, ge=1, le=4)
    output_format: Literal["png", "jpeg", "webp", "svg", "gif"] = "png"
    output_compression: int | None = Field(None, ge=0, le=100)
    background: Literal["", "auto", "transparent", "opaque"] = ""
    quality: str = ""
    complexity: str = ""
    frame_count: int | None = Field(None, ge=1, le=16)
    frame_duration_ms: int | None = Field(None, ge=50, le=5000)
    reference_images: list[str] = Field(default_factory=list)
    masks: list[str] = Field(default_factory=list)
    save_to_library: bool = True


class ChannelCreate(BaseModel):
    id: str | None = None
    display_name: str = ""
    protocol: str = "openai-images"
    base_url: str
    api_key: str = ""
    enabled: bool = True
    models: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class ChannelPatch(BaseModel):
    display_name: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = Field(
        None, description="人工覆盖能力表；按模型给出")
    capability_model: str = ""


class CapabilityUpdate(BaseModel):
    model: str
    capability: dict[str, Any]


class ModelBinding(BaseModel):
    """一个模型的协议绑定。"""
    model: str
    protocol: str = ""
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class ModelBindingUpdate(BaseModel):
    """批量设置模型协议绑定。"""
    models: list[ModelBinding] = Field(default_factory=list)


class ModelFetchRequest(BaseModel):
    """拉取上游模型列表。"""
    protocol: str = ""
    # 是否把拉到的模型直接登记进渠道（默认只预览，不落库）
    apply: bool = False
    # 未显式指定协议时，是否按模型名自动推断
    auto_protocol: bool = True


class CanvasCreate(BaseModel):
    title: str = "未命名画布"


class CanvasSave(BaseModel):
    title: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class CanvasRename(BaseModel):
    title: str


class AssetTagUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list)


class StorageSettings(BaseModel):
    data_root: str
