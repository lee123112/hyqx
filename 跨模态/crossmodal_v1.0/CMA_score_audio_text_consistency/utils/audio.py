"""音频读取与重采样（真实解码，不做任何伪造）。

边界：
- 只接受能真实解码出非静音波形的文件；整段无声按表1 归「原始文件-空」；
- 读取顺序：标准库 ``wave``（PCM WAV） → ``scipy.io.wavfile`` → ``torchaudio``；
- 采样率不为 16000 时用线性插值重采样（自实现，不额外引入依赖）；
- 解码失败一律抛 ``AudioReadError``，由业务层写成「原始文件-无法解析」，不静默丢弃。
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

#: 低于该幅度的波形视为整段无声
SILENCE_EPSILON = 1e-6


class AudioReadError(ValueError):
    """音频无法解码、为空或整段无声。"""


def _dtype_map(width: int):
    if width == 1:
        return np.uint8, 128.0
    if width == 2:
        return np.dtype("<i2"), 32768.0
    if width == 4:
        return np.dtype("<i4"), 2147483648.0
    return None, 0.0


def read_wav(path) -> Tuple[np.ndarray, int]:
    """读取音频为单声道 float32（取值约 [-1, 1]），返回 ``(waveform, sample_rate)``。"""
    target = Path(path)
    if not target.is_file():
        raise AudioReadError("音频文件不存在：%s" % target.name)

    samples: Optional[np.ndarray] = None
    sample_rate = 0
    for reader in (_read_with_wave_module, _read_with_scipy, _read_with_torchaudio):
        try:
            samples, sample_rate = reader(target)
        except AudioReadError:
            continue
        except Exception:  # noqa: BLE001 - 解码后端异常统一转为可归因失败
            continue
        if samples is not None and samples.size:
            break
        samples = None
    if samples is None or sample_rate <= 0:
        raise AudioReadError("音频无法解码：%s" % target.name)

    waveform = np.asarray(samples, dtype=np.float32)
    if waveform.ndim != 1:
        waveform = waveform.reshape(-1)
    if waveform.size == 0:
        raise AudioReadError("音频为空：%s" % target.name)
    if float(np.max(np.abs(waveform))) <= SILENCE_EPSILON:
        raise AudioReadError("整段无声：%s" % target.name)
    return waveform, int(sample_rate)


def _read_with_wave_module(path: Path) -> Tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
        dtype, scale = _dtype_map(width)
        if dtype is None or channels < 1 or frames <= 0 or sample_rate <= 0:
            raise AudioReadError("WAV 头信息非法或为空")
        raw = handle.readframes(frames)
    if len(raw) < frames * channels * width:
        raise AudioReadError("WAV 数据长度不足")
    values = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if width == 1:
        values = (values - scale) / scale
    else:
        values = values / scale
    if channels > 1:
        values = values.reshape(-1, channels).mean(axis=1)
    return values, sample_rate


def _read_with_scipy(path: Path) -> Tuple[np.ndarray, int]:
    from scipy.io import wavfile

    sample_rate, data = wavfile.read(str(path))
    array = np.asarray(data)
    if array.dtype == np.uint8:
        array = (array.astype(np.float32) - 128.0) / 128.0
    elif array.dtype == np.int16:
        array = array.astype(np.float32) / 32768.0
    elif array.dtype == np.int32:
        array = array.astype(np.float32) / 2147483648.0
    else:
        array = array.astype(np.float32)
    if array.ndim > 1:
        array = array.mean(axis=1)
    return array.astype(np.float32), int(sample_rate)


def _read_with_torchaudio(path: Path) -> Tuple[np.ndarray, int]:
    import torchaudio

    tensor, sample_rate = torchaudio.load(str(path))
    array = tensor.mean(dim=0).numpy().astype(np.float32)
    return array, int(sample_rate)


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """线性插值重采样（实算法，非占位）。相同采样率时原样返回。"""
    values = np.asarray(samples, dtype=np.float32)
    if source_rate == target_rate or values.size == 0:
        return values
    if source_rate <= 0 or target_rate <= 0:
        raise AudioReadError("采样率非法：%s -> %s" % (source_rate, target_rate))
    count = int(round(values.shape[0] * float(target_rate) / float(source_rate)))
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    source_index = np.arange(values.shape[0], dtype=np.float64)
    target_index = np.linspace(0.0, float(values.shape[0] - 1), count)
    return np.interp(target_index, source_index, values).astype(np.float32)


def load_audio_mono(path, target_rate: int = 16000,
                    max_seconds: Optional[float] = None) -> Tuple[np.ndarray, int, Dict[str, Any]]:
    """读取为单声道 ``target_rate`` 波形，并按 ``max_seconds`` 截断超长音频。"""
    waveform, source_rate = read_wav(path)
    source_duration = waveform.shape[0] / float(source_rate)
    if source_rate != target_rate:
        waveform = resample_linear(waveform, source_rate, target_rate)
    truncated = False
    if max_seconds is not None and max_seconds > 0:
        limit = int(round(float(max_seconds) * target_rate))
        if limit > 0 and waveform.shape[0] > limit:
            waveform = waveform[:limit]
            truncated = True
    meta = {
        "source_sample_rate": int(source_rate),
        "source_duration": round(source_duration, 4),
        "target_sample_rate": int(target_rate),
        "truncated": truncated,
        "samples": int(waveform.shape[0]),
    }
    return waveform.astype(np.float32, copy=False), int(target_rate), meta
