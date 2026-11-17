# AVP_ADVICE_DATA_009 · 数据契约（DATA_SCHEMA）

> 算子：优化建议生成（`avp_suggest_optimization`）　阶段：S3 效果分析（2/2，阶段末）
> 输入是 **008 的归因产物**；输出以《语音加扰算子开发规范》§8 的 `009_advice.json`
> 为最小契约，附加字段只增不改。建议文案全部由固定模板回填真实数字，不接大模型。

---

## 1. 数据来源与边界

| 项 | 内容 |
|---|---|
| 输入 | `008_attribution.json`（必需）+ `008_alerts.json`（可选，取 008 实际告警阈值） |
| 输入目录形态 | 传目录时优先找 `008_attribution.json`，同目录的 `008_alerts.json` 自动读取 |
| 输入缺失告警文件 | 用 008 attribution 里的 `alert_threshold_pp`；再没有才用本配置 `checks.alert_pp`，并写 `warnings` |
| 输出 | `009_advice.json`（1–3 条建议） |
| 不做 | 不重算归因、不改 008 数字、不评测、不生成自由文本、不调用大模型、不做发布决策 |

---

## 2. 输入字段（008 归因契约，009 只读取）

| 字段 | 类型 | 空值 | 009 用途 |
|---|---|---|---|
| `eval_id` / `external_task_id` | string | task_id 允许空 | 输出 `task_id`、来源追溯 |
| `baseline.eval_id` | string | 允许空 | `source.baseline_eval_id` |
| `metric` | string | 不可空 | 文案里的 CER/WER/SER |
| `metrics` / `baseline_metrics` | object | 整体兜底时必需 | 整体兜底建议的差值 |
| `sample_count` | int | 允许空 | 整体兜底补样后的预计样本 |
| `total_net_error` | int | 不可空 | 事实段与兜底缺口 |
| `main_cause` / `share` / `affected` / `confidence` | — | 允许空 | 根字段透传；无主因时写 `warnings` |
| `error_mix` | object | 允许空 | 样本级错误构成缺失时的兜底 |
| `factors[]` | array | 不可空（可为空数组） | 建议候选来源 |
| `factors[].dimension` / `value` / `samples` | string/string/int | 不可空 | 建议对象与样本缺口 |
| `factors[].in_scope` | bool | 缺省 false | 白名单外不生成建议 |
| `factors[].comparable` | bool | 缺省 false | 不可比不生成维度建议 |
| `factors[].diff_pp` / `affected` / `share` | number/int/float | 允许空 | 优先级、事实段、补样缺口 |
| `factors[].ratio` | float | 允许空 | 覆盖建议排序（最低占比优先） |
| `factors[].confidence` | enum | 允许空 | 证据强度段 |
| `factors[].sample_trace` | object | 允许空 | 错误构成、CI、SNR 桶、sample IDs |
| `alert_count` | int | 允许空 | 与告警文件交叉校验 |
| `alert_threshold_pp` | float | 允许空 | 未拿到 alerts 文件时的阈值来源 |

> 008 不输出 `attributable` 字段时 009 按 true 处理；009 不重新过滤回滚记录（008 已过滤）。

---

## 3. 建议生成规则

### 3.1 候选与分类

| 类别 | 条件 | 用途 |
|---|---|---|
| 退化建议 | `in_scope=true` 且 `comparable=true` 且 `diff_pp>0` | 按优先级给出修复/复测建议 |
| 覆盖建议 | 非退化候选，且 `samples < min_samples_per_value`（默认 100） | 样本不足的维度扩量确认（P2） |
| 整体兜底 | 没有任何可建议候选（或条数不足下限） | 用整体指标给 1 条复核建议 |

### 3.2 优先级（硬口径）

```text
P0：diff_pp ≥ p0_pp（默认 3.0）  或  share ≥ p0_share（默认 0.30）
P1：diff_pp ≥ p1_pp（默认 1.0）
P2：其余（含 diff<1pp 的轻微退化、样本不足的覆盖建议、整体兜底）
```

参数 `p1_pp > p0_pp` 直接报 `invalid_params:`，不做静默修正。

### 3.3 选择顺序与条数

1. 退化建议按 `(优先级, 差值降序, 占比降序, 维度, 取值)` 排序，取前 `max_suggestions` 条（1–3）；
2. 若不足 `max_suggestions`，用覆盖建议按 `(样本占比升序, 差值降序, 维度, 取值)` 补齐；
3. 若仍不足 `min_suggestions`，追加整体兜底；
4. 最终条数保证在 `[min_suggestions, max_suggestions]` 且 1–3 条。

> 主因并列时 008 不给唯一主因，但两个并列维度都会进入退化候选，各自出建议——不丢信息。

### 3.4 补样计划（所有数字可复算）

```text
缺口 gap：
  退化建议：gap = max(min_count, affected)          # 多错样本规模
  覆盖建议：gap = min_samples_per_value − samples    # 样本缺口
  整体兜底：gap = max(min_count, |total_net_error|)

补样 count = max(min_count, round_half_up(gap × sample_plan_factor))
预计样本 projected_samples = 现有样本数 + count
```

- `min_count` 默认 1（`checks.sample_plan_min_count`）；
- `sample_plan_factor` 默认 1.5（params 可覆盖）；
- 舍入 half-up：`3×1.5=4.5→5`、`9×1.5=13.5→14`；
- 每条的 `sample_plan` 都带 `formula`、`gap_definition`、`purpose`，审查者可手算核对。

---

## 4. 输出：`009_advice.json`

### 4.1 契约根字段

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `task_id` | string | 否 | `external_task_id`，缺失时用 `eval_id` |
| `generated_at` | string | 否 | 生成时间 ISO8601（+08:00，秒级） |
| `suggestions` | array | 否 | 1–3 条建议，见 4.2 |

### 4.2 `suggestions[]`

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `title` | string | 否 | 标题，≤`io.max_title_len`（默认 20 字），超长截断并告警 |
| `dimension` | string | 否 | 维度名；整体兜底固定 `overall` |
| `value` | string \| null | **是** | 维度取值；整体兜底为 null |
| `priority` | enum | 否 | `P0` / `P1` / `P2` |
| `fact` | string | 否 | 事实：差值、影响条数、占比（整体兜底：整体差值、净多错） |
| `evidence` | string | 否 | 证据强度：样本数、错误数、错误率、95% CI、置信度 |
| `explanation` | string | 否 | 解释：错误形态构成、SNR 分桶集中度 |
| `verification` | string | 否 | 验证方式：补样条数、目标回落幅度 |
| `sample_plan` | object | 否 | 补样计划，见 4.3 |

附加字段（只增）：`source`（eval_id / baseline_eval_id / factor_index / metric / total_net_error /
main_cause / purpose）、`evidence_source`（`sample_trace` / `record_error_mix` / `sample_gap` /
`overall_metrics`）。

### 4.3 `sample_plan`

| 字段 | 类型 | 含义 |
|---|---|---|
| `count` | int | 补样/扩量条数 |
| `gap` | int | 缺口（定义随 `purpose`） |
| `factor` | float | 补样系数（默认 1.5） |
| `formula` | string | `count = max(min_count, round_half_up(gap × factor))` |
| `gap_definition` | string | 缺口口径的中文说明 |
| `purpose` | enum | `degradation_verification` / `coverage_expansion` / `overall_review` |
| `projected_samples` | int | 补样后预计样本数 |
| `min_samples_per_value` / `min_count` | int | 样本门槛与补样下限 |
| `target_pp_drop` | float | 验证目标回落幅度（默认 0.3pp） |
| `dimension` / `value` / `snr_bucket` | — | 补样对象与 SNR 桶（可空） |

### 4.4 根级附加字段

| 字段 | 含义 |
|---|---|
| `operator` | `avp_suggest_optimization` |
| `record_id` / `eval_id` / `baseline_eval_id` | 008 记录追溯 |
| `metric` / `total_net_error` / `main_cause` / `share` / `affected` / `confidence` | 008 结论透传（不重算） |
| `suggestion_count` / `priority_counts` | 条数与优先级分布 |
| `alerts` | `{available, count, threshold_pp}`：是否读到 008 alerts、条数、阈值 |
| `source_file` | 输入归因文件名 |
| `warnings` | 008 警告透传、阈值差异、标题截断、无主因等提示 |

---

## 5. 文案模板与可追溯性

- 模板全部在 `configs/config.yaml` 的 `templates` 下，`{占位符}` 只由 processor 用真实数字回填；
- 占位符缺一个就抛 `internal_error`（宁可失败，不输出半截文案）；
- 建议文本里的每个数字必须能在以下来源找到：008 的差值/影响条数/占比/样本数/错误构成/CI/SNR 桶、
  补样公式的 count/gap/factor/projected_samples、配置的 95% CI 水平、008 告警阈值；
- 指标脚本 `utils/eval_metrics.py` 会逐条抽取建议文本中的数字并核对来源，`number_traceability` 必须为 1.0。

---

## 6. 与上下游的衔接

- 上游 008：读 `008_attribution.json`（必需）与 `008_alerts.json`（可选）；
- 下游 011 报告：直接引用 `suggestions[].fact/evidence/explanation/verification` 与 `sample_plan`，
  不重新生成文案；
- 009 是 S3 阶段末算子（`isLastOfStage=true`），服务层会在 `isLastOperator=true` 时回传
  `outputFile.main="advice"` 的内容束。

---

## 7. 异常与降级

| 场景 | 行为 |
|---|---|
| 输入不是 008 产物（缺 `factors`/`metric`/`total_net_error`） | `invalid_jsonl:`，不猜 |
| 输入文件不存在 / 目录为空 | `missing_file:` |
| `max_suggestions` 不在 1–3、`min>max`、`p1>p0`、系数≤0 | `invalid_params:` |
| 没有可建议维度 | 整体兜底 1 条（`overall`），不硬编维度结论 |
| 整体兜底缺目标/基准指标 | `invalid_jsonl:`，不编数字 |
| 找不到 `008_alerts.json` | 用 attribution 的 `alert_threshold_pp`，再退配置默认值，并写 `warnings` |
| 标题超长 | 截断到 20 字以内并写 `warnings` |
| 008 主因并列/低于阈值 | 透传 `main_cause=null` 与原因，按维度因素降级出建议 |
