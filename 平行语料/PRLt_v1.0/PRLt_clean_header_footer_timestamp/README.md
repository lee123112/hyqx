# 算子5：页眉页脚与时间戳清理（layout_noise_cleanup）

平行语料粗清洗阶段第 5 个节点。只做**版式噪声**清理：整行字幕时间戳、整行页码、
跨记录高频重复的页眉/页脚行、用户自定义整行模式，以及具备明确边界的行内字幕时间片段。
正文中的日期、时间与版本号默认保留；**不产生任何模型分数**。

- 算子编号：05（端口 8005，`id = layout_noise_cleanup`）
- 输入：`04_html_cleaned.jsonl`（来自算子 4）
- 输出：`05_layout_cleaned.jsonl`、`05_layout_rejects.jsonl`、`artifacts/stage_05_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）
- 正式接口：`POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`（**不含** `/downloadFile`）
- 无血缘改造：`/predict` 成功时 `data` 固定返回 `outputFile`、`rejectFile`、`stageZip`、`storageUri` 四个字段

## 1. 功能边界

| 会做 | 不会做 |
| --- | --- |
| 删除**整行**字幕时间戳（含 `-->` 的时间行恒为字幕噪声） | 不做空白归一、HTML 解码（属算子 3/4） |
| 删除**整行**页码（中文、英文、`- 7 -`、`3/10`、裸数字） | 不改写 `id`、`source_lang`、`target_lang` 与既有 metadata |
| 删除跨记录高频重复且位于记录首/尾区域的行 | 不生成/转发任何模型分数（无 score/confidence 字段） |
| 按用户配置删除自定义页眉页脚整行模式 | 不删除正文中间的时间、日期、版本号 |
| 删除行内 `00:01:02.500 --> 00:01:05.000` 片段（`-->` 边界明确，默认开启） | 行内页码默认**不**删除（`remove_inline_page_markers: false`） |
| 删除纯空白行（`remove_blank_lines`） | 不合并行、不重排顺序、不做去重 |

### 行区域定义（`head_lines` / `tail_lines`，默认各 2 行）

对每个字段按 `\r\n|\r|\n` 切分为逻辑行；`line_index < head_lines` 为首部，
`line_index >= total - tail_lines` 为尾部，其余为正文。首尾区域用于跨记录重复行识别，
以及 `boundary_only=true` 模式的生效范围。

> 上游对接提示：若算子 4 以 `whitespace_mode: global` 折叠了换行，则字段退化为单行，
> 此时只有「整字段等于某模式」与「行内 `-->` 时间戳 / 行内页码」仍然生效。
> 需要行级页眉页脚识别时，请让算子 4 使用保留换行的模式。

## 2. 输入输出字段

**输入记录**（上游 `04_html_cleaned.jsonl`）：

```json
{"id": "layout-0001", "source": "平行语料清洗规范\n本文介绍通用粗清洗流程。\n第 1 页",
 "target": "Corpus Cleaning Standard\nThis document describes the general cleaning pipeline.\nPage 1",
 "source_lang": "zh", "target_lang": "en", "metadata": {}}
```

**输出记录**：`id/source/target/source_lang/target_lang/metadata` 结构不变，
在 `metadata` 中追加 `layout_cleaning`、`removed_patterns`、`cleaning_status`：

```json
{
  "id": "layout-0001",
  "source": "本文介绍通用粗清洗流程。",
  "target": "This document describes the general cleaning pipeline.",
  "source_lang": "zh", "target_lang": "en",
  "metadata": {
    "layout_cleaning": {
      "removed_lines": 4, "inline_removed": 0, "removed_chars": 43,
      "removed_by_kind": {"repeated_line": 2, "page_number": 2},
      "fields_cleaned": ["source", "target"], "uncertain_matches": 0
    },
    "removed_patterns": [
      {"field": "source", "kind": "repeated_line", "pattern": "repeated_line", "scope": "line",
       "line_index": 0, "region": "head", "hits": 1, "removed_chars": 8},
      {"field": "source", "kind": "page_number", "pattern": "page_number_cn", "scope": "line",
       "line_index": 2, "region": "tail", "hits": 1, "removed_chars": 5}
    ],
    "cleaning_status": "cleaned"
  }
}
```

- `cleaning_status`：`unchanged` | `cleaned` | `waiting_manual`
- 删除证据**只**含模式名、类别、命中次数（`hits`）、位置摘要（`line_index`/`region`/`scope`）
  与删除字符数，**绝不写入原文**。
- 异常清单 `05_layout_rejects.jsonl` 每行：
  `{line, record_id, record, reject_type: "hard"|"uncertain", reasons[], removed_patterns?, uncertain_matches?}`。
  `reject_type=hard` 覆盖损坏 JSONL、非对象记录、缺字段、类型错误、空字段；
  `reject_type=uncertain` 为待人工记录，`record` 为**未经修改的原文**。

## 3. 配置（`configs/config.yaml`）

总开关：`remove_timestamps`、`remove_page_numbers`、`remove_header_footer`、
`remove_repeated_lines`、`remove_blank_lines`、`remove_inline_timestamps`、
`remove_inline_page_markers`。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `input_encodings` | `[utf-8]` | 严格解码，逐个尝试，全失败即 `failed` |
| `text_fields` | `[source, target]` | 必须同时包含 source 与 target |
| `timestamp_patterns` | 4 条 | 整行字幕时间戳（`fullmatch`，需覆盖整行） |
| `page_number_patterns` | 5 条 | 整行页码；`page_number_bare`（裸数字）为 `boundary_only` |
| `header_footer_patterns` | 3 条 | 用户自定义页眉页脚（版权/URL/机密），均为 `boundary_only` |
| `inline_timestamp_patterns` | 1 条 | 行内 `-->` 时间区间 |
| `inline_page_patterns` | 2 条 | 行内页码（默认关闭） |
| `min_repeat_ratio` | `0.5` | 重复行命中记录占比阈值，**含等于**；须满足 `0 < r <= 1` |
| `min_repeat_count` | `2` | 重复行最少跨记录次数 |
| `head_lines` / `tail_lines` | `2` / `2` | 首/尾区域行数 |
| `max_tracked_lines` | `200000` | 频率表上限，超出即停止新增键并在统计中标记截断 |
| `min/max_repeat_line_chars` | `2` / `120` | 参与重复行判定的行长范围 |
| `max_whole_line_chars` | `120` | 命中整行模式但过长 → 视为可疑，转 `waiting_manual` |
| `protect_whole_field` | `true` | 清理后整字段为空 → 保留原文并转 `waiting_manual` |
| `uncertain_action` | `waiting_manual` | 见 §5 |

模式写法支持两种：对象 `{name, pattern, boundary_only}`，或字符串简写（自动命名 `<prefix>_<index>`）。
`boundary_only=true` 只在首尾区域删除；命中正文中间时**保留且不报不确定**（属于正常正文）。

配置错误（未知字段、`min_repeat_ratio <= 0` 或 `> 1`、`min_repeat_count < 2`、
非法正则、`text_fields` 缺 source/target、空 `input_encodings`、非法 `uncertain_action` 等）
一律拒绝，`status=failed`。

## 4. 算法/模型后端

无模型、无外部服务。全部为确定性正则与频率统计，可手工复算：

1. 第一遍流式扫描（`remove_repeated_lines=true` 时）：统计**首尾区域**内
   `(field, 规范化行文本)` 出现在多少条记录中，得到高频重复行集合。
2. 第二遍流式处理：逐行按「时间戳 → 页码 → 自定义页眉页脚 → 重复行 → 行内片段」顺序判定，
   写入输出与异常清单，同时累计真实统计。

## 5. 状态与降级行为

| 状态 | 触发条件 |
| --- | --- |
| `success` | 有有效记录，且无硬异常、无待人工 |
| `partial` | 存在硬异常（损坏行/缺字段/类型错误/空字段） |
| `waiting_manual` | 存在不确定记录（`pattern_line_too_long` 或 `whole_field_would_be_empty`） |
| `failed` | 配置错误、输入为空、编码无法解码、输入路径等于输出路径、读取失败 |

不确定处理由 `uncertain_action` 决定（**都保留原文，绝不静默删除**）：

- `waiting_manual`（默认）：记录**整体**不修改并写入 `05_layout_rejects.jsonl`
  （`reject_type=uncertain`），候选删除记入 `removed_patterns` 证据。
- `keep_original`：记录保留在输出文件、`cleaning_status=waiting_manual`，
  安全删除照常生效，不确定命中写入 `uncertain_matches`。

不变量：`passed + rejected == total`（每条非空输入行恰好进入输出或异常清单之一）。

已知行为：仅关闭 `remove_timestamps` 时，行内规则仍会把「纯时间行」清空删除；
要完整保留字幕时间行需同时关闭 `remove_inline_timestamps`。

## 6. 启动、调用与规范参数映射

```bash
pip install -r requirements.txt
python -m uvicorn inference:app --host 0.0.0.0 --port 8005   # 或 python inference.py

# 阶段首算子（不传 inputFile，使用上传文件）
curl -X POST "http://127.0.0.1:8005/uploadFile?uniqueId=task1_file1" -F "file=@dataset/sample/demo.jsonl"
curl -X POST http://127.0.0.1:8005/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task1_file1","operatorId":"05","isLastOperator":false}'

# 阶段内后续算子（inputFile 传上一算子中间响应的 data.outputFile 路径）
curl -X POST http://127.0.0.1:8005/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task1_file1","operatorId":"05","isLastOperator":true,"inputFile":"output/task1_file1/05_layout_cleaned.jsonl","params":{"uncertain_action":"keep_original"}}'

curl -X POST "http://127.0.0.1:8005/removeFile?uniqueId=task1_file1"
```

`/predict` 请求体字段：`uniqueId`（必传）、`isLastOperator`（必传，布尔）、`inputFile`（阶段内后续算子必传；
本算子也兼容阶段首算子不传）、`operatorId`（选传，若传须为 `"05"`）、`params`（选传，覆盖配置，优先级高于
兼容字段 `config`）。

### 规范参数映射（规范文档 4.4）

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_页眉页脚正则` | `header_footer_patterns` | 整行正则模式列表（`{name, pattern, boundary_only}` 或字符串简写），默认内置版权/URL/机密 3 条，均 `boundary_only`（仅首尾区域生效） |
| `param_时间戳格式` | `timestamp_patterns` | 字幕时间戳按**正则整行匹配**（`fullmatch`）配置，非格式串；默认 4 条，支持 `00:00:00,000 --> 00:00:01,000`、`[00:00:00]`、`00:00:00`、`第 N 页` 等常见样式，可通过 `params` 覆盖 |

CLI：

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

## 7. 响应示例（无血缘改造后）

`/predict` 成功响应 `data` 固定 4 个字段，**不再返回 `lineageFile`**：

```json
{"code":0,"msg":"success","data":{
  "outputFile":"<中间算子=输出文件路径；阶段末=输出文件内容>",
  "rejectFile":".../05_layout_rejects.jsonl",
  "stageZip":".../artifacts/stage_05_task1_file1.zip",
  "storageUri":"local-mirror://task1_file1/stage_05_task1_file1.zip"}}
```

- 中间节点（`isLastOperator=false`）：`data.outputFile` 是**下一算子要传的临时路径**（`05_layout_cleaned.jsonl`）。
- 阶段末（`isLastOperator=true`）：`data.outputFile` 是**最终文件内容**字符串（不是路径）。
- `stageZip` 由 `utils/artifacts.finalize_stage_artifacts` 在成功/部分成功/`waiting_manual` 返回前生成，
  内含主输出、异常清单与 `run.log`；`status=failed` 时不生成（`stageZip`/`storageUri` 为 `null`）。
- `storageUri`：设置 `OBJECT_STORE_DIR` 环境变量时返回归档文件 `file://` 地址，否则返回
  `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- 失败响应仍为 `{"code":1,"msg":"...","data":null}`，不产生阶段产物与任何 lineage 文件。

## 8. 资源需求

纯 CPU/IO，无 GPU。内存与文件大小基本无关（两遍流式，仅保留频率表，
上限由 `max_tracked_lines` 控制）。单文件 9 条样例耗时 < 1s；
磁盘占用 ≈ 输入大小 + 异常清单 + 阶段产物 zip（约等于主输出 + 异常清单 + 日志）。

## 9. 已知限制

- 行级识别依赖字段内存在换行符（见 §1 上游对接提示）。
- `page_number_bare` 会命中首尾区域的纯数字行（如「42」）；正文中间的同型行保留。
- 若整条记录的某个字段清空，按 `protect_whole_field` 转人工而非删除（如整条只有「第 6 页」）。
- 重复行判定依赖记录数与占比，记录数极少时（如 1 条）`min_repeat_count=2` 使其不生效。
- 未做亿级文件压测；未与真实上游算子 4 产物联调。

## 10. 真实测试结果（本机 Windows / Python 3.9.7）

以下为【无血缘改造 2026-10-19】后的实跑结果：

```
D:\python\python.exe -m pytest tests -q
→ 52 passed in 2.90s      # tests/test_operator.py 46 例 + tests/test_api.py 6 例

D:\python\python.exe processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
→ status=waiting_manual
  stageZip=output_smoke\smoke_cli\artifacts\stage_05_smoke_cli.zip
  storageUri=local-mirror://smoke_cli/stage_05_smoke_cli.zip
  任务目录：05_layout_cleaned.jsonl、05_layout_rejects.jsonl、run.log、artifacts/stage_05_smoke_cli.zip
  无 lineage.json
  total=9 passed=8 rejected=1 hard_rejected=0 uncertain_records=1 records_changed=6
  lines_removed=22 inline_removed=0 chars_removed=266
  removed_by_kind: timestamp=2 page_number=10 repeated_line=10 blank_line=0 header_footer=0
  repeated_lines_detected=2 repeated_line_candidates=40 frequency_scan_records=9 truncated=false
  reason:uncertain:whole_field_would_be_empty=2 encoding=utf-8
  核对：输出 8 行 = passed；异常 1 行 = rejected；passed + rejected = 9 = total；
        chars_removed 266 = 页眉 160（8×5 + 24×5）+ 页码 48（5×4 + 6×4 + 2×2）+ 时间戳 58（29×2）✓

D:\python\python.exe -m json.tool map.json      → exit 0（端口 8005）
D:\python\python.exe -m compileall -q .         → exit 0
docker build                                    → 未运行（不声称镜像已构建）
```

API 契约冒烟（TestClient）：`/uploadFile` 返回 `{"code":0,"msg":"success"}`；
中间调用 `data` 为 `{outputFile,rejectFile,stageZip,storageUri}` 且各路径文件存在；
阶段末 `outputFile` 为文件内容（8 行）；`operatorId` 不匹配、非法 `params`、缺失/错误
`isLastOperator` 均返回结构化失败；`params={"remove_timestamps":false,"remove_inline_timestamps":false}`
覆盖生效（输出保留 `-->` 时间行）；`/removeFile` 后任务目录不存在；`/downloadFile` 为 404。

历史 TDD 记录（改造前）：先写测试 → 首轮 `ModuleNotFoundError: No module named 'processor'`（2 collection errors）
→ 实现后 50 passed / 1 failed（真实缺陷：测试误判「仅关 `remove_timestamps` 即可保留时间行」，
实际行内规则仍会清空纯时间行）→ 修正用例并补充该行为的显式测试 → 52 passed 全绿。
