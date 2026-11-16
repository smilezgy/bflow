#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bflow P0 —— 渠道行为黑盒探测工具

目的：为 clean-room 重写采集「平台行为规格」。

设计约束（法务层面，决定了本工具的写法）：
  * 本工具**不复用**任何被测项目的源码，只按公开 API 约定发 HTTP 请求；
  * 采集对象是**可观察事实**：HTTP 状态、响应报文原文、产物真实格式/尺寸、
    是否计费（由状态码与报文推断）；
  * 输出为结构化 JSON，供人工整理成规格文档。

用法：
    python probe_platforms.py --list                 # 列出可探测渠道
    python probe_platforms.py --channel ch-a   # 探测单个渠道（代号见 channels.json）
    python probe_platforms.py --channel all --out ../probe-data
"""

import argparse
import base64
import json
import os
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- 配置

# 渠道定义放在仓库外的本地配置文件里，原因有二：
#   1) 仓库是公开的，写死渠道名/域名等于替第三方渠道做广告；
#   2) 渠道接入信息是使用者自己的配置，不该进版本库。
#
# 读取顺序：channels.json（本地，已 gitignore）→ channels.example.json（模板）
# 模板里的渠道名与域名均为占位符，按需替换即可开始探测。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
CHANNELS_FILE = os.path.join(_ROOT, "channels.json")
CHANNELS_TEMPLATE = os.path.join(_ROOT, "channels.example.json")
SECRETS_FILE = os.path.join(_ROOT, "secrets.env")


def load_channels():
    """载入渠道定义（本地配置优先，回退到仓库内模板）。"""
    path = CHANNELS_FILE if os.path.exists(CHANNELS_FILE) else CHANNELS_TEMPLATE
    if not os.path.exists(path):
        raise SystemExit(f"找不到渠道配置：{path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    channels = data.get("channels", data)
    if path == CHANNELS_TEMPLATE:
        print(f"[提示] 使用仓库内模板 {os.path.basename(CHANNELS_TEMPLATE)}，"
              f"请先复制为 channels.json 并填入真实渠道信息")
    return channels


CHANNELS = load_channels()

# 密钥文件：本地 secrets.env（已 gitignore），格式 KEY_NAME=xxxx
ENV_FILE = SECRETS_FILE

# 探测用的模型（各渠道通用的 gpt-image-2.6 变体）
MODELS = ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"]

# 极简提示词：风格偏向图标/扁平，避免触发「SVG 不适合照片」这类内容限制
PROMPT_ICON = "a flat vector icon of a red apple, simple shapes, minimal, white background"
PROMPT_PHOTO = "a red apple on a wooden table, studio light"

# ---------------------------------------------------------------- 基础工具


def read_env(path=ENV_FILE):
    """读取 .env 为 dict（不打印任何值）。"""
    env = {}
    if not os.path.exists(path):
        return env
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def guess_ext(raw):
    """按魔数判断图片真实格式（不信任上游声明的 mime）。"""
    if not raw:
        return ""
    head = raw[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    if b"<svg" in raw[:300].lower():
        return "svg"
    return ""


def image_size(raw, ext):
    """尽力解析图片尺寸（png/gif/webp/jpeg）。"""
    try:
        if ext == "png" and raw[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", raw[16:24])
            return w, h
        if ext == "gif":
            w, h = struct.unpack("<HH", raw[6:10])
            return w, h
        if ext == "webp" and raw[:4] == b"RIFF":
            chunk = raw[12:16]
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", raw[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = raw[21:26]
                bits = b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if ext == "jpeg":
            i = 2
            while i < len(raw) - 9:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
                i += 2 + seg
    except Exception:
        pass
    return 0, 0


def fetch_image(url, timeout=180):
    """下载图片 URL，返回 (bytes, content_type)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Bflow-Probe/0.1"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return r.read(), r.headers.get("Content-Type", "")


# ---------------------------------------------------------------- 请求


def call(channel, model, payload, endpoint="images/generations", timeout=240, image_bytes=None,
         retries=2):
    """向渠道发一次请求，返回结构化观察结果。

    网络类失败（status=0）会自动重试：渠道侧偶发连接重置较常见，
    不重试会把「网络抖动」误记成「渠道不支持该参数」。
    """
    last = None
    for attempt in range(retries + 1):
        last = _call_once(channel, model, payload, endpoint, timeout)
        if last["status"] != 0 or attempt >= retries:
            break
        time.sleep(3 + attempt * 3)
        print(f"    (网络失败，重试 {attempt + 1}/{retries})")
    return last


def _call_once(channel, model, payload, endpoint="images/generations", timeout=240):
    base = CHANNELS[channel]["base"].rstrip("/")
    # base 可能已含 /v1（部分渠道的 base 自带版本段），此时直接追加端点
    if base.endswith("/v1"):
        url = f"{base}/{endpoint}"
    else:
        url = f"{base}/v1/{endpoint}"
    key = read_env().get(CHANNELS[channel]["key_env"], "")
    headers = {"Content-Type": "application/json", "User-Agent": "Bflow-Probe/0.1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            raw_text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw_text = e.read().decode("utf-8", "replace")
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {e}", "elapsed": round(time.time() - started, 1)}

    result = {
        "status": status,
        "elapsed": round(time.time() - started, 1),
        "billed": status == 200,          # 推断：非 200 未生成、未计费
    }

    if status != 200:
        result["body"] = raw_text[:600]
        return result

    try:
        data = json.loads(raw_text)
    except Exception:
        result["body"] = raw_text[:600]
        return result

    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        result["body"] = raw_text[:600]
        result["note"] = "200 但响应无 data 数组"
        return result

    first = items[0] if isinstance(items[0], dict) else {}
    if first.get("b64_json"):
        try:
            raw = base64.b64decode(first["b64_json"])
        except Exception:
            raw = b""
    elif first.get("url"):
        try:
            raw, ctype = fetch_image(first["url"])
            result["content_type"] = ctype
        except Exception as e:
            result["note"] = f"下载产物失败: {e}"
            raw = b""
    else:
        result["note"] = "data 项无 b64_json 也无 url"
        raw = b""

    if raw:
        ext = guess_ext(raw)
        w, h = image_size(raw, ext)
        result["actual_format"] = ext or "unknown"
        result["actual_size"] = f"{w}x{h}" if w else ""
        result["bytes"] = len(raw)
    if isinstance(data, dict) and data.get("usage"):
        result["usage"] = data["usage"]
    return result


# ---------------------------------------------------------------- 探测用例


def probe_formats(channel, model):
    """探测 output_format 各取值的真实行为。"""
    cases = []
    for fmt in ["png", "jpeg", "webp", "svg", "gif"]:
        payload = {"model": model, "prompt": PROMPT_ICON, "size": "1024x1024",
                   "output_format": fmt}
        if fmt == "gif":
            payload["frame_count"] = 4
            payload["frame_duration_ms"] = 200
        r = call(channel, model, payload)
        r["case"] = f"output_format={fmt}"
        r["requested_format"] = fmt
        cases.append(r)
        print(f"  [{channel}/{model}] {fmt:5} → {r['status']} "
              f"实际={r.get('actual_format','-')} 尺寸={r.get('actual_size','-')} "
              f"计费={r.get('billed')}")
        if r.get("body"):
            print(f"        报文: {r['body'][:220]}")
    return cases


def probe_quality(channel, model):
    cases = []
    for q in ["low", "medium", "high", "xhigh", "max", "auto"]:
        r = call(channel, model, {"model": model, "prompt": PROMPT_ICON,
                                  "size": "1024x1024", "quality": q})
        r["case"] = f"quality={q}"
        cases.append(r)
        print(f"  [{channel}/{model}] quality={q:6} → {r['status']} "
              f"计费={r.get('billed')} {r.get('body','')[:160]}")
    return cases


def probe_complexity(channel, model):
    cases = []
    for c in ["low", "medium", "high"]:
        r = call(channel, model, {"model": model, "prompt": PROMPT_ICON,
                                  "size": "1024x1024", "complexity": c})
        r["case"] = f"complexity={c}"
        cases.append(r)
        print(f"  [{channel}/{model}] complexity={c:6} → {r['status']} "
              f"计费={r.get('billed')} {r.get('body','')[:160]}")
    return cases


def probe_sizes(channel, model):
    """尺寸边界。优先用预期失败的参数（不计费）。"""
    cases = []
    for size in ["256x256", "512x512", "768x768", "896x896", "1024x1024",
                 "1536x1024", "2048x2048", "3072x3072"]:
        r = call(channel, model, {"model": model, "prompt": PROMPT_ICON, "size": size})
        r["case"] = f"size={size}"
        cases.append(r)
        print(f"  [{channel}/{model}] size={size:10} → {r['status']} "
              f"实际尺寸={r.get('actual_size','-')} 计费={r.get('billed')} "
              f"{r.get('body','')[:140]}")
    return cases


def probe_background(channel, model):
    cases = []
    for bg in ["auto", "transparent", "opaque"]:
        r = call(channel, model, {"model": model, "prompt": PROMPT_ICON,
                                  "size": "1024x1024", "output_format": "png",
                                  "background": bg})
        r["case"] = f"background={bg}"
        cases.append(r)
        print(f"  [{channel}/{model}] background={bg:11} → {r['status']} "
              f"实际格式={r.get('actual_format','-')} 计费={r.get('billed')} "
              f"{r.get('body','')[:140]}")
    return cases


def probe_compression(channel, model):
    """压缩率与格式的冲突矩阵（这是踩坑最多的点）。"""
    cases = []
    for fmt in ["png", "jpeg", "webp"]:
        for comp in [80, 100]:
            r = call(channel, model, {"model": model, "prompt": PROMPT_ICON,
                                      "size": "1024x1024", "output_format": fmt,
                                      "output_compression": comp})
            r["case"] = f"output_format={fmt} + output_compression={comp}"
            cases.append(r)
            print(f"  [{channel}/{model}] {fmt}/comp={comp} → {r['status']} "
                  f"实际格式={r.get('actual_format','-')} 计费={r.get('billed')} "
                  f"{r.get('body','')[:140]}")
    return cases


def probe_n(channel, model):
    cases = []
    for n in [1, 2]:
        r = call(channel, model, {"model": model, "prompt": PROMPT_ICON,
                                  "size": "1024x1024", "n": n})
        r["case"] = f"n={n}"
        cases.append(r)
        print(f"  [{channel}/{model}] n={n} → {r['status']} "
              f"计费={r.get('billed')} {r.get('body','')[:140]}")
    return cases


def probe_unknown_param(channel, model):
    """未定义参数的行为：判断渠道是忽略还是报错。"""
    r = call(channel, model, {"model": model, "prompt": PROMPT_ICON,
                              "size": "1024x1024", "zzz_bogus_param": "x"})
    r["case"] = "unknown_param"
    print(f"  [{channel}/{model}] unknown_param → {r['status']} "
          f"计费={r.get('billed')} {r.get('body','')[:160]}")
    return [r]


GROUPS = {
    "formats": probe_formats,
    "quality": probe_quality,
    "complexity": probe_complexity,
    "sizes": probe_sizes,
    "background": probe_background,
    "compression": probe_compression,
    "n": probe_n,
    "unknown": probe_unknown_param,
}


# ---------------------------------------------------------------- 主流程


def probe_channel(channel, groups, only_failed=None):
    report = {"channel": channel, "meta": CHANNELS[channel], "model": None, "groups": {}}
    # 先确认模型可用
    for model in MODELS:
        r = call(channel, model, {"model": model, "prompt": PROMPT_ICON, "size": "1024x1024"})
        print(f"[{channel}] 模型可用性探测 {model} → {r['status']} "
              f"{r.get('body','')[:160]}")
        report["model_probe"] = {"model": model, "status": r["status"], "body": r.get("body", "")}
        if r["status"] == 200:
            report["model"] = model
            break
    if not report["model"]:
        print(f"[{channel}] 无可用模型，跳过参数探测")
        return report

    model = report["model"]
    for g in groups:
        # 补跑模式：只重跑该组里网络失败(status=0)的用例
        if only_failed and g in only_failed:
            prev = only_failed[g]
            pending = [(c, r) for c, r in prev.items() if r.get("status") == 0]
            if not pending:
                print(f"[{channel}/{model}] ── 组：{g} ── 无失败用例，跳过")
                continue
            print(f"[{channel}/{model}] ── 组：{g} ── 补跑 {len(pending)} 个失败用例")
            report["groups"][g] = _rerun_failed(channel, model, g, pending)
            continue
        print(f"[{channel}/{model}] ── 组：{g} ──")
        report["groups"][g] = GROUPS[g](channel, model)
    return report


def _rerun_failed(channel, model, group, pending):
    """按用例名重跑失败项，结果写回原结构。"""
    fn = GROUPS[group]
    out = {}
    # 各探测函数返回 list[dict]，用 case 字段做键
    for case_name, _old in pending:
        pass
    # 简化：整组重跑（失败的用例本来就少，整组重跑代价可接受）
    results = fn(channel, model)
    for r in results:
        out[r.get("case", "")] = r
    return list(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="ch-a")
    ap.add_argument("--groups", default=",".join(GROUPS))
    ap.add_argument("--out", default="")
    ap.add_argument("--list", action="store_true", help="列出可探测渠道")
    ap.add_argument("--only-failed", default="", help="从已有 probe JSON 中找出 status=0 的用例并重跑")
    args = ap.parse_args()

    if args.list:
        for cid, meta in CHANNELS.items():
            has = bool(read_env().get(meta["key_env"]))
            print(f"  {cid:14} {meta['name']:14} base={meta['base']:32} key={'有' if has else '缺'}")
        return 0

    groups = [g.strip() for g in args.groups.split(",") if g.strip() in GROUPS]
    channels = list(CHANNELS) if args.channel == "all" else [args.channel]

    # 载入历史结果，识别网络失败项
    only_failed = None
    if args.only_failed:
        only_failed = {}
        try:
            with open(args.only_failed, encoding="utf-8") as f:
                hist = json.load(f)
            hist = hist if isinstance(hist, list) else [hist]
            for rep in hist:
                for g, cases in (rep.get("groups") or {}).items():
                    bad = {c.get("case", ""): c for c in cases if c.get("status") == 0}
                    if bad:
                        only_failed.setdefault(g, {}).update(bad)
        except Exception as e:
            print(f"读取历史失败: {e}")
            only_failed = None
        if only_failed:
            total = sum(len(v) for v in only_failed.values())
            print(f"识别到 {total} 个网络失败用例，涉及组: {list(only_failed)}")
        else:
            only_failed = None

    reports = []
    for cid in channels:
        if cid not in CHANNELS:
            print(f"未知渠道: {cid}")
            continue
        print(f"\n{'=' * 60}\n探测渠道 {cid}（{CHANNELS[cid]['name']}）\n{'=' * 60}")
        reports.append(probe_channel(cid, groups, only_failed))

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(args.out, f"probe-{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reports, f, ensure_ascii=False, indent=2)
        print(f"\n已保存: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
