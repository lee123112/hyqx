# 算子 #11：生成候选配对（generate_candidate_pairs）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

表1 v4.1 第 11 行「数据加工 / 生成候选配对」的正式实现。把 #10 准入通过的跨模态对象按三级证据规则
组成候选配对，输出可被 #12/#13/#14/#15 消费的候选对与证据，未配对记录留清单、不隔离。

## 1. 功能边界

做：

- 按 **同来源编号或场景编号（证据 A）→ 时间范围重叠（证据 B）→ 共同关键词至少 N 个（证据 C）** 的顺序尝试配对，**配上即停**；
- 一对多拆成多条一对一，同一组共享 `group_id`；
- 只记录 A/B/C 证据（类型 + 判定依据 + 取值）；
- 配不出候选的记录写入未配对清单，保留完整样本；
- 坏行、编码错误、缺字段、无模态、重复编号、未准入标记进入异常清单，保留物理行号与台账字段。

不做（明确越界项）：

- **不加载 CLIP、不算相似度、不输出任何模型分数**（那是 #12–#15）；
- 不做准入核对（#10）、不做描述准备（#9）、不做质量隔离（#21）；
- 不把「配不出候选」当成低质量隔离——表1 规定留在产线未配对清单；
- 不生成 Token、不打包交付（#22/#23）。

## 2. 输入字段

输入是 #10 输出的准入通过对象 JSONL，每行一个 dict：

| 字段 | 必需 | 说明 |
|---|---|---|
| `sample_id` | 是 | 样本唯一标识；缺失或重复进入异常清单 |
| `source_id` / `metadata.source_id` / `metadata.source` | 否 | 证据 A 的来源编号 |
| `scene_id` / `group_id` / `metadata.scene_id` / `metadata.scene` | 否 | 证据 A 的场景编号（#2 写入的 group_id 可用） |
| `text` | 否 | 字符串或对象：`content`、`tokens`（上游 #4 产物，优先）、`time_index`、`segments` |
| `image` / `video` / `audio` | 否 | 字符串路径或对象：`path`、`duration`、`segments`、`frames[].timestamp` |
| `metadata` | 否 | `image_tags`/`keywords`/`tags`/`labels` 参与关键词 |
| `processing`/`alignment`/`qc` 及未知扩展 | 否 | 原样保留并随配对透传，不覆盖上游结论 |
| `admission` | 否 | 防御性校验：`{"passed": false}` 的记录不配对并记异常 |

至少一种模态存在，否则进入 `no_modality` 异常清单。

## 3. 输出字段

| 文件 | 角色 | 内容 |
|---|---|---|
| `11_candidate_pairs.jsonl` | 主产物 | 每个候选对一行 |
| `11_unpaired.jsonl` | sidecar | 未配对清单（`reason: no_candidate`，含 `tried_evidence`） |
| `11_rejects.jsonl` | review | 异常/拒收清单（含 `line`/`label`/`action`/`destination`/`event_time`/`batch_id`/`retry_count`） |
| `manifest.json` | 控制 | `crossmodal-manifest-v1`，登记 main/sidecar/review/media |
| `run.log` | 控制 | 逐行日志 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`utils/lineage.py`、`utils/mty_compliance.py` 代码保留存档，`app.py`/`inference.py` 不再调用其血缘构建接口）；原血缘中的统计/配置快照等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

候选对结构：

```json
{
  "pair_id": "pair-sample-0001--sample-0002",
  "group_id": "grp-src-src-A",
  "modality_pair": ["text", "image"],
  "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
               "value": {"source_id": "src-A"}},
  "evidence_priority": 1,
  "left": {"sample_id": "sample-0001", "modalities": ["text", "image"], "record": {}},
  "right": {"sample_id": "sample-0002", "modalities": ["text", "image"], "record": {}},
  "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0", "uniqueId": "task_file"}
}
```

证据取值：A → `{"source_id": ...}` 或 `{"scene_id": ...}`；B → `{"overlap_seconds", "left_range", "right_range"}`；
C → `{"min_overlap", "matched": [...], "matched_count": N, "tokenizer": "upstream_tokens+cjk_bigram_rule"}`。
`evidence.evidence_source` 固定为 `rule`（本算子不含模型证据），字段名与兄弟算子 08 保持一致。
`left/right.record` 保存完整原始记录，便于下游直接评分。

## 4. 配置（`configs/config.yaml`）

| 配置 | 默认 | 范围 | 表1 对应 |
|---|---|---|---|
| `pairing.enabled` | `true` | 只能为 true | 默认开启且不能关闭；置 false 直接配置失败 |
| `pairing.priority` | `source` | `source` / `time_overlap` / `keywords` | 「优先用什么配对」下拉框 |
| `pairing.keyword_min_overlap` | `2` | 2–5 | 「关键词至少相同几个」 |
| `pairing.time_min_overlap_seconds` | `0.0` | ≥0 | 时间重叠下限（重叠必须为正且达到该值） |
| `pairing.max_pairs_per_group` | `200` | ≥1 | 单播种记录拆分的安全上限，超出部分进异常清单 |

请求级 `config` 只允许覆盖上表中的业务参数；`operator`/`stage_id`/`io` 等身份与部署项会被拒绝。

## 5. 算法/模型后端与降级行为

**无模型**。本算子不使用 CLIP、wav2vec2、YOLO 或任何向量/相似度后端，全部为确定性规则：

- 关键词：优先上游 `text.tokens`；缺失时按「中文 2–8 字整体词 + 全部二元字组 + 拉丁词长 ≥3」的规则抽取；**不使用文件名**；
- 时间范围：汇总 `time_index`、`segments`、`duration`、`frames[].timestamp`；没有任何真实时间戳记为空（不适用），不用零值填充。

因此**不存在「模型不可用降级」这一分支**，也不存在 rule_score 之类的替代分数。失败与降级语义只有：

| 情况 | 表现 |
|---|---|
| 配置非法（如关掉开关、阈值越界） | `status=failed`，`reason=config_error: ...`，接口 `code=1` |
| 输入文件不存在 | `status=failed`，`reason=input_not_found` |
| 全部行都是坏行 | `status=success`，主产物为空，`statistics.bad_lines=N`，异常清单完整 |
| 空输入 | `status=success`，`statistics.empty_input=true` |
| 媒体文件缺失 | 计数 `statistics.media_missing`，不复制、不伪造文件 |
| 配不出候选 | `status=success`，记录进 `11_unpaired.jsonl`，**不隔离** |

## 6. 启动与调用

```powershell
python -m pip install -r requirements.txt
python dataset/sample/make_sample.py
python -m uvicorn app:app --host 0.0.0.0 --port 8111
```

CLI（本目录无 `processor.py`，业务入口是 `inference.py`）：

```powershell
python inference.py --input dataset/sample/samples.jsonl --unique-id smoke_task_file1 --output-root output_smoke
```

三接口（只用这三个，不提供 `/downloadFile`）：

| 接口 | 请求 | 成功返回 |
|---|---|---|
| `POST /uploadFile?uniqueId=` | multipart file（zip 或单 JSONL） | `{"code":0,"msg":"success","data":{"entrypoint":"..."}}` |
| `POST /predict` | `uniqueId`、严格布尔 `isLastOperator`、可选 `inputFile`、可选 `config` | 见下 |
| `POST /removeFile?uniqueId=` | query `uniqueId` | `{"code":0,"msg":"success","data":{"deleted":true}}` |

[v4.2] `isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；**中间与阶段末响应同构**，
均返回 `data = {operator, status, outputFile, result}`：`status` 为运行状态字符串（如 `success`），
`outputFile` 为 `crossmodal-files-v1` 文件内容对象，`result` 为业务摘要
`{runId, runDir, files{outputFile,unpairedFile,rejectsFile}, statistics, config, stageId}`。
不再用 `false` 只回血缘对象。阶段首请求省略 `inputFile`，算子按任务上传清单定位入口；
阶段内后续算子把上一响应的 `result.files.outputFile` 作为 `inputFile` 传入（允许任务根内绝对路径）。

## 7. 响应示例（v4.2 统一结构）

```json
{"code":0,"msg":"success","data":{
  "operator":"generate_candidate_pairs",
  "status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"11_candidate_pairs.jsonl",
    "files":[{"path":"11_candidate_pairs.jsonl","encoding":"utf-8","content":"..."},
             {"path":"11_unpaired.jsonl","encoding":"utf-8","content":"..."},
             {"path":"11_rejects.jsonl","encoding":"utf-8","content":"..."},
             {"path":"manifest.json","encoding":"utf-8","content":"..."},
             {"path":"golden_retriever_grass.png","encoding":"base64","content":"..."}]},
  "result":{"runId":"run001","runDir":"generate_candidate_pairs/run001",
            "files":{"outputFile":"generate_candidate_pairs/run001/11_candidate_pairs.jsonl",
                     "unpairedFile":"generate_candidate_pairs/run001/11_unpaired.jsonl",
                     "rejectsFile":"generate_candidate_pairs/run001/11_rejects.jsonl"},
            "statistics":{"input_records":13,"valid_records":8,"paired_records":7,
              "unpaired_records":1,"pairs_total":4,"pairs_by_evidence":{"A":2,"B":1,"C":1},
              "groups_total":3,"rejected_records":4,"bad_lines":1,"rejects_total":5,
              "media_carried":6,"media_missing":0,"empty_input":false,"conservation_ok":true,
              "input":13,"output":8,"rejected":5,"applicable":7,"not_applicable":1,
              "degraded":false,"degradation_reason":null,"active_backend":"rule"},
            "config":{"pairing":{"enabled":true,"priority":"source","keyword_min_overlap":2,
              "max_pairs_per_group":200,"time_min_overlap_seconds":0.0}},
            "stageId":"S3"}}}
```

中间与阶段末响应同构：`outputFile` 为文件内容对象（`entrypoint=11_candidate_pairs.jsonl`，
文本 utf-8、媒体 base64，共 3 个清单 + manifest + 6 个媒体），`result` 为业务摘要。
失败返回 `{"code":1,"msg":"<原因>","data":{}}`（参数不合法为 HTTP 400 + `code=1`）。

`statistics` 同时提供两套口径：本算子明细口径（`input_records`/`valid_records`/`paired_records`/`unpaired_records`/`pairs_total`/`pairs_by_evidence`/…）
和与兄弟算子（08/14/21）一致的记录级口径（`input`/`output`/`rejected`/`applicable`/`not_applicable`/`degraded`/`active_backend`），
其中 `input == output + rejected`、`output == applicable + not_applicable`。`degraded` 恒为 `false`，因为本算子没有模型后端。

`stageId` 说明：demo 阶段表把 #8–#15 归入「跨模态数据对齐标注」，本算子取共用约定 `S3`；表1 正式环节为「数据加工」。`stageId` 随 `data.result.stageId` 与运行日志返回，待工程侧最终确认。

## 8. 资源需求

- CPU 单核可跑，无 GPU、无模型权重、无外网；
- 运行内存与单文件有效记录数线性相关（配对需要全量视图，媒体只按路径复制）；
- HTTP 侧限制：`OP11_MAX_FILES`（默认 1000）、`OP11_MAX_UNPACKED_BYTES`（默认 64 MiB）。

## 9. 真实测试结果（本机实测）

环境：Windows + Python 3.9.7、pytest 8.4.2、FastAPI 0.128.8、httpx。

```powershell
python -m pytest tests/ -q
# 91 passed in 6.65s

python -m json.tool map.json        # 通过（8 个字段，formal_operator_numbers=[11]）
python -m compileall -q .          # exit=0
python inference.py --input dataset/sample/samples.jsonl --unique-id smoke_task_file1 --output-root output_smoke
# result: success pairs=4 paired=7 unpaired=1 evidence={"A": 2, "B": 1, "C": 1} rejects=5   （runId run001）
python inference.py --input dataset/sample/samples.jsonl --unique-id smoke_task_file2 --output-root output_smoke
# result: success pairs=4 paired=7 unpaired=1 evidence={"A": 2, "B": 1, "C": 1} rejects=5   （两个 uniqueId 产物物理隔离）
python inference.py --input dataset/sample/samples.jsonl --unique-id ../escape --output-root output_smoke
# result: failed reason=uniqueId 只能是单个安全目录名   （exit=1）
# 同一 uniqueId 再跑一次 → runId run002，run001 保留不覆盖
```

真实 uvicorn + httpx 三接口 smoke：**22/22 通过**（v4.1 时期实测；启动、两个 uniqueId 上传、
中间/阶段末响应同构 `data{operator,status,outputFile,result}`（旧 `lineageFile` 已停用）、
文件对象含 3 清单 + manifest + 6 媒体（base64）、
非法 uniqueId 拒绝、严格布尔、跨任务 inputFile 拒绝、`/downloadFile` 404、removeFile 隔离与两任务物理隔离）。

Docker：**未运行**——本机未安装 docker（`docker --version` 不存在），因此不声称镜像验证通过。

样例回归基线（`dataset/sample/samples.jsonl`，13 行）：有效 8、坏行 1、拒收 4、配对 4 对（A2/B1/C1）、
已配对 7、未配对 1（`sample-0008`，仅与 `sample-0004` 共享 1 个关键词，低于默认阈值 2）、媒体携带 6。

## 10. 已知限制

1. 本工具只形成候选与证据，不做任何语义判定；语义过滤由 #12–#15 承接；
2. 媒体只做原样携带，不解码、不校验内容质量；
3. 中文关键词在缺少上游 `tokens` 时用二元字组规则，阈值 2 实际是「2 个重叠字组」，比人工分词宽松；
4. `source_id` 会回退 `metadata.source`，若上游把全部样本写成同一个来源标签，证据 A 会过度成组（见 `DEVELOPMENT_STATUS.md` 待决策项）；
5. 样例未包含真实可解码 MP4（仓库不外挂编码器），视频时间范围与携带由单测用合成记录覆盖；
6. 配对需要内存内保留有效记录，超大单文件需评估内存；
7. `stageId=S3` 是 demo 阶段约定，表1 正式环节为「数据加工」，待工程侧确认。
