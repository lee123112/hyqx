# 算子 13：S/A/C 对齐质量分级

把算子 12 融合出的对齐置信度映射为 S/A/C 三级质量等级，形成可解释的分级结果、待人工复核清单
与阶段产物，供质量检测节点与数据集交付节点使用。本节点为**纯规则型**，不加载任何模型，只做
「上游分数 → 0-100 质量分 → 等级」的可解释映射，因此不存在伪造模型分数的路径。

- 算子编号：13（多粒度双语对齐处理阶段，必选）
- 类型：规则型（无模型、无外部服务）
- 端口：`8013`
- 映射 id：`alignment_quality_grading`
- 输入：默认 `12_scored.jsonl`
- 输出：`13_graded.jsonl`、`13_rejects.jsonl`、`artifacts/stage_13_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）

## 运行

```bash
pip install -r requirements.txt
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
python inference.py
# 或 uvicorn inference:app --host 0.0.0.0 --port 8013
```

输出根目录默认为当前算子目录下的 `output/`，部署时可通过 `OUTPUT_DIR` 指定。
任务目录为 `OUTPUT_DIR/<uniqueId>/`。

## 接口

本算子实现三个正式接口：`POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`。
不提供 `/downloadFile`。外层响应固定 `{code,msg,data}`（`code` 0=成功，1=失败）。

`/predict` 请求体：`uniqueId`（必传）、`isLastOperator`（strict bool，必传）、`inputFile`（阶段内必传）、
`operatorId`（选传，若传须为 `"13"`）、`params`/`config`（配置覆盖，`params` 优先）。

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
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_13_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回
  `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- 失败返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物。
- 不提供 `/downloadFile`，也不再产出或返回血缘文件。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.13 小节的 `param_` 行与 `configs/config.yaml`
字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_分级阈值` | `grade_s_threshold` / `grade_a_threshold` / `grade_labels` / `grade_status_map` | S/A/C 分级阈值（冻结配置）与等级名、复核状态映射 |

## 分级规则

内部统一使用 0-100 的 `quality_score`；等级边界以 `DEVELOPMENT_PLAN.md` 的「验收」一行为准：

| 等级 | 区间 | 复核状态 | 交付 |
|---|---|---|---|
| `S` | `90 < N <= 100` | `accepted_for_quality_check` | 允许 |
| `A` | `80 <= N <= 90` | `accepted_for_quality_check` | 允许 |
| `C` | `0 <= N < 80` | `waiting_manual` | 拦截（`delivery_eligible=false`） |

- `79.999` 判 C、`80` 判 A、`90` 判 A、`90.001` 判 S；判定不使用 `round`，因此不会改变边界语义。
- 分数来源：优先 `alignment_confidence`；`0<=N<=1` 视为 0-1 比例并 `×100`（记录 `score_scale=ratio_0_1`
  与 `score_conversion_version`），`1<N<=100` 视为百分制；若记录已带 `quality_score`，必须与置信度
  换算值一致（默认容差 `0.01`），不一致判 `score_conflict` 进异常清单。
- 缺分、NaN/Inf、负数、`>100`、类型错误一律进异常清单；不补分、不静默 clamp、不 round。
- C 级默认 `review_status=waiting_manual` 且 `delivery_eligible=false`，不得直接进入交付节点 18。
- 等级名做成配置项 `grade_labels`，甲方「优质/合格/待复核/不入库」口径可改名而不改代码。

## 默认处理行为

- 输入严格按 UTF-8 增量解码；损坏 JSON、非对象行、`metadata` 非对象、缺/空 `source`/`target`、
  声明语言与 `zh → en` 不一致、分数异常全部写入 `13_rejects.jsonl` 并带稳定 `reasons`。
- **只做声明语言一致性校验，不做真实语言检测**（真实语言检测属算子 6 职责）。
- 上游证据原样保留：`alignment_confidence`、`confidence_version`、`component_scores` 等字段不重算、
  不覆盖；上游 `review_status` 与等级状态不同时转存到 `metadata.upstream_review_status` 留痕。
- JSONL 逐行流式读写；输出先写 `*.jsonl.tmp` 再 `os.replace` 原子替换；输出路径与输入路径相同即拒绝执行。
- 任务目录隔离在 `OUTPUT_DIR/<uniqueId>/`；`uniqueId` 做目录穿越校验。正式契约不提供 `/downloadFile`，中间产物路径通过 `data.outputFile` 传递。
- 状态优先级：`failed`（配置/编码/空输入/路径冲突）→ `partial`（存在异常条目）→ `waiting_manual`
  （无异常但存在 C 级）→ `success`。

配置项位于 `configs/config.yaml`，可通过 `/predict` 的 `params`（或兼容旧参数 `config`）对象覆盖，
`params` 优先；未知字段直接拒绝。

## 测试

```bash
python -m pytest tests/ -q
```

实测结果（Windows / Python 3.9.7，2026-10-19 无血缘改造后）：

```
42 passed in 1.64s
```

- `tests/test_operator.py`：38 例——S/A/C 严格边界（含 `79.999/80/90/90.001`）、边界规则串、自定义等级名、
  0-1 量纲换算与显式百分制、仅 `quality_score`、两分一致/不一致（`score_conflict`）、容差可配、
  metadata 兜底可关、缺分、NaN/Inf、越界、bool/字符串非法类型、端到端混合文件、C 级 `waiting_manual`
  与 `delivery_eligible=false`、S/A only → `success`、规则版本与边界串落盘、上游证据与状态保留、
  无模型分数伪造、结构/语言异常、空文件、错误编码、非法配置、倒置阈值、阶段产物 zip 与「无血缘文件」、
  UTF-8、双 uniqueId 隔离、日志无原文、输入路径等于输出路径保护、增量解码逐行。
- `tests/test_api.py`：3 例——三接口契约与 4 字段返回（`outputFile`/`rejectFile`/`stageZip`/`storageUri`）、
  路径安全、`isLastOperator`/`inputFile`/`uniqueId` 校验、`operatorId` 不匹配、`params` 覆盖生效。

## CLI / API smoke（2026-10-19 无血缘改造后实跑）

CLI：

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

实测：

```
status=partial
input_records=7  graded=6  rejected_records=1
S_count=2  A_count=2  C_count=2
waiting_manual_count=2  accepted_for_quality_check_count=4
score_conversion_applied_count=6  min_quality_score=70.0  max_quality_score=95.0
score_source:alignment_confidence=5  score_source:both=1  reason:score_missing=1
encoding=utf-8  grade_rule_version=v1.0.0  grade_boundary_rule=S:(90,100];A:[80,90];C:[0,80)
stageZip=output_smoke\smoke_cli\artifacts\stage_13_smoke_cli.zip
storageUri=local-mirror://smoke_cli/stage_13_smoke_cli.zip
```

核对（均为实测）：`13_graded.jsonl` 6 行 = `graded`，`13_rejects.jsonl` 1 行 = `rejected_records`，
`S+A+C = 6 = graded`；`artifacts/stage_13_smoke_cli.zip` 存在且含主输出、异常清单与 `run.log`；
任务目录**无** `lineage.json`；产物严格 UTF-8 可解码且中文原样；`component_scores`/`alignment_confidence`
未被覆盖；C 级两条 `waiting_manual` + `delivery_eligible=false`；`run.log` 不含原文与堆栈。

其它验证命令：

```bash
D:\python\python.exe -m json.tool map.json     # 通过（port 8013）
D:\python\python.exe -m compileall -q .        # 退出码 0
```

API smoke（pytest TestClient）：upload→predict（中间/阶段末）→removeFile；成功 `data` 键恰为
`{outputFile,rejectFile,stageZip,storageUri}`；`operatorId` 不匹配、非法 `params`、缺 `isLastOperator`
/`inputFile` 均返回结构化失败；`params.grade_labels` 覆盖可在 `quality_grade` 中观察到；
`GET /downloadFile` 返回 404；任务目录无 `lineage*` 文件。

## 已知限制、降级行为与未确认口径

- **无模型、无降级路径**：本节点不加载任何模型，不存在模型不可用降级，也不存在伪造模型分数的代码路径；
  `component_scores` 中的上游分数原样保留，`quality_score` 只是输入分数的量纲换算结果。
- **口径冲突（必须标记，已按验收实现）**：`DEVELOPMENT_PLAN.md` §2 正文写「A 为 80<N<=90、C 为 N<=80」，
  同文件「验收」行写「80 分为 A、90 分为 A」。二者不能同时成立；本实现以**「验收」行**为准，取
  `S=(90,100]`、`A=[80,90]`、`C=[0,80)`（无缝无重叠），并在配置与文档中显式说明。若业务确认应采用
  §2 口径（80 判 C），把 `grade_a_threshold` 判定改为严格大于即可，边界测试需同步修改。
- **任务「三、算子 6 的专项要求」不适用于本算子**：该节（`04_html_cleaned.jsonl`、`06_lang_checked.jsonl`、
  `langdetect`、`detected_*`、`heuristic_degraded`）通篇是算子 6 的内容。按任务开头「处理逻辑、输出文件名
  和配置以当前算子的 DEVELOPMENT_PLAN.md / TECHNICAL_DESIGN.md 为准」，本算子未实现语言检测与 langdetect。
- **接口契约**：当前正式规则固定为三接口，测试已验证 `/downloadFile` 返回 404；中间路径由
  `data.outputFile` 传递，阶段归档由 `data.stageZip`/`data.storageUri` 返回，不再使用血缘文件。
- **S/A/C 与甲方四级口径的关系**：规则文档 §3.5 要求「不要把 S/A/C 当默认最终口径」，而本算子计划明确
  以 S/A/C 为默认。折中做法：等级名做成配置项 `grade_labels`/`grade_status_map`，默认仍为 S/A/C，
  改配置即可切换为「优质/合格/待复核/不入库」，代码不需改动。
- **与算子 20 的回流口径**：本节点不伪造人工结果，C 级只标 `waiting_manual` 并 `delivery_eligible=false`；
  复核回流字段（`manual_review_status`、`quality_score`、`quality_grade`、`review_status`、`reviewer`、
  `reviewed_at`、`review_comment`）见 `dataset/DATA_SCHEMA.md`，正式字段名待工程侧确认。
- **`delivery_eligible` 为本节点新增标记**：计划只要求「C 级不得直接进入 18」，该布尔字段是本实现的落地
  方式（供交付节点 18 直接过滤），非计划原文，需工程侧确认。
- **上游产物的正式字段名**：TECHNICAL_DESIGN 写输入为 `12_scored.jsonl`，上游算子 12 实际输出即该文件名；
  字段读取已做成 `confidence_fields`/`declared_score_fields` 配置项，字段改名不需改代码。
- **阈值与容差为 demo 值**：`grade_s_threshold/grade_a_threshold/score_consistency_tolerance` 可在配置中调整，
  正式阈值需任务方确认，并随统计快照固定。
- **阶段产物**：zip 由算子侧生成；对象存储仅提供 `OBJECT_STORE_DIR` 本地镜像占位，未接入真实 OSS/S3 客户端；
  未在真实容器环境验证。
- 未执行 Docker 构建（本机无 docker 命令）；未做真实上游联调与并发/大文件压测。
