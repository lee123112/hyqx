# 噪声混音加扰 · 服务器运行版本

- 算子编号：`03`。
- `operatorId`：`avp_mix_noise`。
- 目录：`AVP_PERTURB_AUD_003`。
- 阶段：`S1`，位置 `3/6`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

按加扰策略把噪声/环境音素材按目标 SNR 混入标准语料，产出带加扰参数快照的批次。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `batch` | `003_batch.jsonl` |
| `report` | `003_perturb_report.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
