# 准备候选描述 算子说明书（#09）

本算子按跨模态共享契约提供 `POST /uploadFile?uniqueId=`、`POST /predict`、
`POST /removeFile?uniqueId=` 三个业务接口；[v4.2] 中间与阶段末 `predict` 响应同构，
均返回 `crossmodal-files-v1` 文件内容对象（`outputFile`）与业务摘要（`result`），不提供下载接口。

## 作用

在数据加工环节为每个跨模态样本准备**候选描述**：按表1固定优先级
（图注→Alt→标题→字幕→语音转写→人工补写）取第一条 5–500 字有效文本；文件名和分类标签无效；
超长截断并记录；没有有效描述时只生成人工补写任务，样本留在产线等待回写，不进入准入也不进隔离。

[v1.0.1] 描述质量：词数不足 `description.min_words`（默认 6）时，先在同源候选图注
（`metadata.alt_candidates`）里换一条更长的有效候选；换不到就保留原文并标
`description_quality=short`（不阻塞下游）。描述语言按字符脚本判定写入
`description_lang`；当 `text.lang` 与内容明显矛盾（如英文图注被上游按部署声明标成 `zh`）
时纠正为实际语言，原值保留在 `text.lang_declared`。

## 不做

不配对、不打 CLIP 分、不做 OCR、不调用 BLIP/LLaVA 等图像描述模型；不修改上游其他算子命名空间。

## 产物

- `09_descriptions.jsonl`：有效候选描述记录，交 #10 准入核对。
- `09_rewrite_tasks.jsonl`：人工补写任务队列（`action=waiting_manual`、`destination=pipeline_wait`）。
- `09_rejects.jsonl`：损坏 JSON、非对象、缺 `sample_id` 的原始隔离台账（带物理行号）。

## 输入输出

输入输出字段、统计守恒与 `crossmodal-files-v1` 文件内容对象见 `dataset/DATA_SCHEMA.md`；启动命令、
HTTP 示例、配置范围和实测结果见 `README.md`；未验证项与风险见 `HANDOFF.md`。
