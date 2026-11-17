# 算子 11：子句与短语级对齐（clause_phrase_alignment）

在算子 10 句子级句对内做可解释的子句/短语切分，用双语词典命中、位置一致、长度比和父句
置信度做单调匹配（`1-1` / `1-2` / `2-1` / 跳过），输出最小粒度对齐证据，供算子 12 汇总
统一置信度。模型不可用时只输出 `rule_degraded` 与真实规则证据，`semantic_score` 写 `null`，
绝不伪造模型分数。

- 算子编号：11（多粒度双语对齐处理阶段）
- 版本：v1.0.0
- 输出：`11_clause_pairs.jsonl`、`11_clause_rejects.jsonl`、`artifacts/stage_11_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）

## 职责边界

- 做：子句/短语切分、词典证据、分项打分、父句内单调 DP、置信度与异常归集、阶段产物打包与日志。
- 不做：句子/段落对齐（算子 10）、语言检测（算子 6）、编码归一（算子 2/4）、
  最终置信度治理（算子 12）、语义等价检测（算子 14）。

## 目录

```
processor.py               核心逻辑 + CLI（load_config/resolve_backend/split 调用/run）
inference.py               三接口 FastAPI 服务（端口 8011）
configs/config.yaml        方向、模型后端、窗口、权重、阈值、切分与术语表
utils/io.py                流式 JSONL 读写、任务目录、uniqueId 与 path 穿越防护
utils/artifacts.py         阶段产物 zip 打包与 storageUri 对象存储占位
utils/timeutil.py          时间戳工具（now_iso）
utils/lineage.py           历史血缘实现（已停用，仅保留便于回溯）
utils/logger.py            逐行日志 [时间] [动作] 消息
utils/schemas.py           配置与请求契约（未知字段一律拒绝）
utils/segmenter.py         中英子句切分与 unit_type 标注
utils/phrase_aligner.py    词典证据、分项打分与单调 DP
utils/semantic.py          mDeBERTa-v3-base 语义后端（本地 weights，fast tokenizer；缺依赖/权重即 BackendUnavailable）
tests/test_operator.py     切分、打分、降级、模型注入、异常输入、阶段产物、隔离、UTF-8
tests/test_api.py          三接口、4 字段返回契约、operatorId/params、路径安全、任务隔离
dataset/sample/demo.jsonl  4 条父句样例（覆盖 1-1、1-2、2-1）
dataset/DATA_SCHEMA.md     输入/输出/异常字段说明
```

## 输入输出

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 输入 | `10_sentence_pairs.jsonl`（默认名，阶段内由 `inputFile` 指定） | 句子级句对，见 `dataset/DATA_SCHEMA.md` |
| 通过输出 | `11_clause_pairs.jsonl` | 子句/短语对齐关系 + 父句引用 + 置信度 + 分项分 + 证据 + backend |
| 异常输出 | `11_clause_rejects.jsonl` | 切分失败、词典无证据、冲突、低置信、未对齐、模型不可用及输入格式异常 |
| 阶段产物 | `artifacts/stage_11_<uniqueId>.zip` | 主输出 + 异常清单 + `run.log`；无血缘文件 |
| 日志 | `run.log` | 逐行 `[时间] [动作] 消息`，不写密钥、完整原文和完整堆栈 |

产物目录：`OUTPUT_DIR/<uniqueId>/`（`OUTPUT_DIR` 默认 `./output`，可用环境变量覆盖）。

## 接口（三接口）

外层响应固定 `{code,msg,data}`（`code` 0=成功，1=失败）。上传/删除成功严格返回 `{"code":0,"msg":"success"}`。

| 接口 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | multipart 字段 `file` 写入任务输入目录 `OUTPUT_DIR/<uniqueId>/input/` |
| `POST /predict` | JSON 必传 `uniqueId`、布尔 `isLastOperator` 与阶段内 `inputFile`；`operatorId` 选传（若传须为 `"11"`）；`params`/`config` 覆盖配置，`params` 优先 |
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
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_11_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- 失败（输入缺失 / 配置错误 / 模型不可用且 `fallback_policy=fail`）返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物。
- 不提供 `/downloadFile`，也不再产出或返回血缘文件。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.10 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_句法拆分规则` | `zh_clause_punctuation` / `en_clause_punctuation` / `zh_conjunctions` / `en_conjunctions` / `split_terms` / `min_clause_units` / `max_clauses` | 中英标点、连词与配置词表切分；碎片合并与单侧上限 |
| `param_词典匹配模式` | `terminology` / `require_lexicon_evidence` / `dictionary_weight` | 双语术语表；是否要求词典证据；词典证据权重（默认精准匹配） |

## 对齐口径

1. **切分**：中文按 `，。；：！？、…` 与连词（`并且/而且/但是/然而/因为/所以/如果/虽然/因此/此外`）
   切分；英文按 `, ; : . ! ?` 切分，句点遵守缩写表（`Mr.`、`e.g.`、`i.e.`、`U.S.` 等）
   与小数点规则；配置词表 `split_terms` 命中的边界保留在下一片段；`min_clause_units=2`
   以下的碎片并入相邻片段，单侧最多 `max_clauses=12` 段。
2. **unit_type**：整段命中某个术语词条 → `phrase`；否则为主谓宾核心片段 `clause`。
   `pydantic` 校验后，输出 `metadata.granularity` 固定为 `clause_or_phrase`。
3. **分项打分**（全部可解释、可手工复算）：
   - `dictionary`：源词条译文在目标片段中的覆盖率；源片段无词条命中时为 `null`；
   - `position`：`1 - |pos_src - pos_tgt|`，位置为片段在父句内的归一化中心；
   - `length`：计量单元（汉字数 + 拉丁词数 + 数字串数）Dice `2*min/(a+b)`；
   - `parent`：父句 `metadata.confidence` / `alignment_confidence`；缺失为 `null`；
   - `semantic`：模型真实余弦相似度；规则降级下恒为 `null`。
4. **置信度**：只对实际可用分项按权重重新归一（默认语义 0.45 / 词典 0.20 / 位置 0.10 /
   长度 0.15 / 父句 0.10），因此规则降级下 `confidence` 是规则置信度而非语义分。
5. **父句内单调 DP**：动作含 `1-1`、`1-2`、`2-1`、`skip-source`、`skip-target`；
   仅当片段序号差 ≤ `window`（默认 3）时参与评分，窗口外只能跳过；`skip_penalty` 为跳过代价。
6. **判定**：最优与次优配对总分差 ≤ `ambiguity_margin` → `conflict_multi_candidate`；
   否则 `confidence < min_confidence` → `low_confidence`（`low_semantic` 或
   `length_ratio_abnormal`）；`require_lexicon_evidence=true` 且词典无证据 → `no_evidence`；
   其余进入通过集。跳过的片段写 `unaligned_source` / `unaligned_target`。
   通过集内 `confidence ≥ review_threshold` 标记 `review_status=auto_pass`，否则 `needs_review`。
7. **碎片 / 跨位门禁（2026-11-04）**：源或目标任一侧去空白后 ≤ `fragment_min_chars`
   （默认 4）→ `fragment_too_short`；`1-2` / `2-1` 多单位对若两侧起始序号差 >
   `cross_position_max_gap`（默认 2）→ `cross_position_mismatch`。命中记录的异常清单条目
   照旧保留；按 `fragment_policy` 决定去向（默认 `mark_and_keep`：仍写入通过输出，
   标 `review_status=needs_review`、`low_confidence_reason=<原因>`、`flagged_by=10`，
   并附 `metadata.gate` 证据；`drop` 恢复旧行为，只进异常清单）。
   交付门禁应在 17 号按标记拦截，而不是在这里让记录凭空消失——
   否则下游 11/12 看不到这些记录，缺陷在链路上不可见。`fragment_gate` /
   `cross_position_gate` 可分别关闭，`1-1` 对不受跨位门禁限制。
8. **重复单位守卫（2026-11-05）**：父句内两两比较子句的相邻二元 shingle 包含度
   （中文按字、英文按词，`min_shingles=3` 过滤短单位噪声），任一侧 ≥
   `repetition_containment`（默认 0.80）即把该父句全部子句对标 `needs_review`、
   `low_confidence_reason=duplicate_unit`、`flagged_by=10`，附 `metadata.repetition`
   证据（两侧原文与引用）。不删记录；`repetition_guard: false` 可关闭。
9. **上游复核标记接力（2026-11-05）**：父句 `metadata.review_status=needs_review` 时，
   本算子通过集内的子句对也标 `needs_review`、`low_confidence_reason=upstream_needs_review`、
   `flagged_by` 继承上游（如 `08`）。上游的判断不因本算子分数高而被翻案。

## 模型后端与降级

| 配置 | 行为 |
| --- | --- |
| `model_backend: rule_degraded` | 显式规则后端，状态 `degraded`，语义分恒为 `null` |
| `model_backend: model` + `fallback_policy: fail`（当前 `config.yaml` 默认） | 模型不可用即结构化 `failed`，不静默降级 |
| `model_backend: model`/`auto` + `fallback_policy: rule_degraded` | 模型可用则 `backend=model`；不可用则 `backend=rule_degraded`、状态 `degraded`、语义分 `null` |
| `model_backend: auto` | 依赖可用则走模型，否则按 `fallback_policy` 处理 |

- 正式版模型 `microsoft/mdeberta-v3-base`（本地 `models/mdeberta-v3-base`，hidden=768），由
  `transformers` 从 `model_path` 加载；`AutoTokenizer` 固定 `use_fast=True`（中文路径下
  sentencepiece 直接加载 `spm.model` 会失败，模型目录已提供 `tokenizer.json`）。
  语义分为句向量（attention-mask 平均池化）余弦；`model_version` 记录
  `<模型目录>(hidden=768)`、`model_name` 记录配置模型名，证据不再出现 XLM-R。
  **权重下载不是测试前提**，离线环境按上面的策略降级或失败。
- 本算子 `config.yaml` 只写相对 `../models/mdeberta-v3-base`，不硬编码绝对路径。
- 模型加载/编码失败抛 `BackendUnavailable`（错误码 `mdeberta_load_failed` /
  `mdeberta_encode_failed`）：单条记录失败写入异常清单 `model_error` 后继续，
  累计超过 `max_model_errors` 则整次执行 `failed`，避免坏后端刷出大量无意义产物。
- 单元测试默认**不加载** 1.33GB 权重：模型接线由注入向量后端覆盖，需要模型分支的用例
  显式 `model_backend`；真实编码冒烟需 `PRLT_REAL_MODEL_SMOKE=1` 显式开启。

## CLI

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

可选参数：`--config`（配置文件路径）、`--output-root`（产物根目录）。

## 测试

```bash
python -m pytest tests/ -q
# 45 passed, 1 skipped（2026-11-05 重复单位守卫/上游标记接力后；真实 mDeBERTa 编码冒烟需 PRLT_REAL_MODEL_SMOKE=1）
```

覆盖：中英切分、配置词表切分、碎片合并、上限截断、词典证据、分项加权、单调 DP
（1-1/1-2/2-1）、损坏/空输入、错误编码、错误配置、规则降级不伪造分数、注入真实向量
后端的模型路径、禁止降级的结构化失败、任务隔离、UTF-8 输出、阶段产物 zip 与
「无血缘文件」校验、三接口 4 字段契约、`operatorId`/`params` 校验。

## 验证结果（2026-10-19 无血缘改造后实跑）

- `D:\python\python.exe -m pytest tests -q` → `28 passed in 8.67s`
- `D:\python\python.exe -m compileall -q .` → exit 0
- `D:\python\python.exe -m json.tool map.json` → 通过
- CLI（默认配置 + 已挂载 `../models/mdeberta-v3-base` 时）走 `backend=model`；模型目录缺失时
  `mdeberta_load_failed` + `fallback_policy=fail` → 结构化失败，不伪造分数。
  单元测试默认显式 `model_backend: rule_degraded`，不加载 1.33GB 权重。
- 2026-10-31 模型切换后（开发侧串行实跑，详见交付报告）：
  `_env/prlt_clean` + transformers 5.22.1 下 `AutoTokenizer(..., use_fast=True)` 得到
  `DebertaV2Tokenizer(is_fast=True)`，`AutoModel` 加载 `hidden=768`，中英两句前向输出
  `(2, 768)`；mDeBERTa 平均池化余弦在 5 对探针上 match 均值 0.9356 / mismatch 均值 0.9044
  （分离度 +0.031，优于同环境 XLM-R 的 +0.002）。
- CLI（显式 `model_backend: rule_degraded` + `fallback_policy: rule_degraded` 的离线配置）：
  → `status=degraded`，`input_pairs=4`、`input_clauses=10`、`aligned_clauses=4`、`rejected_clauses=0`，
  动作 `1-1`×2、`1-2`×1、`2-1`×1；产出 `artifacts/stage_11_smoke_cli_degraded.zip`
  （内含 `11_clause_pairs.jsonl`、`11_clause_rejects.jsonl`、`run.log`），
  `storageUri=local-mirror://smoke_cli_degraded/stage_11_smoke_cli_degraded.zip`，
  任务目录**无** `lineage.json`。
- API smoke（TestClient）：upload→predict（中间/阶段末）→removeFile；成功 `data` 键恰为
  `{outputFile,rejectFile,stageZip,storageUri}`；`operatorId` 不匹配、非法 `params`、缺失
  `isLastOperator`、缺 `inputFile` 均返回结构化失败；任务目录无 `lineage*` 文件。

## 已知限制与待确认口径

1. 真实 mDeBERTa 编码已验证可用（fast tokenizer、hidden=768、768 维池化），但**没有人工标注
   对齐 Gold**，语义分判别力未做正式评估：mDeBERTa 未微调句向量余弦基线偏高（无关子句对
   cos≈0.90–0.95），开发期 5 对探针上 match 均值 0.9356 vs mismatch 0.9044（+0.031），
   因此语义分主要提供相对排序，最终置信度仍靠词典/位置/长度/父句分项；若要提升判别力，
   需业务方提供对齐标注做微调或在冻结数据上重估权重。Docker build 未执行（离线环境无镜像拉取权限）。
2. 只校验记录的**声明语言**与配置方向是否一致，不做真实语言检测（属算子 6 职责）；
   `zh → en` 为默认方向。
3. 内置术语表为 demo 字段级证据来源；`require_lexicon_evidence` 默认 `false`（宽松），
   开启后词典无证据的片段进异常清单。该默认值需业务复核。
4. 未确认口径：`11_clause_pairs.jsonl` 与算子 12 的正式字段名（当前按 `DEVELOPMENT_PLAN.md` /
   `TECHNICAL_DESIGN.md` 冻结）；`11_` 输出文件名的编号对齐（上游实际产物名为
   `10_sentence_pairs.jsonl`，阶段内由 `inputFile` 传递，不依赖默认名）。
5. 阶段产物 zip 由算子侧生成；对象存储仅提供 `OBJECT_STORE_DIR` 本地镜像占位，未接入真实
   OSS/S3 客户端。
6. 不再使用血缘文件传递阶段中间路径：中间路径由 `data.outputFile` 返回，阶段末由
   `data.outputFile` 返回内容；不再提供 `/downloadFile`。
