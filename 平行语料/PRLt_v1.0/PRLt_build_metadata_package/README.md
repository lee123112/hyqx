# 算子 18：元数据构建与数据集打包

平行语料 demo 流水线「数据集交付」阶段的规则型算子：对上游 16 领域识别与筛选的输出执行
**入库门禁 → 元数据完整性构建 → JSONL/TSV/ZIP 原子打包 → 清单/阶段产物/日志**，
产出可交付数据集与可审计的异常清单。

> 【无血缘改造 2026-10-19】本算子不再生成/读取/返回任何 `lineage.json` / `lineage.jsonl`。
> 阶段内中间路径由 `/predict` 返回的 `data.outputFile`（路径字符串）接力，阶段末该字段为交付
> 文件内容；成功后 `data` 固定为 `outputFile`、`rejectFile`、`stageZip`、`storageUri` 四字段，
> 阶段产物归档为 `artifacts/stage_18_<uniqueId>.zip`。

- 算子编号：18
- 服务端口：8018
- 入口：`inference:app`（FastAPI）
- 上游：算子 16（默认 `16_domain_checked.jsonl`）；无血缘改造后不读取上游阶段血缘文件
- 下游：算子 19 数据质量报告

## 1. 功能边界

**做什么**

1. **入库门禁**：默认只放行 `quality_grade ∈ {S, A}`、`delivery_eligible != false`、
   `quality_status/fluency_status/domain_status` 均为 `accepted` 的记录；例外是算子15
   未完成 Gold 校准时的 `fluency_status=waiting_calibration` + `fluency_business_gate_approved=false`，
   按规范 4.15 仅作辅助证据放行（记录 `fluency_gate`/`fluency_evidence_only`，不阻塞交付）。
   **最小长度兜底（2026-11-04）**：`source` / `target` 去空白后短于 `min_source_chars` /
   `min_target_chars`（默认均为 5）的记录进入长度检查，且在 `waiting_manual` 例外判定**之前**执行——
   长度是记录本身的缺陷，不因状态允许而放行。
   **短 + 碎片形态双条件（2026-11-04 补）**：纯长度会把「增编/Addendum」「缅甸/Myanmar」
   这类真术语对一起砍掉（flow_15 实测 37 条短记录中 12 条是术语对）。默认
   `short_record_policy=fragment_only` 时，短记录还须命中碎片形态才拒收（`too_short_source` /
   `too_short_target`）：源文以 `、，；;（(「『“` 收尾、译文以小写字母或 `、，；;）)` 开头、
   译文以 `、，,；;` 收尾、或译文是裸日期/编号（`6-24 April 2015`）。冒号**不算**碎片信号，
   `报告员：/Rapporteur:` 属完整标题式术语对。放行的短记录写
   `metadata.short_record_exempt=term_pair` 并计入 `short_record_exempt_records`，不静默放行；
   `short_record_policy=always` 可一键退回旧的纯长度硬门。
2. **元数据构建**：构建 `language_pair`、`created_at`、`operator_versions`、`lineage_refs`，
   并镜像进 `metadata`；`operator_versions` 只来自**显式配置**（无配置时 `{}` +
   `operator_versions_source=unavailable`），`lineage_refs` 恒为空数组且
   `lineage_refs_source=unavailable`（不再读取上游血缘，不伪造版本号）。
3. **完整性检查**：必填字段缺失 → `missing_metadata`；顶层与 `metadata` 冲突 → 默认拒收；
   数值类型/量纲异常 → 拒收（不补分、不 clamp、不猜类型）。
4. **打包**：写 `18_dataset.jsonl`、`18_dataset.tsv`、`18_manifest.json`、`18_delivery.zip`，
   全部先写 `.tmp` 再 `os.replace` 原子替换；`bytes`/`sha256` 从真实落盘文件计算。
5. **异常清单与阶段产物**：不合格样本进 `18_rejects.jsonl`（不静默丢弃）；阶段结束前把主输出、异常清单与 `run.log` 打包为 `artifacts/stage_18_<uniqueId>.zip`，不再产出任何血缘文件。

**不做什么**

- 不做任何模型推理，不重算质量分/语义分/对齐置信度；上游分数原样透传。
- 不修改文本内容（切断、改写、翻译均不在本节点）。
- 不连接 MongoDB / Elasticsearch / Parquet，只在 manifest 预留 `indexes` 描述。
- 不实现 `/downloadFile`（共享契约只允许三个接口）。
- 不把阶段末 `data.outputFile` 当作下一算子的路径。

## 2. 输入输出

输入（严格 UTF-8 JSONL）与输出字段的完整定义见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。

| 产物 | 说明 |
| --- | --- |
| `18_dataset.jsonl` | 交付数据集（透传字段 + 交付元数据） |
| `18_dataset.tsv` | 同内容的 TSV 视图（12 列，可无损回读） |
| `18_manifest.json` | 清单：计数、字节数、sha256、来源、模型/规则版本、预留索引 |
| `18_delivery.zip` | 交付包，默认包含 `18_dataset.jsonl`、`18_dataset.tsv`、`18_manifest.json`（**不含任何血缘文件**） |
| `18_rejects.jsonl` | 异常清单：行号、id、稳定原因码、原记录 |
| `artifacts/stage_18_<uniqueId>.zip` | 阶段产物：主输出、异常清单与 `run.log`（2026-10-19 起取代 `lineage.jsonl`） |
| `run.log` | 逐行日志：`[时间] [动作] 消息` |

交付记录字段（除上游透传字段外）：

```json
{
  "id": "del-0001",
  "language_pair": "zh-en",
  "language_pair_derived": true,
  "operator_versions": {},
  "operator_versions_source": "unavailable",
  "lineage_refs": [],
  "lineage_refs_source": "unavailable",
  "created_at": "2026-10-05T09:35:54+08:00",
  "package_version": "v1.0.0",
  "delivery_status": "delivered",
  "delivery_checked_at": "2026-10-05T09:35:54+08:00"
}
```

## 3. 配置（`configs/config.yaml`）

关键项（可通过 `/predict` 的 `config` 对象覆盖，未声明字段一律拒绝）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `input_encodings` | `[utf-8]` | 严格解码，失败即 `failed` |
| `allowed_grades` | `[S, A]` | 放行等级 |
| `allow_c` | `false` | 是否放行 C 级 |
| `allow_waiting_manual` | `false` | 是否放行任务许可的待人工记录 |
| `fluency_gate_mode` | `evidence_only_until_gold` | `enforce`=严格门禁；`evidence_only_until_gold`=仅当 `fluency_business_gate_approved` 明确为 `false` 时，把 `waiting_calibration` 作为非阻塞辅助证据 |
| `fluency_approval_fields` | `[fluency_business_gate_approved]` | 判断算子15 是否已获业务批准的字段 |
| `fluency_evidence_statuses` | `[waiting_calibration]` | 可作辅助证据的流畅度状态 |
| `require_status_fields` | `true` | 三个质控状态字段是否必填 |
| `min_source_chars` / `min_target_chars` | `5` / `5` | 最小长度兜底：去空白后不足进入长度检查（`too_short_source` / `too_short_target`），`0` 关闭 |
| `short_record_policy` | `fragment_only` | 短记录处置：`fragment_only` 仅碎片形态拒收、术语对放行并标记；`always` 纯长度硬门 |
| `respect_delivery_eligible` | `true` | 是否遵循上游 `delivery_eligible` 标记 |
| `required_fields` | 10 个上游字段 | 缺失即 `missing_metadata` |
| `derive_language_pair` | `true` | 是否由语言声明派生 `language_pair` |
| `metadata_conflict_policy` | `reject` | `reject` / `prefer_top_level` / `prefer_metadata` |
| `duplicate_id_policy` | `reject` | 重复 id：`reject` / `keep`（keep 时标记 `metadata.duplicate_id=true`） |
| ~~`upstream_lineage_file`~~ | 已停用 | 2026-10-19 无血缘改造后不再查找/读取任何上游血缘文件（配置项已注释） |
| `tsv_fields` / `tsv_delimiter` | 12 列 / `\t` | TSV 视图定义 |
| `zip_members` | 数据集 + TSV + 清单 | 交付包成员（只允许业务产物，不含血缘文件） |
| `empty_delivery_policy` | `partial` | 无记录通过门禁时：`partial`（默认，零交付是严格门禁的正常裁定，产出空数据集）或 `failed`（严格模式） |
| `indexes` | 全部 `enabled=false` | 预留的索引描述 |

## 3.1 规范参数映射（工具清单第 18 项）

| 规范/清单参数 | `configs/config.yaml` 键 | 说明 |
| --- | --- | --- |
| 存储格式策略：单条 JSON/TSV | `dataset_jsonl_name` / `dataset_tsv_name` / `tsv_fields` | 一期固定 JSONL+TSV（清单「批量 Parquet/TFRecord」为 reserved，未实现） |
| 数据库策略：MongoDB 结构化存储 / Elasticsearch 索引开关 | `indexes`（全部 `enabled=false`） | 一期 reserved，未实现，README 已声明 |
| 元数据组装模板（任务书元数据字段） | `required_fields` / `conflict_check_fields` / `derive_language_pair` | 元数据完整性校验与派生规则；`operator_versions`/`lineage_refs` 降级口径见 §4 |
| 数据集过滤策略：S/A 入库 / S/A + 人工判定合格 C 级入库 | `allowed_grades` / `allow_c` / `allow_waiting_manual` / `accepted_status_map` / `respect_delivery_eligible` | 入库门禁，见 §5 降级行为 |

> 备注：`dataset/sample/lineage.json` 为 2026-10-05 血缘版遗留样例，仅作历史对照，
> 现行实现不再生成/读取该文件。

## 4. 算法/模型后端

**纯规则，无模型**。本节点不引入任何模型权重或推理服务，因此
`operator_versions`、`model_versions` 只记录真实存在的版本证据：

- `operator_versions`：仅取配置项 `operator_versions`（`operator_versions_source=config`）；
  未配置时为 `{}` 且 `operator_versions_source=unavailable`，
  并在 `statistics.operator_versions_unavailable` 计数，**绝不编造版本号**。
- `model_versions`：仅收录交付记录中真实存在的 `*_model_version` 字段
  （无血缘改造后不再从上游血缘读取）。

## 5. 降级行为

| 场景 | 状态 | 行为 |
| --- | --- | --- |
| 配置非法（未知键、类型错误、`zip_members` 未知产物名等） | `failed` | 不产出任何交付文件，返回 `配置错误：<异常类型>` |
| 输入无法按配置编码解码 | `failed` | 不产出文件，提示检查文件编码 |
| 输入为空 / 无有效记录 | `failed` | 不留下空产物 |
| 输入路径与输出路径相同 | `failed` | 拒绝覆盖输入 |
| 全部记录被拦截且 `empty_delivery_policy=failed` | `failed` | 产物与异常清单仍落盘供审计，但不暴露 `outputFile` |
| 全部记录被拦截且 `empty_delivery_policy=partial`（默认） | `partial` | 产出空数据集与完整清单；与"输入为 0"的 failed 语义区分 |
| 部分记录被拦截 | `partial` | 交付合格记录 + 异常清单 |
| 无拦截且交付集含许可的 `waiting_manual` | `waiting_manual` | `delivery_status=waiting_manual` |
| 未显式配置 `operator_versions` | `success`/`partial` | `operator_versions={}` + `unavailable` 标记（不影响业务状态） |

## 6. 启动命令

```bash
pip install -r requirements.txt
# 服务（默认 8018，产物写入 OUTPUT_DIR，默认 ./output）
uvicorn inference:app --host 0.0.0.0 --port 8018
# 容器（本机未安装 Docker，未执行构建验证）
docker build -t parallel-corpus-op18 .
docker run -p 8018:8018 -e OUTPUT_DIR=/app/output parallel-corpus-op18
```

## 7. CLI 示例

```bash
python processor.py \
  --input dataset/sample/demo.jsonl \
  --unique-id smoke_cli \
  --output-root output_smoke
```

实测输出（2026-10-19 无血缘改造后真实执行，`D:\python\python.exe -X utf8 processor.py ...`）：

```
status=partial statistics={'input_records': 8, 'accepted_records': 3, 'rejected_records': 5,
'delivery_records': 3, 'S_count': 2, 'A_count': 5, 'C_count': 1, 'waiting_manual_count': 1,
'delivered_waiting_manual_records': 0, 'duplicate_id_records': 0, 'metadata_conflict_records': 1,
'missing_metadata_records': 1, 'operator_versions_unavailable': 3, 'bytes': 3675, ...,
'zip_members': ['18_dataset.jsonl', '18_dataset.tsv', '18_manifest.json'],
'operator_versions_source': 'unavailable', 'lineage_refs_source': 'unavailable'}
stageZip=output_smoke\smoke_cli\artifacts\stage_18_smoke_cli.zip
storageUri=local-mirror://smoke_cli/stage_18_smoke_cli.zip
```

任务目录内 `18_delivery.zip` 含 3 个业务成员（无 `lineage.jsonl`），任务目录下无任何
`lineage*` 文件。

## 8. API 示例

```bash
# 1) 上传
curl -X POST "http://127.0.0.1:8018/uploadFile?uniqueId=task01_file01" -F "file=@data.jsonl"

# 2) 阶段中间算子（isLastOperator=false）：data.outputFile 是输出文件路径
curl -X POST http://127.0.0.1:8018/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task01_file01","isLastOperator":false,"inputFile":"input/data.jsonl"}'
# -> {"code":0,"msg":"success","data":{"outputFile":".../18_dataset.jsonl","rejectFile":".../18_rejects.jsonl","stageZip":".../artifacts/stage_18_task01_file01.zip","storageUri":"local-mirror://task01_file01/stage_18_task01_file01.zip"}}

# 3) 阶段末算子（isLastOperator=true）：data.outputFile 是交付文件内容
curl -X POST http://127.0.0.1:8018/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task01_file01","isLastOperator":true,"inputFile":"input/data.jsonl"}'
# -> {"code":0,"msg":"success","data":{"outputFile":"<UTF-8 数据集内容>","rejectFile":"...","stageZip":"...","storageUri":"..."}}

# 可选：operatorId（传了必须等于 "18"）、params / config 覆盖项（params 优先；未知键报配置错误）

# 4) 清理
curl -X POST "http://127.0.0.1:8018/removeFile?uniqueId=task01_file01"
```

## 9. 输出与阶段产物示例

阶段产物（`artifacts/stage_18_<uniqueId>.zip`）固定包含主输出 `18_dataset.jsonl`、
异常清单 `18_rejects.jsonl` 与 `run.log`；响应同时返回 `stageZip` 与 `storageUri`
（`file:///...` 或 `local-mirror://<uniqueId>/<zipname>`）。任务目录**不再产出**
`lineage.json` / `lineage.jsonl`，交付包 `18_delivery.zip` 也不包含任何血缘成员。

清单（`18_manifest.json`，节选）：`record_count=3`，
`files` 中每个成员带真实 `bytes` 与 `sha256`；zip 自身 `sha256` 见运行统计
`statistics.package_sha256`（清单在 zip 内，不能自引用；无血缘文件承载该哈希）。

## 10. 资源需求

- CPU 单核即可，规则处理；无 GPU、无模型权重。
- 内存与输入规模线性相关但不随文件增长累积：JSONL 与 TSV 均为**流式**读写，
  仅交付记录数上限受 `delivered_records` 列表（用于汇总 manifest）影响。
- 磁盘：数据集 + TSV + 清单 + 交付包 + 阶段产物 zip（zip 与源数据同量级）。

## 11. 已知限制

1. `18_delivery.zip` 的成员默认为数据集、TSV、清单；异常清单默认不入包（可用 `zip_members` 调整）；包内只允许业务产物，不含血缘文件。
2. zip 内清单不含 zip 自身哈希（自引用不可能），该哈希只记录在运行统计 `statistics.package_sha256`（无血缘文件）。
3. `statistics.S_count/A_count/C_count` 口径为**输入有效记录**的等级分布
   （用于算子 19 计算留存率），交付集分布另见 `delivery_grade_counts`。
4. 未连接外部索引（MongoDB/Elasticsearch/Parquet），`indexes` 仅为预留描述。
5. `lineage_refs` 恒为空数组且 `lineage_refs_source=unavailable`（字段保留兼容，不再读取上游血缘）。
6. `operator_versions` 仅在显式配置 `operator_versions` 时非空；`dataset/sample/lineage.json`
   为历史样例，已停用（处理器不再读取），仅保留回溯。
7. 本机未安装 Docker，镜像构建**未验证**。

## 12. 真实测试结果

环境：Python 3.9.7 / Windows。

```bash
D:\python\python.exe -m pytest tests -q
# 48 passed（2026-11-04 短+碎片形态双条件后；最小长度兜底时为 43 passed）
```

> 历史记录（2026-10-05 血缘版）：`python -m pytest tests/ -q` → `34 passed, 3 warnings in 9.72s`。

| 测试范围 | 覆盖点 |
| --- | --- |
| 样例与统计一致性 | 8 条样例 → 交付 3 / 拦截 5，S/A/C 计数、交付集等级分布 |
| 门禁 | C 级拦截、`allow_c`、`waiting_manual`、`allow_waiting_manual`、状态缺失/非接受、`delivery_eligible=false`、最小长度兜底（`too_short_source`/`too_short_target`，优先于 `waiting_manual` 放行；默认仅碎片形态拒收，术语对标记放行） |
| 元数据完整性 | 缺字段、`derive_language_pair=false`、冲突默认拒收、`prefer_top_level`、类型错误、越界、重复 id 两种策略 |
| 异常输入 | 损坏 JSON、非对象行、`metadata` 非对象、空输入、错误编码（GB18030）、非法配置、输入=输出路径 |
| 打包一致性 | TSV 表头与特殊字符（制表符/换行/引号）无损回读；manifest 计数/字节/sha256 与真实文件一致；zip 成员与哈希一致 |
| 阶段产物与隔离 | 交付 zip 无 `lineage.jsonl`、任务目录无 `lineage*`；阶段产物 zip 含主输出/异常清单/`run.log`；`operator_versions` 仅显式配置、`lineage_refs` 恒空；四字段契约；两个 uniqueId 隔离 |
| 接口契约 | 三接口、无 `/downloadFile`、`isLastOperator` 两种返回形态、缺参校验、`inputFile` 路径穿越拒绝、`removeFile` |

> 以下保留 2026-10-05 血缘版冒烟记录作为历史对照（当时的 `lineageFile` / `lineage.json`
> 行为已停用）。2026-10-19 无血缘改造后的真实 CLI 结果见 §7（`status=partial`，输入 8 /
> 交付 3 / 拦截 5，`stageZip` 与 `storageUri` 均已生成，任务目录无 `lineage*`）；API 契约
> 由 `tests/test_api.py` 四字段与阶段产物断言覆盖（pytest 36 passed）。

CLI 冒烟（2026-10-05）：`status=partial`，输入 8 / 交付 3 / 拦截 5，product sha256 与 manifest 一致。
API 冒烟（2026-10-05，真实 `uvicorn` 127.0.0.1:8018 走 TCP）：19 项检查全部通过（`/downloadFile` 404、
非法 `uniqueId` 与路径穿越拒绝、两个 uniqueId 隔离、`removeFile` 清理等；响应形态为当时的旧血缘口径）。
2026-10-19 无血缘改造后，`tests/test_api.py` 已改为断言四字段、阶段产物 zip 与「无血缘文件」，由 pytest 36 passed 覆盖。

`python -m json.tool map.json`、`python -m compileall -q .` 均通过。
