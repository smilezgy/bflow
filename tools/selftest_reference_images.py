"""参考图编辑自测（假上游，零费用）—— 回归「带参考图必报 AttributeError」。

历史 bug：generation.py 的 _load_reference_files / _validate_masks 产出
(字段名, 文件名, bytes) 三元组，ark/gemini/base.post_json 都按此约定处理，
唯独 openai_images._build_edit_fields 误用 item.filename / item.data ——
tuple 没有这些属性，带参考图时必抛 AttributeError。

本自测：
1. 直接断言 _build_edit_fields 对三元组与对象两种格式都正确；
2. 走完整 generation.generate() 链路，确认 multipart 请求真的把图片发出去。

运行：python tools/selftest_reference_images.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        RECEIVED.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "raw": raw,
        })
        body = json.dumps({"data": [{"b64_json": base64.b64encode(PNG_1x1).decode()}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bflow-refs-")
    os.environ["BFLOW_DATA_ROOT"] = tmp

    from bflow.db import init_db
    from bflow.services import assets as assets_svc
    from bflow.services import channels as channels_svc
    from bflow.services import generation as gen
    from bflow.services.protocol import openai_images

    init_db()
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    print(f"假上游：{base}\n")

    print("=== [1] _build_edit_fields 字段格式 ===")
    fields, files = openai_images._build_edit_fields({
        "model": "gpt-image-2.6-flare", "prompt": "p", "output_format": "gif",
        "reference_images": [("image", "ref_a.png", b"AAA"), ("image", "ref_b.png", b"BBB")],
        "masks": [("mask", "mask.png", b"MMM")],
    })
    check("三元组不抛 AttributeError 且原样透传",
          files == [("image", "ref_a.png", b"AAA"),
                    ("image", "ref_b.png", b"BBB"),
                    ("mask", "mask.png", b"MMM")], str(files))
    check("表单字段含 model/prompt/output_format",
          fields.get("model") == "gpt-image-2.6-flare" and
          fields.get("output_format") == "gif", str(fields))

    class Obj:
        def __init__(self, fn, data):
            self.filename = fn
            self.data = data

    _, obj_files = openai_images._build_edit_fields({
        "model": "m", "prompt": "p",
        "reference_images": [Obj("obj.png", b"OOO")],
    })
    check("对象格式仍兼容（防御性）",
          obj_files == [("image", "obj.png", b"OOO")], str(obj_files))

    print("\n=== [2] 完整 generation 链路（带参考图）===")
    ch = channels_svc.create_channel({
        "id": "edit_ch", "display_name": "编辑渠道", "base_url": base,
        "api_key": "sk-test", "protocol": "openai-images",
        "models": ["gpt-image-2.6-flare"],
    })
    ref = assets_svc.store_bytes(PNG_1x1, kind="input", origin="test")
    req = gen.GenerationRequest(
        prompt="参考图一衣着，给参考图二人物生成动态图",
        channel_id=ch["id"], model="gpt-image-2.6-flare",
        size="896x896", output_format="gif",
        reference_asset_ids=[ref.id],
    )
    outcome = asyncio.run(gen.generate(req))
    check("带参考图生成成功（不再是 AttributeError）",
          outcome.ok, str(outcome.error))
    if outcome.error:
        check("错误里不含 AttributeError",
              "AttributeError" not in (outcome.error.get("message") or ""),
              str(outcome.error))

    check("假上游收到 POST /v1/images/edits",
          any(r["path"].endswith("/v1/images/edits") for r in RECEIVED),
          str([r["path"] for r in RECEIVED]))
    edits = [r for r in RECEIVED if r["path"].endswith("/v1/images/edits")]
    if edits:
        req0 = edits[0]
        check("请求是 multipart",
              "multipart/form-data" in req0["content_type"], req0["content_type"])
        check("multipart 里带上了参考图字节与字段名",
              b"name=\"image\"" in req0["raw"] and PNG_1x1 in req0["raw"],
              f"body_len={len(req0['raw'])}")

    srv.shutdown()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
