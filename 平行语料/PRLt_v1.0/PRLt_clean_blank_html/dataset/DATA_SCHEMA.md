# 算子 4 数据 Schema

## 输入

输入是算子 3 产生的 UTF-8 JSONL，每个非空行是一个 JSON 对象。默认只清理 `source` 和 `target` 字段；`id`、语言声明及其他元数据保持原样。

```json
{
  "id": "sample-0001",
  "source": "<p>源语言文本</p>",
  "target": "目标语言文本&nbsp;",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {}
}
```

## 清理规则

- 使用 Python 标准库 `HTMLParser` 提取可见文本，移除标签和注释，HTML 实体还原为字符。
- 丢弃 `script`、`style`、`noscript` 和 `template` 内容；块标签和 `<br>` 转为空格。
- 删除零宽空格、BOM 等不可见格式字符。
- 连续空白（包括换行、制表符和不间断空格）折叠为一个普通空格并裁剪首尾空格。
- 不负责网页抓取、URL 删除、页眉页脚识别或时间戳清理；这些属于其他节点或工程侧能力。

## 输出

- `04_html_cleaned.jsonl`：清理后的 UTF-8 JSONL。
- `04_html_rejects.jsonl`：损坏 JSON、非对象记录、文本字段类型错误和元数据类型错误。
- `artifacts/stage_04_<uniqueId>.zip`：阶段产物（主输出、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json`。
- `run.log`：带时间和动作标签的日志。

每条通过记录的 `metadata` 增加：

```json
{
  "html_tags_removed": 2,
  "whitespace_normalized": 1,
  "invisible_chars_removed": 0,
  "cleaned_fields": ["source"]
}
```

统计包含 `total`、`passed`、`rejected`、`records_changed`、三类清理计数、`encoding` 和 `reason:*`。

## 配置

```yaml
input_encodings: [utf-8]
text_fields: [source, target]
strip_html: true
remove_invisible: true
normalize_whitespace: true
whitespace_mode: linewise  # linewise | global | trim | preserve
```
