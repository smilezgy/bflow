# Bflow

> 面向 AI 多模态创作的工作台 —— **clean-room 重写版**。
>
> 目标：独立品牌、可商用、不受上游更新干扰。
>
> 状态：**P2 已实现** —— 可运行的完整应用（工作台 / 画布 / 素材库 / 任务中心 / 渠道管理 / 设置）。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 快速开始

**前置条件：Python 3.10+**（Windows 安装时记得勾选 *Add python.exe to PATH*）。

```bash
# Windows
run.bat

# Linux / macOS
./start.sh

# 桌面端（原生窗口，WebView2 / 系统 WebView）
pip install -r requirements-desktop.txt
python desktop.py

# 容器
docker compose up -d --build
```

启动后访问 <http://127.0.0.1:8000/>。

首次使用：进入「渠道」→ 新增渠道（地址 + API Key）→ 编辑能力表 → 回工作台生成。

### 聚合网关：一个渠道挂多个协议

newapi / sub2api / openrouter 这类聚合网关把多个厂商的模型聚在同一个地址下，
但每个模型要的协议不同（`gpt-image-*` 走 OpenAI 协议、`gemini-*-image` 走
Gemini 协议、`doubao-seedream-*` 走方舟协议）。所以 Bflow 的协议绑在**模型**上，
不是一个渠道只能有一种协议。

1. 新增渠道时填聚合网关的地址与密钥，协议选该网关的**默认协议**（多数是 OpenAI 兼容）。
2. 进「渠道」→ 该渠道的 **「模型与协议」** → 点 **「拉取模型」**，从 `/v1/models`
   拉取模型清单。协议会按模型名自动建议（`gemini-*` → Gemini、`doubao-*` → 方舟、
   `flux-*` → 工作流协议），**建议只是建议**，按需改。
3. 保存。工作台生成时自动按所选模型走它自己的协议。

同一个渠道下每个模型的能力表按各自协议独立计算 —— Gemini 模型不会显示 SVG 选项，
方舟模型和 OpenAI 模型的尺寸策略各按各的。

### 找不到 Python？

Windows 自带的 `WindowsApps\python.exe` 只是 Microsoft Store 的 **0 字节占位符**，
执行它只会打印 "Python was not found" 并返回 9009，看起来像"没装 Python"。
启动脚本会实际执行候选解释器来验证，**自动跳过这个占位符**。

如果仍然找不到，任选一种：

| 方式 | 操作 |
|---|---|
| 装正式版（推荐） | <https://www.python.org/downloads/>，安装时勾选 *Add python.exe to PATH* |
| 放进项目目录 | 把解释器放到 `<项目目录>\python\python.exe`，脚本会自动识别 |
| 显式指定 | `set PYTHON=C:\path\to\python.exe` 然后运行 `run.bat`（Linux/macOS 同理 `PYTHON=... ./start.sh`） |

### 命令行参数

```bash
python main.py --host 0.0.0.0 --port 9000 --data-root /data
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BFLOW_HOST` | `127.0.0.1` | 监听地址 |
| `BFLOW_PORT` | `8000` | 监听端口 |
| `BFLOW_DATA_ROOT` | 按系统惯例 | 数据根目录 |
| `BFLOW_MASTER_KEY` | 自动生成 | 密钥加密主密钥 |

---

## 本目录的定位

这是 **clean-room 重写**项目的完整实现 —— 规格文档与可运行程序都在这里：

| 内容 | 用途 |
|---|---|
| `spec/` | **规格文档** —— 实现的唯一知识来源 |
| `bflow/` | 后端（FastAPI + SQLite + 多协议适配层）|
| `static/` | 前端（原生 ES 模块，无构建步骤）|
| `tools/probe_platforms.py` | 黑盒探测工具（产出规格数据的手段）|
| `tools/selftest_protocols.py` | 多协议离线自测（假上游，零费用）|
| `tools/smoke_api.py` | API 冒烟测试（零费用）|
| `tools/selftest_api.py` | 端到端自测（含真实生图，会计费）|

> **不包含任何被参考项目的源码**。规格文档只描述**可观察行为**
> （HTTP 状态、报文原文、产物格式/尺寸），不含代码表达。

---

## 为什么是 clean-room

参考项目的 LICENSE 明确「禁止商业用途」且「二次开发必须保持开源并注明来源作者」。
要满足**可商用**这一目标，唯一合法路径是：不继承其代码，独立实现功能。

著作权保护**表达**，不保护**功能、行为、接口**。因此：

```
公开 API 文档 + 黑盒实测  →  行为规格  →  独立实现
（干净的知识来源）          （无代码表达）    （新代码）
```

本阶段（P0）就是把中间那层「行为规格」建起来。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`spec/01-platform-behavior.md`](spec/01-platform-behavior.md) | 各渠道逐项实测结果 + 错误报文样本 + 需求清单（格式 F / 尺寸 S / 错误 E / 编辑 X）|
| [`spec/02-capability-matrix.md`](spec/02-capability-matrix.md) | 渠道能力矩阵（结构化配置格式）+ 跨渠道对比 + 实现要求 |
| [`spec/03-api-contract.md`](spec/03-api-contract.md) | 对外 API 契约：响应包络、错误码表、尺寸/格式处理策略、端点清单 |
| [`spec/04-data-model.md`](spec/04-data-model.md) | 存储选型、11 张表的 DDL、迁移机制、并发与保留策略 |
| [`spec/05-ui-ia.md`](spec/05-ui-ia.md) | 信息架构、关键界面设计、状态与反馈规范、用户旅程 |

### 渠道代号约定

公开仓库不列出第三方商业渠道的名称与域名 —— 那等于替其做推广。
所有文档、代码与配置一律用**代号**：

| 代号 | 行为特征 |
|---|---|
| **CH-A** | 行为最规范，唯一完整 honor `output_format`，尺寸越界静默归一化 |
| **CH-B** | 严格模式：尺寸越界即报错，部分格式被静默丢弃 |
| **CH-C** | 输出格式与请求无对应关系，尺寸走固定值，中文报错 |
| **CH-D** | 能力随 key 分组变化 |
| **CH-E** | 上游暂时不可用 |
| **CH-F** | 新版模型无通道，仅旧版模型可用 |

真实渠道信息只存在于本地 `channels.json`（已 gitignore）。
仓库内提供脱敏模板 [`channels.example.json`](channels.example.json)，
密钥在本地 `secrets.env`（已 gitignore）。

---

## P0 结论摘要

### 渠道实测覆盖

| 渠道 | 探测深度 | 状态 |
|---|---|---|
| CH-A | 全组（8 组）| ✅ 行为最规范，作为参考基线 |
| CH-B | 全组（8 组）| ✅ 严格模式代表 |
| CH-C | formats / sizes / quality / complexity | ✅ 格式错乱案例 |
| CH-D | formats / sizes / quality | ⚠️ 分组下模型通道已下线 |
| CH-E | 模型可用性 | ❌ 上游暂时不可用 |
| CH-F | 模型可用性 | ❌ 2.6 无通道；`gpt-image-2` 可用 |

### 三条最重要的发现

1. **`output_format` 不可信。** 4 个渠道里只有 CH-A 完整 honor。
   CH-C 甚至「请求 png → 返回 webp」。
   → 调用方**必须**按魔数解析产物格式，不能信任上游声明。

2. **尺寸策略因渠道而异。** 同一模型同一尺寸：
   - CH-A：静默归一化（256×256 → 1024×1024）
   - CH-B：越界报错（256×256 → 400）
   - CH-C：固定输出（256×256 → 1254×1254）

3. **能力会随时间变化。** ch-d 的分组通道从有变无。
   → 能力表必须**数据驱动 + 可配置 + 可热更新**，不能写死在代码里。

### 像素边界（跨渠道一致，属模型层约束）

| 项 | 值 | 来源 |
|---|---|---|
| 总像素下限 | 655,360（≈816×816）| CH-B 实测 + CH-C 报错明示 |
| 总像素上限 | 8,294,400（≈2880×2880）| CH-B 实测 + CH-C 报错明示 |
| 长边上限 | 3,840 | CH-B GIF 报文明示 |

---

## 阶段进度

| 阶段 | 状态 | 产出 |
|---|---|---|
| **P0** 渠道行为规格 | ✅ 完成 | `spec/01`、`spec/02`、探测工具 |
| **P1** 规格补全 | ✅ 完成 | `spec/03`（API 契约）、`spec/04`（数据模型）、`spec/05`（UI IA）|
| **P1** edits 链路探测 | ✅ 完成 | 单参考图 / 多参考图 / 参数矩阵 / mask 行为 |
| **P2** 完整实现 | ✅ 完成 | 后端（FastAPI + SQLite）、前端（原生 ES 模块）、启动脚本、自测脚本 |
| **P3-A** 协议扩展 | ✅ 完成 | 协议注册表 + Gemini / 方舟 / APIMart / ComfyUI / RunningHub 五个适配器 |
| **P3-B** 画布增强 | ✅ 完成 | 拖线建边、节点就地生成、遮罩局部编辑 |
| **P3-C** 打包分发 | ✅ 完成 | Dockerfile / compose / 桌面端 / CI |
| **P3-D** 多协议渠道 | ✅ 完成 | 模型级协议绑定 + 上游模型拉取 + 按模型隔离能力表 |

### P3 交付内容

**P3-A 协议扩展**

| 协议 | 说明 |
|---|---|
| `openai-images` | 原有协议，行为不变 |
| `gemini` | Google Generative Language；尺寸用宽高比表达；产物在 `inlineData` |
| `ark` | 火山方舟；**提交 + 轮询**；参考图以 base64 传入 |
| `apimart` | 异步任务；参数按模型不同，通过渠道配置透传 |
| `comfyui` | 工作流协议；渠道配置里给工作流 JSON，提示词写入指定节点 |
| `runninghub` | 工作流协议；给 `workflow_id` + 提示词节点 id |

各协议的**天然能力**如实写进默认能力表（工作流类协议格式由工作流决定 →
`honors_request: false`），前端据此显示选项与提示。

**P3-B 画布增强**

- **拖线建边**：节点左右两侧端口，拖到目标节点即建边；点击连线可选中并删除
- **节点就地生成**：节点头栏 ⚡ 按钮，结果直接落进该节点
- **遮罩局部编辑**：涂绘遮罩（涂掉=要改的区域），只提交该区域

**P3-C 打包分发**

| 形式 | 入口 |
|---|---|
| 容器 | `Dockerfile` + `docker-compose.yml`（非 root、健康检查、数据卷 `/data`）|
| 桌面端 | `desktop.py`（pywebview 原生窗口）+ `packaging/Bflow.spec`（PyInstaller）|
| CI | `.github/workflows/ci.yml`（多平台自测 + 镜像构建）|

**P3-D 多协议渠道**

| 项 | 说明 |
|---|---|
| 模型级协议绑定 | `channel_model` 表；协议解析优先级：模型绑定 > 渠道默认协议 > openai-images |
| 上游模型拉取 | `GET /v1/models`（OpenAI 兼容）/ `GET /v1beta/models`（Gemini）；只列目录，不产生生成费用 |
| 协议自动建议 | 按模型名推断（`gemini-*` → gemini、`doubao-*` → ark、`flux-*` → 工作流协议），仅建议待用户确认 |
| 能力表按协议隔离 | 同渠道不同模型的能力表独立计算，跨协议不继承（否则 Gemini 模型会误显示 SVG 选项）|
| 协议变更作废旧能力 | 改了协议即重置该模型能力表 —— 旧协议下的实测结论对新协议不成立 |


### P2 交付内容

| 层 | 内容 |
|---|---|
| 后端 | FastAPI 应用、SQLite 存储层（13 表 + 迁移机制）、协议适配层（OpenAI Images + 魔数解析 + 错误归一化）、服务层（渠道/能力表/生图/资源/画布/配置）|
| 前端 | 6 个视图：工作台（快速生成）、画布（无限画布 + 节点 + 连线）、素材库、任务中心、渠道管理、设置；原生 ES 模块，无构建步骤 |
| 存储 | SQLite + 文件系统混合，产物按魔数定扩展名，密钥 AES-256-GCM 加密 |
| 自测 | `tools/selftest_api.py` —— 21 项断言，覆盖前置校验、真实生图、格式偏差、任务、资源、画布、配置 |

**落地 P0/P1 规格的关键决策**：

| 规格条款 | 实现位置 |
|---|---|
| 不降级格式 / 不篡改尺寸 / 不丢弃产物 / 不隐藏偏差 | `services/generation.py` + `protocol/openai_images.py` |
| 产物格式一律按魔数判定 | `services/protocol/imaging.py` |
| 格式/尺寸偏差返回 200 + 警告 | `api/routes.py` 的 `warnings` 字段 |
| 前置校验拦下注定失败的请求（省额度）| `services/generation.py::validate_request` |
| 错误码服务端归一化 | `services/protocol/errors.py` |

### P1 edits 链路实测结论

| 项 | 结论 |
|---|---|
| 单参考图编辑 | ✅ 200，产物 png 1024×1024 |
| 参考图数量 | 1/2/3 张均 ✅（上限待测）|
| `input_fidelity` | 2.6 渠道侧仍接受（不报错）|
| `complexity` | ✅ 支持 |
| `n=2` | ✅ 支持 |
| **`mask` 内容校验** | ❌ **上游不校验**，非法 mask 被静默忽略 → 客户端必须预校验 |

---

## 待补探测

| 项 | 说明 |
|---|---|
| 参考图角色（role）语义 | 首图待编辑 / 其余为参考，渠道是否支持 |
| edits 的 `output_format=webp` 复测 | 本次产物下载超时，未取到格式 |
| CH-D 全组 | 当前分组下模型通道不可用 |
| CH-F 的 gpt-image-2 参数矩阵 | 老模型规格不同 |
| 非 OpenAI 协议渠道 | ComfyUI / RunningHub / 即梦 / 火山 / ModelScope |

---

## 探测方法学

### 首次使用：配置渠道

```bash
cd D:/codes/Bflow

# 1) 复制模板并填入真实渠道信息（该文件不会进版本库）
cp channels.example.json channels.json
#    编辑 channels.json：填 base / key_env，id 用中性代号
#
# 2) 填密钥（同样不入版本库）
#    secrets.env 格式：CH_A_KEY=xxxxx
```

### 探测

```bash
python tools/probe_platforms.py --list                    # 列出渠道
python -u tools/probe_platforms.py --channel ch-a --groups formats,sizes
python -u tools/probe_platforms.py --channel ch-c --groups sizes \
    --only-failed probe-data/probe-20261116-182441.json --out probe-data
```

**探测组**：`formats` / `quality` / `complexity` / `sizes` / `background` /
`compression` / `n` / `unknown`

**成本**：每个 200 视为已计费，4xx/5xx 不计费。本轮约 60 次成功、15 次失败。

**注意**：探测产物落在 `probe-data/`，该目录已 gitignore —— 里面的原始日志
含渠道标识与报错原文，不适合公开。整理后的结论请写进 `spec/`。

**成本**：每个 200 视为已计费，4xx/5xx 不计费。本轮约 60 次成功、15 次失败。
