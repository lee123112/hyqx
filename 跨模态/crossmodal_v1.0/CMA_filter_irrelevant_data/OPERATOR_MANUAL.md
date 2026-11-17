# 数据初筛算子说明书

本 demo 按共享契约提供 `POST /uploadFile?uniqueId=`、`POST /predict` 和 `POST /removeFile?uniqueId=` 三个业务接口。[v4.2] `/predict` 形态二（JSON）统一返回 `data.outputFile`（`crossmodal-files-v1` 内容对象）+ `result`，中间与阶段末响应同构、不再返回 `lineageFile`；形态一（multipart，`?detection=M03`）返回判定型 `status` JSON（1=保留/0=过滤）。不提供下载接口。

算子使用图文关键词重叠和音视频时长差规则，只做明显异常初筛，不输出模型概率或语义判定。音视频时长差默认 7 秒（2026-10-22 试验值，原规格 2 秒/可选 1–5，代码允许 1–10）。输入与输出均按 `DATA_SCHEMA.md` 的 JSONL 结构保存，拒绝记录进入 `02_rejects.jsonl`。
