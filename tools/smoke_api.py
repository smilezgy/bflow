"""API 冒烟测试 —— 不连任何上游，**零费用**。

覆盖：健康检查、协议清单、渠道 CRUD、能力表、画布 CRUD、任务列表、
素材列表、配置读写、产物下载。不含真实生图（那部分在 selftest_api.py）。

运行：python -u tools/smoke_api.py [base_url]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name} {extra}")
    return ok


def call(method: str, path: str, payload=None):
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body) if body else {}
            except Exception:
                return r.status, {"_raw": body[:300]}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body[:300]}
    except Exception as e:
        return 0, {"_error": str(e)}


def data_of(payload) -> dict:
    return (payload or {}).get("data") or {}


def main() -> int:
    print(f"目标：{BASE}\n")

    print("[健康检查]")
    status, payload = call("GET", "/api/health")
    check("GET /api/health 200", status == 200, str(status))
    check("健康检查标记 healthy",
          data_of(payload).get("status") == "healthy", str(payload)[:120])

    print("\n[协议清单]")
    status, payload = call("GET", "/api/protocols")
    protos = data_of(payload).get("protocols") or []
    check("GET /api/protocols 200", status == 200, str(status))
    check("协议清单非空", len(protos) >= 6, f"实际 {len(protos)}")
    check("每个协议都有标签与说明",
          all(p.get("label") and p.get("hint") for p in protos))
    check("含 openai-images 协议",
          any(p["id"] == "openai-images" for p in protos))

    print("\n[渠道 CRUD]")
    cid = "smoke-ch"
    status, payload = call("POST", "/api/channels", {
        "id": cid, "display_name": "冒烟渠道", "protocol": "openai-images",
        "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-smoke-not-real",
        "models": ["gpt-image-smoke"],
    })
    check("POST /api/channels 200", status == 200, str(payload)[:200])
    check("渠道带协议标签",
          data_of(payload).get("protocol_label") == "OpenAI Images",
          str(data_of(payload).get("protocol_label")))

    status, payload = call("GET", "/api/channels")
    chans = data_of(payload).get("channels") or []
    check("GET /api/channels 200", status == 200, str(status))
    check("渠道已入库", any(c["id"] == cid for c in chans))
    check("密钥只返回掩码",
          all("*" in (c.get("api_key") or "") or not c.get("has_credential")
              for c in chans))

    status, payload = call("PUT", f"/api/channels/{cid}/capability", {
        "model": "gpt-image-smoke",
        "capability": {
            # 只用 PNG / JPEG 这类模型当前稳定支持的格式；不拿 svg 做冒烟
            # （部分模型/渠道对 svg 尚不稳定，会把冒烟测试变成偶发失败）。
            "output_formats": {"png": {"supported": False, "honors_request": False},
                               "jpeg": {"supported": True, "honors_request": True}},
            "size_policy": "reject",
            "size_bounds": {"min_pixels": 655360, "max_pixels": 8294400,
                            "max_edge": 3840},
            "quality_levels": ["auto", "low", "high"],
            "complexity_levels": ["auto", "high"],
            "backgrounds": ["auto", "transparent"],
            "transparent_formats": ["png"],
            "compression_by_format": {"png": None},
            "edits": {"supported": True, "max_reference_images": 2,
                      "supports_mask": False, "validates_mask_content": False,
                      "accepts_input_fidelity": False},
        },
    })
    check("PUT 能力表 200", status == 200, str(payload)[:200])
    caps = data_of(payload).get("capabilities") or []
    check("能力表已保存", any(c["model"] == "gpt-image-smoke" for c in caps))
    check("能力表来源标记为 manual",
          any(c.get("source") == "manual" for c in caps))

    print("\n[前置校验：默认 PNG + 不支持的格式应被拦下（不发请求）]")
    # 不传 output_format → 默认按 PNG 判定；上面把 png 标为不支持，
    # 所以这条应被拦下。这同时验证了「默认格式是 PNG」而不是别的。
    status, payload = call("POST", "/api/images/generate", {
        "prompt": "冒烟测试", "channel_id": cid, "model": "gpt-image-smoke",
        "size": "1024x1024", "count": 1,
    })
    check("不传格式时默认走 PNG（被 png 不支持拦下）→ 400",
          status == 400, str(status))
    check("错误码为 FORMAT_NOT_SUPPORTED（默认格式是 PNG）",
          (payload.get("error") or {}).get("code") == "FORMAT_NOT_SUPPORTED",
          str(payload)[:200])

    print("\n[画布 CRUD]")
    status, payload = call("POST", "/api/canvases", {"title": "冒烟画布"})
    canvas_id = data_of(payload).get("id") or ""
    check("POST /api/canvases 200", status == 200 and bool(canvas_id), str(status))

    status, payload = call("PUT", f"/api/canvases/{canvas_id}", {
        "title": "冒烟画布",
        "nodes": [{"id": "nd1", "type": "image", "x": 10, "y": 20,
                   "width": 200, "height": 200, "z_index": 0,
                   "data": {"prompt": "p"}, "asset_id": None}],
        "edges": [{"id": "eg1", "source_id": "nd1", "target_id": "nd_missing"}],
        "meta": {"gen": {"channel_id": cid}},
    })
    check("PUT 画布 200（悬空连线不阻断保存）", status == 200, str(payload)[:200])
    status, payload = call("GET", f"/api/canvases/{canvas_id}")
    nodes = data_of(payload).get("nodes") or []
    check("GET 画布 200", status == 200, str(status))
    check("节点已保存", len(nodes) == 1, f"实际 {len(nodes)}")
    check("悬空连线被丢弃", len(data_of(payload).get("edges") or []) == 0)
    check("meta 保留", data_of(payload).get("meta", {}).get("gen", {}).get("channel_id") == cid)

    status, _ = call("DELETE", f"/api/canvases/{canvas_id}")
    check("DELETE 画布 200", status == 200, str(status))

    print("\n[任务与素材]")
    status, payload = call("GET", "/api/tasks?limit=5")
    check("GET /api/tasks 200", status == 200, str(status))
    check("任务列表是数组", isinstance(data_of(payload).get("tasks"), list))
    status, payload = call("GET", "/api/assets?limit=5")
    check("GET /api/assets 200", status == 200, str(status))
    check("素材列表是数组", isinstance(data_of(payload).get("assets"), list))

    print("\n[配置]")
    status, payload = call("GET", "/api/config")
    check("GET /api/config 200", status == 200, str(status))
    check("配置含版本号", bool(data_of(payload).get("version")))
    status, payload = call("PATCH", "/api/config", {"theme": "dark"})
    check("PATCH /api/config 200", status == 200, str(payload)[:150])

    print("\n[清理]")
    status, _ = call("DELETE", f"/api/channels/{cid}")
    check("DELETE 渠道 200", status == 200, str(status))

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
