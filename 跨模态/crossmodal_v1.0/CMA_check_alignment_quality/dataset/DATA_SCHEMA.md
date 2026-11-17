# 算子 #20「质检对齐产物」数据结构契约

> 环节：表1 第 4 环节「数据质量评估」（`stageId=S4`）　正式编号：**20**　算子名：`alignment_qc` / `v1.0.0`
> 本文只描述**本算子真实读写**的字段与产物，字段名与 `utils/alignment_qc.py` 常量、`output_smoke` 实测产物逐字一致。

## 1. 输入

### 1.1 目录与入口

| 项 | 说明 |
|---|---|
| 任务输入目录 | `OUTPUT_DIR/<uniqueId>/input/`（HTTP 上传解包后）或 `--input` 指定文件/目录 |
| 运行清单 | 任务目录内 `manifest.json`（`crossmodal-manifest-v1`），`entrypoint` 指向主 JSONL |
| 主记录 | **UTF-8 JSONL**，一行一条对象；空行计入 `blank_lines`，非法 JSON/非对象计入 `rejects` |

### 1.2 记录字段（上游保留，本算子只读）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sample_id` | string | 是 | 样本唯一标识；缺失 → 原始隔离（`original_missing_field`） |
| `text` | string \| object | 否 | 文本模态。object 取 `content`；字符串直接作为内容 |
| `image` | string \| object \| array | 否 | 图像模态。object 取 `path`；数组为多图 |
| `video` | string \| object | 否 | 视频模态。object 取 `path`；`frames[].path` 为抽帧引用 |
| `audio` | string \| object | 否 | 音频模态。object 取 `path` |
| `qc` | object | 否 | 上游质量结论，复用入口（见 §1.3） |
| `processing` | object | 否 | 上游各算子命名空间，本算子**不覆盖**，仅追加 `processing["20"]` |
| `metadata` | object | 否 | 批次/来源等，原样透传 |
| 其他未知字段 | any | 否 | **原样透传**，不静默丢弃 |

> 模态引用路径必须是**包内相对路径**（如 `media/ok_a.png`）；`http(s)://`、绝对路径、`..`、反斜杠一律判 `unsupported_media_path` → 原始隔离。

### 1.3 上游质量结论复用点（`qc.*`）

| 指标 | 优先读取路径 | 无上游时的行为 |
|---|---|---|
| 困惑度 PPL | `qc.text_fluency.ppl` | 命中 10% 子集才本地补测，否则 `insufficient_evidence` |
| BRISQUE | `qc.media_quality.brisque` / `image.brisque` | 同上 |
| SNR | `qc.media_quality.snr_db` / `audio.snr_db` | 同上 |
| 反向验证 | `qc.reverse_verify`（`passed`/`score`/`check_status`） | 无上游 → 整池本地 CLIP 反向检索（权重缺失则 `degraded`） |

## 2. 输出

### 2.1 目录布局

```text
OUTPUT_DIR/<uniqueId>/alignment_qc/<runId>/
  20_alignment_qc.jsonl              # 主清单（通过 + flagged + skipped）
  20_quality_checks.jsonl            # 检查明细（通过/隔离**都**有）
  20_reverse_verification.jsonl      # 图文反向检索明细
  20_original_quarantine.jsonl       # 原始隔离台账【原始文件-*】
  20_sample_quarantine.jsonl         # 样本隔离台账【样本-语义不通】
  20_flagged.jsonl                   # 旁路 flagged 子集（不拦样本，供 #24 抽检）
  20_rejects.jsonl                   # 行级异常台账
  20_quality_report.json             # 待回写质量报告（status=pending_writeback）
  20_supplement_list.json            # 偏科待补清单（不整批隔离）
  manifest.json                      # run 运行清单（crossmodal-manifest-v1）
  run.log                            # 逐行运行日志（UTF-8）
  media/**                           # 按原相对路径复制的被引用媒体
```

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.2 主清单 `20_alignment_qc.jsonl`

每条 = **原记录原样** + 追加命名空间：

```jsonc
{
  "sample_id": "vlm-0001",
  "text": { "content": "..." },
  "image": { "path": "media/ok_a.png" },
  "processing": {
    "20": {
      "operator": "alignment_qc",
      "operator_number": 20,
      "operator_version": "v1.0.0",
      "stage_id": "S4",
      "run_stage": "data_quality_assessment",
      "status": 1,                      // 1=通过/待复核/跳过，0=隔离/失败
      "process_status": "success",      // success | pending_review | isolated | failed | skipped
      "action": "accept",
      "decision": "accepted",           // accepted | flagged | sample_quarantine | origin_quarantine | skipped
      "label": "样本-质检通过",
      "destination": "质量报告待回写",
      "reason_code": "quality_check_passed",
      "reason": "通过文件可打开/模态/解码/静音/文本与指标检查",
      "modalities": ["text", "image"],
      "route": "image+text",
      "checks": { "<检查项>": { "status": "...", "reason": null, "evidence": {...} } },
      "metrics": { "ppl": {...}, "brisque": {...}, "snr": {...} },
      "reverse_verify": { ... },
      "issues": [],
      "checked_at": "2026-10-07T15:08:48+08:00",
      "source_reference": "operator_20"
    }
  }
}
```

> 记录级 `processing["20"].status` 为 v4.2 int（1=通过/待复核/跳过，0=隔离/失败），原字符串
> 迁入 `process_status`；`checks.<项>.status` 仍为检查项字符串（`ok`/`failed`/…）。

### 2.3 检查项与状态字典

| `checks` 键 | 检查内容 | 来源 |
|---|---|---|
| `text_nonempty` | 文本去空白后字符数 ≥ `min_text_chars` | 记录 |
| `file_openable` | 每个媒体引用可打开（Pillow/OpenCV/wave） | 本地探测 |
| `min_modalities` | 有效模态数 ≥ 2 | 记录 + 探测 |
| `video_decodable` | 视频可解码（最多 `max_video_frames_probe` 帧） | 本地探测 |
| `audio_non_silent` | 音频解码后 RMS > `silence_rms_threshold` | 本地探测 |
| `ppl` / `brisque` / `snr` | 复用上游；无上游按 10% 子集本地补测 | 上游 / 本地 |
| `reverse_verify` | 图文 CLIP 反向检索一致性 | 上游 / 本地 |

状态取值（**全部真实计算，无固定/随机值**）：

| 状态 | 含义 |
|---|---|
| `ok` | 通过 |
| `failed` | 明确不通过（计入隔离/flagged） |
| `not_applicable` | 模态不适用，**不计失败** |
| `insufficient_evidence` | 证据不足（无上游且未命中补测子集 / 无本地后端），**不伪造分数** |
| `pending_pool` | 已入池待反向检索 |

`metrics.*` 分桶计数（互斥，不重复加总）：`upstream` / `local` / `not_applicable` / `insufficient_evidence`。

### 2.4 台账与报告

| 文件 | 关键字段 |
|---|---|
| `20_original_quarantine.jsonl` | `sample_id`/`line`/`reason`(如 `media_missing`)/`detail`/`label`(`原始文件-无法解析`或`原始文件-损坏`)/`destination`/`evidence`/`record`(原记录) |
| `20_sample_quarantine.jsonl` | 同上；`label=样本-语义不通`、`destination=样本隔离库` |
| `20_rejects.jsonl` | `line`/`reason`(`json_parse_error`…)/`raw_preview` |
| `20_flagged.jsonl` | 主输出的**旁路子集**（`decision=flagged`），不参与 `output` 计数 |
| `20_quality_report.json` | `status`(固定 `pending_writeback`)/`writeback`/`checks`/`metrics`/`reverse_verification`/`topic_skew`/`isolation_suggestions`/`statistics`/`config` |
| `20_supplement_list.json` | `topic_skew_threshold`/`route_distribution`/`dominant_route`/`skewed`/`supplement_targets`/`batch_isolation=false` |

报告 `writeback` 固定：`{ "required": true, "writeback_by": "operator_24", "writeback_completed": false, "version_state": "pending_release" }`。

### 2.5 统计守恒

```
input == output + rejected + origin_quarantined + sample_quarantined
```

- `input`：有效业务记录数（不含空行、坏行）；`input_lines = input + blank_lines + rejected`
- `flagged` 是主输出的**旁路子集**，**不与主记录重复加总**
- `duplicates` 仅计数（表1 #20 未要求去重）
- 任一守恒式不成立 → 运行 `failed`，不产出报告

## 3. 降级与失败语义（禁止伪造）

| 场景 | 行为 |
|---|---|
| CLIP 权重缺失 | `reverse_verify` 状态 `insufficient_evidence`、`reverse_model_available=false`、`statistics.degraded_records>0`、运行 `status=degraded`；**不产出相似度分数** |
| 本地指标后端缺失 | 对应 `checks.<metric>=insufficient_evidence`；**不产出 PPL/BRISQUE/SNR 数值** |
| `allow_degraded=false` 且依赖缺失 | 抛出 `DependencyError`（CLI 退出码 3 / HTTP `code=1`），不产出任何结论 |
| 空输入 | `statistics.empty_input=true` |

## 4. 下游交接

- 主输出（`20_alignment_qc.jsonl`）交由下一算子；通过 `crossmodal-files-v1` 文件内容对象交付。
- 原始隔离 + 样本隔离台账交 **#21 处置错配样本**；`flagged` 与 `quality_report` 供 **#24 抽检并回写质量报告**。
- 报告状态固定 `pending_writeback`，**不能提前封版**。
