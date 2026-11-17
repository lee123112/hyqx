# 算子说明书：核对输入对象准入（表1 #10）

## 1. 定位

| 项 | 内容 |
| --- | --- |
| demo ID | `input_admission` |
| 正式编号 | **10**（表1 第 10 项「核对输入对象准入」） |
| 阶段 | `S3`（表1 第 3 环节“数据加工”；demo 五阶段中属“跨模态数据对齐标注”） |
| 上游 | `#9 准备候选描述`；登记信息核对 `#1 接入多模态批次` |
| 下游 | `#11 生成候选配对`（主清单）；`#21`（隔离台账）；`#9`/`#1`（退回清单） |
| 服务端口 | 8110（`map.json`） |
| 算法类型 | 纯规则校验/分流；**无模型、无权重、不训练、不联网** |

## 2. 处理流程

```
输入 JSONL（#9 输出）
  └─ 逐行严格 UTF-8 解码
       ├─ 坏行（解析失败/非对象）───────────────► 原始隔离台账（line + reason）
       └─ 有效记录
            ├─ ① sample_id 非空？ 否 ──────────► 原始隔离区（missing_sample_id）
            ├─ ② 媒体引用能否打开？
            │     否（缺失/空/格式不符/签名不符/解码失败）
            │                                   ► 原始隔离台账（原始文件-损坏/空/无法解析）
            ├─ ③ 来源 ID、批次号齐备？ 否 ──────► 退回 #1 批次登记（不隔离）
            ├─ ④ 描述有效（非空、非文件名、非标签、5–500 字）？
            │     否 → 未达退回上限 ───────────► 退回 #9 候选描述（不隔离）
            │          已达退回上限 ───────────► 样本隔离库 + 锁定
            ├─ ⑤ 授权为“可训练”？ 否 ──────────► 样本隔离库（样本-敏感）
            ├─ ⑥ 模态数 ≥ min_modalities？ 否 ─► 样本隔离库（样本-语义不通）
            └─ 通过 ───────────────────────────► 主清单 + admission / processing["10"]
```

通过记录会被追加 `admission`；被隔离/退回的记录不做任何改写（只在台账里整条留档）。
只有主清单仍引用的媒体会被复制到本次运行目录，隔离记录的媒体不复制（避免孤儿文件）。

## 3. 输入

- 入口：`inputFile`（阶段内串联）或任务 `input/` 内的入口 JSONL（`manifest.json` 优先）；
- 每行一条记录；严格 UTF-8；
- 读取字段与媒体路径规则见 `dataset/DATA_SCHEMA.md §1`。

## 4. 输出

| 文件 | 说明 |
| --- | --- |
| `10_admitted.jsonl` | 合格待配对对象池 → #11 |
| `10_return.jsonl` | 退回/待补（`destination` ∈ `09_准备候选描述` / `01_接入多模态批次`） |
| `10_origin_quarantine.jsonl` | 原始隔离台账（文件损坏/空/无法解析 + 坏行） |
| `10_sample_quarantine.jsonl` | 样本隔离库台账（授权、模态、退回超限锁定） |
| `manifest.json` / `run.log` / `media/` | 运行、审计与携带媒体 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`lineage.py`/`mty_compliance.py` 代码保留存档）；原血缘中的统计/配置快照等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

统计守恒：`input = valid_records + bad_lines`，
`valid_records = admitted + returned + origin_quarantined + sample_quarantined`。

## 5. 配置

见 `configs/config.yaml` 与 `README.md §3`。业务参数可由 `/predict` 的 `config` 覆盖，
`operator`/`stage_id`/`required_license`/`allowed_extensions` 为只读，携带即报错。

## 6. 接口

| 接口 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | 上传 zip 素材包，安全解压到 `OUTPUT_DIR/<uniqueId>/input`（两阶段原子提交） |
| `POST /predict` | [v4.2] JSON `{uniqueId, isLastOperator, inputFile?, config?}` 或 multipart `?detection=M10` + `file`；中间与阶段末同构，均回 `{operator, status, outputFile, result}`（不再回 `lineageFile`） |
| `POST /removeFile?uniqueId=` | 删除整个任务，幂等 |
| ~~`/downloadFile`~~ | **不提供**（未注册路径统一 404 信封） |

响应壳：`{"code": 0|1, "msg": "...", "data": {...}}`。

## 7. 异常与降级

| 情形 | 行为 |
| --- | --- |
| 单行 JSON 损坏 / 顶层非对象 | 逐行写原始隔离台账，继续处理；运行 `status=partial` |
| 整文件非严格 UTF-8 | `InputError`：HTTP 400 / CLI `code=1`，不猜编码 |
| 统计不守恒 | `InputError`，并清理本次产物 |
| `config` 非法 / 缺字段 / 类型错 | HTTP 422 或 `code=1`，不采用默认值兜底 |
| `uniqueId` 非法（空、分隔符、盘符、保留名） | HTTP 422，不创建任何目录 |
| `inputFile` 出任务目录 | HTTP 400 |
| decode 模式缺解码器且 `allow_degraded=false` | `DependencyError`：HTTP 400 / CLI `code=1`，**不伪造已解码** |
| decode 模式缺解码器且 `allow_degraded=true` | 该文件标 `header_only`，运行 `status=degraded` |
| 同任务并发写 | `TaskBusyError`（任务锁），`code=1`；不同任务互不影响 |

## 8. 性能与资源

- 流式逐行处理，内存与文件大小无关（只与单行大小相关）；
- 纯 CPU、单进程即可（任务锁保证互斥）；无 GPU、无网络；
- 本机实测样例（16 行 + 5 个媒体）单次运行 < 1 秒。

## 9. 交付与验证

```
python -m pytest tests/ -q                                                     # 68 passed
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
python -m json.tool map.json
python -m compileall -q .
```

Docker 镜像**未实际构建**（开发环境无 Docker）；`Dockerfile` 与
`packaging/prepare_package.py` 已按契约提供，容器侧首次构建后需回归上表四条命令。
