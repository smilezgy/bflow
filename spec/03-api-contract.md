# Bflow —— 对外 API 契约

> 设计依据：P0 渠道实测结论（见 `01-platform-behavior.md` / `02-capability-matrix.md`）。
> 本文档定义 Bflow **对外暴露**的 HTTP 接口契约。内部实现可自由，但契约一旦发布即视为
> 公共 API，变更需走版本号。

---

## 0. 通用约定

### 0.1 基础

| 项 | 约定 |
|---|---|
| 基址 | `/api` |
| 编码 | 请求与响应均为 `application/json; charset=utf-8`（上传除外）|
| 时间 | Unix 秒（浮点）|
| 标识符 | 字符串（UUID v4 或带前缀短 ID，如 `img_7f3a2b`）|
| 版本 | URL 不带版本号，通过 `VERSION` 文件与响应头 `X-Bflow-Version` 声明 |

### 0.2 统一响应包络

**成功**：

```json
{
  "ok": true,
  "data": { },
  "meta": {
    "request_id": "req_01H...",
    "version": "2026.11.16",
    "elapsed_ms": 1234
  }
}
```

**失败**：

```json
{
  "ok": false,
  "error": {
    "code": "FORMAT_NOT_HONORED",
    "message": "你选择了 SVG 格式，但上游实际返回的是 PNG。",
    "hint": "如需 SVG，请换一个支持该格式的渠道。",
    "detail": { },
    "retryable": false
  },
  "meta": { "request_id": "req_01H...", "version": "2026.11.16" }
}
```

> **设计依据**：P0 实测发现渠道错误报文极不统一（英文 / 中文 / 结构不一 / 有的无 code）。
> 因此 Bflow **必须**在服务端归一化，客户端不应直接看到上游原文。

### 0.3 错误码表

| code | HTTP | 含义 | 可重试 |
|---|---|---|---|
| `INVALID_REQUEST` | 400 | 请求参数不合法（自建校验）| 否 |
| `PROMPT_REQUIRED` | 400 | 提示词为空 | 否 |
| `NO_REFERENCE_IMAGE` | 400 | 编辑请求未带参考图 | 否 |
| `REFERENCE_TOO_MANY` | 400 | 参考图超出渠道上限 | 否 |
| `FORMAT_NOT_SUPPORTED` | 400 | 渠道明确不支持所选格式 | 否 |
| `SIZE_OUT_OF_RANGE` | 400 | 尺寸超出渠道区间 | 否 |
| `SIZE_NORMALIZED` | 200 | 渠道静默改了尺寸（**警告**，见 §2.3）| — |
| `CONTENT_REJECTED` | 502 | 上游以内容策略拒绝（如 SVG 不适合照片）| 否 |
| `FORMAT_NOT_HONORED` | 200 | 上游返回了别的格式（**警告**，产物已交付）| — |
| `CHANNEL_UNAVAILABLE` | 503 | 渠道无模型通道 / 上游暂时不可用 | 是 |
| `CHANNEL_INTERNAL_ERROR` | 502 | 渠道内部故障（如 500 内部错误）| 是 |
| `UPSTREAM_TIMEOUT` | 504 | 上游超时 | 是 |
| `RATE_LIMITED` | 429 | 被限流 | 是 |
| `STORAGE_ERROR` | 500 | 落盘失败 | 是 |
| `NOT_FOUND` | 404 | 资源不存在 | 否 |

> **`FORMAT_NOT_HONORED` 与 `SIZE_NORMALIZED` 返回 200 而非 4xx**：
> 上游已经生成并计费，产物有效，只是与请求不完全一致。P0 实测这在 4 个渠道里
> 普遍存在（CH-B 丢弃 webp/svg、CH-C 格式全乱、CH-A 静默归一化尺寸）。
> 走错误通道会导致「付费了但拿不到图」。

### 0.4 能力查询

客户端在渲染选项前需知道渠道支持什么：

```http
GET /api/channels
```

```json
{
  "ok": true,
  "data": {
    "channels": [
      {
        "id": "ch-a",
        "display_name": "渠道 A",
        "protocol": "openai-images",
        "models": ["gpt-image-2.6-flare"],
        "capabilities": {
          "output_formats": {
            "png":  { "supported": true,  "honors_request": true },
            "jpeg": { "supported": true,  "honors_request": true },
            "webp": { "supported": true,  "honors_request": true },
            "svg":  { "supported": true,  "honors_request": true },
            "gif":  { "supported": true,  "honors_request": true }
          },
          "quality_levels": ["auto", "low", "medium", "high", "xhigh", "max"],
          "complexity_levels": ["auto", "low", "medium", "high"],
          "backgrounds": ["auto", "transparent", "opaque"],
          "transparent_formats": ["png", "webp"],
          "size_bounds": { "min_pixels": 655360, "max_pixels": 8294400, "max_edge": 3840 },
          "max_reference_images": 4,
          "supports_edits": true
        },
        "last_verified_at": "2026-11-16T18:18:00Z",
        "verified": true
      }
    ]
  }
}
```

> `verified` 为渠道能力表的实测状态；`last_verified_at` 超过 30 天时前端应提示
> 「能力数据可能已过期」。P0 实测到渠道能力会在数小时内变化（分组通道下线）。

---

## 1. 生图

### 1.1 `POST /api/images/generate`

**请求**：

```json
{
  "prompt": "a red apple on a wooden table, studio light",
  "channel_id": "ch-a",
  "model": "gpt-image-2.6-flare",
  "size": "1024x1024",
  "count": 1,
  "output_format": "png",
  "output_compression": 80,
  "background": "auto",
  "quality": "high",
  "complexity": "high",
  "reference_images": [],
  "save_to_library": true
}
```

**字段约束**：

| 字段 | 必填 | 约束 |
|---|---|---|
| `prompt` | ✅ | 非空字符串 |
| `channel_id` | ✅ | 须为已配置渠道 |
| `model` | ✅ | 须在渠道 `models` 内 |
| `size` | ❌ | 默认 `1024x1024`；超渠道区间时按 §2.3 处理 |
| `count` | ❌ | 默认 1；上限 4；SVG 强制为 1 |
| `output_format` | ❌ | `png` / `jpeg` / `webp` / `svg` / `gif`；默认 `png` |
| `output_compression` | ❌ | 0–100；仅 `jpeg` / `webp` 有效；`png` 传了会被忽略 |
| `background` | ❌ | `auto` / `transparent` / `opaque`；`transparent` 要求格式支持 alpha |
| `quality` | ❌ | 见渠道 `quality_levels` |
| `complexity` | ❌ | 见渠道 `complexity_levels` |
| `reference_images` | ❌ | 编辑模式；元素为已上传资源 ID |

**响应**：

```json
{
  "ok": true,
  "data": {
    "task_id": "task_01H...",
    "images": [
      {
        "id": "img_7f3a2b",
        "url": "/api/assets/img_7f3a2b",
        "width": 1024,
        "height": 1024,
        "format": "png",
        "bytes": 1089794,
        "requested_format": "png",
        "requested_size": "1024x1024"
      }
    ],
    "warnings": [
      {
        "code": "FORMAT_NOT_HONORED",
        "message": "你选择了 SVG 格式，但上游实际返回的是 PNG。",
        "hint": "如需 SVG，请换一个支持该格式的渠道。",
        "actual": { "format": "png", "width": 1254, "height": 1254 }
      }
    ]
  }
}
```

> **关键设计**：产物里的 `format` / `width` / `height` 一律来自**产物魔数解析**，
> 不是请求值。`requested_format` / `requested_size` 单独保留，供前端对比提示。

### 1.2 异步生成

长耗时渠道（异步协议、ComfyUI、RunningHub）需要异步：

```http
POST /api/images/generate
```

若渠道为异步模式，响应改为：

```json
{
  "ok": true,
  "data": {
    "task_id": "task_01H...",
    "status": "pending",
    "poll_url": "/api/tasks/task_01H..."
  }
}
```

### 1.3 `GET /api/tasks/{task_id}`

```json
{
  "ok": true,
  "data": {
    "task_id": "task_01H...",
    "status": "succeeded",
    "progress": 1.0,
    "images": [ ],
    "warnings": [ ],
    "error": null,
    "created_at": 1794797186.2,
    "finished_at": 1794797251.8
  }
}
```

**状态机**：

```
pending ──→ running ──→ succeeded
   │           │
   │           ├──→ failed          （可重试错误可由客户端重新提交）
   └───────────┴──→ canceled
```

**状态取值**：`pending` / `running` / `succeeded` / `failed` / `canceled`

### 1.4 `POST /api/tasks/{task_id}/cancel`

```json
{ "ok": true, "data": { "task_id": "task_01H...", "status": "canceled" } }
```

---

## 2. 尺寸与格式的处理策略

这一节是 P0 实测的直接产物，**决定了 API 的核心语义**。

### 2.1 尺寸

| 渠道 `size_policy` | 请求越界时的行为 | API 表现 |
|---|---|---|
| `reject` | 上游返回 400 | 抛 `SIZE_OUT_OF_RANGE`（400），**不出图** |
| `normalize_silently` | 上游返回 200，尺寸被改 | 正常返回 + `SIZE_NORMALIZED` 警告 |
| `fixed` | 上游返回 200，固定尺寸 | 正常返回 + `SIZE_NORMALIZED` 警告 |

**`SIZE_NORMALIZED` 警告内容示例**：

```json
{
  "code": "SIZE_NORMALIZED",
  "message": "你请求了 256x256，该渠道输出为 1024x1024。",
  "actual": { "width": 1024, "height": 1024 }
}
```

> 客户端**不得**因为出现该警告而丢弃产物 —— 产物有效，只是尺寸与请求不同。

### 2.2 格式

| 上游表现 | API 表现 |
|---|---|
| 返回的就是所选格式 | 正常返回，无警告 |
| 上游 400 明确拒绝该格式 | 抛 `FORMAT_NOT_SUPPORTED`（400），**不出图**（未计费）|
| 上游 200 但返回别的格式 | 正常返回 + `FORMAT_NOT_HONORED` 警告（**已计费，产物照交**）|

### 2.3 Bflow 的主动行为约束

| 规则 | 说明 |
|---|---|
| **不降级格式** | 请求体始终携带用户所选的 `output_format`；渠道不支持就报错，不偷偷换格式 |
| **不篡改尺寸** | 不主动缩放/放大以迎合渠道；让渠道自己决定，然后如实告知实际尺寸 |
| **不丢弃产物** | 一旦上游返回 200，产物必须交付，即使格式/尺寸与请求不符 |
| **不隐藏偏差** | 任何与请求不一致的实际结果，都必须以 warning 形式回传 |

> 这四条是 P0 实测总结的最重要的行为约束，直接来自「渠道能力不可信」这一事实。

---

## 3. 参考图与编辑

### 3.1 上传

```http
POST /api/assets/upload
Content-Type: multipart/form-data
```

| 字段 | 说明 |
|---|---|
| `file` | 图片文件（可多个，同名多文件字段）|
| `purpose` | `reference` / `mask` / `output` / `library` |

**响应**：

```json
{
  "ok": true,
  "data": {
    "assets": [
      {
        "id": "as_3c9d1e",
        "url": "/api/assets/as_3c9d1e",
        "width": 1024,
        "height": 1024,
        "format": "png",
        "bytes": 1089794
      }
    ]
  }
}
```

### 3.2 编辑

编辑复用 `POST /api/images/generate`，通过 `reference_images` 传入已上传资源 ID：

```json
{
  "prompt": "把背景换成蓝色渐变",
  "channel_id": "ch-a",
  "model": "gpt-image-2.6-flare",
  "reference_images": ["as_3c9d1e"],
  "output_format": "png"
}
```

| 约束 | 值 | 来源 |
|---|---|---|
| 参考图数量上限 | 按渠道 `max_reference_images`（P1 实测待填）| 待实测 |
| mask 支持 | 部分渠道支持局部遮罩编辑 | 待实测 |
| `input_fidelity` | gpt-image-2 及以后官方已移除 | 已知 |

**错误**：`NO_REFERENCE_IMAGE`（400）/ `REFERENCE_TOO_MANY`（400）

---

## 4. 资源与历史

| 端点 | 方法 | 说明 |
|---|---|---|
| `GET /api/assets` | GET | 列出资源（分页、过滤）|
| `GET /api/assets/{id}` | GET | 资源详情 |
| `DELETE /api/assets/{id}` | DELETE | 删除资源 |
| `GET /api/history` | GET | 生成历史 |
| `DELETE /api/history` | DELETE | 批量清理历史 |
| `GET /api/tasks` | GET | 任务列表 |

**分页**：游标式，`?cursor=<opaque>&limit=<n>`，响应含 `next_cursor`。

---

## 5. 渠道管理

| 端点 | 方法 | 说明 |
|---|---|---|
| `GET /api/channels` | GET | 渠道列表 + 能力 |
| `POST /api/channels` | POST | 新增渠道 |
| `PATCH /api/channels/{id}` | PATCH | 修改渠道配置（含**能力表覆盖**）|
| `DELETE /api/channels/{id}` | DELETE | 删除渠道 |
| `POST /api/channels/{id}/verify` | POST | 触发一次实测以刷新能力表 |

**能力表覆盖**：`PATCH /api/channels/{id}` 可提交 `capabilities` 字段覆盖实测值。
理由：P0 实测到渠道能力变化快于人工复测频率，且用户比服务端更清楚自己的渠道。

```json
{
  "ok": true,
  "data": {
    "id": "ch-c",
    "capabilities": {
      "output_formats": {
        "png":  { "supported": true, "honors_request": false },
        "svg":  { "supported": false, "honors_request": false }
      }
    },
    "source": "manual"
  }
}
```

`source` 取值：`probe`（实测）/ `manual`（人工覆盖）/ `default`（内置兜底）。

---

## 6. 配置与存储

| 端点 | 方法 | 说明 |
|---|---|---|
| `GET /api/config` | GET | 应用配置（不含密钥）|
| `PATCH /api/config` | PATCH | 修改配置 |
| `GET /api/storage/settings` | GET | 存储路径设置 |
| `PATCH /api/storage/settings` | PATCH | 修改存储路径 |

**密钥处理**：API Key 只写不读。`GET /api/channels` 返回的渠道对象中
`api_key` 字段恒为 `"***"`，**永不回传明文**。

---

## 7. WebSocket

### 7.1 `WS /ws/stats`

推送运行状态：

```json
{ "type": "stats", "data": { "cpu": 12.4, "mem": 340, "tasks_running": 2, "tasks_queued": 0 } }
```

### 7.2 `WS /ws/tasks`

推送任务状态变更：

```json
{ "type": "task_update", "data": { "task_id": "task_01H...", "status": "succeeded", "images": [] } }
```

**心跳**：应用层 ping/pong（间隔 30s）；断线后客户端指数退避重连。

---

## 8. 契约变更规则

| 变更类型 | 是否破坏性 | 处理 |
|---|---|---|
| 新增可选字段 | 否 | 直接发布 |
| 新增端点 | 否 | 直接发布 |
| 新增错误码 | 否 | 客户端应按未知码降级处理 |
| 新增 `warnings[].code` | 否 | 同上 |
| 必填字段变更 | **是** | 主版本号 +1 |
| 字段语义变更 | **是** | 主版本号 +1 |
| 删除字段 / 端点 | **是** | 主版本号 +1，保留一个版本的兼容期 |

> 客户端**必须**对未知的 `error.code` 与 `warnings[].code` 做优雅降级
> （展示原始 message，不崩溃）。

---

## 9. 与 P0 结论的对应关系

| 本文档条款 | 来自哪条实测结论 |
|---|---|
| §0.3 错误码表 | 各渠道报文格式不一，必须服务端归一化 |
| §0.4 能力查询 + `verified` | 能力会变（CH-D 分组通道下线）|
| §1.1 产物含 `format`/`width`/`height` | 产物格式尺寸不可信，必须按魔数解析 |
| §2.1 `SIZE_NORMALIZED` | CH-A 静默归一化、CH-B 报错、CH-C 固定值 |
| §2.2 `FORMAT_NOT_HONORED` | CH-B/CH-C 静默换格式 |
| §2.3 四条行为约束 | 综合全部渠道实测 |
| §5 能力表人工覆盖 | 能力变化快于复测频率 |
| §6 密钥不回传 | 通用安全基线 |
