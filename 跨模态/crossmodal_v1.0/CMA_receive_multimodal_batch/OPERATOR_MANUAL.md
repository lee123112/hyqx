# 算子 01「接入多模态批次」说明书

## 1. 基本信息

| 项 | 值 |
| --- | --- |
| 正式编号 | **#01** |
| 中文名称 | 接入多模态批次 |
| 英文标识 | `multimodal_batch_intake` |
| 目录 | `跨模态/demo_10.6/01_接入多模态批次` |
| 环节 / 阶段 | 第 1 环节「输入核验」，`stageId=S1`，**阶段首算子** |
| 类型 | 系统功能（表1 模型列 `--`） |
| 优先级 / 开关 | P0；总开关默认**开启**（`enabled=true`），可关闭（关闭则本环节不跑） |
| 端口 | 8101（demo_10.6 约定 8100+编号） |
| 入口 | `app:app`（HTTP）/ `processor.py`（CLI） |
| 版本 | `v1.0.0` |

## 2. 业务职责

工程侧把「批次清单 + 解压后的文件清单 + 来源/批次/授权等登记信息」交给本算子，本算子完成三件事：

1. **准入校验**（表1 冻结）：来源是否在五类之内、文件扩展名是否在四组白名单内、授权结论是否为「可训练」；
   可选的文件可读性检查（本地文件非空 + 文件头签名与扩展名一致）。
2. **登记**：校验通过的对象写入工作集 `01_workitems.jsonl`，**只登记原文件链接**（不复制媒体），
   并把 `operator_01` / `processing["01"]` 命名空间写入记录，供下游与溯源使用。
3. **分流**：不合格对象按表1 口径落到三类台账，坏行作为拒绝子类并入原始隔离台账。

```
                 ┌──────────────┐
批次清单 ──────► │ 01 接入多模态批次 │
                 └──────┬───────┘
   原文件问题（缺 sample_id / 链接不可搬运 / 格式不在白名单 / 文件空·打不开·头不符 / 坏行）
                        ├──► 01_origin_quarantine.jsonl  【原始文件-无法解析】→ 原始隔离区
   授权不是「可训练」    ├──► 01_sample_quarantine.jsonl  【样本-敏感】→ 样本隔离库
   缺登记要素 / 重复登记  ├──► 01_return.jsonl             【流程-待补登记】→ 采集平台批次登记
   全部通过              └──► 01_workitems.jsonl          【流程-可加工】→ 工作集（交 #02 或 #03）
                              01_batch_registry.json       批次台账
```

判定优先级（保证唯一归口）：`结构 → 链接可搬运 → 格式白名单 → 本地文件 → 授权 → 来源/模态 → 登记要素 → 通过`。

## 3. 表1 冻结口径（不允许在页面上改）

| 项 | 取值 |
| --- | --- |
| 数据来源 | 采集平台批次 / 预训练文本产线交付 / 预训练图像产线交付 / 预训练音频产线交付 / 预训练视频产线交付 |
| 允许文件格式 | 文本 `txt/json/csv`；图像 `jpg/png/webp`；音频 `wav/mp3/flac`；视频 `mp4/mov/avi` |
| 授权 | 只有「可训练」可以进入加工（待确认/不可训练/无法确认 → 样本隔离库） |
| 原文件处理 | 只保存链接，不在算子间隐式复制 |

## 4. 配置项

见 `configs/config.yaml` 与 `dataset/DATA_SCHEMA.md` §4。
可由 `/predict` 的 `config` 或 CLI `--override` 覆盖的只有 4 项：
`enabled`、`file_check_mode`（`whitelist|header|strict`）、`dedupe`、`check_source_modality`；
其余（编号/名称/版本/阶段、`allowed_license`、`sources`、`file_formats`、`transport`）为冻结项，
尝试覆盖一律 `code=1`。

## 5. 接口

| 接口 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | 上传批次清单（单个 zip 或 `file`/`files` 多文件）；返回入口名与文件数 |
| `POST /predict` | [v4.2] 双协议：JSON（`uniqueId` + 布尔 `isLastOperator` + 可选 `inputFile`/`config`）；multipart 单文件（`?detection=M01`，`file` 必传） |
| `POST /removeFile?uniqueId=` | 删除任务目录，幂等 |
| `GET /health` | 健康检查（声明三接口、`downloadFile=false`） |

**不实现 `/downloadFile`**；未匹配路径统一 HTTP 404 + `{"code":1,"msg":"接口不存在","data":{}}`。

返回形态（[v4.2] 两种 `isLastOperator` 同形）：
- `data = {operator, status, outputFile, result}`，`outputFile` 是 `crossmodal-files-v1` 内容对象；
- 不再返回 `lineageFile`，运行目录也不再写 `lineage.json` / `stage_context.json`；
- 形态一（multipart）返回 `{status, uniqueId, batch_id, accepted, isolation_records, process_status}`，
  处理型 `status` 为 int（1 正常/0 失败）。

## 6. 运维要点

- `OUTPUT_DIR`：输出根目录，真实产物在 `OUTPUT_DIR/<uniqueId>/multimodal_batch_intake/<runId>/`；
  重跑自动取新 `runId`，不覆盖历史；
- 任务锁：`OUTPUT_DIR/.locks/<uniqueId>.lock`，同任务并发写会被拒绝，不同 `uniqueId` 可并行；
- 限额（环境变量）：`MAX_UPLOAD_BYTES`、`MAX_UNPACKED_BYTES`、`MAX_UPLOAD_FILES`、`MAX_RESPONSE_BYTES`；
- 失败排查：先看 `failure.json`（结构化失败原因）与 `01_run.log`（时间+动作+真实数量）。

## 7. 验收要点

1. `01_workitems.jsonl` 中每条对象只保存链接，运行目录内**没有**媒体副本；
2. 三类台账的 `label/action/destination` 分别为
   【原始文件-无法解析】/isolate/原始隔离区、【样本-敏感】/isolate/样本隔离库、【流程-待补登记】/return/采集平台批次登记；
3. 统计守恒五项全部成立（见 `dataset/DATA_SCHEMA.md` §2.6），守恒不成立时运行失败而不是给出漂亮数字；
4. 阶段首不传 `inputFile` 能跑通；`isLastOperator=true/false` 均返回 `outputFile` + `result`（[v4.2] 不再返回 `lineageFile`）；
5. 两个 `uniqueId` 产物互不污染，非法 `uniqueId` 与跨任务 `inputFile` 被拒绝。
