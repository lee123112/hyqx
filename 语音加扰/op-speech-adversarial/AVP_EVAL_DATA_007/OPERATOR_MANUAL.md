# AVP_EVAL_DATA_007 · 评测结果接收 · 算子说明书

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

| 文档版本 | 适用算子 | 开发日期 |
|---|---|---|
| v1.0 | 07 评测结果接收（`avp_ingest_eval_result`） | 2026-11-02 |

---

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 评测结果接收 |
| 算子编号 | 07（正式编号）/ AVP_EVAL_DATA_007（目录） |
| 英文标识 | `avp_ingest_eval_result` |
| 所属阶段 | S2 评测执行（1/1，阶段末） |
| 算子类型 | 规则型（数据算子，无模型、无音频处理） |
| 输入 | 外部「数据集评测」回传的评测结果 JSONL |
| 输出 | 落库结果 JSONL + 解析失败 JSONL + 汇总报告 JSON |
| 服务端口 | 8037（多容器部署时）、统一池服务 8030 |
| 依赖 | Python 3.9、PyYAML；不联网、无 GPU |
| 上游 | 数据集评测（外部系统）主动推送 |
| 下游 | 08 差异归因（只消费 `attributable=true` 的记录） |

---

## 二、功能定义

### 2.1 解决什么问题

外部评测系统回传的结果是跨系统数据，字段缺陷与版本失效会污染下游归因结论。本算子承担
**门禁 + 归一化**职责：只有字段完整、类型正确、指标在合法区间、版本 ID 真实存在的记录才允许落库；
其余逐条记录失败原因，供对方修正后重推。

### 2.2 输入要求

一条记录 = 一个「评测任务 × 模型 × 版本」的评测结果，必须包含六项：
`external_task_id`、`version_id`、`metrics{cer,wer,ser}`、`dimensions[]`、
`error_mix{sub,del,ins,unk}`、`sample_results[]`。字段级定义见 `dataset/DATA_SCHEMA.md`。

### 2.3 业务规则（硬口径）

| 编号 | 规则 | 处理方式 |
|---|---|---|
| R1 | 版本 ID 不存在于版本库 | **整条解析失败、不落库** |
| R2 | 版本存在且已回滚 | 落库，`attributable=false` |
| R3 | 字段缺失/类型错/数值越界/JSON 坏 | 整条解析失败 |
| R4 | 同一 `external_task_id` 重推 | 按 `last_wins` 覆盖，计 `deduplicated` |
| R5 | 样本级缺 `error_type` | 由 `ref_text`/`hyp_text` 字符级编辑脚本推导 |
| R6 | 比例/样本数/error_mix 互相矛盾 | 只告警，不改数、不阻断 |
| R7 | 全部记录失败 | 输出 3 个文件、结论 `failed`，供排查 |

### 2.4 明确不做

不执行评测、不做归因、不生成建议、不改指标数字、不补造缺失字段、不联网、不接大模型。

---

## 三、输出内容描述

| 产物 | 键名 | 说明 |
|---|---|---|
| `007_eval_results.jsonl` | `results` | 归一化后的落库记录；一行一条，字段契约见 DATA_SCHEMA §3.1 |
| `007_parse_failed.jsonl` | `parse_failed` | 解析失败明细：行号、原因码、问题列表、原始行预览 |
| `007_eval_report.json` | `report` | 统计、版本库来源、检查规则、失败原因分布、告警、结论 |

关键统计字段：

| 字段 | 含义 |
|---|---|
| `input_lines` | 输入总行数（含空行） |
| `valid_records` | 落库记录数（去重后） |
| `parse_failed` | 解析失败条数 |
| `deduplicated` | 重推去重条数 |
| `attributable` / `not_attributable` | 可归因 / 已回滚不可归因条数 |
| `total_samples` | 落库记录样本总数 |
| `dimension_entries` / `sample_result_entries` | 维度得分条数 / 样本级结果条数 |
| `warnings` | 一致性告警条数 |
| `conclusion` | `success` / `partial` / `failed` |

`data.status` 与 `conclusion` 同步；`/predict` 的 `code` 只在**服务/参数级错误**时为 1，
批量数据结论（全失败）仍返回 `code=0`，避免把「数据不合格」误报成「服务不可用」。

---

## 四、技术方案

### 4.1 总体思路

「三级校验 + 版本库合并 + 字符级编辑推导 + 一致性告警」。全部为确定性规则，无模型、无随机，
同一输入每次得到同一结论，便于跨系统对账。

### 4.2 为什么不是纯阈值/纯存在性检查

- **纯存在性检查**只能发现「字段有没有」，发现不了指标越界、版本失效、比例与样本数矛盾，
  坏数据会带着错误比例进入归因；
- **纯阈值判断**（例如只看 CER 是否超限）会把数据质量问题和模型效果问题混在一起；
- 本算子把「数据结构正确性」与「业务效果」分开：本算子只判前者，效果判断留给 008。
  这样失败原因可以精确定位到字段，对方能一次修正全部问题（`problems[]` 一次列全）。

### 4.3 关键算法：字符级最小编辑脚本（Levenshtein DP + 回溯）

样本缺 `error_type` 时，用动态规划求 `ref_text` 与 `hyp_text` 的最小编辑距离矩阵，再从右下角回溯
得到具体编辑操作序列，统计三类操作次数：

```text
dp[i][j] = min(dp[i-1][j-1] + cost(ref[i], hyp[j]),   # 替换/匹配
               dp[i-1][j] + 1,                        # 删除
               dp[i][j-1] + 1)                        # 插入
```

回溯后按「替换 > 删除 > 插入」的优先级取主导形态；识别结果为空判「未识别」，完全一致判「正确」。
复杂度 O(|ref|·|hyp|)（中文按字符计），单条句长几十字符，实际开销可忽略。
相比「字符串相等就判对」的做法，它能区分替换型错误与漏识/多识，为下游错误形态解释提供依据。

### 4.4 版本库合并

把 `known_versions`、`rolled_back_versions` 与 006 注册表文件（单对象/数组/`{versions:[...]}`/
`{version_id:{...}}` 四种形态）合并为 `{version_id: status}`：

- 注册表读不到 → **直接失败**（`missing_file`），不静默降级成空库；
- 注册表「已回滚」状态与显式回滚名单取并集；
- 仅存在于 `known_versions` 的版本推定「已发布」（能被评测拉取说明当时已发布）。

---

## 五、技术实现

| 项 | 说明 |
|---|---|
| 入口 | `processor.run(ctx: OperatorContext) -> dict`，由 `inference.py` 按 operator 路由 |
| 配置 | 全部阈值/枚举来自 `configs/config.yaml`（`common.config.load`，缺键 fail fast） |
| IO | `common.iofs` 原子写 JSON/JSONL；行级读取自实现以保留「坏行」信息 |
| 日志 | `common.logger.RunLog`：开始/输入/读取/校验/进度/输出/告警/部分完成/错误，单行 ≤160 字符 |
| 返回 | `{outputs, statistics, result, status}`；`outputs` 三键与 `map.json` 一致 |
| 代码结构 | 参数校验 → 版本库 → 输入读取 → 记录校验（结构/版本/明细）→ 去重 → 落盘 → 报告/日志 |
| 无魔法数字 | 指标区间、ID 长度、样本下限、占比容差、去重策略、日志步长全部配置化 |
| 确定性 | 无随机、无网络、无模型；除 `received_at` 外同输入同输出 |

---

## 六、资源要求

| 项 | 要求 |
|---|---|
| CPU | 单核即可（实测单次 21 ms，630 条样本） |
| 内存 | < 10 MB（实测 Python 峰值 1.28 MB / 630 条样本） |
| 磁盘 | 输入/输出均文本；示例输入 97.5 KB → 输出约 124 KB |
| 网络 | 不需要 |
| GPU | 不需要 |
| 系统 | Windows / Linux 均可（路径统一用 pathlib） |

---

## 七、性能指标

| 指标 | 实测值 | 测试条件 |
|---|---|---|
| 字段校验准确率 | **1.0000（19/19）** | 构造真值集 19 条（11 无效 + 8 有效行） |
| 解析失败识别率 | **1.0000（11/11）** | 同上 |
| 失败原因命中率 | **1.0000（11/11）** | 同上 |
| 无效记录落库数 | **0** | 版本不存在等无效记录反查落库结果 |
| 回滚标记准确率 | **1.0000（1/1）** | 已回滚版本 `attributable=false` |
| 重推去重准确率 | **1.0000（1/1）** | `last_wins` + `deduplicated` 计数 |
| 一致性告警召回 | **1.0000（2/2）** | ratio 偏差 / error_mix 不符 |
| 单次运行延迟 | **21.3 ms（中位）** | 5 行 / 630 样本 / 97.5 KB，5 次重复 |
| 单元测试 | **36 passed / 2.64 s** | `pytest tests/ -q` |

指标由 `utils/eval_metrics.py` 在构造真值集上真实运行得到，命令：
`cd AVP_EVAL_DATA_007 && python utils\eval_metrics.py`。

---

## 八、输入输出示例

### 8.1 输入（`dataset/sample/external_eval_results.jsonl` 第 1 条，节选）

```json
{"external_task_id": "EVT-E2E-001", "version_id": "V-20261101-01", "model": "自研-ASR-v2",
 "metrics": {"cer": 8.0, "wer": 10.0, "ser": 12.0},
 "dimensions": [
   {"dimension": "噪声", "value": "人群嘈杂", "cer": 12.4, "wer": 15.0, "ser": 18.0, "samples": 120, "ratio": 0.4},
   {"dimension": "噪声", "value": "交通噪声", "cer": 5.6, "wer": 7.0, "ser": 8.0, "samples": 60, "ratio": 0.2},
   {"dimension": "语速", "value": "1.2x", "cer": 10.0, "wer": 12.0, "ser": 15.0, "samples": 60, "ratio": 0.2},
   {"dimension": "语速", "value": "1.0x", "cer": 3.0, "wer": 4.0, "ser": 5.0, "samples": 60, "ratio": 0.2}],
 "error_mix": {"sub": 24, "del": 6, "ins": 4, "unk": 2},
 "sample_results": [{"sample_id": "S0000", "error_type": "sub",
                     "perturb": {"dimension": "噪声", "value": "人群嘈杂", "snr_db": 6.0, "speed": null, "asset": null}}, "..."]}
```

### 8.2 输出（`007_eval_results.jsonl` 第 1 条，节选）

```json
{"eval_id": "EV-EVT-E2E-001", "external_task_id": "EVT-E2E-001", "version_id": "V-20261101-01",
 "model": "自研-ASR-v2", "metrics": {"cer": 8.0, "wer": 10.0, "ser": 12.0},
 "dimensions": [{"dimension": "噪声", "value": "人群嘈杂", "cer": 12.4, "wer": 15.0, "ser": 18.0,
                 "samples": 120, "ratio": 0.4}, "..."],
 "error_mix": {"sub": 24, "del": 6, "ins": 4, "unk": 2}, "sample_results": ["..."],
 "attributable": true, "received_at": "2026-11-02T14:07:19+08:00", "sample_count": 300,
 "sample_results_mode": "full", "version_status": "已发布", "warnings": []}
```

### 8.3 解析失败示例（`007_parse_failed.jsonl`）

```json
{"source_line": 4, "external_task_id": "EVT-E2E-003", "version_id": "V-NOT-EXIST",
 "reason_code": "version_not_found",
 "reason": "版本 ID V-NOT-EXIST 不在版本库中（版本库来源：param_list）",
 "problems": [{"code": "version_not_found", "field": "version_id", "message": "版本 ID V-NOT-EXIST 不在版本库中（版本库来源：param_list）"}],
 "raw_preview": "{\"external_task_id\": \"EVT-E2E-003\", \"version_id\": \"V-NOT-EXIST\", ...}"}
```

### 8.4 CLI 冒烟

```powershell
cd <包根>
python inference.py --operator 7 `
  --input AVP_EVAL_DATA_007\dataset\sample\external_eval_results.jsonl `
  --params-file AVP_EVAL_DATA_007\dataset\sample\cli_params.json --unique-id cli_007 --is-last
```

真实输出关键字段：`code=0`、`status=partial`、`valid_records=3`、`parse_failed=2`、
`attributable=2`、`not_attributable=1`、`total_samples=630`、三个产物路径齐全。
