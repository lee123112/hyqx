# 算子 3 文本清洗标准化与分词（text_normalization）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> 跨模态 demo 算子，阶段 S2 首算子；demo ID `text_normalization`，服务端口 8103。
> **表1 口径**：本算子只做兜底清洗（乱码/控制字符、空文本、标点统一），**不分词**；上游已有的分词结果原样沿用。
> 规范来源：`DEVELOPMENT_PLAN.md`、`TECHNICAL_DESIGN.md`、上级 `DEVELOPMENT_CONTRACT.md`、`DATA_SCHEMA.md`。

## 1. 功能与边界

- 只做三项（`enabled=true` 时）：删除替换字符（U+FFFD）等乱码、删除控制/代理字符（保留 `\t`/`\n`/`\r`）、把中文全角标点统一为半角 ASCII（如 `，`→`,`、`。`→`.`、`“”`→`"`）；
- **不分词、不去重、不改大小写、不解析 HTML、不删广告文本、不合并空白**；不新增 `tokens`/`token_offsets`，上游已有字段原样沿用；
- 保留完整样本：`sample_id`、`processing`、`alignment`、`qc`、`metadata` 与未知字段原样带过；媒体引用随本次运行目录搬运；
- 无文本但 video+audio 合法时保留并标 `not_applicable`；清洗后文本为空（含纯空白）的图文样本进 `03_rejects.jsonl`（`empty_text_after_cleaning`）；
- 本算子**不做**语言识别（`lang` 为声明值）、不做语义改写、不做质检判定，也不新增 `/downloadFile` 业务能力。

## 2. 输入与输出

| 项 | 约定 |
|---|---|
| 输入 | 上游 `02_screened.jsonl`（S2 阶段首，由工程侧重新上传）；后续阶段可传同一任务内 `inputFile` |
| 主输出 | `<output>/<uniqueId>/text_normalization/runNNN/03_text_cleaned.jsonl` |
| 旁路 | 同目录 `03_rejects.jsonl`（含 `sample_id`、行号、原因、证据） |
| 控制文件 | `manifest.json`、`run.log` |
| 媒体 | 记录仍引用的媒体复制到本次 `runNNN/media/`，样本内相对路径重写 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py` 仅保留 `now_iso` 时间工具、`utils/mty_compliance.py` 代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

统计守恒：`total_lines == valid_records + bad_lines`，`output + rejected == valid_records`，`not_applicable` 是 output 的子集。

## 3. 算法与处理顺序

1. 严格 UTF-8 读取，逐行 JSON 解析；文件不是 UTF-8 → `invalid_encoding`，坏行/非对象记录进异常清单并计数；
2. 取记录文本（`text` 为字符串，或对象取 `content`、回退 `original`）；无 `text` 字段 → `not_applicable`；文本为空但有媒体 → `missing_description` 拒绝；
3. 开启 `enabled` 时依次：删除 U+FFFD → 删除控制/代理字符（保留 `\t`/`\n`/`\r`）→ 全角标点映射为半角 ASCII；
4. 清洗后为空 → `empty_text_after_cleaning` 进异常清单；无字母数字 → `unreadable_text` 拒绝；
5. 组装 `text.{content,original,lang}`（上游已有 `tokens`/`token_offsets` 原样保留）与 `processing["03"].metrics`；媒体按记录搬运。

## 4. 配置

配置文件 `configs/config.yaml`；`operator`、`stage_id`、`io`、`server` 为部署身份/限额，**请求 `config` 不能覆盖**。

| 键 | 默认 | 说明 |
|---|---|---|
| `operator.name` / `operator.version` | `text_normalization` / `v1.0.0` | 部署身份，请求不可改 |
| `stage_id` | `S2` | 阶段标识，部署固定 |
| `enabled` | `false` | **唯一业务开关**：开启后执行三项清洗；关闭时记录透传并标 `not_applicable` |
| `lang` | `zh` | 声明语言，写入 `text.lang`，部署固定 |
| `io.output_root` | `output` | 输出根；`OUTPUT_DIR` 环境变量优先 |
| `server.max_files` | `1000` | 单素材包最大文件数 |
| `server.max_unpacked_bytes` | `67108864` | 解压总大小上限（64MiB，demo 限额） |
| `server.max_response_bytes` | `67108864` | 响应序列化上限（64MiB，demo 限额） |

仅 `enabled` 可由请求 `config` / CLI `--override` 覆盖；其余为部署固定项。分词、HTML 清洗、大小写、空白合并、广告正则等旧参数已不再支持，写入即配置失败。

## 5. 安装与运行

```bash
pip install -r requirements.txt

# 生成固定样例（真实可解码媒体，合成数据仅用于功能验证）
python dataset/sample/make_sample.py

# CLI
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_03 \
    --output-root output/smoke
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_03 \
    --output-root output/smoke --override enabled=true

# 服务（手动演示用，自动验证优先 TestClient）
uvicorn app:app --host 127.0.0.1 --port 8103
```

CLI 退出码：成功/部分完成/允许降级为 0，执行失败为 1。业务参数只通过 `config`/`--override`，不接收任意请求路径当配置。

## 6. 接口（严格三接口）

统一信封 `{"code","msg","data","success","message"}`。`/downloadFile` 保留路径但明确返回 404（本 demo 不实现下载）。

| 接口 | 请求 | 说明 |
|---|---|---|
| `POST /predict` | [v4.2] 形态二：JSON（`uniqueId`、严格 bool `isLastOperator`、可选 `inputFile`、`config`），或带 `request`（JSON 字符串）+ `file`（zip）的 multipart；形态一：不带 `request` 字段的 multipart（`?detection=M04`，`file` 必传） | 见下方统一响应 |
| `GET/POST /downloadFile` | `uniqueId`（可选） | 固定 404 `not_implemented` |
| `POST /removeFile` | JSON `{"uniqueId": "..."}` | 删除任务目录；不存在时 `deleted=false`、`success=false` |

[v4.2] `isLastOperator` 仍必传，用于标记阶段末调用；中间与阶段末响应同构，均返回 `data={operator, status, outputFile, result}`，不再返回 `lineageFile`，也不再写 `lineage.json`/`stage_context.json`：

```json
{
  "code": 0, "msg": "产出 6 条，拒绝 7 条，坏行 0 行", "success": true,
  "message": "产出 6 条，拒绝 7 条，坏行 0 行",
  "data": {
    "operator": "clean_normalize_text",
    "status": "success",
    "outputFile": {
      "format": "crossmodal-files-v1",
      "entrypoint": "03_text_cleaned.jsonl",
      "files": [{"path": "03_text_cleaned.jsonl", "encoding": "utf-8", "content": "..."},
                {"path": "03_rejects.jsonl", "encoding": "utf-8", "content": "..."},
                {"path": "manifest.json", "encoding": "utf-8", "content": "..."}]
    },
    "result": {
      "process_status": "partial",
      "text": "03_text_cleaned.jsonl",
      "encoding": "UTF-8",
      "statistics": {"total_lines": 13, "valid_records": 13, "output": 6,
                     "rejected": 7, "bad_lines": 0}
    }
  }
}
```

`files` 为 UTF-8 文本或 base64 二进制（媒体按 base64），不再携带本容器临时绝对路径。形态一（单文件流）返回处理型 JSON：成功 `{"status":1,"process_status":"success","text":"03_text_cleaned.jsonl","encoding":"UTF-8"}`，失败 `{"status":0,"msg":"..."}`（`enabled=false` 时 `process_status="not_applicable"`）。

## 7. 测试

```bash
python -m pytest tests/ -q
```

`tests/test_text_processing.py`（纯函数与边界）、`tests/test_inference.py`（主流程/统计/媒体/CLI）、`tests/test_api.py`（三接口与安全边界）、`tests/test_transport.py`（媒体可搬运）。实际的通过数、CLI 统计与未验证项见 `HANDOFF.md`。

## 8. 资源建议与降级

- CPU 规则/文本处理，无需 GPU、无需训练；单进程内存与 `max_response_bytes`（64MiB）和最大素材包（64MiB）相关，建议容器 ≥2GiB；
- 本算子不使用分词后端，缺少 jieba 不影响运行；开启 `enabled` 后的三项清洗均为纯标准库规则，失败即配置/输入错误，不存在“分词降级”路径；
- 未配置任何模型权重（本算子无模型），不自动联网下载。

## 9. 目录

```text
inference.py                         # run、load_config、CLI
app.py                               # /predict、/downloadFile(404)、/removeFile
configs/config.yaml                  # 默认业务与部署配置
utils/text_processing.py             # 乱码/控制字符/标点统一（不分词）
utils/{io,schemas,transport,logger}.py
utils/lineage.py                     # （v4.2 停用，存档；仅保留 now_iso 时间工具）
dataset/sample/{make_sample.py,demo.jsonl,manifest.json,media/}   # 可复现 fixture
dataset/DATA_SCHEMA.md               # 本算子固定契约与样例
tests/                               # pytest
packaging/{Dockerfile,prepare_package.py}   # 打包（Dockerfile 在代码包上级）
README.md / OPERATOR_MANUAL.md / HANDOFF.md
```
