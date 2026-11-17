# 算子 #02 核验场景编号 —— 数据集与 I/O 契约（dataset/DATA_SCHEMA.md）

- 算子正式编号：**#02**（表1 环节「输入核验」，demo 阶段标识 `S1`）
- 算子名：`scene_id_verification`（中文标签：核验场景编号）
- 版本：`v1.0.0`
- 数据形态：UTF-8 JSONL（一行一条记录），媒体资源以**相对路径**引用
- 模型：**无**（表1 模型列为 `--`；`statistics.model = null`）

> 本文件只描述**本算子实际读取与写出**的字段，不复制其他编号算子的 schema。

---

## 1. 上游输入（表1 #01 接入多模态批次 → #02）

### 1.1 承载方式

| 方式 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | 上传 zip 素材包，安全解压到 `OUTPUT_DIR/<uniqueId>/input/`；包内需含唯一 JSONL 入口（`02_scene_verified.jsonl` / `demo.jsonl` / `samples.jsonl` 优先，或由 `manifest.json` 的 `entrypoint` 指定） |
| `POST /predict` + `inputFile` | [v4.2] 传上一步 `outputFile` 恢复/落盘后的**任务目录内**入口路径（跨任务/穿越一律拒绝）；也支持 multipart `?detection=M02` + `file` 单文件流 |
| CLI `--input` | 本地调试可直接指向 JSONL 或素材目录 |

### 1.2 记录字段

> **两种上游行粒度都支持**：
> ① 样本级（一行一个样本，模态在顶层 `text`/`image`/`video`/`audio`）；
> ② 文件登记项级（`demo_10.6/01_接入多模态批次` 的 `01_workitems.jsonl`：一行一个文件条目，
> 模态在 `files[].modality` 或顶层 `modality`/`operator_01.modality`，
> 同 `sample_id` 多行且**不算重复**，只有「同 `sample_id` + 同一文件」重复才进异常清单）。
> 场景组始终按「组内模态并集」核验。

| 字段 | 类型 | 必需 | 本算子如何使用 |
| --- | --- | --- | --- |
| `sample_id` | string | **是** | 去重与溯源键。缺失 → 原始隔离区（`原始文件-缺字段`）；同批重复 → 异常清单（`流程-重复样本ID`，**不隔离**） |
| `files` | list[object] | 否 | 文件登记项 `[{modality, path/link}]`；无 `modality` 时按 `path` 扩展名推断模态 |
| `modality` / `operator_01.modality` | string | 否 | 单文件记录的模态兜底（`img`/`voice`/`vid`/`caption` 等别名归一） |
| `file_link` / `operator_01.file_link` | string | 否 | 单文件记录的媒体指针兜底；`oss://` 等远端链接只登记不搬运（`media_remote`） |
| `text` | object \| string | 否 | 非空即计一种模态 |
| `image` | object \| string | 否 | 非空即计一种模态；对象取 `path` 作媒体相对路径 |
| `video` | object \| string | 否 | 同上 |
| `audio` | object \| string | 否 | 同上 |
| `scene_id` / `sceneId` | string | 否 | 采集平台已有场景编号（优先级最高） |
| `group_id` / `groupId` / `scene` | string | 否 | 采集平台已有场景编号（次选，写法兼容） |
| `metadata.scene_id`（及上述同义键） | string | 否 | 顶层取不到时从 `metadata` 取 |
| `processing` | object | 否 | 上游算子命名空间；本算子**只写 `processing["02"]`**，不重置 `01`/`alignment`/`qc` 等既有节点 |
| `processing["02"].scene_id` + `.scene_source` | string | 否 | 若存在，视为本算子历史绑定 → **只绑定一次**，重跑沿用不重绑 |
| `capture_platform` / `capturePlatform` | string | 否 | 只读抄录到 `capture_readonly` |
| `device_concurrency` / `deviceConcurrency` | number | 否 | 只读抄录（本算子不调度设备） |
| `device_failover` / `deviceFailover` | any | 否 | 只读抄录 |
| `batch_id` / `source_id` / `authorization` / 其他 | any | 否 | 原样透传 |

### 1.3 判定规则

- **模态集合**（顺序固定 text → image → video → audio）：字段非空即计入；
  `null`、空串、空白串、空对象、空数组视为**不存在**。
- **场景编号来源**：
  1. `processing["02"].scene_id`（本算子历史绑定，只绑定一次）；
  2. 平台编号（第 1.2 节 `scene_id` 一族）；
  3. `scene_id_source=generate` 时补一个编号：`scene-auto-<sha1(uniqueId|sample_id)[:12]>`
     （确定性、可复现、不复用）。
- **场景组**：同一 `scene_id` 的记录合为一组；
  组的**模态并集** ≥ `min_modalities`（默认 2，可选 2–4）→ `verified`（融合集）；
  否则 → `insufficient_modalities`（**保留在产线、改走配对集，不隔离**）。
- **开关关闭**（`enabled=false`）：不核验、不补编号，全部记录 `processing["02"].status=0`、
  `process_status=disabled`，原样继续交 #3，不新增任何 `scene_id`/`group_id` 字段。

### 1.4 媒体路径约定

- 相对入口文件的相对路径，例如 `media/img_a.png`；
- **禁止**绝对路径（`/abs/...`、`C:/...`）、`..` 逃逸、反斜杠、URL、`~`；
- 源文件缺失计入 `media_missing`，路径非法计入 `media_errors`——**都不伪造文件**；
- 主产物引用到的媒体按原相对路径复制到运行目录。

### 1.5 编码与容错

| 情况 | 处置 | 清单 |
| --- | --- | --- |
| 空行/空白行 | 计数为 `blank_lines`，不产出条目 | — |
| JSON 解析失败 | `reason=json_parse_error`，记物理行号 | `02_origin_quarantine.jsonl` |
| 合法 JSON 但不是对象 | `reason=record_not_object` | `02_origin_quarantine.jsonl` |
| 缺 `sample_id` | `reason=missing_field: sample_id` | `02_origin_quarantine.jsonl` |
| 同批 `sample_id` 重复 | `reason=duplicate_sample_id`（流程状态，**不隔离**） | `02_rejects.jsonl` |
| 文件非严格 UTF-8 | 整次运行 `failed`，不留半截产物（不猜编码） | — |

---

## 2. 下游输出（#02 → 表1 #03 数据初筛 / #11 生成候选配对）

### 2.1 产物清单（`OUTPUT_DIR/<uniqueId>/scene_id_verification/runNNN/`）

| 文件 | 角色 | 条数口径 |
| --- | --- | --- |
| `02_scene_verified.jsonl` | main | `main_entries` = `fusion_records + pairing_records` |
| `02_scene_groups.jsonl` | sidecar | `scene_groups_total` |
| `02_routed_to_pairing.jsonl` | sidecar | `pairing_records` |
| `02_origin_quarantine.jsonl` | review | `origin_rejected`（原始文件问题） |
| `02_rejects.jsonl` | review | `duplicate_records`（流程-重复样本ID） |
| `02_scene_report.json` | review | 1 条运行级汇总 |
| `manifest.json` | - | `crossmodal-manifest-v1`，`entrypoint=02_scene_verified.jsonl` |
| `run.log` | - | 逐行动作日志 |
| `media/**` | - | 被引用媒体（原相对路径） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py`、`utils/mty_compliance.py` 代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.2 主产物记录字段

保留上游全部字段，并**追加/覆盖**下列字段（不改动上游 `processing` 其他节点）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scene_id` | string | 解析后的场景编号（`enabled=true` 时写；关闭时不写） |
| `group_id` | string | `grp-scene-<scene_id>`，与 #11 生成候选配对同名口径 |
| `scene_group` | object \| null | `{scene_id, group_id, status, scene_source, modalities, member_count, min_modalities}`；无编号时为 `null` |
| `processing["02"]` | object | 本算子节点，见下 |

`processing["02"]` 字段表：

| 字段 | 取值 |
| --- | --- |
| `operator` | 固定 `scene_id_verification` |
| `status` | int：1（`verified`/正常流程态）/ 0（`insufficient_modalities`/`no_scene_id`/`disabled`） |
| `process_status` | 原字符串：`verified` / `insufficient_modalities` / `no_scene_id` / `disabled` |
| `route` | `fusion`（融合集场景组） / `pairing`（改走配对集） |
| `scene_source` | `platform` / `supplemented` / `none` / `disabled` |
| `scene_id` / `group_id` | 与顶层一致 |
| `modalities` | 本条记录的模态（text→image→video→audio 顺序） |
| `scene_modalities` | 所在场景组的模态并集 |
| `scene_member_count` | 所在场景组成员数（无组为 0） |
| `min_modalities` | 本次生效阈值 |
| `bound_once` | 固定 `true`（编号只绑定一次） |
| `device_control` | 固定 `read_only`（不调度设备） |
| `enabled` | 本次运行是否启用 |
| `reason` | `null` / `insufficient_modalities` / `no_scene_id` / `tool_disabled` |
| `capture_readonly` | 平台并发与故障切换的只读抄录（可为 `{}`） |
| `model` | 固定 `null`（无模型） |

### 2.3 场景组摘要字段（`02_scene_groups.jsonl`）

`scene_id`、`group_id`、`status`、`scene_source`、`modalities`、`modality_count`、
`member_count`、`member_sample_ids`、`min_modalities`、`route`、`operator`、
`formal_operator_number`(=2)、`stage_id`(=S1)。

### 2.4 台账条目字段（`02_origin_quarantine.jsonl` / `02_rejects.jsonl`）

`line`、`sample_id`、`reason`、`message`、`label`、`stage`、`operator`、`action`、
`destination`、`event_time`、`batch_id`、`retry_count`(=0)。

- 原始隔离：`label ∈ {原始文件-无法解析, 原始文件-缺字段}`，`destination=raw_isolation`
- 异常清单：`label=流程-重复样本ID`，`destination=operator_rejects`

### 2.5 统计字段（`data.result.statistics` / `02_scene_report.json` / `manifest.json`）

`input_lines`、`blank_lines`、`input_records`、`valid_records`、`origin_rejected`、
`duplicate_records`、`main_entries`、`fusion_records`、`pairing_records`、
`pairing_no_scene_id`、`pairing_insufficient_modalities`、`pairing_disabled`、
`scene_groups_total`、`verified_groups`、`insufficient_groups`、
`scene_groups_verified_members`、`records_in_groups`、
`platform_scene_ids`、`supplemented_scene_ids`（**均为记录条数**，去重后的场景编号个数见
`scene_groups_total`）、`capture_readonly_records`、`media_carried`、`media_missing`、
`media_errors`、`media_remote`、`media_bytes`、`origin_entries`、`reject_entries`、`pairing_entries`、
`group_entries`、`enabled`、`disabled_records`、`empty_input`、`conservation_ok`、
`degraded`(=false)、`degradation_reason`(=null)、`active_backend`(=`rule`)、`model`(=null)。

守恒等式见 `README.md` §10。

---

## 3. 样例数据

`dataset/sample/`（由 `python dataset/sample/make_sample.py` 确定性生成，纯标准库、无随机数）：

- `demo.jsonl`：16 行，覆盖
  - `scene-A` 3 条（模态并集 4 种）→ 核验通过；
  - `scene-B` 2 条纯文本、`scene-C` 1 条纯图像 → 模态不足（改走配对集）；
  - `scene-D`（由 `group_id` 提供）1 条 `text+audio` → 通过；
  - 无编号 4 条（含 1 条三模态、1 条引用缺失媒体、1 条引用越界媒体路径）；
  - 1 条重复 `sample_id`、1 条缺 `sample_id`、1 行坏 JSON、1 行非对象、1 行空行。
- `media/`：6 个 PNG（zlib+struct 手写，无 Pillow）+ 1 个确定性伪 MP4 + 3 个 16kHz WAV（`wave` 模块）。
- `manifest.json`：`crossmodal-manifest-v1`，`entrypoint=demo.jsonl`。

**未使用 `media/img_missing.png`**：样例故意引用一个不存在的媒体以验证
`media_missing` 只记数、不伪造空文件的行为。

### 预期真实结果（`--override '{"enabled": true}'`，`min_modalities=2`）

| 指标 | 值 |
| --- | --- |
| `input_lines` / `input_records` / `blank_lines` | 16 / 15 / 1 |
| `valid_records` / `origin_rejected` / `duplicate_records` | 12 / 3 / 1 |
| `main_entries` | 11 |
| `scene_groups_total`（`verified` / `insufficient`） | 4（2 / 2） |
| `fusion_records` / `pairing_records` | 4 / 7 |
| `pairing_no_scene_id` / `pairing_insufficient_modalities` | 4 / 3 |
| `media_carried` / `media_missing` / `media_errors` / `media_remote` | 10 / 1 / 1 / 0 |

`--override '{"enabled": true, "scene_id_source": "generate"}'` 时：
`scene_groups_total=8`（`verified=5` / `insufficient=3`）、`fusion_records=7`、
`pairing_records=4`、`supplemented_scene_ids=4`（4 条补编号记录，编号一一对应不复用）。

默认（`enabled=false`，表1 默认关闭）：`fusion_records=0`、`scene_groups_total=0`、
`pairing_disabled=11`、`main_entries=11`，状态 `partial`（输入自带 3 行原始问题 + 1 条重复）。
