# 文本参考语料（reference/）

## 当前主基线：`text_reference.jsonl`

- 1,000 条不重复中文句段，来自 **CLUE CLUENER train** 派生。
- 来源文件：`../text/clue/cluener/train-00000-of-00001-39fc4e004146ece2.parquet`。
- 许可证：镜像标 Apache-2.0；原始 CLUENER 条款仍需复核。
- 生成参数：固定随机种子 42；只取 `text` 字段，过滤长度不足 8 的短句，去重后取 1,000 条；
  每条带 `id/input/scene/source/license/sourceFile`。
- 用途：文本基线模型的 D_ref 语料，与候选集做同架构、同超参、同字符量训练对照。
  它是参考语料，不是评测集；不能拿训练句段当 CLUE 测试集。

## 旧夹具：`text_reference_legacy.jsonl`

- 原文件是 22 条项目自写中文测试句段，保留用于回归对照。
- 它不再作为主基线，因为规模太小、来源不是公开高质量语料；不要在新报告里继续用它
  冒充 D_ref。

## 旧版 README 中的事实修正

旧 README 把这 22 条写成“唯一基线语料”。新规范下，D_ref 应使用来源明确、质量更高的
公开/内部参考数据；当前已切换为 CLUENER 派生语料。旧文件不删除，避免破坏历史测试，
但新实验必须引用 `text_reference.jsonl` 的来源和版本。

## 不要这样用

- 不要把参考语料和 `sample/text/clean.jsonl`、`noisy.jsonl` 混在一起计算候选指标。
- 不要用参考语料训练后再在同一批句段上报告“泛化”结果。
- 不要在没有许可证确认的情况下把 CLUENER 派生文件对外分发。
