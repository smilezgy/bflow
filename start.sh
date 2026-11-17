#!/usr/bin/env bash
# Bflow 启动脚本（Linux / macOS）
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.10+"
  exit 1
fi

echo "安装依赖…"
"$PY" -m pip install -r requirements.txt --quiet

HOST="${BFLOW_HOST:-127.0.0.1}"
PORT="${BFLOW_PORT:-8000}"

echo
echo "启动 Bflow → http://${HOST}:${PORT}/"
echo "数据目录：${BFLOW_DATA_ROOT:-$HOME/.local/share/Bflow}"
echo

exec "$PY" main.py
