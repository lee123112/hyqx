# 算子 3 数据说明（text_normalization）

> 上游固定字段见上级 `DATA_SCHEMA.md` §7；本文件说明本算子的实际输入/输出形状与本地 fixture。
> 本算子只改 `text` 与 `processing["03"]`，并在搬运媒体时重写媒体相对路径；其余字段原样保留。

## 1. 输入记录（每行一条完整样本）

```json
{
  "sample_id": "demo_03_img_001",
  "text": "<b>红色</b>方块&nbsp; 在桌面上 ",
  "image": "media/img_001.png",
  "video": null,
  "audio": null,
  "alignment": {},
  "processing": {"02": {"status": "success", "reason": null, "metrics": {"passed": 1}}},
  "metadata": {"source": "local_fixture", "license": "CC0", "domain": "图文"}
}
```

- `text` 允许字符串或对象；对象优先取 `content`，回退 `original`；`text` 为 `null` 或无文本键视为无文本；
- `text` 为其他类型（如数字）→ 该行进 `03_rejects.jsonl`，原因 `invalid_text_type`；
- `image`/`video`/`audio` 为相对输入根的 POSIX 路径（不允许绝对路径、`..`、反斜杠、URL）；
- `metadata`、`alignment`、`qc`、`processing` 中非 `"03"` 的键以及未知字段一律保留。

## 2. 输出记录（`03_text_cleaned.jsonl`）

```json
{
  "sample_id": "demo_03_img_001",
  "text": {
    "content": "红色方块 在桌面上",
    "original": "<b>红色</b>方块&nbsp; 在桌面上 ",
    "tokens": ["红色", "方块", "在", "桌面上"],
    "token_offsets": [[0, 2], [2, 4], [5, 6], [6, 9]],
    "lang": "zh",
    "tokenizer": "jieba"
  },
  "image": "media/img_001.png",
  "processing": {
    "02": {"status": "success", "reason": null, "metrics": {"passed": 1}},
    "03": {"status": 1, "process_status": "success", "reason": null, "metrics": {
            "html_tags_removed": 2, "script_style_removed": 0, "control_chars_removed": 0,
            "invisible_chars_removed": 0, "whitespace_normalized": 1,
            "normalization_changes": 0, "replacement_chars": 0, "lowercased": 0,
            "punctuation_normalized": 0, "ad_patterns_removed": {}, "chars_in": 23,
            "chars_out": 9, "whitespace_tokens_dropped": 1, "token_offset_mismatch": 0,
            "tokens": 4, "tokenizer": "jieba", "requested_tokenizer": "jieba",
            "degraded": false, "degrade_reason": null}}
  }
}
```

> 以上为 `python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_03
> --output-root output/smoke` 首行的真实输出（缩进仅便于阅读）。

- `text.token_offsets[i]` 满足 `content[start:end] == tokens[i]`（构建时强校验，不一致即失败，不产出坏数据）；
- 已存在 `text.original` 时沿用最初的 `original`，只更新 `content`/`tokens` 等当前结果；
- 无文本但存在 video+audio 的记录保留原样，`processing["03"] = {"status": 1, "process_status": "not_applicable", "reason": "..."}`，计入 `not_applicable`（output 子集）；
- 若开启降级，额外带 `text.tokenizer_degrade_reason`，`metrics.degraded=true`。

## 3. 异常清单（`03_rejects.jsonl`）

```json
{"sample_id": "demo_03_empty_001", "operator": "text_normalization", "line": 7,
 "reason": "empty_text_after_cleaning",
 "record": {"sample_id": "demo_03_empty_001", "text": "<p>   </p>", "image": "media/img_001.png"},
 "evidence": {"chars_in": 10}}
```

常见原因：`empty_text_after_cleaning`、`invalid_type:text`、`missing_field:sample_id`、`invalid_json`、`media_missing:<字段>`、`media_unreadable:<字段>`、`media_path_escape:<字段>`、`duplicate_sample_id`。媒体不可用时同条记录的引用被置为 `null`，不留指向不存在文件的活跃引用。

## 4. 统计守恒

`statistics` 至少含：`total_lines`、`valid_records`、`bad_lines`、`output`、`rejected`、`not_applicable`、`media_copied`、`text_cleaned`、`conservation_ok`，以及 `reason:<原因>` 计数与 `tokenizer`/`degraded`。

- `total_lines == valid_records + bad_lines`（坏 JSON 行/非对象记录计入 `bad_lines` 并进异常清单）；
- `output + rejected == valid_records`；
- 任一等式不成立 → 运行 `failed`，不产出“成功”产物。

## 5. 本地 fixture（`dataset/sample/`）

| 文件 | 内容 |
|---|---|
| `make_sample.py` | 固定种子生成 fixture，`--stage <uid> --output-dir <root> --force` 可铺盘成任务输入 |
| `demo.jsonl` | 13 行：12 有效 + 1 坏 JSON，覆盖上表全部边界 |
| `manifest.json` | `crossmodal-manifest-v1`，入口 `demo.jsonl`，列出 4 个媒体 |
| `media/img_001.png`、`vid_001.mp4`、`aud_001.wav` | 可解码最小媒体（PNG/MP4 容器/WAV），字节可校验 |
| `media/img_zero.png` | 0 字节文件，用于"媒体不可读"负例 |
| `input_bad_encoding.jsonl` | GBK 字节，用于 `invalid_encoding` 负例 |
| `input_empty.jsonl` | 0 字节，用于 `empty_input` 负例 |
| `lineage_history.json` | （v4.2 停用，存档；`prior_lineage` 不再读取） |

`demo.jsonl` 各行意图：正常图文、无文本音视频（`not_applicable`）、纯文本、脏文本（标签/控制字符/零宽/U+FFFD/全角）、已有 `text` 对象重跑、纯空白音视频、清洗后为空（拒绝）、媒体缺失（拒绝）、媒体 0 字节（拒绝）、重复 `sample_id`（拒绝）、`text` 为数字（拒绝）、缺 `sample_id`、坏 JSON 行。

> 合成数据只用于功能与边界验证；本算子无模型，不报告精度指标。
