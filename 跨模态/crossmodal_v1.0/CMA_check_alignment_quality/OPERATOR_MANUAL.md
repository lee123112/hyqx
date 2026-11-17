# 算子说明书 · 20 质检对齐产物

## 1. 表1 基准（v4.1 第 4 环节「数据质量评估」）

| 字段 | 表1 口径 | 本实现 |
|---|---|---|
| 流程环节 | 4 数据质量评估 | `stageId=S4`、`run_stage=data_quality_assessment` |
| 开关 | 默认开启 | `enabled: true` |
| 是否必须 | 必须 | 不可关闭（关闭时仅透传并标 `skipped`） |
| 优先级 | P0 | 主输出、隔离台账、报告、待补清单齐备 |
| 模型/后端 | `--`；复用上游质量指标和 CLIP 反向验证 | 无自训练模型；上游指标复用 + 可选 CLIP 反向检索 |
| 关键技术 | 全量文件可打开、≥2 模态、视频可解码、音频非全静音、文本非空；指标复用 10% 子集补测；图文 CLIP 反向检索；偏科 >70% 出待补清单不整批隔离 | 逐项实现（见 §3、§4） |

## 2. 处理流程

```
读取输入 → 严格 UTF-8 逐行解析
  ├─ 空行 → blank_lines
  ├─ 坏 JSON / 非对象 → 20_rejects.jsonl（原始隔离区）
  └─ 有效对象：
       1) 校验 sample_id、原始隔离原因（路径/缺失/损坏/无声/文本不可读）→ 原始隔离
       2) 文件可打开、模态数、视频可解码、音频非静音、文本非空 → 检查项
       3) 指标：上游优先；无上游且命中 10% 子集 → 本地补测；否则 insufficient_evidence
       4) 图文反向检索：上游结论优先；无上游 → 整池 CLIP 检索（权重缺失 → degraded）
       5) 分诊：通过 → 主输出；不一致 → flagged；模态不足/文本空 → 样本隔离
       6) 追加 processing["20"]，写主输出 + 检查明细 + 台账
汇总 → 20_quality_report.json / 20_supplement_list.json / manifest.json / run.log / 统计守恒
```

## 3. 检查项与判定

| 检查 | 判定规则 | 不通过去向 |
|---|---|---|
| `text_nonempty` | 去空白后字符数 ≥ `min_text_chars` | 样本隔离库 |
| `file_openable` | 每个媒体引用可打开 | 原始隔离区 |
| `min_modalities` | 有效模态数 ≥ 2 | 样本隔离库 |
| `video_decodable` | 可解码（最多 8 帧探测） | 原始隔离区 |
| `audio_non_silent` | 解码 RMS > `silence_rms_threshold` | 原始隔离区 |
| `ppl`/`brisque`/`snr` | 上游优先；子集补测 | 证据不足只标 `insufficient_evidence`（**不判失败**） |
| `reverse_verify` | 一致性 ≥ `min_consistency` | 不一致 → `flagged`（**不拦样本**） |

**原始隔离优先于样本隔离**：引用不存在/损坏/整段无声先判原始隔离，避免统计重复归口。

## 4. 指标复用与子集补测

- **上游优先**（`upstream_first`）：从 `qc.*` / 记录字段读取真实数值，`metrics.<m>.upstream` 计数。
- **子集补测**：仅当无上游数值时，按 `blake2b(sample_id) % 10000 < ratio*10000` **确定性**选取记录做本地测量；
  本机无对应后端时如实记 `insufficient_evidence`，**不产出任何数值**。
- 本地 SNR 由**解码后逐帧能量分位**计算（纯音 ≈ 0 dB，整段无声 → `None` → 原始隔离）。
- 分桶计数互斥：`upstream` + `local` + `not_applicable` + `insufficient_evidence`。

## 5. 反向验证（CLIP）

- 上游 `qc.reverse_verify` 存在 → 直接复用（`source=upstream`）。
- 缺失 → 对**图文子集**整池做真实反向检索：以文本为 query，在图像池中算余弦并排序，
  `top_k`（默认 5）内命中自己且一致性 ≥ `min_consistency`（默认 0.25）→ 一致。
- 编码器优先级：共享任务服务（可选）→ `open_clip`（本算子 `weight/` 本地权重）→ 共享实例注入；
  **权重缺失 → 显式 `degraded`**（`clip_weights_missing`），
  `reverse_model_available=false`，**不产出相似度**。

## 6. 偏科检测

单一路由（如 `image+text`）占比 **严格大于** `topic_skew_threshold`（0.70）→ `skewed=true`，
产出 `supplement_targets`（建议补充的组合与差距）；**`batch_isolation=false`**——只出清单，不整批隔离。

## 7. 质量处置与去向（表1 #20 质量三场景）

| 场景 | 条件 | label | destination | `process_status` |
|---|---|---|---|---|
| 通过 | 全部检查通过 | 样本-质检通过 | 质量报告待回写 | `success` |
| 待人工复核 | 反向验证不一致 | 流程-待人工复核 | 人工复核队列 | `pending_review`（旁路 flagged） |
| 治理后不达标 | 模态不足 / 文本空 | 样本-语义不通 | 样本隔离库 | `isolated` |
| 原始文件问题 | 坏/缺/不可解码/无声 | 原始文件-无法解析 / 原始文件-损坏 | 原始隔离区 | `failed` |
| 未启用 | `enabled=false` | 流程-质检未启用 | — | `skipped` |

记录级 `processing["20"].status` 为 v4.2 int（1=通过/待复核/跳过，0=隔离/失败），上表为
`process_status` 原字符串值。

**抽检未回写不拦样本**：报告 `status` 固定 `pending_writeback`、`version_state=pending_release`。

## 8. 统计守恒

```
input == output + rejected + origin_quarantined + sample_quarantined
input_lines == input + blank_lines + rejected
```

`flagged` 为 `output` 的旁路子集，不重复加总；`not_applicable` 不计失败。任一式不成立 → `failed`。

## 9. 降级与禁止事项

- 允许降级时：`status=degraded`，缺失指标一律 `insufficient_evidence`/`null`，**零伪造分数**。
- 不允许降级（`allow_degraded=false`）：依赖缺失 → `DependencyError`（CLI 退出码 3 / HTTP `code=1`）。
- **禁止**：固定相似度、随机分数、假造报告、把规则分写成模型概率、把预填写成人工确认。
