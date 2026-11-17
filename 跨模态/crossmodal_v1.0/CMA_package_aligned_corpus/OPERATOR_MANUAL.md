# 算子 23「压缩交付对齐语料包」说明书

> 面向使用方/交付方的说明。技术细节见 [`README.md`](README.md)，字段口径见
> [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。

## 1. 一句话说明

本算子把 #22 生成的 Token 样本清单**封成一个可交付 ZIP**，但**只有在三项条件同时满足**时才把
版本从「待发布」改成「可交付」：训练文件已生成、质量报告已写回、抽检已写回。
条件不满足时**不丢任何样本**，只是继续保持「待发布」。

## 2. 它在流程中的位置

```text
… → #20 质检（报告 status=waiting_writeback）
    → #21 处置错配（隔离清单）
    → #22 生成跨模态 Token（Token 清单）
    → 【#23 压缩交付对齐语料包】 ← 本算子，第 6 环节，单算子阶段
    → 工程侧 OSS 保存 / 分片 / 下一阶段重新上传
```

## 3. 三种运行结果（看清楚再消费）

| 运行状态 | 触发条件 | 交付包 | 说明 |
|---|---|---|---|
| `success` | 三项门禁全部满足 + 5 项材料齐全 | `23_deliverable_package.zip`（**可交付**） | 可交工程侧封版 |
| `degraded` | 材料不齐 **或** 质量报告/抽检未回写 | `23_pending_package.zip`（**待发布**） | 样本照常入包，但版本不得对外称已交付 |
| `failed` | 统计守恒失败 / 全部行不可解析 / 严格模式（`require_deliverable=true`）下不可交付 | 仍有本次运行的产物与台账 | 需排查后重跑，不得改配置绕过 |

## 4. 「可交付」到底看什么

```
可交付 = 训练文件已生成  AND  质量报告已写回  AND  抽检已写回   AND  5 项材料齐全
```

- **材料 5 项**：训练文件（入口 JSONL）、质量报告、抽检结论、隔离清单、版本说明；
- **已写回**判定（任一成立即算已写回）：`status=written_back`，或 `writeback.completed=true`，
  或顶层 `written_back=true`（抽检另支持 `conclusion_written_back`）；
- **版本说明**必须声明 `enabled_routes`（⊆ `image_text`/`audio_text`/`video_text`，至少一条）
  与 `annotation_granularity`；缺失或非法 → 该材料判为无效 → 保持待发布；
- 这三项条件写在 `configs/config.yaml` 的只读段（`status_before_review`/`status_after_review`）
  与 `utils/delivery.py` 的 `GATE_REQUIREMENTS` 中，**接口请求无法改写**。

判定结果落盘位置：`23_materials.json`（明细）、包内 `manifest.json`（成品可自证）；
接口响应 `data.result.statistics` 同步返回门禁与统计结果。

## 5. 样本去向（不丢样本）

| 样本情况 | 去向 | 是否进交付包 |
|---|---|---|
| 正常样本 | 主输出 `23_delivery_package.jsonl` | ✅（包内 `train/samples.jsonl`） |
| 上游已隔离（#21 标记或隔离清单命中） | `23_excluded_isolated.jsonl`（样本隔离库口径） | ❌ **绝不入包** |
| 缺 `sample_id`、媒体引用为绝对路径/穿越、Token 为空 | `23_rejects.jsonl`（原始隔离区） | ❌ |
| 坏行、非 JSON 对象、非 UTF-8 | `23_rejects.jsonl`（原始隔离区） | ❌ |
| route 不在版本说明声明的启用路线内 | 主输出（标注 `applicability=not_applicable`） | ✅ 但**单独计数**，供人工确认口径 |

每种去向都有统一台账字段（标签、工序、动作、去向、时间、批次、重跑次数），可追溯。
`input_records = 入包 + 排除隔离 + 原始隔离 + 坏行` 等式在每次运行中校验，不成立即 `failed`。

## 6. 交付包里有什么

```
23_deliverable_package.zip
├── train/samples.jsonl                 # 训练文件（已剔除隔离样本）
├── materials/quality_report.json       # 已回写质量报告（原样）
├── materials/sampling_conclusion.json  # 抽检结论（原样）
├── materials/isolation_list.jsonl      # 隔离清单（原样）
├── materials/version_notes.json        # 版本说明（原样）
├── manifest.json                       # 逐文件 sha256、门禁结果、真实统计、外部引用
├── version.json                        # 启用路线、标注粒度、状态（待发布/可交付）
└── media/**                            # 记录声明的相对媒体（可配置关闭）
```

包内全部为**包内相对路径**；`manifest.json` 的 `files[].sha256` 可逐文件校验完整性。
外部引用（`oss://`、`http(s)://`）只登记在 `manifest.json.externalReferences`，**不复制**。

## 7. 怎么用

```bash
# 上传任务输入包（zip：demo.jsonl + manifest.json + materials/ + media/）
curl -F "file=@task.zip" "http://127.0.0.1:8123/uploadFile?uniqueId=batchA_0001"

# 形态一：multipart/form-data 单文件流；两阶段发布 publish_phase=prepare/finalize
curl -F "file=@task.zip" -F "publish_phase=prepare" \
     "http://127.0.0.1:8123/predict?detection=package_aligned_corpus"
# → {"status":1,"publish_status":"pending","package":"23_pending_package.zip", ...}

# 形态二：判定 + 打包（application/json；isLastOperator 标记阶段末）
curl -H "Content-Type: application/json" \
     -d '{"uniqueId":"batchA_0001","isLastOperator":false}' \
     "http://127.0.0.1:8123/predict?operatorId=m23"
# → data = {operator, status, outputFile, result}；result 含 packagePath/packageName/
#   versionState/deliverable/statusLabel/statistics 等

# 阶段末（isLastOperator=true）：响应结构与中间调用一致（同构；v4.2 起停用血缘输出）
curl -H "Content-Type: application/json" \
     -d '{"uniqueId":"batchA_0001","isLastOperator":true}' \
     "http://127.0.0.1:8123/predict"
```

CLI 等价写法：

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
python processor.py --input dataset/sample/deliverable/demo.jsonl --unique-id t1 \
    --output-root output_smoke --is-last-operator
```

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `msg` 是「版本保持待发布（23_pending_package.zip）…」 | 质量报告或抽检未回写 / 材料缺失 | 让 #24（或自动质检）完成后**重跑**本算子；不要改配置放行 |
| `deliverable=false` 但训练文件正常 | 门禁不满足，属设计行为 | 看 `statistics.deliverable_requirements` 与 `23_materials.json.missing_requirements` |
| 包内没有 `media/` | `include_media=false` | 按需置回 `true`；生产大包建议保持 `false` 并走 OSS |
| `media_missing > 0` | 记录声明的相对媒体不存在 | 仅登记缺失计数（不阻断），请核对上游媒体搬运 |
| 请求 `overrides` 被拒 | 只允许 `include_media`/`require_deliverable`/`require_token` | 门禁与格式类配置只能改部署配置，且多数为只读常量 |
| 状态无法变「可交付」 | 版本说明未声明路线或标注粒度 | 补齐 `enabled_routes` 与 `annotation_granularity` 后重跑 |
| `failed` + `conservation_failures` 非空 | 计数逻辑或输入异常 | 保留本次 `output_smoke`/run 目录并上报，不要删除证据 |

## 9. 明确边界（不要向本算子要求）

- 生成或改写 Token 内容 → 属 #22；
- 质量检测、抽检、人工标注 → 属 #20/#24（本算子只**读取回写结果**）；
- OSS 上传、分片、跨阶段搬运 → **工程侧**；
- 任何分数、相似度、概率、人工确认 → 本算子**不产生**，`human_verified` 恒为 `false`；
- 按任意路径删除文件 → 契约只允许按 `uniqueId` 清理任务目录。
