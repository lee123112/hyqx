# 样本质检 · 服务器运行版本

- 算子编号：`05`。
- `operatorId`：`avp_inspect_batch`。
- 目录：`AVP_QC_AUD_005`。
- 阶段：`S1`，位置 `5/6`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

全量自动检查（完整性 + 信噪比）后按比例抽样生成人工试听队列；复核判定回填后产出最终样本表与版本草稿样本数。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `report` | `005_qc_report.json` |
| `review_queue` | `005_review_queue.jsonl` |
| `batch_final` | `005_batch_final.jsonl` |
| `qc_final` | `005_qc_final.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
