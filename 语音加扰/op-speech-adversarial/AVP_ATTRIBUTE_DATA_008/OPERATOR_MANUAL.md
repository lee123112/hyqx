# AVP_ATTRIBUTE_DATA_008 · 差异归因 · 算子说明书

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

| 文档版本 | 适用算子 | 开发日期 |
|---|---|---|
| v1.0 | 08 差异归因（`avp_attribute_gap`） | 2026-11-02 |

---

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 差异归因 |
| 算子编号 | 08（正式编号）/ AVP_ATTRIBUTE_DATA_008（目录） |
| 英文标识 | `avp_attribute_gap` |
| 所属阶段 | S3 效果分析（1/2） |
| 算子类型 | 规则型（统计归因，无模型、无音频处理） |
| 输入 | 007 落库产物 `007_eval_results.jsonl` |
| 输出 | `008_attribution.json` + `008_alerts.json` + `008_version_compare.json` |
| 服务端口 | 8038（多容器部署）/ 统一池 8030 |
| 依赖 | Python 3.9、PyYAML；不联网、无 GPU |
| 上游 | 07 评测结果接收 |
| 下游 | 09 优化建议生成（读主因/占比/影响条数/证据） |

---

## 二、功能定义

### 2.1 解决什么问题

评测回传只能说明「整体变差了多少」，不能回答「差在哪个维度、值不值得先修」。本算子把整体退化
拆到维度取值上：差值多大、折算成多少条样本、占整体退化的多少、证据强不强，并给出可复核的主因。

### 2.2 输入要求

输入必须是 007 落库产物（字段契约见 007 `DATA_SCHEMA.md`）：至少包含 `eval_id`、`version_id`、
`metrics{cer,wer,ser}`、`dimensions[]`、`error_mix`、`sample_results[]`；`attributable=false`
的记录会被跳过。

### 2.3 业务规则（硬口径）

| 编号 | 规则 |
|---|---|
| R1 | 主因 = 白名单内、可比、`affected>0` 的因素里占比最高且 **≥35%**；并列/不足 → 不设主因 |
| R2 | 影响条数 = 四舍五入(差值pp / 100 × 维度样本数)，负差值保留负号 |
| R3 | 总净多错 = 四舍五入((目标整体指标 − 基准整体指标)/100 × 目标样本数) |
| R4 | 占比 = 影响条数 / 总净多错；总净多错 ≤0 时占比 0、不设主因 |
| R5 | 置信度 = 主因样本 <100 低 / 100–200 中 / >200 高 |
| R6 | 告警 = 单维度差值 **严格大于** 1.5pp |
| R7 | 已回滚版本（`attributable=false`）不参与归因 |
| R8 | 基线找不到就报错，不拿目标自己当基准 |

### 2.4 明确不做

不做评测、不改指标、不生成建议、不写报告、不合并多条记录、不做发布决策。

---

## 三、输出内容描述

### 3.1 `008_attribution.json`

| 契约字段 | 说明 |
|---|---|
| `record_id` | `ATTR-{目标 eval_id}-{基准 eval_id}` |
| `eval_id` | 目标评测记录 |
| `baseline` | 基准类型/ID/模型/版本/指标/样本数 |
| `total_net_error` | 整体净多错条数（可负） |
| `main_cause` | `维度/取值`；无显著主因时 `null` |
| `share` | 主因占比（0–1）；无主因 `null` |
| `affected` | 主因影响条数；无主因 `null` |
| `confidence` | 低/中/高；无主因 `null` |
| `factors` | 每个维度取值的差值、影响条数、占比、可比性、样本级证据 |
| `chain` | record→baseline→dimension→error_type→sample 归因链 |

附加字段：`generated_at`、`metric`、`main_cause_reason`、`main_cause_detail`、`scope`、
`warnings`、`alert_count`、`metrics`/`baseline_metrics`、`error_mix` 等（只增不改）。

### 3.2 `008_alerts.json`

每个告警：维度、取值、差值、阈值、样本数、影响条数、是否在白名单内、可读消息。
条件为差值 **>1.5pp**（等于不告警）。

### 3.3 `008_version_compare.json`

`applicable` 仅在 `baseline_type=version` 时为 true；给出整体三指标差值与各维度差值。
`baseline_type=model` 时只写目标/基准真实数据并标 `applicable=false`，不冒充版本结论。

---

## 四、技术方案

### 4.1 为什么不是「差值排名 + 阈值」

维度退化对整体的贡献 = 差值 × 样本量。只看差值会把「小样本大波动」误判成主因；只看样本量会
忽略实际退化。本算子把差值折算成影响条数，再用占比排名，并叠加 35% 门槛与并列保护：

```text
affected_i = round_half_up(diff_i / 100 × samples_i)       # 影响条数
share_i    = affected_i / total_net_error                  # 占整体退化
main_cause = argmax(share_i)  if max_share ≥ 0.35 and 唯一 else null
```

这样「差值 7.3pp、120 条样本」与「差值 4.8pp、60 条样本」的贡献差异能被正确区分
（9 条 vs 3 条），而不是按差值顺序拍脑袋。

### 4.2 基线选择的确定性

- 跨模型：`baseline_id` 必填；同版本优先，避免版本差异混入模型对比；
- 跨版本：显式 `baseline_id` 优先，否则按 `version_id` 字典序推定上一版本；
- 候选多条：样本数最多者（同数按 `eval_id`），并把选择理由写进 `warnings`。

### 4.3 证据强度

对主因维度，用样本级 `perturb` 快照回溯错误样本，统计：

- 错误形态构成（替换/删除/插入/未识别）；
- SNR 分桶集中度（默认 0/5/10/15/20/30 dB 边界）；
- 错误率的 95% 置信区间半宽 `1.96·√(p(1−p)/n)·100`。

只有样本能完整回溯（`status=exact`/`partial`）才给区间；`unavailable` 时明确降级，
不拿不完整样本估区间——这是「证据强度」段可信的前提。

### 4.4 可复核性

所有中间量（差值、影响条数、占比、置信档、错误构成、CI、SNR 桶、样本 ID）都写进
`factors` 与 `chain`；审查者可从整体指标一路复算到样本，结论不依赖黑盒。

---

## 五、技术实现

| 项 | 说明 |
|---|---|
| 入口 | `processor.run(ctx) -> dict`，由 `inference.py` 路由 |
| 配置 | 阈值、白名单、指标、分桶、精度全部来自 `configs/config.yaml` |
| 输入 | JSONL / JSON 数组 / JSON 对象，目录优先 `007_eval_results.jsonl` |
| 校验 | 契约字段 + 指标存在性；缺字段报 `invalid_jsonl:`，不回填 |
| 计算 | half-up 舍入；影响条数/占比/主因/置信度按 R1–R5 |
| 证据 | `sample_results` 回溯 + SNR 分桶 + 95% CI |
| 输出 | 三个 JSON 原子写；统计与 `result` 同步返回服务层 |
| 日志 | 读取/跳过回滚/警告/归因结论/写产物，单行 ≤160 字符 |
| 确定性 | 无随机、无网络、无模型；除 `generated_at` 外同输入同输出 |

---

## 六、资源要求

| 项 | 要求 |
|---|---|
| CPU | 单核（实测 11.0 ms / 3 条记录、630 条样本级结果） |
| 内存 | < 10 MB（实测 Python 峰值 0.66 MB） |
| 磁盘 | 文本输入输出；示例输出约 14 KB |
| 网络 / GPU | 不需要 |
| 系统 | Windows / Linux |

---

## 七、性能指标

| 指标 | 实测值 | 条件 |
|---|---|---|
| 主因判定与真值一致率 | **1.0000（11/11）** | 12 条构造真值用例（含 1 条参数错误用例） |
| 主因影响条数误差 | **mean 0 / max 0 条** | 7 条主因用例 |
| 总净多错误差 | **mean 0 / max 0 条** | 11 条 |
| 告警条数准确率 | **1.0000（11/11）** | 含 1.5pp 严格边界用例 |
| 单次运行延迟 | **11.0 ms（中位）** | 3 条记录 / 630 样本级结果，5 次 |
| 单元测试 | **36 passed / 2.61 s** | `pytest tests/ -q` |

指标命令：`cd AVP_ATTRIBUTE_DATA_008 && python utils\eval_metrics.py`。

---

## 八、输入输出示例

### 8.1 输入（`007_eval_results.jsonl` 第 1 条，节选）

```json
{"eval_id": "EV-EVT-E2E-001", "external_task_id": "EVT-E2E-001", "version_id": "V-20261101-01",
 "model": "自研-ASR-v2", "metrics": {"cer": 8.0, "wer": 10.0, "ser": 12.0},
 "dimensions": [{"dimension": "噪声", "value": "人群嘈杂", "cer": 12.4, "wer": 15.0, "ser": 18.0,
                 "samples": 120, "ratio": 0.4}, "..."],
 "error_mix": {"sub": 24, "del": 6, "ins": 4, "unk": 2},
 "sample_results": [{"sample_id": "S0000", "error_type": "sub",
                     "perturb": {"dimension": "噪声", "value": "人群嘈杂", "snr_db": 6.0,
                                 "speed": null, "asset": null}}, "..."],
 "attributable": true, "sample_count": 300, "sample_results_mode": "full"}
```

### 8.2 输出（`008_attribution.json` 关键字段）

```json
{
  "record_id": "ATTR-EV-EVT-E2E-001-EV-EVT-E2E-001-BL",
  "eval_id": "EV-EVT-E2E-001",
  "baseline": {"type": "model", "id": "豆包 ASR", "eval_id": "EV-EVT-E2E-001-BL",
               "version_id": "V-20261101-01", "model": "豆包 ASR",
               "metrics": {"cer": 5.0, "wer": 6.0, "ser": 7.0}, "sample_count": 300},
  "total_net_error": 9,
  "main_cause": "噪声/人群嘈杂",
  "share": 1.0,
  "affected": 9,
  "confidence": "中",
  "factors": [
    {"dimension": "噪声", "value": "人群嘈杂", "in_scope": true, "samples": 120, "ratio": 0.4,
     "target_metric": 12.4, "baseline_metric": 5.1, "diff_pp": 7.3, "affected": 9,
     "share": 1.0, "confidence": "中", "comparable": true, "baseline_present": true,
     "sample_trace": {"status": "exact", "error_samples": 18, "error_rate": 0.15,
                      "ci_half_pp": 6.39, "error_counts": {"sub": 12, "del": 3, "ins": 2, "unk": 1},
                      "top_snr_bucket": "5–10", "sample_ids": ["S0000", "..."],
                      "sample_ids_truncated": 0}}
  ],
  "chain": ["record", "baseline", "dimension", "error_type", "sample"],
  "main_cause_reason": "主因：噪声/人群嘈杂 影响 9 条，占比 100.00% ≥ 35.00%，置信度 中"
}
```

### 8.3 输出（`008_alerts.json` 节选）

```json
{"threshold_pp": 1.5, "count": 2,
 "alerts": [{"dimension": "噪声", "value": "人群嘈杂", "diff_pp": 7.3, "samples": 120,
             "affected": 9, "in_scope": true,
             "message": "噪声/人群嘈杂 CER 差值 7.30pp > 告警阈值 1.50pp"}]}
```

### 8.4 无主因示例（`main_cause=null`）

```json
{"main_cause": null, "share": null, "affected": null, "confidence": null,
 "main_cause_reason": "无显著主因：最高占比 11.11% < 阈值 35.00%（噪声/A）"}
```

### 8.5 CLI 冒烟

```powershell
cd <包根>
python inference.py --operator 8 `
  --input AVP_ATTRIBUTE_DATA_008\dataset\sample\007_eval_results.jsonl `
  --params-file AVP_ATTRIBUTE_DATA_008\dataset\sample\cli_params.json --unique-id cli_008
```

真实输出关键字段：`code=0`、`status=success`、`total_net_error=9`、`main_cause=噪声/人群嘈杂`、
`share=1.0`、`affected=9`、`confidence=中`、`alerts=2`、`skipped_not_attributable=1`。
