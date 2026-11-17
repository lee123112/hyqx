# 优化建议报告 · 服务器运行版本

- 算子编号：`11`。
- `operatorId`：`avp_generate_report`。
- 目录：`AVP_REPORT_DATA_011`。
- 阶段：`S5`，位置 `1/1`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

把评测结果、归因与建议汇编成八段式优化建议报告（Markdown + 自包含 HTML + PDF，三种格式同源同数）。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `report_md` | `011_report.md` |
| `report_html` | `011_report.html` |
| `report_pdf` | `011_report.pdf` |
| `report_meta` | `011_report_meta.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
