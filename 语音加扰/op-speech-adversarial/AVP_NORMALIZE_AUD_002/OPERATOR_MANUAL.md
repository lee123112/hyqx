# 语音标准化算子说明书（AVP_NORMALIZE_AUD_002）

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

| 项 | 内容 |
|---|---|
| 算子编号 | 02 |
| 算子名称 | 语音标准化 |
| 算子 ID | `avp_normalize_audio` |
| 目录 | `AVP_NORMALIZE_AUD_002` |
| 版本 | v1.0.0 |
| 所属阶段 | 样本生产（S1）第 2/6 步 |
| 输入 | `001_corpus.jsonl`（01 语料入库产物） |
| 输出 | `002_std.jsonl` / `002_dropped.jsonl` / `002_normalize_report.json` |

---

## 一、基础信息

- **业务位置**：语料入库 → **标准化** → 加扰加工 → 样本质检 → 样本版本。
- **运行形态**：池服务内的一个算子，由 `inference.py` 按 `operator` 路由；同时支持 CLI 单跑。
- **技术形态**：纯 DSP + 规则，无模型、无网络、无 GPU；单线程即可。
- **依赖**：Python 3.9、numpy、scipy、librosa、soundfile、soxr（已在本环境安装）。
- **确定性**：同输入 + 同参数 → 同输出；全程无随机数。

## 二、功能定义

把任意来源的语音统一成**规则可引用的标准语料**，规则固定四件事：

| 规则项 | 取值 | 本算子行为 |
|---|---|---|
| 采样率 | 16000 / 8000 Hz | 按规则重采样；原生已一致则跳过 |
| 声道 | 单声道 | 多声道取均值下混，再走后续流程 |
| 目标格式 | WAV（PCM 16bit） | 统一 `write_pcm16` 落盘 |
| 时长范围 | `[min_duration, max_duration]` 秒 | **超范围直接剔除，不裁剪**，写入 `002_dropped.jsonl` |

**明确不做**：不裁剪、不补静音、不做响度归一化/降噪/VAD；不判断文本质量；不修改 01 产物。

## 三、输出内容描述

### 3.1 `002_std.jsonl`

= 01 字段（`dataset_id/uttid/audio_path/text/duration/created_at/...`）+ 标准化字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `std_audio_path` | string | 标准化后 WAV 真实路径 |
| `sample_rate` | int | 16000 / 8000 |
| `channels` | int | 固定 1 |
| `format` | string | 固定 `wav_pcm16` |
| `std_duration` | float | 标准化后真实时长（秒，3 位小数，附加字段） |

### 3.2 `002_dropped.jsonl`

`{dataset_id, uttid, audio_path, text, duration, reason, min_duration, max_duration}`，
`reason ∈ {below_min_duration, above_max_duration}`，用于任务详情展示「剔除数」。

### 3.3 `002_normalize_report.json`

参数快照、统计、格式合规率、时长误差、盲估 SNR 汇总、逐条样本明细、剔除/失败明细（上限 200 条）。
`statistics` 与接口返回的 `data.statistics` 完全一致。

## 四、技术方案

**核心思路：以"解码后的真实音频"为唯一真相，用规则做确定性判定，用写盘后复核做自证。**

1. **解码与预处理**：libsndfile 解码为 float32；多声道按声道均值下混；去直流。去直流避免
   DC 偏置被后续 PCM16 量化继承，也统一全产线口径。
2. **时长判定用真值**：`duration = 解码采样点数 / 原生采样率`，不信任清单里的旧 `duration`。
   超过上下界（含 1ms 容差吸收帧取整噪声）→ 剔除，不裁剪。这样"清单写 3s、实际 1.5s"的脏数据
   不会漏进加扰阶段。
3. **重采样**：librosa + soxr（`soxr_hq`）。soxr 是带限 sinc 类高质量重采样，相比线性插值
   能抑制 8k/16k 边界附近的混叠，保证后续加扰/评测听到的是"真语音"而不是混叠伪影。
4. **PCM16 限幅**：峰值超过 0.99 时信号**等比缩放**（不削波），保持波形比例，不引入谐波失真。
5. **写盘后复核**：重新读文件头，逐条核验采样率/声道/容器/PCM16，得到真实的格式合规率。
   这是"自证"而不是"自报"：如果写出逻辑坏了，合规率会掉下来。
6. **盲估 SNR**：无干净参考时用帧能量分位数 + 最小值统计 + 非语音帧迭代重估（minimum statistics），
   比固定分位数法更能穿过语音间隙；但仍是**盲估计**，报告标 `estimated` 并写明偏差范围，只作补充
   质量信号。

**与"纯阈值法"的区别**：本算子的阈值只负责**去留判定**，音频质量由真实解码 + 带限重采样 +
写盘复核保证；不是"读清单 → 复制文件 → 声称达标"。

## 五、技术实现

| 环节 | 实现 |
|---|---|
| 入口 | `processor.py::run(ctx)`，返回 `{outputs, statistics, result, status}` |
| 参数校验 | `common.schemas`（`require/enum/as_int/as_float/as_bool`），非法即 `ParamError → invalid_params:` |
| 输入定位 | 文件 / 目录（优先同名 `001_corpus.jsonl`，其次唯一 jsonl）/ `work_dir/input` 兜底 |
| 音频 IO | `common.audio.load_raw / resample / write_pcm16 / file_info / estimate_snr_db` |
| 文件写 | `common.iofs.write_jsonl / write_json`（临时文件 + `os.replace` 原子写） |
| 日志 | `common.logger.RunLog`，按《日志规范模板》逐行输出（无多行/堆栈/绝对路径） |
| 输出目录 | `ctx.out("002_std.jsonl")` 等；音频在 `ctx.out("002_audio/")` 下 |
| 错误口径 | 单条失败计入 `rejects` 并继续（`partial`）；整批无输入才抛错；不伪造成功 |

## 六、资源要求

- **CPU**：单线程；libsndfile + numpy 为主，重采样调用 soxr。16k 目标时 20 条×3s 用时 0.441s
  （≈136× 实时）；44.1k→16k 重采样 20 条×5s 用时 3.366s（≈30× 实时）。
- **内存**：逐条处理，不整批驻留；峰值增量 16k 场景约 12.5 MB，44.1k 高质量重采样约 116 MB。
- **磁盘**：输出 WAV 大小 ≈ 采样率 × 时长 × 2 字节（16k 单声道约 32 KB/s）。
- **运行环境**：1 CPU / 512 MB 容器可跑；无 GPU、无网络、无模型文件。

## 七、性能指标

| 指标 | 实测值 | 测量方式 |
|---|---|---|
| 格式合规率 | **100%** | 7 条合成测试集，5 条入批全部复核通过 |
| 剔除判定准确率 | **100%** | 7/7 条 keep/drop 与真实时长真值一致（2 条超范围剔除） |
| 时长误差（均值） | **0.000009 s** | 标准化前后时长差 |
| 时长误差（最大） | **0.000047 s / 0.0021%** | 重采样帧取整引入 |
| 单条延迟 | 约 20–25 ms（3s@16k） | CLI/基准实测 |
| 测试 | **29 passed** | `pytest tests/ -q` |

> 盲估 SNR 均值 87.4 dB 是合成语音（音节间数字静音）造成的，不代表真实录音质量，列表里不作为验收指标。

## 八、输入输出示例

### 输入 `001_corpus.jsonl`（1 行）

```json
{"dataset_id":"DS-20261102-0001","uttid":"utt_001","audio_path":"/data/t1/input/audio/utt_001.wav","text":"请帮我查询本月账单","duration":2.5,"sample_rate":22050,"channels":1,"format":"wav","created_at":"2026-11-02T10:00:00+08:00"}
```

### 输出 `002_std.jsonl`（1 行）

```json
{"dataset_id":"DS-20261102-0001","uttid":"utt_001","audio_path":"/data/t1/input/audio/utt_001.wav","text":"请帮我查询本月账单","duration":2.5,"sample_rate":16000,"channels":1,"format":"wav_pcm16","created_at":"2026-11-02T10:00:00+08:00","std_audio_path":"/data/t1/tmp/002/002_audio/utt_001.wav","std_duration":2.5}
```

### 剔除示例 `002_dropped.jsonl`

```json
{"dataset_id":"DS-20261102-0001","uttid":"utt_003","audio_path":"/data/t1/input/audio/utt_003.wav","text":"太短的样本","duration":1.5,"reason":"below_min_duration","min_duration":2.0,"max_duration":10.0}
```

### 接口调用

```python
import requests
r = requests.post("http://127.0.0.1:8000/predict", json={
    "operator": "AVP_NORMALIZE_AUD_002",
    "uniqueId": "t1",
    "inputFile": "/data/t1/input/001_corpus.jsonl",
    "params": {"sample_rate": 16000, "channels": 1,
               "min_duration": 2.0, "max_duration": 10.0},
}, timeout=600)
print(r.json()["data"]["statistics"])
```
