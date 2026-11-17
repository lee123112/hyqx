# 数据集字段

支持 JSON `{samples:[...]}`、JSONL/NDJSON（一行一条）、CSV（表头含 id/input）、TXT（每行一条）、ZIP（内含 samples.json / dataset.jsonl / dataset.csv 与媒体文件）。

公共字段：`id`、`input`、`output`、`title`、`scene`、`sourceStep`、`parameters`、`highValue`、`file`/`fileName`、`measurements`。

视频可选字段：`caption`、`description`、`source`/`sourceUrl`、`highValue`。没有 caption 时
`alignment` 是中性证据分 60，不是真实语义对齐；`source` 缺失时引擎回退到 `input/uri` 判断本地/远程。

实测指标：`measurements.baseline` 与 `measurements.candidate` 必须覆盖本类型全部指标；数值范围见 README。

离线 VQA/MOS 标签文件不放进算子输入：使用 `tools/run_video_vqa.py --labels`，格式为 CSV
`video,score` 或 JSON；按文件名匹配。没有真实 MOS 时不得写 SRCC/PLCC。
