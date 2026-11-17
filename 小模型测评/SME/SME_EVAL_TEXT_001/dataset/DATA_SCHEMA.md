# 数据集字段

支持 JSON `{samples:[...]}`、JSONL/NDJSON（一行一条）、CSV（表头含 id/input）、TXT（每行一条）、ZIP（内含 samples.json / dataset.jsonl / dataset.csv 与媒体文件）。

公共字段：`id`、`input`、`output`、`title`、`scene`、`sourceStep`、`parameters`、`highValue`、`file`/`fileName`、`measurements`。

实测指标：`measurements.baseline` 与 `measurements.candidate` 必须覆盖本类型全部指标；数值范围见 README。

## 固定协议 CLUE 数据

离线实验使用独立 JSONL，不混入在线 `measurements`：

- `tnews`: `{"id": "...", "text": "...", "label": 0..14}`
- `afqmc`: `{"id": "...", "text1": "...", "text2": "...", "label": 0|1}`
- `cluener`: `{"id": "...", "text": "...", "entities": [{"start": 0, "end": 2, "label": "name"}]}`

`start`/`end` 是 Python 字符偏移，`end` 为开区间。任务 train 与固定评测集 E
必须按 `id` 不重叠；TNEWS/AFQMC 的 `test` 标签为 `-1`，转换器会跳过，不能
拿来当验证集。
