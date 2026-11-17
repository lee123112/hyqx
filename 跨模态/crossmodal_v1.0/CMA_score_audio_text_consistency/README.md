# 算子 13：评分音文语义一致性（表 1 #13）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

本算子消费 #11 的音文候选配对，复用 #10 已生成的 Whisper-base 转写，与 #9 的对齐描述做可解释的文本一致性检查。

生产门禁只有一项：对两段文本先做**繁体→简体字符级归一化**（默认开启，可配关闭），再 NFKC、小写、去空白/标点/符号，计算字符级 Levenshtein 编辑距离比例（CER）。`CER <= edit_distance_ratio_max` 才通过。默认阈值为 `0.30`，允许任务配置为 `0.10–0.50`。`wer` 作为解释字段输出，但不另设门槛。

繁→简归一化用于消除 ASR 转写偶发繁体输出（如「中心六區中間…」）与简体对齐描述之间的**书写体系差异**；映射表随算子包离线自带（OpenCC `TSCharacters`，Apache-2.0），运行时绝不联网。它只做逐字符转换，不做词组消歧，也不改变 `edit_distance_ratio_max` 的语义。

Wav2Vec2/BERT 裸余弦不再是本算子的生产依赖、入池条件或隔离依据。旧版模型适配代码仍保留，以便离线实验通过显式注入 `scorer` 记录诊断值；诊断值写入 `raw_cosine_similarity`，不会改变 `passed`、台账去向或候选池资格。

## 功能边界

- 路线关闭：不加载模型、不写 `13_audio_text_scores.jsonl`、不做样本评分隔离；记录按原样写入 `13_passthrough.jsonl`。
- 路线开启：缺转写、缺描述或无音频对进入 `13_pending.jsonl`，等待补齐/人工处理，不按低分隔离。
- 音频路径非法、缺失、无法解码或整段无声进入原始问题台账 `13_rejects.jsonl`。
- CER 超过阈值进入 `13_sample_quarantine.jsonl`；只有 `passed=true` 且 `candidate_pool_eligible=true` 的记录写入主产物，交给下游候选池。

## 上游文本接入（2026-11-09 联调修复）

优先使用上游 ASR。仅可信 AISHELL-3 数据允许以 `metadata.official_text` 补充整段转写：
`metadata.dataset_id` 必须为 `kaggle_aishell3_01`、`transcription_producer` 含 `AISHELL-3`、
`sample_id` 含 `AISHELL-3_`，同时存在非空官方文本和整段 `audio.path`。
当 `transcription_gate.direct_scoring_allowed=false` 时，只能替代
`transcription_disabled` / `master_switch_off`；真正失败、待复核及未知原因仍阻止评分。
官方整段文本与整段音频配对，不会误取第一段 WAV；已有段落 ASR 仍配对应段落音频。

M09 的根字段 `description` 优先于旧 `text.content`，`description_source` 留作来源审计；
兼容 M11 的 `left.record` / `right.record` 和旧字符串 `text`。
评分节点追加 `transcript_source_type`、`transcript_producer`、`transcript_scope`、
`transcript_upstream_gate_reason`、`description_origin`，不改上游转写门禁。

本次只接通输入与审计；继续使用语言/转写可靠性门、繁简与 ITN 归一、严格 CER 与同音容错
比例的较小值及当前 `0.30` 阈值。拼音表启动硬依赖、运行期显式不适用和诊断 checkpoint
兼容实现保持不变；缺文本或无音频仍无分数、`passed=null`，不能作为评分通过证据。

## 配置

配置来源为 `configs/config.yaml`，请求 `config` 只允许覆盖业务参数：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 音文路线开关 |
| `edit_distance_ratio_max` | `0.30` | CER 上限，范围 `0.10–0.50` |
| `traditional_to_simplified` | `true` | 比较前先做繁体→简体字符级归一化（默认开启；关闭即恢复纯 NFKC 口径） |
| `inverse_text_normalization` | `true` | 比较前把中文数字/百分比/单位表达归一为阿拉伯数字（`utils/itn.py`，纯规则离线） |
| `language_gate` | `true` | 描述侧无中文字符时判 `not_applicable`（`language_not_applicable`），不隔离 |
| `transcript_reliability_gate` | `true` | 解码重复循环或语速超物理上限的转写判 `not_applicable`（`transcript_unreliable`），不隔离 |
| `max_chars_per_second` | `30.0` | 语速物理上限（字符/秒），范围 `15–60`，只用于识别 ASR 故障 |
| `homophone_tolerance` | `true` | 同音字替换不计编辑距离（阈值与分母口径不变）；开启时拼音表是**硬依赖**，缺失则整次运行启动前失败（`ConfigError`），不会静默按严格字符 CER 判定 |

音频截断和文本长度属于部署默认值，不在生产配置页暴露。`cosine_threshold`、Wav2Vec2/BERT 路径和 Whisper 辅助层同样不属于生产配置页。旧 YAML 中的相关字段仅作读取兼容，绝不参与生产判定。

## 繁→简文本归一化

- 目标：ASR 转写偶发繁体字（如「區間/活躍/板塊」）时，避免把繁体/简体书写差异误算成语义不一致；两侧文本都会先转简体再算编辑距离。
- 词典位置与解析顺序（全部离线）：①算子包内 `utils/data/TSCharacters.txt`（随包交付，OpenCC `TSCharacters`，4148 条）→ ②`$MODEL_ROOT/opencc/TSCharacters.txt`（部署目录）→ ③`utils/t2s.py` 内置高频兜底映射（638 对，仅在词典缺失时使用）。文件损坏或条目数 < 100 会被跳过并继续下一来源。
- 关闭方式：`configs/config.yaml` 设 `traditional_to_simplified: false`，或 `/predict` 的 `config` 传同名布尔键（形态一表单同样支持）。
- 审计字段：每条评分节点新增 `traditional_to_simplified`、`transcript_normalized_before_t2s`、`description_normalized_before_t2s`、`traditional_chars_converted`、`t2s_dictionary_source`、`t2s_mapping_entries`；原有 `transcript_normalized` / `description_normalized` 仍是最终比较口径。
- 英文/数字/全角字符与已经简体的文本行为不变（转换表只命中繁体字）；详见 `tests/test_traditional.py` 与计算说明 `utils/data/README.md`。

## CLI

```bash
# 默认关闭：只透传
python inference.py --input dataset/sample/demo.jsonl --unique-id demo_task --output-root output

# 开启规则型 CER 路线，不需要本地基座权重
python inference.py --input dataset/sample/demo_enabled.jsonl --unique-id demo_on \
  --output-root output --override '{"enabled": true}'
```

## HTTP

服务仍提供三个正式接口：`POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`。

`/predict` 为双协议：

- **形态一**：`POST /predict?detection=score_audio_text_consistency`（`multipart/form-data`，`file` 必传；可选表单标量 `uniqueId`、`enabled`、`edit_distance_ratio_max`、`traditional_to_simplified`、`judge_mode`），返回 JSON 首字段 int `status`（判定型 1 通过/0 不通过；`judge_mode=system` 时按处理型 1 成功/0 失败）。
- **形态二**：`POST /predict?operatorId=M13`（`application/json`，`{uniqueId, isLastOperator, inputFile?, config?}`），返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`。

`isLastOperator` 仍必传（严格布尔），用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile`（`crossmodal-files-v1` 文件内容对象）+ `result`（业务摘要），不再用 `false` 只回血缘对象。主入口在路线开启时为 `13_audio_text_scores.jsonl`，关闭时为 `13_passthrough.jsonl`。

形态二响应示例（`result.files` 为任务内相对路径）：

```json
{"code":0,"msg":"音文评分完成：通过 3 条，不通过 1 条","data":{
  "operator":"audio_text_scoring",
  "status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"13_audio_text_scores.jsonl",
    "files":[{"path":"13_audio_text_scores.jsonl","encoding":"utf-8","content":"…"}]},
  "result":{"runId":"run001","runDir":"audio_text_scoring/run001",
    "entrypoint":"13_audio_text_scores.jsonl",
    "files":{"outputFile":"audio_text_scoring/run001/13_audio_text_scores.jsonl",
             "rejectsFile":"audio_text_scoring/run001/13_rejects.jsonl",
             "sampleQuarantineFile":"audio_text_scoring/run001/13_sample_quarantine.jsonl",
             "pendingFile":"audio_text_scoring/run001/13_pending.jsonl"},
    "statistics":{"input_records":4,"passed_records":3,"sample_isolated":1,
                  "pending_records":0,"conservation_ok":true}}}}
```

形态一响应示例（记录级 `status` 为 int，`process_status` 为字符串；`results[]` 为逐音文对明细）：

```json
{"status":1,"process_status":"success","uniqueId":"task_001",
 "judge_mode":"operator","pair_id":"pair-01","cer":0.0909,"wer":0.0,
 "decision":"pass","decision_source":"character_edit_distance_ratio",
 "model_version":"whisper-base","threshold":0.3,
 "results":[{"pair_id":"pair-01","cer":0.0909,"wer":0.0,
   "edit_distance_ratio":0.0909,"decision":"pass",
   "decision_source":"character_edit_distance_ratio","threshold":0.3,
   "model_version":"whisper-base"}]}
```

失败时为 `{"status":0,"msg":"<原因>"}`；缺少 `file`、`detection` 不匹配、输入不是 JSON/JSONL 等均走此形态。

## 输出字段

每条通过或隔离的记录在 `processing["13"]` 中包含：`transcript_normalized`、`description_normalized`、`edit_distance`、`cer`、`wer`、`edit_distance_ratio`、`edit_distance_ratio_max`、`threshold_source`、`scoring_method`、`scoring_version`、`transcript_model`、`transcript_model_version`、`passed`、`reason`、`candidate_pool_eligible`。繁→简归一化另输出审计字段：`traditional_to_simplified`、`transcript_normalized_before_t2s`、`description_normalized_before_t2s`、`traditional_chars_converted`、`t2s_dictionary_source`、`t2s_mapping_entries`（均为新增字段，不删除/不改名原有字段）。模型未被本算子调用时，`cosine_similarity` 与模型字段保持 `null`。

判定口径相关的审计字段（2026-11-05 新增，均为追加，不删除/不改名原有字段）：

| 字段 | 含义 |
| --- | --- |
| `language_gate` / `language_applicable` / `description_cjk_chars` | 语言适用性门：描述侧无中文字符时判 `not_applicable`（`language_not_applicable`），不隔离 |
| `transcript_reliability_gate` / `transcript_reliable` / `transcript_chars_per_second` / `transcript_repetition` / `transcript_unreliable_reasons` | 转写可靠性门：解码循环（同串连续 ≥6 次）或语速超物理上限时判 `not_applicable`（`transcript_unreliable`），不隔离 |
| `homophone_tolerance` / `edit_distance_ratio_homophone` / `homophone_tolerated` / `pinyin_dictionary_source` / `pinyin_table_entries` | 同音容错：同音字替换不计编辑距离后的同一口径比例；判定取它与 `edit_distance_ratio` 的较小值与同一 `edit_distance_ratio_max` 比较，`reason=passed_homophone_tolerant` 表示容错实际生效 |
| `pinyin_table_unavailable` / `pinyin_table_error` | 同音容错开启但运行期拼音表不可用：严格 CER 超阈值的记录判 `not_applicable`（`pinyin_table_unavailable`，留产线、不隔离）；严格口径本就在阈值内的记录照常通过但同样带标记。正常运行不出现 |

`edit_distance_ratio` 始终是**严格字符口径**（不含同音豁免），便于审计与回归对照。

## 验证

```bash
python -m compileall -q .
python -m pytest tests/test_scoring.py tests/test_traditional.py -q
python inference.py --input dataset/sample/demo_enabled.jsonl --unique-id smoke_rule \
  --output-root output_smoke --override '{"enabled": true}'
```

`test_traditional.py` 覆盖：词典解析与优先级（包内 → `$MODEL_ROOT/opencc` → 内置兜底）、
开关口径（开启/关闭）、英文数字不变、真实第八轮繁体转写由隔离变通过的端到端回归。
