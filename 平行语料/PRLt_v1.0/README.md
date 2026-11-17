# PRLt_v1.0 平行语料工程联调包

本目录保留23个算子的运行源码、原配置/阈值、原模型、词库、适配器与统一HTTP服务。当前使用原本的本地模型和既有增强API，不引入IDG网关、X-Token或其他入站认证。

工程调用：`POST /uploadFile` → `POST /predictChain` → `GET /downloadFile`。完整字段和章节输入协议见 [PREDICT_JAVA_CONTRACT.md](PREDICT_JAVA_CONTRACT.md)。

## 本机启动

本机已将Python依赖外置，运行包中不再附带机器专用虚拟环境。保留下面两个外部目录，它们是当前本地运行环境：

- `G:\HYQX\算子\平行语料\运行环境\PRLt_v1.0`：有效的Python 3.12.14虚拟环境。
- `G:\HYQX\算子\平行语料\运行环境\PRLt_v1.0_dependencies`：沿用原有依赖，未升级或替换模型库。

在PowerShell中启动正式入口：

```powershell
Set-Location 'G:\HYQX\算子\平行语料\PRLt_v1.0'
$env:PORT = '17006'
$env:MODEL_ROOT = 'G:\HYQX\算子\平行语料\PRLt_v1.0\models'
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'G:\HYQX\算子\平行语料\运行环境\PRLt_v1.0\Scripts\python.exe' -X utf8 -B main.py
```

地址为 `http://127.0.0.1:17006`，`GET /health` 应返回 `code=0`、`operatorCount=23`。本包当前无需X-Token。

换机器部署时使用Python 3.12，按 `requirements.txt` 安装依赖，并保留原 `models/` 目录；机器专用的外置环境不属于工程包。Docker/Compose沿用原配置：宿主17006映射容器6291，模型通过 `./models` 只读挂载。

## 最小联调：1条输入走默认链

本包只保留一个必要样例：`examples/smoke_one_pair.csv`（1条中英句对）。在另一PowerShell窗口依次执行正式HTTP接口：

```powershell
Set-Location 'G:\HYQX\算子\平行语料\PRLt_v1.0'
$base = 'http://127.0.0.1:17006'
$uniqueId = 'demo_onepair'
$upload = (& curl.exe -sS -F 'file=@examples/smoke_one_pair.csv' "$base/uploadFile?uniqueId=$uniqueId") | ConvertFrom-Json
$body = @{
    uniqueId = $uniqueId
    inputFile = $upload.data.inputFile
    operatorIdScheme = 'current'
    params = @{ inputMode = 'pairs'; executionMode = 'stage_batched'; sampleLimit = 20 }
} | ConvertTo-Json -Depth 6
$requestPath = Join-Path $env:TEMP 'prlt_onepair_request.json'
[System.IO.File]::WriteAllText($requestPath, $body, [System.Text.UTF8Encoding]::new($false))
& curl.exe -sS --max-time 3600 -H 'Content-Type: application/json' --data-binary "@$requestPath" "$base/predictChain"
& curl.exe -sS "$base/downloadFile?uniqueId=$uniqueId&operatorId=17" -o (Join-Path $env:TEMP 'prlt_stage17.zip')
```

使用上传回包的 `data.inputFile`，工程跨机器调用时不要自行拼接本机路径。这里没有调用本地辅助处理脚本。

默认句对链：01→02→03→04→05→06→07→09→12→13→14→15→11→16→17。

章节使用 `documents` 协议，配套清单/计划/双语TXT，默认链为08→09→12→13→14→15→11→16→17。细节以接口契约为准。API10为可选子句分支；18/19为系统功能；20–23为可选增强，当前保留原实现。

## 结果判断

- 完整成功回包 `code=0,msg=success`；内部出现 `failed` 时顶层 `code=1`，`msg` 和 `data.failedStages` 给出失败阶段及原因。
- `partial`、`waiting_calibration`、审计记录不等于执行失败；物理拒收以 `statistics.rejected_records` 为准。
- `sampleLimit=20` 仅控制回包采样，原文件不截断；不指定 `maxRecords` 时处理全部已上传输入。
- 最后输出、拒收清单、阶段ZIP与审计按原契约保留。运行时生成的数据在 `work/pool` 或环境变量指定的任务目录。

## 本次整理范围

保留全部算子源码、`common/`、`configs/`、词库、`models/`、`weights/`、注册表、评分配置、依赖和原部署入口。既有 `.env`/`.env.example` 保持原样。

机器虚拟环境已外置；测试、缓存、旧运行产物、开发辅助工具、批量演示样例、旧交接材料移到包外归档。算子局部README和DATA_SCHEMA保留作字段参考；统一调用以本文及接口契约为准。

当前目录结构见 [DIRECTORY_STRUCTURE.md](DIRECTORY_STRUCTURE.md)，模型位置见 [MODEL_SERVICES.md](MODEL_SERVICES.md)。本次最小真实运行结果见 `LOCAL_SMOKE_RESULT.txt`；详细请求、原始回包和校验记录保存在包外联调数据目录。逐文件校验清单为 `PACKAGE_MANIFEST.json`、`PACKAGE_SHA256.txt`。