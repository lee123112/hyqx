# 算子9：文档/段落级对齐（document_paragraph_alignment v1.0.0）

## 1. 职责边界

- 输入：算子10 生成的对齐计划 `10_align_plan.json`（或 `alignment_plan.json`）+ 计划引用的源/目标 UTF-8 文档。
  计划定位支持两种方式：`/predict` 的 `inputFile`（任务目录内的计划文件，原有解析逻辑），
  或 `params.planDirectory`（计划目录，目录下需有 `alignment_plan.json` 或 `10_align_plan.json`）。
- 输出：`09_paragraph_pairs.jsonl`（段落级 1:1 / 1:2 / 2:1 关系）、
  `09_paragraph_rejects.jsonl`（低置信、未对齐、多候选、异常）、
  `artifacts/stage_09_<uniqueId>.zip`（阶段产物）、`run.log`。
  **2026-10-19 起不再输出 `lineage.json`**。
- 只做段落级对齐：**不切句**（交给算子10）、**不做语义模型推理**（不接入
  LaBSE 等模型）。所有分数均为长度相似度、段落位置先验、可比 token 重叠的
  真实计算值。

## 2. 目录结构

```
PRLt_align_document_paragraph/
├── processor.py         # 核心处理逻辑 + CLI
├── inference.py         # FastAPI 三接口
├── configs/config.yaml  # 阈值、权重、窗口、降级策略
├── utils/               # io/schemas/logger/lineage(停用)/artifacts/timeutil/documents/scoring/alignment
├── dataset/sample/      # demo.jsonl（对齐计划）+ demo_zh.txt + demo_en.txt
├── dataset/DATA_SCHEMA.md
├── tests/               # test_operator.py + test_api.py
├── map.json / Dockerfile / requirements.txt
```

## 3. 处理流程

1. 解析配置（严格校验，非法配置即结构化失败）。
2. 定位并读取对齐计划；计划缺失、为空、损坏、未路由到段落级阶段 → 失败。
3. 降级策略检查：`backend=model` 或 `fallback_policy=fail` 且无语义模型时
   直接结构化失败（`model_unavailable`），**不伪造任何模型分数**。
4. 解析源/目标文档路径（默认限制在任务目录内，越界即失败），严格按
   `input_encodings` 解码并按空行切分段落。
5. 在 `candidate_window` 窗口内生成 `1:1`/`1:2`/`2:1` 候选并逐项打分。
6. 单调动态规划选优（状态 = 已消费的源/目标段落数，动作 = 对齐或跳过），
   相同输入结果确定；多段关系扣 `group_penalty`。
7. 低于 `min_confidence` 的关系写入异常清单，并按 `low_confidence_policy` 处理：
   默认 `mark_and_keep` —— 关系仍写入通过输出，标 `review_status=needs_review`、
   `low_confidence_reason=low_confidence`、`flagged_by=08`；`drop` 恢复旧行为（只进异常清单）。
   未对齐段落逐条记录；对通过的关系做「第二优对齐」复核，总分差 ≤ `ambiguity_margin`
   时标 `review_status=needs_review`、`flagged_by=08`。
8. 流式写 JSONL，打包 `artifacts/stage_09_<uniqueId>.zip` 并写 `run.log`。

## 4. 配置（configs/config.yaml）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `input_encodings` | `[utf-8]` | 严格解码顺序 |
| `candidate_window` | 3 | 源/目标段落起始索引差上限 |
| `min_confidence` | 0.45 | 通过阈值 |
| `length_weight` / `position_weight` / `overlap_weight` | 0.55 / 0.30 / 0.15 | 打分权重；`overlap` 不可比时前两项重新归一化 |
| `allow_1_to_2` / `allow_2_to_1` | true / true | 是否允许一对多/多对一 |
| `group_penalty` | 0.05 | 多段合并关系的结构惩罚 |
| `ambiguity_margin` | 0.10 | 判定多候选的第二优对齐总分差阈值 |
| `fallback_policy` | rule_degraded | `rule_degraded` / `fail` |
| `backend` | rule_degraded | `rule_degraded` / `model`（未接入 → 失败） |
| `allow_external_document_paths` | false | 是否允许文档位于任务目录之外 |
| `low_confidence_policy` | mark_and_keep | 低置信关系的去向：`mark_and_keep`（默认，标记后保留）/ `drop`（旧行为） |

`/predict` 的 `params`（或兼容旧参数 `config`）可覆盖上述任意配置项，`params` 优先；
未知字段直接报配置错误。

## 5. 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.8 小节的 `param_` 行与
`configs/config.yaml` / 请求参数对应如下：

| 规范参数 | config 字段 / 请求参数 | 说明 |
|---|---|---|
| `params.planDirectory` | 请求参数（由 `inference.py` 解析） | 文档对齐计划目录；目录下取 `alignment_plan.json` 或 `10_align_plan.json`，计划引用的文档允许落在该目录内 |
| `param_长度相似度阈值` | `length_weight` + `min_confidence` | 长度相似度作为三项真实打分之一（权重 `length_weight`）参与加权，最终由 `min_confidence` 判定通过；当前版本不设单独的 0.6 长度相似度硬阈值 |
| `param_关键词重叠度阈值` | `overlap_weight` + `min_confidence` | 关键词（可比 token）重叠度作为打分项（权重 `overlap_weight`）参与加权；当前版本不设单独的 0.5 硬阈值 |

## 6. 接口（三接口）

| 接口 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | 上传文档/计划到 `OUTPUT_DIR/<uniqueId>/input/` |
| `POST /predict` | 入参 `uniqueId`、`operatorId`(选传，须为 `"09"`)、`isLastOperator`(bool)、`inputFile`、`params`/`config`；输入定位：`inputFile` 或 `params.planDirectory` |
| `POST /removeFile?uniqueId=` | 删除任务目录 |

外层响应固定 `{code,msg,data}`（`code` 0=成功 1=失败）。`/predict` 成功响应 `data` 固定 4 个字段
（不再返回 `lineageFile`）：

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
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**输出文件内容**（UTF-8 文本）。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_09_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- `uniqueId` 只允许单个任务标识，含路径分隔符即拒绝；`planDirectory` 只允许任务目录内的目录。

## 7. 运行与验证

```bash
pip install -r requirements.txt

# 处理逻辑 + 接口测试
python -m pytest tests -q

# CLI smoke（09 的输入是对齐计划 JSON，不是普通句对 JSONL）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 服务
uvicorn inference:app --host 0.0.0.0 --port 8009
```

### 验证结果（2026-10-19 无血缘改造后实跑）

- `python -m pytest tests -q` → `27 passed in 1.67s`（2026-11-05 mark_and_keep 后）
- `python -m compileall -q .` → exit 0
- `python -m json.tool map.json` → 通过
- CLI smoke：`status=success`，源/目标各 4 个段落，输出 4 条 `1:1` 关系、异常清单 0 条，
  `statistics.pairs=4`、`rejected=0`；产出 `artifacts/stage_09_smoke_cli.zip`
  （内含 `09_paragraph_pairs.jsonl`、`09_paragraph_rejects.jsonl`、`run.log`），
  `storageUri=local-mirror://smoke_cli/stage_09_smoke_cli.zip`，任务目录**无** `lineage.json`。

## 8. 已知限制

- 未接入语义模型：无法识别「长度与位置都相似但语义不相关」的段落对；
  这类情况依赖下游（算子10/11/12）继续过滤。`backend` 固定为 `rule_degraded`。
- `1:2`/`2:1` 的判定由分数与阈值驱动，与计费/工程上「允许合并的粒度」口径
  无关，是否允许由配置开关控制。
- 多候选复核需要为每条通过关系重算一次 DP，`源段落数 × 目标段落数 > 10000`
  时标记 `ambiguity_evaluated=false`（不猜测）。
- 段落切分只认空行（`\n\n`）；无空行的长文本会被当作单一段落。
- 正式接口不提供 `/downloadFile`；不再产出或返回任何血缘文件。
- 规范 4.8 的长度相似度/关键词重叠度阈值当前以打分权重 + 总置信度阈值实现，
  未拆成两个独立硬阈值，接入方若需严格硬阈值需扩展配置。
