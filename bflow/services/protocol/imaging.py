"""产物格式与尺寸的**魔数解析**。

这是 Bflow 最核心的一条规则（见 spec/01-platform-behavior.md §6.1 F-2）：
渠道声明的 mime / 扩展名不可信 —— 实测见过「请求 png 返回 webp」
「请求 webp 返回 png」。因此产物格式一律以文件头字节判定。

同时提供 SVG 的尺寸提取（矢量图没有像素尺寸，需要从 viewBox/width/height 取）。
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

SUPPORTED_FORMATS = ("png", "jpeg", "webp", "gif", "svg", "bmp", "tif")

# 与前端 FORMAT_LABELS 保持一致
FORMAT_LABELS = {
    "png": "PNG", "jpeg": "JPEG", "webp": "WebP",
    "svg": "SVG", "gif": "GIF", "bmp": "BMP", "tif": "TIFF",
}


@dataclass(frozen=True)
class ImageInfo:
    """产物解析结果。"""
    format: str
    width: int
    height: int
    bytes: int
    sha256: str

    @property
    def size_label(self) -> str:
        return f"{self.width}x{self.height}" if self.width else ""


def format_label(fmt: str) -> str:
    return FORMAT_LABELS.get((fmt or "").lower(), (fmt or "").upper())


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_format(data: bytes) -> str:
    """按魔数判定图片格式；判定不出返回空串。"""
    if not data or len(data) < 12:
        return ""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    # SVG 是文本容器，只能靠内容嗅探（限制在前 1KB 内查找）
    probe = data[:2048].lstrip()
    if probe.startswith(b"<?xml") or probe.startswith(b"<svg"):
        if b"<svg" in data[:4096].lower():
            return "svg"
    return ""


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return 0, 0


def _gif_size(data: bytes) -> tuple[int, int]:
    if len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    return 0, 0


def _webp_size(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        return 0, 0
    chunk = data[12:16]
    try:
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L":
            b = data[21:25]
            bits = b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
    except Exception:
        pass
    return 0, 0


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        if i + 4 > n:
            break
        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg
    return 0, 0


_SVG_W = re.compile(rb'\bwidth\s*=\s*["\']?\s*([0-9.]+)', re.I)
_SVG_H = re.compile(rb'\bheight\s*=\s*["\']?\s*([0-9.]+)', re.I)
_SVG_VIEWBOX = re.compile(
    rb'\bviewBox\s*=\s*["\']\s*([0-9.-]+)[,\s]+([0-9.-]+)[,\s]+([0-9.]+)[,\s]+([0-9.]+)', re.I)


def _svg_size(data: bytes) -> tuple[int, int]:
    head = data[:8192]
    w_m = _SVG_W.search(head)
    h_m = _SVG_H.search(head)
    if w_m and h_m:
        try:
            return int(float(w_m.group(1))), int(float(h_m.group(1)))
        except ValueError:
            pass
    vb = _SVG_VIEWBOX.search(head)
    if vb:
        try:
            return int(float(vb.group(3))), int(float(vb.group(4)))
        except ValueError:
            pass
    return 0, 0


def parse_image(data: bytes) -> ImageInfo:
    """解析产物：格式、尺寸、字节数、校验和。

    格式判定完全基于文件头，不依赖任何外部声明。
    """
    fmt = sniff_format(data) or "unknown"
    if fmt == "png":
        w, h = _png_size(data)
    elif fmt == "gif":
        w, h = _gif_size(data)
    elif fmt == "webp":
        w, h = _webp_size(data)
    elif fmt == "jpeg":
        w, h = _jpeg_size(data)
    elif fmt == "svg":
        w, h = _svg_size(data)
    else:
        w, h = 0, 0
    return ImageInfo(format=fmt, width=w, height=h,
                     bytes=len(data), sha256=sha256_of(data))
