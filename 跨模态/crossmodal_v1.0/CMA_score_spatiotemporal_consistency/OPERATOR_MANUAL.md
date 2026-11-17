# 算子 15 评分时空一致性 · 说明书

## 一、表1 条目对照

| 表1 #15 要求 | 本实现 | 位置 |
| --- | --- | --- |
| 类型：规则型 | 纯规则，无模型、无网络、不解码音视频 | `utils/temporal.py` |
| 系统位置 S3 数据加工 | `stage_id: S3` | `configs/config.yaml` |
| 默认关闭 | `enabled: false`；且 `/predict` 不得反转为默认开启 | `configs/config.yaml` |
| 可选 / 优先级 P1 | 开关打开即启用；关闭时样本原样透传 | `inference.run` |
| 输入：视频帧时间戳、语音片段时间戳、字幕/文本时间戳 | `timeline.{video,audio,text}`，或 `timeline_file` sidecar | `utils/temporal.resolve_spec` |
| 处理：线性插值校准时间差 | `LinearTimeMap`（分段线性 / 全局最小二乘） | `utils/temporal.LinearTimeMap` |
| 阈值：画面/声音/字幕时间差默认不超过 0.5 秒（0.2–2.0） | `max_time_delta_sec=0.5`，定义域 0.2–2.0 强校验 | `utils/schemas.py` |
| 三者缺一 → 不适用，不隔离，不能写成通过 | `status=not_applicable`、`passed=null`、留样本池 | `utils/temporal.evaluate_record` |
| 纯图文不执行时序判定 | 无时间轴 → `reason_code=no_timestamp_timeline` | 同上 |
| 不做目标检测 / OCR / 长文本配多图 | 无相关代码路径 | — |
| 输出：时序通过、不通过或不适用 | `temporal_alignment.status` 三值（+ 可选的待人工校准流程态） | `dataset/DATA_SCHEMA.md` §4 |
| 超过阈值 → 样本隔离库清单（标签 样本-语义不通） | `15_sample_quarantine.jsonl`，交 #21 | `utils/temporal.LABEL_FAIL` |
| 统计：input = output + rejected，旁路 flagged 单列 | 4 条守恒式，`conservation_ok` | `inference._conservation` |
| 无模型 | `statistics.model = null`，`models_available = false` | `inference.run` |

## 二、一分钟流程

```text
JSONL 记录
  ├─ 缺 sample_id ────────────────────────► 原始隔离台账（原始文件-缺字段）
  ├─ 路线关闭 ────────────────────────────► 透传（processing["15"]=skipped）
  └─ 路线打开
       ├─ 时间戳/绑定非法、sidecar 不可读 ─► 原始隔离台账
       ├─ 三段时间轴缺一 / 纯图文 / 无绑定 ─► not_applicable（留样本池，不隔离）
       ├─ 锚点不足且 require_calibration ──► 待人工校准台账（留产线，不隔离）
       └─ 线性插值校准后取逐绑定最大时间差
            ├─ ≤ 阈值（含等号） ──────────► passed（留样本池）
            └─ > 阈值 ───────────────────► 样本隔离台账（交 #21）
```

## 三、关键不变量

1. `not_applicable` 的 `passed` 恒为 `null`，**永不写入 `true`**。
2. 未校准时 `interpolation.applied=false`，判定用的是**真实**原始时间差，不伪造校准结果。
3. `statistics.model` 恒为 `null`；不存在固定分数、随机分数或伪造的模型分数。
4. 输出只写 `OUTPUT_DIR/<uniqueId>/`；`uniqueId` 拒绝空、路径字符、`..`、盘符、保留设备名。
5. 产物先写 `.partial` 再 `os.replace` 原子提交，输入输出同路径也不会截断源文件。
6. 坏行、缺字段、编码异常**一律进台账并计数**，绝不静默丢弃。
7. 阈值 0.5 s 的中间漂移标定（2026-11-05）：`03_m15_drift_calibration.jsonl` 12 条
   （4 组窗口 × 0.3/0.5/0.7 s）实测 0.3 s、0.5 s 通过，0.7 s 隔离；0.5 s 为闭区间
   `<=`，等号边界通过，默认阈值保持不变。

## 四、接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/uploadFile?uniqueId=` | 上传单个素材或含 `manifest.json` 的 zip（安全解压） |
| POST | `/predict` | 双协议：`multipart/form-data` 走形态一（单文件流）；`application/json` 走形态二（阶段文件传递） |
| POST | `/removeFile?uniqueId=` | 幂等清理任务目录 |

- 外层信封：`{"code": 0|1, "msg": "...", "data": {...}}`
- `isLastOperator` 仍必传（严格布尔），用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile`（`crossmodal-files-v1`）+ `result`（业务摘要），不再用 `false` 只回血缘对象
- 形态一返回 `status`（int：判定型 1 通过/不适用，0 不通过）+ `process_status`（`success` / `failed` / `not_applicable` / `pending_review` / `route_disabled`）/`pair_id`/`applicable`/`max_delta_sec`/`threshold_sec`/`decision`/`interpolation_method`
- **未实现 `/downloadFile`**（正式接口只有三个）

## 五、交付核验清单

- [x] 业务模块 `utils/temporal.py`、入口 `inference.py`、`configs/config.yaml`、`utils/`、测试、样例
- [x] `dataset/DATA_SCHEMA.md`、`README.md`、`requirements.txt`、`Dockerfile`、`map.json`
- [x] 空的 `output/`
- [x] 三接口（无 `/downloadFile`）、统一信封、`isLastOperator` 两种调用响应同构
- [x] 形态二统一响应含 `operator`/`status`/`outputFile`/`result`；统计/配置快照由 `data.result`、`manifest.json`、`run.log` 承载
- [x] 统计守恒、流式处理、异常台账、UTF-8 输出、原子替换
- [x] `python -m pytest tests/ -q` → 175 passed
- [x] `python -m compileall -q .`、`python -m json.tool map.json`
