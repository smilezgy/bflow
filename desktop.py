"""Bflow 桌面端 —— 用系统 WebView 打开界面。

与 Web 版的关系：**同一套后端**，只是把浏览器窗口换成原生窗口。
服务端仍在本地跑（127.0.0.1），数据目录与 Web 版一致。

运行：
    python desktop.py

打包（见 packaging/）：
    pyinstaller packaging/Bflow.spec

没有装 pywebview 时会退化为「打开系统浏览器」—— 功能不缺，只是没有原生窗口。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bflow.config import APP_VERSION, settings  # noqa: E402
from main import app  # noqa: E402

WINDOW_TITLE = f"Bflow {APP_VERSION}"


def _free_port(preferred: int) -> int:
    """端口被占用时自动换一个。"""
    for port in [preferred] + list(range(preferred + 1, preferred + 40)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def _serve(port: int) -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port,
                ws_ping_interval=None, ws_ping_timeout=None, log_level="warning")


def main() -> int:
    settings.ensure_dirs()
    port = _free_port(settings.port)
    url = f"http://127.0.0.1:{port}/"

    thread = threading.Thread(target=_serve, args=(port,), daemon=True)
    thread.start()

    # 等服务起来
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)

    print(f"Bflow {APP_VERSION} → {url}")
    print(f"数据目录: {settings.data_root}")

    try:
        import webview  # type: ignore
    except ImportError:
        print("未安装 pywebview，改用系统浏览器打开。")
        print("安装桌面端：pip install -r requirements-desktop.txt")
        webbrowser.open(url)
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return 0

    window = webview.create_window(WINDOW_TITLE, url, width=1440, height=900,
                                   min_size=(1024, 640))
    try:
        webview.start()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
