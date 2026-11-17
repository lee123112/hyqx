# 噪声混音加扰算子说明书（AVP_PERTURB_AUD_003）

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

| 项 | 内容 |
|---|---|
| 算子编号 | 03 |
| 算子名称 | 噪声混音加扰 |
| 算子 ID | `avp_mix_noise` |
| 目录 | `AVP_PERTURB_AUD_003` |
| 版本 | v1.0.0 |
| 所属阶段 | 样本生产（S1）第 3/6 步 |
| 输入 | `002_std.jsonl`（02 语音标准化产物） |
| 输出 | `003_batch.jsonl` / `003_perturb_report.json` |

---

## 一、基础信息

- **业务位置**：语料入库 → 标准化 → **噪声混音加扰** → 语速变换 → 样本质检 → 样本版本。
- **运行形态**：池服务内算子，由 `inference.py` 按 `operator` 路由；支持 CLI 单跑。
- **技术形态**：纯 DSP（numpy + soundfile + librosa/soxr），无模型、无网络、无 GPU。
- **确定性**：`seed` 固定时，目标 SNR、噪声裁剪位置、输出音频逐字节可复现。
- **素材**：仓库内置程序化合成噪声（`weight/assets`），支持通过 `assets_dir` 替换为真实素材。

## 二、功能定义

按「加扰维度 + 取值 + SNR 区间」生成对抗样本：

| 维度 | 取值（仓库默认） | 素材位置 |
|---|---|---|
| `noise`（噪声） | 白噪、人群嘈杂、交通噪声 | `weight/assets/noise/<取值>.wav` |
| `env`（环境音） | 街道、车内 | `weight/assets/env/<取值>.wav` |

- 每条样本在 `[下界, 上界]` dB 内取一个目标 SNR，按功率反算噪声增益；
- `per_value=true`：每条语音 × 每个取值各产一条；`false`：每条语音随机取一个取值；
- 加工范围：全部样本 / 指定前 N 条（`1 ≤ N ≤ 样本数`）。

**明确不做**：口音加扰（已从业务移除）、变速（04 算子）、混响/滤波/降噪、格式转换。

## 三、输出内容描述

### 3.1 `003_batch.jsonl`（每行一个样本，字段固定）

```json
{
  "sample_id": "utt_001__noise__0001",
  "source_uttid": "utt_001",
  "dataset_id": "DS-20261102-0001",
  "audio_path": "/data/t1/tmp/003/003_audio/utt_001__noise__0001.wav",
  "clean_audio_path": "/data/t1/tmp/002/002_audio/utt_001.wav",
  "text": "请帮我查询本月账单",
  "duration": 2.5,
  "perturb": {
    "dimension": "noise",
    "value": "白噪",
    "snr_db": 11.8,
    "speed": null,
    "asset": "/app/weight/assets/noise/白噪.wav"
  }
}
```

`perturb` 是**参数快照**：维度、取值、目标 SNR、变速倍率（03 固定 null）、素材路径。
下游 05 质检据此核对「样本声明的加扰参数」与「实际音频」是否一致。

### 3.2 `003_perturb_report.json`

- 参数快照（dimension/values/snr 区间/assets_dir/scope/seed/per_value）；
- 统计（输入、选中、输出、失败、SNR 误差均值/最大/阈值/违规数、目标区间）；
- 每个取值的素材路径与计数；
- `snr_validation`：实测定义、均值/最大误差、阈值、违规数；
- 逐条样本 `target/applied/error` 明细（上限 200 条）；
- `rejects`（静音/解码/写盘失败）与 `conclusion`（success/partial）。

## 四、技术方案

1. **按 SNR 反算增益**，而不是固定倍数叠加：
   `scale = sqrt(P_sig / (10^(SNR/10) * P_noise))`，再把 `scale * noise` 加到干净信号上。
   这样无论素材电平高低，实际 SNR 都落在业务指定值。
2. **峰值保护不破坏 SNR**：混音后峰值超过 0.99 时，信号与噪声同乘一个系数，波形比例不变，
   因此 SNR 不变、也不会削波产生谐波。
3. **实测而非回填**：`merge_at_snr` 在真实参与混音的信号/噪声分量上计算
   `10*log10(P_sig/P_noise)`，返回 `applied_snr_db`；算子逐条记录 `applied - target`，
   超过 0.5 dB 即违规。目标值只用于控制，不作为"实测结果"复述。
4. **素材缓存**：每个取值只解码一次并缓存（含重采样到语音采样率），避免批内重复 IO。
5. **确定性随机**：`np.random.default_rng(seed)` 同时驱动「区间内取 SNR」和「掩码起始位置」，
   替换/重跑批次可复现。
6. **落盘复核**：时长从写出的 WAV 文件头读取，不用输入值冒充输出值。

## 五、技术实现

| 环节 | 实现 |
|---|---|
| 入口 | `processor.py::run(ctx)` → `{outputs, statistics, result, status}` |
| 参数校验 | `common.schemas`；维度/取值/SNR 区间/scope 全部 fail fast |
| 素材校验 | 取值 → `<assets_dir>/<取值>.wav` 必须存在且可解码、非静音，否则整批拒绝 |
| 混音 | `common.audio.merge_at_snr(sig, noise, snr_db, seed)`（含峰值同比缩放） |
| 音频 IO | `common.audio.load_raw / resample / write_pcm16 / file_duration` |
| 输出 | `003_audio/<sample_id>.wav` + `003_batch.jsonl` + `003_perturb_report.json` |
| 原子写 | `common.iofs.write_jsonl / write_json` |
| 日志 | `common.logger.RunLog`（路由/校验/读取/处理/输出/警告/完成） |
| 错误口径 | 单条静音/解码失败 → `rejects` + `partial`；素材缺/坏 → 整批报错；整批 0 产出 → 抛错 |

## 六、资源要求

- **CPU**：单线程。20 条×3s×3 取值（60 条输出、180s 音频）实测 0.608s，约 **99× 输入实时**；
- **内存**：素材一次性缓存（默认 5 个素材、各 30s，16k 约 9.6MB float32）+ 逐条处理，
  实测峰值增量约 1.2MB（不含解释器基线）；
- **磁盘**：输出 WAV ≈ 32 KB/s（16k 单声道 PCM16）；
- **环境**：1 CPU / 512 MB 容器可跑；无 GPU、无网络、无模型。

## 七、性能指标

| 指标 | 实测值 | 测量方式 |
|---|---|---|
| SNR 误差（均值） | **0.000000 dB** | 15 条测试样本 `applied - target` |
| SNR 误差（最大） | **0.000000 dB** | 阈值 0.5 dB，违规 0 条 |
| 独立复核误差 | **0.000000 / 0.000000 dB** | 从报告逐条重算 |
| 输出条数 | 15（5×3） | 失败 0 条 |
| 时长保持 | 最大误差 0.000000 s | 混音不改时长 |
| 单条延迟 | ≈10 ms | 60 条 / 0.608s |
| 测试 | **30 passed** | `pytest tests/ -q` |

> SNR 误差为 0 是算法口径决定的（按功率反算增益），不是"没测量"：报告逐条记录 target 与 applied，
> 并给出均值/最大值；独立复核脚本从报告重算，结果一致。

## 八、输入输出示例

### 请求

```python
import requests
r = requests.post("http://127.0.0.1:8000/predict", json={
    "operator": "AVP_PERTURB_AUD_003",
    "uniqueId": "t1",
    "inputFile": "/data/t1/tmp/002/002_std.jsonl",
    "params": {
        "dimension": "noise",
        "values": ["白噪", "交通噪声"],
        "snr_db": [5, 15],
        "per_value": True,
        "seed": 20261102,
    },
}, timeout=600)
print(r.json()["data"]["statistics"]["snr_error_max_abs_db"])
```

### 响应关键字段

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "status": "success",
    "statistics": {"output": 4, "snr_error_mean_abs_db": 0.0, "snr_error_max_abs_db": 0.0,
                   "snr_error_violations": 0, "snr_target_min_db": 6.8, "snr_target_max_db": 13.3},
    "outputs": {"batch": "/data/t1/tmp/003/003_batch.jsonl",
                "report": "/data/t1/tmp/003/003_perturb_report.json"}
  }
}
```

### 失败示例

```json
{"code": 1, "msg": "invalid_params: values 中以下取值在素材目录 noise 下没有对应素材（.wav）：没有这个噪声"}
```
