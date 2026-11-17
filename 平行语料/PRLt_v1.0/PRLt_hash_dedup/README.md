# 算子 7：哈希去重

对规范化后的「源文 + 目标文」组合做精确去重，保留首次出现，重复记录写入可追踪清单。
只做精确组合键去重：不删除近义句、不做单边去重、不做模糊相似判断、不输出模型分数。

- 算子编号：7
- 算子名：`parallel_pair_deduplication`
- 版本：`v1.0.0`
- 上游输入：`06_length_filtered.jsonl`（严格 UTF-8）
- 输出：`07_deduped.jsonl`、`07_duplicates.jsonl`、`07_rejects.jsonl`、`artifacts/stage_07_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）
- 端口：`8007`

## 目录结构

```
PRLt_hash_dedup/
├── processor.py            # 核心处理逻辑 + CLI
├── inference.py            # FastAPI 三接口
├── configs/config.yaml     # 默认配置
├── utils/                  # io / lineage(停用) / logger / schemas / artifacts / timeutil
├── tests/                  # test_operator.py / test_api.py
├── dataset/
│   ├── DATA_SCHEMA.md
│   └── sample/demo.jsonl
├── map.json
├── requirements.txt
└── Dockerfile
```

## 产物

`OUTPUT_DIR/<uniqueId>/` 下：

- `07_deduped.jsonl`：保留的首次出现记录
- `07_duplicates.jsonl`：重复清单（行号 / `duplicate_of` / `dedup_key` / 原记录 / `duplicate_pair`）
- `07_rejects.jsonl`：结构异常清单（损坏 JSON、非对象行、缺字段、类型错误、分隔符冲突）
- `artifacts/stage_07_<uniqueId>.zip`：阶段产物（主输出、异常清单、`run.log`）
- `run.log`：逐行时间 + 动作 + 真实统计的日志

> 重复清单是本算子的业务产物；按无血缘契约，`stageZip` 只打包主输出、异常清单与日志。

## 接口（三接口）

| 接口 | 说明 |
|---|---|
| `POST /uploadFile?uniqueId=` | 上传句对文件到 `OUTPUT_DIR/<uniqueId>/input/` |
| `POST /predict` | Body：`{uniqueId, isLastOperator, operatorId?, inputFile, params?, config?}`；本算子位于阶段内部（上游算子 6），`inputFile` 必传 |
| `POST /removeFile?uniqueId=` | 删除整个任务目录 |

外层响应统一为 `{code, msg, data}`；`code=0` 表示成功。`/predict` 成功响应 `data` 固定 4 个字段
（不再返回 `lineageFile`）：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "outputFile": "<中间算子=输出文件路径；阶段末=输出文件内容>",
    "rejectFile": "<异常清单路径>",
    "stageZip": "<阶段产物 zip 路径>",
    "storageUri": "<对象存储地址：file:// 或 local-mirror://>"
  }
}
```

- 中间调用（`isLastOperator=false`）：`data.outputFile` 是**输出文件路径**，下一算子 `inputFile` 直接用它。
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**输出文件内容**（UTF-8 文本）。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_07_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- `operatorId` 选传（若传须为 `"07"`，否则 `code=1`）；`params`（或兼容旧参数 `config`）覆盖配置，`params` 优先。
- 处理失败（配置错误、空输入、去重状态超限）返回 `code=1` 且 `data=null`，错误原因写入 `msg`。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.6 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_哈希算法` | `hash_algorithm` | `sha256`（默认）/`md5`（仅为兼容）；规范默认 MD5，本算子按现有配置默认 SHA-256 |
| `param_跨来源去重开关` | 无对应字段 | 当前版本去重状态按 `uniqueId` 隔离、不跨任务共享，也不维护样本指纹库；如需跨来源去重需扩展独立指纹库能力 |

## 去重键

```text
dedup_key = "<algorithm>:<hexdigest>"
payload   = canonical(source) + "\x1f" + canonical(target)
canonical = 归一化(NFC) → casefold（可选）→ 连续空白折叠 + 首尾裁剪（可选）
```

键同时使用两边文本，因此「仅源文相同」或「仅目标文相同」的句对仍会被保留。
规范化只用于建键，不改写输出原文。分隔符固定写入配置；原文含 `\x1f` 的行按
`separator_in_text` 拦截（该控制字符应由上游算子 3 清除）。

## 配置

```yaml
hash_algorithm: sha256        # sha256（默认）| md5（仅为兼容）
unicode_normalization: NFC    # NFC | NFD | NFKC | NFKD | none
casefold: true
collapse_whitespace: true
keep_first: true              # 本版本仅支持 true，配置为 false 明确失败
max_tracked_pairs: 5000000
text_fields: [source, target]
input_encodings: [utf-8]
```

请求体里的 `params`/`config` 会覆盖配置文件对应项，未知字段直接报错。

## 状态语义

- `success`：读完且无异常行。
- `partial`：有结构异常行进 `07_rejects.jsonl`。
- `failed`：配置错误、输入为空、编码失败、去重状态超过 `max_tracked_pairs`。
  此时 `outputFile` 为 `null`，不产出半成品，也不生成阶段产物 zip。

## 运行

```bash
pip install -r requirements.txt

# 测试
python -m pytest tests/ -q

# CLI
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 服务
python inference.py            # http://0.0.0.0:8007
```

Docker：

```bash
docker build -t operator-07-dedup .
docker run -p 8007:8007 -e OUTPUT_DIR=/app/output operator-07-dedup
```

## 测试与验证结果（2026-10-19 无血缘改造后实跑）

```text
python -m pytest tests -q        → 33 passed in 2.22s
python -m compileall -q .        → exit 0
python -m json.tool map.json     → exit 0
```

CLI smoke：

```text
status=success statistics={'total': 8, 'passed': 5, 'rejected': 0, 'duplicates': 3,
'unique_pairs': 5, 'duplicate_pairs': 3, ...}
stageZip=output_smoke/smoke_cli/artifacts/stage_07_smoke_cli.zip
storageUri=local-mirror://smoke_cli/stage_07_smoke_cli.zip
任务目录无 lineage.json
```

## 责任边界与已知限制

- 只做精确组合键去重，不做模糊/近义/单边去重，不做语言检测。
- 不输出任何模型分数；本节点不依赖外部模型，因此不存在伪造分数的路径。
- 去重状态（`seen`）驻留内存，规模受 `max_tracked_pairs` 限制；超过上限**明确失败**，
  本版本不提供 SQLite 落盘方案，也不静默截断去重范围。
- `keep_first` 仅支持 `true`（保留首次出现）。保留末次出现需缓存全部唯一记录，本版本未实现。
- 去重状态不跨 `uniqueId` 共享，也不做跨任务增量去重（规范 `param_跨来源去重开关` 当前无实现）。
- 正式接口不提供 `/downloadFile`；不再产出或返回任何血缘文件。
