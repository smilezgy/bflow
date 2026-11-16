# Bflow —— 渠道能力矩阵

> 由 P0 黑盒探测产出，是 Bflow 中「渠道能力配置」的**数据来源**。
> 新实现应把本矩阵作为**可配置数据**（而非硬编码判断），并支持用户在界面上
> 覆盖 —— 因为渠道能力会变（本次已实测到：CH-D 的分组通道从有变无）。
>
> 渠道一律以**代号**（CH-A ～ CH-F）指代，真实名称与域名只存在于本地配置
> `channels.json`（已 gitignore）。

## 数据结构定义

```jsonc
{
  "channel_id": "ch-a",
  "channel_name": "CH-A",
  "base_url": "https://<base-url>",
  "protocol": "openai-images",
  "models": ["gpt-image-2.6-flare"],
  "output_formats": {
    "png":  { "supported": true,  "honors_request": true },
    "jpeg": { "supported": true,  "honors_request": true },
    "webp": { "supported": true,  "honors_request": true },
    "svg":  { "supported": true,  "honors_request": true },
    "gif":  { "supported": true,  "honors_request": true, "note": "平台自行决定帧数与画布，实测 960x960" }
  },
  "size_policy": "normalize_silently",
  "size_bounds": { "min_pixels": 655360, "max_pixels": 8294400, "max_edge": 3840 },
  "quality_levels": ["auto", "low", "medium", "high", "xhigh", "max"],
  "complexity_levels": ["auto", "low", "medium", "high"],
  "transparent_allowed": ["png", "webp"],
  "compression_by_format": { "png": null, "jpeg": [0, 100], "webp": [0, 100] },
  "ignores_unknown_params": true,
  "edits": {
    "supported": true,
    "max_reference_images": 3,
    "supports_mask": true,
    "validates_mask_content": false,
    "accepts_input_fidelity": true
  },
  "notes": []
}
```

> `edits.validates_mask_content: false` 表示**上游不校验遮罩文件**，
> 客户端必须自行预校验（尺寸、格式、alpha 通道），否则用户会得到
> 「遮罩没生效」却毫不知情的结果。

### 字段语义

| 字段 | 含义 |
|---|---|
| `honors_request` | **关键**。渠道是否真的按 `output_format` 出图。`false` = 会静默返回别的格式 |
| `size_policy` | `normalize_silently`（静默改写）/ `reject`（越界报错）/ `fixed`（固定输出） |
| `size_bounds` | 总像素下限/上限、长边上限（来自 OpenAI 模型约束，各渠道实测一致）|
| `ignores_unknown_params` | 是否静默忽略未定义字段（决定能否用「发乱字段探能力」——实测都不能）|

---

## 矩阵总表

### ch-a · CH-A · gpt-image-2.6-flare

**基准渠道**（行为最规范，可作为新实现的参考行为）

| 能力 | 值 |
|---|---|
| output_format | png ✅ / jpeg ✅ / webp ✅ / svg ✅ / gif ✅（全部 honor）|
| size_policy | `normalize_silently` |
| size_bounds | 655,360 ～ 8,294,400 像素；长边 3,840 |
| 实测尺寸映射 | 256→1024²、512→1024²、768→816²、896→896²、1024→1024²、1536×1024→精确、2048²→精确、3072²→2880² |
| quality | auto / low / medium / high / **xhigh** / **max** 全支持 |
| complexity | low / medium / high 全支持 |
| background | auto / transparent / opaque 全接受 |
| compression | png 忽略压缩率；jpeg / webp 支持 0～100 |
| 未知参数 | 静默忽略 |
| n | 1、2 均支持 |
| **edits** | ✅ 支持（multipart）|
| 参考图数量 | 至少 3 张（上限待测）|
| 编辑链路参数 | `input_fidelity`（high/low）、`complexity`、`output_format` 均接受 |
| 编辑链路 n | ✅ n=2 支持 |
| **mask** | ⚠️ **上游不校验 mask 内容**，非法 mask 被静默忽略 → 客户端须预校验 |

### ch-b · CH-B · gpt-image-2.6-flare

**严格模式**：越界即报错，格式部分静默丢弃

| 能力 | 值 |
|---|---|
| output_format | png ✅ / jpeg ✅ / **webp ⚠️ 静默→png** / **svg ⚠️ 静默→png** / **gif ❌ 400** |
| size_policy | `reject` |
| 实测尺寸 | 256/512/768 ❌（低于下限）；896/1024/1536×1024/2048² ✅ 精确；3072² ❌（超上限）|
| GIF 限制 | 4 帧 @1024² → 400，拼版尝试 4096×2048，长边超 3840 |
| quality | auto / low / medium / high / **xhigh** / **max** 全支持 |
| complexity | low / medium / high 全支持 |
| background | auto / transparent / opaque 全接受 |
| compression | **png 不接受 <100**；jpeg 任意；**webp 被丢弃后按 png 处理** |
| 未知参数 | 静默忽略 |
| n | 1、2 均支持 |

### ch-c · CH-C · gpt-image-2.6-flare

**格式完全错乱**：不 honor output_format，尺寸走固定值

| 能力 | 值 |
|---|---|
| output_format | **全部不 honor**：请求 png→webp、jpeg→png、webp→png、svg→png、gif→gif |
| size_policy | `fixed`（方形请求一律 1254×1254；1536×1024 精确；2048² → 500 内部错误）|
| size_bounds | 655,360 ～ 8,294,400（报错为中文并直接给出区间）|
| quality | auto / low / medium / high / xhigh / max 全支持 |
| complexity | low / medium / high 全支持 |
| 错误风格 | 中文报错，含 `type` / `code` 结构化字段 |
| 特殊错误 | 500 `服务内部错误，请稍后重试`（可重试）|

### ch-d · CH-D

| 能力 | 值 |
|---|---|
| 模型可用性 | ⚠️ 分组 <group> 下 gpt-image-2.6 无通道（503）——**能力随时间变化** |
| 已知行为（历史）| svg → 400（分组不支持）/ 503（提示词风格限制 `svg_generation_failed`）|

### ch-e · CH-E

| 能力 | 值 |
|---|---|
| 模型可用性 | ❌ 503 `Image generation is temporarily unavailable`（暂时不可用）|

### ch-f · CH-F

| 能力 | 值 |
|---|---|
| gpt-image-2.6-* | ❌ 503 分组 <group> 无通道 |
| gpt-image-2 | ✅ 200（老模型，规格不同，待单独探测）|

---

## 跨渠道对比（最有价值的部分）

### 同一参数，四种处理方式

| 行为维度 | CH-A | CH-B | CH-C | CH-D |
|---|---|---|---|---|
| 越界尺寸 | 静默改写 | **报错 400** | 报错 400（中文）| 报错 400 |
| 不支持的格式 | 正常支持 | **静默换 png** | **静默换 png/webp** | 报错 400 |
| GIF | 支持（960²）| 报错（拼版超限）| 支持（621×623）| 报错（单帧 <1024）|
| 未知参数 | 静默忽略 | — | — | — |
| 报错语言 | 英文 | 英文 | **中文** | 英文 |

### 三条硬结论

1. **`output_format` 不可信**。4 个渠道里只有 CH-A 完整 honor。调用方**必须**按魔数核对产物格式。
2. **尺寸策略必须按渠道配置**，不能全局假设。`reject` / `normalize_silently` / `fixed` 三种都存在。
3. **能力会随时间变化**（CH-D 的通道从有变无）。→ 能力表必须**可配置、可热更新**，
   不能写死在代码里。

---

## 对 Bflow 实现的直接要求

| 编号 | 要求 | 来源 |
|---|---|---|
| B-1 | 渠道能力以**数据表**驱动，支持用户覆盖，不硬编码 | CH-C / CH-B 行为互不相同 |
| B-2 | 产物格式与尺寸**一律按魔数解析**，不信任上游声明 | CH-B/CH-C 静默换格式 |
| B-3 | 格式或尺寸与请求不符 → **交付产物 + 提示**，不丢弃、不静默 | 已计费 |
| B-4 | 上游明确拒绝 → 直接报错，不重试不换格式 | SVG 分组限制实测 |
| B-5 | 5xx 区分「可重试」与「参数错误」 | CH-C 500 内部错误 |
| B-6 | 尺寸越界时按渠道策略处理（报错 or 归一化）| CH-A vs CH-B |
| B-7 | 错误提示需识别 `svg_generation_failed` 等**错误码**（不只靠报文措辞）| gpt-image-2.6 系报文不含参数名 |

---

## 探测方法学（供后续复现）

```bash
cd D:/codes/Bflow

# 列出渠道
python tools/probe_platforms.py --list

# 探测单渠道（指定组）
python -u tools/probe_platforms.py --channel ch-a --groups formats,sizes

# 补跑网络失败的用例（status=0）
python -u tools/probe_platforms.py --channel ch-c --groups sizes \
    --only-failed probe-data/probe-20261116-182441.json --out probe-data
```

**探测组清单**：`formats` / `quality` / `complexity` / `sizes` / `background` /
`compression` / `n` / `unknown`

**成本提示**：每个 200 响应视为已计费。失败（4xx/5xx）不计费。
本轮 P0 约 60 次成功请求、约 15 次失败请求。
