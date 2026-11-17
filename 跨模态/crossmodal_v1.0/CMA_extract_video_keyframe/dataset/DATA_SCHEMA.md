# 跨模态 Demo 数据 Schema 契约（开发约定 v1.3）

> 日期：2026-10-02
> 适用范围：跨模态 demo 流水线（算子 2/3/4/5/6/7/8/14+9/16/17/19/20/21 + 交付打包）
> 字段名一旦冻结不得随意变更；适配变更须同步更新本文件与 README。
> v1.2：按会议文档更新为三接口、阶段末输出内容和条件式血缘路径（该血缘路径已于 v4.2 停用）。
> v1.3：2026-10-02 补全并行开发的记录承接、媒体携带、音视频透传和阶段文件对象约定；参见 DEVELOPMENT_CONTRACT.md。
> v1.4：2026-10-19 对齐 v4.2 接口：`/predict` 双协议、统一响应；血缘/阶段归档输出停用。

## 1. 文件标识与任务目录

```text
uniqueId = 任务id_文件id
```

- 工程侧按单文件循环调用算子，同一 `uniqueId` 的算子链顺序执行
- 任务根：`OUTPUT_DIR/<uniqueId>/`；每次运行再按 operator_id/run_id 隔离，文件搬运约定见 DEVELOPMENT_CONTRACT.md（v4.2 起不再写血缘/阶段上下文文件）
- 重复执行不覆盖其他 `uniqueId` 的任务目录

## 1.1 Demo 阶段

正式阶段及 demo 当前阶段末节点如下：

| 阶段 | 正式算子范围 | demo 当前阶段末节点 |
|---|---|---|
| 数据采集与初筛 | 1、2 | 2 |
| 跨模态数据处理 | 3、4、5、6、7 | 7 |
| 跨模态数据对齐标注 | 8、9、10、11、12、13、14、15 | 14 |
| 跨模态数据存储与管理 | 16、17 | 17 |
| 跨模态数据质检与清洗 | 18、19、20、21 | 21 |

demo 未覆盖的正式节点不改变正式阶段边界；demo 阶段末节点传 `isLastOperator=true`。阶段切换时工程侧保存输出、重新上传并生成新的 `uniqueId`。

## 1.2 接口信封（按 260926 规范冻结）

- 所有接口统一响应信封：`{"code": 0|1, "msg": "...", "data": {...}}`（0=成功 1=失败）
- `POST /uploadFile?uniqueId=`（multipart）：zip 由算子侧解压；返回 code/msg
- `POST /predict`（JSON，形态二）：必传 `uniqueId`、`isLastOperator`（阶段末标记）；阶段首算子凭 uniqueId 取素材，阶段内后续算子必传 `inputFile`；v4.2 起中间与阶段末响应同构，统一返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`；不再返回 `lineageFile`
- `POST /predict?detection=<算子 id>`（`multipart/form-data`，形态一）：`file` 必传，返回 JSON 首字段 int `status`（处理型 1 成功/0 失败）或文件流（M05/M06/M07）；各算子细节见本目录 `README.md`
- `POST /removeFile?uniqueId=`：query 传参，返回 code/msg
- 不提供 `/downloadFile`

## 2. 输入格式

### 2.1 跨模态样本清单 JSONL（流水线入口）

每行一个 dict，通过 `sample_id` 关联不同模态文件：

```json
{
  "sample_id": "sample-0001",
  "text": "一只金毛犬在草地上接飞盘",
  "image": "images/golden_001.jpg",
  "video": null,
  "audio": null,
  "metadata": {"source": "sample", "scene": "outdoor"}
}
```

- 模态字段（`text`/`image`/`video`/`audio`）为相对样例目录的路径或内联文本，无该模态时为 `null`
- 本 demo 支持两类组合：非空 text + image，或 video + audio（允许 text=null）；单模态或无有效组合进入异常清单。音视频组合在 08/14/19 不做图文语义判定，透传并明确 not_applicable/待复核
- 支持格式：文本 JSONL 内联；图像 JPG/PNG/BMP/JPEG；视频 MP4/MOV；音频 MP3/WAV/FLAC/AIFF

### 2.2 初筛输入约定

- 图文初筛：文本分词与图像文件名/标签关键词匹配
- 视频音频初筛：时长差不超过配置阈值（默认 20%）

## 3. 中间产物约定

| 节点 | 输出文件 | 说明 |
|---|---|---|
| 算子2 初筛 | `02_screened.jsonl` + `02_rejects.jsonl` | 候选组合 + 明显无关排除清单 |
| 算子3 文本清洗 | `03_text_cleaned.jsonl` | 清洗文本 + `tokens` 分词结果 + 清理统计 |
| 算子4 图像标准化 | `04_images/` + `04_image_index.jsonl` | 标准图像文件 + 尺寸/格式/模糊度记录 |
| 算子5 视频关键帧 | `05_frames/` + `05_frame_index.jsonl` | 关键帧文件 + 时间戳 |
| 算子6 音频标准化 | `06_audio/` + `06_audio_index.jsonl` | 标准 WAV + 采样率/时长 |
| 算子7 协同预处理 | `07_candidates.jsonl` | 统一语义单元的候选跨模态样本 |
| 算子8 整体对齐 | `08_whole_alignment.jsonl` | 整体对齐置信度 + 判定状态 |
| 算子14+9 候选标注 | `14_grounding_candidates.jsonl` | 候选框 + 置信度 + 确认状态 |
| 算子16/17 存储索引 | `16_metadata.json` + `17_index.json` + `17_versions.json` | 元数据、模态索引、版本记录 |
| 算子16/17 统一流转入口 | `17_stored_samples.jsonl` | 完整样本记录；上述三个 JSON 为旁路文件，不替代样本清单 |
| 算子19 反向验证 | `19_reverse_qc.jsonl` | 检索一致性结果 |
| 算子20 文本质检 | `20_text_qc.jsonl` + `20_text_qc_rejects.jsonl` | 查重/通顺度结果 |
| 算子21 图像视频质检 | `21_media_qc.jsonl` + `21_media_qc_rejects.jsonl` | 分辨率/模糊/遮挡结果 |

## 4. 核心记录结构

### 4.1 候选跨模态样本（算子7 之后的主流转结构）

```json
{
  "sample_id": "sample-0001",
  "text": {"content": "清洗后文本", "tokens": ["金毛犬", "草地", "飞盘"], "lang": "zh"},
    "image": {"path": "04_images/sample-0001.jpg", "width": 1024, "height": 768, "blur_score": 120.0},
  "video": null,
  "audio": null,
  "semantic_unit": "unit-0001",
  "metadata": {}
}
```

### 4.2 整体对齐标注（算子8）

```json
{
  "sample_id": "sample-0001",
  "alignment_type": "whole-whole",
  "modality_pair": ["text", "image"],
  "confidence": 0.91,
  "status": "confirmed",
  "model": {"name": "Chinese-CLIP", "version": "ViT-B/16", "degraded": false},
  "evidence": {"clip_similarity": 0.91}
}
```

`status` 枚举：`confirmed`（≥ 确认阈值，默认 0.80）/ `pending_manual_review`（0.50–0.80）/ `rejected`（< 0.50）。降级时 `model.degraded=true`，置信度来自规则方案，在记录中如实说明。

### 4.3 局部—整体候选框标注（算子14+9）

```json
{
  "sample_id": "sample-0001",
  "alignment_type": "local-whole",
  "modality_pair": ["text", "image"],
  "phrase": "金毛犬",
  "bbox": {"x": 120, "y": 80, "w": 340, "h": 260, "coord": "xywh-pixel"},
  "confidence": 0.87,
  "status": "confirmed",
  "model": {"name": "GroundingDINO", "version": "Swin-T", "degraded": false}
}
```

坐标单位：像素，原点在标准化后图像左上角。`status` 枚举同上。

### 4.4 质检结果（算子19/20/21）

```json
{
  "sample_id": "sample-0001",
  "qc_type": "reverse_verify | text_fluency | media_quality",
  "passed": true,
  "score": 0.88,
  "issues": [],
  "model": {"name": "...", "version": "...", "degraded": false}
}
```

严重问题（`passed=false`）记录 `issues` 原因并进入剔除清单。

### 4.5 元数据记录（算子16）

```json
{
  "sample_id": "sample-0001",
  "modalities": ["text", "image"],
  "files": {"image": "aligned/sample-0001.jpg"},
  "alignment": {"whole": "confirmed", "local_confirmed": 2, "local_pending": 1},
  "quality": {"reverse_verify": null, "text_qc": null, "media_qc": null},
  "created_at": "2026-09-30T15:00:00+08:00",
  "version": "v1.0"
}
```

### 4.6 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

## 5. 三接口与 predict 请求响应

- `POST /uploadFile?uniqueId=`：`multipart/form-data` 上传文件或 zip，返回 `code/msg`。
- `POST /removeFile?uniqueId=`：按 `uniqueId` 删除任务目录，返回 `code/msg`。
- `POST /predict`：必传 `uniqueId`、`isLastOperator`；阶段首算子不传 `inputFile`，其余算子传同任务目录内上一算子的产物路径。不提供 `/downloadFile`。

统一响应（v4.2，中间与阶段末同构，`outputFile` 为 `crossmodal-files-v1` 文件内容对象）：

```json
{"code":0,"msg":"success","data":{"operator":"<算子英文 id>","status":"success","outputFile":{"format":"crossmodal-files-v1","entrypoint":"<入口文件>","files":[{"path":"<入口文件>","encoding":"utf-8","content":"..."}]},"result":{"status":1,"process_status":"success","statistics":{},"entrypoint":"<入口文件>","input":"<任务相对路径>"}}}
```

业务失败：`{"code":1,"msg":"<kind>: <detail>","data":{}}`（部分算子在 `/predict` 运行失败时返回
`data={"status":"failed"}`，以各算子 README 为准）。`data.outputFile` 是文件内容，不是下一算子的路径；
`isLastOperator` 仅标记阶段末，不改变响应结构。

## 6. 最终交付（交付打包）

- `final_aligned_corpus.jsonl`：通过全部质检的对齐样本（4.1 + 4.2 + 4.3 + 4.4 合并结构）
- `final_delivery.zip`：语料 JSONL + 关联媒体文件 + `manifest.json`（文件清单、样本数、版本号）；v4.2 起不再附带 `lineage.json`（全链路血缘汇总已停用）
- 异常/剔除清单不进入交付包正本，作为 `review/` 子目录附交

## 7. 并行开发时的完整记录契约

§4.2—§4.5 是业务子对象示例，不是可以丢掉文本、媒体和上游结果的独立流转记录。所有主 JSONL（包括名称带 index/alignment/qc 的文件）都保持每行一条完整样本，sample_id 在本次素材内唯一，顺序稳定。派生多框/多帧放数组，禁止按行号跨模态配对。

### 7.1 入口归一化与字段归属

- 入口 text 为字符串或 null，image/video/audio 为相对路径或 null；元数据对象默认 {}。03 起将 text 规范为对象，04/05/06 依次将各媒体转为对象。02 接受这种规范对象以便独立回归测试。
- text 对象：content:string、original:string、tokens:string[]、token_offsets:[start,end][]、lang:string。offset 为清洗后 content 的 Unicode 字符索引，end 不含；original 保留原文本供审计，日志不输出原文。空文本不能凭空补词。
- image 对象：path:string、width:int、height:int、format:string、blur_score:number（8bit灰度 Laplacian 方差，无量纲、越大越清晰，并非 0—1 概率）；原图另存 original_path。04 默认最长边 1024、保持比例、不放大小图，尺寸不足交给 21 判断，不硬拉成方形。
- video 对象：path:string（可解码原视频）、duration:秒、fps:number、frames:[{path,timestamp,frame_index,width,height}]；timestamp 为原视频起点秒。无可靠时间戳需报错/说明，不能用任意零值填充。黑边裁剪可另附 crop。
- audio 对象：path:string（标准 WAV）、sample_rate:int、channels:int、duration:秒、segments:[{path,start,end}]；未裁剪时 segments=[]。05/06 是轻量示意，不声称完成语义片段识别。
- processing 对象按两位编号 "02"/"03" 等保存各节点的 int `status`（1 正常/0 失败）+ `process_status` 流程态、`reason`、`metrics`；不得重置其他节点信息。
- alignment.whole：§4.2 的整体结果；alignment.local：§4.3 的候选框数组；局部候选还带 candidate_id、phrase_span（能可靠映射时）、confirmation_method、human_verified=false。未提供模型可用的英文提示时不得伪造翻译。
- qc.reverse_verify、qc.text_fluency、qc.media_quality 分别承接 19/20/21；每项含 passed:bool|null、score:number|null、issues:array、check_status:checked|not_applicable|insufficient_evidence、model 与 evidence。
- metadata 中未知字段保留；16 增加 storage 元数据，17 增加 dataset_version；在 19—21 执行前不填 quality=true。每个节点只改变自己的业务子对象。
- 同一条记录重新处理：替换自身编号的结果，不重复追加同一个 local candidate（v4.2 起不再追加 run 血缘）。不得丢弃原有模型、路径映射和失败原因。

### 7.2 07 之后的最小完整记录示例

```json
{
  "sample_id": "s1",
  "text": {"content": "红色方块", "original": "红色方块", "tokens": ["红色", "方块"], "token_offsets": [[0,2],[2,4]], "lang": "zh"},
  "image": {"path": "04_images/s1.png", "original_path": "media/original/s1.png", "width": 64, "height": 64, "format": "PNG", "blur_score": 120.0},
  "video": null,
  "audio": null,
  "semantic_unit": "s1:unit:0001",
  "processing": {"07": {"status": 1, "process_status": "success", "metrics": {"modality_pair": ["text","image"]}}},
  "alignment": {"whole": null, "local": []},
  "qc": {},
  "metadata": {"image_tags": ["红色","方块"]}
}
```

示例媒体由各目录 make_sample.py 真正生成；数字仅说明类型，不是测试精度。semantic_unit 默认每个 sample_id 一单元；音视频记录保留时长与时间范围，超时长差异先标记待复核，不冒充语义同步成功。

### 7.3 主清单、旁路产物与样本去向

| 服务 | 主清单（下一节点 inputFile 的目标） | 额外产物 |
|---|---|---|
| 02 | 02_screened.jsonl | 02_rejects.jsonl |
| 03 | 03_text_cleaned.jsonl | 03_rejects.jsonl |
| 04 | 04_image_index.jsonl | 04_images/、04_rejects.jsonl |
| 05 | 05_frame_index.jsonl | 05_frames/、05_rejects.jsonl |
| 06 | 06_audio_index.jsonl | 06_audio/、06_rejects.jsonl |
| 07 | 07_candidates.jsonl | 07_rejects.jsonl |
| 08 | 08_whole_alignment.jsonl | 08_review.jsonl |
| 14（含9） | 14_grounding_candidates.jsonl | 14_review.jsonl |
| 16（含17） | 17_stored_samples.jsonl | 16_metadata.json、17_index.json、17_versions.json、16_rejects.jsonl |
| 19 | 19_reverse_qc.jsonl | 19_review.jsonl |
| 20 | 20_text_qc.jsonl | 20_text_qc_rejects.jsonl（问题副本，主清单仍保留待最终分流） |
| 21 | 21_media_qc.jsonl（通过者） | 21_media_qc_rejects.jsonl、21_review.jsonl |

08/14/19/20 有效记录全部保留在主清单，问题是 sidecar 副本；严重输入损坏仍可剔除并记录。21 依据累计质量结果给出 delivery_status=accepted|review|rejected；只有 accepted 进入主清单，其他进入对应清单。其余节点不使用最终 delivery_status。
review 记录以 sample_id、operator、reason、record（完整有效样本或可用字段）、evidence 组织；坏行记录 line 与 reason，避免保存无限长的原始坏行。不同节点 review 清单随包保留，不能重名覆盖。

21 汇总规则：已检查项目存在明确 passed=false，或整体/局部存在明确 rejected 的必要标注，按配置严重性分流 rejected；未检查必要语义项、pending_manual_review、降级结果或遮挡未知为 review；只有全部必需检查有真实通过证据才 accepted。
未适用的 local annotation 在空短语请求场景可忽略，但不能忽略整体图文对齐和反向检索。音视频组合目前没有真实语义对齐模块，因此作为 review 演示输出，不冒充最终语义合格语料。降级 demo 可以跑通并输出 review，最终通过者为空也必须如实报告。

### 7.4 模型与版本约定

08 Chinese-CLIP 与 19 检索使用相同的模型/预处理语义，但 19 必须构建实际候选集合并重新检索，不能复制 08 的分数冒充验证。
08 confidence 可采用 clamp((cosine+1)/2,0,1) 作为 demo 归一分，并记录 score_semantics=normalized_cosine，不是校准概率；evidence.clip_similarity 保存原余弦。阈值默认 confirmed>=0.8、pending>=0.5；这些只是 demo 配置。
规则降级单独记录 rule_score 与 rule_only，真实 clip_similarity=null；标签重叠不是图像语义编码。所有自动确认标记 confirmation_method=auto_threshold、human_verified=false。
19 只有一个候选图时标记 insufficient_evidence，不能声称 Recall@1=1；真实演示至少一正例和多个干扰候选。
16 版本记录内容摘要包括规范化后的样本、引用媒体 bytes 和持久辅助结果；排除时间、run_id、传输临时路径等不稳定字段。同内容重复调用版本不增，内容变化才增；版本状态来自上传包，不使用可变全局 JSON 数据库。

### 7.5 文件携带与格式版本

每个运行入口旁有 manifest.json 登记 main/media/sidecar/review。主清单中的相对媒体路径均以该 manifest 所在目录为根。需复制/重写被携带记录中的所有引用，拒绝不可搬运的外部路径。
阶段内 `inputFile` 指向主清单；`data.outputFile` 统一用 [DEVELOPMENT_CONTRACT.md](./DEVELOPMENT_CONTRACT.md) §4 的 crossmodal-files-v1 文件内容对象承载全部文件（中间/阶段末同构）。工程侧负责打包 zip 和下一阶段重新上传（v4.2 起不再保存历史血缘）。接口字段不因本 demo 内部格式新增。
