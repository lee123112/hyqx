# weight/ · 算子 #8（跨模态协同预处理）本地权重

> 本目录随**单算子**交付，不依赖包根 `weight/`，也不依赖 `shared_clip/`。
> 运行期只从本地路径加载（`local_files_only=True`），**绝不联网下载**。

## 1. 模型

| 项 | 值 |
|---|---|
| 模型 | Whisper-base（OpenAI 语音识别，表1 #8 固定项） |
| 用途 | 语音转写 → 带起止时间的文本（时间索引）+ 段落裁片 |
| 架构/精度 | `WhisperForConditionalGeneration` / fp32（CPU 可跑） |
| 解码语言 | 固定 `zh`，`model` 写其它值配置直接失败（边界：不换更大模型） |

## 2. 来源

- 交付副本复制自：`G:\HYQX\算子\跨模态\crossmodal_package_20261026\weight\whisper-base\`
- 上游原始目录：`G:\HYQX\算子\model\whisper-base`（HuggingFace `openai/whisper-base`）
- 复制方式：整目录 11 个文件全量复制，复制后逐一比对 SHA-256（全部一致）。

## 3. 文件清单（11 个文件，合计 294,776,748 字节）

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `added_tokens.json` | 34,604 | `9715FD2243B6F06A5858B5E32950D2853F73DD5BC201AAFCF76F5082A2D8ACD1` |
| `config.json` | 1,983 | `A153C53883A6799B6F056B4A8D1A515C9926D03994682BA88A7616618D7DA0C1` |
| `generation_config.json` | 3,807 | `444B3F636D2FFF89DD9ECF549E2A085B61F7FF0FA0246D4628BAC6A3B8CC9BA4` |
| `merges.txt` | 493,869 | `2DF2990A395E35E8DFBC7511E08C12D56018D8D04691E0133E5D63B21E154DC6` |
| `model.safetensors` | 290,403,936 | `07CADB9F25677C8D50DF603E66A98FBD842CCE45047139BAEB16E6219A1E807B` |
| `normalizer.json` | 52,666 | `BF1C507DC8724CA9CF9903640DACFB69DAE2F00EDEE4F21CEBA106A7392F26DD` |
| `preprocessor_config.json` | 184,990 | `9B5CD03A36FBB8A627C64D98A5B5B126EAD95A77720723944487311F0110B666` |
| `special_tokens_map.json` | 2,194 | `E67AE3A0AAA99ABCD9F187138E12DB1F65C16A14761C50EF10EEF2C174A7A691` |
| `tokenizer.json` | 2,480,466 | `27FC476BFE7F17299480BE2273FC0608E4D5A99ABA2AB5DEC5374B4482D1A566` |
| `tokenizer_config.json` | 282,683 | `2A4C4281CF9F51AC6CCC406FDC711A087AFE6530F671FA7B80953EDC498275CE` |
| `vocab.json` | 835,550 | `8F680BBA319E01A653D2E8A5DBC17A9157179E0576E6CE74CE0C06356C6E24F9` |

复算命令（Windows PowerShell）：

```powershell
Get-FileHash .\weight\whisper-base\* -Algorithm SHA256 | Format-Table Hash,Path -AutoSize
```

## 4. 默认路径（不设 `MODEL_ROOT` 时）

解析顺序（`utils/transcription.py::_model_root()`）：

1. `$MODEL_ROOT`（环境变量）最高优先；
2. 否则 `<本算子目录>/weight`（源码相对 `parents[1]/"weight"`，与包根无关）。

因此 `configs/config.yaml` 显式写：

```yaml
transcription:
  model_root: .          # 相对 MODEL_ROOT／<算子目录>/weight 解析；schema 缺省值 weight 等价
  model_dir: whisper-base
```

即加载 `<本算子目录>/weight/whisper-base/`（本目录）。`model_root` 写绝对路径时直接用该目录，
不再拼接 `$MODEL_ROOT`。

> 向后兼容：`model_root` 缺省值 `weight`（历史文档口径，大小写/首尾空白不敏感）与显式 `.` 等价，
> 均解析为 `<本算子目录>/weight/whisper-base`，不会再叠加成 `weight/weight`。

## 5. `MODEL_ROOT` 覆盖说明

- 设 `MODEL_ROOT=/models` 时：`model_root: .` + `model_dir: whisper-base` →
  `/models/whisper-base`；容器只需把本目录挂到 `/models/whisper-base`。
- 显式覆盖**优先且不回退**：`MODEL_ROOT` 指向的目录缺少 `config.json` 时，转写以
  `reason=whisper_weights_missing` 显式失败（记录进 `transcription_gate.direct_scoring_allowed=false`），
  不会静默回退到包根或算子内其它副本。
- 加载失败的原因写进运行日志 `run.log`：`whisper_loaded ... weights=<真实目录>` 或
  `whisper_not_loaded：reason=...`。

## 6. 独立部署（包外单算子）实测

测试方式：把本算子目录整体复制到包外临时目录（无包根 `weight/`、无 `shared_clip/`），
清空 `MODEL_ROOT`，用临时配置把 `enabled` 与 `transcription.enabled` 置 `true`：

```powershell
D:\python\python.exe -X utf8 inference.py --input dataset/sample/demo.jsonl `
    --unique-id task010_file10 --output-root out --config configs/standalone_enabled.yaml
```

| 场景 | 结果 |
|---|---|
| 自带 `dataset/sample/demo.jsonl`（2 条音视频） | `whisper_loaded ... weights=<临时算子目录>\weight\whisper-base`、`transcribed=2 failed=0`；转写状态 `ok`、文本非空（注意：该 fixture 音频为**纯静音**，Whisper 输出为重复幻觉文本，仅证明加载与推理链路，不能当质量证据） |
| 本地离线合成的中文语音（16 kHz 单声道 WAV，替换同一记录的 `audio.path`） | `whisper_loaded` 权重目录同上；转写 `text="今天天氣很好，我們現在開始測試語音轉寫功能。請把這段中文語音完整的轉寫下來…"`（繁体为模型输出，非本算子改写），`paragraphs` 起止 `0.04–5.8s / 6.6–13.84s`，段落裁片登记为产物 |
| 省略 `transcription.model_root`（schema 缺省 `weight`） | `whisper_loaded ... weights=<临时算子目录>\weight\whisper-base`（**未叠加**成 `weight/weight`）、`transcribed=1 failed=0`、记录 `status=ok`、文本非空 |
| 显式 `transcription.model_root: "."` 与显式 `"weight"` 对照 | 两者 `weights_dir()` 完全一致（均为 `<临时算子目录>\weight\whisper-base`）、转写成功；`MODEL_ROOT=<空目录>` 时仍显式 `whisper_weights_missing`（覆盖优先） |
| `MODEL_ROOT=<空目录>` | 记录 `transcription.status=failed`、`reason=whisper_weights_missing`、`text=null`、`direct_scoring_allowed=false`；日志 `whisper_not_loaded reason=whisper_weights_missing`（**未回退**到本地权重） |

`python -m pytest -q`：**85 passed, 32 skipped**（既有 skip：v4.2 血缘/运输输出停用等，与本改动无关）。

