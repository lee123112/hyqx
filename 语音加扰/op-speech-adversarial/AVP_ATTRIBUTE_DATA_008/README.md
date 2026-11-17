# 差异归因 · 服务器运行版本

- 算子编号：`08`。
- `operatorId`：`avp_attribute_gap`。
- 目录：`AVP_ATTRIBUTE_DATA_008`。
- 阶段：`S3`，位置 `1/2`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

按归因规则对评测结果逐维度下钻，输出主因、占比、影响条数、置信度与归因链路。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `attribution` | `008_attribution.json` |
| `alerts` | `008_alerts.json` |
| `version_compare` | `008_version_compare.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
