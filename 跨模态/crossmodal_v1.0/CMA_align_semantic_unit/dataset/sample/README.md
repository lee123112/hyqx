# 算子 8 fixture 数据说明

## 数据来源
除 `make_sample.py` 自身外，本目录全部文件由 `dataset/sample/make_sample.py`
**程序化生成**（`input_source=fixture`），不来自生产语料，也未使用任何模型输出：

- `04_images/`、`05_frames/`、`06_audio/`：真实 PNG（纯色，尺寸与记录元数据一致）与
  真实 WAV（16bit 单声道静音，时长与记录一致）；
- `media/original/*.mp4`：占位容器（仅 `ftyp` + `mdat` 盒，无轨道、不可解码），
  只用于验证“原视频引用被搬运且字节一致”，不冒充真实视频，也不用于解码；
- `demo.jsonl`：入口清单，5 条 post-06 形态的完整记录；
- `03_rejects.jsonl`：上游旁路样本（1 条），用于验证上游处理结果随包携带；
- `manifest.json`：`crossmodal-manifest-v1`，entrypoint=`demo.jsonl`。

## 覆盖的判定分支（转写默认关闭）
| sample_id | 组合 | 预期 |
| --- | --- | --- |
| demo_07_a1_en | text+image（含原图） | `not_applicable`（`reason=no_audio_input`），`semantic_unit=null` |
| demo_07_a2_en | text+image（无 original_path） | `not_applicable` |
| demo_07_v1_en | video+audio（text=null） | `skipped`（未加载模型；打开转写后可转写） |
| demo_07_t1_zh | 仅 text | `not_applicable`（透传） |
| demo_07_v2_en | text+image+video+audio | `skipped`（不再做音视频时长比对） |

## 生成与校验
```powershell
python dataset/sample/make_sample.py
python -c "import sys; sys.path.insert(0,'.'); from utils import transport as t; print(t.load_manifest('dataset/sample')['entrypoint'])"
```

## 复现与限制
- 记录中的 `processing.02`—`processing.06` 是手工编写的上游形态样本，用于验证字段保留，
  不是真的跑过 02—06 的产物；
- 媒体内容为合成文件，只能验证流转与字节一致性，不能验证任何解码/特征质量。
