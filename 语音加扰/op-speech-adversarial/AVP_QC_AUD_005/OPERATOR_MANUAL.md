# 05 样本质检 · 算子说明书（OPERATOR_MANUAL）

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 样本质检 |
| 算子英文 ID | `avp_inspect_batch` |
| 算子目录 | `AVP_QC_AUD_005` |
| 正式编号 | 5 |
| 版本 | v1.0.0 |
| 所属阶段 | S1 样本生产 · 加扰（03/04）之后、版本（06）之前 |
| 上游输入 | 03 噪声混音加扰 / 04 变速加扰的批次表（`003_batch.jsonl` / `004_batch.jsonl`） |
| 下游输出 | 06 样本版本：`005_batch_final.jsonl` + `005_qc_final.json` |
| 运行形态 | FastAPI `/predict` 单算子调用；也支持 CLI 单算子冒烟 |
| 依赖 | Python 3.9+、numpy、soundfile、PyYAML；**无模型、无 GPU、无网络** |

---

## 二、功能定义

本算子负责「加扰后的整批样本能不能进入版本」这一道关，分两个动作：

### 2.1 `mode=start`：全量自动检查 + 人工抽样

1. 读取批次表，逐条做**完整性检查**：
   - 7 个契约字段是否齐全；
   - 音频文件是否存在、能否解码；
   - 解码后是否为空音频（RMS 低于阈值）；
   - 实际解码时长是否大于 0。
2. 逐条做**信噪比检查**：
   - 有干净参考 → 按定义实测 `10·log10(P_signal/P_noise)`；
   - 无干净参考 → 盲估，并明确标注 `snr_source="estimated"`。
3. 计算整批是否自动通过：
   - 完整性失败 = 0；
   - 低于 SNR 阈值的样本占比 ≤ 上限（默认 20%，分母 = 输入样本数）。
4. 自动通过 → 按 `max(20, ceil(N×5%))` 抽样生成人工试听队列，结论 `待复核`；
   自动不通过 → 结论 `不通过`，队列为空，整批退回重做。

### 2.2 `mode=finalize`：回填人工判定，形成最终样本表

1. 读取 `review_decisions`（每条 `{sample_id, decision: pass|reject}`）；
2. 校验队列里**每一条**都已判定，缺一条就拒绝提交（`human_review_incomplete:`）；
3. 未知 `sample_id`、重复判定、非法 decision 一律 `invalid_params:`；
4. 生成 `005_batch_final.jsonl`（保留剔除样本并标 `qc.status=rejected`）与 `005_qc_final.json`；
5. 结论：自动检查不通过或「整批退回」→ `不通过`；人工剔除只让对应样本 `rejected`，
   不计入版本，但不改变批次结论（批次仍可通过，版本样本数相应减少）。

### 2.3 明确不做的

- 不自动判断「加扰可听性」，只回填人工结论；
- 不做音频转码/重采样/裁剪；
- 不做入库检测（短音频、空音频、文本重复由 01 负责，且只提示不阻断）；
- 不做版本打包/发布/回滚；不做权限、不做多人复核。

---

## 三、输出内容描述

| 文件 | 何时产出 | 说明 |
|---|---|---|
| `005_qc_report.json` | start | 自动检查报告：逐条结果、统计、抽样、结论 |
| `005_review_queue.jsonl` | start（自动通过时） | 人工试听队列；自动不通过时为空文件 |
| `005_autocheck.json` | start（内部状态） | finalize 复用键；非对外契约 |
| `005_batch_final.jsonl` | finalize | 下游 06 的唯一输入；含 `qc` 4 键快照 |
| `005_qc_final.json` | finalize | 批次质检结论 + 人工复核统计 |

关键字段：

- `qc.status`：`pass` / `rejected`；剔除样本仍保留在表里，由 06 打包时排除，保证「版本为什么少一条」可追溯；
- `qc.snr_db` / `qc.snr_source`：自动检查值及其来源（实测/盲估），人工只判可听性，不改 SNR；
- `conclusion`：`待复核` / `通过` / `不通过`；
- `statistics`：与接口返回完全一致，前端可直接渲染。

---

## 四、技术方案

### 4.1 参考感知的 SNR 计量

行业常见做法是「用工具算一个 SNR 塞进报告」，但那样有两个致命问题：一是没有干净参考时
工具会静默给个估计值，二是估计值会被当成实测值使用。本算子的方案是：

1. **先判来源**：从 `perturb` 或行顶层按 6 个别名找干净参考；
2. **有参考必须实测**：`A.snr_db(clean, mixed-clean)`，严格按均方值定义；
3. **无参考才盲估**：`A.estimate_snr_db()`（谱减 + 帧能量迭代），并写 `snr_source="estimated"`；
4. **参考坏了要记账**：写了参考但解码失败 → 退回盲估并计入 `measure_fallback`，不静默冒充实测。

这样评测阶段（07）看到 `estimated` 就知道这条的 SNR 不可与实测样本直接横比，
避免把「盲估误差」误判成「模型鲁棒性问题」。

### 4.2 两遍式状态机，而不是一次调用搞定

人工试听天然是异步的：start 之后可能几小时后才提交判定。因此：

- start 把逐条检查结果落 `005_autocheck.json`；
- finalize 用**输入 JSONL 的 SHA-256** 判断能否复用；一致就复用，避免二次解码；
- 不一致就重算并写 warning，保证结论永远基于当前输入；
- 人工判定必须全量覆盖，接口层直接阻断「没听完就提交」。

### 4.3 阈值不是拍脑袋

- SNR 阈值 15 dB 与任务书评测口径一致，写在 `configs/config.yaml`，不在代码里写死；
- 低于阈值占比上限 20% 是「整批退回」的业务线，保证少量疑难样本不会毁掉整批；
- 空音频阈值 1e-5（约 -100 dBFS）用 RMS 而不是全零判断，能覆盖带底噪的空录音；
- 抽样下限 20 条、比例 5% 是业务硬口径，参数可覆盖但不改默认。

### 4.4 与「纯阈值法」的区别

| 维度 | 纯阈值法 | 本算子 |
|---|---|---|
| 判定解释 | 只有过/不过 | 5 类问题码 + 逐样本原因 |
| SNR 来源 | 不区分实测/盲估 | 强制标注，且记录参考失败回退 |
| 人工环节 | 容易漏判 | 全量覆盖校验，缺一条不能提交 |
| 结果复现 | 随机抽样不可复现 | seed + 固定排序，同输入同输出 |
| 版本衔接 | 剔除样本直接消失 | 保留 `rejected` 行，06 可解释差异 |

---

## 五、技术实现

| 文件 | 职责 |
|---|---|
| `processor.py` | 参数校验、批次读取、完整性/SNR 检查、抽样、状态读写、finalize、日志 |
| `configs/config.yaml` | 全部阈值/系数/文件名/字段别名，唯一配置源 |
| `utils/__init__.py` | 工具包占位（本算子无额外工具函数） |
| `tests/` | processor 层 31 例 + 接口层 3 例 |
| `dataset/` | 字段契约 + 3 条可运行小样本 |
| `output/model/` | 真实指标与配置快照 |

实现要点：

- **确定性**：抽样只用 `numpy.random.default_rng(seed)`；同输入 + 同 seed 两次输出逐字节一致；
- **不吞错**：坏行/坏音频记为逐条问题并计数；整批无法处理才抛错；
- **不静默降级**：参考解码失败会记账 + warning，不会假装实测成功；
- **无副作用**：只读音频、只写输出目录，不改上游文件；
- **日志**：走 `ctx.log`，start/finalize/等待人工/部分完成措辞不同，单行 ≤160 字。

---

## 六、资源要求

| 项 | 要求 |
|---|---|
| CPU | 单核即可；推荐 2 核以上便于服务层并发调用其他算子 |
| 内存 | 100 条 1s/16k 音频约 37 MB（含解释器）；建议容器 ≥512 MB |
| 磁盘 | 产物为 JSON/JSONL，百条级 <1 MB；音频只读不复制 |
| GPU | 不需要 |
| 网络 | 不需要 |
| 系统依赖 | libsndfile（soundfile 自带）；不需要 ffmpeg |

---

## 七、性能指标

实测环境：Windows，Python 3.9.7，numpy 1.26.4，单进程顺序执行，12 逻辑核。

| 指标 | 实测值 |
|---|---|
| 100 条（全部带干净参考）start 耗时 | 均值 **466.4 ms**（3 次；最小 427.9 ms，最大 513.5 ms） |
| 单条平均耗时 | **4.66 ms/条** |
| 处理完成后 RSS | **36.5 MB**（基准 35.2 MB） |
| 完整性判定准确率 | **40 / 40 = 100%**（30 正常 + 10 已知缺陷） |
| SNR 实测判定准确率 | **12 / 12 = 100%**；误差均值 0.0003 dB，最大 0.001 dB |
| SNR 盲估判定准确率 | **6 / 6 = 100%**（目标 2/3/5/25/27/30 dB） |
| 抽样条数 | N=10→20、N=100→20、N=1000→50；N=100 真实队列 20 |
| 单测 | 34 passed（31 processor + 3 api） |

> 数据集为程序化合成类语音，不是真实业务录音；数字证明实现正确性，不代表真实分布表现。

---

## 八、输入输出示例

### 8.1 start 请求

```json
{
  "operator": "AVP_QC_AUD_005",
  "uniqueId": "qc005_demo",
  "params": { "mode": "start" }
}
```

### 8.2 start 响应（真实冒烟，节选）

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "operator": "avp_inspect_batch",
    "status": "partial",
    "statistics": {
      "input_samples": 3, "integrity_failed": 0,
      "snr_measured": 2, "snr_estimated": 1, "snr_below_threshold": 0,
      "sample_size": 20, "sample_size_actual": 3, "conclusion": "待复核"
    },
    "outputs": {
      "report": "/data/qc005_demo/tmp/005/005_qc_report.json",
      "review_queue": "/data/qc005_demo/tmp/005/005_review_queue.jsonl"
    },
    "result": {"mode": "start", "conclusion": "待复核", "auto_passed": true}
  },
  "logs": ["[2026-11-02 14:15:40] [等待] 人工试听复核 3 条（抽样比例 5.0%，最少 20 条）"]
}
```

### 8.3 finalize 请求

```json
{
  "operator": "AVP_QC_AUD_005",
  "uniqueId": "qc005_demo",
  "params": {
    "mode": "finalize",
    "review_decisions": [
      {"sample_id": "sample_001", "decision": "pass"},
      {"sample_id": "sample_002", "decision": "pass"},
      {"sample_id": "sample_003", "decision": "reject"}
    ]
  }
}
```

### 8.4 finalize 响应（节选）

```json
{
  "code": 0,
  "data": {
    "status": "success",
    "statistics": {
      "review_queue": 3, "reviewed": 3,
      "review_passed": 2, "review_rejected": 1,
      "passed": 2, "rejected": 1, "version_sample_count": 2,
      "conclusion": "通过"
    },
    "outputs": {
      "batch_final": "/data/qc005_demo/tmp/005/005_batch_final.jsonl",
      "qc_final": "/data/qc005_demo/tmp/005/005_qc_final.json"
    }
  }
}
```

### 8.5 失败示例

```json
{"code": 1, "msg": "human_review_incomplete: 人工复核未完成：队列 3 条，仅判定 2 条（缺 1 条：sample_003）"}
```

```json
{"code": 1, "msg": "invalid_params: 参数 snr_threshold_db 必须是数字：'高'"}
```
