# AVP_ADVICE_DATA_009 · 优化建议生成 · 算子说明书

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

| 文档版本 | 适用算子 | 开发日期 |
|---|---|---|
| v1.0 | 09 优化建议生成（`avp_suggest_optimization`） | 2026-11-02 |

---

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 优化建议生成 |
| 算子编号 | 09（正式编号）/ AVP_ADVICE_DATA_009（目录） |
| 英文标识 | `avp_suggest_optimization` |
| 所属阶段 | S3 效果分析（2/2，阶段末） |
| 算子类型 | 规则型（模板回填，无模型、无 LLM） |
| 输入 | 008 归因产物 `008_attribution.json`（必需）+ `008_alerts.json`（可选） |
| 输出 | `009_advice.json` |
| 服务端口 | 8039（多容器部署）/ 统一池 8030 |
| 依赖 | Python 3.9、PyYAML；不联网、无 GPU |
| 上游 | 08 差异归因 |
| 下游 | 11 报告中心（直接引用建议文案） |

---

## 二、功能定义

### 2.1 解决什么问题

归因结论已指出「哪个维度退化、占整体退化的多少」，但评测/算法团队还需要「先修哪个、补多少样本、
怎么判定有效」。本算子把归因数字转成 1–3 条带优先级的可执行建议，并保证所有数字可追溯。

### 2.2 输入要求

必须是 008 的 `008_attribution.json`（至少含 `eval_id`、`metric`、`total_net_error`、`factors[]`）；
`008_alerts.json` 可选，用于取 008 实际告警阈值。输入不是 008 产物 → `invalid_jsonl:`。

### 2.3 业务规则（硬口径）

| 编号 | 规则 |
|---|---|
| R1 | 建议 1–3 条；`min_suggestions ≤ 条数 ≤ max_suggestions` |
| R2 | 优先级：P0 = 差值≥3pp **或** 占比≥30%；P1 = 差值≥1pp；其余 P2 |
| R3 | 退化候选 = 白名单内 + 可比 + 差值>0；覆盖候选 = 样本低于 100 条 |
| R4 | 补样条数 = max(1, half_up(缺口 × 1.5))；缺口口径按建议类型固定 |
| R5 | 每条建议固定四段：事实 / 证据强度 / 解释 / 验证方式 + 补样计划 |
| R6 | 文案不接大模型；模板占位符缺一个就失败，不输出半截文案 |
| R7 | 没有可建议维度 → 整体兜底 1 条，不硬造维度结论 |
| R8 | 008 主因并列/缺失时透传 `main_cause=null`，按维度因素分别建议 |

### 2.4 明确不做

不重算归因、不改 008 数字、不评测、不写报告、不自动调参/改模型、不做发布决策。

---

## 三、输出内容描述

### 3.1 根字段

| 字段 | 说明 |
|---|---|
| `task_id` | 外部任务号（缺失用 eval_id） |
| `generated_at` | 生成时间（+08:00，秒级） |
| `suggestions` | 1–3 条建议 |
| 附加 | `record_id`/`eval_id`/`baseline_eval_id`/`metric`/`total_net_error`/`main_cause`/`share`/`affected`/`confidence`/`suggestion_count`/`priority_counts`/`alerts`/`source_file`/`warnings` |

### 3.2 单条建议

| 字段 | 说明 |
|---|---|
| `title` | ≤20 字，超长截断并告警 |
| `dimension` / `value` | 维度与取值；整体兜底为 `overall` / null |
| `priority` | P0 / P1 / P2 |
| `fact` | 事实：差值、影响条数、占比（兜底：整体差值、净多错） |
| `evidence` | 证据强度：样本数、错误数/率、95% CI 半宽、置信度 |
| `explanation` | 解释：错误形态构成、SNR 分桶集中度 |
| `verification` | 验证方式：补样条数、目标回落幅度 |
| `sample_plan` | 补样计划：count/gap/factor/formula/projected_samples/purpose 等 |
| 附加 | `source`（eval_id、factor_index 等）、`evidence_source`（来源标记） |

### 3.3 建议类型

| 类型 | `sample_plan.purpose` | 事实段口径 |
|---|---|---|
| 退化验证 | `degradation_verification` | 差值、影响条数、占比 |
| 覆盖扩量 | `coverage_expansion` | 样本数、占比、差值、样本缺口 |
| 整体兜底 | `overall_review` | 整体指标差值、总净多错 |

---

## 四、技术方案

### 4.1 为什么用「规则 + 模板」而不是 LLM

- 建议里的「补 14 条」「目标回落 0.3pp」要能进排期与验收，必须逐项可追溯；
- LLM 生成容易引入未出现过的数字或改变归因结论，评审时无法复算；
- 固定模板 + 真实数字回填，保证文案与 008 产物严格一致；表达力不足由模板库迭代解决，而不是放开生成。

### 4.2 选对象的确定性

1. 退化候选按「优先级 → 差值 → 占比 → 维度 → 取值」排序，避免同分随机；
2. 覆盖候选按「样本占比升序 → 差值降序」选最低占比维度，优先补最薄弱的覆盖；
3. 主因并列时两个维度都保留（各一条 P0），不按字典序硬选一个；
4. 白名单外、不可比维度一律不生成建议，只可能进整体兜底。

### 4.3 补样口径

补样是「验证所需的最小追加样本」估算，不是统计功效计算：

- 退化维度：缺口 = 影响条数（多错样本规模），再乘 1.5 留余量；
- 覆盖维度：缺口 = 门槛 100 − 现有样本；
- 整体兜底：缺口 = |总净多错|；
- 下限 1 条，half-up 舍入与 007/008 一致，所有中间量写入 `sample_plan`。

### 4.4 证据强度

- 有样本级 trace：错误数、错误率、95% CI 半宽、错误形态构成、SNR 分桶；
- trace 不可用：降级为「样本数 + 置信度」，明确写「样本级证据不足，未估算置信区间」；
- 错误构成缺失：回落到 008 的全局 `error_mix`，并在 `evidence_source` 标 `record_error_mix`；
- 结论不因证据不足而拔高：样本不足只建议补样，不判定主因。

---

## 五、技术实现

| 项 | 说明 |
|---|---|
| 入口 | `processor.run(ctx) -> dict`，由 `inference.py` 路由 |
| 配置 | 阈值、模板、精度、样本门槛全部来自 `configs/config.yaml` |
| 输入 | 008 attribution JSON；同目录 alerts 可选；传目录时优先 `008_attribution.json` |
| 校验 | 参数 7 项 + 输入必填字段；非法报 `invalid_params:` / `invalid_jsonl:` |
| 计算 | 候选分类 → 优先级 → 排序取条 → 补样公式（half-up） |
| 文案 | 模板 `format(**numbers)`，占位符缺失即失败 |
| 输出 | 单个 JSON 原子写；统计与 `result` 同步返回 |
| 日志 | 输入/校验/读取/建议结论/输出，单行 ≤160 字符 |
| 确定性 | 无随机、无网络、无 LLM；除 `generated_at` 外同输入同输出 |

---

## 六、资源要求

| 项 | 要求 |
|---|---|
| CPU | 单核（实测 4.3 ms / 1 条归因结果、4 个因素） |
| 内存 | < 10 MB（实测 Python 峰值 0.07 MB） |
| 磁盘 | 输出约 5.3 KB / 3 条建议 |
| 网络 / GPU | 不需要 |
| 系统 | Windows / Linux |

---

## 七、性能指标

| 指标 | 实测值 | 条件 |
|---|---|---|
| 建议条数合规率 | **1.0000（9/9）** | 17 条构造真值用例（其余为参数/输入错误） |
| 优先级映射准确率 | **1.0000（9/9）** | 含 P0 双条件与 P1/P2 边界 |
| 数字可追溯率 | **1.0000（263/263）** | 建议文本中的每个数字 |
| 单次运行延迟 | **4.3 ms（中位）** | 1 条归因结果、4 个维度因素，5 次 |
| 单元测试 | **43 passed / 2.71 s** | `pytest tests/ -q` |

指标命令：`cd AVP_ADVICE_DATA_009 && python utils\eval_metrics.py`。

---

## 八、输入输出示例

### 8.1 输入（`008_attribution.json` 节选，由 008 实跑生成）

```json
{
  "eval_id": "EV-EVT-E2E-001",
  "external_task_id": "EVT-E2E-001",
  "metric": "cer",
  "total_net_error": 9,
  "main_cause": "噪声/人群嘈杂",
  "share": 1.0,
  "affected": 9,
  "confidence": "中",
  "alert_count": 2,
  "alert_threshold_pp": 1.5,
  "metrics": {"cer": 8.0, "wer": 10.0, "ser": 12.0},
  "baseline_metrics": {"cer": 5.0, "wer": 6.0, "ser": 7.0},
  "factors": [
    {"dimension": "噪声", "value": "人群嘈杂", "samples": 120, "ratio": 0.4,
     "in_scope": true, "comparable": true, "diff_pp": 7.3, "affected": 9, "share": 1.0,
     "confidence": "中",
     "sample_trace": {"status": "exact", "error_samples": 18, "error_rate": 0.15,
                      "ci_half_pp": 6.39,
                      "error_counts": {"sub": 12, "del": 3, "ins": 2, "unk": 1},
                      "top_snr_bucket": "5–10"}},
    {"dimension": "语速", "value": "1.2x", "samples": 60, "in_scope": true, "comparable": true,
     "diff_pp": 4.8, "affected": 3, "share": 0.3333, "confidence": "低", "sample_trace": {"...": "..."}}
  ]
}
```

### 8.2 输出（`009_advice.json` 第 1 条建议）

```json
{
  "task_id": "EVT-E2E-001",
  "generated_at": "2026-11-02T14:33:51+08:00",
  "suggestions": [
    {
      "title": "人群嘈杂：CER 升高7.30pp",
      "dimension": "噪声",
      "value": "人群嘈杂",
      "priority": "P0",
      "fact": "该维度 噪声/人群嘈杂 的 CER 较基准升高 7.30pp，折算多错约 9 条，占整体净多错的 100.00%。",
      "evidence": "证据强度：该维度 120 条样本，错误样本 18 条（错误率 15.00%），95% 置信区间半宽 ±6.39pp，置信度 中。",
      "explanation": "错误形态构成：替换 12 条、删除 3 条、插入 2 条、未识别 1 条；最集中在 SNR 5–10 dB 分桶。",
      "verification": "补样 14 条（缺口 9 条 × 系数 1.5）复测，目标 CER 回落 ≥0.30pp 视为有效。",
      "sample_plan": {
        "count": 14, "gap": 9, "factor": 1.5,
        "formula": "count = max(min_count, round_half_up(gap × factor))",
        "purpose": "degradation_verification", "projected_samples": 134,
        "min_samples_per_value": 100, "min_count": 1, "target_pp_drop": 0.3,
        "dimension": "噪声", "value": "人群嘈杂", "snr_bucket": "5–10"
      },
      "source": {"eval_id": "EV-EVT-E2E-001", "baseline_eval_id": "EV-EVT-E2E-001-BL",
                 "factor_index": 0, "metric": "cer", "total_net_error": 9,
                 "main_cause": "噪声/人群嘈杂", "purpose": "degradation_verification"},
      "evidence_source": "sample_trace"
    }
  ],
  "priority_counts": {"P0": 2, "P1": 0, "P2": 1}
}
```

### 8.3 无主因 / 兜底示例

```json
{
  "suggestions": [
    {"title": "整体指标复核", "dimension": "overall", "value": null, "priority": "P0",
     "fact": "整体 CER 较基准 升高 4.50pp，总净多错 9 条，未定位到可比维度主因。",
     "evidence": "证据强度：整体 200 条样本按维度拆解后无可比项，暂不能把退化归到单一维度。",
     "explanation": "该结论只覆盖整体指标，不指向具体维度；维度覆盖补齐前，不建议直接改模型或调参。",
     "verification": "补样 14 条（缺口 9 × 系数 1.5）复核整体指标与维度覆盖后再判定主因。",
     "sample_plan": {"count": 14, "gap": 9, "purpose": "overall_review", "...": "..."}}
  ],
  "warnings": ["008 未给出主因（无显著主因：…），009 按维度因素降级生成建议"]
}
```

### 8.4 CLI 冒烟

```powershell
cd <包根>
python inference.py --operator 9 `
  --input AVP_ADVICE_DATA_009\dataset\sample\008_attribution.json `
  --params-file AVP_ADVICE_DATA_009\dataset\sample\cli_params.json `
  --unique-id cli_009 --is-last
```

真实输出关键字段：`code=0`、`status=success`、`suggestions=3`、`priorities=[P0,P0,P2]`、
`main_cause=噪声/人群嘈杂`、`total_net_error=9`、`outputFile.main=advice`。
