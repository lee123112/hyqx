# 数据集字段

支持 JSON `{samples:[...]}`、JSONL/NDJSON（一行一条）、CSV（表头含 id/input）、TXT（每行一条）、ZIP（内含 samples.json / dataset.jsonl / dataset.csv 与媒体文件）。

公共字段：`id`、`input`、`output`、`title`、`scene`、`sourceStep`、`parameters`、`highValue`、`file`/`fileName`、`measurements`。

实测指标：`measurements.baseline` 与 `measurements.candidate` 必须覆盖本类型全部指标；数值范围见 README。

音频字段支持顶层或 `parameters` 中的 `transcript/reference/output/text`、`speaker/speakerId/speaker_id/spk`、`domain/domainLabel/domain_label`、`language`；媒体支持 `input` 或 `audio`。JSON/JSONL 和 CSV 的常用音频字段会保留到归一化后。

CER 为非负有限数值，可大于 100。可选的 `cerEdits`（非负整数）、`cerReferenceChars`（正整数）与 `cer` 必须一致；全部样本、两侧都带计数时以总编辑数/总参考字数汇总，否则沿用导入逐句均值。说话人/领域范围仍为 0–100，LOO 至少 2 类且每类 ≥2 条。

训练协议使用三份独立 JSON/JSONL 清单：D_ours、D_ref、固定 E。每行：

```json
{"id":"utt001","audio":"clips/utt001.wav","text":"今天是十一月四日","speaker":"corpusA-speaker01"}
```

`audio` 相对清单目录解析，也接受本地绝对路径；此入口不下载远程音频。`id` 在每份清单内唯一，`speaker` 必填且跨清单命名空间一致，E 的说话人不得与任一训练集重叠。读取时统一重采样为 16kHz 单声道；不默默截断长音频/转写。

D_ours/D_ref 可以在受控的清洗前后对照中包含同一批训练音频，但不得混入 E；本轮 matched/noisy 测试就是这种公开集敏感性验证，不能把它当独立业务 D_ours。
