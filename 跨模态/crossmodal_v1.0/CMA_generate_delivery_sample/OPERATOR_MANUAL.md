# 算子说明书 · 生成交付样例（#26）

| 项 | 值 |
| --- | --- |
| 算子 ID | `delivery_sample` |
| 中文名 | 生成交付样例 |
| 正式编号 | **26** |
| 阶段 | `S8`（表1 第 8 环节：交付样例） |
| 版本 | `v1.0.0` |
| 类型 | 系统功能（模型列 `--`，无模型、不产出分数） |
| 优先级 | P2（可选，默认开启） |
| 端口 | 8126 |
| 入口 | `app:app`（HTTP）/ `processor.py`（CLI） |
| 输出根 | `OUTPUT_DIR/<uniqueId>/delivery_sample/runNNN/` |

## 做什么

拿到 #23 的 **可交付语料包**，**每条已开启路线抽至多 10 条**样本，导出
**Excel 样例清单**（样本ID、模态、描述摘要、标注结论、评分结论）与**样例报告**，
供验收人员「只打开 Excel 就能看到这一版的样子」。

## 三条硬口径

1. **只从可交付包抽样**：包级 `version_state=deliverable` / `status=可交付`；
   `require_deliverable_source=true` 时非可交付直接 `failed`。
2. **每路线最多 10 条**：表1 只读上限，配置与请求都不可放宽；
   不足 10 条按实际条数导出，并在 Excel「样例报告」与 `routes_insufficient` 写明。
3. **绝不用隔离区顶替**：#21 隔离标记或包内 `materials/isolation_list.jsonl` 命中的样本
   只登记进 `26_excluded.jsonl`；样例生成失败**不拦样本**、只在报告中记载并返回 `degraded`。

## 不做什么

预览页、波形图、HTML；生成/篡改任何分数；写回 #23 包；读取隔离区目录；
把阶段末 `data.outputFile` 当作下一算子输入路径。

## 怎么跑

```bash
python -m uvicorn app:app --port 8126
curl -X POST "http://127.0.0.1:8126/uploadFile?uniqueId=demo" -F "file=@23_deliverable_package.zip"
curl -X POST http://127.0.0.1:8126/predict -H "Content-Type: application/json" \
     -d '{"uniqueId":"demo","isLastOperator":true}'
# CLI
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

## 关键产物

`26_sample_list.xlsx`（主交付物，双工作表）、`26_samples.jsonl`（阶段内传递入口）、
`26_skipped.jsonl` / `26_excluded.jsonl` / `26_rejects.jsonl`（三类台账）、
`26_sample_report.json` / `.md`、`manifest.json`、`26_run.log`。

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py` 仅保留通用工具函数、代码存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

详细字段、配置与真实测试结果见 `README.md` 与 `dataset/DATA_SCHEMA.md`。
