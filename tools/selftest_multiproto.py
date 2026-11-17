"""多协议渠道自测 —— 用假上游，不产生任何费用。

覆盖「一个渠道挂多个协议」这条主线：

1. 模型列表拉取（OpenAI 兼容 /v1/models、Gemini /v1beta/models）；
2. 协议绑定：同一渠道下不同模型走不同协议；
3. 协议解析优先级：模型绑定 > 渠道默认协议；
4. 生成时按模型选协议（真的按模型分别打到不同端点）；
5. 能力表按模型协议隔离（跨协议不串味）；
6. 协议变更后旧能力表作废；
7. 模型级 config 覆盖渠道级 config。

运行：python tools/selftest_multiproto.py
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

# ------------------------------------------------------------------ 假上游

REQUESTS: list[dict] = []
RESPONSES: dict[str, object] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, payload, status=200, content_type="application/json"):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _key(self, method: str) -> str:
        return f"{method} {self.path.split('?', 1)[0]}"

    def do_POST(self):
        raw = self._read()
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        REQUESTS.append({"path": self.path, "body": body,
                         "headers": dict(self.headers)})
        entry = RESPONSES.get(self._key("POST"))
        if callable(entry):
            entry = entry(body)
        if entry is None:
            self._send({"error": {"message": "no stub"}}, 404)
            return
        self._send(entry)

    def do_GET(self):
        REQUESTS.append({"path": self.path, "body": None,
                         "headers": dict(self.headers)})
        entry = RESPONSES.get(self._key("GET"))
        if callable(entry):
            entry = entry(None)
        if entry is None:
            self._send({"error": {"message": "no stub"}}, 404)
            return
        self._send(entry)


def serve() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


# ------------------------------------------------------------------ 场景


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bflow-mp-")
    os.environ["BFLOW_DATA_ROOT"] = tmp

    from bflow.db import init_db
    from bflow.services import channels as svc
    from bflow.services import generation as gen
    from bflow.services.protocol import registry

    init_db()
    srv, base = serve()
    print(f"假上游：{base}\n")

    # 一个聚合网关：默认协议 openai-images，模型列表混着多个厂商
    RESPONSES["GET /v1/models"] = {
        "data": [
            {"id": "gpt-image-1"},
            {"id": "gemini-3-pro-image"},
            {"id": "doubao-seedream-4-0"},
            {"id": "some-unknown-model"},
        ]
    }
    RESPONSES["POST /v1/images/generations"] = {
        "data": [{"b64_json": base64.b64encode(PNG_1x1).decode()}]
    }
    RESPONSES["POST /v1beta/models/gemini-3-pro-image:generateContent"] = {
        "candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png",
                            "data": base64.b64encode(PNG_1x1).decode()}}]},
            "finishReason": "STOP"}]
    }

    svc.create_channel({
        "id": "agg", "display_name": "聚合网关", "base_url": base,
        "api_key": "sk-test", "protocol": "openai-images",
        "models": ["gpt-image-1"],
    })

    # ---------------------------------------------------------- 1. 拉取模型
    print("[模型列表拉取]")
    listing = asyncio.run(registry.list_upstream_models(
        "openai-images", base, "sk-test"))
    check("拉取成功", listing.ok, listing.error)
    check("拉到 4 个模型", len(listing.models) == 4, str(listing.models))
    check("模型名去重且保序", listing.models[:2] == ["gpt-image-1",
                                                    "gemini-3-pro-image"],
          str(listing.models))

    print("\n[协议自动建议]")
    check("gpt-image → openai-images",
          registry.suggest_protocol("gpt-image-1") == "openai-images")
    check("gemini → gemini",
          registry.suggest_protocol("gemini-3-pro-image") == "gemini")
    check("doubao → ark",
          registry.suggest_protocol("doubao-seedream-4-0") == "ark")
    check("未知模型 → 回落默认",
          registry.suggest_protocol("some-unknown-model") == "openai-images")

    # ---------------------------------------------------------- 2. 协议绑定
    print("\n[协议绑定]")
    svc.set_model_binding("agg", "gemini-3-pro-image", "gemini")
    svc.set_model_binding("agg", "doubao-seedream-4-0", "ark")

    check("gemini 模型走 gemini 协议",
          svc.protocol_for("agg", "gemini-3-pro-image") == "gemini")
    check("doubao 模型走 ark 协议",
          svc.protocol_for("agg", "doubao-seedream-4-0") == "ark")
    check("未绑定模型回落到渠道默认协议",
          svc.protocol_for("agg", "gpt-image-1") == "openai-images")
    check("完全未登记的模型也回落默认协议",
          svc.protocol_for("agg", "never-seen") == "openai-images")

    bindings = svc.list_models("agg")
    check("注册表列出 3 个模型", len(bindings) == 3, str(len(bindings)))
    check("注册表带协议标签",
          any(b["protocol_label"] == "Gemini（Google）" for b in bindings),
          str([b["protocol_label"] for b in bindings]))

    # ---------------------------------------------------------- 3. 能力表隔离
    print("\n[能力表按协议隔离]")
    cap_gem = svc.capability_for("agg", "gemini-3-pro-image")
    cap_oai = svc.capability_for("agg", "gpt-image-1")
    check("gemini 能力不含 svg",
          "svg" not in (cap_gem.get("output_formats") or {}),
          str(list((cap_gem.get("output_formats") or {}).keys())))
    check("gemini 尺寸策略为 fixed",
          cap_gem.get("size_policy") == "fixed", str(cap_gem.get("size_policy")))
    check("openai 能力含 svg",
          "svg" in (cap_oai.get("output_formats") or {}),
          str(list((cap_oai.get("output_formats") or {}).keys())))
    check("两个模型的默认能力不同",
          set(cap_gem.get("output_formats") or {}) !=
          set(cap_oai.get("output_formats") or {}))

    # ---------------------------------------------------------- 4. 按模型选协议
    print("\n[生成时按模型选协议]")
    REQUESTS.clear()
    out = asyncio.run(gen.generate(gen.GenerationRequest(
        prompt="红苹果", channel_id="agg", model="gpt-image-1",
        size="1024x1024", output_format="png")))
    check("OpenAI 模型生成成功", out.ok, str(out.error))
    check("打到 /v1/images/generations",
          any(r["path"] == "/v1/images/generations" for r in REQUESTS),
          str([r["path"] for r in REQUESTS]))

    REQUESTS.clear()
    out = asyncio.run(gen.generate(gen.GenerationRequest(
        prompt="红苹果", channel_id="agg", model="gemini-3-pro-image",
        size="1024x1024", output_format="png")))
    check("Gemini 模型生成成功", out.ok, str(out.error))
    check("打到 /v1beta/models/...:generateContent",
          any("v1beta/models/gemini-3-pro-image:generateContent" in r["path"]
              for r in REQUESTS),
          str([r["path"] for r in REQUESTS]))
    check("Gemini 请求体用 contents 结构",
          "contents" in (REQUESTS[-1]["body"] or {}),
          str(list((REQUESTS[-1]["body"] or {}).keys())))

    # ---------------------------------------------------------- 5. 协议变更作废旧能力
    print("\n[协议变更后旧能力作废]")
    svc.set_capability("agg", "gpt-image-1",
                       {"output_formats": {"svg": {"supported": True,
                                                   "honors_request": True}},
                        "size_policy": "reject"},
                       source="manual")
    before = svc.capability_for("agg", "gpt-image-1")
    check("手工能力表已生效",
          before.get("source") == "manual", str(before.get("source")))
    svc.set_model_binding("agg", "gpt-image-1", "gemini")
    after = svc.capability_for("agg", "gpt-image-1")
    check("换协议后能力表重置为默认值",
          after.get("source") == "default", str(after.get("source")))
    check("换协议后 svg 不再可用",
          "svg" not in (after.get("output_formats") or {}),
          str(list((after.get("output_formats") or {}).keys())))
    svc.set_model_binding("agg", "gpt-image-1", "openai-images")

    # ---------------------------------------------------------- 6. 模型级 config
    print("\n[模型级 config 覆盖渠道级]")
    svc.update_channel("agg", {"config": {"aspect_ratio": "1:1",
                                          "response_format": "url"}})
    merged = svc.model_config_for("agg", "gemini-3-pro-image")
    check("渠道级 config 可见", merged.get("response_format") == "url",
          str(merged))
    svc.set_model_binding("agg", "gemini-3-pro-image", "gemini",
                          config={"aspect_ratio": "16:9"})
    merged = svc.model_config_for("agg", "gemini-3-pro-image")
    check("模型级 config 覆盖渠道级",
          merged.get("aspect_ratio") == "16:9", str(merged))
    check("未被覆盖的渠道级 config 保留",
          merged.get("response_format") == "url", str(merged))

    # ---------------------------------------------------------- 7. 删除模型
    print("\n[移除模型]")
    check("删除前存在", svc.get_model_binding("agg", "doubao-seedream-4-0") is not None)
    ok = svc.delete_model_binding("agg", "doubao-seedream-4-0")
    check("删除成功", ok)
    check("删除后不存在",
          svc.get_model_binding("agg", "doubao-seedream-4-0") is None)
    check("删除不存在的模型返回 False",
          svc.delete_model_binding("agg", "not-here") is False)

    # ---------------------------------------------------------- 8. 渠道详情
    print("\n[渠道详情]")
    detail = svc.channel_detail("agg")
    check("详情含模型绑定", len(detail.get("model_bindings") or []) == 2,
          str(len(detail.get("model_bindings") or [])))
    check("详情含协议清单",
          set(detail.get("protocols_in_use") or []) ==
          {"openai-images", "gemini"}, str(detail.get("protocols_in_use")))

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
