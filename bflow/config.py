"""Bflow —— 应用配置与路径管理。

设计要点（见 spec/04-data-model.md §0）：
- 数据根目录可配置，默认按操作系统惯例选择；
- 密钥只写不读，所有读接口返回掩码；
- 路径全部相对于 <data_root>，便于 Docker 命名卷挂载。
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

APP_NAME = "Bflow"
APP_VERSION = "2026.11.17"

# 项目根目录（bflow 包的上一级）
ROOT_DIR = Path(__file__).resolve().parent.parent


def default_data_root() -> Path:
    """按操作系统惯例推导默认数据目录。"""
    env = os.environ.get("BFLOW_DATA_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


class Settings:
    """运行期配置。数据根目录可在启动后通过 API 修改。"""

    def __init__(self) -> None:
        self._data_root = default_data_root()
        self._host = os.environ.get("BFLOW_HOST", "127.0.0.1")
        self._port = int(os.environ.get("BFLOW_PORT", "8000"))

    # ------------------------------------------------------------ 路径
    @property
    def data_root(self) -> Path:
        return self._data_root

    def set_data_root(self, path: str | Path) -> Path:
        """切换数据根目录。调用方负责迁移已有数据。"""
        self._data_root = Path(path).expanduser().resolve()
        self.ensure_dirs()
        return self._data_root

    @property
    def db_path(self) -> Path:
        return self.data_root / "bflow.db"

    @property
    def assets_dir(self) -> Path:
        return self.data_root / "assets"

    @property
    def input_dir(self) -> Path:
        return self.assets_dir / "input"

    @property
    def output_dir(self) -> Path:
        return self.assets_dir / "output"

    @property
    def thumbs_dir(self) -> Path:
        return self.assets_dir / "thumbs"

    @property
    def canvases_dir(self) -> Path:
        return self.data_root / "canvases"

    @property
    def workflows_dir(self) -> Path:
        return self.data_root / "workflows"

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def master_key_path(self) -> Path:
        return self.data_root / "master.key"

    def ensure_dirs(self) -> None:
        for d in (self.data_root, self.assets_dir, self.input_dir, self.output_dir,
                  self.thumbs_dir, self.canvases_dir, self.workflows_dir,
                  self.cache_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 服务
    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def relative_to_root(self, path: Path | str) -> str:
        """把绝对路径转为相对 <data_root> 的 POSIX 路径，用于入库。"""
        p = Path(path)
        try:
            return p.resolve().relative_to(self.data_root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()

    def absolute(self, rel_path: str) -> Path:
        """把库中存的相对路径还原为绝对路径。"""
        p = Path(rel_path)
        return p if p.is_absolute() else (self.data_root / p)


settings = Settings()
