# 数据资产统计 · 甲方算子说明书

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 数据资产统计 |
| 算子编号 | 10（`AVP_ASSET_DATA_010`） |
| 英文标识 | `avp_asset_statistics` |
| 所属阶段 | S4 数据运营（阶段末算子） |
| 标签 | 数据资产 |
| 版本 | v1.0.0 |
| 交付形态 | 单算子目录 + `run(ctx)` 入口，由 `inference.py` 统一挂载 |
| 输入 | 素材库登记表 JSON / 入库记录 JSONL / 006 单版本产物 |
| 输出 | `010_asset.json`（资产统计快照） |
| 依赖 | Python 3.9+ 标准库 + 包内 `common`，无第三方模型、无网络 |

## 二、功能定义

**解决的问题**：为「数据资产」页面提供一份口径固定、可复算的资产统计：总量、数据集数、近 N 天新增、按数据集列表、逐日趋势。

**输入来源**

1. 素材库登记表（`datasets[]` + `records[]`），或
2. 001/002 语料 JSONL（按行聚合），或
3. 006 单版本产物（一个版本视为一个数据集）。

**触发时机**：进入数据运营阶段后调用一次；页面切换 7/30/90 天窗口或搜索关键词时带 `params` 重跑。

**不负责**

- 不做样本质量、异常、回流处理（属 005/007/009）；
- 不做权限、分页、导出、图表渲染（前端职责）；
- 不修改输入文件，不写数据库。

**业务口径**

1. 资产总量 = 各数据集样本数之和，**不去重**；
2. 近 N 天新增按入库时间 `created_at` 聚合，**无数据日补 0**；
3. N ∈ {7, 30, 90}，默认 7；
4. 数据集列表为存量，不随窗口变化；关键词过滤时列表与指标卡同口径。

## 三、输出内容描述

输出文件：`010_asset.json`（`ctx.out("010_asset.json")`，与 `map.json` 的 `outputs.asset` 一致）。

| 字段 | 类型 | 含义 |
|---|---|---|
| `total_samples` | int | 资产总量（Σ 数据集样本数，不去重） |
| `dataset_count` | int | 数据集个数 |
| `new_in_range` | int | 近 N 天新增样本 |
| `time_range_days` | int | 生效的窗口天数 |
| `range.start/end` | string | 窗口首尾日期（含今天） |
| `per_dataset[]` | array | 数据集统计列表（存量）：`dataset_id/name/source/sample_count/new_in_range/updated_at` |
| `trend[]` | array | 连续 N 天趋势：`date/new_samples/new_datasets` |
| `filter.keyword` | string\|null | 生效的关键词 |
| `sources[]` | array | 实际读取的输入文件与条数（溯源） |
| `caliber` | object | 口径说明快照 |
| `warnings[]` | array | 数据质量告警（不阻断） |
| `generated_at` | string | 生成时间 |

返回体 `result` 字段为同一统计的摘要（`total_samples/dataset_count/new_in_range/time_range_days/range/trend_days/keyword/conclusion`），
`statistics` 额外给出记录数、无时间记录数、无记录数据集数、趋势补 0 天数等自检信息。

**空值口径**：没有数据时对应计数为 0、列表为空数组；时间字段无法解析时输出 `null` 并告警，不做猜测填充。

## 四、技术方案

本算子的技术难点不在模型，而在「**口径正确性**」与「**输入异构性**」：

1. **面向业务口径的聚合模型**
   以数据集为最小统计单元，`total_samples` 由各数据集样本数求和，天然满足「同一音频属两个数据集算两次」。
   若按 `uttid` 去重（常见默认做法），会把跨数据集复用的音频误减，导致资产总量偏低——这是本项目明确禁止的口径错误。

2. **异构输入适配层（Schema Adapter）**
   字段别名、列表键、时间键全部配置化（`configs/config.yaml → registry.*`），
   按「登记表 → 记录数组 → 006 版本产物」的顺序探测结构，找到即用；
   每个被读取的文件记入 `sources[]`，统计结果可逐项回溯到输入文件。

3. **连续日历网格 + 本地时区归一**
   先把窗口展开为 N 个连续日期，再按本地日期落桶补 0；时间解析兼容 ISO 偏移、`Z`、无时区字符串，
   并在无 tzdata 的 Windows 上用固定偏移兜底，保证同一份输入在不同环境结果一致。

4. **不静默降级**
   记录时间缺失/非法时，仍给出总量与列表，但把状态置 `partial` 并在 `warnings` 写明条数；
   一个数据集都识别不到时直接报 `invalid_params:`，避免「全 0 报表」被误读为真实资产为零。

5. **确定性**
   纯函数 `aggregate()` 不读时钟（`today` 作为入参），排序规则固定；
   同一输入（含生成日期）两次运行产物逐字节一致，便于审计与回归。

**相较「简单阈值/去重脚本」的优势**

- 口径与业务定义一一对应，不因工程直觉引入去重错误；
- 输入结构变化只改配置，不改代码，接入成本低；
- 异常数据显式暴露（`partial` + `warnings`），报表可信；
- 全流程无随机、无网络、无外部服务，结果可复现。

## 五、技术实现

| 文件 | 职责 |
|---|---|
| `processor.py` | 参数校验、输入加载与归一、聚合、产物写出、日志 |
| `configs/config.yaml` | 时间窗口、时区、字段别名、输出格式等全部可调项 |
| `dataset/DATA_SCHEMA.md` | 输入输出字段契约与空值口径 |
| `dataset/sample/` | 小样本（登记表 JSON + 记录 JSONL） |
| `tests/` | pytest：正常/边界/异常/确定性/接口五类 |
| `output/model/` | `config.json`（生效配置快照）+ `metrics.json`（真实指标） |

**处理流程**

```
校验 params(time_range/keyword/registry_path)
  → 定位输入（registry_path 优先，否则 inputFile 文件/目录）
  → 逐文件解析并归一（datasets[] / records[]）
  → 合并同名数据集、按数据集聚合样本数
  → 过滤关键词（数据集名称）
  → 计算窗口内新增 & 连续逐日趋势
  → 计算 updated_at / 新增数据集代理
  → 写 010_asset.json + 日志
```

**关键实现点**

- `_load_sources()`：输入优先级与目录扫描；`_parse_json_payload()`：三种 JSON 结构探测；
- `_norm_dataset_entry()` / `_norm_records()`：字段别名解析与类型归一；
- `aggregate(datasets, records, time_range, keyword, today, cfg)`：纯函数，可单测；
- `_tz()/_parse_dt()`：时区与时间解析兜底；`_today()`：唯一时钟入口；
- 日志统一走 `ctx.log`（路由/读取/处理/输出/告警/partial/完成），不 `print`；
- 业务失败抛 `common.schemas.OpError`（`missing_file:` / `invalid_params:` / `invalid_jsonl:`），由服务层统一收口。

## 六、资源要求

| 项 | 要求 / 实测 |
|---|---|
| CPU | 1 核即可；纯 Python 聚合，无并行 |
| 内存 | 进程 RSS 34.4 MB（含依赖导入） |
| 磁盘 | 输出单个 JSON，通常 < 100 KB |
| 网络 | 不需要 |
| 依赖 | 无额外第三方库（`common` 为包内模块） |

## 七、性能指标

| 指标 | 结果 | 口径 |
|---|---|---|
| 单次 `run()` 延迟 | **14.05 ms**（min 12.70 / max 16.28） | 2 数据集 / 7 记录，预热 1 次后 5 次均值 |
| 总量/数据集数/新增/趋势真值一致率 | **31/31 = 100%** | 6 组构造真值，手工计算真值 |
| 其中：总量一致率 | 6/6 | 含跨数据集重复 `uttid` 场景 |
| 其中：新增一致率 | 6/6 | 7/30/90 天窗口与缺时间场景 |
| 其中：趋势一致率 | 13/13 | 逐日值/新增数据集/连续天数/补 0 天数/末日值 |
| 确定性 | 同输入两次产物一致 | 固定 `today` 后逐字节比较 |

复现命令与完整输出见 `DEVELOPMENT_STATUS.md`。

## 八、输入输出示例

**输入**：`dataset/sample/asset_registry_sample.json`（2 个数据集、7 条记录；`BAC009S0001` 同时属于两个数据集）
**参数**：`{"time_range": 7}`（生成日 2026-11-02）

**输出摘要**

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "status": "success",
    "result": {
      "total_samples": 7,
      "dataset_count": 2,
      "new_in_range": 5,
      "time_range_days": 7,
      "range": {"start": "2026-10-27", "end": "2026-11-02"},
      "trend_days": 7,
      "conclusion": "success"
    },
    "outputs": {"asset": "/data/cli_smoke_010/tmp/010/010_asset.json"}
  }
}
```

**产物节选**

```json
{
  "total_samples": 7,
  "dataset_count": 2,
  "new_in_range": 5,
  "per_dataset": [
    {"dataset_id": "DS-20261018-002", "name": "AISHELL-3公开数据集", "sample_count": 4, "new_in_range": 2},
    {"dataset_id": "DS-20261020-001", "name": "客服对话语音原始集-V2", "sample_count": 3, "new_in_range": 3}
  ],
  "trend": [
    {"date": "2026-10-27", "new_samples": 1, "new_datasets": 0},
    {"date": "2026-10-28", "new_samples": 2, "new_datasets": 0},
    {"date": "2026-10-29", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-10-30", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-10-31", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-11-01", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-11-02", "new_samples": 2, "new_datasets": 0}
  ]
}
```

**失败示例**

```json
{"code": 1, "msg": "invalid_params: time_range 取值 14 非法，允许：7 / 30 / 90"}
```

**降级示例**：记录缺入库时间时 `status=partial`，`new_in_range` 只统计有效时间记录，`warnings` 写明「N 条记录缺少可解析的入库时间」。
