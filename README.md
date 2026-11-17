# HYQX 算子源码

2026-11-17 整理的四类算子源码与部署配置。模型权重不进入 Git；各项目可在所属目录内独立查看和部署。

| 分类 | 项目入口 | 算子数量 |
|---|---|---:|
| 跨模态 | [跨模态/crossmodal_v1.0](%E8%B7%A8%E6%A8%A1%E6%80%81/crossmodal_v1.0/README.md) | 26 |
| 平行语料 | [平行语料/PRLt_v1.0](%E5%B9%B3%E8%A1%8C%E8%AF%AD%E6%96%99/PRLt_v1.0/README.md) | 23 |
| 小模型测评 | [小模型测评/SME](%E5%B0%8F%E6%A8%A1%E5%9E%8B%E6%B5%8B%E8%AF%84/SME/README.md) | 5 |
| 语音加扰 | [语音加扰/op-speech-adversarial](%E8%AF%AD%E9%9F%B3%E5%8A%A0%E6%89%B0/op-speech-adversarial/README.md) | 11 |

## 使用说明

先进入具体项目目录，阅读原 README 和 `GITHUB_SOURCE_NOTICE.md`，补齐本地权重与 `.env` 后再运行。各项目保留原部署包中的模型配置、分词器、词表、权重路径/散列清单。

- [跨模态：权重恢复说明](%E8%B7%A8%E6%A8%A1%E6%80%81/crossmodal_v1.0/GITHUB_SOURCE_NOTICE.md)
- [平行语料：权重恢复说明](%E5%B9%B3%E8%A1%8C%E8%AF%AD%E6%96%99/PRLt_v1.0/GITHUB_SOURCE_NOTICE.md)
- [小模型测评：权重恢复说明](%E5%B0%8F%E6%A8%A1%E5%9E%8B%E6%B5%8B%E8%AF%84/SME/GITHUB_SOURCE_NOTICE.md)
- [语音加扰：权重恢复说明](%E8%AF%AD%E9%9F%B3%E5%8A%A0%E6%89%B0/op-speech-adversarial/GITHUB_SOURCE_NOTICE.md)

语音加扰 `weight/assets/` 内的 WAV 是运行必需的噪声素材，已保留。其余 `.pt`、`.pth`、`.bin`、`.safetensors` 等模型权重已排除；不使用 Git LFS 上传权重。

`.env.example` 可供配置参考，真实 `.env` 与缓存/日志不在仓库中。原交付清单、验收与内置权重描述属于完整部署包的历史记录，应结合各项目的源码副本说明阅读。

整理过程只生成独立上传副本，未修改四个原始项目。上传源文件的路径、大小和 SHA-256 见 [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json)，未上传权重见 [MODEL_WEIGHTS_EXCLUDED.json](MODEL_WEIGHTS_EXCLUDED.json)。
