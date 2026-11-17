"""算子 6 业务算法：音频解码、声道平均、抗混叠重采样、时间裁剪、PCM WAV 写出。

设计要点（TECHNICAL_DESIGN §4）：
1. PCM WAV 走标准库 ``wave``；浮点/压缩 WAV 与 MP3/FLAC/AIFF 经 ffmpeg 适配；
2. 整数 PCM 正确归一为浮点，立体声取声道平均，``scipy.signal.resample_poly`` 抗混叠
   重采样，裁剪后量化为 PCM16 并真正重写采样数据（不是只改文件头）；
3. 默认不裁剪；同时给出 ``clip_start``/``clip_end`` 时按秒裁剪并保留原时间范围；
4. 子进程使用参数数组、``shell=False``、超时与退出码检查；ffmpeg/ffprobe 缺失时抛
   ``DependencyMissingError``，绝不把失败当成功；
5. 输出用 ``wave`` 重读校验 rate/channels/frames，``duration = nframes / rate``。

后端说明：本算子没有模型节点。真实后端是 ``wave``（原生，优先）与 ``ffmpeg``
（容器/非 PCM 适配）；ffmpeg 缺失属 ``dependency_missing`` 明确失败，不做静默降级，
也不伪造音频或时长。
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import numpy as np
from scipy.signal import resample_poly

from utils.schemas import ConfigError, OperatorConfig

DEFAULT_OPERATOR_NAME = "audio_standardization"
DEFAULT_OPERATOR_VERSION = "v1.0.0"

#: 重采样后允许的一帧误差（wave 路径为 0 误差，ffmpeg 编码延迟另记录）
FRAME_TOLERANCE = 1

BACKEND_WAVE = "wave"
BACKEND_FFMPEG = "ffmpeg"


class AudioProcessingError(ValueError):
    """业务失败基类；``reason`` 直接写入异常清单的 reason 字段。"""

    reason = "audio_processing_failed"

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class MediaUnreadableError(AudioProcessingError):
    """源文件缺失、损坏或解码失败。"""

    reason = "media_unreadable"


class AudioMetadataError(AudioProcessingError):
    """音频参数非法：空音频、非法采样率/声道/位宽、非有限时长。"""

    reason = "invalid_audio_metadata"


class ResourceLimitError(AudioProcessingError):
    """超出配置的时长上限。"""

    reason = "resource_limit"


class DependencyMissingError(AudioProcessingError):
    """需要的解码后端（ffmpeg/ffprobe）不可用。"""

    reason = "dependency_missing"


class ClipRangeError(AudioProcessingError):
    """裁剪区间非法：起止反转或超出原音频时长。"""

    reason = "invalid_clip_range"


class AudioSaveError(AudioProcessingError):
    """写出的 WAV 复读校验不通过。"""

    reason = "audio_save_failed"


class UnsupportedAudioFormatError(AudioProcessingError):
    """容器/位宽不受支持。"""

    reason = "unsupported_format"


@dataclass
class DecodedAudio:
    """解码结果：单声道浮点归一化样本 + 原始参数。"""

    samples: np.ndarray          # float32 单声道，范围约 [-1, 1]
    sample_rate: int             # 原始采样率
    channels: int                # 原始声道数
    duration: float              # 原始时长（秒），来自文件头/ffprobe
    decoder_backend: str         # "wave" | "ffmpeg"


def coerce_config(config: Any) -> OperatorConfig:
    """把 ``OperatorConfig`` / 完整 dict / 纯业务参数 dict 统一成 ``OperatorConfig``。"""
    if isinstance(config, OperatorConfig):
        return config
    if isinstance(config, Mapping):
        payload = dict(config)
        if "operator" not in payload and "stage_id" not in payload:
            payload = {
                "operator": {"name": DEFAULT_OPERATOR_NAME, "version": DEFAULT_OPERATOR_VERSION},
                "stage_id": "S1",
                **payload,
            }
        return OperatorConfig.model_validate(payload)
    raise ConfigError("config 必须是 OperatorConfig 或 dict")


# ---------------------------------------------------------------- 纯函数

def resample_pcm(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """对归一化单声道浮点样本做抗混叠重采样（SciPy 多相滤波）。

    返回长度为 ``round(len(samples) * target_rate / source_rate)`` 的 float32 数组；
    采样率相同时返回副本。长度不匹配时按一帧误差对齐，保证时长可核对。
    """
    values = np.asarray(samples)
    if values.ndim != 1:
        raise AudioMetadataError("resample_pcm 只接受一维单声道样本")
    if values.size == 0:
        raise AudioMetadataError("空音频无法重采样")
    for name, rate in (("source_rate", source_rate), ("target_rate", target_rate)):
        if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
            raise AudioMetadataError("%s 必须是正整数" % name)
    if not np.all(np.isfinite(values)):
        raise AudioMetadataError("样本包含非有限值")

    floats = values.astype(np.float32, copy=False)
    if source_rate == target_rate:
        return floats.copy()

    divisor = math.gcd(source_rate, target_rate)
    up, down = target_rate // divisor, source_rate // divisor
    resampled = resample_poly(floats, up, down)
    resampled = np.asarray(resampled, dtype=np.float32).reshape(-1)

    expected = int(round(floats.size * target_rate / float(source_rate)))
    if resampled.size > expected:
        resampled = resampled[:expected]
    elif resampled.size < expected:
        resampled = np.pad(resampled, (0, expected - resampled.size))
    return resampled.astype(np.float32, copy=False)


def pcm_bytes_to_mono(raw: bytes, sampwidth: int, channels: int) -> np.ndarray:
    """把整数 PCM 字节流归一为单声道 float32（多声道取平均）。"""
    if channels <= 0:
        raise AudioMetadataError("声道数必须为正")
    if sampwidth == 1:
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    elif sampwidth == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        buffer = np.frombuffer(raw, dtype=np.uint8)
        usable = (buffer.size // 3) * 3
        triples = buffer[:usable].reshape(-1, 3).astype(np.int32)
        ints = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        ints = (ints ^ 0x800000) - 0x800000
        values = ints.astype(np.float32) / 8388608.0
    elif sampwidth == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise UnsupportedAudioFormatError("不支持的 PCM 位宽：%d 字节" % sampwidth)

    frame_count = values.size // channels
    if frame_count == 0:
        raise AudioMetadataError("音频没有可用的采样帧")
    values = values[: frame_count * channels].reshape(frame_count, channels)
    if channels == 1:
        mono = values[:, 0]
    else:
        mono = values.mean(axis=1)
    mono = np.asarray(mono, dtype=np.float32)
    if not np.all(np.isfinite(mono)):
        raise AudioMetadataError("音频样本包含非有限值")
    return mono


def float_mono_to_pcm(raw: bytes, channels: int) -> np.ndarray:
    """把 f32le 原始字节流（ffmpeg 输出）转为单声道 float32。"""
    if channels <= 0:
        raise AudioMetadataError("声道数必须为正")
    values = np.frombuffer(raw, dtype="<f4")
    frame_count = values.size // channels
    if frame_count == 0:
        raise AudioMetadataError("解码后没有可用的采样帧")
    values = values[: frame_count * channels].reshape(frame_count, channels).astype(np.float32)
    mono = values[:, 0] if channels == 1 else values.mean(axis=1)
    mono = np.asarray(mono, dtype=np.float32)
    if not np.all(np.isfinite(mono)):
        raise AudioMetadataError("解码后的样本包含非有限值")
    return mono


def quantize_to_pcm(samples: np.ndarray, pcm_bits: int) -> bytes:
    """把浮点样本量化为指定位宽的小端 PCM 字节流。"""
    values = np.asarray(samples, dtype=np.float64)
    if values.size == 0:
        raise AudioMetadataError("空音频无法量化")
    if pcm_bits == 8:
        ints = np.clip(np.rint(values * 128.0) + 128.0, 0, 255).astype(np.uint8)
        return ints.tobytes()
    if pcm_bits == 16:
        ints = np.clip(np.rint(values * 32768.0), -32768, 32767).astype("<i2")
        return ints.tobytes()
    if pcm_bits == 24:
        ints = np.clip(np.rint(values * 8388608.0), -8388608, 8388607).astype("<i4")
        return ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    if pcm_bits == 32:
        ints = np.clip(np.rint(values * 2147483648.0), -2147483648, 2147483647).astype("<i4")
        return ints.tobytes()
    raise AudioMetadataError("不支持的 pcm_bits：%r" % (pcm_bits,))


def rms(samples: np.ndarray) -> float:
    """样本均方根，用于核对输出不是静音（不伪造任何模型分数）。"""
    values = np.asarray(samples, dtype=np.float64)
    if values.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(values * values)))


# ---------------------------------------------------------------- 后端与子进程

def resolve_executable(name_or_path: str) -> Optional[str]:
    """解析可执行文件：优先现有文件，其次 PATH；找不到返回 None。"""
    if not isinstance(name_or_path, str) or not name_or_path.strip():
        return None
    candidate = Path(name_or_path)
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name_or_path)


def _run_process(cmd, timeout: float, action: str):
    """参数数组 + ``shell=False`` + 超时与退出码检查；缺失可执行文件转为依赖错误。"""
    try:
        return subprocess.run(
            cmd, shell=False, check=False, capture_output=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DependencyMissingError("%s 后端不可用" % action, "可执行文件不存在") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaUnreadableError("%s 超时" % action, "超过 %.3fs" % timeout) from exc
    except OSError as exc:
        raise DependencyMissingError("%s 启动失败" % action, type(exc).__name__) from exc


def probe_audio_metadata(source: Path, config: OperatorConfig) -> Dict[str, Any]:
    """用 ffprobe 读取首个音频流的采样率/声道/时长（仅非 WAV 路径使用）。"""
    executable = resolve_executable(config.ffprobe_path)
    if executable is None:
        raise DependencyMissingError(
            "ffprobe 不可用，无法读取非 PCM WAV 的音频参数", config.ffprobe_path)
    cmd = [
        executable, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels,duration:format=duration",
        "-of", "json", str(source),
    ]
    result = _run_process(cmd, config.process_timeout_seconds, "ffprobe")
    if result.returncode != 0:
        raise MediaUnreadableError("ffprobe 无法解析音频", "退出码 %d" % result.returncode)
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise AudioMetadataError("ffprobe 输出不是合法 JSON") from exc

    streams = payload.get("streams") or []
    if not streams:
        raise AudioMetadataError("文件中没有音频流")
    stream = streams[0]
    try:
        sample_rate = int(stream.get("sample_rate"))
        channels = int(stream.get("channels"))
    except (TypeError, ValueError) as exc:
        raise AudioMetadataError("ffprobe 未返回有效采样率/声道") from exc
    if sample_rate <= 0 or channels <= 0:
        raise AudioMetadataError("ffprobe 返回的采样率/声道非法")

    duration = _first_finite(
        stream.get("duration"), (payload.get("format") or {}).get("duration"))
    return {"sample_rate": sample_rate, "channels": channels, "duration": duration}


def _first_finite(*values) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return number
    return None


def decode_pcm_wav(source: Path, read_limit_seconds: Optional[float]) -> DecodedAudio:
    """标准库 ``wave`` 解码 PCM WAV（8/16/24/32 位整数）。"""
    try:
        handle = wave.open(str(source), "rb")
    except (wave.Error, EOFError, OSError) as exc:
        raise UnsupportedAudioFormatError("不是 wave 可读的 PCM WAV", type(exc).__name__) from exc
    with handle as reader:
        channels = reader.getnchannels()
        sampwidth = reader.getsampwidth()
        sample_rate = reader.getframerate()
        nframes = reader.getnframes()
        comptype = reader.getcomptype()
    if comptype != "NONE":
        raise UnsupportedAudioFormatError("压缩 WAV 需要 ffmpeg", comptype)
    if sampwidth not in (1, 2, 3, 4):
        raise UnsupportedAudioFormatError("WAV 位宽不受支持", "sampwidth=%d" % sampwidth)
    if channels <= 0 or sample_rate <= 0:
        raise AudioMetadataError("WAV 头中的声道/采样率非法")

    frame_bytes = sampwidth * channels
    wanted = nframes
    if read_limit_seconds is not None:
        wanted = min(nframes, int(math.ceil(read_limit_seconds * sample_rate)) + 1)
    wanted = max(wanted, 0)

    blocks = []
    remaining = wanted
    with wave.open(str(source), "rb") as reader:
        while remaining > 0:
            block = reader.readframes(min(1 << 16, remaining))
            if not block:
                break
            blocks.append(block)
            got = len(block) // frame_bytes
            if got == 0:
                break
            remaining -= got
    raw = b"".join(blocks)

    samples = pcm_bytes_to_mono(raw, sampwidth, channels)
    header_duration = nframes / float(sample_rate)
    if not math.isfinite(header_duration) or header_duration <= 0:
        raise AudioMetadataError("WAV 头中的时长非法")
    return DecodedAudio(
        samples=samples,
        sample_rate=sample_rate,
        channels=channels,
        duration=max(header_duration, samples.size / float(sample_rate)),
        decoder_backend=BACKEND_WAVE,
    )


def decode_via_ffmpeg(source: Path, config: OperatorConfig,
                      read_limit_seconds: Optional[float]) -> DecodedAudio:
    """经 ffmpeg 解码为 f32le 原始流，再转为单声道浮点。"""
    metadata = probe_audio_metadata(source, config)
    executable = resolve_executable(config.ffmpeg_path)
    if executable is None:
        raise DependencyMissingError(
            "ffmpeg 不可用，无法解码非 PCM WAV/压缩音频", config.ffmpeg_path)

    cmd = [
        executable, "-v", "error", "-nostdin", "-i", str(source),
        "-map", "0:a:0", "-vn", "-f", "f32le", "-acodec", "pcm_f32le",
        "-ac", str(metadata["channels"]), "-ar", str(metadata["sample_rate"]),
    ]
    if read_limit_seconds is not None:
        cmd += ["-t", "%.6f" % read_limit_seconds]
    cmd.append("-")

    result = _run_process(cmd, config.process_timeout_seconds, "ffmpeg")
    if result.returncode != 0:
        raise MediaUnreadableError("ffmpeg 解码失败", "退出码 %d" % result.returncode)

    sample_rate = int(metadata["sample_rate"])
    channels = int(metadata["channels"])
    samples = float_mono_to_pcm(result.stdout, channels)
    decoded_duration = samples.size / float(sample_rate)
    declared = metadata["duration"]
    duration = max(declared, decoded_duration) if declared else decoded_duration
    if not math.isfinite(duration) or duration <= 0:
        raise AudioMetadataError("解码后时长非法")
    return DecodedAudio(
        samples=samples,
        sample_rate=sample_rate,
        channels=channels,
        duration=duration,
        decoder_backend=BACKEND_FFMPEG,
    )


def decode_audio(source: Path, config: OperatorConfig,
                 read_limit_seconds: Optional[float]) -> DecodedAudio:
    """选择真实解码后端：``.wav`` 先试 ``wave``，其余或浮点/压缩 WAV 走 ffmpeg。"""
    suffix = Path(source).suffix.lower()
    if suffix == ".wav":
        try:
            return decode_pcm_wav(source, read_limit_seconds)
        except UnsupportedAudioFormatError:
            return decode_via_ffmpeg(source, config, read_limit_seconds)
    return decode_via_ffmpeg(source, config, read_limit_seconds)


def write_pcm_wav(destination, samples: np.ndarray, sample_rate: int,
                  channels: int, pcm_bits: int) -> int:
    """量化并按目标声道写出 PCM WAV，返回实际写入帧数。"""
    if channels not in (1, 2):
        raise AudioMetadataError("输出声道数只支持 1/2")
    if pcm_bits // 8 * 8 != pcm_bits or pcm_bits not in (8, 16, 24, 32):
        raise AudioMetadataError("输出位宽不受支持：%r" % (pcm_bits,))
    width = pcm_bits // 8
    raw_mono = quantize_to_pcm(samples, pcm_bits)
    if channels == 1:
        payload = raw_mono
    else:
        rows = np.frombuffer(raw_mono, dtype=np.uint8).reshape(-1, width)
        payload = np.repeat(rows, channels, axis=0).tobytes()

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(sample_rate)
        writer.writeframes(payload)
    return len(payload) // (width * channels)


def read_wav_summary(path) -> Dict[str, Any]:
    """用 ``wave`` 重读输出，核对采样率/声道/位宽/帧数。"""
    try:
        with wave.open(str(path), "rb") as reader:
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            sampwidth = reader.getsampwidth()
            nframes = reader.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioSaveError("输出 WAV 无法被 wave 重读", type(exc).__name__) from exc
    if sample_rate <= 0 or channels <= 0:
        raise AudioSaveError("输出 WAV 采样率/声道非法")
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sampwidth": sampwidth,
        "frames": nframes,
        "duration": nframes / float(sample_rate),
    }


# ---------------------------------------------------------------- 业务主函数

class SilentAudioError(AudioProcessingError):
    reason = "silent_audio"


def normalize_audio(source, destination, config):
    """FFmpeg固定转换16000Hz单声道PCM WAV；整段静音拒绝，无裁剪入口。"""
    cfg=coerce_config(config)
    source_path=Path(source);destination_path=Path(destination)
    if not source_path.is_file() or not source_path.stat().st_size:
        raise MediaUnreadableError('源音频不存在或为空')
    executable=cfg.ffmpeg_path
    if executable=='ffmpeg' and not shutil.which(executable):
        try:
            import imageio_ffmpeg
            executable=imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise DependencyMissingError('FFmpeg缺失，不使用wave代跑转换') from exc
    destination_path.parent.mkdir(parents=True,exist_ok=True)
    # 不使用-ss/-t；时长只作为资源检查，不裁剪输入。
    cmd=[str(executable),'-hide_banner','-nostdin','-y','-i',str(source_path),
         '-vn','-ar','16000','-ac','1','-c:a','pcm_s16le',str(destination_path)]
    try:
        proc=subprocess.run(cmd,capture_output=True,timeout=cfg.process_timeout_seconds)
    except OSError as exc:
        raise DependencyMissingError('FFmpeg不可用') from exc
    except subprocess.TimeoutExpired as exc:
        raise ResourceLimitError('FFmpeg转换超时') from exc
    if proc.returncode:
        raise MediaUnreadableError('FFmpeg解码失败',proc.stderr.decode('utf-8',errors='replace')[-1000:])
    summary=read_wav_summary(destination_path)
    # 仅解析输入首条音频流；不能将输出16000Hz误记成源采样率。
    input_log=proc.stderr.decode('utf-8',errors='replace').split('Output #',1)[0]
    source_stream=re.search(r'Audio:.*?,\s*(\d+) Hz,\s*([^,\r\n]+)',input_log)
    source_rate=int(source_stream.group(1)) if source_stream else None
    source_layout=source_stream.group(2).strip() if source_stream else None
    source_channels={'mono':1,'stereo':2}.get(source_layout)
    if source_channels is None and source_layout:
        channel_match=re.fullmatch(r'(\d+) channels',source_layout)
        if channel_match: source_channels=int(channel_match.group(1))
    decoded=decode_pcm_wav(destination_path, None)
    value=rms(decoded.samples)
    if decoded.samples.size==0 or not np.any(decoded.samples):
        destination_path.unlink(missing_ok=True)
        raise SilentAudioError('整段无声，送原始隔离区')
    if summary['duration']>cfg.max_duration_seconds:
        destination_path.unlink(missing_ok=True)
        raise ResourceLimitError('音频时长超过部署资源上限；未裁剪')
    return {'path':str(destination_path),'sample_rate':16000,'channels':1,
            'duration':round(summary['duration'],6),'segments':[],'pcm_bits':16,
            'clipped':False,'clip_start':None,'clip_end':None,'decoder_backend':'ffmpeg',
            'source_sample_rate':source_rate,'source_channels':source_channels,'source_duration':summary['duration'],
            'frames':summary['frames'],'bytes':destination_path.stat().st_size,'rms':round(value,8),
            'frame_tolerance':FRAME_TOLERANCE,'encoding_delay_note':None,'degraded':False}
