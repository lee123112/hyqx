# 算子 18「标注局部对局部」说明书

- 正式编号：**18**
- 算子 ID：`phrase_region_annotation`
- 阶段：**S3 数据加工**
- 优先级：**P1**，类型：小模型 + 人工确认
- 默认状态：**关闭**（`enabled: false`）
- 上游：#17 局部对整体（提供候选框）、文本预处理产线（提供分词）
- 下游：#19 / #22（消费已确认的细粒度标注）、#25（人工确认）、#21（样本隔离库）

---

## 1. 业务目标

表1 #18 要求把「整体对齐」细化到**词—局部区域**级别：一句话里的某个词，究竟对应画面里的哪一块。
本算子不做端到端生成，只做**候选推荐 + 阈值分档**：

1. 用同一套 CLIP-ViT/B-32 给「词」和「候选框裁图」打分；
2. 达到阈值的**预填**成关系，省掉人工从零标注；
3. 低于阈值的**标为待人工**，交由人工判断；
4. 一切都必须经人工确认后才能交付——本算子自己不交付任何关系。

---

## 2. 口径（表1 #18 逐条落地）

| 表1 / 研发测原文 | 落地实现 | 证据字段 |
| --- | --- | --- |
| 总开关关闭时不做 | `enabled=false` 时只写透传文件，不加载模型、不搬媒体、不产关系 | `processing.18.status=1`、`process_status=skipped`、`statistics.skipped_records` |
| 打开后优先调用预训练文本产线的中文分词 | 读 `text.tokens` / `tokens` / `processing.04.tokens` | `tokenizer_source=pretrained` |
| 没有则用结巴分词并写进日志 | 回退 `jieba.lcut`，日志与 `processing["18"]` 都记录实际后端 | `tokenizer_source=jieba`、`tokenizer_backend=jieba` |
| 每个词用已加载的 CLIP 和各候选框裁图算分 | 逐框裁剪 → 逐词/逐框编码 → 余弦；词取「词本身 vs 所在名词短语」最高分（词仍是匹配单元） | `crops_scored`、`relations[].clip_score`、`matched_text`、`variant` |
| 达到页面上的最低分才预填一条 | `clip_score >= min_clip_score`（含等于） | `confirmation_method=auto_prefill`、`statistics.relations_prefilled` |
| 低于最低分的词标为待人工，不预填 | 关系照常落盘但 `process_status=pending_manual`（`status=1`） | `confirmation_method=pending_manual_review` |
| 保存人确认的**一对一**关系 | 按分数降序贪心匹配，一个框只被一个词占用；可先按框的检测类别校准（`class_calibration: prior`，默认；另有 `znorm` / `none`），校准分只用于指派 | `utils.scoring.assign_one_to_one`、`relations[].assignment_score` |
| 输出包含词片段、候选框ID、CLIP分数、confirmation_method | `phrase_box_relations[]` 四个字段齐备 | `dataset/DATA_SCHEMA.md` 2.3 |
| 文件名不能当词 | 文件名/主干/扩展名/主干片段进入禁止集合；描述整体等于文件名视为无效描述 | `description_is_filename`、`tokens_dropped` |
| 词要指向局部区域 | 停用词/数词/量词过滤；有 `jieba.posseg` 时按词性剔除代词、介词、连词、助词等；剔除原因分类计数 | `tokens_dropped_detail` |
| 词的上下文 | 相邻「修饰语 + 名词」合并为名词短语，作为该词的匹配变体；关 `merge_phrases` 即只用词本身 | `phrases_total`、`phrases_used`、`token_variants` |
| 裁图质量 | 可选框外扩边（0–0.25）与框外背景抑制（`mean`/`gray`）；772 条实测不采用为默认 | `crop_padding_ratio`、`background_suppression` |
| 按类别校准 | 节点与统计输出每类关系数/预填数/最高分与该类原始分均值/标准差；`class_calibration=prior`（默认）时用 `s + w×cos(匹配文本, 类别名)` 参与指派，`znorm` 时用 `(s-μ类)/σ类`；**0.25 阈值与分流规则不变** | `class_profile`、`class_calibration`、`class_prior_weight`、`relations[].assignment_score` |
| 没有框时给人工空白列表 | 无可用框 → 流程状态，`relations = []` | `label=流程-无候选框` |
| 没有人确认不能交付 | `human_verified`/`deliverable` 恒 `false`，`deliverable_records` 恒 0 | 记录级 + 统计级 |
| 不新建多语言模型 | 只复用 CLIP 与中文分词，无新增模型 | `model`、`requirements-model.txt` |
| 工具开启后没有对应关系 → 样本隔离库 | 有候选框但分词后无有效词 → `样本-语义不通` | `18_sample_quarantine.jsonl` |
| 模型/服务不可用只能显式失败或显式降级 | `allow_degraded` 控制；降级时分数与模型均为 `null` | `status=failed` / `status=degraded` |
| 不得用固定/随机分数 | 代码路径中不存在任何常量分或随机分；无 encoder 直接降级/失败 | `tests/test_operator.py::test_model_unavailable_*` |

---

## 3. 状态机

```
                       ┌──────────────────────────────┐
                       │ enabled == false             │
                       │ → skip（透传，不产关系）      │
                       └──────────────────────────────┘
                                     │ enabled == true
                                     ▼
        缺描述? ──yes──▶ pending(流程-待补描述)
                                     │ no
        缺图像引用? ──yes──▶ pending(流程-无图像)
                                     │ no
        图像引用非法/文件缺失? ──yes──▶ origin(原始隔离区)
                                     │ no
        无可用候选框? ──yes──▶ pending(流程-无候选框，空白人工列表)
                                     │ no
        分词后无有效词? ──yes──▶ isolate(样本-语义不通 → 样本隔离库)
                                     │ no
        模型不可用? ──yes──┬─ allow_degraded=false ─▶ 整次运行 failed（不写分数）
                           └─ allow_degraded=true  ─▶ pending(degraded，分数=null)
                                     │ no
        全部裁图失败? ──yes──▶ origin(原始文件-损坏)
                                     │ no
                       打分 → 一对一匹配 → 阈值分档
                                     │
              ┌──────────────────────┴──────────────────────┐
        ≥1 条达标                              0 条达标
   prefill（样本-评分：待人工）           pending（流程-待人工确认）
        → 人工确认队列                          → 人工确认队列
```

去向：**人工确认队列**（#25）、**样本隔离库**（#21）、**原始隔离区**（工程侧排障）。
关闭路线不进任何队列，只留样本池。

---

## 4. 关键设计取舍（含待决策）

1. **「开启后没有对应关系」的判据**
   表1 只说「没有对应关系 → 样本-语义不通 → 隔离」，研发测同时要求「无框时给人工空白列表」
   与「低于阈值标为待人工」。三者并存时的唯一自洽解释是：
   **有候选框、有描述，但分词后不存在任何有效词**（描述就是文件名、纯标点等）才算「没有对应关系」。
   其余（无框、缺描述、低分）都是流程状态，留产线不隔离。
   → 若业务侧认为「全部词都低于阈值」也应隔离，请主 agent 明确，改动点仅在
   `utils/phrase_region.evaluate_record` 的第 7 步分档。

2. **候选框坐标系不猜测**
   未显式声明 `coord`/`normalized` 时按**像素**解释，避免把小像素框（如 `[0,0,4,4]`）
   误判成归一化坐标导致静默算错。

3. **非法框丢弃而非整条拒绝**
   单个坏框不应毁掉整条样本，但也不能静默忽略：丢弃并计入 `boxes_invalid`
   （统计 + 节点字段），全部非法时按「无可用框」处理。

4. **样本隔离条目附带原始记录**
   为了能让 #21 直接重跑，`18_sample_quarantine.jsonl` 的条目带 `record` 字段；
   超过 32 KB 会截断并标记 `_truncated`。原始隔离区**不带**原文，只留标识与证据。

5. **一对一 vs 一对多**
   表1 明确要求「一对一」。因此词多于框时，多余的词不强行分配，
   保留 `box_id=null`、`reason=no_available_region`，并把 `best_score` 留给人工参考。

6. **阈值只允许 0.20–0.40**
   与表1 口径一致；超出范围直接 `ConfigError`（HTTP 400 / CLI 退出码 2），
   不允许用 demo 的 0.50/0.80 替代。

---

## 5. 运维要点

- **任务隔离**：输出只在 `OUTPUT_DIR/<uniqueId>/runs/runNNN/`；同一 `uniqueId` 重跑自动新建
  `runNNN+1`，不覆盖历史；并发由 `.lock` 目录互斥（第二个请求返回 409）。
- **媒体搬运**：开启路线下，被引用的本地图像会复制到 `<run_dir>/media/`，
  并在 `manifest.json` / `mediaArtifacts` 登记，保证交付包自洽。
- **原子写入**：所有 JSONL 与 JSON 都先写 `.swp` 再 `os.replace`；
  即使输入输出同路径也不会截断源文件。
- **密码/凭据**：日志只写动作与计数，绝不写 Token/Cookie/完整原文/完整堆栈。

---

## 6. 验收清单

| 项 | 结果 |
| --- | --- |
| `python -m pytest tests/ -q` | 174 passed |
| 关闭路线 CLI smoke | 10 条原样放行，0 条关系，2 条拒绝 |
| 开启 + 显式降级 CLI smoke | 9 条：6 待人工 / 1 隔离 / 2 原始隔离，0 分数，守恒通过 |
| 开启 + 严格模式 CLI smoke | 显式失败，退出码 3，消息 `clip_weights_missing` |
| `/uploadFile` `/predict` `/removeFile` 三接口 | 27/27 项 HTTP smoke 通过 |
| `/downloadFile` | 未注册（404），`/health` 显式声明 |
| `python -m compileall -q .` | 通过 |
| `python -m json.tool map.json` | 通过 |
| `docker build` | **未运行**（见 `DEVELOPMENT_STATUS.md`） |
