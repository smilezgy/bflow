#!/usr/bin/env bash
# Bflow 桌面端打包（Linux / macOS）
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

echo "[1/3] 安装依赖…"
"$PY" -m pip install -r requirements.txt -r requirements-desktop.txt --quiet

echo "[2/3] 清理旧产物…"
rm -rf build dist

echo "[3/3] 打包…"
"$PY" -m PyInstaller packaging/Bflow.spec --noconfirm

echo
echo "完成：dist/Bflow/Bflow"
echo "数据目录：${XDG_DATA_HOME:-$HOME/.local/share}/Bflow"
