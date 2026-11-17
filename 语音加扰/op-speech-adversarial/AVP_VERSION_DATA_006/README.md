# 样本版本 · 服务器运行版本

- 算子编号：`06`。
- `operatorId`：`avp_pack_version`。
- 目录：`AVP_VERSION_DATA_006`。
- 阶段：`S1`，位置 `6/6`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

把质检通过的批次固化成带 sha256 manifest 的版本包，并支持发布/回滚/版本对比。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `version_zip` | `006_version.zip` |
| `version` | `006_version.json` |
| `compare` | `006_compare.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
