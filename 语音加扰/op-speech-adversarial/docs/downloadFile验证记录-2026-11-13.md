# downloadFile 与重新上传链路验证记录

日期：2026-11-13。范围：本地 AVP 源包与 op-speech-adversarial 精简部署包。未登录或修改远端服务器。

## 确认的工程契约

工程侧实际使用 `GET /downloadFile?path=完整服务端路径`，不单独传 uniqueId/operatorId。本次实现与真实 HTTP 复测均采用该格式；兼容 filePath/inputFile 及 POST JSON。

## 回归测试

旧入口先执行新增下载测试，19 项失败，确认接口缺失。实现后新增两项工程侧完整 path、无任务或算子参数的专项测试。

```text
python -B -m pytest AVP_INGEST_AUD_001/tests/test_download.py \
  AVP_INGEST_AUD_001/tests/test_startup.py \
  AVP_INGEST_AUD_001/tests/test_api.py -q -p no:cacheprovider

40 passed in 10.24s
```

覆盖下载文件内容/中文名、GET/POST 参数兼容、Range 206、越界 403、缺参数/目录 400、缺文件 404、指定任务限制，以及源包已有日志级别与算子 API 回归。

## 真实 HTTP

从精简部署目录启动临时服务，使用 `LOG_LEVEL=info` 保留原启动修复的回归覆盖，运行数据写系统临时目录。
输入为源包 3 组音频与同名文本，客户端运行结果：

```text
health apiRevision=2026-11-13-download-v1、capabilities.downloadFile=true  PASS
OpenAPI 中存在 GET/POST downloadFile 和 path 参数                  PASS
上传 ZIP                                                           PASS
GET path 下载上传 ZIP，逐字节相等                                  PASS
01 入库，使用上传响应的 inputFile                                  PASS
GET path 下载 01 语料（3 条）及音频（64044 字节）                     PASS
02 标准化，直接使用 01 返回的 corpus 路径                           PASS
GET path 下载 02 语料（3 条）及 WAV（64044 字节）                     PASS
removeFile 清理本次任务，旧产物再次下载返回 missing_file/404         PASS
```

待上传目录没有新增 data 或缓存；临时服务已停止。原 03–11 DSP/业务算法代码未修改，本次没有重跑远端业务任务。

## 服务器后续验收

部署人员覆盖入口并重建镜像/容器后，先看 health 的 apiRevision 与下载能力标识，再重新上传本轮数据进行完整复测。旧任务文件不存在时仍应返回 missing_file，代码更新不能恢复已丢失文件。
业务后端源工程不在本目录，本次未直接修改其代码；应按 [远程链路说明](downloadFile与远程链路修复-2026-11-13.md) 的 outputs 字段选择与 HTTP 下载规则接入。
