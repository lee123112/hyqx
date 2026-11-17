# 优化建议生成 · 服务器运行版本

- 算子编号：`09`。
- `operatorId`：`avp_suggest_optimization`。
- 目录：`AVP_ADVICE_DATA_009`。
- 阶段：`S3`，位置 `2/2`。
- 服务器统一端口：`17023`，调用 `POST /predict`。

把归因结论转成 1–3 条可执行建议，四段式（事实/证据强度/解释/验证方式）；数字全由系统计算，文字由固定模板回填。

## 运行文件

- `processor.py`：本算子处理入口。
- `configs/config.yaml`：随算子交付的运行配置。
- [字段契约](dataset/DATA_SCHEMA.md)：输入输出字段与判定口径。
- [算子说明书](OPERATOR_MANUAL.md)：完整业务说明。

## 产物

| 名称 | 文件 |
|---|---|
| `advice` | `009_advice.json` |

参数规格以 [map.json](../map.json) 及服务 `GET /operators` 返回的 `paramsSpec` 为准。
部署及远程路径读取见 [服务器部署说明](../服务器部署说明.md)。
本部署目录仅在 `dataset/` 中保留字段契约文档，不携带样本、测试及历史输出。
