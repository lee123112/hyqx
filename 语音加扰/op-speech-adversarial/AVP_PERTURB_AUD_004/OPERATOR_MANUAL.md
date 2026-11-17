# 语速变换加扰算子说明书（AVP_PERTURB_AUD_004）

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

| 项 | 内容 |
|---|---|
| 算子编号 | 04 |
| 算子名称 | 语速变换加扰 |
| 算子 ID | `avp_speed_perturb` |
| 目录 | `AVP_PERTURB_AUD_004` |
| 版本 | v1.0.0 |
| 所属阶段 | 样本生产（S1）第 4/6 步 |
| 输入 | `002_std.jsonl`（02 语音标准化产物） |
| 输出 | `004_batch.jsonl` / `004_speed_report.json` |

---

## 一、基础信息

- **业务位置**：语料入库 → 标准化 → 噪声混音 → **语速变换** → 样本质检 → 样本版本。
- **运行形态**：池服务内算子，由 `inference.py` 按 `operator` 路由；支持 CLI 单跑。
- **技术形态**：纯 DSP（librosa 相位声码器 + soundfile + numpy），无模型、无网络、无 GPU。
- **确定性**：同输入同参数（含 seed）→ 输出音频逐字节一致。
- **倍率枚举**：只允许 `0.8x / 1.0x / 1.2x`，由配置与参数校验双重锁定。

## 二、功能定义

| 倍率 | 效果 | 实现 |
|---|---|---|
| `0.8x` | 语速变慢，时长 ×1.25，音高不变 | 相位声码器 time-stretch |
| `1.0x` | 原样透传，输出与输入**逐字节一致** | 直接复制 WAV PCM16 文件字节 |
| `1.2x` | 语速变快，时长 ÷1.2，音高不变 | 相位声码器 time-stretch |

- `per_value=true`：每条语音 × 每个倍率各产一条；`false`：每条语音随机取一个倍率；
- 加工范围：全部样本 / 指定前 N 条（`1 ≤ N ≤ 样本数`）；
- 每条样本回报**输出时长误差**（相对 `源时长/倍率` 的百分比），超过 1% 计违规。

**明确不做**：混音（03 算子）、口音加扰（已移除）、格式/采样率转换、音高变换、响度归一化。

## 三、输出内容描述

### 3.1 `004_batch.jsonl`（每行一个样本，字段固定）

```json
{
  "sample_id": "utt_001__speed__1p2x__0003",
  "source_uttid": "utt_001",
  "dataset_id": "DS-20261102-0001",
  "audio_path": "/data/t1/tmp/004/004_audio/utt_001__speed__1p2x__0003.wav",
  "text": "请帮我查询本月账单",
  "duration": 2.083,
  "perturb": {
    "dimension": "speed",
    "value": "1.2x",
    "snr_db": null,
    "speed": 1.2,
    "asset": null
  }
}
```

`perturb` 是参数快照：维度固定 `speed`、倍率取值/数值、`snr_db`/`asset` 固定 null。
下游 05 质检据此核对「声明的变速倍率」与「实际音频时长」是否一致。

### 3.2 `004_speed_report.json`

- 参数快照（values/scope/seed/per_value）；
- 统计（输入、选中、输出、失败、时长误差均值/最大/阈值/违规数、1.0× 透传核对数）；
- 分倍率汇总（计数、最大误差、透传一致数）；
- `speed.duration_error` 与 `speed.passthrough` 的**口径定义**；
- 逐条样本明细（源时长、期望时长、实际时长、误差、是否透传/是否一致）；
- `rejects`（解码/写盘失败）与 `conclusion`（success/partial）。

## 四、技术方案

1. **相位声码器变速不变调**：`common.audio.time_stretch` 内部用 STFT 帧率缩放 + 相位累积重建，
   只改时长、不改音高；直接重采样会同时改变音高，不是业务要的"语速"。
2. **1.0× 文件级透传**：解码→去直流→PCM16 重写会引入 ±1 LSB 漂移，无法满足"逐字节一致"；
   因此 1.0× 直接复制 02 写出的 WAV 文件字节，并用 sha256 核对，报告透传通过率。
3. **时长误差实测**：输出时长从**落盘文件头**读取（不是数组长度），期望值为 `源时长/倍率`，
   误差按百分比逐条计算；1.0× 理论 0，1.2× 因帧取整约 0.002%。
4. **确定性**：`time_stretch` 是确定性算法；`per_value=false` 的倍率选择由 `seed` 驱动。
5. **诚实降级**：1.0× 输入若不是 16bit PCM WAV，退化为重新编码并标记 `passthrough_identical=false`，
   批次 `partial`，不伪装成逐字节一致。

## 五、技术实现

| 环节 | 实现 |
|---|---|
| 入口 | `processor.py::run(ctx)` → `{outputs, statistics, result, status}` |
| 参数校验 | `common.schemas`；倍率枚举/重复/空/scope 全部 fail fast |
| 音频 | `common.audio.file_info / load_raw / time_stretch / write_pcm16 / file_duration` |
| 1.0× | `shutil.copyfile` + `sha256_file` 核对 |
| 输出 | `004_audio/<sample_id>.wav` + `004_batch.jsonl` + `004_speed_report.json` |
| 原子写 | `common.iofs.write_jsonl / write_json` |
| 日志 | `common.logger.RunLog`（路由/校验/读取/处理/输出/警告/完成） |
| 错误口径 | 单条解码/写盘失败 → `rejects` + `partial`；整批 0 产出 → 抛错 |

## 六、资源要求

- **CPU**：单线程。稳态 20 条×3s×3 倍率（60 条输出、185s 音频）1.46s，约 **41× 输入实时**；
  仅 1.0× 纯复制 20 条 0.166s，约 **361× 实时**；
- **冷启动**：首次调用 librosa/scipy 约 7–8.5s（一次性导入开销，服务常驻后摊薄）；
- **内存**：DSP 库导入基线约 150MB，实测峰值增量约 159MB；单条音频数据量远小于此；
- **磁盘**：输出 WAV ≈ 32 KB/s（16k 单声道 PCM16），0.8× 文件更大、1.2× 更小；
- **环境**：1 CPU / 1 GB 容器可跑；无 GPU、无网络、无模型。

## 七、性能指标

| 指标 | 实测值 | 测量方式 |
|---|---|---|
| 输出时长误差（均值） | **0.000363%** | 18 条测试样本 |
| 输出时长误差（最大） | **0.002%** | 阈值 1%，违规 0 条 |
| 独立复核误差 | **0.000363% / 0.002%** | 从报告明细重算 |
| 1.0× 逐字节透传 | **6/6（100%）** | 文件字节直接比对 |
| 输出条数 | 18（6×3） | 失败 0 条 |
| 稳态单条延迟 | ≈24 ms（60 条 / 1.46s） | 16k、3s 语音 |
| 测试 | **27 passed** | `pytest tests/ -q` |

## 八、输入输出示例

### 请求

```python
import requests
r = requests.post("http://127.0.0.1:8000/predict", json={
    "operator": "AVP_PERTURB_AUD_004",
    "uniqueId": "t1",
    "inputFile": "/data/t1/tmp/002/002_std.jsonl",
    "params": {"values": ["0.8x", "1.0x", "1.2x"], "per_value": True, "seed": 20261102},
}, timeout=600)
print(r.json()["data"]["statistics"]["duration_error_max_pct"])
```

### 响应关键字段

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "status": "success",
    "statistics": {"output": 6, "duration_error_mean_pct": 0.000267,
                   "duration_error_max_pct": 0.0016, "duration_error_violations": 0,
                   "passthrough_checked": 2, "passthrough_identical": 2,
                   "passthrough_identical_rate": 1.0},
    "outputs": {"batch": "/data/t1/tmp/004/004_batch.jsonl",
                "report": "/data/t1/tmp/004/004_speed_report.json"}
  }
}
```

### 失败示例

```json
{"code": 1, "msg": "invalid_params: values 取值 '1.5x' 非法，允许：0.8x / 1.0x / 1.2x"}
```
