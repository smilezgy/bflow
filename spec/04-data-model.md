# Bflow —— 数据模型与存储

> 存储目标：单机可跑、可迁移、支持 Docker 命名卷持久化。
> 选型原则：**SQLite 为主**（零运维、单文件、事务可靠），二进制产物落文件系统，
> 结构化元数据落 SQLite。

---

## 0. 总体布局

```
<data_root>/
├── bflow.db                  # SQLite 主库
├── assets/
│   ├── input/                # 上传的参考图、mask
│   ├── output/               # 生成产物
│   └── thumbs/               # 缩略图（按需生成）
├── canvases/                 # 画布工程文件
├── workflows/                # ComfyUI 工作流
├── cache/                    # 临时缓存（可安全删除）
└── logs/
    └── app.log
```

`<data_root>` 由 `GET/PATCH /api/storage/settings` 配置，默认：
- Windows：`%APPDATA%/Bflow`
- Linux：`~/.local/share/Bflow`（容器内 `/data`）
- macOS：`~/Library/Application Support/Bflow`

---

## 1. 存储选型理由

| 候选 | 结论 | 理由 |
|---|---|---|
| **SQLite** | ✅ 采用 | 零运维、单文件、事务完整、Python 内置 `sqlite3`；并发写用 WAL 模式足够 |
| JSON 文件 | ❌ | 并发写会损坏；查询需全量加载；历史记录会膨胀 |
| PostgreSQL / MySQL | ❌（本期）| 需要额外服务，与「单机可跑」冲突；后续可加适配层 |
| 对象存储（S3/OSS）| ❌（本期）| 引入外部依赖；产物访问走本地即可 |

**扩展路径**：所有仓储层通过 `Repository` 协议访问，接口与 SQLite 实现一一对应，
后续要换 Postgres 只换实现不改业务。

---

## 2. 核心实体

### 2.1 ER 概览

```
channel 1───────n channel_capability_probe
   │
   ├───────────n channel_model      （模型 → 协议绑定）
   │
   ├───────────n task
   │              │ 1
   │              └──────n task_asset
   │
   ├───────────n asset
   │
   └───────────n api_credential

canvas 1───────n canvas_node
   │                │
   │                └──────n canvas_edge
   └── n──────────n asset   （画布引用资源）

asset 1───────n asset_tag
```

---

### 2.1.1 多协议渠道：为什么协议要下沉到模型级

一个**渠道 = 一个接入点**（`base_url` + 凭据）。但聚合网关
（newapi / sub2api / openrouter 之类）把多个厂商的模型聚在同一个地址下，
它的模型列表里混着不同厂商的模型：

| 模型 | 需要的协议 | 请求形态 |
|---|---|---|
| `gpt-image-*` | `openai-images` | `POST /v1/images/generations` |
| `gemini-*-image` | `gemini` | `POST /v1beta/models/{model}:generateContent` |
| `doubao-seedream-*` | `ark` | 提交 + 轮询 |
| `flux-*` / `sd-*` | `comfyui` / `runninghub` | 工作流 JSON |

如果协议只存在于渠道层，一个聚合网关就**没法用**——它没法同时用
OpenAI 方言和 Gemini 方言。因此：

- `channel.protocol` 语义为 **默认协议**（模型未单独指定时的兜底）；
- `channel_model.protocol` 是该模型**实际使用**的协议。

协议解析优先级：**模型绑定 > 渠道默认协议 > `openai-images`**。

---

### 2.2 `channel` —— 渠道

```sql
CREATE TABLE channel (
    id              TEXT PRIMARY KEY,          -- ch-a / 用户自定义 UUID
    display_name    TEXT NOT NULL,             -- 界面显示名
    protocol        TEXT NOT NULL,             -- **默认协议**：模型未单独指定时的兜底
    base_url        TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    config_json     TEXT NOT NULL DEFAULT '{}',-- 协议特有配置（端点路径、额外头等）
    credential_id   TEXT,                      -- → api_credential.id
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
```

**设计要点**：
- `id` 用**中性代号**而非域名派生 —— 界面与日志不泄露渠道来源（与 P0 脱敏原则一致）。
- `config_json` 承载协议差异，避免为每个渠道加字段。
- 渠道能力**不在本表**，独立存（见 §2.3），因为它有独立的生命周期与来源标记。
- `protocol` 是**默认协议**，不是唯一协议 —— 多协议渠道见 §2.1.1 与 §2.3.1。

### 2.3 `channel_capability` —— 渠道能力表

```sql
CREATE TABLE channel_capability (
    channel_id      TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    capability_json TEXT NOT NULL,   -- 见 02-capability-matrix.md 的数据结构
    source          TEXT NOT NULL,   -- probe / manual / default
    verified_at     REAL,            -- 实测时间；manual 覆盖时为 NULL
    PRIMARY KEY (channel_id, model)
);
```

**为什么独立成表**：

| 理由 | 说明 |
|---|---|
| 生命周期不同 | 渠道配置由用户维护；能力表由实测维护，两者更新频率与来源都不同 |
| 需要溯源 | `source` 区分「实测得出」与「用户断言」，界面据此显示可信度徽章 |
| 能力随时间失效 | `verified_at` 让前端能提示「数据已过期」 |
| 人工覆盖 | 用户可覆盖实测值（渠道能力变化比复测快，见 P0 结论）|

**`capability_json` 结构**（与 `02-capability-matrix.md` 一致）：

```jsonc
{
  "output_formats": {
    "png":  { "supported": true, "honors_request": true },
    "svg":  { "supported": true, "honors_request": true }
  },
  "size_policy": "normalize_silently",   // reject | normalize_silently | fixed
  "size_bounds": { "min_pixels": 655360, "max_pixels": 8294400, "max_edge": 3840 },
  "quality_levels": ["auto", "low", "medium", "high", "xhigh", "max"],
  "complexity_levels": ["auto", "low", "medium", "high"],
  "backgrounds": ["auto", "transparent", "opaque"],
  "transparent_formats": ["png", "webp"],
  "compression_by_format": { "png": null, "jpeg": [0, 100], "webp": [0, 100] },
  "max_reference_images": 4,
  "supports_edits": true,
  "notes": ["GIF 帧数由平台自行决定"]
}
```

### 2.3.1 `channel_model` —— 模型注册表与协议绑定

```sql
CREATE TABLE channel_model (
    channel_id  TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    protocol    TEXT NOT NULL,          -- 该模型实际使用的协议
    enabled     INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',  -- 模型级配置，覆盖渠道级
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (channel_id, model)
);
```

**为什么独立成表**（而不是把 `protocol` 塞进 `channel_capability`）：

| 理由 | 说明 |
|---|---|
| 绑定 ≠ 能力 | 协议是「怎么发请求」，能力表是「这个模型能做什么」。前者由用户配置，后者由实测得出 |
| 生命周期不同 | 改协议不该作废能力表记录本身；改协议只是让旧能力**失效** |
| 需要模型级配置 | 同一网关下不同模型可能需要不同参数（如不同的工作流模板），`config_json` 逐模型覆盖 |
| 拉取与登记分离 | 模型可以从上游拉取（`/v1/models`），拉取本身与能力表无关 |

**关键约束**：

1. **协议解析优先级**：模型绑定 > 渠道默认协议 > `openai-images`。
2. **协议变更作废旧能力**：改了协议，该模型的能力表重置为新协议的默认值。
   旧协议下实测出的结论（支持哪些格式、尺寸策略、能否编辑）对新协议不成立，
   留着会误导前置校验（`03-api-contract.md` §2.3）。
3. **能力表不跨协议继承**：读某模型的能力时，只继承**同协议**其他模型的能力表。
   否则 Gemini 模型会继承到 OpenAI 模型的"支持 svg"，放行一个该协议
   根本不接受的格式。

**`config_json` 合并规则**：模型级覆盖渠道级（同名字段模型级优先），
合并后的结果传给协议适配器。

### 2.4 `api_credential` —— 凭据

```sql
CREATE TABLE api_credential (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,        -- bearer / basic / none
    secret_enc    BLOB NOT NULL,        -- 加密后的密钥
    extra_json    TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
```

**加密方案**（单机场景，无需 KMS）：

| 项 | 方案 |
|---|---|
| 算法 | AES-256-GCM |
| 主密钥来源 | `BFLOW_MASTER_KEY` 环境变量；未设置时自动生成并写入 `<data_root>/master.key`（权限 0600）|
| 附加认证 | GCM 自带认证标签 |
| 密钥轮换 | 启动时检测 `BFLOW_MASTER_KEY` 变更 → 用旧密钥解密、新密钥重加密 |

**密钥永不回传 API**：所有读接口返回 `"***"`。

### 2.5 `asset` —— 资源（上传图 / 生成产物）

```sql
CREATE TABLE asset (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,      -- input / output / mask / thumb
    path              TEXT NOT NULL,      -- 相对 <data_root> 的路径
    format            TEXT NOT NULL,      -- png/jpeg/webp/svg/gif —— 一律按魔数判定
    width             INTEGER NOT NULL DEFAULT 0,
    height            INTEGER NOT NULL DEFAULT 0,
    bytes             INTEGER NOT NULL DEFAULT 0,
    sha256            TEXT,               -- 去重与完整性校验
    origin            TEXT NOT NULL,      -- upload / generate / canvas
    created_at        REAL NOT NULL
);
CREATE INDEX idx_asset_created ON asset(created_at DESC);
CREATE INDEX idx_asset_sha ON asset(sha256);
```

**关键设计 —— `format` 存的是产物真实格式，不是请求值**：
P0 实测表明渠道返回的格式经常与请求不符（CH-C 甚至「请求 png → 返回 webp」）。
若把请求值落库，后续所有「按格式过滤」「按格式决定能否编辑」的逻辑都会错。

`requested_format` 属于**单次生成的事实**，记在 `task_asset` 上，不污染资源本身。

### 2.6 `task` —— 生成任务

```sql
CREATE TABLE task (
    id                TEXT PRIMARY KEY,
    channel_id        TEXT NOT NULL,
    model             TEXT NOT NULL,
    operation         TEXT NOT NULL,      -- generate / edit / upscale / ...
    status            TEXT NOT NULL,      -- pending/running/succeeded/failed/canceled
    prompt            TEXT NOT NULL DEFAULT '',
    request_json      TEXT NOT NULL,      -- 完整请求参数（原样存档）
    requested_size    TEXT,
    requested_format  TEXT,
    actual_size       TEXT,
    actual_format     TEXT,
    error_code        TEXT,
    error_message     TEXT,
    warnings_json     TEXT NOT NULL DEFAULT '[]',
    progress          REAL NOT NULL DEFAULT 0,
    upstream_task_id  TEXT,               -- 异步渠道的外部任务号
    billed            INTEGER NOT NULL DEFAULT 0,  -- 是否已产生费用
    created_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL
);
CREATE INDEX idx_task_created ON task(created_at DESC);
CREATE INDEX idx_task_status ON task(status);
CREATE INDEX idx_task_channel ON task(channel_id, created_at DESC);
```

**关键设计 —— `requested_*` 与 `actual_*` 并列存储**：

| 字段对 | 用途 |
|---|---|
| `requested_format` / `actual_format` | 格式是否被渠道执行 —— 直接驱动 `FORMAT_NOT_HONORED` 警告 |
| `requested_size` / `actual_size` | 尺寸是否被渠道改写 —— 驱动 `SIZE_NORMALIZED` 警告 |

P0 实测这两类偏差在多数渠道**普遍存在**，不存就无法事后复盘。

**`billed` 字段**：标记是否已产生费用。用于「失败但已计费」的提示文案，
以及成本统计。

### 2.7 `task_asset` —— 任务与资源关联

```sql
CREATE TABLE task_asset (
    task_id     TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    asset_id    TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,       -- output / reference / mask
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, asset_id, role)
);
```

### 2.8 `canvas` / `canvas_node` / `canvas_edge` —— 画布

```sql
CREATE TABLE canvas (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '未命名画布',
    path         TEXT NOT NULL,           -- 画布工程文件路径
    thumbnail_id TEXT REFERENCES asset(id),
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE canvas_node (
    id           TEXT PRIMARY KEY,
    canvas_id    TEXT NOT NULL REFERENCES canvas(id) ON DELETE CASCADE,
    type         TEXT NOT NULL,          -- image / text / video / group / ...
    x            REAL NOT NULL,
    y            REAL NOT NULL,
    width        REAL NOT NULL,
    height       REAL NOT NULL,
    z_index      INTEGER NOT NULL DEFAULT 0,
    data_json    TEXT NOT NULL DEFAULT '{}',  -- 节点自有数据
    asset_id     TEXT REFERENCES asset(id),
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX idx_node_canvas ON canvas_node(canvas_id);

CREATE TABLE canvas_edge (
    id           TEXT PRIMARY KEY,
    canvas_id    TEXT NOT NULL REFERENCES canvas(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL REFERENCES canvas_node(id) ON DELETE CASCADE,
    target_id    TEXT NOT NULL REFERENCES canvas_node(id) ON DELETE CASCADE,
    label        TEXT,
    data_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_edge_canvas ON canvas_edge(canvas_id);
```

**画布工程文件与数据库的关系**：
- 数据库存**索引**（列表、检索、缩略图）
- 完整画布内容存**工程文件**（`<data_root>/canvases/<id>.bflow.json`）
- 理由：画布 JSON 读写频繁且结构演进快，用文件做主存储更简单；数据库只做检索索引

**节点 `data_json` 内容**：

```jsonc
{
  "prompt": "...",
  "channel_id": "ch-a",
  "model": "gpt-image-2.6-flare",
  "output_format": "png",
  "size": "1024x1024",
  "quality": "high",
  "complexity": "high",
  "seed": 12345,
  "task_id": "task_01H...",       // 生成后回填
  "warnings": [ ]                  // 最近一次生成的偏差提示
}
```

### 2.9 `asset_tag` —— 资源标签

```sql
CREATE TABLE asset_tag (
    asset_id  TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    PRIMARY KEY (asset_id, tag)
);
CREATE INDEX idx_tag ON asset_tag(tag);
```

### 2.10 `settings` —— 应用配置

```sql
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
```

**存哪些**：

| key | 说明 |
|---|---|
| `storage.root` | 数据根目录 |
| `ui.theme` | 主题 |
| `ui.locale` | 语言 |
| `generation.default_channel` | 默认渠道 |
| `generation.default_size` | 默认尺寸 |
| `generation.default_format` | 默认格式 |
| `update.channel` | 更新源（`github` / `none`）|
| `update.repo` | 更新源仓库地址（**指向自己的仓库**）|

**不存**：API Key（在 `api_credential`）、任何密钥。

### 2.11 `update_state` —— 更新状态

```sql
CREATE TABLE update_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    current_version   TEXT,
    remote_version    TEXT,
    last_checked_at   REAL,
    backup_path       TEXT,
    pending_rollback  INTEGER NOT NULL DEFAULT 0
);
```

单行表（`id = 1` 固定），用于「一键更新 + 出问题可回滚」。

---

## 3. SQLite 具体设置

```sql
PRAGMA journal_mode = WAL;        -- 读写并发
PRAGMA synchronous = NORMAL;      -- WAL 下的推荐值，兼顾性能与安全
PRAGMA foreign_keys = ON;         -- 级联删除生效
PRAGMA busy_timeout = 5000;       -- 锁等待，避免 "database is locked"
PRAGMA temp_store = MEMORY;
```

**迁移机制**：不引入 Alembic（避免额外依赖与学习成本），自建轻量版本管理：

```sql
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL
);
```

启动时按版本号顺序执行 `migrations/00X_xxx.sql`，全部成功才写入版本记录。
迁移脚本**只增不改**（已发布的迁移不修改，改则新建）。

---

## 4. 二进制产物的存储

| 项 | 决策 |
|---|---|
| 存储位置 | `<data_root>/assets/output/` |
| 文件名 | `<asset_id>.<ext>`，扩展名按**魔数判定** |
| 去重 | 按 `sha256`；相同内容复用同一物理文件（多行 asset 指向同一路径）|
| 缩略图 | `<data_root>/assets/thumbs/<asset_id>.webp`，宽 320px |
| 清理 | 孤儿文件清理任务：扫描目录，与 `asset` 表比对，删除无引用文件 |

**为什么不做内容寻址（sha256 作为文件名）**：
素材库里同一张图可能被反复引用，内容寻址会导致文件名不可读、且删除逻辑复杂。
当前方案用短 ID 作文件名、sha256 仅作去重索引。

---

## 5. 数据量估算

| 实体 | 单条大小 | 1 万条 | 100 万条 |
|---|---|---|---|
| `asset` | ~200 B | 2 MB | 200 MB |
| `task` | ~1 KB | 10 MB | 1 GB |
| `canvas_node` | ~500 B | 5 MB | 500 MB |
| 二进制产物 | 平均 1.5 MB | 15 GB | 1.5 TB |

**结论**：SQLite 完全能扛到百万级任务记录。真正的瓶颈是**二进制产物**，
需靠产物清理策略控制（见 §7）。

---

## 6. 并发与事务

| 场景 | 策略 |
|---|---|
| 多请求同时写 `task` | SQLite WAL 支持一写多读；写冲突由 `busy_timeout` 重试 |
| 产物落盘 + 记录入库 | 先落盘（幂等、可重试），再入库；入库失败不删文件，由孤儿清理兜底 |
| 画布保存 | 整份 JSON 原子替换（写临时文件 → `os.replace`），数据库索引最后更新 |
| 长任务 | 任务状态更新走**独立短事务**，不持有长事务跨越网络请求 |

**明确禁止**：在任何网络 I/O 期间持有数据库事务。

---

## 7. 数据保留策略

| 数据 | 默认保留 | 可配置 |
|---|---|---|
| 生成产物 | 永久（用户手动删）| 可设「自动清理 N 天前的未收藏产物」|
| 任务记录 | 90 天 | 是 |
| 画布 | 永久 | — |
| 上传的参考图 | 180 天（未被任何画布/任务引用时）| 是 |
| 缓存与缩略图 | 7 天 | — |

**孤儿文件清理**：每日一次，扫描 `assets/` 与 `asset` 表比对，
删除无引用文件；`cache/` 整个目录按时间清空。

---

## 8. 导入导出

| 场景 | 格式 | 说明 |
|---|---|---|
| 单画布导出 | `.bflow.json` | 画布结构 + 资源引用（不含二进制）|
| 完整画布包 | `.bflowpack`（zip）| 画布结构 + 全部二进制资源 |
| 单资源导出 | 原格式文件 | 直接下载 |
| 批量素材导出 | zip | 按目录结构打包 |

`.bflowpack` 结构：

```
canvas.bflow.json
assets/
  as_3c9d1e.png
  as_4f2a8b.webp
manifest.json      # 版本、创建时间、资源校验和
```

---

## 9. 与 P0 结论的对应

| 数据模型设计 | 来自哪条实测结论 |
|---|---|
| `asset.format` 存真实格式而非请求值 | CH-B/CH-C 静默换格式 |
| `task.requested_*` 与 `actual_*` 并列 | 尺寸/格式偏差普遍存在 |
| `task.billed` 字段 | 上游 200 即计费，失败提示要区分是否已付费 |
| `channel_capability` 独立表 + `source` + `verified_at` | 渠道能力会变（CH-D 通道下线） |
| `size_policy` 三态枚举 | CH-A 归一化 / CH-B 报错 / CH-C 固定值 |
| `channel.id` 用中性代号 | 公开仓库不暴露渠道来源 |
| `api_credential` 加密存储 | 通用安全基线 |
| 产物保留 + 孤儿清理 | 产物量大，需可控增长 |
