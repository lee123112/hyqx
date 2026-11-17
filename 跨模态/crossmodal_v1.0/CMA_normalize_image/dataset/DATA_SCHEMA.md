# 算子 4 数据契约（图像标准化处理）

> 共享契约：`G:/HYQX/算子/跨模态/demo/DATA_SCHEMA.md` §7、`DEVELOPMENT_CONTRACT.md` §3/§4。
> 本文只固定本算子的输入承接、输出改写与样例媒体，字段名冻结后不得随意变更。
> 样例媒体由 `dataset/sample/make_sample.py` 用固定种子真实生成；合成数据仅用于功能验证，**不报告模型精度**。

## 1. 任务目录与入口

```text
uniqueId = 任务id_文件id
OUTPUT_DIR/<uniqueId>/
  input/manifest.json + 03_text_cleaned.jsonl + media/
  image_standardization/<run_id>/  (本次运行的全部产物)
```

> 注：v4.2 起不再写 `lineage.json` / `stage_context.json`（血缘/阶段归档输出已停用）；`lineage_history.json` 为历史样例存档，不再读取。

- 阶段内上游：HTTP `inputFile` 取上游响应 `outputFile` 恢复出的任务内路径；阶段首省略 `inputFile`，按 `input/manifest.json` 定位。
- 样例入口 `dataset/sample/demo.jsonl` 每行一条完整记录（14 行，含 1 行故意损坏 JSON），媒体在 `dataset/sample/media/`。

## 2. 输入记录（承接 03 主清单，完整保留）

```json
{
  "operator": "text_normalization", "operator_version": "v1.0.0",
  "uniqueId": "demoS2_f1", "stageId": "S2",
  "sample_id": "demo_04_wide_001",
  "source_language": "zh", "target_language": "en", "domain": "通用",
  "source": {"type": "web", "uri": "https://example.org/data", "license": "CC-BY-4.0"},
  "text": {"path": "03_text_cleaned.jsonl", "content": "...", "tokens": ["..."], "char_count": 34, "token_count": 4},
  "image": {"path": "media/wide_1600x800.png", "width": 0, "height": 0, "format": null, "blur_score": null, "source": "upstream"},
  "video": null, "audio": null,
  "alignment": {"source_to_text": "exact"},
  "qc": {"stage1_score": 0.91},
  "processing": {"01": {"status": "success"}, "02": {"status": "success"}, "03": {"status": "success"}},
  "metadata": {"batch": "demoS2", "collector": "fixture"}
}
```

入口归一化（共享 §7.1）：`image` 可为 `null`、相对路径字符串、或对象；本算子把对象/字符串统一规范为输出 `image` 对象。`text`/`video`/`audio` 保持原样透传。未知字段（本例 `source_language`、`domain`、`source`）原样保留。

## 3. 输出记录（04 只改写 image 与 processing["04"]）

在输入记录基础上：

- `image`：替换为本算子规范对象（见下），原 `source` 字段保留；
- `processing["04"]`：`{"status":1,"process_status":"success","reason":null,"metrics":{...}}`，不适用模态为 `{"status":1,"process_status":"not_applicable","reason":"video_audio_pair|text_only","metrics":{"images":0}}`；
- 其余字段（`text`、`video`、`audio`、`alignment`、`qc`、`metadata`、上游 `processing` 编号、未知字段）**不得重置**。

`image` 对象（冻结字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | string | 相对 manifest 根的标准化图路径，如 `04_images/s1.png` |
| `original_path` | string | 另存原图路径，如 `media/original/s1.png`；字节与输入一致 |
| `width` / `height` | int | 归一后尺寸 |
| `format` | string | 输出格式，`PNG`/`JPEG` |
| `blur_score` | number | 8bit 灰度 Laplacian 方差，越大越清晰，无量纲、非概率 |
| `blur_metric` | string | 固定 `laplacian_variance` |
| `blur_backend` | string | 实际后端：`opencv_laplacian`\|`numpy_laplacian`\|`numpy_laplacian_fallback` |
| `blur_threshold` | number | 本次生效阈值 |
| `blur_status` | string | `ok`\|`flagged`（`blur_score < threshold` 即 flagged） |
| `original_width` / `original_height` | int | 解码后的原始像素尺寸 |
| `original_format` | string\|null | 原始格式 |
| `source` | string | 透传上游来源标记 |
| `transform` | object | 归一过程明细（见下） |

`transform` 明细：`original_width/height/mode/format`、`exif_orientation`、`orientation_applied`、`oriented_width/height`、`scale`、`resized`、`upscaled`、`background`、`output_format`、`jpeg_quality`、`original_pixel_count`、`output_pixel_count`。

## 4. 异常清单 `04_rejects.jsonl`

```json
{
  "sample_id": "demo_04_missing_001",
  "line": 10,
  "reason": "media_missing:image",
  "record": {"sample_id": "demo_04_missing_001", "image": null, "...": "..."},
  "evidence": {"media_errors": ["media/missing.png"]}
}
```

坏行（无法解析的 JSON）单独记录 `line` 与 `reason=invalid_json`，不保存无限长原文。稳定 `reason` 取值：

| reason | 触发条件 |
|---|---|
| `invalid_json` | 行不是合法 JSON |
| `invalid_type:sample_id` / `invalid_type:image` / `invalid_type:processing` | 关键字段类型错误 |
| `duplicate_sample_id` | 本素材内 `sample_id` 重复（首个保留，后续拒绝） |
| `media_missing:<field>` | 引用的媒体文件不存在 |
| `image_decode_failed` | Pillow 无法解码/像素超限/后缀不匹配等 |
| `image_pixel_limit_exceeded` | 解码前像素数超过 `max_pixels` |
| `blur_rejected` | `blur_action=reject` 且 `blur_score < threshold` |

## 5. 计数与守恒

`statistics` 关键字段：`total_lines`、`valid_records`、`bad_lines`、`output`、`rejected`、`not_applicable`、`images_standardized`、`images_resized`、`images_size_kept`、`originals_copied`、`blur_flagged`、`blur_rejected`、`image_processing_failed`、`media_copied`、`missing_media`、`duplicates`、`reason:*` 明细、`conservation_ok`。

约束（每次运行必须成立，`conservation_ok=true`）：

```text
output + rejected == valid_records          # not_applicable 是 output 子集
total_lines == valid_records + bad_lines
```

## 6. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

## 7. 样例媒体清单（`dataset/sample/media/`）

| 文件 | 用途 | 期望 |
|---|---|---|
| `wide_1600x800.png` | 等比缩放 | 1600×800 → 1024×512 |
| `small_64x32.png` | 不放大小图 | 仍 64×32 |
| `exif_rot_90.jpg` | EXIF 方向归一 | 320×160 → 160×320，输出不含方向标记 |
| `alpha_64x64.png` | RGBA 合成 | 合成到 `alpha_background` 后转 RGB |
| `sharp_checker_512.png` | 清晰样例 | `blur_score` 远大于模糊样例 |
| `blur_checker_512.png` | 模糊样例 | `blur_score < blur_threshold`（默认 80） |
| `corrupt.png` | 解码失败 | `image_decode_failed` |
| `zero.png` | 空文件 | `image_decode_failed` / `media_missing` 之外明确拒绝 |
| `vid_001.mp4` + `aud_001.wav` | 音视频透传 | `not_applicable`，引用随包搬运 |
| （缺）`missing.png` | 缺媒体 | `media_missing:image` |
