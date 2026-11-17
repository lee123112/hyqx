# 算子14：语义等价性检测（semantic_equivalence）

对已通过对齐分级（S/A 或人工确认合格的 C）的双语句对计算语义等价性，识别漏译、错译、
增译和语义偏差，输出可解释的质量筛选结果，供算子 15（译文流畅度）、16（领域识别）使用。

## 1. 处理边界

- **负责**：双语语义等价判定、阈值筛选、漏译/错译/增译/语义偏差标签、规则证据、统计与阶段产物。
- **不负责**：段落/句子/子句对齐（09/10/11）、对齐置信度融合（12）、S/A/C 分级（13）、
  译文流畅度（15）、领域识别（16）、字段完整性（2）、编码归一（3）、HTML 清理（4）、长度过滤（7）。
- **模型边界**：模型后端（本地 Qwen3-Embedding-0.6B，1024 维）只输出双语语义相似度，不负责句法、流畅度或领域分类。
  模型不可用时按 `fallback_policy` 进入 `rule_degraded`（`semantic_score=null`）或 `fail`，
  **绝不用长度、字符重叠或固定常数冒充模型分数**。
- **语言检测**：本节点只把语言方向当作规则证据；记录声明语言与配置方向不一致时拦截。
  真实语言检测属算子6 职责。

## 2. 输入输出

- 输入：`13_graded.jsonl`（阶段内实际路径由 `/predict` 的 `inputFile` 指定）。
- 输出（`OUTPUT_DIR/<uniqueId>/`）：
  - `14_semantic_checked.jsonl`：`accepted`（真实分 ≥ 阈值）+ `waiting_quality`（降级待复核）
    + 判为非等价的记录（默认 `non_equivalent_policy=mark_and_keep`，`quality_status=rejected`、
    `semantic_status=not_equivalent`、`review_status=rejected`、`delivery_eligible=false`，
    留在流里供 15/16/17 看到，交付与否由 17 号门禁决定）
  - `14_rejects.jsonl`：未通过记录（低分、准入失败、结构异常、降级拒收）；`kept_in_output=true`
    的条目是审计留痕，表示该记录仍留在输出文件里、并未删除
  - `artifacts/stage_14_<uniqueId>.zip`：主输出 + 异常清单 + `run.log`（**2026-10-19 起不再输出 `lineage.json`**）
  - `run.log`：逐行日志
- 字段与标签见 `dataset/DATA_SCHEMA.md`。

## 3. 核心处理流程

1. 严格 UTF-8 增量解码，逐行流式解析（损坏行不中断，进异常清单）。
2. 结构/准入校验：`id`、`source`、`target`、`metadata` 类型；等级（`grade`/`quality_grade`，
   顶层或 `metadata`）默认准入 `S`/`A`/`C`（C 级是否交付由 17 号 `grade_not_allowed` 门禁处置），
   人工确认（`manual_confirmed`/`human_confirmed`）同样放行。
3. 声明语言校验：`source_lang`/`target_lang` 必须与配置方向（默认 `zh → en`）一致。
4. 规则证据（真实可复算）：长度比例、数字一致性、语言方向
   （`langdetect` 可用时给出真实概率；不可用时字符规则降级为 `heuristic_degraded`，`confidence=null`）。
5. 语义判定：
   - `backend=model`：真实余弦语义分，`classify_score` 与阈值比较；
     `≥` 阈值通过，`<` 阈值进异常清单（`semantic_not_equivalent`），并附 `model_margin` 等标签证据。
     阈值按判定单元源文长度分两档：默认 `threshold: 0.65`；源文去空白后 ≤ `short_unit_max_chars`（20）字的
     **短单位**改用更宽松的 `short_unit_threshold: 0.50`。实际用的是哪一档写在记录的
     `metadata.semantic_threshold_applied`（异常清单为顶层同名字段）。把 `short_unit_max_chars` 设为 `0` 即关闭豁免。
   - `backend=rule_degraded`：`semantic_score=null`；`degraded_policy=waiting_quality`（默认）时
     写入通过文件并标记待复核，`degraded_policy=reject` 时进异常清单（`semantic_score_missing`）。
6. 数字守卫（`number_guard: true`）：语义分通过后按三条规则复查判定单元的数字，命中任一即改判
   `rejected`（`reasons` 首项 `number_mismatch`），命中的规则与证据写进
   `metadata.semantic_number_guard_rules` / `_missing` / `_count` / `_swap`：
   - **缺失**：源文有、译文没有，且位数 ≥ `number_guard_min_digits`（3）的数字（含 19xx/20xx 年份）；
   - **计数**：位数 ≥ 3 的数字按出现次数比，源文出现 n 次而译文少于 n 次（漏掉重复出现的年份）；
   - **序列**：源文与译文数字序列个数相同、恰好一个位置不同 —— 该位置是改写/替换
     （`4.9 → 4.2`、`2 月 → 9 February` 这类 1–2 位数字替换，缺失规则拦不住）；
     个数不同时还有一条补充口径：源文多出一个 1–2 位数字、译文也多出一个 1–2 位数字
     （`现年 26 岁 → "33"`、`10 月 14 日 → "21"`）同样按替换处理；
     中文日期数字（`9月`、`2014年`、`14日`）不参与该口径 —— 译文写成月份单词
     （`9月 → Sept.`）不是漏译。该口径在自然语料 1546 条句对上未新增误报（实测 0 条）。
   单个 1–2 位小数字只在 `problem_labels` 里留 `omission`/`addition` 标签，不拦截；
   数字个数不同（译文合并/拆分数字、日期换序）一律不判。
   比较前做归一化：千分位/全角/空格写法（`940,000`＝`940 000`＝`９４０，０００`＝`164, 700`）、
   中文万/亿与 million/billion 的换写（`2.639亿`＝`263.9 million`）；
   句首段落编号（`115. 各项举措`）和半截断开的译文片段不算缺失。
   设 `number_guard: false` 可退回"只打标签不拦"。
7. 单位/币种守卫（`unit_currency_guard: true`，2026-11-04）：数字相同但单位或币种被换掉时改判
   `rejected`（`reasons` 首项 `unit_mismatch` / `currency_mismatch`），证据写进
   `metadata.semantic_unit_guard_rules` / `_unit_mismatches` / `_currency_mismatches`：
   - **单位**：`3 年 vs 3 months`、`5 kg vs 5 lb` 这类同数字不同量纲/不同单位；
     同一量纲的等价换算（`3 years = 36 months = 12 quarters`、`1 km = 1000 m`）用
     `unit_guard_relative_tolerance`（默认 2%）判定，不误拦；
   - **币种**：`5000 美元 vs 5000 euros`、`$5000 vs 5000 英镑`；同一币种的不同写法
     （`5,000 dollars` / `USD 5000`）不误拦；
   - **优先级**：数字本身不同（`3 vs 5`）交给数字守卫的 `swap` 规则，单位守卫不重复报；
     日历表达（`2024 年`、`3 月`）不算时长单位；译文不完整时不启用本守卫（避免半截换算误判）。
   - **等价换算折回源文数值（2026-11-04 补）**：`normalize_equivalent_units` 在数字守卫比较前，
     把译文中与源文**换算后相等**的数量折回源文数字（只改数字，不动单位词和词序），
     因此 `3 年 ↔ 36 months`、`5 kg ↔ 5000 g` 不会被 `swap` 误拦；`3 年 ↔ 36 years`
     这类"看着像换算、其实不等价"的仍判 `number_mismatch`。折算对写进
     `metadata.semantic_unit_guard_equivalents`（含 `source` / `target` / 两侧数值），可审计。
8. 否定守卫（`negation_guard: true`，2026-11-04）：**译文出现否定、原文没有否定**时改判
   `rejected`（`reasons` 首项 `negation_added`），证据写进
   `metadata.semantic_negation_guard_rules` / `_target_negations` / `_source_negations`：
   - **依据**：嵌入分对否定翻转几乎不敏感——`defect-14-025` 三段否定翻转得分 0.7194 / 0.7388 / 0.7854
     全部高于阈值（其中一段被判 S 级），与 11-03「改数字 12 条全部漏过」同类；
   - **只判一个方向**：反方向（原文有否定、译文没有）在自然语料误报 17.5%（中文否定常译成肯定式），
     只记录观测不拦截；两边都有否定时不按数量比；
   - **误报控制**：屏蔽 `not only/just/merely`、`not the only`、`nothing but`、`no doubt`、
     `whether or not`、`No. 5` 编号，以及中文「非常/不仅/无论/未来」等假朋友；
     译文去空白后短于 `negation_guard_min_chars`（12）不判（"Yes." / "No." 短答句）；
   - **子句口径（2026-11-05 补）**：两边都有否定时不再直接放弃，改比「含否定的子句数」——
     译文比原文多出否定子句、且译文否定子句 ≥ 2 时判 `negation_clause_added`
     （`defect-14-025:p5`「没有受伤」→「is not aged 64, was not injured」这类）。
     自然语料 150 条带否定的句对误报 5 条（3.3%）；
   - **框架口径（2026-11-05 补）**：译文插入 `It is not true that …` / `It is false that …`
     而原文没有「不是真的/并非/不实/绝非」这类对应说法时判 `negation_frame_added`。
     原文本就带否定时这类插入会挤进同一个子句，子句口径看不见（`defect-14-030:p7`）；
     自然语料 1554 句对 / 543 个父句单元误报 0 条；
   - **「并非」纠正（2026-11-05）**：它此前被当成假否定词屏蔽，导致「并非如此 → It is not true that」
     这种正确译文被第 1 条口径误拦；改回真否定后自然语料误报 15 → 13；
   - **实测**：876 个父句单元误报 6 条（0.68%）；bench 构造负例（否定翻转 18 条）召回 12/18，
     未召回的 6 条都是原文本身带否定、译文改了否定数量或作用域的情况（子句口径补上这条）；
   - 设 `negation_guard: false` 可关闭。
9. 不完整译文守卫（`incomplete_target_guard: true`，2026-11-05）：**原文以句末标点收尾、
   译文却在中途断开**时改判 `rejected`（`reasons` 首项 `incomplete_translation`，标签 `omission`），
   证据写进 `metadata.semantic_incomplete_guard_rules` / `_source_complete` / `_target_complete`：
   - **依据**：`clause_drop` 15 条里 14 条嵌入分高于 0.65（只译了前半句，前半句译对就撑住整句余弦），
     数字/单位/否定守卫都看不见 —— 被删掉的往往是后半句的修饰语；
   - **合法收尾**：不止 `.!?`，联合国文件常见的分号、冒号、右括号、引号、脚注编号（`…2014.42`）都不算截断；
   - **脚注源文（2026-11-05 补）**：源文以 `*` / `†` / `‡` 收尾（名录条目「帕帕·路易斯·法尔先生(塞内加尔)*」）
     按完整句处理；自然语料 1554 句对 / 543 个父句单元这类收尾 0 条，不引入误报；
   - **悬空虚词收尾（2026-11-05 补）**：译文以 `and` / `or` / `of` / `to` / `the` / `with` / `that`
     这类虚词断在句尾即判不完整，**不再要求原文带句末标点** —— `defect-14-026:p9` 的原文以
     「来自西欧和其他国家： [待补]」收尾，旧口径直接放弃判定；只在原文自己也停在句末标点/右闭合
     符号上时计算（否则中文原文断在逗号处、英文译文以 `and` 承接是正常现象），自然语料
     543 + 399 个判定单元误报 1 / 0 条，且该条本来就已被旧口径拦下（新增误报 0）；
   - **下限**：原文去空白后短于 `incomplete_target_min_chars`（10）不判（标题/标签类单位本来常不带句号）；
   - **实测**：自然语料 1554 条入口句对误拦 23 条（1.7%，多为错配片段，拦下来是对的）；
   - 设 `incomplete_target_guard: false` 可关闭。
10. 语义判别器 P2（`semantic_judge: true`，2026-11-05）：本地 Qwen3.5-2B 判「译文里有没有
    原文根本没有的专名/实体」，命中改判 `rejected`（`reasons` 首项 `entity_not_in_source`）：
   - **为什么不用规则**：`entity_substitution` 是把原文某个词换成原文没有的人名（`Anderson`），
     数字没变、单位没变、否定没变、句子完整，中文译名与英文人名之间也没有可查的对照表，
     词法层没有通用信号；
   - **口径**：模型先为认到的每个名字给出**源文中文片段**，片段必须逐字出现在源文里才承认有对应；
     只有明确列为 `missing` 的、形状像专名（首字母大写、在译文里出现、不在句首/公文动词表）的词才判拦；
   - **只在放行后调用**：语义分与全部规则守卫都通过才调用，只对本来要交付的记录付推理开销；
     权重 4.3GB 懒加载，单条约 1.2–1.5s（RTX 4060 / fp16）；
   - **不可用不外推**：权重缺失/推理失败/输出无法解析时不产生规则，`metadata.semantic_judge_status`
     写 `unavailable` / `error` / `skipped_short`，绝不用默认值代替判别结果；
   - **解析失败重试（2026-11-05 补）**：约 2%（9/433）的调用里模型会把源文中文词逐个写进 `missing`，
     96 token 被撑满后截断成非法 JSON；这类失败加严提示重试一次（只对失败的那 2% 生效，
     不改主口径），重试次数记在汇总日志的 judge 状态 `retried`；
   - **实测**：自然语料 150 条抽样误拦 12 条（8.0%），bench 构造负例召回 13/17
     （直接列缺失词的第一版召回 17/17 但误拦 20.7%，不可用）；
   - 设 `semantic_judge: false` 可关闭（`semantic_judge_model_path` 相对本算子目录）。
11. 非等价记录去向（`non_equivalent_policy: mark_and_keep`，2026-11-05）：
    - **为什么不再物理删除**：规范 4.14 只授予本算子「判语义等价」的职责，放行/拒付的最终决定权
      在算子 17 的 `quality_status` / `delivery_eligible` 门禁；此前默认把非等价记录直接删进
      `14_rejects.jsonl`，下游 15/16/17 完全看不到这条缺陷 —— 基准实测 70 条缺陷段落里
      61 条被本算子删掉，16 号只能评到 9 条，闭环断裂；
    - **现在的写法**：记录原样留在 `14_semantic_checked.jsonl`，打上
      `quality_status=rejected`、`semantic_status=not_equivalent`、`review_status=rejected`、
      `delivery_eligible=false`、`semantic_reject_reasons=[…]`；异常清单同时写一条
      `kept_in_output=true` 的审计条目（只留痕、不代表删除）；
    - **下游为什么仍然拒付**：17 号 `status_fields.semantic=[quality_status, semantic_status]`
      命中 `rejected`、`delivery_eligible=false`，记录进不了交付集；16 号 `_merge_review_status`
      原样保留上游 `rejected`。刻意不用 `waiting_manual` —— 它会触发下游的放行旁路；
    - **降级记录不受影响**：`semantic_score` 缺失属于「没有分数可判」，去向仍由
      `degraded_policy` 决定（`reject` 时真删）；
    - 想回到旧行为（物理删除）：`non_equivalent_policy: drop`。
12. 状态优先级：`failed`（配置/编码/空输入/模型不可用且 `fail`/模型错误超上限）>
   `degraded`（规则降级）> `partial`（有拦截）> `success`。
13. 输出先写 `.tmp` 再 `os.replace` 原子替换；`OUTPUT_DIR/<uniqueId>/` 任务隔离。

**一致性**：`output_records = accepted + waiting_quality + kept_rejected
= 14_semantic_checked.jsonl 行数`；`rejected = kept_rejected + rejected_dropped + 准入/结构/降级拒收
= 14_rejects.jsonl 行数`；统计全部来自真实读写结果。

## 4. 接口

外层响应固定 `{code, msg, data}`；正式契约仅三个接口，不提供 `/downloadFile`。

| 能力 | 接口 |
| --- | --- |
| 上传 | `POST /uploadFile?uniqueId=` |
| 调用 | `POST /predict`（`uniqueId`、布尔 `isLastOperator`，阶段内后续算子传 `inputFile`） |
| 删除 | `POST /removeFile?uniqueId=` |

中间调用只返回 `data.outputFile` 路径；阶段末调用返回 `data.outputFile` 内容；
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

- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_14_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回
  `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- `/predict` 额外支持 `operatorId`（选传，若传须为 `"14"`）与 `params`（配置覆盖，优先于兼容字段 `config`）。
- 失败返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物；不提供 `/downloadFile`。

## 4.1 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.14 小节的 `param_` 行与
`configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_语义相似度阈值` | `threshold` / `short_unit_max_chars` / `short_unit_threshold` / `model_backend` / `fallback_policy` / `degraded_policy` | 语义等价判定阈值（默认 0.65，短单位 0.50）及模型不可用时的降级/失败策略 |

## 5. 配置项（`configs/config.yaml`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `source_lang` / `target_lang` | `zh` / `en` | 声明的语言方向 |
| `input_file_name` | `13_graded.jsonl` | 上游默认产物名 |
| `allowed_grades` | `[S, A, C]` | 默认准入等级（C 级是否放行由 17 号门禁决定，本算子不越权删） |
| `grade_fields` | `[grade, quality_grade]` | 等级字段名（顶层/`metadata`） |
| `allow_manual_confirmed` / `manual_confirmed_fields` | `true` / `[manual_confirmed, human_confirmed]` | 人工确认放行 |
| `allow_missing_grade` | `false` | 缺等级是否放行 |
| `threshold` | `0.65` | 语义等价阈值（只对真实分生效） |
| `short_unit_max_chars` / `short_unit_threshold` | `20` / `0.50` | 短单位豁免：判定单元源文去空白后不超过该字数时改按短阈值判；`0` 表示关闭 |
| `scoring_unit` / `parent_fields` | `parent` / `[parent_pair_id]` | 判定单元：同父句子句拼成整句打一次分（`clause` 为旧的逐子句行为） |
| `model_name` / `model_path` | `Qwen3-Embedding-0.6B` / `../models/Qwen3-Embedding-0.6B` | 模型标识与本地权重路径（相对本算子目录，不硬编码绝对路径） |
| `model_backend` | `model` | `auto` / `model` / `rule_degraded`（交付默认要求真实 Qwen3-Embedding 模型） |
| `fallback_policy` | `fail` | 模型不可用：`rule_degraded` 降级 / `fail` 结构化失败 |
| `device` / `batch_size` / `max_model_errors` | `cpu` / `32` / `10` | 推理设备、批大小、模型错误上限 |
| `degraded_policy` | `waiting_quality` | 降级记录去向：待复核 / 拒收 |
| `non_equivalent_policy` | `mark_and_keep` | 判为非等价的记录去向：留在输出流打标记（下游 17 号门禁拒付）/ `drop` 物理删除（旧行为） |
| `length_ratio_min` / `length_ratio_max` | `0.4` / `2.5` | 长度比合理区间（规则证据） |
| `number_check` / `language_check` | `true` / `true` | 数字一致性 / 声明语言校验 |
| `number_guard` / `number_guard_min_digits` | `true` / `3` | 数字守卫：缺失、重复计数、序列替换三条规则命中任一即拦截；位数门槛只管缺失与计数两条，`false` 退回只打标签 |
| `negation_guard` / `negation_guard_min_chars` | `true` / `12` | 否定守卫：译文多出否定即拦截（只判 zh→en 一个方向）；两边都有否定时按含否定的子句数比，译文插入 `It is not true that …` 而原文无对应时单独判；译文去空白后短于该字数不判 |
| `incomplete_target_guard` / `incomplete_target_min_chars` | `true` / `10` | 不完整译文守卫：原文以句末标点收尾（含 `*`/`†`/`‡` 脚注收尾）、译文中途断开即拦截；原文短于该字数不判 |
| `incomplete_target_dangling_tail` | `true` | 悬空虚词收尾：译文以 `and`/`or`/`of`/`the` 这类虚词断在句尾即判不完整（只对原文自己停在句末标点/右闭合符号上的单元计算）；自然语料 543 + 399 个判定单元误报 1 / 0 条 |
| `semantic_judge` / `semantic_judge_model_path` / `semantic_judge_min_chars` | `true` / `../models/Qwen3.5-2B` / `12` | 语义判别器（P2）：译文多出原文没有的专名即拦截；只在放行后调用，不可用时不产生规则；译文短于该字数不判 |
| `input_encodings` / `text_fields` | `[utf-8]` / `[source, target]` | 严格 UTF-8；只处理 source/target |

未知配置字段直接报配置错误（`extra=forbid`）。

## 6. 命令行

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

## 7. 服务启动

```bash
uvicorn inference:app --host 0.0.0.0 --port 8014
# 或
python inference.py
```

## 8. 测试

```bash
python -m pytest tests/ -q     # 100 passed（2026-11-05 不完整译文 / 否定框架 / 判别器重试等守卫用例；含真实权重冒烟）
python -m json.tool map.json    # 通过
python -m compileall -q .       # 退出码 0
```

离线测试策略：模型分支通过注入"真实计算"的测试替身（字符 2-gram Jaccard）验证接线与阈值判定，
不可用路径显式指向不存在的权重目录；真实权重只由 `tests/test_qwen3_real_smoke.py` 验证
（CPU、`local_files_only`，权重缺失时自动跳过）。测试不伪造任何模型分数。

## 9. 模型部署与资源

- 交付默认 `model_backend: model` + `fallback_policy: fail`：要求真实 Qwen3-Embedding-0.6B 权重；
  权重路径写相对 `../models/Qwen3-Embedding-0.6B`，`sentence-transformers` 加载并强制
  `local_files_only=True`（1024 维，last-token 池化 + L2 归一化），不联网下载、不连远程模型服务。
- 启用真实模型：安装 `sentence-transformers`（或 `transformers`+`torch`）；本环境已安装
  `sentence-transformers 6.2.0` / `torch 2.6.0+cpu`。离线验证用
  `params: {"model_backend": "rule_degraded"}` 走规则路径。
- 资源：规则降级仅需 CPU；Qwen3-Embedding-0.6B 推理建议 ≥16GB 内存 CPU 或 1 张 GPU，
  `HF_HOME` 需可写（Dockerfile 已设 `HF_HOME=/app/.cache/huggingface`）。

## 10. 已知限制、降级行为与未确认口径

- **真实模型验证**：由 `tests/test_qwen3_real_smoke.py` 覆盖（CPU、`local_files_only`）；其余模型分支
  通过注入"真实计算"的测试替身（字符 2-gram Jaccard）验证接线与阈值判定，生产代码零硬编码分数。
- 降级行为：`rule_degraded` 显式 → 状态 `degraded`、`semantic_score=null`；
  `model`+`fail` → 结构化 `failed`，不静默降级；`auto`/`model`+`rule_degraded` → 降级并标记。
  **`semantic_score` 缺失时交付节点默认拒收。**
- **语言检测非本算子职责**：`langdetect` 未安装时语言方向证据降级为 `heuristic_degraded`
  （`confidence=null`），此时 `direction_ok=null`（未知），不会产出 `semantic_drift` 标签。
- `model_version` 取权重目录/仓库标识（如 `Qwen3-Embedding-0.6B(dim=1024)`），取不到时不编造。
- 阈值 `0.70`、长度比区间 `[0.4, 2.5]`、`allowed_grades=[S, A]`、`degraded_policy=waiting_quality`
  均为 demo 默认值，正式阈值需任务方确认。
- 逐条调用模型（未做批处理），大文件时吞吐可能受限；如需提升可改为按 `batch_size` 批量打分。
- **数字守卫的残余漏检**（有意取舍，宁漏勿错）：译文用文字/中文数字写数字（"第 3 条"→"article three"）、
  源译数字个数发生变化（如 `20→27` 而译文另有日期数字）、译文多出数字（增译方向）这三类只打标签不拦截；
  `number_guard` 依赖上游切分质量，译文在数字中间被切断时会跳过该单元。
- **否定守卫的残余漏检**（有意取舍）：原文本身带否定、译文改变了否定数量或作用域的情况不判
  （自然语料上反方向误报 17.5%）；同义词式否定（`拒绝`→`declined`、`缺乏`→`lack`）不在词表内。
  断句漏译、实体替换仍无词法信号，需语义判别器，见 `_修改方案_语义等价校验14_20261104.md`。
- **语义判别器的残余漏检**（如实记录，未解决）：44 篇缺陷集 `defect-14-027:p4`、`-030:p10`、`-031:p8`
  三条实体替换仍漏过 —— 前两条的植入名位于**句首**、第三条被上游对齐截断，2B 模型都没报；
  试过在提示词里直说「句首人名也要判」，同一组 150 条自然语料误报从 8.0% 降到 2.7%，
  但 17 条构造负例召回掉到 8/17，故未采用。
- **14 号在 44 篇缺陷集上的实测**（`_eval/runs/defect44_op14v3_20261105`，`constructed_proxy` 口径的**检出率**）：
  严格口径（整段记录全部被拦）56/67 = 83.6%，记录口径（段内至少拦下一条）58/67 = 86.6%
  （改动前 26/67 → 54/67 → 56/67）。剩余 9 条（记录口径）分三类：
  - **上游对齐吃掉缺陷**（6 条，不是 14 号能看见的）：`026:p3`（`It is not true that` 前缀被切掉）、
    `028:p8`（段落编号 `24.`→`31.`，译文侧 `31.` 被切掉）、`028:p10`（`On behalf of the people of
    Anderson,` 前缀被切掉）、`029:p6`（编号 `12)` 被切掉）、`030:p4`（源文「上午 10 点开始」整句被丢）、
    `030:p10`（译文截到 `the Milky`）；
  - **判别器局限**（3 条，其中 `030:p10` 与上面重叠）：`027:p4`、`031:p8` 的植入名在句首且被模型
    认成了原文里真实存在的中文片段，对账口径看不出来；
  - **源文本身不完整**（1 条）：`026:p9` 的源文是 `来自西欧和其他国家：[待补]` 占位，守卫按设计不判。
- **阶段产物**：zip 由算子侧生成；对象存储仅提供 `OBJECT_STORE_DIR` 本地镜像占位，未接入真实
  OSS/S3 客户端；未在真实容器环境验证。
- **未确认口径**：阶段末 `data.outputFile` 的序列化形式（当前为 UTF-8 字符串）；
  `removeFile` 按路径还是按 `uniqueId`；下载接口的最终 HTTP 方法与返回结构（正式契约不提供 `/downloadFile`）。

## 11. 未验证项（不声称已完成）

- 未执行 `docker build`（环境未提供可用 Docker 守护进程），Dockerfile 仅静态编写。
- 真实 Qwen3-Embedding 权重推理由任务方在内存允许时串行执行（本包内权重 `models/Qwen3-Embedding-0.6B`，
  1024 维，不联网下载）；单测不加载真实权重。
- 未与算子 13（上游 `13_graded.jsonl`）/15/16 做真实工程侧联调，未做并发/大文件压测。
