# 语料入库匹配校验 · 服务器运行版本

- 算子编号：`01`。
- `operatorId`：`avp_ingest_corpus`。
- 目录：`AVP_INGEST_AUD_001`。
- 阶段：`S1`，位置 `1/6`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

把音频与参考文本清单配对成带 uttid 的语料数据集，并按三项内置规则做入库检测（只提示不阻断）。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `corpus` | `001_corpus.jsonl` |
| `rejects` | `001_rejects.jsonl` |
| `report` | `001_ingest_report.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
