# 算子10：句子级对齐（sentence_alignment）

在算子9 已确定的段落对应关系内，把中英段落切成句子并建立句子级句对；优先使用本地
Qwen3-Embedding-0.6B（1024 维，`local_files_only`）语义相似度，模型不可用时按配置降级为
长度+证据规则后端，状态标记 `degraded` 且语义分写 `null`，任何情况下都不伪造模型分数。

## 职责边界

- 做：段落内切句、句对打分、段落内单调 DP（`1-1`/`1-2`/`2-1`/跳过）、置信度与异常归集、
  阶段产物打包与日志。**2026-10-19 起不再输出 `lineage.json`。**
- 不做：段落关系判定（算子9）、语言检测、去重、最终交付治理、编码归一（上游算子）。
- 输入默认取上游通过集 `09_paragraph_pairs.jsonl`；下游默认消费 `10_sentence_pairs.jsonl`。

## 目录

```
processor.py               核心逻辑 + CLI
inference.py               三接口 FastAPI 服务（端口 8010）
configs/config.yaml        模型、窗口、权重、阈值、编码等配置
utils/                     io / lineage(停用) / logger / schemas / segmenter / embedder / aligner / artifacts / timeutil
tests/test_segmenter.py    切句、分项打分、向量后端、DP 单测
tests/test_operator.py     模型/降级、低置信、多候选、异常输入、阶段产物、编码端到端测试
tests/test_api.py          三接口、4 字段契约、链式输入、路径安全与任务隔离
dataset/sample/demo.jsonl  4 条段落样例（覆盖 1-1、1-2、2-1）
dataset/DATA_SCHEMA.md     输入/输出/异常字段说明
```

## 输入输出

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 输入 | `09_paragraph_pairs.jsonl` | 段落级句对，见 `dataset/DATA_SCHEMA.md` |
| 通过输出 | `10_sentence_pairs.jsonl` | 句子级句对，含引用、`confidence`、`scores`、`backend`、`low_confidence_reason`、`model_version` |
| 异常输出 | `10_sentence_rejects.jsonl` | 未对齐句子、多候选、低置信、模型错误及输入格式异常 |
| 阶段产物 | `artifacts/stage_10_<uniqueId>.zip` | 主输出 + 异常清单 + `run.log`（不含任何血缘文件） |
| 日志 | `run.log` | 逐行 `[时间] [动作] 消息`，不写密钥、完整原文和完整堆栈 |

产物目录：`OUTPUT_DIR/<uniqueId>/`（`OUTPUT_DIR` 默认 `./output`，可用环境变量覆盖）。

## 对齐口径

1. **切句**：中文按 `。！？；…` 等终止符切分，吸收随后的引号/括号收尾；英文按 `. ! ? …` 切分，
   小数点（`3.14`）和缩写表（`Mr.`、`e.g.`、`i.e.`、`U.S.` 等）不断句，句点后无空白（域名/网址）也不断句。
   句子保留 `start`/`end` 偏移，引用形如 `para-001-sent-001`。
2. **分项打分**：
   - `length`：计量单元（汉字数 + 拉丁词数 + 数字串数）的 Dice 相似度 `2*min/(a+b)`；
   - `evidence`：硬 token（数字串、长度 ≥ 2 的拉丁词）重合 Dice，两端都缺少可比 token 时为 `null`；
   - `semantic`：Qwen3-Embedding 真实余弦相似度，规则降级下为 `null`。
3. **置信度**：只对实际可用分项按权重重新归一（默认语义 0.65 / 长度 0.25 / 证据 0.10），
   因此规则降级下的 `confidence` 是规则置信度而非语义分。
4. **段落内单调 DP**：动作含 `1-1`、`1-2`、`2-1`、`skip-source`、`skip-target`；
   仅当句对两端序号差 ≤ `window`（默认 5）时参与评分，窗口外只能跳过；`skip_penalty` 为跳过代价。
5. **判定**：最优与次优配对总分差 ≤ `ambiguity_margin` → `conflict_multi_candidate`；
   否则 `confidence < min_confidence` → `low_confidence`（`low_semantic` 或 `length_ratio_abnormal`）；
   其余进入通过集。跳过的句子写 `unaligned_source` / `unaligned_target`。
6. **低置信去向（`low_confidence_policy`，默认 `mark_and_keep`）**：低置信/多候选句对仍写入通过输出，
   标 `review_status=needs_review`、`low_confidence_reason`、`flagged_by=09`，异常清单同时保留审计条目；
   `drop` 恢复旧行为（只进异常清单）。上游段落关系为 `needs_review` 时，句子再高分也标
   `low_confidence_reason=upstream_needs_review`、`flagged_by=08`。依据：句子对齐不是内容终裁者，
   整段低置信时若在这里删记录，下游 10/12/11 永远看不到该段落，缺陷在链路上不可见。
7. **文本证据门禁（2026-11-05）**：语义分和长度比都对「两条句子说的是同一件事、但句子本身不完整」不敏感，
   这里补两条确定性的文本证据，命中后把置信度压到门禁上限（默认 `0.45`，原分留在
   `metadata.confidence_uncapped`）、标 `review_status=needs_review`、同时写异常清单审计条目，
   **不删句对**：
   - `residual_note`（`residual_note_patterns` / `residual_note_policy` / `residual_note_confidence_cap`）：
     译文或原文里残留「后半句缺失」「未翻译」「[sic]」这类编辑注记。注记很短，语义分照旧很高，
     但注记本身就是「这一对不完整」的直接证据。默认 `mark_and_keep`，可配 `drop` 或 `ignore`。
   - `boundary_gate`（`boundary_*` 系列配置）：`boundary_mismatch` = 一侧以续接标点（逗号/冒号/
     破折号/左引号）结尾而另一侧不是，说明双语引语或列举跨了句界；`unclosed_quote` = 引号/括号
     不配对且没有句末终止符，说明句子在中途被截断。
   两者只在没有更高优先级的歧义判定时才改 `low_confidence_reason`；歧义 > 残留注记 > 句界 > 低置信。
   误报实测（按算子实际输出统计）：`residual_note` 默认正则集在 3002 条自然句上 0 命中；
   `boundary_gate` 在 flores 543 条 0 条、holdout 399 条 1 条（0.25%）、粤语会话集 391 条 7 条
   （1.79%，逐条核对都是引语跨句界的真错误对）。

## 模型后端与降级

| 配置 | 行为 |
| --- | --- |
| `model_backend: qwen3_embedding` | 强制本地 Qwen3-Embedding 后端；依赖或权重不可用即 `failed`，不静默降级 |
| `model_backend: labse` | **旧配置兼容别名**，解析后等价于 `qwen3_embedding`（不指向 LaBSE 权重） |
| `model_backend: auto` + `allow_degraded: true` | 模型可用则 `backend=qwen3_embedding`；否则 `backend=rule_degraded`、状态 `degraded`、语义分 `null` |
| `model_backend: auto` + `allow_degraded: false` | 模型不可用即 `failed`（`embedding_unavailable`） |
| `model_backend: rule_degraded` | 显式规则后端，状态 `degraded`，语义分恒为 `null`（调试/离线基线用） |

- 权重来源：本地 `../models/Qwen3-Embedding-0.6B`（1024 维），`sentence-transformers` 加载并强制
  `local_files_only=True`，不联网下载、不连远程模型服务。**权重下载不是测试前提**，离线环境会走降级。
- 资源：默认 `device: cpu`（`auto` 时检测 CUDA），`batch_size` 可调；CPU 亦可运行，吞吐取决于句对数量。
- 模型加载/编码失败会被识别为 `BackendUnavailable`：单条段落的失败写入异常清单 `model_error` 后继续，
  累计超过 `max_model_errors` 则整次执行 `failed`，避免坏后端刷出大量无意义产物。

## 三接口

| 接口 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | 上传输入 JSONL 到 `OUTPUT_DIR/<uniqueId>/input.jsonl` |
| `POST /predict` | 请求体 `{uniqueId, operatorId?, isLastOperator, inputFile, params?, config?}`；`inputFile` 取算子9 中间响应的 `data.outputFile` |
| `POST /removeFile?uniqueId=` | 删除任务目录 |

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
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**输出文件内容**（UTF-8 文本）。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_10_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`；
  `degraded` 状态同样生成阶段产物，`failed` 状态不生成。
- `storageUri`：配置 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- `operatorId` 选传（若传须为 `"10"`，否则 `code=1`）；`params`（或兼容旧参数 `config`）覆盖配置，`params` 优先。
- `uniqueId` 与输入路径均做目录穿越校验：拒绝 `..`、路径分隔符、绝对路径和任务目录外文件。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.9 小节的 `param_` 行与
`configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_长度比区间` | 无对应字段 | 规范默认 0.2–5.0；当前版本未提供可配置长度比区间，仅在低置信且无语义分的条目上归类 `length_ratio_abnormal`，审核以异常清单为准 |
| `param_词汇重叠度阈值` | `evidence_weight` + `min_confidence` | 硬 token 重叠度作为打分项（权重 `evidence_weight`）参与加权；两端无可比 token 时该项为 `null`，最终由 `min_confidence` 判定 |
| `param_语义相似度阈值` | `min_confidence` | 语义分低于阈值归为 `low_semantic`；模型固定本地 Qwen3-Embedding-0.6B（`model_name: ../models/Qwen3-Embedding-0.6B`），不连远程模型服务 |

## CLI

```bash
# 离线/无权重环境用显式降级配置验证阶段产物
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke \
  --config output_smoke/config_degraded.yaml
```

可选参数：`--config`（配置文件路径）、`--output-root`（产物根目录）。默认 `model_backend: qwen3_embedding`
需要本机 Qwen3-Embedding-0.6B 权重；无权重时请复制 `configs/config.yaml` 并改 `model_backend: rule_degraded`
（离线验证即用此方式，未伪造任何语义分数）。

## 验证（2026-10-31 Qwen3-Embedding 改造后实跑）

```text
python -m pytest tests -q --ignore=tests/test_qwen3_real_smoke.py   → 39 passed（2026-11-05 mark_and_keep 后）
python -m compileall -q .        → exit 0
python -m json.tool map.json     → exit 0
```

CLI 冒烟（`model_backend: rule_degraded`，本机不加载模型权重）：

```text
status=degraded statistics={'total': 4, 'pairs': 5, 'rejected': 0, 'degraded': 1,
'backend': 'rule_degraded', 'semantic': null, 'action:1-1': 3, 'action:2-1': 1, 'action:1-2': 1, ...}
stageZip=.../output_smoke/smoke_cli/artifacts/stage_10_smoke_cli.zip
storageUri=local-mirror://smoke_cli/stage_10_smoke_cli.zip
任务目录无 lineage.json，zip 内为 ['10_sentence_pairs.jsonl', '10_sentence_rejects.jsonl', 'run.log']
```

## 已知限制与待确认口径

1. 真实模型验证由 `tests/test_qwen3_real_smoke.py` 覆盖（CPU、`local_files_only`，权重缺失时自动跳过）：
   维度 1024、`model_version` 含 `Qwen3-Embedding-0.6B`、正样本余弦高于负样本；其余单测用替身编码器，
   不加载真实权重。Docker build 未在本机执行。
2. 英文句点判定刻意不采用"后接小写字母不断句"的保守口径（会把 `tax. then he left.` 这类合法句末误判为句内），
   改用"缩写表 + 小数点 + 后接空白/收尾符"口径；该取舍已在单测中固定。
3. 长度分使用计量单元 Dice 而非 `min/max`；口径与阈值（`min_confidence=0.55`）为工程默认值，需业务复核。
4. `1-2`/`2-1` 合并句的语义分取合并文本的向量余弦（合并文本按空格拼接，不改写内容）。
5. `paragraph_count_mismatch` 只做诊断计数，不自动拒绝段落。
6. 状态取值使用 `success/partial/degraded/failed`；低置信条目默认同时进入通过输出（带复核标记）
   和异常清单，只有 `low_confidence_policy: drop` 时才只进异常清单。
7. 规范 4.9 的 `param_长度比区间` 当前无对应配置项（见上表）；`param_语义相似度阈值` 落在 `min_confidence` 上，
   语义与规则置信度共用该阈值。
8. 日志不输出完整原文，异常清单为可回溯保留了句级文本；更严格的脱敏口径需另行确认。
9. 正式接口不提供 `/downloadFile`；不再产出或返回任何血缘文件。
