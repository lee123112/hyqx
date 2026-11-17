# Java 接入契约（2026-11-09，PRLt_v1.0）

本文件描述同事优化版算法包的工程接口。当前编号以 `map.json` 为准，共23个算子，通过根服务7个业务接口调用。API18为质量报告，API19为人工抽检；处理器历史文件前缀不代表当前operatorId。

## 1. 接口与原有字段

|方法|路径|用途|
|---|---|---|
|GET|/health|健康检查|
|GET|/operators|查询当前23算子|
|POST|/uploadFile|上传单个输入文件|
|POST|/predict|执行单算子|
|POST|/predictChain|执行算子链|
|GET|/downloadFile|下载任务/算子的当前阶段ZIP|
|POST|/removeFile|清理任务文件|

成功外层仍为 `{code:0,msg:"success",data:{...}}`。`/predict` 保留 `outputFile/rejectFile/stageZip/storageUri`。`isLastOperator=false` 时 outputFile 为服务器路径，true 时为完整输出文本；新增采样限制不会截断原文件。`storageUri` 可能为服务器本地 file URI，跨机器下载使用 `/downloadFile`。

### /predictChain 异常回包（2026-11-11）

内部任一阶段 `status="failed"` 时，顶层返回 `code=1`，`msg` 包含首个失败阶段与原因；HTTP沿用200。`data` 保留各阶段的统计、样本、审计及已有产物，并新增 `failedStages` 数组，每项为 `{"operatorId":"02","reason":"配置错误：ValidationError"}`。各失败阶段的 `message` 保留处理器原始原因；调用异常也会关联到对应阶段。上游无输出导致阶段未执行时，原因明确标注“上游无可处理记录，本阶段未执行”。

全部阶段无 `failed` 时，原成功回包保持不变，不新增 `failedStages`。`partial`、`waiting_calibration` 等业务状态不单独触发顶层失败。阶段已有的空输出判定、过滤和交付策略保持原样；本次仅修复接口错误汇总，不改算法或阈值。入口参数/文件校验失败仍返回原有 `code=1,data=null`。

工程侧应以顶层 `code` 判断本次调用是否执行成功，失败时读取 `msg` 和 `data.failedStages`，并按各阶段原有下载字段获取已经产生的诊断产物；未产生阶段ZIP时 `/downloadFile` 返回404。

## 2. 四个回包字段和一个参数

```json
{
  "uniqueId": "task01_file01",
  "operatorId": "01",
  "isLastOperator": false,
  "params": {"sampleLimit": 20}
}
```

`sampleLimit` 默认20，正整数取前N条，<=0回传全量样本；它只控制响应。兼容 `config.sampleLimit`，params优先。它会在调用处理器前移除，不属于算法配置。布尔值、小数、null及非整数字符串会明确失败。不要放在请求顶层；顶层不是该参数的契约位置。

|字段|类型|含义|
|---|---|---|
|statistics|object|统一input_records/output_records/rejected_records整数；含audit_records、raw_statistics，必要时带计数是否已知的标记|
|outputSample|array|输出业务记录，保留id、source/target、document_id、metadata及真实评分等原有字段|
|rejectedSample|array|异常/审计样本，兼容record嵌套与reason/reasons，带本阶段decision；其中不一定都是被移除记录|
|rejectReasons|object|全量异常记录按拒因/告警码计数；不受sampleLimit影响|

示例：输入26条、输出25条、物理拒收1条时，statistics三个计数为26/25/1。原始处理器的total/passed/rejected等保留在raw_statistics，audit_records记录异常事件数量。

decision=`retained` 表示该阶段留在输出、等待下游；`rejected` 表示该阶段物理移除；`unknown` 表示证据不足。Java应保存判定和阶段，不要把rejectedSample长度、rejectReasons总和、needs_review或waiting_manual直接当拦截条数。单条样本可能带多个原因，原因数不等于异常数。

API08计划→段对、API09段对→句对、API10句对→子句存在扩展及单侧未对齐，三项计数不是同单位守恒公式；不要使用input-output计算拒收。API18输出是JSON报告，output_records可为1、outputSample可为空；它不是交付句对数量。

## 3. 血缘与实例身份

由Java自行记录业务血缘；本服务不新增lineage文件。建议保存uniqueId、当前operatorId、统计、样本业务id、父引用、文档id、本阶段decision、原因、原始状态和阶段下载参数。保留同ID重复实例的行号/实例标识，不以业务id单独做全局去重。

嵌套record的业务id优先，外层不同的审计id保留为audit_id。业务记录以前阶段的decision不能代替本阶段的判断。文档/段落身份通过document_id、parent_input_id、paragraph_id、parent_pair_id及metadata中已有引用关联；原文档ID与清单ID不同的情况会保留declared_document_id。

`/predictChain` 的各stages也返回上述四字段；顶层四字段对应末阶段。另有auditFile（JSONL文本）、auditFileUri（本地URI）、auditSummary，用于查看最终去向和阶段。未知判断不能作为拒收；输入行数与拆分后业务单元数应分别展示。

## 4. 章节输入

双TXT采用documents协议：

```text
input.jsonl
plans/<document_id>.json
documents/<document_id>/source_zh.txt
documents/<document_id>/target_en.txt
```

清单每行声明document_id；计划引用两侧UTF-8文档。`params.inputMode="documents"`、`params.planDirectory` 指向服务器上的plans目录。计划中的相对路径需与服务器实际目录树一致。文件名不应包含路径穿越，清单引用的计划缺失应修正后再执行。`maxRecords`如使用，表示本次选择的输入文档行数；不等同于样本限制。

`/uploadFile`一次只传一个文件，不自动解压ZIP或生成配对计划。Java/接入层须将清单、计划和两侧文档准备为可读目录。不能仅送中文TXT、将两TXT首尾拼接、或把长章节CSV最终被长度门过滤误判为只读一行。

演示请求（目录以服务器实际上传位置为准）：

```json
{
  "uniqueId": "task01_chapters",
  "inputFile": "/server/pool/task01_chapters/input/input.jsonl",
  "params": {
    "inputMode": "documents",
    "planDirectory": "/server/pool/task01_chapters/input/plans",
    "executionMode": "stage_batched",
    "sampleLimit": 20
  }
}
```

默认章节链：08→09→12→13→14→15→11→16→17，API10默认未启用。新版低置信/非等价保留策略仍由下游最终裁决，不能将保留样本当交付合格样本。

## 5. 下载

```text
GET /downloadFile?uniqueId=task01_chapters&operatorId=08
```

返回application/zip，Content-Disposition携带 `stage_08_task01_chapters.zip`。文件与该阶段stageZip指向同一产物。只接受uniqueId、operatorId及可选operatorIdScheme；不接受path。默认current；只有显式legacy才转换旧编号。非法参数400、越界403、当前阶段产物不存在404、缺少必传字段422。

运维更新源码后需构建包含新源码的镜像并重建相应服务容器；源码已COPY进镜像时只重启旧容器不会加载新文件。启动和服务器部署由运维负责。
