# 评测结果接收 · 服务器运行版本

- 算子编号：`07`。
- `operatorId`：`avp_ingest_eval_result`。
- 目录：`AVP_EVAL_DATA_007`。
- 阶段：`S2`，位置 `1/1`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

校验数据集评测回传的字段与版本 ID，通过则落库，不通过整条标记解析失败不落库。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `results` | `007_eval_results.jsonl` |
| `parse_failed` | `007_parse_failed.jsonl` |
| `report` | `007_eval_report.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
