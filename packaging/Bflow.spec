# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— Bflow 桌面端。

用法：
    pip install -r requirements-desktop.txt pyinstaller
    pyinstaller packaging/Bflow.spec --noconfirm

产物：
    dist/Bflow/Bflow.exe        （Windows）
    dist/Bflow/Bflow            （Linux / macOS）

说明：
- 前端静态资源与数据库迁移脚本必须打进包里（--add-data 第一个参数是源，
  第二个是包内路径，分隔符在 Windows 用 ';'，其他平台用 ':'）；
- uvicorn 的 loop / protocol / lifespan 都是动态导入，必须显式声明 hiddenimports，
  否则运行时才报错「No module named uvicorn.protocols.http.auto」；
- onedir 而不是 onefile —— 启动更快，也不用每次解压到临时目录。
"""

import sys
from pathlib import Path

# PyInstaller 在 Windows 用 ';'，其他平台用 ':'
sep = ";" if sys.platform.startswith("win") else ":"
ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "static"), f"static{sep}static"),
    (str(ROOT / "migrations"), f"migrations{sep}migrations"),
]

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio",
    "PIL.Image",
    "PIL.ImageOps",
    "cryptography.hazmat.primitives.ciphers.aead",
    "sqlalchemy.dialects.sqlite",
]

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas", "pytest",
        "notebook", "IPython", "jupyter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Bflow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # 保留控制台，便于查看启动日志
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Bflow",
)
