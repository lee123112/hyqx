# 算子 12：双语对齐置信度（alignment_confidence）

汇总子句/短语级、句子级、文档/段落级对齐证据，按配置权重输出统一对齐置信度
`alignment_confidence`，并标记低置信与多候选并列冲突。本算子不重新生成对齐关系，只做
**证据聚合与加权融合**；所有分项都来自上游真实字段，缺失分项不进分母，绝不补默认分。

- 算子编号：12（多粒度双语对齐处理阶段）
- 类型：规则型（不加载模型、不伪造分数）
- 版本：v1.0.0
- 输出：`12_scored.jsonl`、`12_rejects.jsonl`、`artifacts/stage_12_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）

## 职责边界

- 做：分项读取（顶层 / `metadata` / 旁路证据文件）、有效权重归一、加权融合、低置信标记、
  多候选冲突识别、异常清单、阶段产物打包与日志。
- 不做：重新计算相似度或对齐关系（属算子 09/10/11）、S/A/C 分级（算子 13）、
  语义等价检测（算子 14）、流畅度检测（算子 15）。

## 目录

```
processor.py               核心逻辑 + CLI（load_config/run/分项校验/融合/异常归集）
inference.py               三接口 FastAPI 服务（端口 8012）
configs/config.yaml        方向、粒度与权重、阈值、冲突容差、旁路证据文件
utils/io.py                流式 JSONL 读写、任务目录、uniqueId 与 path 穿越防护
utils/artifacts.py         阶段产物 zip 打包与 storageUri 对象存储占位
utils/timeutil.py          时间戳工具（now_iso）
utils/lineage.py           历史血缘实现（已停用，仅保留便于回溯）
utils/logger.py            逐行日志 [时间] [动作] 消息
utils/schemas.py           配置与请求契约（未知字段一律拒绝）
utils/scoring.py           分项解析、有效权重归一、加权融合、冲突候选打分
tests/test_operator.py     分项融合、边界、冲突、异常输入、阶段产物、UTF-8 与隔离
tests/test_api.py          三接口、4 字段返回契约、operatorId/params、路径安全
dataset/sample/demo.jsonl  6 条子句级样例（含旁路证据与冲突样例）
dataset/DATA_SCHEMA.md     输入/输出/异常字段说明
```

## 输入输出

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 输入 | `11_clause_pairs.jsonl`（默认名，阶段内由 `inputFile` 指定） | 上游子句/短语对齐记录，含 `metadata.source_ref`/`target_ref` 与可选分项证据 |
| 通过输出 | `12_scored.jsonl` | 原记录 + `alignment_confidence`、`component_scores`、`used_weights`、`review_status`、`conflict_candidates` |
| 异常输出 | `12_rejects.jsonl` | 缺字段、语言声明不符、缺父句引用、分项缺失/越界/类型错误、无候选分数等 |
| 阶段产物 | `artifacts/stage_12_<uniqueId>.zip` | 主输出 + 异常清单 + `run.log`；无血缘文件 |
| 日志 | `run.log` | 逐行 `[时间] [动作] 消息`，不写密钥、完整原文和完整堆栈 |

产物目录：`OUTPUT_DIR/<uniqueId>/`（`OUTPUT_DIR` 默认 `./output`，可用环境变量覆盖）。

## 接口（三接口）

外层响应固定 `{code,msg,data}`（`code` 0=成功，1=失败）。上传/删除成功严格返回 `{"code":0,"msg":"success"}`。

| 接口 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | multipart 字段 `file` 写入任务输入目录 `OUTPUT_DIR/<uniqueId>/input/` |
| `POST /predict` | JSON 必传 `uniqueId`、布尔 `isLastOperator` 与阶段内 `inputFile`；`operatorId` 选传（若传须为 `"12"`）；`params`/`config` 覆盖配置，`params` 优先 |
| `POST /removeFile?uniqueId=` | 只删除当前 `uniqueId` 任务目录 |

`/predict` 成功响应 `data` 固定 4 个字段（不再返回 `lineageFile`）：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "outputFile": "<中间算子=输出文件路径；阶段末=输出文件内容>",
    "rejectFile": "<异常清单路径>",
    "stageZip": "<阶段产物 zip 路径>",
    "storageUri": "<对象存储地址：file:// 或 local-mirror://>"
  }
}
```

- 中间调用（`isLastOperator=false`）：`data.outputFile` 是**输出文件路径**，下一算子 `inputFile` 直接用它。
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**输出文件内容**。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_12_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- 失败返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物。
- 不提供 `/downloadFile`，也不再产出或返回血缘文件。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.12 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_粒度权重` | `enabled_granularities` / `component_weights` / `component_source_operators` | 子句/短语 0.6、句子 0.3、文档/段落 0.1；未启用粒度权重置零不进分母 |
| `param_低置信标记阈值` | `low_confidence_threshold` / `review_threshold` / `low_confidence_policy` | 低于阈值只标记并审计，不物理删除；`mark_and_keep` 或 `move_to_rejects` |
| `param_父句结构守卫`（扩展） | `parent_guard` / `repetition_containment` / `parent_ratio_min` / `parent_ratio_max` / `guard_confidence_cap` / `sibling_duplicate_guard` | 父句内重复单位、跨语言长度比失衡、段内兄弟句对重复时封顶置信度并转人工复核 |

## 融合口径

1. **分项来源**：`component_scores` 或 `confidence_components` 显式字段优先；否则读取
   `metadata.confidence`（子句/短语）、旁路证据文件（句子/段落级，`sentence_evidence_file` /
   `paragraph_evidence_file`）。缺失分项不进分母，只按实际可用权重重新归一。
2. **有效权重**：`used_weights = component_weights[实际可用粒度] / Σ(实际可用粒度权重)`；
   全部缺失 → `score_missing` 进异常清单，绝不补默认分。
3. **冲突**：`metadata.candidates` 中多个候选按同一融合口径打分，最优与次优分差
   ≤ `ambiguity_margin` → `conflict_multi_candidate` 且 `review_status=waiting_manual`；
   冲突候选完整留存，不物理删除。
4. **父句级结构守卫（2026-11-05）**：融合分只看得见单条子句对，看不到父句内的结构问题。
   预扫输入按 `metadata.parent_pair_id` 聚合，父句内任一侧 shingle 包含度 ≥
   `repetition_containment`（默认 0.80）或源/目标计量单元比落在
   [`parent_ratio_min`, `parent_ratio_max`] = [0.88, 4.10] 之外时，把该父句全部记录的
   `alignment_confidence` 压到 `guard_confidence_cap`（默认 0.45），原因写
   `metadata.parent_guard`（含 `repetition` / `ratio` 证据与 `reasons`），`fused_confidence`
   保留原融合分。不删记录；`parent_guard: false` 可关闭。
   实测：自然语料 399 个父句仅 1 个误报（长度比 0.400，0.25%）；构造缺陷 length_ratio 15 条中
   7 条长度比 < 0.88、7 条包含度 ≥ 0.83。
   **段内兄弟对重复**（`sibling_duplicate_guard`，2026-11-05）：整段译文被复制一遍后，
   09/10 会把它拆成两条 1-1 句对，各自长度比正常、父句内部也没有重复——只有按段落
   （`parent_pair_id` 去掉 `-pairNNNN` 后缀）把兄弟 pair 的译文放在一起比才看得见。
   两条 pair 的 target shingle 包含度 ≥ `repetition_containment` 时，两条都标
   `duplicate_sibling_pair` 并同样封顶。实测自然语料 895 段 0 命中；
   缺陷 `defect-12-021` p2（整段译文重复）包含度 1.0 命中。
5. **状态**：`alignment_confidence >= review_threshold` → `auto_pass`；
   否则 `needs_review`；低于 `low_confidence_threshold` 追加 `low_confidence` 审计标记。
   父句守卫命中、或上游 `metadata.review_status=needs_review` 时同样转 `needs_review`。
6. `confidence_version` 与全部融合参数在记录与 `metadata` 中镜像，便于复算。

## 测试

```bash
python -m pytest tests/ -q
# 48 passed
```

覆盖：分项加权手算复算、缺失分项重归一、禁用粒度不影响分母、上游可追溯性、显式
`component_scores`、旁路证据文件、0/1 边界、越界/负数/非数值/布尔分数、低置信策略、
多候选冲突、语言声明不符、缺父句引用、空文件、非法配置、UTF-8 输出与任务隔离、
阶段产物 zip 与「无血缘文件」、三接口 4 字段契约、`operatorId`/`params` 校验。

## 验证结果（2026-10-19 无血缘改造后实跑）

- `D:\python\python.exe -m pytest tests -q` → `48 passed`（2026-11-05 段内兄弟对重复守卫后）
- `D:\python\python.exe -m compileall -q .` → exit 0
- `D:\python\python.exe -m json.tool map.json` → 通过
- CLI：`processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke`
  → `status=degraded`，`input_records=6`、`scored=6`、`rejected=2`、`low_confidence=1`、
  `waiting_manual=1`、`conflicts=1`；产出 `artifacts/stage_12_smoke_cli.zip`
  （内含 `12_scored.jsonl`、`12_rejects.jsonl`、`run.log`），
  `storageUri=local-mirror://smoke_cli/stage_12_smoke_cli.zip`，任务目录**无** `lineage.json`。
- API smoke（TestClient）：upload→predict（中间/阶段末）→removeFile；成功 `data` 键恰为
  `{outputFile,rejectFile,stageZip,storageUri}`；`operatorId` 不匹配、非法 `params`、缺失
  `isLastOperator`/`inputFile` 均返回结构化失败；`params.confidence_version=v9.9.9` 可在输出记录中观察到。

## 已知限制与待确认口径

- 只融合已有对齐证据，不重新计算相似度；分项缺失时按有效权重重新归一，不做任何默认补分。
- 旁路证据文件路径支持绝对路径、任务目录、算子目录与工作目录相对路径；找不到时不补分。
- 阶段产物 zip 由算子侧生成；对象存储仅提供 `OBJECT_STORE_DIR` 本地镜像占位，未接入真实
  OSS/S3 客户端；未在真实容器环境验证。
- 不再使用血缘文件传递阶段中间路径：中间路径由 `data.outputFile` 返回，阶段末由
  `data.outputFile` 返回内容；不再提供 `/downloadFile`。
