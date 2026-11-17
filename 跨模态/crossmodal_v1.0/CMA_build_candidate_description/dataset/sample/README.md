# 样例说明

`make_sample.py` 只用 Python 标准库（`zlib`/`struct`/`wave`）生成可复现样例，不下载任何外部数据：

- `demo.jsonl`：12 条记录 + 1 行损坏 JSON（第 13 行）。
- `img_000*.png`：合法最小 PNG（本算子不解析像素，仅用于路径/文件名校验与随包携带）。
- `aud_0004.wav` / `aud_0005.wav`：合法 16kHz 单声道 WAV，承载字幕与语音转写场景。

| sample_id | 场景 | 期望结果 |
|---|---|---|
| sample-0001 | 图注 + Alt + 标题齐备 | `described`，来源 `caption` |
| sample-0002 | 仅 Alt | `described`，来源 `alt` |
| sample-0003 | 仅标题 | `described`，来源 `title` |
| sample-0004 | 音频分段字幕 | `described`，来源 `subtitle`，文本为拼接结果 |
| sample-0005 | `audio.transcript` | `described`，来源 `transcript` |
| sample-0006 | 图注等于文件名 | `pending_manual`，跳过原因 `equals_filename` |
| sample-0007 | 图注等于分类标签 | `pending_manual`，跳过原因 `equals_tag` |
| sample-0008 | 裸图，仅通用 `text` | `pending_manual`，`is_bare_image=true` |
| sample-0009 | 图注过短(1 字) + 有效 Alt | `described`，来源 `alt` |
| sample-0010 | 标题为空白 + 人工补写 | `described`，来源 `manual` |
| sample-0011 | 图注 800 字 | `described`，截断到 500，`truncated=true` |
| sample-0012 | 缺 `sample_id` | 进入 `09_rejects.jsonl` |
| 第 13 行 | 损坏 JSON | 进入 `09_rejects.jsonl`，`reason=json_parse_error` |

期望统计：`described=8`、`pending_manual=3`、`bad_lines=1`、`missing_sample_id=1`、
`truncated=1`、`records=12`、`total_lines=13`、`rejected=2`。

重新生成：`python dataset/sample/make_sample.py`
