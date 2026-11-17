# 数据集字段

支持 JSON `{samples:[...]}`、JSONL/NDJSON（一行一条）、CSV（表头含 id/input）、TXT（每行一条）、ZIP（内含 samples.json / dataset.jsonl / dataset.csv 与媒体文件）。

公共字段：`id`、`input`、`output`、`title`、`scene`、`sourceStep`、`parameters`、`highValue`、`file`/`fileName`、`measurements`。

实测指标：`measurements.baseline` 与 `measurements.candidate` 必须覆盖本类型全部指标；数值范围见 README。

## benchmark 专属契约

JSON/JSONL 每条至少提供唯一非空 `id`、非空 `input`、`measurements.baseline.score` 和 `measurements.candidate.score`。两侧得分都是同一题目的 0–100 有限数值，拒绝布尔/字符串/越界。建议提供 `output`（标准答案）及 `scene`（能力或业务场景分组）。缺分不按 0 补齐，也不回退 Demo。

CSV 可用 `id,input,output,scene,baseline_score,candidate_score` 列；数值列由 CSV 解析器显式转为数值。TXT 没有逐题得分，只能用于 Demo，不能完成 benchmark 实测。

重复内容按 NFKC 与空白规范化后的 `input` + `output` 检测，ID 仍须唯一；重复题仅提示，不自动去重或修改权重。每题等权，批次判定比较分差均值与阈值，不要求每题均达标。

`sample/import_example.jsonl` 是合成分数夹具。真实业务验收还需上游模型版本、评分标准、固定评测集、无泄漏证据以及适用的多种子实验记录；不能把导入分差当作训练数据质量的因果证明。
