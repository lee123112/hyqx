# 跨模态算子包（crossmodal_v1.0 · 前后端联调版）

> **本目录为 2026-11-11 前后端联调版**，来源为 `crossmodal_v1.0_deploy`，保留其最新链路修复、
> 26 个算子、业务配置与完整本地权重。业务接口可直接调用，`/docs`、`/redoc`、`/openapi.json` 可访问。
> **Docker Compose 启动：`docker compose up -d --build`**，默认宿主机 `17059` → 容器 `8100`。
> 包内 `.env` 已提供运行参数与国内构建源；`deploy.sh` 可额外校验权重并等待健康检查。
> 部署与联调步骤见 [部署说明.md](部署说明.md)，K8s 示例见 [K8s 部署说明](k8s/README-k8s.md)。
> 全部模型由各算子本地 `weight/` 加载；内置权重校验清单见 `SHA256SUMS.txt`。

> **v1.0 正式版（2026-10-31）**：开发侧负责的 **#3–#22** 完成上线前最后一版整改——本地小模型权重
> （CLIP-ViT/B-32、whisper-base、YOLOv8n）已**逐算子内置**到 `<算子目录>/weight/`，默认由各算子本地加载；
> 单算子目录可独立复制、独立启动，不再要求包根共享 `weight/` 或设置 `MODEL_ROOT`。
> 接口/协议/配置字段/阈值未变；`shared_clip/` 仅保留为可选链路优化。全量回归见 `_v1.0正式版改造与测试报告.md`。
>
> 来源基线：`G:\HYQX\算子\跨模态\demo_10.6`（#16/#24 取最新版 `_v2`）；本文件由 2026-10-26 交付包演进而来。
> **上传结构对照甲方《【开发规范】数据集合成算子开发规范与集成-多算子打包V1.0》第 3/6 页**：算子上层目录
> `Dockerfile` / `inference.py` / `weight/` / `map.json`；算子目录 `dataset/ utils/ configs/ tests/ output/model/ README.md`。
> 目录结构另参考示例：`G:\HYQX\算子\源码\pretrain_operator_demo_image`（多算子打包示例，池化入口写法来源）。
> 名称登记：`G:\HYQX\算子\算子情况登记表.xlsx`（C 列中文名称、D 列英文名称）。
> 生成日期：2026-10-11；**修订 2026-10-12**（补 MTY 合规层）；**重新打包 2026-10-22**（v4.2 双协议、`/health` 全量不进 OpenAPI、
> M07/M09/M11 探针修复、#03 时长差阈值 7 秒试验值）；**重新打包 2026-10-26**（包含 2026-10-23 收口改动：
> #19 四项门冻结、#14 整段视频帧预算、#14 关闭态阈值回填；test7 全链路 `#3–#21` 19 阶段 PASS；包卫生清理，
> 不含缓存/运行镜像/测试夹具；test7 发布钩子默认关闭，不改变交付行为；**补回 `docker-compose.yml`**——旧 20261022 包的交付件，按当前镜像/权重/端口口径更新，默认宿主机 17059 → 容器 8100；**Dockerfile 支持换镜像源**——`PYTHON_IMAGE` / `PIP_INDEX_URL` / `APT_MIRROR` 构建参数，并补回 `ffmpeg` / `tzdata` 系统依赖）。
> **本轮重打包未再改算子算法、阈值或接口契约**；#19/#14 的收口改动为 2026-10-23 已确认项，详见使用说明第 11 节。

## 0. v4.2 变更（2026-10-19）

对照《跨模态对齐产线任务书审阅与开发规则 v4.2》完成 26 个算子的源码改造：

1. **血缘/阶段归档/运输输出停用**：`utils/mty_compliance.py`、`utils/lineage.py`、`utils/transport.py` 不再被 `app.py`/`inference.py` 调用（代码保留存档，文件首行加 `# [v4.2]` 声明）；`/predict` 响应不再返回 `lineageFile`；根目录 `merge_lineage.py`、`task_output.py`、`sync_mty_compliance.py` 与血缘契约测试标记停用。
2. **`/predict` 双协议**：形态一 `POST /predict?detection=<算子标识>`（`multipart/form-data`，`file` 必传，返回 int `status`：判定型 1 不过滤/0 过滤，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，阶段文件传递）。池化入口 `inference.py` 同步支持 `detection` 路由。
3. **输出记录 status 双字段**：写盘 JSONL 与业务字段中的 `status` 改为 int（1/0），原字符串迁入 `process_status`（`success/failed/not_applicable/pending_review/pending_writeback/...`）。
4. **注册表修正**：`map.json` 的 13 处 `stageId` 按 v4.2 阶段表修正（M03→S1、M04–M08→S2、M11/M12/M16→S3、M21→S4、M24/M25→S7、M26→S8），并补齐 `isLastOperator`（M03/M08/M19/M21/M22/M23/M25/M26 为阶段末）。
5. 详细改造规范见包内 `_v4.2改造说明.md`；改造前基线见 `_archive_*` 与各算子 `tests/` 中已跳过项。

## 0.1 v4.3 变更（2026-10-29）

按业务平台实际调用方式（**不传文件 id、直接传单文件流**）完成 #3–#21 共 19 个自研算子的形态一压缩包改造：

1. **形态一（`POST /predict?detection=<算子标识>`）新增 ZIP 包输入**：业务把阶段输入（入口 JSONL + 媒体 + 可选 `manifest.json`）打成一个 ZIP，用 multipart 字段 `file` 直接上传；`manifest.json` 使用 `format=crossmodal-manifest-v1`；不再需要先调 `/uploadFile`（`uniqueId` 仍可选，缺省时服务端按 `upload_<时间戳>` 生成合法标识）。
2. **形态一响应改为 JSON 契约**：首字段 `status`（1=执行成功/0=执行失败）+ `process_status` + 业务指标 + `outputFile` 路径引用（`format=crossmodal-files-v1`、`mode=path-reference`、`runDir`、`entrypoint`、`files[]`、`zip`）。平台既可按 `runDir`/`entrypoint` 取产物，也可直接把 `outputFile.zip` 作为下一算子形态一的 `file` 输入，实现逐算子直传。
3. **每个算子自带 `utils/form1_package.py`**（仅标准库）负责解包、入口解析、输出引用与 `*_form1.zip` 打包；非 ZIP 单文件调用保持 v4.2 行为不变（M05/M06/M07 非 ZIP 仍返回文件流）。
4. 详细契约见 `跨模态算子使用说明.md` §4.4.4 / §5.2 / §7.1；改造说明与回传清单见 `_v4.3改造说明.md`。

## 0.2 v1.1 变更（2026-10-29）

应业务平台要求，形态一 `/predict?detection=` 在原有 multipart `file` 之外增加 **`filePath` 本地路径输入**：

1. 平台可直接传**池进程可读的本地文件绝对路径**（典型为上一步响应的 `outputFile.zip` 容器路径），
   由池化入口 `inference.py` 读成文件流后按原 multipart 协议转发给子算子；
2. 原有 `file` 上传方式完全保留，两者**二选一**；`/uploadFile`、`/removeFile`、形态二 `inputFile` 均不变；
3. **只改 `inference.py`**，19 个自研算子代码零改动；部署只需覆盖 `inference.py` 并重启池化入口；
4. 平台按 `filePath` 驱动链路时，无需下载/解压产物，也不需要平台侧挂载输出目录；
   只要路径在池容器内可见即可；
5. 详细契约见 `跨模态算子使用说明.md` §4.4.7 与 `_v1.1路径输入说明.md`。

## 1. 打包规则

1. **只取最新版**：同一编号有多个版本时取最新版，即 `16_标注整体对齐关系_v2`、`24_抽检并回写质量报告_v2`（登记表 F 列已指向 `_v2`）。
2. **一个算子一个目录**：26 个算子各自独立成目录，保留其自身可运行的完整代码（`app.py`/`inference.py`/`processor.py`、`utils/`、`configs/`、`tests/`、`dataset/`、`output/`、`README.md`、`OPERATOR_MANUAL.md`）；`map.json`、`Dockerfile`、`requirements.txt` 按甲方上传结构统一收到上层（原各算子同名文件已归档，见第 3 节）。
3. **文件名去中文**：`算子说明书.md` → `OPERATOR_MANUAL.md`；样例媒体（如 `金毛犬_草地.jpg`）改为英文名，并同步更新引用。
4. **剔除缓存与临时产物**：`__pycache__/`、`*.pyc`、`.pytest_cache/`、`_pytest_out.txt`、`output/` 下的运行结果；`output/` 目录保留为空（含 `.gitkeep`）。
5. **算子目录名 = 登记表 D 列英文名称**（2026-10-11 完成重命名，包内已无中文目录名/文件名）。

## 2. 算子清单

| 编号 | 中文名称（登记表C列） | 目录名（登记表D列） | 端口 | 所属阶段 | 阶段内位置 |
|---:|---|---|---:|---|---|
| M01 | 接入多模态批次 | `CMA_receive_multimodal_batch` | 8101 | 输入核验 | 1/3 |
| M02 | 核验场景编号 | `CMA_check_scene_id` | 8108 | 输入核验 | 2/3 |
| M03 | 初筛明显无关数据 | `CMA_filter_irrelevant_data` | 8102 | 输入核验 | 3/3 |
| M04 | 清洗并标准化文本 | `CMA_clean_normalize_text` | 8103 | 数据清洗 | 1/5 |
| M05 | 标准化图像 | `CMA_normalize_image` | 8104 | 数据清洗 | 2/5 |
| M06 | 抽取视频关键帧 | `CMA_extract_video_keyframe` | 8105 | 数据清洗 | 3/5 |
| M07 | 标准化音频 | `CMA_normalize_audio` | 8106 | 数据清洗 | 4/5 |
| M08 | 切齐跨模态语义单元 | `CMA_align_semantic_unit` | 8107 | 数据清洗 | 5/5 |
| M09 | 准备候选描述 | `CMA_build_candidate_description` | 8109 | 数据加工 | 1/11 |
| M10 | 核对输入对象准入 | `CMA_check_input_admission` | 8110 | 数据加工 | 2/11 |
| M11 | 生成候选配对 | `CMA_generate_candidate_pair` | 8111 | 数据加工 | 3/11 |
| M12 | 评分图文语义一致性 | `CMA_score_image_text_consistency` | 8112 | 数据加工 | 4/11 |
| M13 | 评分音文语义一致性 | `CMA_score_audio_text_consistency` | 8113 | 数据加工 | 5/11 |
| M14 | 评分视文语义一致性 | `CMA_score_video_text_consistency` | 8114 | 数据加工 | 6/11 |
| M15 | 评分时空一致性 | `CMA_score_spatiotemporal_consistency` | 8115 | 数据加工 | 7/11 |
| M16 | 标注整体对齐关系 | `CMA_annotate_whole_alignment` | 8116 | 数据加工 | 8/11 |
| M17 | 标注局部对整体 | `CMA_annotate_local_to_whole` | 8117 | 数据加工 | 9/11 |
| M18 | 标注局部对局部 | `CMA_annotate_local_to_local` | 8118 | 数据加工 | 10/11 |
| M19 | 筛选高置信样本入池 | `CMA_filter_high_confidence_samples` | 8119 | 数据加工 | 11/11 |
| M20 | 质检对齐产物 | `CMA_check_alignment_quality` | 8120 | 数据质量评估 | 1/2 |
| M21 | 处置错配样本 | `CMA_dispose_mismatched_sample` | 8121 | 数据质量评估 | 2/2 |
| M22 | 生成跨模态Token | `CMA_generate_crossmodal_tokens` | 8122 | 样本构建 | 1/1 |
| M23 | 压缩交付对齐语料包 | `CMA_package_aligned_corpus` | 8123 | 数据集交付 | 1/1 |
| M24 | 抽检并回写质量报告 | `CMA_writeback_sampling_quality_report` | 8124 | 人工抽检复核 | 1/2 |
| M25 | 管理人工复核任务 | `CMA_manage_manual_review_task` | 8125 | 人工抽检复核 | 2/2 |
| M26 | 生成交付样例 | `CMA_generate_delivery_sample` | 8126 | 交付样例 | 1/1 |

> 重命名前的目录名即登记表 F 列（`01_接入多模态批次` … `26_生成交付样例`）。历史文档与数据契约中出现的旧名称
> （如 `destination` 回退目标 `09_准备候选描述`、`01_接入多模态批次`）属于**跨算子业务取值**，未随之改动，详见第 7 节。

## 3. 目录结构（对照甲方《开发规范》上传结构）

### 3.1 算子上层目录（本包根目录）

```text
opcrossmodalpool/（发布包根目录）
├── Dockerfile             # 容器化打包文件（1 个，覆盖 26 个算子；权重随算子目录进镜像）
├── docker-compose.yml     # Docker Compose 部署文件（默认 17059→8100；权重内置，不再挂载共享模型目录）
├── deploy.sh              # 可选：校验权重、构建启动与健康等待
├── verify_service.sh      # 健康、算子列表、权重与预检验收（compose 容器内执行）
├── verify_service.py      # 【部署版新增】验收逻辑（仅标准库，可 --local 直连宿主机端口）
├── 部署说明.md             # 【部署版新增】构建、启动、验收、回滚、与 deployment-kit 的差异说明
├── SHA256SUMS.txt         # 【部署版新增】18 个内置模型权重文件的 SHA-256 清单
├── k8s/                   # Kubernetes 联调清单（Deployment+Service）与说明
├── inference.py           # 主推理服务入口（读 map.json，按 operatorId 路由；预检逐算子权重）
├── weight/                # 【仅说明/清单】运行权重已内置到各算子目录 weight/（包根不再放模型大文件）
├── map.json               # 算子映射表（编号 → 目录/端口/入口/产物/权重内置关系）
├── requirements.txt       # 上层统一依赖（已合并 26 个算子的依赖）
├── README.md              # 本说明
├── .dockerignore          # 构建忽略项（不排除各算子 weight/）
├── .env.example           # 运行环境变量示例（MODEL_ROOT 仅可选覆盖）
├── shared_clip/           # 【可选】任务级共享 CLIP 优化；未启用时各算子回退本地 weight/
├── tests/                 # 包级契约/合规测试（随包 5 个文件）
├── task_output.py         # 任务级输出合并（v4.2 停用·历史存档）
├── merge_lineage.py       # 血缘合并（v4.2 停用·历史存档）
├── sync_mty_compliance.py # MTY 合规同步（v4.2 停用·历史存档）
├── _v4.2改造说明.md        # v4.2 改造设计与差异说明
├── _v4.3改造说明.md        # v4.3 形态一压缩包改造说明
├── _v1.1路径输入说明.md     # v1.1 filePath 本地路径输入契约
├── _v1.0正式版改造与测试报告.md # v1.0 正式版：权重内置、链路测试与拦截统计
├── 跨模态算子使用说明.md      # 平台对接主文档（接口、协议、配置）
├── 跨模态算子-单算子调用接入文档.md # 单算子直连接入说明
└── CMA_xxx/  ×26          # 26 个算子目录，见 3.2
```

### 3.2 算子目录（26 个，目录名 = 登记表 D 列英文名）

```text
CMA_xxx/
├── dataset/               # DATA_SCHEMA.md + sample/ 小样本
├── utils/                 # IO、血缘、日志、schema 与算法工具
├── configs/               # 配置文件（config.yaml）
├── tests/                 # 单测与接口/路径安全测试
├── output/                # 本地运行输出；output/model/ 为空占位（本包算子均无训练过程）
├── README.md              # 必填：用途、配置、启动、接口、输出与限制
├── OPERATOR_MANUAL.md     # 算子说明书（甲方交付要求）
├── app.py                 # 本算子 HTTP 服务入口（/uploadFile、/predict、/removeFile）
├── inference.py           # 本算子编排/核心流程入口
├── processor.py           # 本算子 CLI 入口
└── weight/                # 【v1.0 正式版】本算子自带模型权重（仅 #8/#12/#14/#16/#17/#18/#20）
```

对照说明：

- 甲方规范中算子目录为 `dataset/ utils/ configs/ tests/ output/model/ README.md`。本包**多做一步**：
  保留各算子自己的 `app.py`/`inference.py`/`processor.py`（26 个算子代码零改动、各自单测口径不变），
  由上层 `inference.py` 按 `map.json` 拉起这些入口、对甲方只暴露统一接口（见第 5 节）。
- 各算子原有的 `Dockerfile`、`requirements.txt`/`requirements-model.txt`/`requirements-dev.txt`、`map.json`、
  `packaging/`、`DEVELOPMENT_PLAN.md`、`HANDOFF.md`、`DEVELOPMENT_STATUS.md`、`TECHNICAL_DESIGN.md`
  已移出包外归档：`G:\HYQX\算子\跨模态\_archive_crossmodal_pre_upload_20261011\<算子目录>\`（可回滚）。
- 算子目录名沿用登记表 D 列 `CMA_` 英文名；甲方格式 `{SS}_{CATEGORY}_{MODALITY}_{SEQ3}`
  （PDF 实例 `057a_prohibited_goods_ecommerce`）的具体取值待甲方确认后统一改名。
- 归档带来的两处必要回迁/同步（**仅测试与交付校验工具，未动业务逻辑**）：
  ① `CMA_package_aligned_corpus` 的交付包离线校验工具由 `packaging/prepare_package.py` 迁到
  `utils/prepare_package.py`（`parents[1]` 语义不变；`tests/conftest.py` 的 `sys.path` 同步改指 `utils/`）；
  ② `CMA_extract_video_keyframe/tests/test_api.py` 的「交付件一致性」用例改为校验**上层**
  `map.json`/`Dockerfile`/`inference.py`/`weight/` 与算子目录 `README.md`/`OPERATOR_MANUAL.md`
  （原断言指向已上收的 `map.json`、`requirements.txt`、`packaging/Dockerfile`）。

## 4. 根目录文件

| 文件 | 说明 |
|---|---|
| `Dockerfile` | **容器化打包文件**（上层唯一构建文件）：装上层 `requirements.txt` 与 `ffmpeg`/`tzdata`，`python inference.py --preflight` 自检，`CMD uvicorn inference:app --port 8100`；构建参数 `PYTHON_IMAGE` / `PIP_INDEX_URL` / `APT_MIRROR` 可换镜像源。 |
| `docker-compose.yml` | **Docker Compose 部署文件**：服务/镜像 `opcrossmodalpool:1.0.0`，宿主机 17059 → 容器 8100；v1.0 权重随算子目录内置进镜像，不再挂载包根 `weight/`；产物写 `OUTPUT_HOST_DIR`；池化变量可在 `.env` 覆盖，上传/响应限额按需改该文件。 |
| `inference.py` | **主推理服务入口**：读 `map.json`，按 `operatorId` 路由到对应算子进程，三接口原样转发；`--preflight` 额外校验逐算子内置权重。 |
| `weight/` | **仅说明/清单**。v1.0 正式版运行权重在各算子目录 `weight/` 内：`#8 whisper-base`、`#12/#14/#16/#18/#20 clip/ViT-B-32.pt`、`#12/#17 YOLOv8n/yolov8n.pt`，详见 `weight/README.md` 与 `map.json.weightBundle`。 |
| `map.json` | 26 个算子的统一注册表（编号/名称/目录/端口/入口/阶段/产物），另含 `uploadStructure`、`pool` 两节说明。 |
| `requirements.txt` | 上层统一依赖（合并 26 个算子依赖；原各算子 `requirements*.txt` 已归档）。 |
| `README.md` | 本说明。 |
| `.dockerignore` | 容器构建忽略项（缓存/输出/临时产物；**不排除**各算子自带 `weight/`）。 |
| `.env.example` | 运行环境变量示例（`OUTPUT_DIR`、可选 `MODEL_ROOT`、上传/响应限额等）。 |
| `deploy.sh` / `verify_service.sh` / `verify_service.py` | 构建启动与验收：校验 18 个权重 SHA-256、compose 启动、健康等待、接口与预检检查。 |
| `SHA256SUMS.txt` | 18 个内置模型权重文件的 SHA-256 清单（`sha256sum -c SHA256SUMS.txt`）。 |
| `部署说明.md` | 发布信息、模型调用方式、部署/验收/回滚步骤、与 deployment-kit 的差异、发布记录模板。 |
| `shared_clip/` | **【可选】任务级共享 CLIP 服务**（`runtime.py`）。由产线启动一个常驻进程时，`#12/#14/#16/#18/#20` 可通过本机 IPC 复用同一份 CLIP 实例；未启动或目录不存在时各算子回退到自身本地 `weight/`，不影响单算子独立运行。 |

### 4.1 模型调用逻辑（当前实现）

**结论：全部是本地/本机调用，没有任何公网模型 API；跨模态包内无 HTTP 模型客户端。**

按算子分两类：

| 模型 | 使用算子 | 调用方式 |
|---|---|---|
| CLIP-ViT/B-32 | #12、#14、#16、#18、#20 | 默认：各自进程内加载本算子 `weight/clip/ViT-B-32.pt`（`open_clip`→`clip`→`transformers` 依次尝试）；若编排侧显式启动共享 CLIP，则优先复用本机 IPC 实例 |
| whisper-base | #08 | 算子进程内 `transformers` 本地加载本算子 `weight/whisper-base`（`local_files_only=True`）；#13 v2 为规则路线，复用上游转写 |
| YOLOv8n | #17（必需）；#12（可选） | 算子进程内 `ultralytics.YOLO(本算子 weight/YOLOv8n/yolov8n.pt)` |
| wav2vec2-base + bert-base-chinese | —（v1.0 不加载） | #13 生产路径为规则 CER；这两份权重不随正式包内置，仅实验诊断可显式注入 |

**共享 CLIP 服务（`shared_clip/runtime.py`）机制：**

1. 产线编排脚本用 `SharedClipSession(output_dir)` 启动子进程：
   `subprocess.Popen([python, shared_clip/runtime.py, endpoint_file, key, stats_file])`，
   服务监听 `127.0.0.1` 随机端口 + 32 字节随机 authkey，把 `{address, key}` 写入 endpoint JSON；
   随后设置环境变量 **`MULTIMODAL_CLIP_SESSION=<endpoint.json>`**，各算子子进程继承。
2. 算子侧 `RemoteClip.from_env()` 读该环境变量；有则连接共享服务，无则返回 `None` → 走本地加载。
3. 共享服务硬约束：架构只接受 `ViT-B-32`（拒绝其它模型）、必须有真实权重、
   **同一任务不得加载第二份 CLIP 权重**（`weight_loads` 恒为 1），产物里写 `shared_task_instance=true`。
4. **连接失败即显式失败**：环境变量已设置但连不上时抛 `BackendUnavailable("共享CLIP不可用，不重复加载")`，
   不会偷偷另加载一份权重。

**加载与失败语义（所有用模算子一致）：**

- 惰性加载：只在真正需要时才 import 重依赖 / 读权重（#08 开关关闭时零模型加载，日志与统计可证）。
- 复用：进程内按签名缓存（`cached_encoder`）；#08 同一 run 内复用同一实例。
- 设备：`auto|cpu|cuda`，`cuda` 不可用时退回 `cpu` 并记录原因。
- 失败：`allow_degraded=false` → 直接抛错；`true` → 返回 `degraded` + 真实原因。
  **绝不用固定分/随机分/规则分冒充模型分数**，`model_identity()` 如实写入血缘。

## 5. 运行方式

### 5.1 池化入口（对照甲方「多算子打包」：一个镜像、一个入口、一个端口）

容器部署（推荐，包根目录已带 `docker-compose.yml`；默认宿主机 17059 → 容器 8100）：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:17059/health
```

构建镜像时如下载慢，可换源（详见使用说明 §3.1.1）：

```bash
docker build -t opcrossmodalpool:1.0.0 \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn .
```

本地联调（不构建镜像）：

```powershell
# 在包根目录执行；权重由各算子从自身 weight/ 加载，无需设置 MODEL_ROOT
pip install -r requirements.txt
$env:OUTPUT_DIR = "G:\data\op_output"
python -m uvicorn inference:app --host 0.0.0.0 --port 8100
```

```powershell
# 启动自检（不拉起子进程）：注册表 + 26 个算子目录关键项 + 逐算子内置权重
python inference.py --preflight

# 调用：?operatorId= 与 body 内 operatorId 等价；
# M12 / 12 / 012 / image_text_semantic_scoring / CMA_score_image_text_consistency 均可
curl -X POST "http://127.0.0.1:8100/predict?operatorId=M12" -H "Content-Type: application/json" -d "@req.json"
```

池化行为：

- 首次调用某个算子时**按需拉起**该算子的服务进程（`cwd=<算子目录>`，
  `python -m uvicorn app:app --port <map.json 中的端口>`）；`POOL_AUTOSTART=1` 启动即全部拉起，
  `POOL_ONLY=12,13` 只暴露部分算子，`POOL_STARTUP_TIMEOUT` 控制就绪等待秒数。
- `/uploadFile`、`/predict`、`/removeFile` 的请求与响应**原样转发**，返回体与单算子服务完全一致；
  `/predict` body 中的 `operatorId` 会在转发前剔除，不影响子算子的 pydantic 校验。
- 池化自身状态接口：`GET /health`、`GET /ready`、`GET /operators`（含子进程状态与端口）。
- 26 个算子的端口（8101–8126）只在容器内可见，对外只有 `PORT`（默认 8100）。

### 5.2 单算子直跑（联调与单测口径，与改动前一致）

```powershell
cd CMA_receive_multimodal_batch
pip install -r ..\requirements.txt        # 依赖已合并到上层
$env:OUTPUT_DIR = "G:\data\op_output"
python -m uvicorn app:app --host 0.0.0.0 --port 8101
```

需要模型的算子（#08、#12、#14、#16、#17、#18、#20）默认从**本算子目录** `weight/` 读权重：
各算子 `configs/config.yaml` 里的权重仍是**相对路径**（如 `clip/ViT-B-32.pt`），按
`$MODEL_ROOT`（可选覆盖）或 `<算子目录>/weight` 解析；v1.0 正式版无需挂载共享模型目录、无需设置 `MODEL_ROOT`。
**运行期绝不联网下载**；缺权重时按各算子 `allow_degraded` 显式失败或降级，绝不伪造分数。
#13 音文评分为 v2 规则路线（复用上游 whisper 转写），生产不加载 wav2vec2/BERT。

单算子独立运行：把 `CMA_xxx/` 目录整体复制到任意位置即可（含自己的 `weight/`），
`cd CMA_xxx && python -m uvicorn app:app --port 8112`；不依赖包根 `weight/` 或 `shared_clip/`。

## 6. 模型清单（各算子 `<算子目录>/weight/`）

| 模型 | 内置算子 | 算子内相对路径 | 备注 |
|---|---|---|---|
| `CLIP-ViT/B-32` | #12、#14、#16、#18、#20 | `weight/clip/ViT-B-32.pt` | 表1 冻结架构；5 个算子各内置一份，可独立运行；未收录 `ViT-L-14.pt` |
| `whisper-base` | #08 | `weight/whisper-base/` | #08 转写（默认关闭，开启时本地加载）；#13 v2 不加载 |
| `YOLOv8n` | #17（必需）、#12（可选） | `weight/YOLOv8n/yolov8n.pt` | 通用类别预填；#12 默认 detector.weights=null，不改变 CLIP 分档 |
| `wav2vec2-base`、`bert-base-chinese` | —（不内置） | — | #13 生产为规则 CER，复用上游转写；仅实验诊断可显式注入 |

权重来源：`G:\HYQX\算子\跨模态\crossmodal_package_20261026\weight`，字节数/SHA-256 记录在
`map.json.models`、`weight/README.md` 及各算子 `weight/README.md`。其余模型
（`bge-m3`、`LaBSE`、`nllb-200-distilled-600M`、`Qwen2.5-1.5B-Instruct`、`xlm-roberta-base`、
`ViT-L-14`）在 #3–#22 中无引用，未收录。

## 7. 待办与注意事项

- [x] 算子目录名按登记表 D 列英文名称统一（2026-10-11 完成；`shared_clip/runtime.py` 的硬编码路径、各算子 `prog=` 与根 `map.json` 已同步）。
- [x] `#02`/`#14`/`#21` 缺失的算子说明书已补齐（`OPERATOR_MANUAL.md`）。
- [x] `#04`–`#08` 已补根目录 `Dockerfile`（原 `packaging/` 构建方式保留不变）。
- [x] 缺失 `processor.py` 的 12 个算子已补 CLI 入口。
- [x] **上传结构对齐甲方规范**（2026-10-11）：上层补齐 `Dockerfile`、`inference.py`、`weight/`（原 `model/` 重命名）；
      各算子目录清出 `Dockerfile`/`requirements*.txt`/`map.json`/`packaging/`/开发过程文档并补齐 `output/model/`，
      移出项归档在 `G:\HYQX\算子\跨模态\_archive_crossmodal_pre_upload_20261011`（可回滚）；
      上层 `inference.py` 提供池化入口（`map.json` + `operatorId` 路由，默认端口 8100）。
- [ ] **业务取值未改**：跨算子数据契约中的算子标识仍是旧中文目录名，例如 `#10` 的
      `DEST_CANDIDATE_DESCRIPTION = "09_准备候选描述"`、`DEST_BATCH_INTAKE = "01_接入多模态批次"`，
      以及 `#21` 台账 `destination` 的 `^^\d{2}_` 校验规则。这类值写入 jsonl 产物并被他算子消费，
      是否随目录改名一并改英文需与甲方确认后再动（改动需同步测试与对接说明）。
- [ ] 端口以工程侧正式端口分配表为准（登记表中部分算子端口为“待配置”）；池化对外端口默认 8100，可用 `PORT` 覆盖。
- [x] **v1.0 正式版权重内置（2026-10-31）**：所需模型已逐算子复制到 `<算子目录>/weight/`（#8 whisper-base；#12/#14/#16/#18/#20 CLIP-ViT/B-32；#12/#17 YOLOv8n），默认加载算子本地权重，支持单算子目录独立复制运行；包根 `weight/` 只保留说明/清单，`shared_clip/` 降级为可选优化；`python inference.py --preflight` 新增逐算子权重校验。
- [x] 权重（历史，2026-10-26 之前口径）：交付 zip 内含包根 `weight/` 26 文件约 2.27 GB，运行期只读挂载 `/models`；该口径已由 v1.0 正式版取代。
- [ ] **算子目录名待甲方确认**：甲方格式 `{SS}_{CATEGORY}_{MODALITY}_{SEQ3}`（PDF 实例 `057a_prohibited_goods_ecommerce`
      = 序号 + 模态缩写 `a` + 英文关键词）的 `SS`/`CATEGORY`/跨模态模态缩写取值，在甲方文档与工作区文件中均未给出
      （`平行语料算子开发背景与规则.md` 明确「未确认前不要自行编造」）；确认后统一改名并同步 `map.json` 与测试。
- [x] **2026-10-31 v1.0 正式版全量链路（872 终版）**：`crossmodal_v1.0_test` 用 `deliver_dataset_1023`
      （812 基线 + 60 条 AISHELL-3 音文 = 872 条，媒体 454）跑完 `#3 → #22` 全部 20 个算子
      （uid=`t5full_v2_872`，#8 开转写、#13 enabled、#16 原生视文路线；算子执行 1562.0 秒 / 约 26.0 分钟）：
      #22 产出 **samples=758 / tokens=758（一致）**、image_text 384 + video_text 374、`22_isolated_skipped=0`；
      #13 音文 44 通过/16 隔离/800 无音文对（AISHELL 子集 42 通过/10 隔离/0 待人工）；
      #16 399 对（图文 198 + 视文 201）→ 379 主清单/20 隔离/`not_applicable=0`；
      #19 入池 379/379；#20 质检 758（658 通过 + 100 flagged）。逐算子拦截统计见
      `crossmodal_v1.0_test/report_872_full.md`、`report_interceptions.md` 与本包
      `_v1.0正式版改造与测试报告.md`。HTTP 形态一协议冒烟 #3–#22 **20/20 status=1**。
- [x] **#16 视文路线（2026-10-31）**：`#16` 原生识别 `video+text`，消费 #14 的 `video_text_score`/
      `processing.14`，按 0.25/0.32 分档；图像仍最高优先，`video+image+text` 走图文；人工改写描述时
      用中间帧 rescore，无帧显式 pending。872 全量中视文 187 条 `score_source=video_text_score`、
      图文 192 条不变；协议/schema/config 0 diff。
- [x] **v1.0 单测（2026-10-31 终版）**：整改范围 `#3–#22` 共 20 套合计 **2302 passed / 684 skipped / 0 failed**
      （#16 当时新增 17 条视文用例后 203 passed/17 skipped；**2026-11-05 阈值联动/时序路线改动后为 231 passed/17 skipped**；逐算子独立 pytest；skip 均为既有 v4.2 血缘/运输停用等）；
      包级 `tests/` 3 passed / 35 skipped。
- [ ] 本机无 Docker CLI：`Dockerfile`/compose 未在本机构建容器；已做本地池化入口装载、`--preflight`、
      单算子 standalone、形态一协议冒烟与 872 全量链路验证；容器构建需在目标服务器补跑。
- [x] **单测状态（历史 2026-10-24）**：迭代范围 `#3–#21` 共 19 套 2150 passed / 653 skipped / 0 failed。
- [x] **2026-10-24 test7 全链路（历史）**：`crossmodal_test7\run_all.ps1 -Stage all` → `#3–#21` 19 阶段全部 code=0。
- [x] **2026-10-26 打包核验**：`python -X utf8 -B inference.py --preflight` → `operatorCount=26`/`okCount=26`；
      全量 580 个 `.py` 语法编译 0 错误；与开发目录 `crossmodal` 逐文件哈希一致；包内无 `__pycache__`/`.pytest_cache`/
      `_mty_transport`/`output/pytest_*` 等运行期残留（1023 文件 / 2283.4 MB）。
- [ ] **业务待决策（不影响本轮交付）**：#8 缺模态待补清单/退回口径；#21 处置下拉参数；#20 单开关+10% 子集补测；
      #10 长描述 `description_too_long` 退回无重试；#14 帧数新口径待确认；#17/#18 真实人工确认流程
      （#18 需下游 #25）待接平台。
- [ ] **环境验收缺口**：本机无 Docker CLI，容器构建需在目标服务器补跑；本机有 GPU（GTX 1660 Ti），
      CLIP/Whisper/YOLO 已在本地真实推理验证（#14 修复后长字幕 score_error 归零）。
- [x] **单测状态（历史 2026-10-12）**：3917 passed / 3 skipped（既有）/ 0 failed。
      2026-10-19 追加：`tests/`（任务级输出合并/校验/发布、26 包 mty_compliance 同步一致性、挂载点行为、血缘契约）
      35 条用例全绿；单算子自检 `--selftest` 9/9。
      上一轮 5 套 18 条「默认权重指向 `weight/` 后旧断言仍假定无权重必须降级」的失败已收尾：
      降级类用例显式把 `MODEL_ROOT` 指向空目录（用例名即 `*_unavailable` / `*_without_weights` 等），
      包内已随附权重导致「未配置权重」原始原因不可复现的个别断言按环境条件跳过；**未放宽任何业务断言**。
> 以下 MTY / 血缘 / 任务级输出条目自 v4.2（2026-10-19）起停用，仅作历史存档；当前交付不依赖 MTY 对象存储/关系库。

- [x] **MTY 规范缺口补齐（历史）**（2026-10-12，26 个算子统一注入 `utils/mty_compliance.py`，同一份实现）：
      `uniqueid=任务id_文件id` 强校验、血缘节点补 `isLastOperator`、阶段末产出 `stage_lineage.jsonl`
      （全部血缘拼接文本，一条记录一行）与 `stage_artifact.zip`、血缘/输出/阶段产物 OSS 归档、
      `mty_stage_relation` 关系入库（任务/源文件/血缘文件/阶段/区域(槽)/去向/输出文件）。
      未配置对象存储与库连接时退化为本地镜像 + 标准库 SQLite（`<算子目录>/_mty_transport/`），
      归档与入库失败**只记录、不抛异常**，不影响响应与产物；详见 `USAGE.md` 第 7.1 节，
      单算子自检 `python -m utils.mty_compliance --selftest`。
- [ ] 【v4.2 停用】OSS 密钥与关系库 DSN 待工程侧下发（`MTY_OSS_ENDPOINT/BUCKET/ACCESS_KEY_ID/ACCESS_KEY_SECRET`、
      `MTY_REGISTRY_DSN`）；未下发期间使用本地镜像目录与 SQLite，需人工同步上云或改配后重跑。
- [ ] 【v4.2 停用】`mty_stage_relation` 表在目标库中由工程侧预建（DDL：`python -m utils.mty_compliance --ddl`）。

## 血缘与阶段归档（HTTP/MTY）【v4.2 停用 · 历史存档】

> 自 v4.2（2026-10-19）起血缘/阶段归档/运输输出已停用；本节保留用于历史追溯，当前交付不产出这些文件。

每次算子运行会在 `runNNN/lineage/` 下按输入文件写入一个 `lineage_<uniqueId>.jsonl`。文件首行是 `type=file`，随后按执行顺序写入 `stage_segment`、阶段归档 `oss_supplement`、`outcome`，最终交付时追加 `deliver_supplement`。公开血缘中的输入/输出路径使用包内相对路径或对象存储 URI，不暴露宿主机绝对路径。

阶段末算子（#3、#8、#19、#21、#22、#23、#25、#26）会额外生成 `stage_lineage.jsonl` 和 `stage_artifact.zip`。ZIP 包含当前阶段文本输出、汇总血缘、`lineage/lineage_<uniqueId>.jsonl` 单文件血缘以及 `stage_artifact_manifest.json`；中间算子只生成单文件血缘和汇总血缘，不生成阶段 ZIP。血缘节点同时记录静态 `isLastOperator` 与请求中的 `requestIsLastOperator`，便于工程侧审计。

### 任务级最终输出（《输出格式.md》§3，2026-10-19 新增）【v4.2 停用 · 历史存档】

> 自 v4.2（2026-10-19）起任务级最终输出已停用；本节保留用于历史追溯。

每个输入源文件一份最终血缘，落在 `<输出根>/<任务id>/output/lineage/jsonl/lineage_<任务id>_<文件id>.jsonl`，由**每个大阶段的阶段末算子**（#3/#8/#19/#21/#22/#23/#25/#26）在 `/predict` 收尾时增量合并写入，中间算子不写。阶段末算子扫描同任务同阶段全部兄弟 run 目录，把本阶段所有算子节点合成一个 `stage_segment`；重跑同阶段整体替换（幂等）；M23 交付阶段同时把交付包复制到 `output/data/zip/`。实现：包根 `task_output.py`（约 700 行，含 `--replay-report` 验收回放），挂载点在各算子 `utils/mty_compliance.py` §4.1。环境变量：`TASK_OUTPUT_ENABLED`（默认 1）、`TASK_OUTPUT_ROOT`、`TASK_OUTPUT_SCAN_ROOT`、`TASK_OUTPUT_LOCK_TIMEOUT`（详见 `.env.example` 与使用文档 §3.4/§7.2）。
