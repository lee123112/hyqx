# 算子2 数据 Schema

> 全局契约见 `../DATA_SCHEMA.md`（冻结版 v1.0），本文件仅描述本算子差异。

## 输入

句对 JSONL，每行：`{"id","source","target","source_lang","target_lang","metadata":{}}`。
必填：`id`、`source`、`target`（见 `configs/config.yaml` 的 `required_fields`）。

## 输出

- `02_validated.jsonl`：通过校验的记录，`warn` 策略下违规记录保留并写入 `metadata.warnings`
- `02_rejects.jsonl`：异常清单，每行 `{"line": 行号, "record": 原始记录或null, "reasons": [...]}`

## 异常原因枚举

| reason | 含义 |
|---|---|
| `invalid_json` | 行不是合法 JSON |
| `invalid_record` | 行不是 JSON 对象 |
| `missing_field:<字段>` | 缺少必填字段 |
| `empty_field:<字段>` | 字段为空白 |
| `lang_mismatch:source(实际!=配置)` | 源语言声明与任务不一致 |
| `lang_mismatch:target(实际!=配置)` | 目标语言声明与任务不一致 |
