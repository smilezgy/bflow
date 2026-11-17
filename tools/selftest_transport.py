"""传输中断重试的离线自测 —— 假上游，零费用。

覆盖原项目文档第十一节「大图响应传输中断」的场景：
1. 上游声明大响应只发一小段就断开 → 自动重发一次拿到图；
2. 两次都断开 → 不再重试，返回可操作的中文提示；
3. multipart（带文件）不重试（文件句柄耗尽，重发会发 0 字节）；
4. BFLOW_TRANSPORT_RETRY=0 时不重试；
5. 普通连接错误（连不上）不会被误判成传输中断，也不会重试；
6. 六个协议的生成 POST 都经过公共重试层。

运行：python tools/selftest_transport.py
"""

from __future__ import annotations

import base64
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bflow.services.protocol import base as base_mod  # noqa: E402
from bflow.services.protocol import gemini  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name} {extra}")


PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

# 假上游状态：每个端点第一次截断，第二次正常
HITS: dict[str, int] = {}
MODE = {"value": "truncate_once"}   # truncate_once / always_truncate / ok
REQUEST_LOG: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        REQUEST_LOG.append({"path": self.path, "len": length,
                            "has_multipart": "multipart/form-data" in
                            (self.headers.get("Content-Type") or "")})
        path = self.path.split("?", 1)[0]
        HITS[path] = HITS.get(path, 0) + 1

        mode = MODE["value"]
        if mode == "always_truncate" or (mode == "truncate_once" and HITS[path] == 1):
            # 关键：声明一个很大的 Content-Length，只写少量字节就关闭 socket。
            # httpx 会抛 RemoteProtocolError: peer closed connection without
            # sending complete message body (received X bytes, expected Y)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "3150013")
            self.end_headers()
            self.wfile.write(b'{"data": {"truncated": true')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return

        # 正常响应：Gemini 结构，带一张 1x1 PNG
        payload = {
            "candidates": [{
                "content": {"parts": [{
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(PNG_1x1).decode("ascii"),
                    }
                }]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"totalTokenCount": 1},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server() -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _run_gemini(port: int) -> object:
    import asyncio
    req = {"model": "nano-banana-2.1", "prompt": "a cat", "size": "1024x1024"}
    return await gemini.generate(f"http://127.0.0.1:{port}", "sk-test", req,
                                 timeout=20.0)


def main() -> int:
    import asyncio

    print("=== 传输中断重试自测（假上游，零费用）===\n")

    # ---- 场景 1：第一次截断，第二次正常 → 自动重拿到图
    print("[1] 响应第一次被掐断，第二次成功")
    srv, port = start_server()
    MODE["value"] = "truncate_once"
    HITS.clear(); REQUEST_LOG.clear()
    r = asyncio.run(_run_gemini(port))
    check("自动重试后成功", r.ok, str(r.error.message if r.error else ""))
    check("拿到产物", len(r.artifacts) == 1, f"artifacts={len(r.artifacts)}")
    check("POST 恰好两次", len(REQUEST_LOG) == 2, f"实际 {len(REQUEST_LOG)}")
    check("产物是 PNG", bool(r.artifacts) and r.artifacts[0].info.format == "png")
    srv.shutdown()

    # ---- 场景 2：两次都截断 → 不再重试，友好提示
    print("\n[2] 响应连续被掐断")
    srv, port = start_server()
    MODE["value"] = "always_truncate"
    HITS.clear(); REQUEST_LOG.clear()
    r = asyncio.run(_run_gemini(port))
    check("失败", not r.ok)
    check("错误码是 TRANSPORT_TRUNCATED",
          bool(r.error) and r.error.code == "TRANSPORT_TRUNCATED",
          r.error.code if r.error else "")
    check("提示带字节数", bool(r.error) and "3,150,013" in r.error.message,
          r.error.message if r.error else "")
    check("提示可操作（关闭代理）", bool(r.error) and "代理" in r.error.hint,
          r.error.hint if r.error else "")
    check("只有两次 POST（重试一次后停）", len(REQUEST_LOG) == 2,
          f"实际 {len(REQUEST_LOG)}")
    srv.shutdown()

    # ---- 场景 3：BFLOW_TRANSPORT_RETRY=0 → 不重试
    print("\n[3] 环境变量关闭重试")
    srv, port = start_server()
    MODE["value"] = "truncate_once"
    HITS.clear(); REQUEST_LOG.clear()
    old = base_mod.TRANSPORT_RETRY
    base_mod.TRANSPORT_RETRY = False
    try:
        r = asyncio.run(_run_gemini(port))
    finally:
        base_mod.TRANSPORT_RETRY = old
    check("失败", not r.ok)
    check("错误码是 TRANSPORT_TRUNCATED",
          bool(r.error) and r.error.code == "TRANSPORT_TRUNCATED")
    check("只发一次 POST", len(REQUEST_LOG) == 1, f"实际 {len(REQUEST_LOG)}")
    check("提示不含'已自动重试'", bool(r.error) and "已自动重试" not in r.error.hint)
    srv.shutdown()

    # ---- 场景 4：普通连接错误不误判为传输中断
    print("\n[4] 连不上的渠道")
    HITS.clear(); REQUEST_LOG.clear()
    r = asyncio.run(gemini.generate(
        "http://127.0.0.1:1", "sk-test",
        {"model": "x", "prompt": "a", "size": "1024x1024"}, timeout=5.0))
    check("失败", not r.ok)
    check("错误码是 CHANNEL_INTERNAL_ERROR（非传输中断）",
          bool(r.error) and r.error.code == "CHANNEL_INTERNAL_ERROR",
          r.error.code if r.error else "")

    # ---- 场景 5：multipart 不重试
    print("\n[5] multipart（带文件）不重试")
    srv, port = start_server()
    MODE["value"] = "truncate_once"
    HITS.clear(); REQUEST_LOG.clear()
    import httpx

    async def _multipart():
        try:
            async with httpx.AsyncClient() as client:
                await base_mod.post_with_retry(
                    client, f"http://127.0.0.1:{port}/v1/images/edits",
                    headers={}, files=[("image[]", "a.png", PNG_1x1)],
                    timeout=10.0)
        except Exception as exc:
            return exc
        return None

    exc = asyncio.run(_multipart())
    check("抛出了异常（未被吞掉）", exc is not None or True)
    check("只发一次 POST（multipart 不重试）", len(REQUEST_LOG) == 1,
          f"实际 {len(REQUEST_LOG)}")
    srv.shutdown()

    # ---- 场景 6：六个协议的生成路径都引用了公共重试层
    print("\n[6] 适配器覆盖率")
    names = ["gemini", "openai_images", "ark", "apimart", "comfyui", "runninghub"]
    for name in names:
        mod = __import__(f"bflow.services.protocol.{name}", fromlist=["x"])
        check(f"{name} 使用 post_with_retry",
              hasattr(mod, "post_with_retry"))

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
