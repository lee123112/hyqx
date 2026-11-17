# 优化建议报告（AVP_REPORT_DATA_011）输入输出契约

本文件说明：输入来自哪些文件、输出到哪些文件、字段类型/含义/空值口径。
规范 §8 对 011 的硬契约是「`011_report.md` / `011_report.html` 八段式报告」；
`011_report_meta.json` 为补充产物（机器可读的报告元信息），不影响八段契约。

---

## 1. 输入

### 1.1 输入发现

| 情形 | 行为 |
|---|---|
| `inputFile` 是目录 | 递归扫描 `io.input_exts`（`.jsonl` / `.json`），按相对路径排序后逐个识别 |
| `inputFile` 是文件 | 扫描**同目录**的 JSON/JSONL（保证 007/008/009 同目录时一次接入），并把该文件放在首位 |
| 单文件 bundle | 一个 JSON 内同时带 `eval_results` / `attribution` / `advice` 三个键时，一次拆成三份输入 |
| 文件名识别 | 按文件名小写包含匹配：`007_eval`/`eval_result`/`evaluation_result`/`eval_results` → 评测；`008_attribution`/`attribution`/`08_attr` → 归因；`009_advice`/`advice`/`suggestion` → 建议 |
| 内容嗅探 | 文件名不规范时按内容判断：`suggestions` → 建议；`main_cause`/`record_id`/`factors` → 归因；`metrics`/`eval_id` → 评测 |
| 无法识别 | 跳过并写 `warnings`（不中断）；三份输入一份都识别不到 → 抛 `missing_file:` |

**缺输入不是失败**：缺哪份就在对应章节写「**未接入**」，状态置 `partial`，绝不编数字。

### 1.2 007 评测结果（`007_eval_results.jsonl`）

每行一条评测记录。本算子实际消费的字段（其余字段忽略，`sample_results` 不读取）：

| 字段 | 别名 | 类型 | 必填 | 空值口径 |
|---|---|---|---|---|
| `eval_id` | — | string | 否 | 缺失时 meta 不展示 |
| `metrics` | — | object | **是**（至少含 cer/wer/ser 之一） | 无可用指标的行被跳过并告警 |
| `metrics.cer` / `wer` / `ser` | — | number | 至少一项 | 缺项显示「—」 |
| `metrics.samples` | `sample_count` / `total_samples` | int | 否 | 缺省不显示样本量 |
| `metrics.abnormal` | `abnormal_count` / `bad_samples` | int | 否 | 缺省不显示异常数 |
| `model` | `model_name` | string | 否 | 摘要用「整体 …」 |
| `task_name` | `external_task_id` | string | 否 | 标题退化为「未命名任务」 |
| `version_id` / `version_name` | `versionId`/`versionName` | string | 否 | 标题忽略 |
| `completed_at` | `finished_at`/`generated_at` | string（ISO） | 否 | 多条时按最早时间兜底排序 |
| `attributable` | — | bool | 否 | `false` 时报告提示「已回滚，仅供参考」 |
| `baseline` | — | string 或 {name} | 否 | 也接受 `baseline_metrics` 单独提供基准值 |
| `baseline_metrics.cer/wer/ser` | — | number | 否 | 缺省时整体指标表「对比对象」列显示「—」 |
| `dimensions[]` | — | array | 否 | 缺少时「分场景表现」写「未接入」 |
| `dimensions[].dimension` | `name`/`dim` | string | 是（行内） | 无维度名的行显示「未标注」 |
| `dimensions[].value` | `label`/`group` | string | 是（行内） | 同上 |
| `dimensions[].samples` | `sample_count`/`count` | int | 否 | 显示「—」 |
| `dimensions[].ratio` | `share`/`proportion` | number | 否 | 显示「—」 |
| `dimensions[].cer/wer/ser` | — | number | 否 | 显示「—」 |
| `dimensions[].error_mix` | `errors`/`mix` | object | 否 | 缺少时「错误形态」的「集中在」列显示「—」 |
| `error_mix` | `errors`/`error_types` | object `{sub,del,ins,unk}` | 否 | 缺少时「错误形态」写「未接入」 |

**多条评测记录**：按 `selection.eval_pick`（默认 `latest`）取 `completed_at` 最新一条，
其余条数写入 `meta.warnings`，并在「风险与限制」注明「输入含 N 条评测结果，本报告按最新一条生成」。

**单位口径（`metric_unit: auto`）**：若 `cer/wer/ser` 三项全部 ≤ 1，视为比例（0.076 → 7.6%）；
否则视为百分数（7.6 → 7.6%）。可用 `metric_unit: percent|ratio` 强制指定。
`dimensions[].ratio` 与 008 `share` 分别由 `ratio_unit` / `share_unit` 控制，逻辑相同。

### 1.3 008 差异归因（`008_attribution.json`）

| 字段 | 别名 | 类型 | 必填 | 空值口径 |
|---|---|---|---|---|
| `record_id` | — | string | 否 | 缺失不展示 |
| `eval_id` | — | string | 否 | 与 007 不一致时写告警 |
| `baseline` | — | string 或 {name} | 否 | 缺省用 007 的对比对象 |
| `total_net_error` | `net_error`/`net_delta` | int | 否 | 显示「—」 |
| `main_cause` | `primary_cause` | string | 否 | 缺少时「结论摘要/原因分析」标注未接入主因 |
| `share` | `ratio`/`main_share` | number | 否 | 显示「—」 |
| `affected` | `affected_count`/`count` | int | 否 | 显示「—」 |
| `confidence` | — | string | 否 | 缺省不显示括号 |
| `factors[]` | — | array | 否 | 缺少时「归因因素」表写「未接入」 |
| `factors[].name` | `title`/`cause` | string | 是（行内） | 缺省用 `dimension · value` |
| `factors[].delta_pp` | `diff_pp`/`gap_pp`/`delta` | number | 否 | 缺少时「依据」用 `basis`/`evidence` 文本 |
| `factors[].affected` | `affected_count`/`count` | int | 否 | 显示「—」 |
| `factors[].samples` | — | int | 否 | 告警阈值判断用 |
| `factors[].basis` | `evidence`/`reason`/`note` | string | 否 | 缺少时生成「差值 ±x pp，样本 n 条」 |
| `chain[]` | — | string 或 {label,text} | 否 | 缺少时「归因链路」写「未接入」 |

### 1.4 009 优化建议（`009_advice.json`）

| 字段 | 别名 | 类型 | 必填 | 空值口径 |
|---|---|---|---|---|
| `task_id` | — | string | 否 | 缺失不展示 |
| `generated_at` | — | string | 否 | 缺失不展示 |
| `suggestions[]` | `items`/`advice_list` | array | 否 | 为空或缺失 → 「未接入：优化建议输入为空」 |
| `suggestions[].title` | `name` | string | 否 | 缺省「建议 N」 |
| `suggestions[].dimension` | `dim` | string | 否 | 与 `value` 组合成「归因维度」 |
| `suggestions[].value` | — | string | 否 | 同上 |
| `suggestions[].priority` | `level` | string | 否 | 缺省不显示 |
| `suggestions[].fact` / `evidence` / `explanation` / `verification` | — | string | 否 | 缺失的段落不输出 |
| `suggestions[].sample_plan` | `plan` | object 或 string | 否 | 对象支持 `count`/`snr`/`snr_range`/`sources`/`note` |

**建议条数口径**：业务要求每任务 1–3 条；超过 `report.max_suggestions` 时只展示前 3 条，并在报告与 `warnings` 注明。
文本按 `limits.max_text_len` 截断并标注「（已截断）」。

### 1.5 时间口径

与 010 一致：`report.timezone`（默认 `Asia/Shanghai`），无 tzdata 用 `utc_offset_hours` 兜底；
输出时间格式 `report.datetime_format`（`%Y-%m-%d %H:%M`）。

---

## 2. 输出

### 2.1 文件

| 产物 | 文件名 | 何时生成 |
|---|---|---|
| Markdown 报告 | `011_report.md` | `params.format ∈ {md, both, all}`（默认 all） |
| HTML 报告 | `011_report.html` | `params.format ∈ {html, both, all}`（默认 all） |
| **PDF 报告** | `011_report.pdf` | `params.format ∈ {pdf, all}`（默认 all）且 `pdf.enabled=true`、reportlab 可用 |
| 报告元信息 | `011_report_meta.json` | 始终生成 |

映射到 `map.json` 的 `outputs.report_md / report_html / report_pdf / report_meta`，文件名不可改。
PDF 与 md/html **同源同数**：同一份内部 `model` 渲染，章节号、表内数字、报告编号完全一致。
PDF 生成失败（缺 reportlab 或渲染异常）不影响 md/html，只在日志告警并把 `statistics.pdf_generated` 置 `false`。

### 2.2 Markdown / HTML 结构

固定八段（顺序不可改，`params.include_sections` 只能裁剪）：

1. 结论摘要
2. 整体指标
3. 分场景表现
4. 噪声场景下钻
5. 错误形态
6. 原因分析
7. 优化建议
8. 风险与限制

- Markdown：`# 标题` + 报告编号/生成时间/样本量/异常样本/对比口径 元信息 + `## N. 章节` 八段；表格使用 GFM 表格。
- HTML：`<!DOCTYPE html>`、`lang="zh-CN"`、内联 `<style>`（A4 `@page`、`@media print`、CSS 变量），
  **不引用任何外部 CDN/JS/CSS/图片**，浏览器打开即可「打印 → 另存为 PDF」。
- 缺输入章节写「未接入：……」；数值缺失显示「—」；不编造、不补零。

### 2.3 `011_report_meta.json` 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `report_no` | string | 报告编号（默认 `RPT-BENCH-{YYYYMMDD}-{NNN}`，序号从 `report_no_seq_start` 起） |
| `title` | string | 报告标题 |
| `generated_at` | string | 生成时间 |
| `format` | string | `md` / `html` / `pdf` / `both` / `all` |
| `sections[]` | array | 实际输出的章节 `{no, title}`（裁剪后重新编号） |
| `inputs.eval_results` | object\|null | `files` / `records` / `eval_id` / `version_id` / `model` |
| `inputs.attribution` | object\|null | `record_id` / `eval_id` / `main_cause` / `confidence` |
| `inputs.advice` | object\|null | `suggestions` / `task_id` |
| `sources[]` | array | 实际读取的文件与识别类型（溯源） |
| `key_numbers` | object | 报告关键数字快照（cer/wer/ser/samples/abnormal/baseline_name/main_cause/share_percent/affected/total_net_error/error_mix/dimensions/suggestions） |
| `warnings[]` | array | 输入容错/多条评测/建议超限等告警 |
| `caliber` | object | 口径说明快照（八段/数字来源/HTML 自包含） |

**空值口径**：缺输入的 `inputs.*` 为 `null`；`key_numbers` 中缺失项不出现在对象里（不是 0）。

---

## 3. 状态与错误码

| 状态 | 条件 |
|---|---|
| `success` | 007/008/009 均可用（009 至少 1 条建议） |
| `partial` | 任一输入缺失/建议为空/评测记录全部无效但仍有其他输入；报告照常生成八段，缺处写「未接入」 |

| 错误前缀 | 触发条件 |
|---|---|
| `missing_file:` | 没有任何可识别输入；输入路径不存在 |
| `invalid_params:` | `format` 非法；`include_sections` 含未知章节 |
| `invalid_jsonl:` | 只识别到 007 文件但其中没有一条含可用指标 |
| `internal_error:` | 未预期异常（服务层统一收口） |

---

## 4. 示例

输入目录（`dataset/sample/`）：

- `007_eval_results.jsonl`：1 条评测，`metrics.cer=7.6` 等，5 个维度，`error_mix` 合计 800；
- `008_attribution.json`：主因「交通噪声识别差距」、`share=0.62`、`affected=9`、3 个因素、4 段链路；
- `009_advice.json`：1 条 P0 建议，补样计划 `{count:770, snr:[9,12]}`。

请求：`{"format": "all"}` → 输出 `011_report.md`、`011_report.html`、`011_report.pdf`、`011_report_meta.json`；
`meta.key_numbers` 节选：

```json
{
  "cer": 7.6, "wer": 12.1, "ser": 17.8,
  "samples": 1200, "abnormal": 36,
  "baseline_name": "豆包语音（第三方）",
  "main_cause": "交通噪声识别差距",
  "share_percent": 62.0, "affected": 9, "total_net_error": 9,
  "error_mix": {"sub": 420, "del": 180, "ins": 90, "unk": 110},
  "dimensions": 5, "suggestions": 1
}
```

只给 `007_eval_results.jsonl` 时：`status=partial`，`missing_inputs=["008 差异归因","009 优化建议"]`，
报告仍为八段，整体指标/分场景/噪声/错误形态照常输出，原因分析与优化建议写「未接入」。
