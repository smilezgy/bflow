"""应用配置读写（settings 表）。"""

from __future__ import annotations

import json
import time
from typing import Any

from ..config import APP_VERSION
from ..db import execute, query

DEFAULTS: dict[str, Any] = {
    "storage.root": "",
    "ui.theme": "auto",
    "ui.locale": "zh-CN",
    "generation.default_channel": "",
    "generation.default_size": "1024x1024",
    "generation.default_format": "png",
    "generation.default_quality": "",
    "generation.default_complexity": "",
    "update.channel": "none",
    "update.repo": "",
}


def get_all() -> dict:
    rows = query("SELECT key, value_json FROM settings")
    values = dict(DEFAULTS)
    for r in rows:
        try:
            values[r["key"]] = json.loads(r["value_json"])
        except Exception:
            values[r["key"]] = r["value_json"]
    values["app.version"] = APP_VERSION
    return values


def get(key: str, default: Any = None) -> Any:
    row = query("SELECT value_json FROM settings WHERE key = ?", (key,))
    if not row:
        return DEFAULTS.get(key, default)
    try:
        return json.loads(row["value_json"])
    except Exception:
        return row["value_json"]


def set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings (key, value_json, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
        " updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), time.time()))


def update_many(patch: dict) -> dict:
    for key, value in patch.items():
        if key.startswith("_"):
            continue
        set(key, value)
    return get_all()
