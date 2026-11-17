# 数据集字段

## 1. 支持的数据格式

支持 JSON `{samples:[...]}`、JSONL/NDJSON（一行一条）、CSV（表头含 id/input）、TXT（每行一条）、ZIP（内含 samples.json / dataset.jsonl / dataset.csv 与媒体文件）。

公共字段：`id`、`input`、`output`、`title`、`scene`、`sourceStep`、`parameters`、`highValue`、`file`/`fileName`、`measurements`。

图像资产按 `uri` / `input` / `path` / `assetPath` / `fileName` / `sourceFile` 取值，支持本地路径、业务资产 URL（`/api/v1/datasets/{kind}/{datasetId}/assets/{path}`）或公开 `http(s)` 链接。图片后缀：`.jpg` / `.jpeg` / `.png` / `.webp` / `.bmp`。

实测指标（旧口径）：`measurements.baseline` 与 `measurements.candidate` 必须覆盖本类型全部指标（`iou`、`clarity`、`semantic`、`accuracy`）；数值范围见 README。

CSV 只认 `baseline_` / `candidate_` 前缀的 `iou`、`accuracy`、`f1`、`lossCv`、`cer`、`speakerAccuracy`、`domainF1`、`score` 列；`clarity`、`semantic` 可在 CSV 的 `measurements` 列填 JSON 字符串，或用 JSON/JSONL 的 `measurements` 对象提供，否则由引擎测量补齐。

## 2. 分类质量协议目录布局（`image-classification-quality-v1`）

离线/数据集级评估不使用上面的清单格式，而是三份并列的类别目录：

```text
<data-root>/image/
├─ ours/<class>/*.jpg        # D_ours：待评估业务数据
├─ ref/<class>/*.jpg         # D_ref：同域参考数据
└─ eval/<class>/*.jpg        # E：固定独立 holdout
```

- 目录名即类别名，是协议里的**权威标签**；不要在文件名或清单里另造标签体系。
- 三份目录可来自不同磁盘位置，但类别目录名必须逐字一致（大小写敏感）。
- 类别数 ≥ 2；每个类别目录必须至少有一张可解码图片。
- 建议每类 20 张以上、总量 200 张以上再下结论；条数明显偏少时只能报探索性结果。

公开夹具 `dataset/image/cifar10/` 是 `images/train/<class>` / `images/test/<class>` 布局，并附 `labels.json` 固定类别顺序；它属于公开基准候选，不是业务 `D_ours`，也不能直接当作本协议三目录使用。

## 3. 类别一致性与数据量对齐

| 约束 | 要求 | 违反时的错误码 |
|---|---|---|
| 类别集合 | `D_ours`、`D_ref`、`E` 的类别目录集合完全相同，排序一致 | 422 `DATASET_INVALID` |
| 训练总量 | `D_ours` 与 `D_ref` 的总图片数相同 | 422 `PROTOCOL_MISMATCH` |
| 每类条数 | `D_ours` 与 `D_ref` 的每类条数相同（防止类别不平衡混入数据质量差异） | 422 `PROTOCOL_MISMATCH` |
| 类别数 | 每份数据至少 2 个非空类别目录 | 422 `DATASET_INVALID` |
| 评测集非空 | `E` 至少有一张可解码图片 | 422 `DATASET_INVALID` |
| `E` 独立性 | `E` 不参与训练、增强与调参 | 协议要求，靠泄漏审计与评审 |

数据量对齐的含义：`D_ours` 与 `D_ref` 只允许内容不同，不允许条数不同。若业务数据确实更多，先按类别等比抽样到相同条数，并把抽样规则写进报告。

## 4. 标签字段

- **离线协议**：标签只由类别目录名决定；文件名、扩展名与图片元数据不参与标签生成。
- **在线清单格式**：沿用平台字段 `output` 作为类别名/标签文本；`id` 为样本键，缺省时按 `S001`、`S002`… 生成。
- 不要把 `label` 当成平台已支持字段（解析器不读取）；需要新字段必须先改解析器，不能在文档里先承诺。

## 5. 评测集 E 的独立性与复用

- `E` 必须独立于 `D_ours` / `D_ref`：不能是训练集的切片、增强副本或近似重复副本。
- 训练前会审计 `E` 与训练集的精确重复与 **16x16 灰度、8 向翻转/旋转归一化** average-hash 近重复（256 bit，Hamming ≤ 5）；严格模式命中即返回 422 `LEAKAGE_DETECTED`，非严格模式标记 `valid=false`。pair 上限截断单独返回 422 `AUDIT_INCOMPLETE`。
- `E` 固定后跨 run 复用；更换 `E` 必须提升 `configs/config.json` 的 `protocol.protocolVersion`，否则前后结果不可比。
- OOD 的类别 JSD 始终强制；特征 MMD 只在加载本地预训练 backbone 时强制，随机初始化时仅作诊断。
- `E` 不得参与调参、模型选择或阈值搜索；一旦用于选型，只能在报告里标注为验证集并重新划出测试集。

## 6. 结果字段（协议实验 JSON）

`tools/run_image_protocol.py --output` 或 `POST /api/v1/eval/image/protocol` 的产出记录：

| 字段 | 说明 |
|---|---|
| `experiment` | `image-quality-protocol` |
| `valid` | 泄漏与 OOD 审计是否都通过，且是否加载本地预训练权重；false 时分数只能当诊断 |
| `conclusionEligible` | 与 `valid` 同义的显式结论开关 |
| `S_ours` / `S_ref` | `{top1, macroF1}`，0–100 |
| `delta` | `S_ours - S_ref`，逐指标 |
| `benchmark` / `benchmarkTask` | 声明的对标口径；`mlperftiny` 记为 `visual_wake_words`，其余为 `classification` |
| `proxyModel` / `trainingMode` | 两侧共用；`full` 或 `linear_probe` |
| `protocol` | 模型、权重路径与 `pretrained`、imageSize、epochs、batchSize、lr、seed、增强口径（`train-only`/`none`）、`testTimeAugmentation`、`fixedEval`、训练量、评测量、类别与每类条数 |
| `data` | 三份数据目录与 SHA256 digest |
| `leakage` | `passed`、近重复阈值与比对 pair 数、跨集/集内重复计数、样例、告警 |
| `ood` | `passed`、`labelJSD`、`featureMMD`、`featureMMDPValue`、`featureMMDEnforced`、阈值、类别分布、样本数、告警 |
| `provenance` | `{top1: measured_model, macroF1: measured_model}` |
| `notes` | 协议声明、审计说明、权重来源与局限 |

`S_ours` / `S_ref` 只在该 JSON 中出现，**不通过** `POST /predict` 的 `measurements` 上传；`/predict` 仍是旧四项代理口径。
