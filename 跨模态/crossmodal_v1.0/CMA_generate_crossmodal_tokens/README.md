# 算子 22：生成跨模态Token（crossmodal_token）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> 表1 v4.1 正式编号 **22**｜大环节 **5 样本构建**｜工具分类 **记录封装与输出**｜工具类型 **系统功能**
> 开关 **默认开启，不能关**｜是否必须 **必须**｜优先级 **P0**｜模型列 **--**（无模型、无外部服务、不联网）
> 版本 `v1.0.1`｜HTTP 端口 **8122**｜阶段 `S5`

## 1. 功能边界

### 1.1 做什么

把**已有质量结论、且不在隔离区**的对齐样本，写成带模态标记的 Token 与样本文件：

| 表1 要求 | 本实现落地 |
|---|---|
| 没有质量结论则不生成 Token，不拦、留产线 | 读 #20 结论；缺失/`skipped` → `22_pending.jsonl`（`no_quality_conclusion`），不生成、不隔离 |
| 按路线分文件写 `samples.jsonl` | `samples.jsonl`（全部已生成样本，固定名）+ `samples_<route>.jsonl` 分路线文件 |
| 文本中的模态位置写成 `<image>` / `<audio>` / `<video>` | `token_text = "<image> " + 文本`，标记按 `image→audio→video` 固定顺序，默认图文必以 `<image>` 开头 |
| 隔离区样本不进入该文件 | `processing["21"]`/`["20"]` 隔离、#17 `incomplete`、#18 `isolated`、#19 `isolated` → `22_isolated_skipped.jsonl` |
| 未开启的音文/视文/局部标注不产出对应文件 | `routes.audio_text/local_annotation` 默认 false，**开启才产出文件**；`routes.video_text` v1.0.1 起出厂默认 true（避免视频样本被当成「无路线」全进待处理） |
| 输出 Token 文件和样本文件，原文件仅存链接 | `22_tokens.jsonl` + `samples.jsonl`；**不搬运媒体**，只登记 `object_key`（`reference_only`） |
| 本工具不压缩、不改版本状态 | `compressed=false`、`version_state_changed=false`、`packaged=false` |
| 不能训练的样本单独列出 | `22_untrainable.jsonl`（授权不可训练，标注 `label_detail=样本-敏感`、建议去向 `sample_quarantine`），不进入 `samples.jsonl` |
| 旁路 flagged 单列（计划 §7） | `22_flagged.jsonl`：`#20 decision=flagged` 的样本仍生成 Token，但单列复核 |

### 1.2 不做什么

- **不加载任何模型、不产出任何分数**（`model=null`、`score_fields_produced=false`）。
- **不伪造分词**：无子词分词器，`subword_token_ids` / `tokenizer` 恒为 `null`；
  「Token」指模态标记封装（F-ALIGN-058/059），不是模型 token id。
- **不搬运、不复制媒体**（`media_copied=false`），不把文件大小当业务统计。
- 不压缩、不打包、不改版本状态（属 #23 语料包交付）。
- 不改写任何上游命名空间（`processing["19"|"20"|"21"|"17"|"18"]` 原样保留）。
- 不实现 `/downloadFile`（访问返回 404）。
- 不把 `data.outputFile`（文件内容对象）当作下一算子的输入路径，链式调用取 `data.result.outputFile` 路径。

## 2. 输入与输出

### 2.1 输入

- 入口：UTF-8 JSONL，一行一条 JSON 对象；支持扁平记录（`sample_id`）与成对记录（`pair_id` + `left`/`right`）。
- 形态一还接受 ZIP（可直接传 #21 的 `outputFile.zip`）：ZIP 解压后按 `manifest.json` / 唯一 `*.jsonl` 定位入口；媒体仍按记录中的相对路径只读引用。
- 读取的上游结论：**#19 候选池 → #20 质量结论 → #21 隔离结果 → #10 授权 → 版本/模态路线开关**（只读）。
- 媒体引用：只读取记录中声明的**相对路径**；非法路径（绝对、盘符、`..`、`http://`、反斜杠）→ 原始隔离区。
- 上游显式锁定标记（`locked` / `retry_locked` / `isolation_locked`，含 `metadata.*`）→ 按隔离跳过
  （规则 §2.3 的「第 3 次锁定」阈值由上游 #19/#21 判定，本算子只尊重标记，见 DATA_SCHEMA §7.6）。
- 字段级优先级表见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md) §1.2。

### 2.2 输出（`OUTPUT_DIR/<uniqueId>/crossmodal_token/<runNNN>/`）

| 文件 | 说明 | 下游 |
|---|---|---|
| `samples.jsonl` | **主输出**：全部已生成样本，原字段 + `processing["22"]` + `operator_22` | #23、#24 |
| `samples_image_text.jsonl` | 图文路线子集（默认开启） | 工程侧 |
| `samples_audio_text.jsonl` / `samples_video_text.jsonl` / `samples_local_annotation.jsonl` | 对应路线**开启时**才产出 | 工程侧 |
| `22_tokens.jsonl` | Token 文件：`markers` + `token_text` + 标记与词元引用校验结果 | #23、#24 |
| `22_metadata_index.jsonl` | 元数据索引：模态索引 + 对象存储链接 + 版本 | #23、#24 |
| `22_pending.jsonl` | 流程待处理台账（不隔离，留产线） | 主 agent / 人工 |
| `22_flagged.jsonl` | **旁路 flagged 单列**（#20 `flagged` 样本已生成 Token，另列复核） | #24 / 人工 |
| `22_untrainable.jsonl` | 不可训练样本清单 | 主 agent |
| `22_isolated_skipped.jsonl` | 隔离样本跳过台账 | #21 / 人工 |
| `22_rejects.jsonl` | 原始隔离（坏行、非对象、编码异常、缺标识、路径非法） | 原始文件排查 |
| `22_run.log` / `manifest.json` | 日志 / 运行清单 | 工程侧 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止写入血缘/阶段上下文、不再调用 `utils/mty_compliance.py`；`utils/lineage.py` 的时间戳/配置快照工具与 `utils/transport.py` 的响应打包仍在用，血缘写入代码保留存档）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

原因码表、统计守恒等式、字段清单见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md) §2–§5。

## 3. 配置

`configs/config.yaml`（未知键即失败）。关键项（详见 DATA_SCHEMA §6）：

```yaml
enabled: true                    # 总开关：默认开，不能关
exclude_isolated: true           # 隔离样本不进入样本文件（必须 true）
samples_file_name: samples.jsonl # 固定名
markers: {image: "<image>", audio: "<audio>", video: "<video>"}   # 只读固定值
routes: {image_text: true, audio_text: false, video_text: true, local_annotation: false}
missing_quality_policy: waiting_manual   # 或 failed（严格模式）
```

请求级覆盖白名单（`POST /predict` 的 `overrides`）：`enabled`、`routes`、`missing_quality_policy`。
例：`{"overrides": {"routes": {"audio_text": true}}}` 只对本请求开启音文路线。

### 3.1 任务书配置约束（配置员不改这些项）

| 页面项 | 控件 | 默认显示 / 可选 | 页面说明 | 代码约束 |
|---|---|---|---|---|
| 模板总开关 | 开关 | **默认开，不能关** | — | `enabled=false`（配置或 overrides）直接 `ConfigError`，启动/请求失败 |
| 1. 文字里用什么标记模态 | 只读 | 图像 `<image>`；音频 `<audio>`；视频 `<video>` | 没打开的路线不会生成对应文件 | `Markers` 固定赋值，改值即配置失败；运行时按样本实际模态选标记 |
| 2. 样本文件名 | 只读文本 | `samples.jsonl` | 隔离区里的样本不会写进这个文件 | `samples_file_name` 非 `samples.jsonl` 即配置失败；`exclude_isolated=false` 也失败 |

`routes`（图文/音文/视文/局部标注）是路线开关，不属于上述两个只读项；默认
`image_text=true`、其余 false，未开启路线不产出对应 `samples_<route>.jsonl`。
配置员不改标记/文件名，任务按上表默认值执行。

## 4. 算法 / 模型后端

**纯规则、流式处理，无模型、无外部服务、无网络。** 决策链为确定性规则（§1.1 + DATA_SCHEMA §2），
所有 `statistics` 均来自真实处理结果并与落盘产物行数核对。

### 4.1 降级与失败行为

| 情况 | 行为 |
|---|---|
| 上游暂无质量结论（正常业务等待） | 不生成 Token，写 `22_pending.jsonl`；若一条也没生成则 `status=degraded`，**不伪造结果** |
| `missing_quality_policy=failed`（严格模式） | 存在待处理记录时 `status=failed`（退出码 1） |
| 输入文件非 UTF-8 | 计入输入与坏行，写一条 `encoding_error` 拒绝条目，置 `file_read_incomplete=true`，不静默丢弃 |
| 全部行不可解析 | `status=failed` |
| 媒体本地不可见 | 不报错（对象存储链接策略），登记 `local_presence=absent` 并计入 `media_links_unresolved` |
| 守恒校验失败 | `status=failed`，`conservation_failures` 逐条列出 |
| 同一任务并发运行 | `.lock` 目录锁，第二个请求返回 busy（HTTP 400） |

## 5. 启动与运行

```bash
pip install -r requirements.txt

# CLI（单机批处理）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# HTTP 服务
python app.py                       # 或 uvicorn app:app --host 0.0.0.0 --port 8122
```

```bash
# 测试
python -m pytest tests/ -q
python tests/smoke_api.py           # 真实 uvicorn + 三接口全流程 smoke（31 项断言）
```

> Windows 提示：pytest 若因系统临时目录清理被安全策略拦截（表现为命令无输出并卡住），
> 可先建父目录再指定 `--basetemp`（本仓库验证时使用该方式，详见 `DEVELOPMENT_STATUS.md` §4.1）：
>
> ```powershell
> python -c "import pathlib; pathlib.Path('_pytest_tmp').mkdir(exist_ok=True)"
> python -m pytest tests/ -q -p no:cacheprovider --basetemp="$PWD\_pytest_tmp\t1"
> ```

## 6. CLI / API 示例

### 6.1 CLI

```powershell
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

真实运行结果（2026-10-08，本机 Python 3.9.7）：

```json
{"code": 0, "msg": "已生成 Token 3 条；样本文件 3 条；待处理 4 条；不可训练 1 条；隔离跳过 1 条；原始隔离 1 条",
 "data": {"status": "success", "runId": "run002", "entrypoint": "samples.jsonl",
          "runDir": "...\\output_smoke\\smoke_final\\crossmodal_token\\run002"}}
```

关键统计（`conservation_ok=true`，`conservation_failures=[]`）：

```json
{"input_records": 11, "valid_records": 10, "blank_lines": 1, "bad_lines": 1,
 "generated_records": 3, "pending_records": 4, "flagged_records": 1,
 "untrainable_records": 1, "isolated_skipped_records": 1, "origin_quarantined": 1,
 "rejects_entries": 2,
 "by_route": {"image_text": 3}, "primary_by_route": {"image_text": 3},
 "pending_by_reason": {"route_disabled": 2, "no_quality_conclusion": 1, "missing_text": 1},
 "isolated_skipped_by_reason": {"upstream_isolated": 1},
 "untrainable_by_reason": {"license_not_trainable": 1},
 "origin_by_reason": {"unsupported_media_path": 1},
 "bad_line_by_reason": {"json_parse_error": 1},
 "file_counts": {"samples_records": 3, "tokens_records": 3, "metadata_records": 3,
                 "pending_records": 4, "flagged_records": 1, "untrainable_records": 1,
                 "isolated_records": 1, "rejects_records": 2,
                 "route_image_text_records": 3},
 "media_links": 9, "media_links_unresolved": 9,
 "marker_validation_failures": 0, "model": null, "score_fields_produced": false}
```

> 说明：样例的 9 条媒体引用本地均不可见（`media_links_unresolved=9`），因为
> 「原文件仅保留对象存储链接」策略下不搬运媒体；这也验证了本算子**不会**因媒体缺失而伪造或失败。

### 6.2 API

> `isLastOperator` 仍必传（严格布尔），仅用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，
> 均返回 `outputFile`+`result`，不再用 `false` 只回血缘对象。

```bash
# 1) 上传（zip：入口 JSONL + 可选 manifest.json / 媒体）
curl -X POST "http://127.0.0.1:8122/uploadFile?uniqueId=task_a" -F "file=@bundle.zip"

# 2) 形态一：multipart/form-data 单文件流（处理型 status：1 成功/0 失败）
curl -X POST "http://127.0.0.1:8122/predict?detection=generate_crossmodal_tokens&uniqueId=task_a_file01" \
  -F "file=@demo.jsonl" -F "missing_quality_policy=waiting_manual"
# → {"status":1,"operator":"generate_crossmodal_tokens","uniqueId":"task_a_file01",
#    "runDir":".../crossmodal_token/run001","outputFile":".../run001/samples.jsonl",
#    "token_records":"22_tokens.jsonl","samples_jsonl":"samples.jsonl",
#    "manifest":"manifest.json","sample_count":3,"isolated_skipped_count":1,
#    "pending_count":4,"process_status":"success","compressed":false,
#    "version_state_changed":false}
# 失败 → {"status":0,"msg":"<原因>"}

# 3) 形态二：application/json 阶段链式（阶段内中间）
curl -X POST "http://127.0.0.1:8122/predict?operatorId=m22" -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_a","isLastOperator":false}'
# → {"code":0,"msg":"<运行消息>","data":{"operator":"generate_crossmodal_tokens","status":"success",
#    "outputFile":{"format":"crossmodal-files-v1",...},"result":{"uniqueId":"task_a","runDir":"...",
#    "inputFile":"...","outputFile":".../samples.jsonl","runId":"run001","statistics":{...},
#    "startedAt":"...","finishedAt":"..."}}}

# 4) 阶段末（isLastOperator=true）：响应结构与中间调用一致
curl -X POST "http://127.0.0.1:8122/predict" -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_a","isLastOperator":true}'

# 5) 清理（幂等）
curl -X POST "http://127.0.0.1:8122/removeFile?uniqueId=task_a"
```

### 6.3 输出示例

`samples.jsonl`（单行，节选）：

```json
{"sample_id": "demo-0001", "text": {"content": "一只橘猫趴在绿色的草地上", "language": "zh"},
 "image": {"path": "media/demo-0001.png", "modality": "image"},
 "admission": {"license": "可训练"},
 "processing": {"19": {...}, "20": {...},
                "22": {"operator": "crossmodal_token", "operator_number": 22, "stage_id": "S5",
                       "status": "generated", "route": "image_text", "routes": ["image_text"],
                       "markers": ["<image>"], "token_text": "<image> 一只橘猫趴在绿色的草地上",
                       "media_link_policy": "original_files_link_only", "media_copied": false,
                       "version_state_changed": false, "compressed": false,
                       "version": "v1.0", "human_verified": false, "review_required": true}},
 "operator_22": {"operator": "crossmodal_token", "status": "generated", "token_char_count": 20, ...}}
```

`22_tokens.jsonl`（单行，节选）：

```json
{"sample_id": "demo-0001", "route": "image_text", "markers": ["<image>"],
 "token_text": "<image> 一只橘猫趴在绿色的草地上", "token_char_count": 20,
 "text_reference": {"field": "text.content", "length": 12, "sha256": "a74c6e2a…"},
 "media_links": [{"modality": "image", "path": "media/demo-0001.png",
                  "object_key": "crossmodal/demo-0001/media/demo-0001.png",
                  "storage": "object_storage_reference", "link_status": "reference_only",
                  "local_presence": "absent"}],
 "marker_validation": {"valid": true, "declared": ["<image>"], "required": ["<image>"],
                       "missing": [], "unexpected": [], "reference_ok": true},
 "subword_token_ids": null, "tokenizer": null, "version_state_changed": false, "compressed": false}
```

`22_pending.jsonl`（单行，节选）：

```json
{"line": 4, "sample_id": "demo-0004", "reason": "route_disabled", "label": "流程-路线未开启",
 "action": "waiting_manual", "destination": "pipeline_wait", "status": "pending",
 "detail": "适用的路线 ['audio_text'] 均未开启，不产出对应文件", "record": {...}}
```

形态二 `/predict` 响应（v4.2 统一结构，节选）：

```json
{"code": 0, "msg": "已生成 Token 3 条；样本文件 3 条；待处理 4 条；不可训练 1 条；隔离跳过 1 条；原始隔离 1 条",
 "data": {"operator": "generate_crossmodal_tokens", "status": "success",
          "outputFile": {"format": "crossmodal-files-v1", "entrypoint": "samples.jsonl",
                         "totalBytes": 0,
                         "files": [{"path": "samples.jsonl", "role": "records", "encoding": "utf-8",
                                    "size": 0, "content": "..."},
                                   {"path": "manifest.json", "role": "sidecar", "encoding": "utf-8",
                                    "size": 0, "content": "..."}]},
          "result": {"uniqueId": "smoke_final", "runDir": "…\\crossmodal_token\\run002",
                     "inputFile": "…\\dataset\\sample\\demo.jsonl",
                     "outputFile": "…\\run002\\samples.jsonl", "runId": "run002",
                     "statistics": {"input_records": 11, "generated_records": 3,
                                    "pending_records": 4, "isolated_skipped_records": 1},
                     "startedAt": "2026-10-08T14:30:49+08:00",
                     "finishedAt": "2026-10-08T14:30:50+08:00"}}}
```

> 说明：`outputFile.files` 实际为主产物 + 各路线/台账/日志 + `manifest.json`（示例仅节选）；
> 业务失败为 `{"code":1,"msg":"<原因>","data":{}}`，参数不合法为 HTTP 422 + `code=1`。

## 7. 资源需求

| 项 | 值 |
|---|---|
| CPU / 内存 | 单核即可；常驻内存 < 200 MB（流式逐行，不整文件载入） |
| GPU | 不需要 |
| 网络 | 不需要（不下载任何数据/权重） |
| 磁盘 | 输入副本 + 输出 JSONL；**不复制媒体**，故输出体积远小于输入媒体量 |
| 依赖 | fastapi 0.128.8 / uvicorn 0.39.0 / pydantic 2.13.5 / PyYAML 6.0.1 / python-multipart 0.0.20 |

## 8. 已知限制

1. 未接入真实对象存储：`object_key` 为按 `identifier + 相对路径` 生成的稳定键，
   `link_status=reference_only`、`object_storage_connected=false`；接入后需替换为真实 OSS key。
2. 无子词分词：`subword_token_ids=null`；如需真实 token id，应由下游带分词器的环节补齐，
   本算子不伪造。
3. 媒体引用识别依赖上游字段名（见 DATA_SCHEMA §1.2）；若上游新增字段名，需同步 `utils/upstream.py`。
4. 重复 `sample_id` 按原始隔离登记（`duplicate_identifier`），未做静默去重（见 DATA_SCHEMA §7.5）。
5. `local_annotation` 路线的开启判断依赖 #17/#18 的状态或结构证据；两侧口径不同时以 `status` 为准。
6. Docker 镜像未实际构建（环境无 Docker），见 §9。

## 9. 真实测试结果（2026-10-30，Windows / Python 3；2026-11-04 复核）

```text
$ python -m pytest tests/ -q
129 passed, 31 skipped in 4.92s      # 2026-10-30
131 passed, 31 skipped in 6.67s      # 2026-11-04（video_text 出厂默认开启 + 2 例）

$ python tests/smoke_api.py     # 真实 uvicorn（127.0.0.1:8122），v4.2 三接口全流程
total=31  passed=31  failed=[]

$ python -m json.tool map.json      # 通过
$ python -m compileall -q .         # 通过
$ docker build …                    # 未运行（环境无 Docker，镜像未经运行验证）
```

`tests/` 覆盖：正常样例、边界值（空文件/全坏行/缺标识/非法路径/重复标识）、损坏 JSONL、
缺字段、错误配置、错误编码（GBK 输入）、任务隔离（两个 uniqueId 物理隔离 + 跨任务 inputFile 拒绝）、
路径安全（zip-slip、目录穿越、绝对路径、保留名）、中间/阶段末同构返回形态、
`outputFile` 文件内容对象可恢复并再次回传、`/downloadFile` 404、`/removeFile` 幂等、以及本算子的
降级（`degraded`）与严格失败（`failed`）路径。

## 10. 目录结构

```text
22_生成跨模态Token/
├── app.py                  # HTTP 三接口
├── inference.py            # 编排：校验 → 流式判定 → 分路线产物 → manifest
├── processor.py            # CLI 入口
├── configs/config.yaml     # 配置（未知键即失败）
├── utils/
│   ├── names.py            # 产物名与业务常量（唯一来源）
│   ├── io.py               # 路径安全 / 流式 UTF-8 / 原子写入 / 任务锁 / 安全解压
│   ├── logger.py           # 逐行日志
│   ├── lineage.py          # 血缘 / 阶段上下文 / 配置快照（v4.2 停用，存档）
│   ├── transport.py        # crossmodal-files-v1 文件对象与清单
│   ├── schemas.py          # 严格配置与请求模型
│   ├── upstream.py         # 上游结论取值与归一化（只读）
│   └── tokens.py           # ★业务规则（表1 #22）
├── dataset/
│   ├── DATA_SCHEMA.md      # 数据契约
│   └── sample/             # demo.jsonl / manifest.json / make_sample.py
├── tests/                  # 129 个单测 + smoke_api.py（31 项 HTTP 检查）
├── output/                 # 运行期输出根目录（保持为空）
├── map.json / requirements.txt / Dockerfile
├── README.md / OPERATOR_MANUAL.md / HANDOFF.md / DEVELOPMENT_STATUS.md
└── DEVELOPMENT_PLAN.md / TECHNICAL_DESIGN.md   # 任务输入计划（只读）
```

## 11. 术语与编号对照

| 项 | 值 |
|---|---|
| 正式编号 | 22（与 `DEVELOPMENT_PLAN.md`、`TECHNICAL_DESIGN.md`、`map.json`、表1 一致） |
| 工程 slug | `crossmodal_token`（`operator` 字段、输出子目录名） |
| 中文名称 | 生成跨模态Token |
| 阶段 | `S5`（表1 第 5 环节「样本构建」） |
| 端口 | 8122 |
| 外部依赖算子 | 上游 #19/#20/#21/#10/#17/#18；下游 #23/#24 |


