# 算子15 译文流畅度检测（translation_fluency）

对目标语言译文做语法、自然度和可读性检测，输出 0–100 流畅度评分、异常类型和过滤结果。

- 输入：`14_semantic_checked.jsonl`（算子 14 语义等价性检测产物）
- 输出：`15_fluency_checked.jsonl`、`15_rejects.jsonl`、`artifacts/stage_15_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）
- 算子 id：`translation_fluency`；服务端口：`8015`
- 模型：**本地 Qwen3.5-2B**（`models/Qwen3.5-2B/adapter.py`，`model_version=Qwen3.5-2B-local-fluency-defect-v3`）；
  历史文档里的 "BITEXT-Qwen2.5 / Qwen2.5-1.5B-Instruct v6" 已停止使用，以本行为准

> 正式接口仅有 `POST /uploadFile`、`POST /predict`、`POST /removeFile`；不提供 `/downloadFile`。

## 1. 目录结构

```text
15_译文流畅度检测/
  processor.py            核心处理逻辑 + CLI
  inference.py            FastAPI 三接口
  configs/config.yaml     默认配置
  utils/io.py             流式 JSONL、任务目录、uniqueId/path 穿越防护
  utils/artifacts.py      阶段产物 zip 打包 + storageUri 对象存储占位
  utils/timeutil.py       时间戳工具（now_iso）
  utils/lineage.py        历史血缘实现（已停用，仅保留便于回溯）
  utils/logger.py         逐行规范日志
  utils/schemas.py        配置契约（pydantic，extra=forbid）
  utils/fluency.py        模型后端适配 + 规则证据
  dataset/sample/demo.jsonl   7 条样例
  dataset/DATA_SCHEMA.md      数据契约
  tests/test_operator.py      处理逻辑测试
  tests/test_api.py           三接口契约测试
  map.json / Dockerfile / requirements.txt / output/
```

## 2. 核心处理逻辑

### 2.1 后端门禁（`utils/fluency.resolve_backend`）

| `backend` | 模型可用 | `fallback_policy` | 结果 |
| --- | --- | --- | --- |
| `rule_degraded` | — | — | 规则降级，`status=degraded` |
| `model` | 是 | — | 模型后端 |
| `model` | 否 | — | 结构化 `failed`（不静默降级） |
| `auto` | 是 | — | 模型后端 |
| `auto` | 否 | `rule_degraded` | 规则降级，`status=degraded` |
| `auto` | 否 | `fail` | 结构化 `failed` |

模型来源：`model_service_url`（HTTP，POST `{task,text,target_lang}` 返回
`{fluency_score, grammar_errors, evidence, model_version}`）或 `model_path`
（目录下的 `adapter.py`，入口函数默认 `score_fluency(text, target_lang)`）。

### 2.2 绝不伪造分数

- `fluency_score` 只来自模型返回值；`_parse_model_payload` 只做类型/范围校验后**原样转发**。
- 模型不可用 → `fluency_score=null`、`fluent=null`、`fluency_status=model_unavailable`（或
  明确允许人工复核时的 `waiting_manual`）；单条推理异常 → `model_error`。
- 规则层（`rule_evidence`）只产出 `fluency_evidence` 证据（空文本、不可见字符、替换字符、
  乱码片段、异常标点、括号/引号不配对、连续重复词、残留源语言字符、过短文本），
  **不产出、不映射任何分数**，也不复制上游 `semantic_score`。

### 2.2.1 模型提示词（rubric-v2，2026-11-04）

本地适配器 `models/Qwen3.5-2B/adapter.py` 的提示词升级为 rubric-v2
（`RUBRIC_VERSION=rubric-v2-qwen3.5-2b-20261104`，`model_version=Qwen3.5-2B-local-fluency-rubric-v2`）：

- **职责限定**：只评译文的语法、语序/搭配、可读性，明确**不评**语义、忠实度、数字、术语、
  长度与复杂度（这些由 14/16 号负责），避免模型把"内容对不对"混进流畅度分；
- **评分维度**：语法 0–40、语序与搭配 0–30、可读性 0–30；先列具体错误（最多 3 条）再按维度扣分，
  找不到错误给 90–100，**明令禁止编造错误来显得严格**；
- **输出约束**：只回一行 JSON，分数必须是 JSON 数字，无错误用 `[]`，JSON 前后不加任何文字；
- **解析容错**：兼容 `score` 别名、字符串数字（`"92"`）；仍无法解析则抛错（不补默认分）。

### 2.2.2 计分口径改用 defect_count_v1（2026-11-06）

rubric-v2 在自然语料上的实测结果推翻了「让模型直接给整体印象分」这条路：

- 942 条自然句只吐出 **16 个不同分值**，71.9% 集中在 95 和 78；
- 被判 <60 的记录里，理由是**事后倒推**的 —— `"Mendoza was gunned down."` 判 20 分、
  理由 `wrong collocation`；`"The find also grants insight into…"` 判 20 分、理由
  `subject-verb disagreement`。98 条人工 Gold 上流畅误伤率 30.3%，ρ(分数, 人工 not_fluent)
  仅 −0.159。

根因是结构性的：**只要输出里有一个「分数」槽位，模型就会先填分数再编理由**，改 rubric 改不掉。
因此默认口径改为 `scoring_mode: defect_count_v1`：

1. **模型不打分**，入口改为 `detect_defects`，只返回 `{"defects":[{type, span, fix}]}`，
   最多 3 条，没错误就返回空列表；
2. **算子核验**（`utils/fluency.verify_defects`）：span 必须在原文逐字出现、fix 与 span 的
   token 编辑距离 ≤2、类型必须在 `scored_defect_types` 白名单、通过该类机械校验；
   不通过的一律降级为 `fluency_suspects`，**不计分**；
3. **确定性计分**（`processor.compute_score`）：
   `score = 100 × max(0, 1 − rate/score_scale)`，`rate = 加权错误数 / max(词数, token_floor) × 100`。

结构保证：**0 条核验缺陷恒等于 100 分**（「没错却低分」不再可能）。
60 分线的含义变成一句人话：每 100 词不超过 5 个加权错误。`score_scale=12.5`、
`token_floor=20` 是先验设定，不随数据回调。

核验层能证明什么、不能证明什么（2026-11-06 实测，见报告「六·补二」）：

- 能证明：主张**形式**成立——span 逐字存在、修正是最小编辑、类型在可计分白名单内、
  通过该类机械校验。形式不成立的编造一律降级为 `fluency_suspects`，不压分。
- **不能证明修正内容是对的。** 20 条探针里 6 条主张通过核验，人工核对后
  **3 条错**（`State → States`、`offset → offsets`、对照句 `State party → State parties`）。
  这类错的共同点是「一步合法的数变形」，机械校验放行，但原文本来就不需要改。
- 因此 `fluency_score` 目前只能当**规则与模型双弱的缺陷计数近似值**，
  不是语法正确率，也不是流畅度排序。span 可核验率实测 0.30（判据 0.80），未达标。

口径切换**不动门禁**：`defect_count_v1` 不是校准概率，不在 `accepted_score_semantics`
白名单内，记录仍为 `waiting_calibration`，仍只作辅助证据（规范 4.15）。
`scoring_mode: rubric_v2` 可回退到旧口径做对比。

完整分析与验收标准见 `_eval/reports/op15_fluency_redesign.md`。

### 2.3 阈值过滤

默认阈值 60：仅当分数语义已校准且校准器获业务批准时，
`score >= 60` → `accepted`/`fluent=true`；`score < 60` → `low_fluency`/`fluent=false`。
未校准或仅用于评价的校准器输出 `waiting_calibration`，不得参与阈值比较。
`low_fluency_policy` 与 `degraded_policy` 默认 `mark_and_keep`（保留在通过集并写审计条目），
可切 `move_to_rejects`（只写异常清单，计入 `moved_to_rejects`）。

#### 2.3.1 规则门禁（2026-11-05）

`waiting_calibration` 只约束**分数**，不约束**结构性硬伤**：`rule_gate: true` 时，
`rule_evidence` 命中的结构性证据（默认 `punctuation_run`，即连续标点串 `text ,, .. ??`）
直接把状态改为 `low_fluency` / `fluent=false`，并写
`review_status=needs_review`、`low_confidence_reason=punctuation_disorder`、`flagged_by=15_rule`；
记录按 `low_fluency_policy` 保留在通过集，异常清单同时留审计条目（计 `rule_gate_flagged_records`）。

- **不改分数**：`fluency_score` 仍是模型原值（无模型时为 `null`），规则结论不写成模型分数；
- **口径**：半角 `, . ! ? ; :` 连排、去空白后 ≥3 个符号且不全为句点。
  `"..."` 省略号、`"?!"`/`"!!"` 两符号组合、`"(ibid., para. 38)"` 缩写标点都不触发；
- **实测**：`fullflow_effect_bench_v1` 的 15 号缺陷（译文末尾注入 `,, .. ??`）55 段中
  45 段在 15 号输出里可直接命中；自然语料 2253 条 0 命中。
- 需要纯观察模式时设 `rule_gate: false`（证据仍写 `fluency_evidence`，只是不触发状态变更）。

### 2.4 工程口径

- 严格 UTF-8 增量解码；输出先写 `.tmp` 再 `os.replace` 原子替换。
- JSONL 流式读写；异常行/审计条目写入独立 `15_rejects.jsonl`；`target_excerpt` 最多 40 字符。
- 任务隔离：输出写入 `OUTPUT_DIR/<uniqueId>/`；`uniqueId` 与下载 `path` 双重穿越校验。
- 状态优先级：`failed` > `partial`（有硬异常或被移出）> `degraded`（有降级/模型错误）> `success`。
- 恒等式：`input_records == passed_records + hard_rejected_records + moved_to_rejects`。
- 批量推理（2026-11-05 核实）：`models/Qwen3.5-2B/adapter.py` 的 `score_fluency_batch` 只在
  `FLUENCY_BATCH_SIZE > 1` **且** `_fast_batched_kernels_available()` 为真时才走批量通路；
  该函数要求 `causal_conv1d` 与 `fla` 两个可选加速包都存在。本机实测两者均未安装
  （`ModuleNotFoundError: No module named 'causal_conv1d'` / `'fla'`），函数返回 False，
  因此实际走的是逐条 `score_fluency` —— 批量参数不会生效，也不会因此报错。
  若日志里出现异常偏高的单条耗时，先确认是不是这条通路被改过，而不是模型本身变慢。

## 3. 接口

外层响应固定 `{code, msg, data}`。

| 接口 | 请求 | 说明 |
| --- | --- | --- |
| `POST /uploadFile?uniqueId=` | multipart/form-data | 上传输入文件 |
| `POST /predict` | JSON：`uniqueId`、`isLastOperator`、`inputFile`；可选 `operatorId`（`"15"`）、`params`/`config` | 中间返回输出文件路径；阶段末返回输出文件内容 |
| `POST /removeFile?uniqueId=` | 查询参数 | 删除当前任务目录 |

成功响应 `data` 固定 4 个字段（不再返回 `lineageFile`）：

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

- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_15_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回
  `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- 不提供 `/downloadFile`；中间产物路径通过 `data.outputFile` 传递；失败返回 `data=null` 且不生成阶段产物。
- `params` 优先于兼容字段 `config`。

### 3.1 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.15 小节的 `param_` 行与
`configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_流畅度阈值` | `fluency_threshold` / `require_calibrated_score` / `require_approved_calibration` | 默认 60 分阈值；未校准分数不得参与阈值门禁 |
| `param_目标语言` | `target_lang` / `source_lang` / `language_check` | 声明目标语言（默认 en）与方向一致性校验 |

## 4. 配置（`configs/config.yaml`）

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `input_encodings` | `[utf-8]` | 严格 UTF-8 |
| `input_file_name` | `14_semantic_checked.jsonl` | 上游默认产物名 |
| `text_field` | `target` | 只读译文 |
| `source_lang` / `target_lang` | `zh` / `en` | 声明语言方向 |
| `language_check` | `true` | 记录声明 `target_lang` 与配置不一致即硬异常 |
| `backend` | `model` | `auto` / `model` / `rule_degraded`（本地 Qwen3.5-2B 适配器） |
| `model_service_url` / `model_path` | `null` / `../models/Qwen3.5-2B/adapter.py` | 模型来源（本算子不调用远程服务，只读本地权重） |
| `model_timeout_seconds` | `900` | HTTP 超时 |
| `model_entrypoint` | `detect_defects` | 本地适配器入口函数（`scoring_mode=rubric_v2` 时改回 `score_fluency`） |
| `scoring_mode` | `defect_count_v1` | 计分口径；`rubric_v2` 为历史口径，仅用于对比/回退 |
| `scored_defect_types` | `[agreement, verb_form, dup_word, punctuation]` | 可进入分数的缺陷类型（须能被机械规则独立确证） |
| `severity_weights` | `{hard: 1.0, minor: 0.4}` | 缺陷严重度权重 |
| `score_scale` / `token_floor` | `12.5` / `20` | 计分常数；先验设定，不随数据回调 |
| `max_defects` / `max_fix_edit_distance` | `5` / `2` | 单条记录最多计分缺陷数；fix 与 span 的 token 编辑距离上限 |
| `fallback_policy` | `fail` | `rule_degraded` / `fail` |
| `max_model_errors` | `20` | 超过则整次 `failed` |
| `fluency_threshold` | `60.0` | 0–100 |
| `require_calibrated_score` | `true` | 未校准分数不得参与阈值门禁 |
| `require_approved_calibration` | `true` | 仅业务批准的校准器可自动放行 |
| `accepted_score_semantics` | 校准概率/既有百分制 | 允许参与阈值比较的分数语义 |
| `score_min` / `score_max` | `0` / `100` | 模型分数合法区间 |
| `low_fluency_policy` | `mark_and_keep` | / `move_to_rejects` |
| `degraded_policy` | `mark_and_keep` | / `move_to_rejects` |
| `allow_degraded_delivery` | `false` | 为 true 时降级标记 `waiting_manual` |
| `rule_gate` | `true` | 结构性硬伤（连续标点串）直接判 `low_fluency`，不改模型分 |
| `rule_gate_kinds` | `[punctuation_run]` | 触发门禁的证据类型白名单 |

## 5. 启动与调用

```bash
pip install -r requirements.txt
uvicorn inference:app --host 0.0.0.0 --port 8015
```

命令行：

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

## 6. 测试与验证（2026-10-19 无血缘改造后真实结果）

```bash
D:\python\python.exe -m pytest tests -q     # 47 passed in 246.74s（2026-11-04 rubric-v2；此前 24 passed in 2.70s）
D:\python\python.exe -m json.tool map.json  # OK（port 8015）
D:\python\python.exe -m compileall -q .     # exit 0
```

CLI 冒烟（真实结果，如实记录）：

- 默认配置（`backend: model` + `fallback_policy: fail`）：
  `processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke`
  → `status=failed`，原因是本地适配器不可用：本算子目录内相对
  `../models/Qwen2.5-1.5B-Instruct/adapter_v6.py` 未挂载（`适配器模型路径不存在：adapter_v6.py`）；
  且交付机 `weights/adapter_v6.py` 依赖 `weights/adapter.py` 与
  `runs/.../fluency_calibration_v2.json`，实测这两个文件缺失，适配器必然加载失败；按
  `fallback_policy=fail` 结构化失败，不伪造分数、不生成阶段产物。开发验证机权重位于
  `G:\HYQX\算子\model`，根 `inference.py` 通过 `MODEL_ROOT` 回退注入 `weights/adapter_v6.py`。
- 离线降级路径（显式 `backend: rule_degraded` + `fallback_policy: rule_degraded`）：

```text
status=partial
input_records=7 passed_records=5 rejected_records=7 hard_rejected_records=2
degraded_records=5 model_unavailable_records=5 threshold=60.0 backend=rule_degraded
reason:model_unavailable=5 reason:empty_target=1 reason:language_declaration_mismatch=1
stageZip=output_smoke\smoke_cli_degraded\artifacts\stage_15_smoke_cli_degraded.zip
storageUri=local-mirror://smoke_cli_degraded/stage_15_smoke_cli_degraded.zip
```

核对：`15_fluency_checked.jsonl` 5 行 == `passed_records`；`15_rejects.jsonl` 7 行 ==
`rejected_records`（2 硬异常 + 5 降级审计）；`artifacts/stage_15_smoke_cli_degraded.zip` 存在且含
主输出、异常清单与 `run.log`；任务目录**无** `lineage.json`；降级记录 `fluency_score=null`，无伪造分数。

三接口 API 冒烟（pytest TestClient）：uploadFile `code=0` → predict(中间) `data` 恰为
`{outputFile,rejectFile,stageZip,storageUri}`、`outputFile` 指向 `15_fluency_checked.jsonl` →
predict(阶段末) `data.outputFile` 为输出内容且 `stageZip` 存在 → `operatorId` 不匹配/非法 `params`
`code=1` → uploadFile(`../escape`) `code=1` → removeFile `code=0` 且任务目录消失；
`GET /downloadFile` 返回 404。

## 7. 已知限制与降级行为

- 历史 BITEXT-Qwen2.5 / Qwen2.5-1.5B-Instruct v6 适配器已不再使用；当前模型为本地
  `models/Qwen3.5-2B/adapter.py`（`Qwen3.5-2B-local-fluency-defect-v3`）。默认
  `backend=model` + `fallback_policy=fail`，模型不可用时结构化失败，不静默降级。
- `rule_degraded` 只给证据不给分；干净句子 `fluency_evidence` 可为空列表（表示未发现规则问题），
  此时无分数、`fluent=null`。
- 规则证据的去向分两路：`punctuation_run` / `abnormal_punctuation` / `repeated_token`
  会作为 `source=rule` 的**已确证缺陷**参与计分（原本就在规则门禁管辖内）；
  其余规则证据（不可见字符、乱码、括号不配对、残留源语言字符）仍只作证据，
  不触发状态变更也不计分 —— 那些属于 2/3 号算子的职责，重复计价会让同一处问题被扣两次。
- 结构性硬伤（`rule_gate_kinds`，默认连续标点串）仍强制判 `low_fluency`，与分数独立。
- **`scored_defect_types` 只含能被机械规则独立确证的类型**（agreement / verb_form /
  dup_word / punctuation）。`collocation`、`word_order`、`article`、`preposition` 等
  即使模型提出来也只作 suspect 证据，**不进分数** —— 无法独立确证的主张不得影响判定。
  代价是这类问题目前测不出来，缺口由 14/16 号与人工复核承担。
- 机械校验只验「主张是否具体且可证伪」（有定位、有最小修正），**不是语法分析器**。
  本环境未安装 spacy / nltk / LanguageTool，`grants → grant` 这类形式合法但判断错误的
  修正仍可能通过。上线前应按 `_eval/reports/op15_fluency_redesign.md` 2.4 节做类型有效性闸。
- 只校验记录的**声明** `target_lang` 与配置方向是否一致，不做真实语言检测（属算子 6 职责）。
- `target_lang` 默认 `en`（本产线为 zh→en，检查译文=英文）；规范文档默认值为 `zh`（面向
  en→zh 产线），两者为同一配置项的不同产线默认，按任务方向配置即可。
- 阈值 60、`score_min/max`、各策略默认值均为 demo 口径，正式取值需任务方确认。
- 阶段产物 zip 由算子侧生成；对象存储仅提供 `OBJECT_STORE_DIR` 本地镜像占位，未接入真实
  OSS/S3 客户端；未在真实容器环境验证。
- 当前三接口、阶段传输和 `removeFile` 按 `uniqueId` 删除均按根规则执行。
- 未运行 Docker build（本机无 docker 命令），Dockerfile 仅静态编写。
- 上游产物名冲突：根 `DATA_SCHEMA.md` 记录算子 14 输出为 `14_sentence_pairs_final.jsonl`，
  而本算子计划写 `14_semantic_checked.jsonl`；阶段内实际路径由 `/predict` 的 `inputFile` 传递，
  默认文件名仅作回退。
