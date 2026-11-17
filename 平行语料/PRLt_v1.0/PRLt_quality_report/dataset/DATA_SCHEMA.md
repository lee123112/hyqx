# 算子19 数据契约（dataset/DATA_SCHEMA.md）

对应 `configs/config.yaml` 与 `processor.py` 的实际实现，文件编号 19，不含旧版编号与旧版四接口。

## 0. 样例文件

| 文件 | 场景 | 期望结果 |
| --- | --- | --- |
| `dataset/sample/demo.jsonl` | 达标：转化率 172/200=0.86，留存率 172/182=0.945055 | `status=success`、`passed=true` |
| `dataset/sample/quality_input.jsonl` | 不达标 + 多语言对分组：转化率 180/300=0.60，留存率 140/160=0.875 | `status=partial`、`passed=false` |

## 1. 输入：主输入 JSONL（`inputFile`）

逐行一条 JSON 对象，UTF-8，流式读取。按 `record_type` 优先、内容推断兜底分类。

### 1.1 `stage_statistics` —— 阶段统计（可按阶段多行累加）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `record_type` | string | 否（建议） | 固定 `stage_statistics` |
| `stage` | string | **是** | 必须属于 `stage_order`，否则 `unknown_stage:<stage>` |
| `input_units` | int ≥ 0 | **是** | 进入该阶段的业务单元数 |
| `output_units` | int ≥ 0 | **是** | 该阶段输出业务单元数 |
| `filtered_units` | int ≥ 0 | **是** | 被规则过滤数 |
| `failed_units` | int ≥ 0 | **是** | 处理失败数 |
| `duplicate_units` | int ≥ 0 | **是** | 去重删除数 |
| `low_confidence_units` | int ≥ 0 | **是** | 低置信数 |
| `manual_rejected_units` | int ≥ 0 | **是** | 人工驳回数 |
| `operator` | string | 否 | 产出该统计的算子名，写入 `stage_statistics.<stage>.operators` |
| `task_id` / `pipeline_type` / `pipeline_version` / `unit_type` | string | 否 | 口径字段，参与一致性校验 |
| `lineage_ref` / `lineage_refs` | string / string[] | 否 | 原血缘引用；无血缘改造后不再读取，报告不再包含 `lineage_refs` |

计数必须为**整数**（bool 不算），负数按 `negative_value:<field>` 拒绝，小数按 `invalid_type:<field>` 拒绝。
**禁止用文件大小替代业务单元计数。**

### 1.2 `delivery_summary` —— 交付汇总（算子18）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `record_type` | string | 否（建议） | 固定 `delivery_summary` |
| `delivery_records` | int ≥ 0 | **是**（或 `record_count`） | 交付记录数 = 转化率分子 |
| `grade_counts` | object<string,int> | 否 | 最终治理前等级分布；S/A 之和 = 留存率分母 |
| `delivery_grade_counts` | object<string,int> | 否 | 实际交付等级分布；S/A 之和 = 留存率分子 |
| `language_pair_counts` | object<string,int> | 否 | 语言对分组计数 |
| `source_type_counts` | object<string,int> | 否 | 来源分组计数 |
| `domain_counts` | object<string,int> | 否 | 行业分组计数 |
| `task_id_counts` | object<string,int> | 否 | 任务分组计数 |
| `language_pair` / `task_id` / `pipeline_version` / `unit_type` | string | 否 | 口径字段 |

### 1.3 `dataset_record` —— 交付明细（可选，用于分组与交叉校验）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `record_type` | string | 否 | 固定 `dataset_record`（含 `source`+`target` 时自动识别） |
| `source` / `target` | string | 否（识别用） | 双语句对内容（本算子不读取其语义） |
| `quality_grade` | string | 否 | 等级，用于 `group_statistics[*].high_quality_units` |
| `language_pair` / `source_type` / `domain` / `task_id` | string | 否 | 分组维度；缺失记 `unknown` |

出现交付明细记录时：交付计数以明细条数为准，并与交付汇总 `delivery_records` 交叉校验，
不一致即 `delivery_count_mismatch`（failed）。

## 2. 侧输入（可选）

| 配置项 | 默认文件名 | 结构 |
| --- | --- | --- |
| `stage_statistics_file` | `stage_statistics.json` | 数组，或 `{"records":[...]}` / `{"stage_statistics":[...]}` / `{"stages":[...]}` |
| `manifest_file` | `18_manifest.json` | 对象：`record_count`、`statistics.grade_counts`、`statistics.delivery_grade_counts`、`source_types`、`language_pair`、`unit_type`、`pipeline_version` |
| `upstream_lineage_file` | ~~`lineage.json`~~ | 已停用（2026-10-19 无血缘改造）：不再读取血缘文件，`use_lineage_statistics` 同步停用 |

查找顺序：`<输入文件同目录>` → `<任务目录 OUTPUT_DIR/<uniqueId>>` → `<算子目录>`。

## 3. 输出

写入 `OUTPUT_DIR/<uniqueId>/`，全部 UTF-8、原子替换：

| 文件 | 结构 |
| --- | --- |
| `19_quality_report.json` | 对象，字段见下表 |
| `19_quality_report.jsonl` | 每行 `{"dimension": "<task_id\|language_pair\|source_type\|domain>", "key": "...", "units": int, "high_quality_units": int\|null}` |
| `19_rejects.jsonl` | 每行 `{"line": int, "record_type": "...", "stage": string\|null, "reasons": [..], "record": object\|null, "missing_fields"/"invalid_fields"/"source": 可选}`；指标级异常 `record_type="metrics"`、`line=0` |
| `artifacts/stage_19_<uniqueId>.zip` | 阶段产物（质量报告、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json` |
| `run.log` | 逐行 `[时间] [动作] 消息` |

### `19_quality_report.json` 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `report_version` | string | 报告版本 |
| `operator` / `operator_version` | string | `data_quality_report` / `v1.0.0` |
| `uniqueId` | string | 任务id_文件id |
| `task_id` | string | 记录中的 task_id，缺失时按 uniqueId 前缀推导 |
| `task_id_source` | string | `records` / `unique_id_prefix` / `unique_id` |
| `pipeline_type` / `pipeline_version` / `unit_type` | string | 出现次数最多者，缺失取配置默认 |
| `input_unit_count` | int\|null | 产线首个阶段有效输入 |
| `low_quality_input_count` | int\|null | 转化率分母 |
| `converted_ai_ready_count` | int\|null | 转化率分子 |
| `low_quality_conversion_rate` | float\|null | 保留 6 位小数 |
| `pre_final_high_quality_count` | int\|null | 留存率分母 |
| `retained_high_quality_count` | int\|null | 留存率分子 |
| `high_quality_retention_rate` | float\|null | 保留 6 位小数 |
| `thresholds` | object | 固定两项：`low_quality_conversion_rate`=0.85、`high_quality_retention_rate`=0.90 |
| `passed` | bool | 两项指标同时达标且无 blocking 原因 |
| `status` | string | `success` / `partial` / `failed` |
| `statistics_scope` | object | 分母/分子的取值来源、时点、统计单位、配置版本、等级口径、维度列表、单位别名 |
| `stage_statistics` | object | 每阶段 7 个计数 + `records` + `operators` |
| `stage_totals` | object | 各计数跨阶段累加 + `stages` |
| `group_statistics` | object | 维度 → 键 → `{units, high_quality_units}` |
| `group_totals` | object | 维度 → 合计，需等于 `converted_ai_ready_count` |
| `generated_at` | string | ISO8601（+08:00） |
| `failure_reasons` | string[] | 稳定原因码，见 §4 |
| `warnings` | string[] | 非阻断提示码 |
| `statistics` | object | 本次运行真实计数（输入/阶段/明细/汇总/拒绝条数、`reason:*`、`warning:*` 等） |
| `config` | object | 配置快照 |
| `encoding` | string\|null | 实际解码编码 |

## 4. 原因码

| 原因码 | 含义 | 是否阻断 |
| --- | --- | --- |
| `invalid_json` / `invalid_record` / `unknown_record_type` | 损坏行 / 非对象 / 未知记录类型 | 否（进异常清单，转 `partial`） |
| `missing_field:<field>` | 关键字段缺失 | 由 `missing_field_policy` 决定（默认 `failed`） |
| `invalid_type:<field>` / `negative_value:<field>` | 计数类型/取值非法 | 是 |
| `unknown_stage:<stage>` | 阶段名不在 `stage_order` | 否（进异常清单） |
| `stage_balance_mismatch:<stage>` | 阶段守恒校验失败（默认关闭） | 是 |
| `unit_type_inconsistent` | 多种统计单位并存 | 仅 `allow_mixed_unit_types=false` 时阻断；默认 true 下降级为 warning `unit_type_mixed:<units>`，报告按阶段各自 `unit_type` 呈现（一期链路 09→10→11 切换单元是设计行为） |
| `pipeline_version_inconsistent` / `pipeline_type_inconsistent` | 口径版本/类型不一致 | 是 |
| `delivery_statistics_conflict:<field>` | 多个来源交付计数冲突 | 是 |
| `delivery_count_mismatch:summary=..,dataset=..` | 交付汇总与交付明细不一致 | 是 |
| `group_total_mismatch:<dim>:groups=..,total=..` | 分组合计与总计不一致 | 是 |
| `lineage_missing`（已停用） | 原「未取得任何血缘引用」校验；无血缘改造后不再触发 | 否 |
| `lineage_broken:<stage>` | 阶段链连续性缺环（业务口径，与血缘文件无关，保留） | 是 |
| `lineage_unavailable`（已停用） | 原「未取得血缘引用」降级原因；无血缘改造后不再触发 | 否 |
| `missing_denominator_source:low_quality_input_count` | 首阶段统计缺失 | 是 |
| `missing_converted_count` / `missing_high_quality_candidate_count` / `missing_retained_high_quality_count` | 分子/分母来源缺失 | 是 |
| `denominator_zero:low_quality_conversion_rate` / `denominator_zero:high_quality_retention_rate` | 分母为零 | 是 |
| `low_quality_conversion_below_threshold` / `high_quality_retention_below_threshold` | 指标不达标 | 否（转 `partial`，`passed=false`） |
| `empty_input` / `encoding_error:<T>` / `read_error:<T>` / `config_error:<T>` | 空输入、编码/读取/配置错误 | 是 |

## 5. 任务隔离与路径安全

- 产物只写 `OUTPUT_DIR/<uniqueId>/`；`uniqueId` 为空、含 `/\:*?"<>|`、为 `.`/`..` 或解析后越出任务目录一律拒绝。
- `inputFile` 仅允许解析到 `OUTPUT_DIR/<uniqueId>/` 内，绝对路径与目录穿越拒绝。
- 输入路径与输出文件路径重合时拒绝执行，避免截断源文件。
- 不同 `uniqueId` 互不读写对方目录。

## 6. 阶段产物

【无血缘改造 2026-10-19】原 `lineage.json`（含 operator / operator_version / uniqueId /
inputFile / outputFile / status / startedAt / finishedAt / config / statistics）已停用，
任务目录不再产出任何血缘文件。阶段内中间路径通过 `/predict` 返回的 `data.outputFile` 传递
（指向 `19_quality_report.json`），阶段末由接口以内容形式返回；阶段结束前把质量报告、
异常清单与 `run.log` 打包为 `artifacts/stage_19_<uniqueId>.zip`（分组明细不进入 zip），
响应返回 `stageZip` 与 `storageUri`。
