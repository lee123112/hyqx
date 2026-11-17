# weight/ · 算子 #13（评分音文语义一致性）权重说明

> **本算子生产路线不加载任何独立基座权重**，因此本目录**不内置** `wav2vec2-base` / `bert-base-chinese`
> （合计约 750 MB）。本文件记录核定依据、诊断接口的默认路径与 `MODEL_ROOT` 覆盖方式。
> 运行期绝不联网下载、也不静默回退到包根 `weight/`。

## 1. 生产判定用什么模型？

| 项 | 值 |
|---|---|
| 判定方式 | 规则型 CER：`通过 ⇔ 转写文本与描述编辑距离比例 ≤ edit_distance_ratio_max(0.30)` |
| 转写文本来源 | **上游 #10/#8 产出的 Whisper-base 转写**（本算子不重新加载 Whisper） |
| 本地基座 wav2vec2/BERT | **生产不加载**；旧版适配器仅保留为离线实验/诊断（显式注入 `scorer`） |
| 归一化 | 繁→简（`utils/data/TSCharacters.txt`）+ 中文 ITN，均纯规则、离线 |

## 2. 核定证据（调用链，文件:行号）

1. `app.py:366`、`app.py:474` 只会调用 `inference.run(...)`；`app.py:33` 的 `from utils import scoring`
   仅用于 `app.py:104` 的 `DependencyError` 异常处理器，不探测/不加载模型。
2. `inference.py:463-476`（`run()` 开启路线）：`models = {"scorer": scorer, "source": "rule_only"}`，
   注释明确「v2 生产判定复用上游 Whisper-base 转写，只做文本一致性规则。Wav2Vec2/BERT 不再作为本算子依赖；
   显式传入 scorer 仅用于实验诊断」。
3. `inference.py:315-343`：`_resolve_models()` 的 docstring 写明「生产 `run` 不调用此函数」；
   唯一会 `scoring.probe_models()` / `scoring.load_scorer()` 的入口就在此兼容诊断函数里，
   而 `run()` 从不调用它（全仓 `_resolve_models` 仅此定义、无生产调用点）。
4. `inference.py:346-352` + `inference.py:484-487`：辅助层「Whisper-base is produced upstream by #10
   and is never reloaded here」，`auxiliary_models` 恒为 `scorer=None / available=False / source="upstream"`。
5. `configs/config.yaml:2`：「v2 生产口径：复用上游 Whisper-base 转写，规则型 CER 门禁；
   不在本算子加载独立基座模型」。
6. 结论：**没有任何生产路径会加载 `wav2vec2-base` / `bert-base-chinese`**，故不复制约 750 MB 权重；
   生产 `enabled=true` 时规则评分可正常产出，且 `raw_cosine_similarity` 等诊断值为 `null`（不伪造）。

## 3. 诊断接口的默认路径（不设 `MODEL_ROOT` 时）

`utils/scoring.py::_model_root()` 的解析顺序：

1. `$MODEL_ROOT`（环境变量）最高优先；
2. 否则**算子本地目录** `<本算子目录>`（源码相对 `parents[1]`）。

配置字段 `model_root`（默认 `weight`）相对该根解析，因此默认诊断路径为：

| 配置 | 默认解析结果 |
|---|---|
| `model_root: weight`（schema 默认） + `audio_model_dir: wav2vec2-base` | `<本算子目录>/weight/wav2vec2-base` |
| `model_root: weight` + `text_model_dir: bert-base-chinese` | `<本算子目录>/weight/bert-base-chinese` |
| `auxiliary_model_dir: whisper-base`（`auxiliary_enabled` 默认关） | `<本算子目录>/weight/whisper-base` |

由于本目录未内置这些权重，`scoring.probe_models()` 会**显式**返回
`missing=["audio_weights:...","text_weights:..."]`，**不会联网下载**；生产 `run()` 根本不做该探测。

## 4. `MODEL_ROOT` 覆盖说明

- 设 `MODEL_ROOT=/models` 时：默认 `model_root: weight` → `/models/weight/wav2vec2-base`、
  `/models/weight/bert-base-chinese`（相对字段挂在 `MODEL_ROOT` 之下）。
- 覆盖**优先且不回退**：`MODEL_ROOT` 下找不到权重即显式 `missing`/`DependencyError`，不会静默改用
  `<本算子目录>/weight` 或包根副本。
- 只有做离线诊断实验时才需要放权重：把 `wav2vec2-base/`、`bert-base-chinese/` 放到
  `<本算子目录>/weight/`（或用 `MODEL_ROOT` 指向的目录），再通过显式 `scorer=` 注入调用；
  生产判定与入池/隔离口径不因此改变。

## 5. 独立部署（包外单算子）实测

本算子目录整体复制到包外临时目录（无包根 `weight/`、无 `shared_clip/`），`weight/` 内只有本
README；`--override '{"enabled": true}'` 开启规则路线后运行自带 `dataset/sample/demo_enabled.jsonl`：

| 场景 | 结果 |
|---|---|
| 默认（不设 `MODEL_ROOT`） | `code=0`；`models={"available":false,"source":"rule_only","reason":"production_uses_upstream_whisper_transcript"}`；`scored_records=4`、`passed_records=3`、`pending_records=1`、`conservation_ok=true`、`degraded=false`；记录 `scoring_method=character_edit_distance_ratio`、`cer=0.0`、`transcript_source=audio.transcript`、`cosine_similarity=null`、`audio_model=null`、`auxiliary_similarity=null` |
| `MODEL_ROOT=<空目录>` | 同上（result 完全一致）；规则评分**不依赖**本地权重 |
| 诊断 `scoring.probe_models()`（默认根） | `available=false`，`missing=["audio_weights:<算子目录>/weight/wav2vec2-base","text_weights:<算子目录>/weight/bert-base-chinese"]` |
| 诊断 `scoring.probe_models()`（`MODEL_ROOT=<空目录>`） | `missing=["audio_weights:<空目录>/weight/wav2vec2-base", ...]`，覆盖生效且**不回退**、不联网 |

`python -m pytest -q`：**225 passed, 36 skipped**（既有 skip：v4.2 血缘/运输输出停用、模型门禁反转等，与本改动无关）。

