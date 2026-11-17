# 加扰素材库

> **当前为真实录制噪声**：4 个取值来自 NOIZEUS 语料的真实噪声类别（由「混音 − 干净」反推得到），
> 白噪按定义保留程序化合成。生成脚本 `tools/make_real_noise_assets.py`，来源与处理链见
> `ATTRIBUTION.md` 与 `real_noise_manifest.json`。
>
> 本部署包保留上述 5 个实际运行素材；备用素材及生成脚本只在原 AVP 开发源包中保留。

| 维度 | 取值 | 文件 | 来源 | 时长 |
|---|---|---|---|---|
| noise | 白噪 | `noise/白噪.wav` | 程序化合成（平谱即定义） | 30 s |
| noise | 人群嘈杂 | `noise/人群嘈杂.wav` | NOIZEUS `Babble` | 60 s |
| noise | 交通噪声 | `noise/交通噪声.wav` | NOIZEUS `Street+Station+Train` | 60 s |
| env | 街道 | `env/街道.wav` | NOIZEUS `Street` | 60 s |
| env | 车内 | `env/车内.wav` | NOIZEUS `Car` | 60 s |

统一参数：单声道 / 16000 Hz / WAV PCM16 / 峰值 0.8。

**已知局限**：NOIZEUS 原始采样率 8 kHz，噪声有效带宽 4 kHz，升采样后仍是带限的，
高频细节不如现场 16 kHz 录音——用作加扰素材成立，但不能当作「甲方真实场景」。

`dimension` 取值与任务书 §3.3 一致：`noise`（噪声）/ `env`（环境音）。
口音不是加扰维度，本目录不含口音素材。
