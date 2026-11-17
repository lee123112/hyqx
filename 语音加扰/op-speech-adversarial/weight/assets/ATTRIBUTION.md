# 加扰素材来源与许可

本目录 `noise/`、`env/` 下的 4 个真实噪声素材，由 `tools/make_real_noise_assets.py`
从 **NOIZEUS 语料**反推生成；`白噪.wav` 为程序化合成。

## 1. 噪声原始来源

| 项 | 内容 |
|---|---|
| 语料 | NOIZEUS（Hu & Loizou, 2007），本机副本位于 `dataset/speech_noise_cc0/Audio/Data/Audio/Noizeus/` |
| 噪声类别 | `Babble`（多人交谈）、`Street`（街边）、`Station`（车站）、`Train`（列车）、`Car`（车内） |
| 原始采样率 | **8 kHz**，16-bit |
| 用途 | 仅**内测/联调**。许可条款未取得书面确认前，**不作为对外交付资产** |

## 2. 反推方法（为什么可信）

NOIZEUS 对同一句话提供两个版本：干净版 `NoNoise/spNN.wav` 与按 5 dB SNR 混入真实噪声的版本
`<Noise>/spNN_<noise>_sn5.wav`。混音是逐样本相加，因此

    noise = noisy − clean

就是当初混进去的那段真实噪声。验证结果（`sp01`–`sp03` × 3 类噪声）：

- 残差与干净语音的相关系数 **|r| < 0.03**（无语音泄漏）
- 由残差反算的 SNR ≈ **4.33–4.45 dB**（目标 5 dB，差额来自 PCM16 量化与既有增益）
- 30 条残差按 5 ms 交叉淡化拼接 → 每类噪声约 75 s，取前 60 s

## 3. 加工链

1. 逐条读 `NoNoise` 与 `<Noise>` 版本，取较短长度相减
2. 残差按 5 ms 线性交叉淡化拼接
3. 补足/裁剪到 **60 s**
4. 8 kHz → **16 kHz** 重采样
5. 峰值归一到 **0.8**
6. 写 **16 kHz / 单声道 / PCM16**

## 4. 业务取值映射

| 维度 | 取值 | 使用的噪声类别 |
|---|---|---|
| noise | 人群嘈杂 | `Babble` |
| noise | 交通噪声 | `Street` + `Station` + `Train` |
| env | 街道 | `Street` |
| env | 车内 | `Car` |
| noise | 白噪 | 程序化合成（平谱噪声按定义即合成） |

## 5. 局限与替代方案

- **带宽**：源素材 8 kHz，噪声有效带宽仅 4 kHz，升采样后仍带限，高频细节弱于现场录音。
- **替代**：若需 16 kHz 全带宽原始录音，可改用 DEMAND（CC BY-SA 3.0，
  `https://zenodo.org/records/1227121`）的 `STRAFFIC / PCAFETER / SPSQUARE / TCAR` 四个 16 kHz 包
  （合计约 467 MB）。本轮实测该站点下载速度仅约 32 KB/s，未能获取；
  脚本预留了 `--source demand` 的落点，下载到 `dataset/real_noise/demand/` 后即可切换。
- **合成素材**：`../assets_synthetic/` 保留原 5 个程序化合成素材，用于对照与许可回退。
