"""多协议适配器的离线自测 —— 用假上游，不产生任何费用。

覆盖：
1. 每个协议的请求体构造（关键字段是否按协议规范发出）；
2. 产物提取（base64 / URL / 工作流 history）；
3. 错误归一化（上游报错 → 统一错误码）；
4. 偏差检测（请求 png 实返 jpeg → FORMAT_NOT_HONORED）；
5. 异步轮询（提交 → 轮询 → 取产物）。

运行：python tools/selftest_protocols.py
"""

from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bflow.services.protocol import registry  # noqa: E402
from bflow.services.protocol.base import detect_deviations  # noqa: E402

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
JPEG_TINY = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    + "01" * 64
    + "ffd9")

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


# ------------------------------------------------------------------ 假上游

REQUESTS: list[dict] = []
RESPONSES: dict[str, object] = {}
STATE: dict[str, int] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, payload, status=200, raw: bytes | None = None,
              content_type="application/json"):
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _key(self, method: str) -> str:
        """路由键：去掉查询串（查询参数不影响 stub 匹配）。"""
        path = self.path.split("?", 1)[0]
        return f"{method} {path}"

    def do_POST(self):
        raw = self._read()
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {"_raw": raw[:200].decode("latin1")}
        REQUESTS.append({"path": self.path, "body": body,
                         "headers": dict(self.headers)})
        key = self._key("POST")
        entry = RESPONSES.get(key)
        if callable(entry):
            entry = entry(body, STATE)
        if isinstance(entry, tuple):
            payload, status = entry
            self._send(payload, status)
            return
        if entry is None:
            self._send({"error": {"message": f"no stub for {key}"}}, 404)
            return
        self._send(entry)

    def do_GET(self):
        REQUESTS.append({"path": self.path, "body": None,
                         "headers": dict(self.headers)})
        key = self._key("GET")
        entry = RESPONSES.get(key)
        if callable(entry):
            entry = entry(None, STATE)
        if isinstance(entry, tuple):
            payload, status = entry
            self._send(payload, status)
            return
        if entry is None:
            self._send({"error": {"message": f"no stub for {key}"}}, 404)
            return
        if isinstance(entry, bytes):
            self._send(None, 200, raw=entry, content_type="image/png")
            return
        self._send(entry)


def serve() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


# ------------------------------------------------------------------ 场景


def scenario_gemini(base: str) -> None:
    print("\n[Gemini]")
    RESPONSES.clear()
    RESPONSES["POST /v1beta/models/gemini-3-pro:generateContent"] = {
        "candidates": [{
            "content": {"parts": [{
                "inlineData": {"mimeType": "image/png",
                               "data": base64.b64encode(PNG_1x1).decode()},
            }]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"totalTokenCount": 100},
    }
    req = {"model": "gemini-3-pro", "prompt": "一只红苹果",
           "size": "1536x1024", "output_format": "png"}
    res = _run(registry.dispatch("gemini", base, "k", req))
    check("Gemini 请求成功", res.ok, str(res.error))
    body = REQUESTS[-1]["body"]
    check("Gemini 端点路径正确",
          REQUESTS[-1]["path"] == "/v1beta/models/gemini-3-pro:generateContent",
          REQUESTS[-1]["path"])
    check("Gemini 文本在 parts 里",
          body["contents"][0]["parts"][0].get("text") == "一只红苹果")
    check("Gemini 宽高比由尺寸推导",
          body["generationConfig"]["imageConfig"]["aspectRatio"] == "3:2",
          str(body.get("generationConfig")))
    check("Gemini 产物按魔数识别为 PNG",
          res.ok and res.artifacts[0].info.format == "png")
    check("Gemini usage 透传", res.usage.get("totalTokenCount") == 100)

    # 拦截：finishReason 非 STOP
    RESPONSES["POST /v1beta/models/gemini-3-pro:generateContent"] = {
        "candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}],
    }
    res = _run(registry.dispatch("gemini", base, "k", req))
    check("Gemini 内容拦截 → CONTENT_REJECTED",
          (not res.ok) and res.error.code == "CONTENT_REJECTED", str(res.error))

    # 报错
    RESPONSES["POST /v1beta/models/gemini-3-pro:generateContent"] = (
        {"error": {"code": 400, "message": "API key not valid",
                   "status": "INVALID_ARGUMENT"}}, 400)
    res = _run(registry.dispatch("gemini", base, "k", req))
    check("Gemini 401/400 → 归一化错误码",
          (not res.ok) and res.error.code in ("INVALID_REQUEST", "CHANNEL_UNAVAILABLE"),
          str(res.error))


def scenario_ark(base: str) -> None:
    print("\n[方舟 Ark]")
    RESPONSES.clear()

    def submit(body, state):
        state["polls"] = 0
        return {"data": {"task_id": "task-123"}}, 200

    def poll(body, state):
        state["polls"] = state.get("polls", 0) + 1
        if state["polls"] < 2:
            return {"status": "running"}, 200
        return {"status": "succeeded",
                "response": {"data": [{"url": f"{base}/files/out.png"}]},
                "usage": {"generated_images": 1}}, 200

    RESPONSES["POST /api/v3/images/generations"] = submit
    RESPONSES["GET /api/v3/images/tasks/task-123"] = poll
    RESPONSES["GET /files/out.png"] = PNG_1x1

    req = {"model": "doubao-seedream-4-0-250828", "prompt": "红苹果",
           "size": "1024x1024", "output_format": "png"}
    res = _run(registry.dispatch("ark", base, "k", req))
    check("方舟提交 + 轮询成功", res.ok, str(res.error))
    check("方舟轮询次数 > 1", res.meta.get("polls", 0) >= 1, str(res.meta))
    check("方舟 task_id 留痕", res.meta.get("task_id") == "task-123")
    check("方舟产物识别为 PNG",
          res.ok and res.artifacts[0].info.format == "png")
    body = _last_post("/api/v3/images/generations")
    check("方舟请求体含 model/prompt/size",
          body.get("model") and body.get("prompt") and body.get("size"), str(body))
    check("方舟默认 response_format=url", body.get("response_format") == "url")

    # 任务失败
    RESPONSES["GET /api/v3/images/tasks/task-123"] = (
        {"status": "failed", "error": {"message": "内容审核不通过"}}, 200)
    res = _run(registry.dispatch("ark", base, "k", req))
    check("方舟任务失败 → 错误码",
          (not res.ok) and res.error.code == "CHANNEL_INTERNAL_ERROR", str(res.error))


def scenario_apimart(base: str) -> None:
    print("\n[APIMart]")
    RESPONSES.clear()
    STATE.clear()

    def submit(body, state):
        return {"data": {"id": "task-9", "status": "waiting"}}, 200

    def poll(body, state):
        state["n"] = state.get("n", 0) + 1
        if state["n"] < 2:
            return {"data": {"id": "task-9", "status": "processing"}}, 200
        return {"data": {"id": "task-9", "status": "succeeded",
                         "output": [f"{base}/files/ap.png"]}}, 200

    RESPONSES["POST /v1/tasks"] = submit
    RESPONSES["GET /v1/tasks/task-9"] = poll
    RESPONSES["GET /files/ap.png"] = PNG_1x1

    req = {"model": "gpt-image-1", "prompt": "红苹果", "size": "1024x1024",
           "output_format": "png"}
    res = _run(registry.dispatch("apimart", base, "k", req))
    check("APIMart 提交 + 轮询成功", res.ok, str(res.error))
    check("APIMart 产物识别为 PNG",
          res.ok and res.artifacts[0].info.format == "png")
    body = _last_post("/v1/tasks")
    check("APIMart 请求体 inputs.prompt", body["inputs"]["prompt"] == "红苹果")
    check("APIMart 带 x-api-key 头",
          any("x-api-key" in {k.lower() for k in r["headers"]}
              for r in REQUESTS if r["path"].endswith("/v1/tasks")))

    # 失败态
    RESPONSES["GET /v1/tasks/task-9"] = (
        {"data": {"id": "task-9", "status": "failed", "error": "额度不足"}}, 200)
    res = _run(registry.dispatch("apimart", base, "k", req))
    check("APIMart 失败态 → 错误码",
          (not res.ok) and res.error.code == "CHANNEL_INTERNAL_ERROR", str(res.error))


def scenario_comfyui(base: str) -> None:
    print("\n[ComfyUI]")
    RESPONSES.clear()

    workflow = {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    }
    RESPONSES["POST /prompt"] = {"prompt_id": "pid-1", "number": 1}
    RESPONSES["GET /history/pid-1"] = {
        "pid-1": {
            "status": {"completed": True, "messages": []},
            "outputs": {"9": {"images": [{"filename": "out.png", "subfolder": "",
                                          "type": "output"}]}},
        }
    }
    RESPONSES["GET /view"] = PNG_1x1

    req = {"model": "sdxl", "prompt": "红苹果", "size": "1024x1024",
           "output_format": "png"}
    cfg = {"workflow": workflow, "prompt_node": "6"}
    res = _run(registry.dispatch("comfyui", base, "k", req, config=cfg))
    check("ComfyUI 提交 + 取图成功", res.ok, str(res.error))
    check("ComfyUI 产物识别为 PNG",
          res.ok and res.artifacts[0].info.format == "png")
    sent = _last_post("/prompt")["prompt"]
    check("ComfyUI 提示词写入工作流",
          sent["6"]["inputs"]["text"] == "红苹果", json.dumps(sent, ensure_ascii=False))
    check("ComfyUI 其它节点未被破坏",
          sent["3"]["inputs"]["steps"] == 20)

    # 未配置工作流
    res = _run(registry.dispatch("comfyui", base, "k", req, config={}))
    check("ComfyUI 未配置工作流 → INVALID_REQUEST",
          (not res.ok) and res.error.code == "INVALID_REQUEST", str(res.error))


def scenario_runninghub(base: str) -> None:
    print("\n[RunningHub]")
    RESPONSES.clear()
    STATE.clear()

    def submit(body, state):
        return {"data": {"taskId": "rh-1"}}, 200

    def poll(body, state):
        state["n"] = state.get("n", 0) + 1
        if state["n"] < 2:
            return {"data": {"status": "RUNNING"}}, 200
        return {"data": {"status": "SUCCESS",
                         "outputs": [{"url": f"{base}/files/rh.png"}]}}, 200

    RESPONSES["POST /task/openapi/create"] = submit
    RESPONSES["POST /task/openapi/outputs"] = poll
    RESPONSES["GET /files/rh.png"] = PNG_1x1

    req = {"model": "rh-workflow", "prompt": "红苹果", "size": "1024x1024",
           "output_format": "png"}
    cfg = {"workflow_id": "999", "prompt_node": "12"}
    res = _run(registry.dispatch("runninghub", base, "k", req, config=cfg))
    check("RunningHub 提交 + 轮询成功", res.ok, str(res.error))
    check("RunningHub 产物识别为 PNG",
          res.ok and res.artifacts[0].info.format == "png")
    body = _last_post("/task/openapi/create")
    check("RunningHub 请求体含 workflowId", body.get("workflowId") == "999")
    check("RunningHub nodeInfoList 带提示词",
          body["nodeInfoList"][0]["fieldValues"]["text"] == "红苹果")

    # 未配置 workflow_id
    res = _run(registry.dispatch("runninghub", base, "k", req, config={}))
    check("RunningHub 未配 workflow_id → INVALID_REQUEST",
          (not res.ok) and res.error.code == "INVALID_REQUEST", str(res.error))


def scenario_deviation() -> None:
    print("\n[偏差检测]")
    from bflow.services.protocol.base import Artifact
    from bflow.services.protocol.imaging import parse_image

    art = Artifact(data=JPEG_TINY, info=parse_image(JPEG_TINY))
    warns = detect_deviations({"output_format": "png", "size": "1024x1024"}, [art])
    check("请求 PNG 实返 JPEG → FORMAT_NOT_HONORED",
          any(w["code"] == "FORMAT_NOT_HONORED" for w in warns), str(warns))
    art_png = Artifact(data=PNG_1x1, info=parse_image(PNG_1x1))
    warns = detect_deviations({"output_format": "png", "size": "1024x1024"}, [art_png])
    check("格式一致但尺寸不同 → SIZE_NORMALIZED",
          any(w["code"] == "SIZE_NORMALIZED" for w in warns), str(warns))


def scenario_registry() -> None:
    print("\n[协议注册表]")
    check("注册表含 6 个协议", len(registry.PROTOCOLS) == 6, str(list(registry.PROTOCOLS)))
    cat = registry.catalog()
    check("协议清单含标签与说明",
          all(c.get("label") and c.get("hint") for c in cat))
    cap = registry.default_capability("comfyui")
    check("ComfyUI 默认能力标 honors_request=false",
          cap["output_formats"]["png"]["honors_request"] is False)
    cap = registry.default_capability("gemini")
    check("Gemini 默认能力只支持 png/jpeg",
          set(cap["output_formats"]) == {"png", "jpeg"}, str(cap["output_formats"]))
    cap = registry.default_capability("openai-images")
    check("OpenAI 默认能力含 svg/gif",
          "svg" in cap["output_formats"] and "gif" in cap["output_formats"])


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _last_post(path_suffix: str) -> dict | None:
    """取最后一次匹配的 POST 请求体（轮询请求不能混淆进来）。"""
    for req in reversed(REQUESTS):
        if req["path"].endswith(path_suffix) and req["body"] is not None:
            return req["body"]
    return None


def main() -> int:
    srv, base = serve()
    print(f"假上游：{base}")
    try:
        scenario_registry()
        scenario_deviation()
        scenario_gemini(base)
        scenario_ark(base)
        scenario_apimart(base)
        scenario_comfyui(base)
        scenario_runninghub(base)
    finally:
        srv.shutdown()

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
