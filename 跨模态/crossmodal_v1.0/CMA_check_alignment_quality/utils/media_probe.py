"""媒体探测：全量文件可打开、视频可解码、音频非全静音、文本非空。

表1 #20 研发边界的第一组硬检查，全部基于**真实解码**：
- 图像：Pillow 打开并 ``load()``；
- 视频：OpenCV ``VideoCapture`` 真实读帧（无 ffmpeg 时的回退）；
- 音频：``soundfile`` → 标准库 ``wave`` → ``ffmpeg`` 依次回退，取真实 PCM 计算
  RMS / 峰值 / SNR；
- 文本：UTF-8 严格解码后去空白判空。

设计原则：
- 后端缺失返回 ``no_backend``，**绝不猜结果**；调用方据此降级或结构化失败；
- SNR 用「分帧能量 + 低分位噪声底」计算，纯音无噪声时如实给出 ≈0 dB，
  不人为抬高。
"""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_STATUS_OK = "ok"
_STATUS_MISSING = "missing"
_STATUS_UNREADABLE = "unreadable"
_STATUS_UNDECODABLE = "undecodable"
_STATUS_NO_BACKEND = "no_backend"

try:  # 数值后端
    import numpy as _np
except Exception:  # pragma: no cover - numpy 是硬依赖
    _np = None  # type: ignore

try:
    from PIL import Image as _Image
except Exception:  # pragma: no cover
    _Image = None  # type: ignore

try:
    import cv2 as _cv2
except Exception:  # pragma: no cover
    _cv2 = None  # type: ignore

try:
    import soundfile as _sf
except Exception:  # pragma: no cover
    _sf = None  # type: ignore

#: 文本类字段的取值优先级（结构化文本对象）
_TEXT_KEYS = ("content", "description", "caption", "text", "original",
              "transcript", "value")

#: 音频可解码扩展名（决定何时尝试解码而不是直接判不可读）
_AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
                   ".wma", ".aiff", ".aif"}


class AudioDecodeError(RuntimeError):
    """音频无法解码为 PCM。"""


def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


def brisque_backend_available() -> bool:
    """OpenCV contrib 的 BRISQUE 是否可用（同时需要模型文件，见 upstream_metrics）。"""
    return bool(_cv2 is not None and hasattr(_cv2, "quality")
                and hasattr(getattr(_cv2, "quality"), "QualityBRISQUE_create"))


def available_backends() -> Dict[str, Any]:
    ffmpeg = ffmpeg_path()
    image_decoder = "pillow" if _Image is not None else None
    video_decoder = "opencv" if _cv2 is not None else ("ffmpeg" if ffmpeg else None)
    if _sf is not None:
        audio_decoder = "soundfile"
    elif _np is not None:
        audio_decoder = "wave"
    elif ffmpeg:
        audio_decoder = "ffmpeg"
    else:
        audio_decoder = None
    return {
        "numpy": _np is not None,
        "pillow": _Image is not None,
        "opencv": _cv2 is not None,
        "soundfile": _sf is not None,
        "wave": True,
        "ffmpeg": ffmpeg,
        "image_decoder": image_decoder,
        "video_decoder": video_decoder,
        "audio_decoder": audio_decoder,
        "brisque_backend": brisque_backend_available(),
    }


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #

def text_state(value: Any) -> Dict[str, Any]:
    """归一化文本取值并判定非空。返回 ``{nonempty, chars, text, source}``。

    支持 ``str``、结构化 dict（``content`` / ``description`` / ``caption`` 等）
    以及 ``None``。``chars`` 为去首尾空白后的字符数。
    """
    text = ""
    source = "none"
    if isinstance(value, str):
        text, source = value, "string"
    elif isinstance(value, dict):
        for key in _TEXT_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                text, source = candidate, key
                break
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text, source = str(value), "number"
    elif isinstance(value, (list, tuple)):
        joined = " ".join(str(item) for item in value if isinstance(item, (str, int, float)))
        text, source = joined, "list"

    stripped = text.strip()
    chars = len(stripped)
    return {"nonempty": chars > 0, "chars": chars, "text": stripped, "source": source}


def text_from_file(path) -> Dict[str, Any]:
    """读取外部文本文件（严格 UTF-8）。失败时返回 ``error`` 而不是抛异常。"""
    target = Path(path)
    if not target.is_file():
        return {"nonempty": False, "chars": 0, "text": "", "source": "file",
                "error": "missing"}
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"nonempty": False, "chars": 0, "text": "", "source": "file",
                "error": "encoding_error"}
    except OSError as exc:
        return {"nonempty": False, "chars": 0, "text": "", "source": "file",
                "error": type(exc).__name__}
    stripped = text.strip()
    return {"nonempty": bool(stripped), "chars": len(stripped), "text": stripped,
            "source": "file", "error": None}


# --------------------------------------------------------------------------- #
# 图像
# --------------------------------------------------------------------------- #

def probe_image(path) -> Dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {"status": _STATUS_MISSING, "error": "file_not_found"}
    if _Image is None:
        return {"status": _STATUS_NO_BACKEND, "error": "pillow_missing"}
    try:
        with _Image.open(str(target)) as image:
            image.load()
            width, height = image.size
            image_format = image.format
        if not width or not height:
            return {"status": _STATUS_UNREADABLE, "error": "zero_dimension"}
        return {"status": _STATUS_OK, "decoder": "pillow", "width": int(width),
                "height": int(height), "format": image_format, "error": None}
    except Exception as exc:  # noqa: BLE001 - 任何解码失败都算不可读
        return {"status": _STATUS_UNREADABLE, "error": type(exc).__name__}


# --------------------------------------------------------------------------- #
# 视频
# --------------------------------------------------------------------------- #

def probe_video(path, max_frames: int = 8) -> Dict[str, Any]:
    """真实读帧验证可解码；``max_frames`` 沿用表1 #14 的「最多 8 帧」约束。"""
    target = Path(path)
    if not target.is_file():
        return {"status": _STATUS_MISSING, "error": "file_not_found"}
    if _cv2 is None:
        return {"status": _STATUS_NO_BACKEND, "error": "video_decoder_missing"}

    capture = _cv2.VideoCapture(str(target))
    try:
        if not capture.isOpened():
            return {"status": _STATUS_UNDECODABLE, "error": "capture_not_opened"}
        frames = 0
        limit = max(1, int(max_frames))
        while frames < limit:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            frames += 1
        fps = float(capture.get(_cv2.CAP_PROP_FPS) or 0.0)
        total = int(capture.get(_cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        capture.release()

    if frames <= 0:
        return {"status": _STATUS_UNDECODABLE, "error": "no_frame_decoded"}
    duration = (total / fps) if (fps > 0 and total > 0) else None
    return {"status": _STATUS_OK, "decoder": "opencv", "frames_probed": frames,
            "fps": round(fps, 3) if fps else None,
            "duration": round(duration, 3) if duration else None,
            "frame_count": total or None, "error": None}


# --------------------------------------------------------------------------- #
# 音频
# --------------------------------------------------------------------------- #

def _decode_with_wave(path) -> Dict[str, Any]:
    if _np is None:
        raise AudioDecodeError("numpy_missing")
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        if handle.getcomptype() != "NONE":
            raise AudioDecodeError("compressed_wav_unsupported")
        raw = handle.readframes(frames)
    if frames <= 0 or not raw:
        raise AudioDecodeError("empty_audio")
    dtype = {1: _np.int8, 2: "<i2", 4: "<i4"}.get(width)
    if dtype is None:
        raise AudioDecodeError("unsupported_sample_width")
    scale = float(2 ** (8 * width - 1))
    samples = _np.frombuffer(raw, dtype=dtype).astype(_np.float64) / scale
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return {"samples": samples.astype(_np.float32), "sample_rate": int(rate),
            "channels": int(channels), "decoder": "wave"}


def _decode_with_soundfile(path) -> Dict[str, Any]:
    if _sf is None or _np is None:
        raise AudioDecodeError("soundfile_missing")
    data, rate = _sf.read(str(path), dtype="float32", always_2d=True)
    if data.size == 0:
        raise AudioDecodeError("empty_audio")
    samples = data.mean(axis=1)
    return {"samples": samples.astype(_np.float32), "sample_rate": int(rate),
            "channels": int(data.shape[1]), "decoder": "soundfile"}


def _decode_with_ffmpeg(path) -> Dict[str, Any]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg or _np is None:
        raise AudioDecodeError("ffmpeg_missing")
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
         "-ar", "16000", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not proc.stdout:
        raise AudioDecodeError("ffmpeg_decode_failed")
    samples = _np.frombuffer(proc.stdout, dtype="<f4")
    return {"samples": samples.astype(_np.float32), "sample_rate": 16000,
            "channels": 1, "decoder": "ffmpeg"}


def decode_audio(path) -> Dict[str, Any]:
    """把音频解码为单声道 float32 PCM。全部后端失败时抛 ``AudioDecodeError``。"""
    target = Path(path)
    if not target.is_file():
        raise AudioDecodeError("file_not_found")
    errors: List[str] = []
    for decoder in (_decode_with_soundfile, _decode_with_wave, _decode_with_ffmpeg):
        try:
            return decoder(target)
        except AudioDecodeError as exc:
            errors.append("%s:%s" % (decoder.__name__, exc))
        except Exception as exc:  # noqa: BLE001 - 解码器内部异常按解码失败处理
            errors.append("%s:%s" % (decoder.__name__, type(exc).__name__))
    raise AudioDecodeError(";".join(errors) or "no_audio_decoder")


def audio_snr_db(samples, framerate: int, frame_ms: float = 20.0) -> Optional[float]:
    """分帧能量法估算 SNR（dB）：``10*log10(平均能量 / 10 分位噪声能量)``。

    - 少于 2 帧或全静音 → ``None``（不编造数值）；
    - 恒定纯音时噪声底≈信号，如实返回 ≈0 dB。
    """
    if _np is None or samples is None:
        return None
    array = _np.asarray(samples, dtype=_np.float64)
    if array.size == 0 or not framerate:
        return None
    frame = max(1, int(int(framerate) * frame_ms / 1000.0))
    usable = array.size // frame
    if usable < 2:
        return None
    energies = _np.mean(array[:usable * frame].reshape(usable, frame) ** 2, axis=1)
    positive = energies[energies > 0]
    if positive.size == 0:
        return None
    signal = float(_np.mean(energies))
    noise = float(_np.percentile(positive, 10))
    if signal <= 0 or noise <= 0:
        return None
    return round(float(10.0 * _np.log10(signal / noise)), 3)


def probe_audio(path, silence_rms_threshold: float = 1e-4) -> Dict[str, Any]:
    """解码音频并给出真实时长/采样率/RMS/峰值/SNR 与是否整段无声。"""
    target = Path(path)
    if not target.is_file():
        return {"status": _STATUS_MISSING, "error": "file_not_found"}
    if available_backends()["audio_decoder"] is None:
        return {"status": _STATUS_NO_BACKEND, "error": "audio_decoder_missing"}
    try:
        decoded = decode_audio(target)
    except AudioDecodeError as exc:
        return {"status": _STATUS_UNDECODABLE, "error": str(exc)[:200]}

    samples = decoded["samples"]
    rate = decoded["sample_rate"]
    if _np is not None and samples.size:
        rms = float(_np.sqrt(_np.mean(samples.astype(_np.float64) ** 2)))
        peak = float(_np.max(_np.abs(samples)))
    else:  # pragma: no cover - numpy 缺失时无法给出真实统计
        rms, peak = 0.0, 0.0
    threshold = float(silence_rms_threshold)
    return {
        "status": _STATUS_OK,
        "decoder": decoded["decoder"],
        "sample_rate": int(rate),
        "channels": int(decoded.get("channels", 1)),
        "duration": round(samples.size / float(rate), 4) if rate else None,
        "samples": int(samples.size),
        "rms": round(rms, 8),
        "peak": round(peak, 8),
        "snr_db": audio_snr_db(samples, rate),
        "silent": bool(rms <= threshold),
        "error": None,
    }


def audio_suffix_supported(relative_path: str) -> bool:
    return Path(str(relative_path)).suffix.lower() in _AUDIO_SUFFIXES
