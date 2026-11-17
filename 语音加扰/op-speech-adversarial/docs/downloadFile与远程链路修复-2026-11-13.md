# downloadFile 接口与远程文件链路修复

更新日期：2026-11-13。目标地址：`http://192.168.1.105:17023`。本次仅修改本地，服务器由部署人员手动上传更新。

## 1 工程侧的实际调用契约

工程侧使用 **GET + 完整 path 查询参数**，不单独传 uniqueId 或 operatorId：

```text
GET /downloadFile?path=/data/本次任务/tmp/002/002_std.jsonl
```

path 使用上传响应、预测的 outputs、文件清单或产物记录中的真实服务端路径。请求时应由 HTTP 客户端编码查询参数，不能把中文、空格、加号或百分号直接拼接为未编码 URL。

成功响应为文件字节流，不是 `{code,msg,data}` JSON 信封；文件名通过 Content-Disposition 返回。支持文本、JSONL、ZIP、音频、HTML 和 PDF 单文件，GET 支持 Range 分段读取。传解压目录会被拒绝，应下载 upload 响应的 archive 或预测返回的具体产物文件。

兼容入口：filePath、inputFile 可作为 path 的别名；也支持 POST JSON。uniqueId 为可选限制参数，提供时只允许下载该任务内文件。现有工程侧只传 path 即可。

```bash
curl --fail --show-error --get \
  'http://192.168.1.105:17023/downloadFile' \
  --data-urlencode 'path=/data/本次任务/tmp/002/002_std.jsonl' \
  --output 002_std.jsonl
```

以上路径为结构示意，执行前替换为本次真实返回路径。PowerShell 使用 curl.exe 单行执行：

```powershell
curl.exe --fail --show-error --get "http://192.168.1.105:17023/downloadFile" --data-urlencode "path=/data/本次任务/tmp/002/002_std.jsonl" --output "002_std.jsonl"
```

失败时返回 `code=1` 的 JSON：缺参数/类型非法/请求目录为 400，越出数据根目录或任务限制为 403，文件不存在为 404 且 msg 以 `missing_file:` 开头。下载端必须先处理 HTTP 错误，不能把错误 JSON 保存为 WAV 或 PDF。

## 2 手动更新服务器

运行接口修复只需要覆盖新版 `inference.py`，不增加 Python 依赖。Compose 的 `LOG_LEVEL: INFO` 修复保持不变。map.json、文档和 http_smoke.py 同步更新用于接口说明及复测，若需要使用新版复测客户端，请一并上传 `tools/http_smoke.py`。

在服务器部署目录备份入口，上传本地新版 inference.py 覆盖后执行：

```bash
docker compose up -d --build --force-recreate avp
sleep 5
curl --fail --show-error http://127.0.0.1:17023/health
```

预期 health 含以下增量字段；只有容器处于 Started 状态不足以确认入口已更新：

```json
{
  "apiRevision": "2026-11-13-download-v1",
  "capabilities": {"downloadFile": true}
}
```

若字段不存在，说明当前请求仍到达旧入口，应检查镜像重建、容器更新和实际服务地址。重启旧容器不能把宿主机新的 inference.py 自动装入既有镜像。

已有服务要求容器 UID 1000 对 data 可写；保留服务器已设置的目录权限，不上传或覆盖运行 data。

## 3 重新上传并按本轮路径串联

1. 生成新的业务任务 ID 和 uniqueId，重新上传完整音频与参考文本 ZIP。保存上传响应的 inputFile、archive、workDir。
2. 01 的 inputFile 使用这次上传返回的 data.inputFile。仅在 code=0 后读取产物和继续调用。
3. 02 的 inputFile 使用 01 的 data.outputs.corpus；后续按下表选择正确产物，原样传回服务端路径。
4. 后端需要读取语料表、音频或报告时，GET downloadFile 并传完整 path，将文件流保存到自己的存储。下载后的本地路径不替换下一步预测的服务端 inputFile。
5. 每次保存中间血缘记录并累计为列表，下一次作为 lineageFile 传回。阶段末是列表，不把整个列表或 outputFile 对象当作文件路径。
6. 产物下载及归档完成、且没有下游继续引用后，再调用 removeFile。若旧任务已被删除、容器数据未持久化或原文件不存在，必须重新上传与重新执行，不能仅修改路径字符串冒充文件已存在。

| 下一步 | inputFile 应取 |
|---|---|
| 01 入库 | 本次 upload 的 data.inputFile |
| 02 标准化 | 01 的 data.outputs.corpus |
| 03 噪声或 04 语速 | 02 的 data.outputs.corpus；03 和 04 是两个分支 |
| 05 质检 | 选定分支的 data.outputs.batch |
| 06 pack | 05 finalize 的 data.outputs.batch_final，确认质检结论 |
| 06 publish | 06 pack 的 data.outputs.version 或该任务有效注册表；不是 version_zip |
| 07 评测接收 | 外部评测 JSONL 重新上传后的路径，并提供真实版本库 |
| 08 归因 | 07 的 data.outputs.results，包含有效基线和目标 |
| 09 建议 | 08 的 data.outputs.attribution |
| 10 资产 | 数据集/入库记录或注册表，按业务数据另行构造 |
| 11 报告 | 真实评测、归因、建议文件汇总包上传后的目录 |

尤其注意：05 主输出可能是质检报告，06 主输出可能是版本 ZIP。工程侧不能统一取每一步 lineageFile.outputFile 而忽略业务需要的文件种类。

## 4 无共享目录的工程侧接入示例

以下为 HTTP 编排要点，不代表已直接修改业务后端源工程：

```python
# upload_response 为本轮上传的已校验成功响应。
remote_input = upload_response["data"]["inputFile"]
response01 = predict(operator="avp_ingest_corpus", inputFile=remote_input)
assert response01["code"] == 0
remote_corpus = response01["data"]["outputs"]["corpus"]

# 下载给业务后端落库/展示，不改写远程预测的 inputFile。
downloaded_bytes = http_get("/downloadFile", params={"path": remote_corpus})
response02 = predict(operator="avp_normalize_audio", inputFile=remote_corpus)
```

以上是伪代码，实际请求仍需 uniqueId、params 和累计 lineageFile，见复测客户端。已使用 downloadFile 的工程侧可通过 HTTP 取回产物；若仍沿用本地路径读取实现，必须改用 HTTP 下载或保留共享目录挂载。

本部署目录不包含业务后端源工程，因此本次完成的是算法服务接口、工程侧调用约定和可执行客户端验证；业务后端的字段选择和状态保存按上述规则接入。

## 5 部署后复测

准备一份含音频和同名 UTF-8 文本的小型 corpus.zip，在可以访问服务器的调用机执行：

```bash
python3 tools/http_smoke.py --base-url http://192.168.1.105:17023 --input corpus.zip
```

客户端先核对 health 下载能力，随后实际验证：上传 → 下载上传 ZIP 比较字节 → 01 → 按 GET path 下载语料和音频 → 使用返回路径执行 02 → 下载标准化语料和 WAV → 删除本次任务 → 确认旧文件返回 missing_file/404。

脚本默认只清理自己的独立任务；若增加 `--keep`，保留该任务供排查，并输出 uniqueId。测试样本及运行数据不打入部署包。
