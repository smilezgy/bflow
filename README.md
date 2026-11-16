# Bflow

> 面向 AI 多模态创作的工作台 —— **clean-room 重写版**。
>
> 目标：独立品牌、可商用、不受上游更新干扰。
>
> 状态：**P0 规格阶段** —— 尚未包含可运行的应用程序。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 本目录的定位

这是 **P0 阶段**的工作区，当前只包含两样东西：

| 内容 | 用途 |
|---|---|
| `spec/` | **规格文档** —— clean-room 重写的唯一知识来源 |
| `tools/probe_platforms.py` | 黑盒探测工具（产出规格数据的手段）|
| `probe-data/` | 探测原始数据（JSON），规格文档的事实底稿 |

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
| [`spec/01-platform-behavior.md`](spec/01-platform-behavior.md) | 各渠道逐项实测结果 + 错误报文样本 + 需求清单 |
| [`spec/02-capability-matrix.md`](spec/02-capability-matrix.md) | 渠道能力矩阵（结构化配置格式）+ 跨渠道对比 + 实现要求 |

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

## 下一阶段（P1）待办

| 项 | 说明 |
|---|---|
| 探测补全 | edits 链路（multipart）、参考图数量/角色、非 OpenAI 协议渠道（ComfyUI / RunningHub / 即梦 / 火山 / ModelScope）|
| 规格补全 | `spec/03-api-contract.md`（对外 API 契约）、`spec/04-data-model.md`（存储结构）、`spec/05-ui-ia.md`（交互与信息架构）|
| 仓库初始化 | git init、`LICENSE`（自有许可）、README、CI |

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
