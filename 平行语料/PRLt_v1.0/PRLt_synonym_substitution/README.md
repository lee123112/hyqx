# PRLt_synonym_substitution（算子22）

使用通用同义词库进行单词/短语替换，每条最多一处。禁止领域词库和整句重写；输出记录替换字段、原词、替换词、匹配方式（`match_mode`）和词库版本。候选必须回流 14–18。

**当前默认词库（2026-10-21 起，临时采用）**：`cilin-v3-m75-curated`（1100 条；哈工大同义词词林
扩展版 V3，8 位编码 `=` 同义组 + 词向量 sim≥0.75 选词，人工剔除 2 条语境不可接受词对）。来源/许可/
筛选/回滚记录见 [`configs/DICTIONARY_PROVENANCE.json`](configs/DICTIONARY_PROVENANCE.json)；旧
`general-v1`（10 条人工词表）备份在 `configs/general_synonyms-v1.json`。**词林数据商用许可尚未确认，
交付前需业务/法务确认。**

**匹配口径（v1.0.1 起）**：英文等拉丁字母词条按**词边界**匹配（`\b`），不命中词内子串——`start` 不会把 `started` 改成 `begined`，`help` 不会命中 `helpful/helpdesk`；大小写敏感，不做词形推演。中文等无词边界词条仍按子串匹配。

> 【无血缘改造 2026-10-19】本算子不再生成/读取/返回任何血缘文件。接口由
> `common/augmentation_api.create_app` 统一提供；成功响应 `data` 固定为
> `outputFile` / `rejectFile` / `stageZip` / `storageUri` 四字段。

- 算子编号：22，包名 `PRLt_synonym_substitution`，端口 **8022**
- 上游默认输入：`17_entity_checked.jsonl`（见 `map.json`），可选算子（`optional: true`）
- 回流：`rejoin: ["14", "15", "16", "17", "18"]`
- 入口：`inference:app`

## 1. 处理边界

**做什么**

1. **通用同义词替换**：按 `configs/general_synonyms.json`（`cilin-v3-m75-curated`，1100 条；
   2026-10-21 临时采用词林 V3 精筛版）在 `source`/`target` 中命中替换，每条记录最多替换 1 处。
2. **候选记录**：新 `id`（`<parent_id>__aug22__NNNN`）、`parent_pair_id`、
   `augmentation_operator=PRLt_synonym_substitution`、`augmentation_type=synonym_substitution`、
   `augmentation_status=pending_qc`、`delivery_eligible=false`；`metadata.generation_evidence`
   记录 `dictionary_version`、`replacement_field`、`original`、`replacement`、`match_mode`
   （`word_boundary` / `substring`）。
3. **异常清单**：未命中词库、缺 `source`/`target` 等进入 `22_rejects.jsonl`，不静默丢弃。

**不做什么**

- 不调用模型；不做词形推演（`start` 不会变成 `started` 的匹配）。
- 不允许领域词库（`allow_domain_dictionary=true` 直接配置错误）、不做整句重写。
- 每条最多替换 1 处；不覆盖原始数据；不生成/不返回任何血缘文件。

## 2. 输入输出

输入：严格 UTF-8 JSONL，每行一个 dict（默认 `17_entity_checked.jsonl`）；阶段内实际路径由
`/predict` 的 `inputFile` 指定。必填 `id`、`source`、`target`。

输出（`OUTPUT_DIR/<uniqueId>/`）：

| 产物 | 说明 |
| --- | --- |
| `22_synonym_substituted.jsonl` | 同义替换候选（新增句对，不覆盖原数据） |
| `22_rejects.jsonl` | 异常清单：`{line, id, reasons[], augmentation_operator, augmentation_type, delivery_eligible:false}` |
| `artifacts/stage_22_<uniqueId>.zip` | 阶段产物：主输出 + 异常清单（`run.log` 存在时一并收录；2026-10-19 起取代 `lineage.json`） |

输出记录主要字段：`id`、`parent_pair_id`、`source`、`target`、`source_lang`、`target_lang`、
`augmentation_operator`、`augmentation_type`、`augmentation_status`、`human_review_status`、
`delivery_eligible`、`metadata`（含 `config_hash`、`generation_evidence`）。

## 3. 接口

外层响应固定 `{code, msg, data}`；正式契约仅三个接口，**不提供** `/downloadFile`。接口由
[`common/augmentation_api.py`](../common/augmentation_api.py) 的 `create_app` 统一实现。

| 能力 | 接口 |
| --- | --- |
| 上传 | `POST /uploadFile?uniqueId=`（返回 `data.inputFile`） |
| 调用 | `POST /predict` |
| 删除 | `POST /removeFile?uniqueId=` |

`/predict` 请求体：`uniqueId` 必传；`isLastOperator` 默认 `false`；`inputFile` 选传（缺省取任务
`input/` 下最后一个 `.jsonl`）；`operatorId` 选传（传了必须为 `"22"`）；`params` 覆盖配置，
兼容字段 `config` 同义（`params` 优先）；未知配置键报配置错误。

成功响应 `data` 固定 4 个字段：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "outputFile": "<中间算子=输出文件路径；阶段末=输出文件内容>",
    "rejectFile": "<异常清单路径>",
    "stageZip": "<阶段产物 zip 路径>",
    "storageUri": "<file:// 或 local-mirror:// 地址>"
  }
}
```

- 中间调用（`isLastOperator=false`）：`outputFile` 为 `OUTPUT_DIR/<uniqueId>/22_synonym_substituted.jsonl` 路径；
- 阶段末调用（`isLastOperator=true`）：`outputFile` 为该文件的 UTF-8 文本内容；
- 失败返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物。

## 4. 阶段产物与无血缘

阶段结束前，`common/augmentation_runtime.run_records(..., operator_id="22")` 调用
[`common/artifacts.py`](../common/artifacts.py) 的 `finalize_stage_artifacts` 生成
`artifacts/stage_22_<uniqueId>.zip`，内含主输出与异常清单（任务目录存在 `run.log` 时一并收录）。配置环境变量
`OBJECT_STORE_DIR` 时 `storageUri` 为归档文件 `file://` 地址，否则为
`local-mirror://<uniqueId>/stage_22_<uniqueId>.zip`。

【无血缘改造 2026-10-19】原 `lineage.json`（`operator`/`operator_version`/`uniqueId`/`inputFile`/
`outputFile`/`rejectFile`/`status`/`startedAt`/`finishedAt`/`config`/`statistics`）输出已停用：
任务目录不再产出任何血缘文件，处理器返回 dict 不再含 `lineageFile`；阶段内路径由
`data.outputFile` 接力。

## 5. 配置

默认值以 [`processor.py`](processor.py) 的 `DEFAULT` 为准；`configs/config.yaml` 为同值静态说明
（处理器不自动加载该文件），运行时通过 `/predict` 的 `params`/`config` 覆盖。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `dictionary_path` | `configs/general_synonyms.json` | 通用同义词库（不存在即配置错误）；默认词林 V3 精筛版 |
| `dictionary_version` | `cilin-v3-m75-curated` | 词库版本（写入证据） |
| `language_scope` | `[source, target]` | 只允许含 `source`/`target` |
| `max_replacements_per_record` | `1` | 一期固定为 1，其它值配置错误 |
| `allow_domain_dictionary` | `false` | 一期不支持领域同义词库，`true` 直接配置错误 |

## 6. 词库与匹配口径

- 词库 `configs/general_synonyms.json`：默认 `cilin-v3-m75-curated`，1100 条中文同义替换
  （哈工大同义词词林扩展版 V3 的 `=` 同义组，词向量 sim≥0.75 选词后人工剔除 2 条语境不可接受
  词对；如 `维持→保持`、`实行→实施`、`到底→究竟`）。旧人工词表 `general-v1`（10 条中英词条，
  如 `快速→迅速`、`help→assist`）备份在 `configs/general_synonyms-v1.json`，未默认启用。
- 词林数据许可：包 `cilin==0.0.3` 代码 MIT，词林数据许可 UNKNOWN（研究用途；原版版权
  上海辞书出版社/梅家驹），**商用需授权**；词表 JSON 随算子配置交付前需业务/法务确认。
- 拉丁字母词条：首尾均为字母/数字时编译 `\b...\b` 词边界正则（`match_mode=word_boundary`），
  大小写敏感、不做词形推演；含标点边缘的词条回退子串匹配。
- 中文等无词边界词条：子串匹配（`match_mode=substring`）。
- 不调用模型，无 GPU/权重依赖，CPU 秒级完成。

## 7. 启动与本地验证

```bash
uvicorn inference:app --host 0.0.0.0 --port 8022
# 或
python inference.py
```

`OUTPUT_DIR` 控制任务目录根：API 默认 `PRLt/work/augmentation_outputs`。本包 `processor.py`
只提供 `run()`，无独立 CLI；本地直调示例：

```bash
python -c "import processor; r=processor.run('<seed.jsonl>','smoke22','output_smoke'); print(r['status'], r['stageZip'], r['storageUri'])"
```

## 8. 真实测试与验证

- 契约：根 [`tests/test_augmentation_21_24.py`](../tests/test_augmentation_21_24.py) + 本包
  [`tests/test_operator.py`](tests/test_operator.py) 覆盖：种子 2 条 → 2 条 `pending_qc` 候选、
  `delivery_eligible=false`、id 前缀 `22__`、`stageZip`/`storageUri` 可用、任务目录无 `lineage*`；
  领域词库开关被拒绝；默认词库版本/条数/精筛项校验。
- 2026-10-21 默认词库切换验证（`prlt_test/runs/aug22_cilin_adopt_20261021/`）：24 条样本
  产出 12/24（50.0%），12 处替换逐条复核 0 处不可接受；`/predict` 4 条命中样本四字段契约通过
  （输出 4、拒绝 0）；全链增强演示 4 条样本 0/4 命中（诚实失败）。
- 本算子无模型依赖，无真实推理耗时记录；其余行为由上述契约测试覆盖。

## 9. 已知限制

1. 默认词库为词林 V3 精筛版（`cilin-v3-m75-curated`，1100 条），2026-10-21 临时采用、**待业务
   确认最终词表**；24 条样本覆盖 12/24，覆盖仍取决于语料用词；词林数据商用许可未确认。
2. 每条记录最多替换 1 处、不做词形推演，可能漏掉需形态变化的替换。
3. 未与下游 14–18 做工程侧联调；未做并发/大文件压测。
4. 运行统计只记数量与文件名，不落原文全文。
