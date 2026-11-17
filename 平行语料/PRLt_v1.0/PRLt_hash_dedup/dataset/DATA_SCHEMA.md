# 算子 8 数据 Schema

## 输入

默认输入是算子 7 产生的 `06_length_filtered.jsonl`（严格 UTF-8，每行一个 JSON 对象）。

```json
{
  "id": "len-0001",
  "source": "欢迎使用平行语料系统。",
  "target": "Welcome to the parallel corpus system.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {}
}
```

- 必填字段：`source`、`target`（字符串）；`metadata` 若存在必须是对象（缺省时补 `{}`）。
- `id` 可选；`source_lang`/`target_lang` 及其他字段原样保留。
- 缺字段、类型错误、损坏 JSON、非对象行都进入 `07_rejects.jsonl`，不静默丢弃。

## 去重键

```text
dedup_key = "<algorithm>:<hexdigest>"
payload   = canonical(source) + "\x1f" + canonical(target)
canonical = unicode_normalization → 可选 casefold → 可选连续空白折叠与首尾裁剪
```

- 键**同时**使用源文和目标文；只有源文相同或只有目标文相同的两条记录不会被判为重复。
- 规范化只用于计算键，**不改写**输出记录里的原文；不删除标点。
- 分隔符固定为 `\x1f`，写入配置与输出记录 metadata，避免源/目标边界碰撞。
- 原文中若出现 `\x1f`，该行以 `separator_in_text` 进入异常清单（控制字符应由算子 3 清除）。
- 规范化幂等，因此可用重复清单中的原始记录 + 输出记录 metadata 中的 dedup 配置回放同一个 `dedup_key`。

## 输出

| 文件 | 内容 |
|---|---|
| `07_deduped.jsonl` | 保留的首次出现记录（源+目标组合唯一） |
| `07_duplicates.jsonl` | 重复记录清单（含行号、`duplicate_of`、`dedup_key`、原记录、原因 `duplicate_pair`） |
| `07_rejects.jsonl` | 结构异常清单（损坏 JSON、非对象行、缺字段、类型错误、分隔符冲突） |
| `artifacts/stage_07_<uniqueId>.zip` | 阶段产物（主输出、重复清单、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json` |
| `run.log` | 带时间和动作标签的日志 |

去重输出记录的 `metadata` 增加：

```json
{
  "dedup": {
    "dedup_key": "sha256:...",
    "hash_algorithm": "sha256",
    "unicode_normalization": "NFC",
    "casefold": true,
    "collapse_whitespace": true,
    "pair_separator": "\u001f",
    "first_line": 1,
    "first_id": "len-0001",
    "is_duplicate": false
  }
}
```

重复清单每行：

```json
{
  "line": 17,
  "duplicate_of": {"line": 3, "id": "len-0003"},
  "dedup_key": "sha256:...",
  "reasons": ["duplicate_pair"],
  "record": {"id": "len-0017", "source": "...", "target": "..."}
}
```

统计包含：`total`、`passed`、`rejected`、`duplicates`、`unique_pairs`、`duplicate_pairs`、
`unique_source_count`、`unique_target_count`、`duplicate_ratio`、`hash_algorithm`、
`unicode_normalization`、`casefold`、`collapse_whitespace`、`pair_separator`、
`max_tracked_pairs`、`memory_limit_exceeded`、`encoding` 以及 `reason:*` 计数。

`unique_source_count` / `unique_target_count` 按所有有效记录（保留 + 重复）的规范化单边文本去重统计。

## 配置

```yaml
hash_algorithm: sha256        # sha256（默认）| md5（仅为兼容）
unicode_normalization: NFC    # NFC | NFD | NFKC | NFKD | none
casefold: true
collapse_whitespace: true
keep_first: true              # 本版本仅支持 true
max_tracked_pairs: 5000000    # 超过上限明确失败，不静默截断去重范围
text_fields: [source, target]
input_encodings: [utf-8]
```

## 责任边界

本节点只做精确的规范化句对去重：不删除近义句、不做单边去重、不做模糊相似判断、
不做语言检测、不评价语义等价、不输出模型分数、不跨 `uniqueId` 共享状态。
去重状态不会写到任务目录之外。
