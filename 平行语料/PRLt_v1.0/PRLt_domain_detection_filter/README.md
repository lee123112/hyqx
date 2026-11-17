# 算子 16：领域相关性校验

**v1.2.0（2026-11-03）业务流程变更**：不再把句对归到固定的五个领域，而是**按任务给出的
关键词表判定相关性**，低于阈值的记录**不再物理拒收**，而是标记后进入抽检台。

> 需求来源：需求表第 32 行「领域相关性校验」（客户编号 30 号）：
> ① 领域关键词列表 `[关键词1;关键词2...]`，--1021 输入框按「；」批量录入；
> ② 领域相关性阈值 ≥，默认 60%；③ 低质量进入抽检台：【是】。

- 算子编号：16（双语质量检测过滤阶段，主链 `14 → 15 → 16 → 17`；评估链路称 11 号）
- 类型：模型型（本地 Qwen3-Embedding-0.6B 关键词相关性打分） + 规则降级后端
- 版本：v1.2.0（`relevance_mode: legacy_domain` 可回滚到 v1.1.0 固定领域分类）
- 端口：8016（`map.json`）
- 输出：`16_domain_checked.jsonl`、`16_rejects.jsonl`、`artifacts/stage_16_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）

## 1. 功能边界

负责：

- 校验句对结构与语言方向；
- 解析任务关键词表（「；」分隔，去重、限长、限量，超限报配置错误而不是静默截断）；
- 计算关键词相关性分 `relevance_score`（`logistic_cos_diff`，见 §3.1）；
- 依据 `relevance_threshold`（默认 0.60）判定 `relevant` / `low_relevance`，低相关记录标记
  `review_status=waiting_manual` 并写入抽检台触发字段；
- 输出可复核的相关性统计（分档直方图、主关键词计数）、证据、异常清单、日志与阶段产物 zip。

不负责：

- 语义等价（14）、流畅度（15）、数字与实体一致性（17）；
- 真实语言检测（属算子 6），本节点只校验记录**声明**的语言方向与任务方向是否一致；
- 改写 `source`/`target` 原文（只读判定，不改文本）；
- 人工确认（抽检台 20 号）。

## 2. 输入 / 输出字段

输入：`15_fluency_checked.jsonl`，UTF-8 JSONL。必填 `source`、`target`；语言方向取自 `language_pair`（缺省回退 `source_lang` + `target_lang`）；`metadata` 可选。

输出 `16_domain_checked.jsonl` 追加字段：`domain`、`subdomain`、`domain_confidence`、`domain_status`、`backend`、`model_version`、`evidence`、`candidates`、`allowed_domains`、`domain_reasons`、`domain_threshold`、`review_status`、`language_pair`、`domain_checked_at`，并在 `metadata` 中镜像关键字段。

**v1.2.0 新增相关性字段（语义权威）**：`relevance_mode`、`relevance_status`、`relevance_score`、`relevance_threshold`、`relevance_temperature`、`relevance_aggregate`、`relevance_score_type`、`relevance_calibration`、`relevance_backend`、`relevance_model_version`、`relevance_keywords_count`、`relevance_keywords_sha256`、`matched_keywords`、`cos_pos`、`cos_neg`，以及抽检台入队三件套 `trigger_reason` / `review_requirement` / `delivery_blocking`。

**v1.3.0 新增打分粒度字段（2026-11-04，父句/段落聚合）**：`relevance_scoring_unit`（`parent` / `unit`）、`relevance_group_size`、`relevance_parent_id`、`relevance_child_score`。同一 `parent_pair_id`（或 `paragraph_id`）分组只对拼接后的父级文本打一次分，组内记录继承该分；`relevance_scoring_unit=parent` 表示这条分来自父级而不是子句本身。阈值 `0.60` 不变。

**兼容门禁键（17/18 号零改动）**：`domain_status` 取 `accepted` / `low_confidence` / `unknown_domain` / `rule_degraded` / `model_error`；`domain_confidence` = `relevance_score`；`domain_threshold` = `relevance_threshold`；`domain` = 命中度最高的关键词（低相关记 `low_relevance`，无关键词记 `unknown`）。

记录去向（`relevance_mode: keyword_relevance`，默认）：

| relevance_status | 条件 | review_status | 主输出 | 抽检台 | 可交付 |
|---|---|---|---|---|---|
| `relevant` | `score >= relevance_threshold` | `accepted` | ✔ | — | ✔ |
| `low_relevance` | `score < threshold` | `waiting_manual` | ✔（标记） | ✔ | ✘ |
| `no_keywords` | 关键词为空且 `empty_keyword_policy=manual_review` | `waiting_manual` | ✔（标记） | ✔ | ✘ |
| `disabled` | 关键词为空且 `empty_keyword_policy=pass_all` | `accepted` | ✔ | — | ✔ |
| `rule_degraded` | 无模型，只有词面证据 | `waiting_manual` | ✔（标记） | ✔ | ✘ |
| `model_error` | 单条模型调用异常 | `waiting_manual` | ✔（标记） | ✔ | ✘ |
| 结构 / 语言方向错误 | — | — | ✘ | — | ✘（仅异常清单） |

`legacy_domain` 模式仍保留 v1.1.0 的 `out_of_scope` 物理拒收行为。

异常清单 `16_rejects.jsonl` 每条为 `{line, id, language_pair, reasons, ...}`，不落盘原文；原因码含 `invalid_json`、`invalid_record`、`missing_field:*`、`invalid_type:*`、`empty_source|target`、`missing_language_pair`、`language_pair_mismatch`、`low_relevance`、`no_keywords`、`low_confidence`、`model_error`，以及 legacy 模式的 `out_of_scope`。

## 3. 算法 / 模型后端

### 3.1 关键词相关性打分（v1.2.0 默认）

```
v        = 句对嵌入(source + "\n" + target)              # 1024 维，左填充末位 token 池化
cos_pos  = max_j cos(v, 关键词_j)                        # relevance_aggregate=max；top3_mean 可选
cos_neg  = max_k cos(v, 负样本_k)                        # 10 条内置通用/日常/新闻文本
score    = 1 / (1 + exp(-(cos_pos - cos_neg) / T))       # T=relevance_temperature，默认 0.05
```

- **0.5 = 打平**：分值与"更像关键词主题"还是"更像通用文本"单调相关；`score >= 0.60`
  表示关键词相似度至少比通用文本基线高 `0.05 × ln(1.5) ≈ 0.020`。
- **不是概率**：输出固定带 `relevance_score_type=logistic_cos_diff`、
  `relevance_calibration=no_human_relevance_gold`；60% 是**业务阈值**，不是准确率。
- 证据字段：`matched_keywords`（按余弦排序 top-3）、`cos_pos`、`cos_neg`、`temperature`、
  `negative_sample_sha256`。
- 关键词向量按原样字符串进程内缓存，同一批里重复关键词只嵌入一次。
- 关键词随记录一起不落盘，只写 `relevance_keywords_count` 与 `relevance_keywords_sha256[:16]`。

### 3.2 模型后端接线

- 首选本地 `Qwen3-Embedding-0.6B` 适配器（`models/Qwen3-Embedding-0.6B/domain_adapter.py`，1024 维）：
  - HTTP：`model_service_url`，POST `{task:"domain", source, target, language_pair}`，返回 `{domain, subdomain, domain_confidence, candidates?, evidence?, model_version?}`；
  - 本地：`model_path` 指向适配器文件（或含 `adapter.py` 的目录）。
    - `keyword_relevance` 模式调用 `score_relevance(source, target, keywords, language_pair, temperature, aggregate)`（由配置 `relevance_entrypoint` 指定）；
    - `legacy_domain` 模式调用 `classify_domain(source, target, language_pair)`（由配置 `model_entrypoint` 指定）。
  - 本模块只做**范围校验与转发**：分值/置信度必须为 0–1 数值，否则按 `model_error` 处理，绝不用规则结果顶替。
    HTTP 后端没有相关性入口，`keyword_relevance` 模式下按"后端不可用"处理（由 `fallback_policy` 决定降级或失败）。
  - **分类策略（零样本原型，不是监督分类器）**：对 7 个标签（medical/legal/finance/technology/education/out_of_scope/unknown）各 3 条种子描述做平均嵌入得到原型；句对嵌入（`source\ntarget`，左填充末位 token 池化）与原型求余弦，再经温度 softmax（T=0.03）得到 0–1 真实概率。**不复用**任何 BGE-M3 两阶段逻辑回归系数；旧 `models/bge-domain-prototype` 保持不动但已不被配置引用。
  - 证据如实标注 `method=zero_shot_prototype`、`training_label_quality=no_human_item_level_domain_gold`、`formal_business_accuracy=blocked_no_human_item_level_domain_gold`、`business_gate=waiting_manual`：模型概率只作为筛选证据，业务门禁仍需领域 Gold，不单独放行。
  - 原型缓存：首次调用把原型写入同目录 `domain_prototypes.npz`（含种子指纹；种子变更会自动重建）。
  - **权重位置**：`models/Qwen3-Embedding-0.6B`（包内相对路径 `../models/Qwen3-Embedding-0.6B/domain_adapter.py`）；可用 `QWEN3_EMBEDDING_MODEL_PATH` / `MODEL_ROOT` 覆盖，不硬编码 `G:\` 绝对路径，不联网下载。
- 规则降级后端 `rule_degraded`：只统计**真实词面命中**，`relevance_score` / `domain_confidence` 恒为 `null`。
  - `keyword_relevance` 模式：按任务关键词做子串命中，输出 `matched_keywords`（`cosine=null`）与证据，一律 `rule_degraded` → 待复核；
  - `legacy_domain` 模式：按 `domain_keywords` / `subdomain_keywords` 生成候选领域；命中允许领域 → `rule_degraded`，无命中 → `unknown_domain`，命中但不在允许集合 → `out_of_scope`。

### 3.3 超短碎片守卫（2026-11-05）

源与目标**两侧都**去空白后字符数 ≤ `fragment_min_chars`（默认 4）的记录不进入相关性判定：
直接置 `relevance_status=low_relevance`、`review_status=waiting_manual`、
`trigger_reason=fragment_too_short`，`relevance_score=null`（不伪造分数），并计入
`fragment_flagged_records`。

- 为什么放在判定之前：`银行` / `bank`、「心」/「heart」这类 1–4 字片段的余弦噪声极大
  （历史 rel-027「心」/ heart 得 0.985），交给嵌入模型判相关性只会放大噪声；
  它们本身就不是可用的双语句对。
- 为什么要求两侧都短：只要求任一侧短会在自然语料上命中 39/844（4.6%），
  误伤「增编」/Addendum、「更正」/Corrigendum 这类正常短术语对和上游单侧碎屑；
  两侧都短的口径自然语料 0 命中，构造缺陷 20/20 命中（2026-11-05 实测）。
  单侧碎屑由算子10 的 `fragment_too_short` 标记接力，不在本算子重复拦截。
- 为什么不删：记录仍写入通过输出并带复核标记，由 17 号交付门禁按标记拦截；
  删掉会让缺陷在链路上不可见。
- `fragment_guard: false` 可关闭，`fragment_min_chars` 可调。

### 3.4 文档主题一致性守卫（2026-11-05）

模型分只看「这条单位与关键词有多像」，看不见这份文档整体是不是跑题。跑题文档里
恰好命中关键词的那几条会拿到高分，而它们的上下文是别人的话题——这就是
`keyword_but_off_topic` 这一类：单句层面无解，文档层面才有信号。

口径：主流程逐条判完之后回扫一次输出，按 `id` 的 `::` 前缀分文档：

- 文档内 `relevance_status=low_relevance` 的单位数 ≥ `document_consistency_min_units`（默认 5）
  且占比 ≥ `document_consistency_min_low_ratio`（默认 0.3）时，
  把该文档其余 `relevant` 的单位改判 `relevance_status=manual_review`、
  `review_status=waiting_manual`、`trigger_reason=document_topic_inconsistent`、
  `delivery_blocking=true`，并写 `document_consistency` 证据（文档 id / 单位数 / 低相关数 / 占比）。
- **不重新打分、不改分数、不物理删除**：判定用的是本算子自己已经算出的低相关占比。
- 实测（Fullflow-Effect-Bench-v1 defect-11-013）：跑题歌手段落 0.28、孤立碎片「银行」0.99、
  合同段落 0.96，整篇 0.38——逐条判定里合同段落全放行，回扫后改判待复核。
- 误报实测：109 篇自然文档（flores 50 篇 + holdout 59 篇，共 1058 条）里低相关单位数为 0，
  0 篇命中。
- `document_consistency_guard: false` 可关闭；`min_units` / `min_low_ratio` 可调。

## 4. 降级行为

| 配置 | 模型可用 | 模型不可用 |
|---|---|---|
| `backend=auto` + `fallback_policy=rule_degraded` | `model` | 降级 `rule_degraded`，run 状态 `degraded` |
| `backend=auto` + `fallback_policy=fail` | `model` | 结构化 `failed`，不静默降级 |
| `backend=model` | `model` | 结构化 `failed` |
| `backend=rule_degraded` | —— | 只做规则降级，run 状态 `degraded` |

- 单条模型异常记 `model_error` 并写异常清单；超过 `max_model_errors` 整次 `failed`。
- 降级记录绝不写入任何伪造置信度（`domain_confidence=null`、`model_version=null`）。

### 4.1 `keyword_relevance` 模式下的降级语义（v1.2.0）

| 情况 | 记录状态 | 打分 | 去向 |
|---|---|---|---|
| 模型后端缺少 `score_relevance` 入口 | 后端解析失败 | —— | `backend=model` 或 `fallback_policy=fail` → 整次 `failed`；否则降级为 `rule_degraded` |
| 规则降级（真子串命中关键词） | `rule_degraded` | `relevance_score=null`（不伪造分数） | 写入主输出，`relevance_status=rule_degraded`，`review_requirement=mandatory` |
| 关键词列表为空 + `empty_keyword_policy=manual_review` | `unknown_domain` | 不调用模型 | 全部进抽检台，**绝不静默放行** |
| 关键词列表为空 + `empty_keyword_policy=pass_all` | `accepted` | 不做相关性判定 | 任务显式声明才允许整库放行 |
| 单条模型报错 | `model_error` | `null` | 进抽检台，计入 `relevance_model_error` |

- 降级分支不会因为"缺少分数"而把记录判成相关；`domain_confidence` 在降级时为 `null`，
  下游不允许把它当成 0 或 1 使用。

## 5. 启动

```bash
pip install -r requirements.txt
python inference.py
# 或 uvicorn inference:app --host 0.0.0.0 --port 8016
```

输出根目录默认 `<算子目录>/output/`，部署时用环境变量 `OUTPUT_DIR` 指定；任务目录为 `OUTPUT_DIR/<uniqueId>/`。

## 6. CLI / API 示例

CLI：

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
# status=partial statistics={'input_records': 7, 'passed_records': 4, ...}
# stageZip=.../output_smoke/smoke_cli/artifacts/stage_16_smoke_cli.zip
```

接口（正式三接口，不提供 `/downloadFile`；外层响应固定 `{code,msg,data}`）：

```bash
curl -X POST "http://127.0.0.1:8016/uploadFile?uniqueId=task_1" -F "file=@15_fluency_checked.jsonl"
curl -X POST "http://127.0.0.1:8016/predict" -H "Content-Type: application/json" \
     -d '{"uniqueId":"task_1","operatorId":"16","isLastOperator":false,"inputFile":"input/15_fluency_checked.jsonl",
          "params":{"relevance_keywords":"医疗；法律；合同；法院","relevance_threshold":0.60}}'
curl -X POST "http://127.0.0.1:8016/removeFile?uniqueId=task_1"
```

`/predict` 请求体：`uniqueId`（必传）、`isLastOperator`（必传 strict bool）、`inputFile`（本算子要求传）、
`operatorId`（选传，若传须为 `"16"`）、`params`/`config`（选传对象，`params` 优先，键名为 `configs/config.yaml` 英文字段名）。

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

- 中间调用（`isLastOperator=false`）：`data.outputFile` 是**输出文件路径**，下一算子 `inputFile` 直接用它。
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**输出文件 UTF-8 内容**（不是路径）。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_16_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回
  `local-mirror://<uniqueId>/<zip名>` 逻辑地址。

## 7. 输出 / 阶段产物示例

### 7.1 `keyword_relevance` 模式（v1.2.0 默认）

判为相关（`relevance_score >= 0.60`，`domain` 写入匹配度最高的关键词）：

```json
{"id":"...::clause0003","source":"这位31岁的男子说","target":"said the 31-year-old.",
 "relevance_mode":"keyword_relevance","relevance_status":"relevant","relevance_score":0.6689,
 "relevance_threshold":0.6,"relevance_score_type":"logistic_cos_diff",
 "relevance_calibration":"no_human_relevance_gold",
 "matched_keywords":[{"keyword":"医疗","cosine":0.3321},{"keyword":"patient","cosine":0.3171}],
 "cos_pos":0.3321,"cos_neg":0.2969,
 "domain":"医疗","domain_confidence":0.6689,"domain_threshold":0.6,
 "domain_status":"accepted","trigger_reason":null,
 "review_status":"accepted","review_requirement":"optional","delivery_blocking":false,
 "backend":"model","model_version":"Qwen3-Embedding-0.6B-zero-shot-prototype-v1.0(relevance=logistic_cos_diff)"}
```

判为低相关（进抽检台，`review_status=waiting_manual`、`delivery_blocking=true`）：

```json
{"id":"ntrex-1484-bbc.381780::...clause0001","source":"伦敦德里:小轿车撞进房屋，几名男子被捕",
 "target":"Londonderry: Men arrested after house rammed by car",
 "relevance_mode":"keyword_relevance","relevance_status":"low_relevance","relevance_score":0.0897,
 "relevance_threshold":0.6,"matched_keywords":[{"keyword":"lawsuit","cosine":0.3075}],
 "cos_pos":0.3075,"cos_neg":0.4234,
 "domain":"lawsuit","domain_confidence":0.0897,"domain_threshold":0.6,
 "domain_status":"low_confidence","trigger_reason":"low_relevance",
 "review_status":"waiting_manual","review_requirement":"mandatory","delivery_blocking":true}
```

- `domain_status` 仍取 `accepted | low_confidence | unknown_domain | rule_degraded | model_error`，
  供 17/18 号沿用旧口径；新字段 `relevance_status` 才是本次业务语义（`relevant | low_relevance |
  no_keywords | disabled | rule_degraded | model_error`）。
- 低相关记录**默认保留在主输出**（`low_confidence_policy=mark_and_keep`），不会物理拒收；
  `16_rejects.jsonl` 只收真正的异常/审计记录。

### 7.2 `legacy_domain` 模式（v1.1.0 回滚口径）

`16_domain_checked.jsonl`（规则降级，模型在当前环境不可用）：

```json
{"id":"domain-0001","source":"患者出现心脏不适，需要临床诊断。","target":"The patient has cardiac discomfort...",
 "language_pair":"zh-en","metadata":{"domain":"medical","domain_confidence":null,"domain_status":"rule_degraded","review_status":"waiting_manual"},
 "domain":"medical","subdomain":"cardiology","domain_confidence":null,"domain_status":"rule_degraded",
 "backend":"rule_degraded","model_version":null,
 "evidence":[{"type":"keyword","field":"source","keyword":"心脏","domain":"medical","backend":"rule_degraded"}],
 "candidates":[{"domain":"medical","subdomain":"cardiology","confidence":null,"hits":3,"evidence":[...]}],
 "allowed_domains":["medical","legal","finance","technology","education"],
 "domain_reasons":["rule_degraded"],"domain_threshold":0.8,"review_status":"waiting_manual","domain_checked_at":"2026-10-05T09:35:09+08:00"}
```

阶段产物：`artifacts/stage_16_<uniqueId>.zip`，内含 `16_domain_checked.jsonl`、`16_rejects.jsonl` 与 `run.log`，
与 `storageUri` 一起由 `processor.run()` 返回；`status` 取值 `success | partial | degraded | waiting_manual | failed`。

> 【无血缘改造 2026-10-19】原 `lineage.json`（含 operator / operator_version / uniqueId / inputFile /
> outputFile / status / startedAt / finishedAt / config / statistics）已停用，任务目录不再产出任何 lineage
> 文件，原实现保留在代码注释中便于回溯。阶段内路径传递改用 `data.outputFile`，阶段归档改用
> `data.stageZip` / `data.storageUri`。

### 7.3 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.11 小节（算子 16）的 `param_` 行与
`configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_领域关键词列表` | `relevance_keywords` | 输入框原样的 `关键词1；关键词2；…` 字符串（`--1021` 需求：必须用「；」分隔）；工程侧同时容忍 `;` 与换行 |
| `param_领域相关性阈值` | `relevance_threshold` | 默认 `0.60`；`relevance_score >= threshold` 判为相关（含等号），低于阈值进抽检台 |
| `param_低质量进入抽检台` | `low_relevance_action` | 默认 `manual_review`（保留在主输出并触发 20 号抽检台）；`reject`/`keep` 为可选覆盖 |
| （legacy 回滚）`param_领域置信度阈值` | `domain_confidence_threshold` | 仅 `relevance_mode: legacy_domain` 生效，v1.1.0 口径默认 0.80 |
| （legacy 回滚）`param_目标领域列表` | `allowed_domains` / `allow_all_domains` | 仅 `relevance_mode: legacy_domain` 生效 |


## 8. 资源需求

- CPU：2 vCPU 起步（纯规则降级可在无 GPU 环境运行，demo/CLI 已验证）。
- GPU：本地 Qwen3-Embedding-0.6B 用 CPU 即可（0.6B 参数，首次加载约 5s）；需要吞吐时可用 `QWEN3_DEVICE=cuda` 指定 GPU。使用 HTTP 模型服务时本算子不需要 GPU。
- 内存：≥4GB（流式逐行处理，不整库驻留；Qwen3-Embedding float32 常驻约 2.5GB）。
- 存储：与输入 JSONL 同级，输出体积约为输入的 1.5–2 倍（追加领域元数据与证据）。
- 依赖：`fastapi`、`uvicorn`、`pydantic`、`PyYAML`、`python-multipart`；本地模型后端另需 `torch`、`transformers`、`numpy`（当前验证环境 `_env/prlt_clean` 已安装 torch 2.6.0+cpu / transformers 5.22.1）。

## 9. 真实测试结果

### 9.1 关键词相关性 v1.2.0（2026-11-03）

```text
cd PRLt_domain_detection_filter && ../.venv_prlt/Scripts/python.exe -m pytest tests/ -q
→ 53 passed, 1 skipped（2026-11-05 超短碎片守卫；v1.3.0 父级聚合时为 52 passed, 1 skipped；
   v1.2.0 时为 47 passed, 1 skipped）
```

覆盖点：关键词分隔/去重/上限（`；`/`;`/换行）、阈值含等号、低相关进抽检台、
兼容字段镜像（`domain_status`/`domain_confidence`/`domain_threshold`）、空关键词不放行、
规则降级不伪造分数、`low_relevance_action=reject`、确定性（同输入同分）、
适配器返回体校验、直方图/关键词统计、适配器打分溯源字段、关键词向量缓存。

**样例集**（`dataset/sample/relevance_zh_en.jsonl`，30 条，6 组：显式关键词 9 / 同义改写 7 /
完全无关 8 / 有关键词但跑题 3 / 超短碎片 2 / 边界 1；关键词表 `relevance_keywords.txt` 57 词）：

| 指标 | 结果 |
|---|---|
| 预期高相关被放行 | **16 / 16** |
| 预期低相关被拦下 | **10 / 14** |
| 物理拒收 | 0（全部 `mark_and_keep`，进抽检台） |

4 条漏拦均为已记录的局限类型，不是随机错误：

```text
rel-025 全院职工大会（有关键词 hospital，实际为行政通知）  0.958
rel-026 合同签署仪式（contract 字面命中，非法律实务）      0.930
rel-027 超短碎片「心」/ heart                            0.985
rel-028 profit（边界样本，词表仅覆盖 investment/stock）    0.820
```

**阈值标定**（无人工 Gold，用真实语料分档；844 条 `flow_10`，57 词表，`aggregate=max`）：

```text
score 分位数（T=0.05）：P10 0.257 / P25 0.426 / P50 0.601 / P75 0.752 / P90 0.876
```

| 温度 T | 阈值 0.40 | 阈值 0.50 | **阈值 0.60** | 阈值 0.70 | 阈值 0.80 |
|---|---|---|---|---|---|
| 0.03 | 73.6% | 64.1% | 56.6% | 46.1% | 35.7% |
| **0.05（默认）** | 78.0% | 64.1% | **50.2%** | 35.4% | 18.5% |
| 0.08 | 82.8% | 64.1% | 42.1% | 18.8% | 6.5% |

选取 T=0.05 / 阈值 0.60：0.60 对应 `cos_pos - cos_neg ≈ 0.0203`，即"最相关关键词比通用文本负样本
更接近"这一语义边界；0.5 是并列点，低于 0.5 表示负样本更像。**60% 是业务阈值，不是准确率。**

**链路复跑**（输入 `flow_15.jsonl` 805 条，真实模型后端，config 默认 61 词表）：

```text
11 领域相关性校验  入 805 → 相关 416 / 低相关 389（进抽检台）/ 物理拒收 0
   score_histogram  0.0-0.1:11  0.1-0.2:34  0.2-0.3:59  0.3-0.4:67  0.4-0.5:110
                    0.5-0.6:108 0.6-0.7:112 0.7-0.8:140 0.8-0.9:108 0.9-1.0:56
   top_keyword      contract 63 / legal 38 / court 35 / 法庭 32 / treatment 28
16 数字实体一致性  入 805 → 审计拒收 83
17 数据集打包      入 805 → 通过 346 / 拒收 459
18 质量报告        入 346 → 交付 346，转化率 0.865，留存率 1.0，status=success
```

同一条 805 条输入，**v1.1.0 固定领域口径交付 107 条**（11 号 accepted 130 → 17 号过 107，
转化率 0.2675），**v1.2.0 相关性口径交付 346 条**。差异来自判定语义不同（相关性分 ≥0.60
不等于固定领域置信度 ≥0.80），不是模型换了或阈值放宽；若要更严可上调 `relevance_threshold`，
按上表 0.70 约降到 35%、0.80 约降到 18%。复跑脚本：`_eval/rerun_domain_relevance_chain.py`，
标定脚本：`_eval/eval_domain_relevance.py`，样例集脚本：`_eval/eval_domain_relevance_sample.py`。

### 9.2 正式版（2026-10-31，Qwen3-Embedding 零样本原型）

```text
_env/prlt_clean/Scripts/python.exe -X utf8 -m pytest tests -q
→ 35 passed, 1 skipped in 4.49s
```

- 单元/接口测试默认显式 `backend=rule_degraded`，**不加载** 1.19GB 的 Qwen3-Embedding 权重；
  真实模型用例由 `PRLT_REAL_MODEL_SMOKE=1` 门控（默认 skip，见
  `tests/test_operator.py::test_real_qwen3_six_sample_classification`）。
- 适配器自测（真实推理，串行加载权重）：

```text
python -X utf8 models/Qwen3-Embedding-0.6B/domain_adapter.py
→ 6 条样例 domain 6/6 与期望一致
  domain_confidence 0.9420–0.9916（均超过 0.80 阈值）
  证据 method=zero_shot_prototype / business_gate=waiting_manual /
       legacy_bge_logistic_reused=False
  首次调用（含模型与原型加载）32.07s，单条平均 0.470s（CPU）
  调用方 _parse_model_payload 接受返回值（domain/confidence/candidates）
```

### 9.3 历史结果（2026-10-19 无血缘改造后，规则降级后端）

```text
D:\python\python.exe -m pytest tests -q     →  33 passed in 2.73s（Windows / Python 3.9.7）
D:\python\python.exe -m compileall -q .     →  exit 0
D:\python\python.exe -m json.tool map.json  →  通过（port 8016）
```

CLI 冒烟（规则降级，历史记录）：

```text
D:\python\python.exe processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
→ status=partial
  input_records=7  passed_records=4  rejected=4  audit_records=1  physical_reject_records=3
  accepted=0  out_of_scope=1  low_confidence=0  degraded=4  unknown_domain=1
  waiting_manual=4  multi_label_records=1  backend=rule_degraded  model_version=None
  threshold=0.8  allowed_domains_count=5  encoding=utf-8
  stageZip=output_smoke/smoke_cli/artifacts/stage_16_smoke_cli.zip
  storageUri=local-mirror://smoke_cli/stage_16_smoke_cli.zip
```

核对（实测）：`16_domain_checked.jsonl` 4 行 = `passed_records`；`16_rejects.jsonl` 4 行 = `rejected`
（其中 1 条为审计记录、3 条为物理拒绝）；降级记录 `domain_confidence` 全为 `null`、`model_version` 全为 `null`
（未伪造分数）；阶段 zip 成员为 `16_domain_checked.jsonl`、`16_rejects.jsonl`、`run.log`，
任务目录**无** `lineage.json`；产物按严格 UTF-8 解码通过。

API 冒烟（pytest TestClient 覆盖）：

```text
uploadFile → {"code":0,"msg":"success"}
predict(中间,false) → code 0, data={outputFile,rejectFile,stageZip,storageUri}，outputFile 为存在的路径
predict(阶段末,true) → code 0, data={outputFile,rejectFile,stageZip,storageUri}，outputFile 为文件内容
predict(operatorId=99) → code 1（operatorId 不匹配）
predict(params 非法键) → code 1；predict(合法 params) → code 0
predict(缺 isLastOperator) → code 1；predict(缺 inputFile) → code 1
predict(inputFile=../../configs/config.yaml) → code 1（越界拒绝）
uploadFile/removeFile(uniqueId=../escaped) → code 1
两个 uniqueId 产物与阶段 zip 互不污染；任务目录均无 lineage 文件
```

## 10. 已知限制

- **`relevance_score` 不是概率，也未经人工相关性 Gold 校准**：分值是
  `sigmoid((cos_pos - cos_neg) / T)`，证据标注 `relevance_score_type=logistic_cos_diff`、
  `relevance_calibration=no_human_relevance_gold`。0.60 是业务阈值（对标 `cos_pos - cos_neg ≈ 0.0203`），
  不能当成"准确率 60%"对外表述；阈值/温度只用 30 条样例集与 844 条真实语料分档检查过。
  业务侧若要调整严格度，应按 §9.1 的敏感度表整档调 `relevance_threshold`，并在任务口径内固化为配置快照。
- **关键词表决定上限**：判定只看任务给的关键词。词表缺词 → 相关内容必然低分（如 rel-028 仅给
  `investment`/`stock` 时 `profit` 落 0.82）；词面命中但语义跑题 → 可能误放（rel-025 全院职工大会、
  rel-026 合同签署仪式）。建议任务方按业务主题提供 20–200 个中英词组，并定期用抽检结果回补词表。
- **超短/无主题碎片不稳定**：`心`/`heart` 这类 1–2 词记录余弦噪声大（rel-027 得 0.985），
  建议上游 05 长度过滤保留下限，或对超短记录改用 `relevance_aggregate=top3_mean` 并人工复核。
- **模型不可用 = 全量进抽检台**：`keyword_relevance` 模式要求后端提供 `score_relevance`；
  规则降级只做真实子串命中且 `relevance_score=null`，不会伪造分数，也不会自动放行。
- **旧配置不能直接复用**：v1.1.0 的 `domain_confidence_threshold` / `allowed_domains` 在
  `keyword_relevance` 模式下不生效（仅 `relevance_mode: legacy_domain` 生效）。任务侧需改为传
  `relevance_keywords` + `relevance_threshold`，否则会走 `empty_keyword_policy` 全量进抽检台。
- **抽检台压力**：v1.3.0 父级聚合后，805 条真实输入在阈值 0.60 下产生 **253 条待复核（31.4%）**，
  较 v1.2.0 的逐子句口径（389 条，48%）减少 136 条；其中 UN 官方文件 166 条、NTREX 新闻 87 条
  （新闻侧与 v1.2.0 完全一致，说明聚合没有放松新闻判定）。20 号抽检台的复核人力按 253 条估算；
  阈值敏感度表（§9.1）是**逐子句口径**下测的，聚合后需按新口径重新整档测，不能直接沿用。
- **legacy 零样本原型说明（回滚口径）**：`legacy_domain` 模式下 `domain_confidence` 是"句对嵌入与种子
  原型余弦"的温度 softmax（T=0.03），证据标注 `training_label_quality=no_human_item_level_domain_gold`，
  只在 6 条自测样例 + 5 条边界样例 + demo 上人工检查过，不是准确率声明。
- 旧 `models/bge-domain-prototype`（BGE-M3 两阶段逻辑回归）保持不动但**已不被配置引用**；
  本适配器不复用其系数，也不声称与其等价。模型分支由「由文本真实推导」的测试替身覆盖接线与阈值判定
  （算子代码零硬编码置信度）。
- 规则降级词表（`domain_keywords` / `subdomain_keywords`）与 `allowed_domains` 为 demo 默认值，正式取值需任务方
  提供并固定到本任务配置快照（不再写入血缘 `config`）。
- `allow_multi_label=false` 时只保留主标签；多标签筛选仍以主标签与主置信度为准（技术设计 §1）。
- 只校验记录**声明**语言方向，不做真实语言检测。
- 与根目录共享 schema 的差异：根 `demo/DATA_SCHEMA.md` 将本节点异常清单写作 `16_domain_rejects.jsonl`，本算子目录计划写作 `16_rejects.jsonl`；本实现按算子目录计划（与 12/13/14/15 先例一致），差异已在 `DEVELOPMENT_STATUS.md` 标记。
- 未执行 `docker build`；未做工程侧联调（算子 17/18 实际消费本算子输出）与并发/大文件压测。
- 未在真实对象存储（`OBJECT_STORE_DIR`）环境验证 `storageUri`；未配置时返回 `local-mirror://` 逻辑地址。
