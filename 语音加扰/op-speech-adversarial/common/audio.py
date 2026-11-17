# -*- coding: utf-8 -*-
"""共享音频工具：解码、重采样、单声道、PCM16 写出、时长、RMS、按 SNR 混音、SNR 估计、变速。

统一约定（全产线 11 个算子一致，改动需同步《语音加扰算子开发规范》）：

1. 内部一律 **单声道 float32**，解码后立刻去直流（DC remove）。
2. 字节落盘一律 **WAV / PCM 16bit**（``subtype='PCM_16'``），与任务书「目标格式固定 WAV（PCM 16bit）」一致。
3. 所有随机行为必须**可复现**：调用方传 ``seed``，内部用 ``numpy.random.default_rng``。
4. **不静默降级**：解码失败抛 :class:`AudioDecodeError`，由上层转成统一错误码，
   绝不返回伪造的音频或伪造的分数。

SNR 口径（全产线统一）
----------------------
``SNR(dB) = 10 * log10(P_signal / P_noise)``，其中 ``P`` 为均方值（mean square，即功率）。
- 混音时按目标 SNR 反算噪声增益：``scale = sqrt(P_sig / (10**(snr/10) * P_noise))``。
- 混音后若峰值超过 0.99，信号与噪声**同比例**缩放，SNR 不变。
- 无参考估计用「帧能量分位数法」，见 :func:`estimate_snr_db`，属**盲估计**，README 必须写明。
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

import soundfile as sf

AUDIO_EXTS = (".wav", ".mp3", ".flac", ".aiff", ".aif", ".ogg", ".opus")
DEFAULT_SR = 16000
MAX_ABS = 0.99
SILENCE_RMS = 1e-5


class AudioDecodeError(RuntimeError):
    """音频无法解码（格式不支持 / 文件损坏 / 非音频）。"""


# --------------------------------------------------------------------------- #
# 基础读
# --------------------------------------------------------------------------- #
def _res_type() -> str:
    """优先 soxr（快且质量高），未安装则回退 kaiser_best。"""
    try:
        import soxr  # noqa: F401

        return "soxr_hq"
    except Exception:  # pragma: no cover - 取决于环境
        return "kaiser_best"


def dc_remove(y: np.ndarray) -> np.ndarray:
    """去直流。空数组原样返回。"""
    y = np.asarray(y, dtype=np.float32)
    if y.size == 0:
        return y
    return (y - np.float32(np.mean(y, dtype=np.float64))).astype(np.float32)


def load_raw(path) -> Tuple[np.ndarray, int]:
    """读取音频，返回 ``(单声道 float32 已去直流, 原生采样率)``。

    支持 wav / flac / aiff / mp3（libsndfile 1.2.2 及以上）。
    """
    p = Path(path)
    if not p.is_file():
        raise AudioDecodeError("file_not_found: %s" % p.name)
    try:
        y, sr = sf.read(str(p), dtype="float32", always_2d=False)
    except Exception as exc:  # noqa: BLE001 - 统一转成解码错误
        raise AudioDecodeError("decode_failed: %s: %s" % (p.name, exc)) from exc
    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=1, dtype=np.float32)
    if y.size == 0:
        raise AudioDecodeError("empty_audio: %s" % p.name)
    return dc_remove(y), int(sr)


def resample(y: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """重采样。相同采样率或空输入直接返回。"""
    y = np.asarray(y, dtype=np.float32)
    if y.size == 0 or int(orig_sr) == int(target_sr):
        return y
    import librosa

    out = librosa.resample(
        y, orig_sr=int(orig_sr), target_sr=int(target_sr), res_type=_res_type()
    )
    return np.asarray(out, dtype=np.float32)


def load_mono(path, sr: Optional[int] = None) -> Tuple[np.ndarray, int]:
    """读取音频并按需重采样，返回 ``(float32 单声道, 实际采样率)``。"""
    y, native = load_raw(path)
    if sr is not None and int(sr) != native:
        y = resample(y, native, int(sr))
        return y, int(sr)
    return y, native


# --------------------------------------------------------------------------- #
# 基础写
# --------------------------------------------------------------------------- #
def limit_peak(y: np.ndarray, max_abs: float = MAX_ABS) -> np.ndarray:
    """按峰值等比限幅到 ``max_abs``；不改变波形比例（因此不改变 SNR）。"""
    y = np.asarray(y, dtype=np.float32)
    if y.size == 0:
        return y
    m = float(np.max(np.abs(y)))
    if m > float(max_abs) and m > 0.0:
        y = y * np.float32(float(max_abs) / m)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def write_pcm16(path, y: np.ndarray, sr: int) -> Path:
    """写 WAV PCM16，父目录自动创建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), limit_peak(y), int(sr), subtype="PCM_16")
    return p


# --------------------------------------------------------------------------- #
# 度量
# --------------------------------------------------------------------------- #
def rms(y: np.ndarray) -> float:
    """均方根（幅度域）。"""
    y = np.asarray(y, dtype=np.float32)
    if y.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(y, dtype=np.float64))))


def peak(y: np.ndarray) -> float:
    y = np.asarray(y, dtype=np.float32)
    return 0.0 if y.size == 0 else float(np.max(np.abs(y)))


def duration_sec(y: np.ndarray, sr: int) -> float:
    """数组时长（秒，4 位小数）。"""
    if sr <= 0:
        return 0.0
    return round(float(np.asarray(y).size) / float(sr), 4)


def file_duration(path) -> float:
    """用文件头取时长（不整段解码），秒，4 位小数。"""
    try:
        info = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001
        raise AudioDecodeError("decode_failed: %s: %s" % (Path(path).name, exc)) from exc
    if info.samplerate <= 0:
        return 0.0
    return round(float(info.frames) / float(info.samplerate), 4)


def file_info(path) -> dict:
    """文件头元数据快照（采样率/声道/编码/时长），入库检测与标准化复用。"""
    try:
        info = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001
        raise AudioDecodeError("decode_failed: %s: %s" % (Path(path).name, exc)) from exc
    dur = round(float(info.frames) / float(info.samplerate), 4) if info.samplerate else 0.0
    return {
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "format": str(info.format).lower(),
        "subtype": str(info.subtype).lower(),
        "frames": int(info.frames),
        "duration": dur,
    }


def power_db(power: float, floor: float = 1e-12) -> float:
    """功率（均方值）转 dB。"""
    return 10.0 * math.log10(max(float(power), floor))


def snr_db(sig: np.ndarray, noise: np.ndarray) -> float:
    """按定义算两段等长信号的 SNR(dB)。"""
    ps = float(np.mean(np.square(np.asarray(sig, np.float64))))
    pn = float(np.mean(np.square(np.asarray(noise, np.float64))))
    if pn <= 0.0:
        return float("inf")
    if ps <= 0.0:
        return float("-inf")
    return round(10.0 * math.log10(ps / pn), 3)


def is_silent(y: np.ndarray, threshold: float = SILENCE_RMS) -> bool:
    """空音频判定（任务书「空音频」检查口径）。"""
    return rms(y) < float(threshold)


# --------------------------------------------------------------------------- #
# 加扰：混音 / 变速
# --------------------------------------------------------------------------- #
def _tile_to(y: np.ndarray, n: int, seed: int = 0) -> np.ndarray:
    """把素材循环或裁剪到正好 ``n`` 个采样点（裁剪位置由 seed 决定，保证可复现）。"""
    y = np.asarray(y, dtype=np.float32)
    n = int(n)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    if y.size == 0:
        return np.zeros(n, dtype=np.float32)
    if y.size < n:
        reps = int(math.ceil(n / float(y.size)))
        return np.tile(y, reps)[:n]
    if y.size == n:
        return y
    rng = np.random.default_rng(int(seed))
    offset = int(rng.integers(0, int(y.size - n) + 1))
    return y[offset : offset + n]


def merge_at_snr(
    sig: np.ndarray,
    noise: np.ndarray,
    snr_target_db: float,
    seed: int = 0,
) -> Tuple[np.ndarray, np.ndarray, float]:
    """按目标 SNR 把噪声混入干净语音。

    返回 ``(mixed, scaled_noise, applied_snr_db)``：

    - ``mixed``：混音结果（已按峰值 0.99 等比限幅，SNR 不变）
    - ``scaled_noise``：缩放后的噪声分量（写参数快照/复盘用）
    - ``applied_snr_db``：实际达成 SNR，用于自检（与目标误差应 ≤ 0.5 dB）

    任一分量功率为 0 时抛 ``ValueError``，不静默造数据。
    """
    sig = np.asarray(sig, dtype=np.float32)
    n = int(sig.size)
    if n == 0:
        raise ValueError("empty_signal")
    nz = _tile_to(noise, n, seed=seed)
    ps = float(np.mean(np.square(sig.astype(np.float64))))
    pn0 = float(np.mean(np.square(nz.astype(np.float64))))
    if ps <= 0.0:
        raise ValueError("silent_signal")
    if pn0 <= 0.0:
        raise ValueError("silent_noise")
    target_ratio = 10.0 ** (float(snr_target_db) / 10.0)  # ps / pn
    scale = math.sqrt(ps / (target_ratio * pn0))
    nz = (nz * np.float32(scale)).astype(np.float32)
    mixed = (sig + nz).astype(np.float32)
    m = float(np.max(np.abs(mixed)))
    sig_ref = sig
    if m > MAX_ABS:
        # 等比限幅：信号与噪声同乘 k，SNR 不变（因此自检必须用缩放后的参考）
        k = np.float32(MAX_ABS / m)
        mixed = (mixed * k).astype(np.float32)
        nz = (nz * k).astype(np.float32)
        sig_ref = (sig * k).astype(np.float32)
    applied = snr_db(sig_ref, nz)
    return mixed, nz, applied


def estimate_snr_db(
    y: np.ndarray,
    sr: int = DEFAULT_SR,
    frame: int = 512,
    hop: int = 160,
    win_sec: float = 0.8,
    rel_threshold: float = 2.0,
    iterations: int = 3,
    bias_db: float = 0.0,
) -> float:
    """**盲估计** SNR（无干净参考时使用），单位 dB。

    做法：帧能量 + 最小值统计（minimum statistics）+ 两遍非语音帧重估：

    1. 按 ``frame/hop``（32 ms / 10 ms）切帧算均方能量；
    2. 用滑动窗口（``win_sec``）的**最小值序列中位数**作初值噪声底 ``Pn0``
       —— 滑动最小值能穿过语音间隙，比固定分位数稳；
    3. 以 ``rel_threshold * Pn`` 判非语音帧，用**非语音帧能量均值**重估 ``Pn``，
       迭代 ``iterations`` 次（均值对平稳噪声是无偏的，最小值是有偏的）；
    4. ``SNR = 10*log10((mean(语音帧能量) - Pn) / Pn)``，下限截断 0 dB，
       再减去可配置的 ``bias_db``（标定用，默认 0）。

    实测标定（``tools/fixture_lib`` 合成语料 + 白噪/粉噪，3 秒，白噪 SNR 5–20 dB）：
    本估计器系统性**偏高约 1.0–2.5 dB**，误差随 SNR 升高而收敛。
    因此：

    - **有干净参考时必须用** :func:`snr_db` **真值，不得用本函数替代**；
    - 调用方必须把结果来源标成 ``estimated``，并在 README/报告里写明盲估精度，
      不得伪装成实测值；
    - 需要消除系统偏差时，用配置项把 ``bias_db`` 设成标定值（见各算子 config.yaml）。
    """
    y = np.asarray(y, dtype=np.float32)
    if y.size == 0:
        return 0.0
    frame = int(min(max(64, frame), y.size))
    hop = int(max(1, min(hop, frame)))
    n_frames = 1 + max(0, (y.size - frame) // hop)
    if n_frames <= 0:
        return 0.0
    idx = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    idx = idx[idx[:, -1] < y.size]
    if idx.size == 0:
        return 0.0
    energy = np.mean(np.square(y[idx].astype(np.float64)), axis=1) + 1e-12

    from scipy.ndimage import minimum_filter1d

    win = int(round(float(win_sec) * float(sr) / float(hop)))
    win = max(1, min(win, max(1, energy.size // 2)))
    pn = float(np.median(minimum_filter1d(energy, size=win, mode="nearest")))

    min_silent_frames = max(5, energy.size // 20)
    for _ in range(max(1, int(iterations))):
        silent = energy <= float(rel_threshold) * pn
        if int(np.count_nonzero(silent)) >= min_silent_frames:
            candidate = float(np.mean(energy[silent]))
            if candidate > 0.0:
                pn = candidate

    speech_mask = energy > float(rel_threshold) * pn
    if int(np.count_nonzero(speech_mask)) >= 3:
        ps_total = float(np.mean(energy[speech_mask]))
    else:
        ps_total = float(np.percentile(energy, 95.0))

    snr = 10.0 * math.log10(max(ps_total - pn, 1e-12) / max(pn, 1e-12))
    return round(max(0.0, snr - float(bias_db)), 3)


def time_stretch(y: np.ndarray, rate: float) -> np.ndarray:
    """变速不变调（相位声码器）。

    ``rate=1.2`` 表示加速到 1.2 倍（时长变为原长 / 1.2）。
    ``rate=1.0`` 原样返回副本，保证 1.0× 透传与输入 PCM 完全一致。
    """
    y = np.asarray(y, dtype=np.float32)
    rate = float(rate)
    if rate <= 0.0:
        raise ValueError("rate_must_be_positive")
    if abs(rate - 1.0) < 1e-9:
        return y.copy()
    import librosa

    out = librosa.effects.time_stretch(y, rate=rate)
    return np.asarray(out, dtype=np.float32)


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
def sha256_file(path, chunk: int = 1 << 20) -> str:
    """文件 SHA-256（版本包 manifest 用）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def to_mono(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=0).astype(np.float32)
    return y


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)
