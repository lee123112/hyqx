# 算子 16 数据 Schema（v1.2.0：领域相关性校验）

> 2026-11-03 起默认走 `relevance_mode: keyword_relevance`：按任务关键词算句对相关性分，
> 与 `relevance_threshold`（默认 0.60）比较，低相关进抽检台。原有固定领域分类保留为
> `relevance_mode: legacy_domain` 回滚口径，输出字段保持兼容（见 §3.2）。

## 1. 输入

输入是算子 15 产生的 UTF-8 JSONL（`15_fluency_checked.jsonl`），每个非空行是一个 JSON 对象。
必填句对字段为 `source` 与 `target`；语言方向取自 `language_pair`（缺省时回退 `source_lang` + `target_lang`）；`metadata` 可选，若存在必须是对象。

```json
{
  "id": "domain-0001",
  "source": "患者出现心脏不适，需要临床诊断。",
  "target": "The patient has cardiac discomfort and needs a clinical diagnosis.",
  "language_pair": "zh-en",
  "metadata": {}
}
```

`language_pair` 支持 `zh-en` / `zh_en` / `zh->en` / `zh→en` / `zh-cn-en-us` 等写法，统一归一为 `zh-en`。

## 2. 处理逻辑

1. 结构校验：`source`、`target` 必须存在、为非空字符串，`metadata` 若存在必须是对象。
2. 语言对校验：`language_pair`（或 `source_lang`+`target_lang`）必须等于任务配置方向（默认 `zh-en`）。
3. 领域相关性判定（`relevance_mode: keyword_relevance`，默认）：
   - 关键词来自 `relevance_keywords`（输入框原样字符串，`；` 分隔；工程侧同时容忍 `;` 与换行），
     解析为列表后去重；超过 `max_relevance_keywords`/`max_keyword_length` 直接报配置错误。
   - 模型后端：`score_relevance(source, target, keywords)` 返回
     `score = sigmoid((cos_pos - cos_neg) / T)`，`T = relevance_temperature`（默认 0.05）；
     本地 Qwen3-Embedding-0.6B 真实推理，分数原样转发，绝不改写。
   - 规则降级后端（模型不可用且允许降级）：只做**真实子串命中**，输出 `matched_keywords`
     （`cosine=null`），`relevance_score`/`domain_confidence` 恒为 `null`，绝不伪造分数。
   - 关键词列表为空：不调用模型；`empty_keyword_policy=manual_review`（默认）全部进抽检台，
     `pass_all` 才整库放行。
4. 判定与去向（只影响状态与标记，**低相关默认不物理拒收**）：
   - `relevance_score >= relevance_threshold`（含等号）→ 相关，`domain_status=accepted`；
   - 低于阈值 → `domain_status=low_confidence`、`relevance_status=low_relevance`、
     `trigger_reason=low_relevance`、`review_requirement=mandatory`，保留在主输出并进 20 号抽检台；
   - `low_relevance_action=reject` 时才物理拒收（只进异常清单，审计 `low_relevance`），`keep` 则标记后放行；
   - 规则降级 → `rule_degraded`；模型单条异常 → `model_error`；两者都待复核。
   - `legacy_domain` 回滚模式：仍按 `allowed_domains` + `domain_confidence_threshold`（0.80）判定，
     `out_of_scope` 物理拒收（v1.1.0 口径）。
5. 输出产物：原子替换写盘（先写 `.tmp` 再 `os.replace`），严格 UTF-8。

`metadata` 会镜像写入 `domain`、`subdomain`、`domain_confidence`、`domain_status`、`domain_backend`、`model_version`、`review_status`、`allowed_domains`，并为下游算子 18 追加 `relevance_score`、`relevance_status`、`relevance_threshold`、`relevance_mode`。

## 3. 输出字段

通过记录（`16_domain_checked.jsonl`）在原有字段基础上追加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `domain` | string \| null | 主领域标签；`keyword_relevance` 模式下为匹配度最高的关键词，低相关/未知为 `low_relevance`/`unknown` |
| `subdomain` | string \| null | 子领域标签（仅 legacy 规则后端） |
| `domain_confidence` | number \| null | 兼容字段：等于 `relevance_score`（legacy 模式为领域置信度）；规则降级恒为 `null` |
| `domain_status` | string | `accepted` / `low_confidence` / `out_of_scope` / `rule_degraded` / `unknown_domain` / `model_error`（兼容旧口径） |
| `backend` | string | `model` 或 `rule_degraded` |
| `model_version` | string \| null | 模型版本；规则降级为 `null` |
| `evidence` | array | 证据列表：相关性模式为 `{type:"keyword_relevance_decision", method, cos_pos, cos_neg, temperature, calibration, negative_sample_sha256, …}`；规则为 `{type:"keyword", …}` |
| `candidates` | array | 候选领域：`{domain, subdomain, confidence}`；多标签时长度 > 1（legacy） |
| `allowed_domains` | array | 本次生效的允许领域集合（legacy） |
| `domain_reasons` | array | 完整原因列表，主因在首位 |
| `domain_threshold` | number | 兼容字段：等于 `relevance_threshold`（legacy 模式为置信度阈值） |
| `review_status` | string | `accepted` / `waiting_manual` / `rejected` |
| `language_pair` | string \| null | 归一后的语言方向 |
| `domain_checked_at` | string | 判定时间（ISO8601，+08:00） |

v1.2.0 新增字段（`keyword_relevance` 模式写入，`legacy_domain` 模式为 `null`/不写）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `relevance_mode` | string | `keyword_relevance` / `legacy_domain` |
| `relevance_status` | string | `relevant` / `low_relevance` / `no_keywords` / `disabled` / `rule_degraded` / `model_error` |
| `relevance_score` | number \| null | `sigmoid((cos_pos - cos_neg)/T)`，0–1；**不是概率**；规则降级/空词表为 `null` |
| `relevance_threshold` | number | 本次生效的相关性阈值（默认 0.60） |
| `relevance_temperature` | number | 打分温度（默认 0.05） |
| `relevance_aggregate` | string | `max` / `top3_mean` |
| `relevance_score_type` | string | `logistic_cos_diff` |
| `relevance_calibration` | string | `no_human_relevance_gold`（无人工 Gold） |
| `relevance_backend` / `relevance_model_version` | string \| null | 打分后端与版本 |
| `relevance_keywords_count` / `relevance_keywords_sha256` | number / string | 关键词数量与指纹（不落原文特判，仅可复算） |
| `matched_keywords` | array | 前 3 个最近关键词 `{keyword, cosine}`；规则降级时 `cosine=null` |
| `cos_pos` / `cos_neg` | number \| null | 关键词最大余弦 / 通用文本负样本最大余弦 |
| `trigger_reason` | string \| null | 进抽检台的原因，相关性低为 `low_relevance`（20 号按其入队） |
| `review_requirement` | string | `mandatory`（低相关/降级/模型异常）或 `optional` |
| `delivery_blocking` | boolean | `true` = 未复核不得交付（对应 `domain_status != accepted`） |

`domain_status` 与 `review_status` 的对应关系：

| domain_status | relevance_status | review_status | 去向 |
|---|---|---|---|
| `accepted` | `relevant` / `disabled` | `accepted` | 仅主输出 |
| `low_confidence` | `low_relevance` | `waiting_manual` | 主输出（标记）+ 异常清单审计 → 20 号抽检台 |
| `low_confidence` | `low_relevance`（`low_relevance_action=reject`） | `rejected` | 仅异常清单 |
| `rule_degraded` | `rule_degraded` | `waiting_manual` | 主输出（标记） |
| `unknown_domain` | `no_keywords` | `waiting_manual` | 主输出（标记） |
| `model_error` | `model_error` | `waiting_manual` | 主输出（标记）+ 异常清单审计 |
| `out_of_scope` | （legacy） | `rejected` | 仅异常清单 |

## 4. 异常清单（`16_rejects.jsonl`）

每条为 `{line, id, language_pair, reasons, domain?, domain_status?, domain_confidence?, backend?, relevance_score?, relevance_status?, relevance_threshold?, matched_keywords?}`。
不落盘完整原文。`reasons` 取值：

- 结构类：`invalid_json`、`invalid_record`、`missing_field:source|target`、`invalid_type:source|target`、`empty_source|target`、`invalid_type:metadata`、`missing_language_pair`、`language_pair_mismatch`；
- 筛选/审计类：`low_relevance`（低于相关性阈值时的审计记录，默认记录仍在主输出）、`out_of_scope`（legacy）、`low_confidence`、`model_error`。

`rule_degraded` 与 `unknown_domain` 只保留在主输出，不写异常清单（技术设计 §4 规定异常清单只包含范围外、低置信与 schema_error）。

> 口径提示（必须标记的差异）：根目录 `demo/DATA_SCHEMA.md` 的正式输出前缀表把本节点异常清单写作 `16_domain_rejects.jsonl`，而本算子目录的 `DEVELOPMENT_PLAN.md` 与 `TECHNICAL_DESIGN.md` 均写作 `16_rejects.jsonl`。本实现按算子目录计划使用 `16_rejects.jsonl`（与算子 12/13/14/15 的先例一致），差异已在 `DEVELOPMENT_STATUS.md` 记录，未静默改动共享 schema。

## 5. 配置

```yaml
input_encodings: [utf-8]
input_file_name: 15_fluency_checked.jsonl
text_fields: [source, target]
source_lang: zh
target_lang: en
language_check: true
backend: model                   # auto | model | rule_degraded
model_service_url: null
model_path: ../models/Qwen3-Embedding-0.6B/domain_adapter.py
model_version: Qwen3-Embedding-0.6B-zero-shot-prototype-v1.0
model_entrypoint: classify_domain
relevance_entrypoint: score_relevance
fallback_policy: fail            # rule_degraded | fail
max_model_errors: 20
# ---- 关键词相关性（v1.2.0 默认）----
relevance_mode: keyword_relevance   # keyword_relevance | legacy_domain
relevance_keywords: "医疗；患者；诊断；…；medical；patient"   # 「；」分隔，容忍 ; 与换行
relevance_threshold: 0.60           # score >= threshold 判为相关
relevance_temperature: 0.05
relevance_aggregate: max            # max | top3_mean
low_relevance_action: manual_review # manual_review | reject | keep
empty_keyword_policy: manual_review # manual_review | pass_all
max_relevance_keywords: 200
max_keyword_length: 64
# ---- 以下仅 relevance_mode: legacy_domain 生效 ----
allowed_domains: [medical, legal, finance, technology, education]
allow_all_domains: false
domain_confidence_threshold: 0.80
allow_multi_label: false
max_candidates: 5
domain_keywords: {...}
subdomain_keywords: {...}
domain_aliases: {...}
low_confidence_policy: mark_and_keep   # mark_and_keep | move_to_rejects
degraded_policy: mark_and_keep
```
