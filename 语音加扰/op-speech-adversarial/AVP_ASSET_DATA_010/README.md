# 数据资产统计 · 服务器运行版本

- 算子编号：`10`。
- `operatorId`：`avp_asset_statistics`。
- 目录：`AVP_ASSET_DATA_010`。
- 阶段：`S4`，位置 `1/1`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

汇总资产总量、数据集数、时间范围内新增与按天趋势；只做统计，不承载质量问题处理与回流运营。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `asset` | `010_asset.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
