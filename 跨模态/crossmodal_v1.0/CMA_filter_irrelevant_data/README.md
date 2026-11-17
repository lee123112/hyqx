# 算子 2：数据初筛

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

本算子对图文做关键词初筛，对视频音频做时长差初筛，只排除明显异常，不输出语义模型分数。音视频时长差默认 7 秒（2026-10-22 试验值，原规格 2 秒/可选 1–5，代码允许 1–10）。

```powershell
python -m pip install -r requirements.txt
python dataset/sample/make_sample.py
python inference.py --input dataset/sample/samples.jsonl --unique-id demo_file1 --output-root output
python -m uvicorn app:app --host 0.0.0.0 --port 8102
```

HTTP 严格使用三接口：

| 接口 | 用途 |
|---|---|
| `POST /uploadFile?uniqueId=` | 上传 JSONL 或 zip 素材包 |
| `POST /predict` | [v4.2] 双协议：JSON（`uniqueId`、严格布尔 `isLastOperator`，阶段内后续可传 `inputFile`）；或 multipart 单文件（`?detection=M03`，`file` 必传） |
| `POST /removeFile?uniqueId=` | 幂等清理任务目录 |

[v4.2] `isLastOperator` 仍必传，用于标记阶段末调用；中间与阶段末响应同构，形态二 `data` 均为 `{operator, status, outputFile, result}`，不再返回 `lineageFile`，也不再写 `lineage.json` / `stage_context.json`。不注册 `/downloadFile`。输出主清单为 `02_screened.jsonl`，异常清单为 `02_rejects.jsonl`，并携带 `manifest.json` 和被引用媒体。

形态二响应示例（`outputFile` 为 `crossmodal-files-v1` 内容对象，`files` 按实际产物列出）：

```json
{"code":0,"msg":"success","data":{
  "operator":"filter_irrelevant_data",
  "status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"02_screened.jsonl",
    "files":[{"path":"02_screened.jsonl","encoding":"utf-8","content":"..."},
             {"path":"02_rejects.jsonl","encoding":"utf-8","content":"..."},
             {"path":"manifest.json","encoding":"utf-8","content":"..."}]},
  "result":{"process_status":"success",
            "statistics":{"total":12,"kept":9,"rejected":3,"bad_lines":0}}}}
```

失败示例：`{"code":1,"msg":"<原因>","data":{}}`；形态一为判定型 JSON，保留返回
`{"status":1,"process_status":"success","filter_status":"pass","reason_code":"keyword_or_duration_check_passed","sample_id":"s-001","msg":"success"}`，
被过滤返回 `{"status":0,"process_status":"failed","filter_status":"quarantine","reason_code":"no_common_words","sample_id":"s-002","msg":"初筛过滤：no_common_words"}`。

音频时长当前使用标准库 WAV 探针；无法探测的格式进入 `duration_probe_failed`，不会伪造时长。完整 Schema、限制和交接证据见 `dataset/DATA_SCHEMA.md`、`OPERATOR_MANUAL.md` 与 `HANDOFF.md`。
