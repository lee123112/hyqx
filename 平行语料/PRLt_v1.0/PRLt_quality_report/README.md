# 19_数据质量报告（data_quality_report）

平行语料产线「数据集交付」阶段的数据质量报告算子。汇总各阶段统计事实，按固定公式计算
**低质量数据转化率**与**高质量语料留存率**，给出 `passed` 判定，并产出可追溯的质量报告、
分组明细与异常清单。

- 编号：19
- operator id：`data_quality_report`
- 端口：8019
- 阶段：数据集交付（承接 18，可与 20 串接）
- 类型：**规则型汇总算子，无模型、无模型服务依赖**

## 1. 功能边界

做：
- 汇总阶段统计（输入/输出/过滤/失败/重复/低置信/人工驳回 业务单元数）；
- 计算两项交付指标并与阈值比较，输出 `passed` 与失败原因；
- 按任务、语言对、来源、行业分组统计并给出总计，校验分组总计与总计一致；
- 校验统计口径一致性（统计单位、pipeline_version、pipeline_type、阶段链连续性、交付计数交叉校验）；
- 产出 `19_quality_report.json`、`19_quality_report.jsonl`（分组明细）、`19_rejects.jsonl`（异常/拒绝清单）、
  `artifacts/stage_19_<uniqueId>.zip`、`run.log`（**2026-10-19 起不再输出 `lineage.json`**）。

不做：
- 不重复执行上游清洗/对齐/质检，不重新计算任何质量分或置信度；
- 不使用任何模型，不做语义判定，不改写语料内容；
- 不使用文件大小、字节数替代业务单元计数；
- 不实现 `/downloadFile`（正式接口只有三个）。

## 2. 指标口径

| 指标 | 公式 | 阈值 |
| --- | --- | --- |
| 低质量数据转化率 | `converted_ai_ready_count / low_quality_input_count` | ≥ 0.85 |
| 高质量语料留存率 | `retained_high_quality_count / pre_final_high_quality_count` | ≥ 0.90 |

分母/分子定义（同时写入报告 `statistics_scope`，含来源字段与时点）：

| 字段 | 含义 | 取值来源（按优先级） |
| --- | --- | --- |
| `low_quality_input_count` | 进入产线的有效低质量单元总数 = 产线首个阶段的有效输入 | `stage_statistics:<low_quality_input_stage>.input_units` |
| `converted_ai_ready_count` | 最终 AI-Ready 合格单元数 | 交付汇总 `delivery_records` → 交付明细记录条数 |
| `pre_final_high_quality_count` | 最终治理前已判为高质量候选数 | 显式 `pre_final_high_quality_count` → 交付汇总 `grade_counts` 中 `high_quality_grades`（默认 S/A）之和 → 交付明细 `quality_grade` 统计 |
| `retained_high_quality_count` | 算子18 实际交付的高质量数 | 显式 `retained_high_quality_count` → `delivery_grade_counts` 中 S/A 之和 → 交付明细 S/A 条数 |

`passed = true` 需同时满足：两项指标均已算出且均达阈值，且无 blocking 失败原因（无血缘不可用项）。

## 3. 输入

### 3.1 主输入（`inputFile`，必传）

JSONL（或 `.json` 数组/对象）逐行流式读取，每行是一条「统计事实」记录，按内容自动分类：

| `record_type` | 说明 | 必填字段 |
| --- | --- | --- |
| `stage_statistics` | 单个阶段（可多行累加）的业务单元计数 | `stage` + 7 个计数：`input_units`/`output_units`/`filtered_units`/`failed_units`/`duplicate_units`/`low_confidence_units`/`manual_rejected_units` |
| `delivery_summary` | 算子18 交付汇总（可一行） | `delivery_records`（或 `record_count`）；可选 `grade_counts`、`delivery_grade_counts`、`language_pair_counts`、`source_type_counts`、`domain_counts`、`task_id_counts` |
| `dataset_record` | 交付明细记录（可多行） | 建议含 `source`/`target`；用于分组与交叉校验，`quality_grade`、`language_pair`、`source_type`、`domain`、`task_id` 可选 |

公共可选字段：`task_id`、`pipeline_type`、`pipeline_version`、`unit_type`（`lineage_ref`/`lineage_refs` 已不再使用，见 §4）。

无 `record_type` 时按内容推断：含任一计数或 `stage` → 阶段统计；含 `source`+`target` → 交付明细；
含 `delivery_records`/`record_count` → 交付汇总；否则按 `unknown_record_type` 进异常清单。

### 3.2 侧输入（可选，工程侧提供）

按 `<输入文件同目录>` → `<任务目录 OUTPUT_DIR/uniqueId>` → `<算子目录>` 顺序查找，命中即读取：

| 配置项 | 默认文件名 | 用途 |
| --- | --- | --- |
| `stage_statistics_file` | `stage_statistics.json` | 阶段统计数组/对象，与主输入合并累加 |
| `manifest_file` | `18_manifest.json` | 取 `record_count`、`statistics.grade_counts`、`statistics.delivery_grade_counts` 等 |

> 【无血缘改造 2026-10-19】原 `upstream_lineage_file`（`lineage.json`）侧输入及其 `use_lineage_statistics`
> 配置已停用：本算子不再读取任何血缘文件，交付计数只来自主输入记录与 `18_manifest.json`。

## 4. 输出

写入 `OUTPUT_DIR/<uniqueId>/`：

| 文件 | 内容 |
| --- | --- |
| `19_quality_report.json` | 质量报告（UTF-8，原子替换写入） |
| `19_quality_report.jsonl` | 分组明细，每行 `{dimension,key,units,high_quality_units}` |
| `19_rejects.jsonl` | 异常/拒绝清单：损坏 JSON、未知类型、缺字段、分母为零、阶段链断裂、口径不一致等 |
| `artifacts/stage_19_<uniqueId>.zip` | 阶段产物：质量报告 + 异常清单 + `run.log`（不再含血缘成员） |
| `run.log` | 逐行日志（时间 + 动作 + 真实数量） |

报告关键字段：`report_version`、`operator`、`operator_version`、`uniqueId`、`task_id`、`task_id_source`、
`pipeline_type`、`pipeline_version`、`unit_type`、`input_unit_count`、`low_quality_input_count`、
`converted_ai_ready_count`、`low_quality_conversion_rate`、`pre_final_high_quality_count`、
`retained_high_quality_count`、`high_quality_retention_rate`、`thresholds`、`passed`、`status`、
`statistics_scope`、`stage_statistics`、`stage_totals`、`group_statistics`、`group_totals`、
`generated_at`、`failure_reasons`、`warnings`、`statistics`、`config`、`encoding`
（**无 `lineage_refs` / `lineage_ref_count`**）。

### 输出示例（节选，`dataset/sample/demo.jsonl` 的真实运行结果）

```json
{
  "task_id": "taskA",
  "unit_type": "sentence_pair",
  "low_quality_input_count": 200,
  "converted_ai_ready_count": 172,
  "low_quality_conversion_rate": 0.86,
  "pre_final_high_quality_count": 182,
  "retained_high_quality_count": 172,
  "high_quality_retention_rate": 0.945055,
  "thresholds": {"low_quality_conversion_rate": 0.85, "high_quality_retention_rate": 0.9},
  "passed": true,
  "status": "success",
  "stage_totals": {"input_units": 774, "output_units": 756, "filtered_units": 11,
                   "failed_units": 4, "duplicate_units": 2, "low_confidence_units": 1,
                   "manual_rejected_units": 0, "stages": 4},
  "group_totals": {"task_id": 172, "language_pair": 172, "source_type": 172, "domain": 172}
}
```

> 【无血缘改造 2026-10-19】原 `lineage.json` 与报告中的 `lineage_refs` / `statistics.lineage_ref_count`
> 已停用：任务目录不再产出任何 lineage 文件，报告不再引用血缘字段，原实现保留在代码注释中便于回溯。

## 5. 状态机与降级行为

| status | 触发条件 |
| --- | --- |
| `success` | `passed=true`，无拒绝记录 |
| `partial` | 指标不达标（`passed=false`），或存在被拒/异常记录；报告仍正常生成 |
| `failed` | 空输入、编码/配置/读取错误、关键字段缺失（`missing_field_policy=failed`）、分母为零、统计单位或口径不一致、分组总计不一致、交付计数冲突、阶段链断裂 |

**降级说明**：本节点为规则型、无模型、无模型服务调用，因此不存在模型降级。
【无血缘改造 2026-10-19】已移除 `degraded`（血缘来源不可用）状态：不再读取血缘，
也不存在 `lineage_unavailable`/`lineage_missing` 失败原因；`lineage_broken:<阶段>` 仅是
**阶段链连续性**校验的业务原因码（与血缘文件无关），保留不变。绝不伪造分数或补零。

`failed` 时 `outputFile` 为 `null`（不向下游传递内容），但报告文件仍落盘供审计；
`failed` 分支不生成阶段产物 zip。

## 6. 接口

正式接口仅三个，外层统一 `{"code":0|1,"msg":"...","data":{...}}`：

```
POST /uploadFile?uniqueId=
POST /predict
POST /removeFile?uniqueId=
```

`/predict` 请求体：
`{"uniqueId": "...", "operatorId": "19", "isLastOperator": true|false, "inputFile": "...", "config": {...}, "params": {...}}`

- `operatorId` 可选；传入时必须等于 `"19"`，否则返回 `code=1`。
- `params`/`config` 均为可选对象：先合并 `config`，再用 `params` 覆盖（`params` 优先），
  合并结果作为配置覆盖项；非法键由 `OperatorConfig(extra="forbid")` 拒绝（`code=1`）。
- `inputFile` 必传（阶段首算子由工程侧省略时，服务会退回取任务目录 `input/` 下最新文件）；
  只能解析到任务目录 `OUTPUT_DIR/<uniqueId>/` 内，**绝对路径与目录穿越一律拒绝**。
- `isLastOperator=false`：`data = {outputFile, rejectFile, stageZip, storageUri}`，其中
  `outputFile` 是当前中间输出的临时路径。
- `isLastOperator=true`：同一四字段结构，但 `data.outputFile` 是
  `19_quality_report.json` 的 UTF-8 文本内容（**不是路径**）；`rejectFile`/`stageZip` 为路径，
  `storageUri` 为对象存储地址（设置 `OBJECT_STORE_DIR` 时为 `file://`，否则为
  `local-mirror://<uniqueId>/<zip>`）。
- 【无血缘改造 2026-10-19】`data` 不再包含 `lineageFile`；不存在 `/downloadFile`。

### 启动

```bash
pip install -r requirements.txt
export OUTPUT_DIR=./output            # Windows: $env:OUTPUT_DIR='./output'
python -m uvicorn inference:app --host 0.0.0.0 --port 8019
# 或 python inference.py
```

### 调用示例

```bash
curl -X POST "http://127.0.0.1:8019/uploadFile?uniqueId=taskA_file1" -F "file=@dataset/sample/demo.jsonl"
curl -X POST http://127.0.0.1:8019/predict -H "Content-Type: application/json" \
     -d '{"uniqueId":"taskA_file1","isLastOperator":true,"inputFile":"input/demo.jsonl"}'
curl -X POST "http://127.0.0.1:8019/removeFile?uniqueId=taskA_file1"
```

### CLI

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

实测输出：

```
status=success passed=True
statistics={... 'low_quality_conversion_rate': 0.86, 'high_quality_retention_rate': 0.945055, ...}
stageZip=output_smoke\smoke_cli\artifacts\stage_19_smoke_cli.zip storageUri=local-mirror://smoke_cli/stage_19_smoke_cli.zip
```

## 7. 配置

见 `configs/config.yaml`，全部键在 `utils/schemas.py::OperatorConfig` 中声明且 `extra="forbid"`
（未知键或非法枚举即报配置错误）。`/predict` 的 `config` 字段可覆盖任意键；
【无血缘改造 2026-10-19】`params` 覆盖 `config`（`params` 优先）。

常用项：`low_quality_conversion_threshold`、`high_quality_retention_threshold`、`high_quality_grades`、
`stage_order`、`low_quality_input_stage`、`missing_field_policy`、`stage_balance_check`、
`below_threshold_status`、`group_dimensions`、`stage_statistics_file`、`manifest_file`。
原 `require_lineage`、`upstream_lineage_file`、`use_lineage_statistics` 为血缘专用配置，已停用并注释。

### 7.1 规范参数映射

算子规范（`param_` 中文参数）到 `configs/config.yaml` 键的映射：

| 规范参数 | 本算子配置键 | 说明 |
| --- | --- | --- |
| `param_统计维度`（规范 4.18） | `group_dimensions` | 取值为 `task_id`/`language_pair`/`source_type`/`domain` 列表；/predict 可经 `params` 传入 |

## 8. 资源需求

纯 IO/CPU 汇总，无 GPU、无模型。内存与输入规模无关（流式逐行读取、逐行写出）；
磁盘占用≈报告 + 分组明细 + 异常清单。6.8 万行统计输入为秒级。

## 9. 已知限制与未确认口径

1. `input_unit_count` 与 `low_quality_input_count` 在同一时点（产线首个阶段输入），
   报告保留两个字段以对齐设计文档要求；两者数值相同。
2. `missing_field_policy` 的 `partial`/`skip` 差异仅体现在是否记入 `warnings`，
   均只跳过该条记录、不补零。
3. `stage_balance_check`（输入 == 输出+过滤+失败+重复+低置信+人工驳回）默认关闭：
   真实数据各类别可能存在包含关系，口径未与甲方确认。
4. 交付汇总未提供各分组计数、且无交付明细记录时，该维度只记录
   `group_dimension_unavailable:<dim>` 告警，不阻断。
5. `below_threshold_status` 默认 `partial`（报告已生成、仅指标不达标）。
   若甲方要求指标不达标即整节点失败，改为 `failed` 即可。
6. 报告未被审计签名；如需防篡改由工程侧对 `19_quality_report.json` 做哈希登记。
7. `unit_type` 采用别名归一（`sentence` → `sentence_pair`），用于兼容上游 18 的取值，
   避免把同一粒度误判为统计单位不一致。
8. 【无血缘改造 2026-10-19】不再读取/生成/返回血缘文件；报告不含 `lineage_refs`，
   `statistics` 不含 `lineage_ref_count`，任务目录不产出 `lineage.json`；阶段产物 zip 不含
   血缘成员。`lineage_broken:<阶段>` 仅表示阶段链连续性缺口，属业务口径原因码，保留。
9. 规范的「缺少人工 Gold 标注 → 仅出受限报告、不出正式报告」门禁一期未实现：当前对任何输入
   均产出报告并标注 `evaluation_status`/`metric_status` 口径；如甲方要求该门禁，可作为
   配置项追加。

## 10. 真实测试结果

见 `DEVELOPMENT_STATUS.md`（含 `pytest`、CLI/API smoke、`map.json`、`compileall` 的真实命令与输出）。
无血缘改造后实测：`D:\python\python.exe -m pytest tests -q` → **40 passed in 4.15s**；
CLI `processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke`
→ `status=success passed=True`，`stageZip=output_smoke\smoke_cli\artifacts\stage_19_smoke_cli.zip`，
zip 成员为 `19_quality_report.json`/`19_rejects.jsonl`/`run.log`，任务目录无 lineage 文件；
`python -m json.tool map.json` 通过；`python -m compileall -q .` 退出码 0。
Docker 镜像**未构建**（本机未执行 `docker build`）。
