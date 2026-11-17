# 算子 7 dataset 目录

| 路径 | 内容 |
| --- | --- |
| `DATA_SCHEMA.md` | 输入/输出记录、异常清单、媒体与统计字段的落地数据说明 |
| `sample/make_sample.py` | fixture 生成脚本（可重复执行，就地重建） |
| `sample/demo.jsonl` | 入口清单：5 条 post-06 形态记录 |
| `sample/manifest.json` | `crossmodal-manifest-v1`，`entrypoint=demo.jsonl`，`input_source=fixture` |
| `sample/03_rejects.jsonl` | 上游旁路样本，验证“上游处理结果随包携带” |
| `sample/04_images/` `sample/05_frames/` `sample/06_audio/` `sample/media/original/` | 真实 PNG/WAV 与占位 MP4 容器 |
| `sample/README.md` | fixture 覆盖分支表、生成与校验方式、限制 |

## 数据来源与性质

全部 fixture 数据由脚本程序化生成，**不来自生产语料，也不含任何模型输出**：

- PNG/WAV 是**真实可解析文件**（尺寸/时长与记录元数据一致），用于验证搬运与字节一致性；
- `media/original/*.mp4` 是**占位容器**（仅 `ftyp` + `mdat` 盒，无轨道、不可解码），
  只用于验证“原视频引用被搬运”，不冒充真实视频、不用于解码；
- 记录中的 `processing.02`—`processing.06` 是手写的上游形态样本，用于验证字段保留，
  并非真的跑过 02—06。

## 生成、校验与用途

```powershell
# 就地重建 fixture（保留脚本自身）
python dataset/sample/make_sample.py

# 校验清单与入口
python -c "import sys; sys.path.insert(0,'.'); from utils import transport as t; m=t.load_manifest('dataset/sample'); print(m['entrypoint'], len(m['artifacts']))"

# 作为上传素材跑一次
python inference.py --input dataset/sample/demo.jsonl --unique-id demo_task_001 --output-root output --quiet
```

明细见 `sample/README.md`；字段级契约见 `DATA_SCHEMA.md`。
