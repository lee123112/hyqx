# 算子 #11 生成候选配对 说明书

## 1. 基础信息

| 项 | 值 |
|---|---|
| 正式编号 / 名称 | #11 生成候选配对 |
| 流程环节 | 数据加工 |
| 工具类型 | 规则型 |
| 是否可跳过 / 默认状态 / 优先级 | 否（必须） / 默认开启 / P0 |
| 模型 | 无（本工具不加载 CLIP，不算相似度） |
| 版本 | v1.0.0 |
| 端口 | 8111 |

## 2. 功能定义

把 #10 准入通过的跨模态对象组成候选配对，并给出可追溯的证据等级：

1. **证据 A**：同来源编号（`source_id`）或同场景编号（`scene_id`/`group_id`）；
2. **证据 B**：时间范围真实重叠；
3. **证据 C**：共同关键词数量达到阈值（默认 2，可选 2–5）。

系统按「来源 → 时间 → 关键词」顺序尝试，配上即停，同一个配对不会重复记多套证据。
一对多拆成多条一对一，共享 `group_id`。配不出候选的记录进入未配对清单，不隔离、不进入评分。

## 3. 输出内容描述

| 文件 | 角色 | 说明 |
|---|---|---|
| `11_candidate_pairs.jsonl` | 主产物 | 每条一个候选配对：两侧 sample_id、模态、证据类型和值、group_id |
| `11_unpaired.jsonl` | 旁路 | 无候选的未配对清单（不隔离） |
| `11_rejects.jsonl` | 异常 | 坏行、缺字段、无模态、重复编号、未准入、配对上限，带台账字段与物理行号 |
| `manifest.json` / `run.log` | 控制文件 | 清单与日志 |
| 被引用媒体 | 媒体 | 按原相对路径复制进运行目录，保证可搬运 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`lineage.py`/`mty_compliance.py` 代码保留存档）；原血缘中的统计/配置快照等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

## 4. 技术方案

- 纯规则实现：确定性关键词抽取（上游 `tokens` 优先，缺失时用中文二元字组 + 拉丁词规则），不使用分词/向量模型；
- 时间范围由文本时间索引、音频段落/时长、视频帧时间戳汇总；无真实时间戳记为不适用，不用零值填充；
- 流式逐行读取输入，坏行与编码错误进入异常清单，不静默丢弃；
- 输出走临时文件 + 原子替换，输入文件只读。

## 5. 技术实现

`utils/pairing.py`（业务核心）→ `inference.py`（`run` + CLI）→ `app.py`（三接口）。
只提供 `POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`，不提供下载接口。
中间响应与阶段末响应同构：均返回 `{operator, status, outputFile, result}`，`outputFile` 为
`crossmodal-files-v1` 文件内容对象，`result` 为业务摘要；`isLastOperator` 仅用于标记阶段末调用。

## 6. 资源要求

CPU 单核即可；内存随单文件记录数线性增长（配对需要全量视图）。无 GPU、无模型权重、无外网依赖。

## 7. 性能指标

样例 13 行（含 1 坏行）耗时 < 1 秒；无模型推理。真实规模性能需在产线数据上实测，本 demo 未做吞吐压测。

## 8. 输入输出示例

输入一行（准入通过对象）：

```json
{"sample_id": "sample-0001", "source_id": "src-A", "scene_id": "outdoor",
 "text": {"content": "一只金毛犬在草地上接飞盘", "tokens": ["金毛犬", "草地", "飞盘"]},
 "image": {"path": "golden_retriever_grass.png"}, "video": null, "audio": null, "metadata": {}}
```

输出一行（候选配对）：

```json
{"pair_id": "pair-sample-0001--sample-0002", "group_id": "grp-src-src-A",
 "modality_pair": ["text", "image"],
 "evidence": {"type": "A", "criterion": "same_source_id", "value": {"source_id": "src-A"}},
 "evidence_priority": 1,
 "left": {"sample_id": "sample-0001", "modalities": ["text", "image"], "record": {}},
 "right": {"sample_id": "sample-0002", "modalities": ["text", "image"], "record": {}},
 "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0", "uniqueId": "task_file"}}
```

## 9. 部署方法

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8111
```

环境变量：`OUTPUT_DIR`（输出根，默认算子目录下 `output/`）、`OP11_MAX_FILES`（ZIP 成员上限，默认 1000）、
`OP11_MAX_UNPACKED_BYTES`（解压总字节上限，默认 64 MiB）、`OP11_PORT`（默认 8111）。

## 10. 已知限制

- 本工具不做语义判定，只给候选与证据；真实语义过滤由 #12–#15 承接；
- 媒体只做原样携带，不解码、不校验内容质量（#10/#20 负责）；
- 中文关键词在缺少上游 `tokens` 时使用二元字组规则，阈值 2 意味着「2 个重叠字组」，比人工分词更宽松；
- 来源编号会回退读取 `metadata.source_id` / `metadata.source`，若上游把所有样本写成同一个 `source`，证据 A 会过度成组；
- 视频样例未包含真实可解码 MP4（仓库不外挂编码器），视频时间范围与携带由单测覆盖；
- 单次运行的配对需要内存内保留有效记录，超大单文件需评估内存。
