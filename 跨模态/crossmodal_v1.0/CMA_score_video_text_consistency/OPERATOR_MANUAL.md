# 算子说明书 · 评分视文语义一致性

| 项 | 内容 |
| --- | --- |
| 算子名称 | 评分视文语义一致性（登记表 C 列） |
| 正式编号 | **#14**（表1 v4.1 第 14 行） |
| 目录 | `CMA_score_video_text_consistency`（登记表 D 列；来源 `demo_10.6/14_评分视文语义一致性`） |
| 所属阶段 | **S3 数据加工**（阶段内 6/11） |
| 节点位置 | 阶段内**中间节点**（阶段末由工程侧以 `isLastOperator=true` 决定） |
| 算子 id | `video_text_scoring` |
| 版本 | `v1.0.0` |
| 端口 | `8114` |
| 可跳过 | 是（表1「默认关闭」，`enabled=false`） |
| 优先级 | 普通（不阻断其余单模态算子） |

## 1. 职责

复用图文评分（#12）的 **CLIP-ViT/B-32**，对**视频抽帧序列**与文本描述逐帧计算余弦相似度，
按语义段取均值后与阈值比较，判定样本是否通过，并把通过 / 不通过分别交给 #19 / #21。

- **整段视频合计最多取 8 帧**（`max_frames_per_segment`，范围 1–8）→ 逐帧编码 → 段分；
  单语义段时与旧行为等价；多语义段把帧预算分配到各段（总帧数 ≤ 上限）：
  段数 ≤ 上限时每段至少 1 帧、其余按候选帧数占比分配；段数 > 上限时按段均匀覆盖，
  未被覆盖的段 0 帧（段分记**无证据**、不因该段隔离，证据里注明）；
- 段分 = 该段被选中帧的余弦均值；**整段视频分 = 全部被选中帧的均值**（不再是段均值的均值）；
- 长视频按语义段**分别**计算，**任一段分低于阈值（配置默认 0.20）即整段不通过**，
  0 帧段不参与判定；
- 输出视文分数、段分、最低段分、通过结论与模型版本，以及
  `frames_used`（恒 ≤ `max_frames_per_segment`）等帧预算证据，交 #19 与 #16。

## 2. 表1 冻结参数

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `false` | 布尔 | 关闭时不加载模型、不写视文评分文件、不搬运抽帧、不隔离，样本原样透传 |
| `low_score_threshold` | `0.20` | 0.20–0.40 | 复用图文评分低分阈值；任一段段分低于该值 → 整段不通过 |
| `max_frames_per_segment` | `8` | 1–8 | **整段视频合计**最多取帧数（多段共享该预算，不再按段累计） |
| `max_text_chars` | `500` | 资源保护 | 送模型文本按字符数截断并记录 |
| `temporal_route_exclusion` | `true` | 布尔 | 带 `timeline`/`temporal_bindings`/`temporal_anchors` 的记录判 `not_applicable`（不评分不隔离，写 `14_not_applicable.jsonl`），时序结论交 #15；`false` 仅用于回归对比 |
| CLIP 架构 | `ViT-B-32` | 固定 | 仅接受等价别名（如 `CLIP-ViT/B-32`），不允许换更大模型 |
| `clip.library` | `auto` | `auto`/`open_clip`/`clip`/`transformers` | 依次尝试三库 |
| `clip.device` | `cpu` | `cpu`/`cuda`/`auto` | `cuda` 不可用时退回 `cpu` 并记录原因 |
| `clip.batch_size` | `8` | — | 段内帧批量 |
| `allow_degraded` | `false` | 布尔 | 依赖/权重缺失时：`false` 整个任务显式失败；`true` 显式降级 |

> 路线关闭（`enabled=false`）时，透传节点同样回填**本次配置生效值**
> （`low_score_threshold` / `max_frames_per_segment`），不硬编码默认阈值，
> 保证开关两态输出口径一致。

## 3. 输入

- 主输入：#11 `11_candidate_pairs.jsonl` 的视文候选配对；
- 关键字段：`video.path` / `video.segments[]` / `video.keyframes[]`、`text.content`；
- 媒体路径必须为素材包内相对路径。

详见 `dataset/DATA_SCHEMA.md`。

## 4. 输出

| 产物 | 去向 |
| --- | --- |
| `14_video_text_scores.jsonl`（`enabled=true`） | 通过结果 → **#19**（并供 **#16** 引用） |
| `14_passthrough.jsonl`（`enabled=false`） | 原样透传 |
| `14_sample_quarantine.jsonl` | 不通过样本 → **#21** |
| `14_not_applicable.jsonl` | 时序窗口（带 timeline）→ 不评分不隔离，留待 **#15** 判定 |
| `14_pending.jsonl` | 流程待处理（**不隔离**） |
| `14_rejects.jsonl` | 原始隔离区 + 坏行 |

记录内结论位于 `processing["14"]`。

## 5. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/uploadFile?uniqueId=` | 上传 zip 素材包或单文件 |
| POST | `/predict` | 双协议：`multipart/form-data` 走形态一（单文件流，`?detection=`）；`application/json` 走形态二（阶段文件传递） |
| POST | `/removeFile?uniqueId=` | 幂等清理任务目录 |
| GET | `/health` | 健康检查 |

`isLastOperator` 仍必传（严格布尔），用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile`（`crossmodal-files-v1`）+ `result`（业务摘要），不再用 `false` 只回血缘对象。

- 形态一返回 `status`（int：判定型 1 通过/0 过滤）+ `process_status`/`uniqueId`/`pair_id`/`segment_scores`/`decision`/`model_version`；
- 形态二返回 `{code,msg,data{operator,status,outputFile,result}}`，失败 `data={}`。

**无 `/downloadFile`**。外层响应固定 `{"code","msg","data"}`。

## 6. 依赖

- 运行：`fastapi`、`uvicorn`、`pydantic>=2`、`PyYAML`、`python-multipart`、`numpy`、`torch`、`Pillow`；
- 测试：`pytest`、`httpx`；
- 权重：默认 `<算子目录>/weight/clip/ViT-B-32.pt`（353,976,522 字节，随算子目录交付，
  见 `weight/README.md`；**运行期绝不联网下载**）；
- `$MODEL_ROOT` 仅作覆盖：不设时按 `<算子目录>/weight` 解析相对路径；指向空目录/缺权重时
  显式 `weights_not_found`（`allow_degraded` 决定失败还是降级），**不会**偷偷回退本地副本。

## 7. 资源

CPU 4 核以上 / 内存 ≥ 8 GB / 建议 GPU 12 GB（可选）；磁盘按抽帧结果计，权重约 0.34 GB。

## 8. 质量标签与去向

| 场景 | 标签 | 去向 |
| --- | --- | --- |
| 通过 | `样本-评分：通过` | #19（`DEST_NEXT = "#19"`） |
| 不通过（任一段低于阈值） | `样本-语义不通` | 样本隔离库（#21） |
| 缺视频/缺抽帧/缺描述 | `流程-无视频文本对` / `流程-无视频帧` / `流程-待补描述` | 产线待处理（**不隔离**） |
| 模型/权重不可用 | `流程-待评分`（`process_status=degraded`） | 产线待处理，分数为 `null` |
| 路线关闭 | `流程-视文路线未启用` | 原样透传 |
| 抽帧缺失/损坏/路径非法 | `原始文件-缺字段` / `原始文件-损坏` / `原始文件-无法解析` / `原始文件-编码异常` | 原始隔离区 |

**绝不产出固定或随机分数**：依赖或权重缺失时按 `allow_degraded` 显式失败或显式降级（分数为 `null`）。

## 9. 与其他算子的关系

- **上游**：#11 提供视文候选配对；#09 提供对齐描述；
- **下游**：通过 → #19；不通过 → #21；分数与模型版本另供 #16 标注整体对齐关系；
- **同族关系**：#12（图文）、#13（音文）、#14（视文）、#15（时空）为并列评分路线，
  #14 直接**复用 #12 的 CLIP-ViT/B-32 后端口径**（本地加载、实例复用、默认关闭 + 显式降级）；
- **跨算子约束**：图文评分关闭时 #13/#14 至少一条开启，由任务创建侧拦截，本算子只负责自身开关；
- **旧目录关系**：旧 `demo/14_半自动化候选标注` 保持不动，不得冒充表1 #14。
