# 算子说明书 · 评分音文语义一致性

| 项 | 内容 |
| --- | --- |
| 算子名称 | 评分音文语义一致性 |
| 正式编号 | **#13**（表1 v4.2 第 13 行） |
| 目录 | `demo_10.6/13_评分音文语义一致性` |
| 所属阶段 | **S3 数据加工** |
| 节点位置 | 阶段内**中间节点**（阶段末由工程侧以 `isLastOperator=true` 决定） |
| 算子 id | `audio_text_scoring` |
| 版本 | `v2.0.0` |
| 端口 | `8113` |
| 可跳过 | 是（表1「默认关闭」，`enabled=false`） |
| 优先级 | 普通（不阻断其余单模态算子） |

## 1. 职责

对音文候选配对复用上游 Whisper-base 转写，计算**转写-描述文本一致度**（字符级 CER/编辑距离比例），据此判定样本是否通过，并把通过/不通过分别交给 #19 / #21。

## 2. 表1 冻结参数

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| 启用开关 | 关闭 | 布尔 | 关闭时不加载模型、不写音文文件、不隔离 |
| 编辑距离比例上限 | 0.30 | 0.10–0.50 | 转写 vs 人工描述 |
| 繁→简归一化 | 开启 | 布尔 | 比较前先把繁体字逐字符转简体；可配关闭 |
| 评分方法 | 字符级 CER | 固定 | 繁→简归一化后做 NFKC、小写、去空白/标点/符号再计算 |
| 转写模型 | 上游 `whisper-base` | 复用 | 本算子不重新加载 |
| 采样率 | 16000 Hz | 固定 | 非 16k 先重采样 |
| 转写缺失行为 | 待处理 | `pending` | 不按低分隔离，不伪造分数 |

判定式：`通过 ⇔ CER ≤ edit_distance_ratio_max`。CER 计算前两侧文本都会先做繁体→简体字符级归一化（默认开启），以消除 ASR 繁体输出与简体描述之间的书写体系差异；归一化不改变阈值语义。Wav2Vec2/BERT 裸余弦只允许通过显式注入 scorer 做实验诊断，不能参与判定、入池或隔离。

繁→简映射表随算子包离线自带：优先 `utils/data/TSCharacters.txt`（OpenCC，Apache-2.0），
其次 `$MODEL_ROOT/opencc/TSCharacters.txt`，最后回落到 `utils/t2s.py` 内置高频映射；
运行期绝不联网。实际来源与条目数写入节点 `t2s_dictionary_source` / `t2s_mapping_entries`，
归一化前后口径分别写入 `transcript_normalized` 与 `transcript_normalized_before_t2s`。

## 3. 输入

- 主输入：#11 `11_candidate_pairs.jsonl` 的音文候选配对；
- 兼容输入：单记录形态（同一记录内同时含音频与对齐描述）；
- 关键字段：`audio.path` / `audio.segments[]`、`audio.transcript`、`text.content`；
- 媒体路径必须为素材包内相对路径。

详细解析顺序见 `dataset/DATA_SCHEMA.md §1.2`。

## 4. 输出

| 产物 | 去向 |
| --- | --- |
| `13_audio_text_scores.jsonl`（`enabled=true`） | 通过结果 → **#19** |
| `13_passthrough.jsonl`（`enabled=false`） | 原样透传 |
| `13_sample_quarantine.jsonl` | 不通过样本 → **#21** |
| `13_pending.jsonl` | 流程待处理（不隔离） |
| `13_rejects.jsonl` | 原始隔离区 + 坏行 |

记录内结论位于 `processing["13"]`；台账条目携带 `audio_text` 与完整 `record`。
繁→简归一化新增审计字段：`traditional_to_simplified`、`transcript_normalized_before_t2s`、
`description_normalized_before_t2s`、`traditional_chars_converted`、`t2s_dictionary_source`、
`t2s_mapping_entries`；原有输出字段保持不变。

## 5. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/uploadFile?uniqueId=` | 上传 zip 素材包或单文件 |
| POST | `/predict` | 双协议：`multipart/form-data` 走形态一（单文件流，`?detection=`）；`application/json` 走形态二（阶段文件传递） |
| POST | `/removeFile?uniqueId=` | 幂等清理任务目录 |

`isLastOperator` 仍必传（严格布尔），用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile`（`crossmodal-files-v1`）+ `result`（业务摘要），不再用 `false` 只回血缘对象。

- 形态一返回 `status`（int：判定型 1 通过/0 不通过，`judge_mode=system` 时按处理型 1 成功/0 失败）+ `process_status`/`uniqueId`/`pair_id`/`cer`/`wer`/`decision`/`decision_source`/`model_version`/`threshold`/`results`；
- 形态二返回 `{code,msg,data{operator,status,outputFile,result}}`，失败 `data={}`。

无 `/downloadFile`。外层响应固定 `{"code","msg","data"}`。

## 6. 依赖

- 运行：`fastapi`、`uvicorn`、`pydantic>=2`、`PyYAML`、`python-multipart`、`numpy`；
- 可选：`scipy`（非 PCM WAV 兜底解码）；
- 生产路线不需要本地基座权重；旧版模型依赖仅用于离线诊断适配。

## 7. 资源

规则评分不需要本地基座权重；资源主要用于音频读取和 JSONL 处理，GPU 不是生产必需项。

## 8. 质量标签与去向

| 场景 | 标签 | 去向 |
| --- | --- | --- |
| 通过 | `样本-评分：通过` | #19 |
| 不通过 | `样本-语义不通` | 样本隔离库（#21） |
| 缺转写/缺描述/无音文对 | `流程-待转写`/`流程-待补描述`/`流程-无音文对` | 产线待处理 |
| 路线关闭 | `流程-音文路线未启用` | 原样透传 |
| 原始文件问题 | `原始文件-无法解析/损坏/空/缺字段/编码异常` | 原始隔离区 |

## 9. 与其他算子的关系

- **上游**：#11 提供音文候选配对；#08 提供转写；#09 提供对齐描述；
- **下游**：通过 → #19；不通过 → #21；
- **同源关系**：本算子由旧目录 `demo/13_多语言文本图像对齐` 拆出的**音文路线**，
  旧目录保持不动，也不得冒充表1 #13；
- **跨算子约束**：图文评分（#12）与本算子同时关闭时由任务创建侧业务拦截，本算子只负责自身开关。
