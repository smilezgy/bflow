# -*- coding: utf-8 -*-
"""Bflow 后端 API 端到端自测（使用本地配置的真实渠道）。"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = os.environ.get("BFLOW_BASE", "http://127.0.0.1:8000")


def read_env(path="secrets.env"):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def call(method, path, payload=None, raw=None, headers=None, binary=False):
    url = f"{BASE}{path}"
    data = None
    hdrs = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=400) as r:
            body = r.read()
            if binary:
                return r.status, body
            text = body.decode("utf-8", "replace")
            return r.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body = e.read()
        if binary:
            return e.code, body
        text = body.decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, {"raw": text[:500]}


fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {extra}"))
    if not ok:
        fails.append(name)


env = read_env()

print("=" * 66)
print("Bflow API 端到端自测")
print("=" * 66)

# ---------------------------------------------------------------- 渠道
status, r = call("GET", "/api/channels")
check("GET /channels 200", status == 200, r)
existing = [c["id"] for c in (r.get("data") or {}).get("channels", [])]
print(f"     现有渠道: {existing}")

if "ch-a" not in existing:
    status, r = call("POST", "/api/channels", {
        "id": "ch-a", "display_name": "CH-A", "protocol": "openai-images",
        "base_url": "<base-url>",
        "api_key": env.get("CH_A_KEY", ""),
        "models": ["gpt-image-2.6-flare"],
    })
    check("POST /channels 建渠道", status == 200, r)

# 能力表覆盖
CAP = {
    "output_formats": {
        "png": {"supported": True, "honors_request": True},
        "jpeg": {"supported": True, "honors_request": True},
        "webp": {"supported": True, "honors_request": True},
        "svg": {"supported": True, "honors_request": True},
        "gif": {"supported": True, "honors_request": True},
    },
    "size_policy": "normalize_silently",
    "size_bounds": {"min_pixels": 655360, "max_pixels": 8294400, "max_edge": 3840},
    "quality_levels": ["auto", "low", "medium", "high", "xhigh", "max"],
    "complexity_levels": ["auto", "low", "medium", "high"],
    "backgrounds": ["auto", "transparent", "opaque"],
    "transparent_formats": ["png", "webp"],
    "compression_by_format": {"png": None, "jpeg": [0, 100], "webp": [0, 100]},
    "edits": {"supported": True, "max_reference_images": 3, "supports_mask": True,
              "validates_mask_content": False, "accepts_input_fidelity": True},
}
status, r = call("PUT", "/api/channels/ch-a/capability",
                 {"model": "gpt-image-2.6-flare", "capability": CAP})
check("PUT capability 覆盖能力表", status == 200, r)
check("能力来源标为 manual",
      (r.get("data") or {}).get("capabilities", [{}])[0].get("source") == "manual",
      r.get("data"))

# 密钥不回传
status, r = call("GET", "/api/channels")
ch = next((c for c in (r.get("data") or {}).get("channels", []) if c["id"] == "ch-a"), {})
check("密钥不回传明文", "*" in (ch.get("api_key") or "") and
      env.get("CH_A_KEY", "") not in (ch.get("api_key") or ""), ch.get("api_key"))

# ---------------------------------------------------------------- 前置校验
print("\n--- 前置校验（不应发请求、不计费）---")
status, r = call("POST", "/api/images/generate", {
    "prompt": "test", "channel_id": "ch-a", "model": "gpt-image-2.6-flare",
    "output_format": "png", "output_compression": 50})
check("PNG + 压缩率<100 被拦下",
      status == 400 and (r.get("error") or {}).get("code") == "INVALID_REQUEST", r)

status, r = call("POST", "/api/images/generate", {
    "prompt": "", "channel_id": "ch-a", "model": "gpt-image-2.6-flare"})
check("空提示词被拦下",
      status == 400 and (r.get("error") or {}).get("code") == "PROMPT_REQUIRED", r)

status, r = call("POST", "/api/images/generate", {
    "prompt": "test", "channel_id": "ch-a", "model": "gpt-image-2.6-flare",
    "output_format": "jpeg", "background": "transparent"})
check("JPEG + 透明背景被拦下",
      status == 400 and (r.get("error") or {}).get("code") == "INVALID_REQUEST", r)

status, r = call("POST", "/api/images/generate", {
    "prompt": "test", "channel_id": "ch-a", "model": "gpt-image-2.6-flare",
    "output_format": "svg", "count": 3})
check("SVG + count=3 被拦下",
      status == 400 and (r.get("error") or {}).get("code") == "INVALID_REQUEST", r)

# 不支持的格式（先临时把能力改成不支持 svg）
CAP2 = json.loads(json.dumps(CAP))
CAP2["output_formats"]["svg"] = {"supported": False, "honors_request": False}
call("PUT", "/api/channels/ch-a/capability",
     {"model": "gpt-image-2.6-flare", "capability": CAP2})
status, r = call("POST", "/api/images/generate", {
    "prompt": "test", "channel_id": "ch-a", "model": "gpt-image-2.6-flare",
    "output_format": "svg"})
check("能力表标记不支持 svg 时被拦下",
      status == 400 and (r.get("error") or {}).get("code") == "FORMAT_NOT_SUPPORTED", r)
call("PUT", "/api/channels/ch-a/capability",
     {"model": "gpt-image-2.6-flare", "capability": CAP})

# ---------------------------------------------------------------- 真实生图
print("\n--- 真实生图（会计费）---")
status, r = call("POST", "/api/images/generate", {
    "prompt": "a flat vector icon of a red apple, simple shapes, minimal, white background",
    "channel_id": "ch-a", "model": "gpt-image-2.6-flare",
    "size": "1024x1024", "output_format": "png"})
check("生图 200", status == 200, r)
data = r.get("data") or {}
imgs = data.get("images") or []
check("返回 1 张图", len(imgs) == 1, r)
if imgs:
    img = imgs[0]
    check("产物格式按魔数判定为 png", img.get("format") == "png", img)
    check("产物尺寸 1024x1024",
          f"{img.get('width')}x{img.get('height')}" == "1024x1024", img)
    check("记录了 requested_format", img.get("requested_format") == "png", img)
    check("无偏差警告（格式与尺寸都符合）", not data.get("warnings"), data.get("warnings"))
    # 产物可下载
    status2, raw = call("GET", f"/api/assets/{img['id']}/raw", binary=True)
    check("产物可下载", status2 == 200 and len(raw) > 1000, status2)
    check("下载内容是 PNG（魔数）", raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:8])

# ---------------------------------------------------------------- 格式偏差
print("\n--- 格式偏差场景（渠道不 honor 时应警告而非报错）---")
status, r = call("POST", "/api/images/generate", {
    "prompt": "a flat vector icon of a red apple, simple shapes",
    "channel_id": "ch-a", "model": "gpt-image-2.6-flare",
    "size": "1024x1024", "output_format": "webp"})
check("webp 生图 200", status == 200, r)
d = r.get("data") or {}
warns = d.get("warnings") or []
actual = (d.get("images") or [{}])[0].get("format")
if actual and actual != "webp":
    check("格式不符时产生 FORMAT_NOT_HONORED 警告",
          any(w.get("code") == "FORMAT_NOT_HONORED" for w in warns), warns)
    check("格式不符时仍交付产物", len(d.get("images") or []) == 1, d)
else:
    print(f"     （本次渠道返回的就是 {actual}，跳过偏差断言）")

# ---------------------------------------------------------------- 任务
print("\n--- 任务 ---")
status, r = call("GET", "/api/tasks?limit=10")
check("GET /tasks 200", status == 200, r)
tasks = (r.get("data") or {}).get("tasks") or []
check("任务已记录", len(tasks) >= 2, len(tasks))
if tasks:
    t = tasks[0]
    check("任务含 requested/actual 格式",
          "requested_format" in t and "actual_format" in t, t)
    status, r2 = call("GET", f"/api/tasks/{t['task_id']}")
    check("GET 单任务 200", status == 200, r2)

# ---------------------------------------------------------------- 资源
print("\n--- 资源 ---")
status, r = call("GET", "/api/assets?limit=5")
check("GET /assets 200", status == 200, r)
assets = (r.get("data") or {}).get("assets") or []
check("资源已入库", len(assets) >= 1, len(assets))
if assets:
    check("资源含真实格式", assets[0].get("format") in ("png", "jpeg", "webp", "svg", "gif"),
          assets[0])

# ---------------------------------------------------------------- 画布
print("\n--- 画布 ---")
status, r = call("POST", "/api/canvases", {"title": "自测画布"})
check("建画布 200", status == 200, r)
cid = (r.get("data") or {}).get("id")
status, r = call("PUT", f"/api/canvases/{cid}", {
    "title": "自测画布",
    "nodes": [{"id": "nd1", "type": "image", "x": 10, "y": 20,
               "width": 320, "height": 320, "data": {"prompt": "x"}}],
    "edges": [{"id": "eg1", "source_id": "nd1", "target_id": "nd2"}]})
check("保存画布 200", status == 200, r)
status, r = call("GET", f"/api/canvases/{cid}")
check("读画布 200 且节点在", status == 200 and
      len((r.get("data") or {}).get("nodes") or []) == 1, r)

# ---------------------------------------------------------------- 配置
print("\n--- 配置 ---")
status, r = call("GET", "/api/config")
check("GET /config 200", status == 200, r)
status, r = call("PATCH", "/api/config", {"ui.theme": "dark"})
check("PATCH /config 200", status == 200, r)

print("\n" + "=" * 66)
print(f"失败 {len(fails)} 项" + (f": {fails}" if fails else " —— 全部通过"))
print("=" * 66)
sys.exit(1 if fails else 0)
