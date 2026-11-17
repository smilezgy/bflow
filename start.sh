#!/usr/bin/env bash
# Bflow 启动脚本（Linux / macOS）
set -euo pipefail

cd "$(dirname "$0")"

# 找一个真正能用的 Python。
# 注意：某些环境里的 python3 可能是个壳或占位符，光看 command -v 不够，
# 必须真正执行一次确认它能跑代码。
find_python() {
  # 1) 用户显式指定
  if [ -n "${PYTHON:-}" ]; then
    if [ -x "$PYTHON" ] || command -v "$PYTHON" >/dev/null 2>&1; then
      echo "$PYTHON"; return 0
    fi
    echo "[X] PYTHON 指向的解释器不可用: $PYTHON" >&2
    return 1
  fi

  # 2) 项目内自带
  if [ -x "./python/python3" ]; then echo "./python/python3"; return 0; fi
  if [ -x "./python/python" ]; then echo "./python/python"; return 0; fi

  # 3) PATH 上的候选 —— 逐个执行验证
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
      command -v "$c"; return 0
    fi
  done

  # 4) 常见安装位置
  for p in /usr/local/bin/python3 /usr/bin/python3 /opt/homebrew/bin/python3 \
           "$HOME/.pyenv/shims/python3" /usr/local/opt/python@3*/bin/python3; do
    if [ -x "$p" ] && "$p" -c "import sys" >/dev/null 2>&1; then
      echo "$p"; return 0
    fi
  done

  return 1
}

if ! PY="$(find_python)"; then
  echo
  echo "[X] 没有找到可用的 Python 解释器。"
  echo
  echo "    请安装 Python 3.10+："
  echo "      macOS:  brew install python"
  echo "      Debian: sudo apt install python3 python3-venv python3-pip"
  echo "      Fedora: sudo dnf install python3 python3-pip"
  echo
  echo "    或显式指定解释器："
  echo "      PYTHON=/path/to/python3 ./start.sh"
  echo
  exit 1
fi

echo "使用 Python: $PY"
"$PY" -c "import sys;print('版本:', sys.version.split()[0])"

echo "安装依赖…"
"$PY" -m pip install -r requirements.txt --quiet

HOST="${BFLOW_HOST:-127.0.0.1}"
PORT="${BFLOW_PORT:-8000}"

# 仅为显示目的解析 --host / --port；实际参数原样透传给 main.py
_prev=""
for arg in "$@"; do
  case "$_prev" in
    --host) HOST="$arg" ;;
    --port) PORT="$arg" ;;
  esac
  case "$arg" in
    --host=*) HOST="${arg#*=}" ;;
    --port=*) PORT="${arg#*=}" ;;
  esac
  _prev="$arg"
done

echo
echo "启动 Bflow → http://${HOST}:${PORT}/"
echo "数据目录：${BFLOW_DATA_ROOT:-$HOME/.local/share/Bflow}"
echo

exec "$PY" main.py "$@"
