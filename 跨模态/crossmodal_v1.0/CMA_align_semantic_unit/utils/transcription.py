"""算子 8 语音转写（表1 #8「跨模态协同预处理」）。

表1 #8 冻结口径
----------------
* 输入：音频或带音轨的视频；
* 步骤 1) 总开关或「是否做语音转写」关闭时，**不加载模型**；
* 步骤 2) 打开后算法固定使用 Whisper-base，语言按中文解码，输出带起止时间的文本；
* 步骤 3) 再按段落把音频裁开；
* 输出：时间索引。转写失败则留在产线中，进入候选描述，**不能直接评分**；
* 边界：不换成更大的 Whisper、不在本工具打对齐分、不补采；
* 完成：纯图文默认任务的日志写明未加载 Whisper-base。

工程约束
--------
* 本模块顶层不 import ``transformers``/``torch``；重依赖只在真正要转写时才加载，
  从而保证开关关闭时「零模型加载」可被日志与统计证实。
* 只从本地权重目录加载（``local_files_only=True``），运行期不联网下载（不补采）。
* 任何失败都不伪造文本：``status=failed`` + 真实原因，样本留在产线。
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 表1 固定模型：不允许换成更大的 Whisper
FIXED_MODEL = "whisper-base"
#: 表1 固定解码语言：中文
FIXED_LANGUAGE = "zh"
#: 送入 Whisper 的采样率（Whisper 系列固定 16kHz）
WHISPER_SAMPLE_RATE = 16000

AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".wma"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".ts"}

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
#: 没有音频输入 / 确认无音轨：不需要 ASR，记录透传
STATUS_NOT_APPLICABLE = "not_applicable"
#: 有音轨但转写为空：不伪造文本，留产线进候选描述
STATUS_NO_SPEECH = "no_speech"

#: 记录级流程态（表1 #8）：由转写结果唯一决定，五个取值互斥且覆盖全部主清单记录
PROCESS_TRANSCRIBED = "transcribed"
PROCESS_SKIPPED = "skipped"
PROCESS_NOT_APPLICABLE = "not_applicable"
PROCESS_NO_SPEECH = "no_speech"
PROCESS_FAILED = "failed"

PROCESS_STATUS_OF = {
    STATUS_OK: PROCESS_TRANSCRIBED,
    STATUS_SKIPPED: PROCESS_SKIPPED,
    STATUS_NOT_APPLICABLE: PROCESS_NOT_APPLICABLE,
    STATUS_NO_SPEECH: PROCESS_NO_SPEECH,
    STATUS_FAILED: PROCESS_FAILED,
}


def record_process_status(payload: Dict[str, Any]) -> str:
    """把转写载荷的产出态映射为记录级流程态（未知状态按 failed 处理）。"""
    return PROCESS_STATUS_OF.get(payload.get("status"), PROCESS_FAILED)

REASON_TRANSCRIPTION_DISABLED = "transcription_disabled"
REASON_MASTER_SWITCH_OFF = "master_switch_off"
REASON_NO_MEDIA = "no_audio_input"
REASON_MODEL_MISSING = "whisper_weights_missing"


def _model_root() -> Path:
    """权重根目录：``$MODEL_ROOT`` 优先，否则算子本地 ``<算子目录>/weight``。"""
    override = os.environ.get("MODEL_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "weight"


REASON_MODEL_UNAVAILABLE = "whisper_backend_unavailable"
REASON_DECODE_FAILED = "audio_decode_failed"
REASON_INFERENCE_FAILED = "whisper_inference_failed"
REASON_EMPTY_TEXT = "empty_transcript"
#: 媒体容器确认不含音轨（与「解码失败」区分：无声视频不是坏文件）
REASON_NO_AUDIO_TRACK = "no_audio_track"
#: 没有任何可用的音频解码后端（soundfile/torchaudio/ffmpeg 都不可用）
REASON_DECODER_UNAVAILABLE = "audio_decoder_unavailable"
#: 预检：音频短于 ``min_duration_seconds``（Whisper-base 上极易幻觉/空文本）
REASON_AUDIO_TOO_SHORT = "audio_too_short"
#: 预检：估计信噪比低于 ``min_snr_db`` 且波形接近稳态（噪声/稳态音乐片段会触发解码循环）
REASON_LOW_SNR = "low_snr"
#: 预检：整段电平低于静音阈值（无需跑 ASR）
REASON_AUDIO_SILENT = "audio_silent"

#: 预检默认阈值（与 ``configs/config.yaml`` 的 ``min_duration_seconds`` /
#: ``min_snr_db`` 保持一致；配置可覆盖）
PRECHECK_MIN_DURATION_SECONDS = 0.4
PRECHECK_MIN_SNR_DB = 6.0
#: 估计信噪比时的一帧长度（秒）：20ms 帧足够覆盖语音的浊音段
PRECHECK_FRAME_SECONDS = 0.02
#: 整段 RMS 峰值低于该值视为静音（float32 满幅为 1.0）
PRECHECK_SILENCE_RMS = 1e-4
#: low_snr 的「稳态」附加条件：帧 RMS 波动系数（std/mean）低于该值才判稳态噪声。
#: 稳态单音/白噪声实测 CV≈0.008/0.040，真实语音/音乐伴奏 CV≥0.21；
#: 只有低 SNR 且几乎不波动时才跳过解码，避免把带背景声的语音误杀。
PRECHECK_STATIONARY_CV = 0.10


class TranscriptionConfigError(ValueError):
    """转写相关配置非法（表1 固定项被改写等）。"""


class AudioDecodeError(RuntimeError):
    """音频/音轨无法解码为 PCM（``reason`` 区分无声与解码失败）。"""

    def __init__(self, message: str, reason: str = REASON_DECODE_FAILED) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class TranscriptionSettings:
    """表1 #8 的转写设置（由部署配置 + 请求覆盖合成）。"""

    master_enabled: bool          # 总开关
    switch_enabled: bool          # 「是否做语音转写」
    model: str
    language: str
    model_root: str
    model_dir: str
    device: str
    max_segment_seconds: float
    paragraph_silence_seconds: float
    beam_size: int
    # 可选的中文书写提示。它只影响 Whisper 解码，不改变模型或语言固定项。
    initial_prompt: str = ""
    # 送 ASR 前的时长/信噪比预检（默认开启）；阈值见同名字段注释。
    precheck: bool = True
    min_duration_seconds: float = PRECHECK_MIN_DURATION_SECONDS
    min_snr_db: float = PRECHECK_MIN_SNR_DB

    @property
    def active(self) -> bool:
        """表1 #8 步骤 1)：总开关**或**转写开关关闭 ⇒ 不加载模型。"""
        return bool(self.master_enabled and self.switch_enabled)

    @property
    def inactive_reason(self) -> Optional[str]:
        if self.active:
            return None
        if not self.master_enabled:
            return REASON_MASTER_SWITCH_OFF
        return REASON_TRANSCRIPTION_DISABLED

    def switch_detail(self) -> Dict[str, Any]:
        """记录两个开关的真实取值，便于在日志与统计里自证「未加载」。"""
        return {
            "master_switch": bool(self.master_enabled),
            "transcription_switch": bool(self.switch_enabled),
            "transcription_effective": self.active,
            "model": self.model,
            "language": self.language,
            "device": self.device,
            "beam_size": int(self.beam_size),
            "initial_prompt_enabled": bool(self.initial_prompt.strip()),
            "precheck": bool(self.precheck),
            "min_duration_seconds": float(self.min_duration_seconds),
            "min_snr_db": float(self.min_snr_db),
        }

    @classmethod
    def from_config(cls, config: Any) -> "TranscriptionSettings":
        """从 ``OperatorConfig`` 取值并复核表1 固定项。"""
        master = bool(getattr(config, "enabled", True))
        section = getattr(config, "transcription", None)
        defaults = dict(master_enabled=master, switch_enabled=False, model=FIXED_MODEL,
                        language=FIXED_LANGUAGE, model_root="weight",
                        model_dir=FIXED_MODEL, device="auto",
                        max_segment_seconds=30.0, paragraph_silence_seconds=0.6,
                        beam_size=1, initial_prompt="",
                        precheck=True,
                        min_duration_seconds=PRECHECK_MIN_DURATION_SECONDS,
                        min_snr_db=PRECHECK_MIN_SNR_DB)
        if section is None:
            return cls(**defaults)
        model = str(getattr(section, "model", FIXED_MODEL))
        language = str(getattr(section, "language", FIXED_LANGUAGE))
        if model != FIXED_MODEL:
            raise TranscriptionConfigError(
                "表1 #8 固定使用 %s，不接受 %s（边界：不换成更大的 Whisper）"
                % (FIXED_MODEL, model))
        if language != FIXED_LANGUAGE:
            raise TranscriptionConfigError(
                "表1 #8 固定按中文解码，不接受 language=%s" % language)
        defaults.update(
            switch_enabled=bool(getattr(section, "enabled", False)),
            model=model,
            language=language,
            model_root=str(getattr(section, "model_root", "weight")),
            model_dir=str(getattr(section, "model_dir", FIXED_MODEL)),
            device=str(getattr(section, "device", "auto")),
            max_segment_seconds=float(getattr(section, "max_segment_seconds", 30.0)),
            paragraph_silence_seconds=float(
                getattr(section, "paragraph_silence_seconds", 0.6)),
            beam_size=int(getattr(section, "beam_size", 1)),
            initial_prompt=str(getattr(section, "initial_prompt", "") or "").strip(),
            precheck=bool(getattr(section, "precheck", True)),
            min_duration_seconds=float(getattr(
                section, "min_duration_seconds", PRECHECK_MIN_DURATION_SECONDS)),
            min_snr_db=float(getattr(section, "min_snr_db", PRECHECK_MIN_SNR_DB)),
        )
        return cls(**defaults)


# ------------------------------------------------------------------ 音频解码

def is_audio_media(relative: Optional[str]) -> bool:
    return bool(relative) and Path(relative).suffix.lower() in AUDIO_SUFFIXES


def is_video_media(relative: Optional[str]) -> bool:
    return bool(relative) and Path(relative).suffix.lower() in VIDEO_SUFFIXES


def _decode_pcm(frames: bytes, width: int) -> "Any":
    import numpy as np

    if width == 1:
        data = np.frombuffer(frames, dtype=np.uint8).astype("float32")
        return (data - 128.0) / 128.0
    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype("float32")
        return data / 32768.0
    if width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype("int32")
        value = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        value = np.where(value >= 0x800000, value - 0x1000000, value)
        return value.astype("float32") / 8388608.0
    if width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype("float32")
        return data / 2147483648.0
    raise AudioDecodeError("不支持的 PCM 位宽：%d" % width)


def _tool_path(env_key: str, name: str) -> Optional[str]:
    """解析外部工具：``$<ENV_KEY>`` 优先，否则从 PATH 查找。"""
    override = os.environ.get(env_key)
    if override:
        candidate = Path(override).expanduser()
        return str(candidate) if candidate.is_file() else None
    return shutil.which(name)


def ffmpeg_binary() -> Optional[str]:
    """ffmpeg 可执行文件（``$FFMPEG_PATH`` 或 PATH）；不下载、不打包二进制。"""
    return _tool_path("FFMPEG_PATH", "ffmpeg")


def ffprobe_binary() -> Optional[str]:
    return _tool_path("FFPROBE_PATH", "ffprobe")


def probe_audio_stream(path: Path) -> Optional[bool]:
    """探测媒体是否含音轨：True 有 / False 确认没有 / None 无法判定。

    优先 ffprobe（只看流信息，不解码）。无 ffprobe 或探测出错时返回 None，
    后续由解码结果兜底，**绝不把「探测不了」当成「没有音轨」**。
    """
    binary = ffprobe_binary()
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def decode_with_ffmpeg(path: Path) -> Tuple["Any", int]:
    """用 ffmpeg 子进程把任意容器解成 16k 单声道 float32。

    不落地临时文件：``-f f32le pipe:1`` 直接读 stdout。视频没有音轨时
    ffmpeg 正常退出但输出为空，按 ``no_audio_track`` 上报，不当成损坏。
    """
    import numpy as np

    binary = ffmpeg_binary()
    if not binary:
        raise AudioDecodeError(
            "无可用音频后端（soundfile/torchaudio/ffmpeg 均不可用）",
            REASON_DECODER_UNAVAILABLE)
    try:
        proc = subprocess.run(
            [binary, "-v", "error", "-nostdin", "-i", str(path),
             "-vn", "-ac", "1", "-ar", str(WHISPER_SAMPLE_RATE),
             "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioDecodeError("ffmpeg 调用失败：%s" % type(exc).__name__) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise AudioDecodeError("ffmpeg 解码失败：%s" % (detail or "exit %d" % proc.returncode))
    data = np.frombuffer(proc.stdout, dtype="<f4")
    if data.size == 0:
        raise AudioDecodeError("媒体不含音轨（ffmpeg 解码为空）", REASON_NO_AUDIO_TRACK)
    return np.ascontiguousarray(data, dtype="float32"), WHISPER_SAMPLE_RATE


def read_audio(path: Path) -> Tuple["Any", int]:
    """把音频解码成 ``(float32 单声道波形, 采样率)``。

    优先标准库 ``wave``（#7 输出即 16k 单声道 PCM WAV，容器内无 ffmpeg 也能解）；
    其余容器退回 ``soundfile`` / ``torchaudio``；全部失败则如实报错，不产假数据。
    """
    import numpy as np

    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                channels = handle.getnchannels()
                width = handle.getsampwidth()
                rate = handle.getframerate()
                frames = handle.readframes(handle.getnframes())
            data = _decode_pcm(frames, width)
            if channels > 1:
                data = data.reshape(-1, channels).mean(axis=1)
            return np.ascontiguousarray(data, dtype="float32"), int(rate)
        except (wave.Error, EOFError, OSError) as exc:
            raise AudioDecodeError("WAV 解码失败：%s" % type(exc).__name__) from exc

    try:  # soundfile（若环境提供）
        import soundfile  # type: ignore

        data, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        return np.ascontiguousarray(mono, dtype="float32"), int(rate)
    except Exception:  # noqa: BLE001 - 继续尝试下一种后端
        pass

    try:  # torchaudio（依赖 ffmpeg 后端，带音轨视频走这条）
        import torchaudio  # type: ignore

        waveform, rate = torchaudio.load(str(path))
        mono = waveform.mean(dim=0).numpy()
        return np.ascontiguousarray(mono, dtype="float32"), int(rate)
    except Exception:  # noqa: BLE001 - 继续尝试 ffmpeg CLI
        pass

    # ffmpeg CLI 兜底：torchaudio 在多数 Linux 镜像里没有编译进 ffmpeg 后端，
    # 而部署环境通常带 ffmpeg 可执行文件；用它解码任意容器，避免「有音轨也报失败」。
    return decode_with_ffmpeg(path)


def resample(waveform: "Any", source_rate: int, target_rate: int) -> "Any":
    """线性插值重采样；Whisper 只接受 16kHz。"""
    import numpy as np

    if source_rate == target_rate or waveform.size == 0:
        return waveform
    duration = waveform.size / float(source_rate)
    target_size = max(1, int(round(duration * target_rate)))
    source_positions = np.linspace(0.0, duration, num=waveform.size, endpoint=False)
    target_positions = np.linspace(0.0, duration, num=target_size, endpoint=False)
    return np.interp(target_positions, source_positions, waveform).astype("float32")


def write_wav(path: Path, waveform: "Any", rate: int = WHISPER_SAMPLE_RATE) -> None:
    """把段落裁片写成 16bit 单声道 PCM WAV。"""
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(np.asarray(waveform, dtype="float32"), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm)


# ------------------------------------------------------------------ 送 ASR 前预检

def frame_rms(waveform: "Any", rate: int,
              frame_seconds: float = PRECHECK_FRAME_SECONDS) -> "Any":
    """按固定帧长切分并返回每帧 RMS（末尾不足一帧的样本丢弃）。"""
    import numpy as np

    samples = np.asarray(waveform, dtype="float32").reshape(-1)
    frame = max(1, int(round(float(rate) * float(frame_seconds))))
    frames = samples.size // frame
    if frames <= 0:
        return np.zeros(0, dtype="float32")
    trimmed = samples[: frames * frame].reshape(frames, frame)
    return np.sqrt(np.mean(np.square(trimmed, dtype="float64"), axis=1))


def estimate_snr_db(waveform: "Any", rate: int) -> Optional[float]:
    """粗估信噪比：20·log10(高分位帧 RMS / 低分位帧 RMS)。

    - 用 10% / 90% 分位避免单帧脉冲（爆音）主导结果；
    - 整段电平低于 ``PRECHECK_SILENCE_RMS`` 时返回 ``None``（静音，无 SNR 可言）。
    这是**预检**指标，不用于评分，也不写进任何评分算子。
    """
    import numpy as np

    rms = frame_rms(waveform, rate)
    if rms.size == 0 or float(rms.max()) < PRECHECK_SILENCE_RMS:
        return None
    floor = max(float(np.percentile(rms, 10)), PRECHECK_SILENCE_RMS)
    level = max(float(np.percentile(rms, 90)), floor)
    return round(20.0 * math.log10(level / floor), 3)


def frame_rms_cv(waveform: "Any", rate: int,
                 frame_seconds: float = PRECHECK_FRAME_SECONDS) -> Optional[float]:
    """帧 RMS 的波动系数（std/mean），用于区分稳态噪声与有起伏的语音/音乐。

    - 稳态单音、白噪声：接近 0（实测 0.008 / 0.040）；
    - 真实语音、带伴奏的讲解：明显更大（实测 0.21–1.5）；
    - 整段静音或帧数不足时返回 ``None``。
    """
    rms = frame_rms(waveform, rate, frame_seconds)
    if rms.size == 0:
        return None
    mean = float(rms.mean())
    if mean <= 0.0:
        return None
    return float(rms.std() / mean)


def precheck_audio(waveform: "Any", rate: int,
                   settings: "TranscriptionSettings") -> Optional[Dict[str, Any]]:
    """送 Whisper 前的时长/信噪比预检；命中返回 ``no_speech`` 载荷，否则 ``None``。

    命中时不加载模型、不产生任何文本：``status=no_speech`` + 具体原因码，
    记录仍在产线（进候选描述），与「有音轨但空转写」走同一条处置路径。
    """
    if not bool(getattr(settings, "precheck", True)):
        return None
    size = int(getattr(waveform, "size", 0) or 0)
    duration = round(size / float(rate), 3) if rate else 0.0
    snr_db = estimate_snr_db(waveform, rate)
    frame_cv = frame_rms_cv(waveform, rate)
    metrics = {"enabled": True, "duration_sec": duration, "snr_db": snr_db,
               "frame_cv": None if frame_cv is None else round(frame_cv, 6),
               "min_duration_seconds": float(settings.min_duration_seconds),
               "min_snr_db": float(settings.min_snr_db)}

    reason: Optional[str] = None
    detail: Optional[str] = None
    if snr_db is None:
        reason, detail = REASON_AUDIO_SILENT, "整段电平低于静音阈值，无需送 ASR"
    elif duration < float(settings.min_duration_seconds):
        reason = REASON_AUDIO_TOO_SHORT
        detail = ("音频时长 %.3fs 低于 %.2fs，Whisper-base 易输出幻觉/空文本"
                  % (duration, float(settings.min_duration_seconds)))
    elif snr_db < float(settings.min_snr_db):
        if frame_cv is not None and frame_cv >= PRECHECK_STATIONARY_CV:
            # 低 SNR 但能量起伏明显：更像带背景声/伴奏的语音，交给 ASR 判断，
            # 不因全局分位比偏低而误杀（2026-11-05 真实 TTS/时序素材回归结论）。
            return None
        reason = REASON_LOW_SNR
        detail = ("估计信噪比 %.1f dB 低于 %.1f dB 且帧 RMS 波动系数 %.3f < %.2f"
                  "（稳态噪声），易触发解码循环"
                  % (snr_db, float(settings.min_snr_db),
                     float(frame_cv or 0.0), PRECHECK_STATIONARY_CV))
    if reason is None:
        return None
    return {"status": STATUS_NO_SPEECH, "reason": reason, "detail": detail,
            "duration": duration, "precheck": metrics}


# ------------------------------------------------------------------ 段落切分

def group_paragraphs(segments: List[Dict[str, Any]], *,
                     silence_seconds: float,
                     max_segment_seconds: float) -> List[Dict[str, Any]]:
    """把转写片段按段落合并（表1 #8 步骤 3）。

    合并规则：相邻片段间隔超过 ``silence_seconds``，或累计时长超过
    ``max_segment_seconds``，即切出新段落。段落起止时间取首尾片段的时间戳。
    """
    paragraphs: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for segment in segments:
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        if current is None:
            current = {"start": start, "end": end, "text": text}
            continue
        gap = start - current["end"]
        too_long = (end - current["start"]) > max_segment_seconds
        if gap > silence_seconds or too_long:
            paragraphs.append(current)
            current = {"start": start, "end": end, "text": text}
            continue
        current["end"] = max(current["end"], end)
        current["text"] = (current["text"] + " " + text).strip()
    if current is not None:
        paragraphs.append(current)
    for index, paragraph in enumerate(paragraphs):
        paragraph["index"] = index
        paragraph["start"] = round(paragraph["start"], 3)
        paragraph["end"] = round(paragraph["end"], 3)
    return paragraphs


def _safe_stem(sample_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", sample_id) or "sample"


def cut_paragraphs(waveform: "Any", rate: int, paragraphs: List[Dict[str, Any]], *,
                   target_dir: Path, sample_id: str, relative_prefix: str) -> None:
    """按段落把音频裁开并落盘，把相对路径写回每个段落。"""
    for paragraph in paragraphs:
        start = int(math.floor(paragraph["start"] * rate))
        end = int(math.ceil(paragraph["end"] * rate))
        start = max(0, min(start, waveform.size))
        end = max(start + 1, min(end, waveform.size))
        name = "%s_%03d.wav" % (_safe_stem(sample_id), paragraph["index"])
        clip = waveform[start:end]
        write_wav(target_dir / name, clip, rate)
        paragraph["segment_path"] = "%s/%s" % (relative_prefix.strip("/"), name)
        paragraph["segment_bytes"] = int(clip.size * 2)


# ------------------------------------------------------------------ Whisper

class WhisperTranscriber:
    """惰性加载的 Whisper-base 转写器（同一 run 内复用同一实例）。"""

    def __init__(self, settings: TranscriptionSettings) -> None:
        self.settings = settings
        self.loaded = False
        self.load_reason: Optional[str] = None
        self.device: Optional[str] = None
        self._processor = None
        self._model = None

    # -- 加载 ---------------------------------------------------------------
    def weights_dir(self) -> Path:
        """Whisper 权重目录。

        ``model_root`` 为相对路径（默认 ``weight``，与显式 ``.`` 等价）时按
        ``$MODEL_ROOT``／算子本地 ``<算子目录>/weight`` 解析，因此
        ``model_root: weight``（或 ``.``）+ ``model_dir: whisper-base``
        都指向算子本地 ``weight/whisper-base``，不会叠加出一层 ``weight/weight``。
        """
        text = "" if self.settings.model_root is None else str(self.settings.model_root).strip()
        # 兼容 model_root="weight"：其语义即 <算子目录>/weight（schema 缺省值），
        # 与显式 "." 等价，这里直接落在 _model_root() 上，避免叠加出 weight/weight。
        if text.lower() == "weight":
            return _model_root() / self.settings.model_dir
        root = _model_root() if not text else Path(text).expanduser()
        if not root.is_absolute():
            root = _model_root() / root
        return root / self.settings.model_dir

    def _resolve_device(self) -> str:
        if self.settings.device in ("cpu", "cuda"):
            return self.settings.device
        try:
            import torch  # noqa: PLC0415

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001
            return "cpu"

    def load(self) -> Tuple[bool, Optional[str]]:
        """首次调用才真正加载权重；返回 ``(是否可用, 原因)``。"""
        if self.loaded:
            return True, None
        if self.load_reason is not None:
            return False, self.load_reason

        weights = self.weights_dir()
        if not (weights / "config.json").is_file():
            self.load_reason = REASON_MODEL_MISSING
            return False, self.load_reason
        try:
            import torch  # noqa: F401,PLC0415
            from transformers import (  # noqa: PLC0415
                WhisperForConditionalGeneration,
                WhisperProcessor,
            )
        except Exception:  # noqa: BLE001
            self.load_reason = REASON_MODEL_UNAVAILABLE
            return False, self.load_reason

        try:
            self._processor = WhisperProcessor.from_pretrained(
                str(weights), local_files_only=True)
            self._model = WhisperForConditionalGeneration.from_pretrained(
                str(weights), local_files_only=True)
            self.device = self._resolve_device()
            self._model.to(self.device).eval()
        except Exception:  # noqa: BLE001
            self._processor = None
            self._model = None
            self.load_reason = REASON_MODEL_UNAVAILABLE
            return False, self.load_reason
        self.loaded = True
        return True, None

    # -- 转写 ---------------------------------------------------------------
    def transcribe(self, media_path: Path) -> Dict[str, Any]:
        """转写单条媒体，返回带起止时间的文本（失败如实上报，不产假文本）。"""
        # 先探测音轨：无声视频不是「解码失败」，也不该白跑 Whisper。
        if probe_audio_stream(media_path) is False:
            # 无声视频/纯视频不是失败：不需要 ASR，归 not_applicable
            return {"status": STATUS_NOT_APPLICABLE, "reason": REASON_NO_AUDIO_TRACK,
                    "detail": "媒体容器不含音轨（ffprobe 确认）"}
        try:
            waveform, rate = read_audio(media_path)
        except AudioDecodeError as exc:
            return {"status": STATUS_FAILED, "reason": exc.reason,
                    "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"status": STATUS_FAILED, "reason": REASON_DECODE_FAILED,
                    "detail": type(exc).__name__}

        if waveform.size == 0:
            return {"status": STATUS_NO_SPEECH, "reason": REASON_EMPTY_TEXT,
                    "detail": "音频为空"}

        # 送 ASR 前的时长/信噪比预检：明显不可转写的音频不白跑 Whisper，
        # 也避免音乐/噪声片段触发解码循环产出垃圾文本（不加载模型）。
        precheck = precheck_audio(waveform, rate, self.settings)
        if precheck is not None:
            return precheck

        available, reason = self.load()
        if not available:
            return {"status": STATUS_FAILED, "reason": reason or REASON_MODEL_UNAVAILABLE}

        import numpy as np  # noqa: PLC0415

        audio = resample(waveform, rate, WHISPER_SAMPLE_RATE)
        prompt_used = False
        prompt_fallback = False
        try:
            with _no_grad():
                prompt = self.settings.initial_prompt.strip()
                if prompt:
                    try:
                        prompt_ids = self._processor.get_prompt_ids(
                            prompt, return_tensors="pt")
                        # transformers 版本不同可能返回 numpy 或 torch Tensor。
                        if not hasattr(prompt_ids, "to"):
                            import torch  # noqa: PLC0415
                            prompt_ids = torch.as_tensor(prompt_ids)
                        prompt_ids = prompt_ids.to(self.device)
                        prompted = self._decode_audio(audio, prompt_ids=prompt_ids)
                        prompted_text = " ".join(item["text"] for item in prompted).strip()
                        if has_prompt_echo(prompted_text, prompt):
                            # 不能把提示词或其重复当成音频内容；回退到同一模型的无提示解码。
                            prompt_fallback = True
                        else:
                            segments = prompted
                            prompt_used = True
                    except Exception:  # noqa: BLE001 - 提示增强失败时保留无提示真实结果
                        prompt_fallback = True
                if not prompt or prompt_fallback:
                    segments = self._decode_audio(audio)
                # Whisper 在长静音处可能提前生成结束标记。对未覆盖的尾部
                # 重新调用同一 Whisper-base，并把局部时间戳平移回原音频坐标。
                segments = self._retry_uncovered_tail(audio, segments)
                # 某些 Whisper 版本会把长静音前后的语音合并成一个 chunk，
                # 即使返回时间戳也无法触发段落切分。用波形静音边界定位段落，
                # 再用同一个 Whisper-base 对每个语音区间重解码；这只补齐
                # 时间索引和实体 WAV，不改变模型、阈值或对齐评分。
                segments = self._decode_detected_regions(audio, segments)
        except Exception as exc:  # noqa: BLE001
            return {"status": STATUS_FAILED, "reason": REASON_INFERENCE_FAILED,
                    "detail": type(exc).__name__}

        text = " ".join(item["text"] for item in segments).strip()
        if not text:
            return {"status": STATUS_NO_SPEECH, "reason": REASON_EMPTY_TEXT}
        return {"status": STATUS_OK, "segments": segments, "text": text,
                "audio": audio, "sample_rate": WHISPER_SAMPLE_RATE,
                "duration": round(float(np.size(audio)) / WHISPER_SAMPLE_RATE, 3),
                "prompt_used": prompt_used,
                "prompt_fallback": prompt_fallback}

    def _decode_audio(self, audio: Any, *, prompt_ids: Any = None) -> List[Dict[str, Any]]:
        """把一段音频送入固定 Whisper-base，返回该窗口内的局部时间戳。"""
        features = self._processor(
            audio, sampling_rate=WHISPER_SAMPLE_RATE,
            return_tensors="pt").input_features.to(self.device)
        return self._generate_segments(features, prompt_ids=prompt_ids)

    def _retry_uncovered_tail(self, audio: Any, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """重试长静音后的尾部，避免整段解码只覆盖第一段语音。"""
        import numpy as np  # noqa: PLC0415

        duration = float(np.size(audio)) / WHISPER_SAMPLE_RATE
        covered_end = max((float(item.get("end")) for item in segments
                           if isinstance(item.get("end"), (int, float))), default=0.0)
        merged = list(segments)
        retries = 0
        while covered_end < duration - 0.5 and retries < 4:
            start = min(duration - 0.1, covered_end + self.settings.paragraph_silence_seconds)
            if start <= 0.0 or start >= duration:
                break
            end = min(duration, start + 30.0)
            left = int(round(start * WHISPER_SAMPLE_RATE))
            right = int(round(end * WHISPER_SAMPLE_RATE))
            window = np.asarray(audio[left:right], dtype="float32")
            if window.size < int(0.25 * WHISPER_SAMPLE_RATE):
                break
            local = self._decode_audio(window)
            shifted: List[Dict[str, Any]] = []
            for item in local:
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                local_start = item.get("start")
                local_end = item.get("end")
                if not isinstance(local_start, (int, float)):
                    local_start = 0.0
                if not isinstance(local_end, (int, float)):
                    local_end = local_start
                shifted.append({"start": round(start + float(local_start), 3),
                                "end": round(start + max(float(local_end), float(local_start)), 3),
                                "text": text})
            if not shifted:
                break
            for item in shifted:
                duplicate = any(item["text"] == old.get("text") and
                                abs(item["start"] - float(old.get("start") or 0.0)) < 0.2
                                for old in merged)
                if not duplicate:
                    merged.append(item)
            next_end = max((float(item.get("end")) for item in shifted
                            if isinstance(item.get("end"), (int, float))), default=covered_end)
            if next_end <= covered_end + 0.01:
                break
            covered_end = next_end
            retries += 1
            if end >= duration:
                break
        return sorted(merged, key=lambda item: float(item.get("start") or 0.0))

    def _decode_detected_regions(self, audio: Any,
                                 segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """对明显静音分隔的语音区间分别解码，避免段落被合并。"""
        import numpy as np  # noqa: PLC0415

        arr = np.asarray(audio, dtype="float32")
        if arr.size < int(0.5 * WHISPER_SAMPLE_RATE):
            return segments
        frame = max(1, int(0.02 * WHISPER_SAMPLE_RATE))
        count = int(math.ceil(arr.size / frame))
        rms = np.asarray([
            float(np.sqrt(np.mean(np.square(arr[i * frame:min(arr.size, (i + 1) * frame)]))))
            for i in range(count)
        ])
        peak = float(np.max(rms)) if rms.size else 0.0
        if peak <= 1e-5:
            return segments
        threshold = max(0.0015, peak * 0.08)
        active = rms >= threshold
        # 轻微膨胀，避免把词内短停顿误判为段落边界。
        pad = max(1, int(round(0.12 / 0.02)))
        for _ in range(pad):
            active[:-1] |= active[1:]
            active[1:] |= active[:-1]
        regions: List[Tuple[int, int]] = []
        start = None
        for idx, flag in enumerate(active.tolist() + [False]):
            if flag and start is None:
                start = idx
            elif not flag and start is not None:
                end = idx
                if end - start >= int(0.25 / 0.02):
                    regions.append((start * frame, min(arr.size, end * frame)))
                start = None
        if len(regions) <= 1:
            return segments
        # 只在确实存在超过配置静音阈值的间隔时重解码。
        merged_regions: List[Tuple[int, int]] = [regions[0]]
        max_gap = int(self.settings.paragraph_silence_seconds * WHISPER_SAMPLE_RATE)
        for left, right in regions[1:]:
            if left - merged_regions[-1][1] <= max_gap:
                merged_regions[-1] = (merged_regions[-1][0], right)
            else:
                merged_regions.append((left, right))
        if len(merged_regions) <= 1:
            return segments
        decoded: List[Dict[str, Any]] = []
        for left, right in merged_regions:
            window = np.asarray(arr[left:right], dtype="float32")
            if window.size < int(0.25 * WHISPER_SAMPLE_RATE):
                continue
            try:
                local = self._decode_audio(window)
            except Exception:  # noqa: BLE001 - 保留原始解码证据
                local = []
            for item in local:
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                local_start = item.get("start")
                local_end = item.get("end")
                local_start = float(local_start) if isinstance(local_start, (int, float)) else 0.0
                local_end = float(local_end) if isinstance(local_end, (int, float)) else local_start
                decoded.append({"start": round(left / WHISPER_SAMPLE_RATE + local_start, 3),
                                "end": round(left / WHISPER_SAMPLE_RATE + max(local_end, local_start), 3),
                                "text": text})
        return sorted(decoded, key=lambda item: float(item.get("start") or 0.0)) or segments

    def _generate_segments(self, features: Any, *, prompt_ids: Any = None) -> List[Dict[str, Any]]:
        """用固定 Whisper-base 生成带时间戳的片段。"""
        kwargs = {
            "language": self.settings.language,  # 表1：固定中文解码
            "task": "transcribe",
            "return_timestamps": True,
            "num_beams": self.settings.beam_size,
        }
        if prompt_ids is not None:
            kwargs["prompt_ids"] = prompt_ids
        generated = self._model.generate(features, **kwargs)
        return _extract_segments(self._processor, generated)


def _no_grad():
    import torch  # noqa: PLC0415

    return torch.no_grad()


def has_prompt_echo(text: Any, prompt: Any) -> bool:
    """判断生成文本是否包含初始提示词，命中时必须回退而不能把提示当音频。"""
    def _compact(value: Any) -> str:
        value = unicodedata.normalize("NFKC", str(value or "")).lower()
        return "".join(ch for ch in value if not ch.isspace() and not unicodedata.category(ch).startswith("P"))

    prompt_text = _compact(prompt)
    return bool(prompt_text and prompt_text in _compact(text))


def _extract_segments(processor: Any, generated: Any) -> List[Dict[str, Any]]:
    """把 Whisper 的 chunk 时间戳规范化成 ``[{start,end,text}]``。"""
    try:
        decoded = processor.batch_decode(
            generated, skip_special_tokens=True, output_offsets=True)
    except Exception:  # noqa: BLE001 - 老版本 transformers 无 output_offsets
        text = processor.batch_decode(generated, skip_special_tokens=True)[0].strip()
        return [{"start": None, "end": None, "text": text}] if text else []

    entry = decoded[0]
    if isinstance(entry, str):
        entry = {"text": entry, "offsets": []}
    text = str(entry.get("text") or "").strip()
    chunks = entry.get("offsets") or []
    if not chunks:
        return [{"start": None, "end": None, "text": text}] if text else []

    segments: List[Dict[str, Any]] = []
    duration = None
    for chunk in chunks:
        stamp = chunk.get("timestamp") or (None, None)
        start, end = (list(stamp) + [None, None])[:2]
        piece = str(chunk.get("text") or "").strip()
        if not piece:
            continue
        if end is None and duration is not None:
            end = duration
        segments.append({"start": start, "end": end, "text": piece})
        if end is not None:
            duration = end
    return segments


def finalize_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """补齐缺失时间戳并保证单调，使「时间索引」始终可用。"""
    filled: List[Dict[str, Any]] = []
    cursor = 0.0
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = segment.get("start")
        end = segment.get("end")
        start = float(start) if isinstance(start, (int, float)) else cursor
        end = float(end) if isinstance(end, (int, float)) else start
        if end < start:
            end = start
        filled.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        cursor = end
    return filled


# ------------------------------------------------------------------ 记录级入口

def audio_source_reference(record: Dict[str, Any]) -> Optional[str]:
    """取用于转写的媒体引用：优先 ``audio``，其次带音轨的视频容器（表1 #8 输入）。"""
    for key in ("audio", "video"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str) and path.strip():
                if key == "video" and not is_video_media(path):
                    continue
                return path.strip()
    return None


def build_payload(status: str, *, reason: Optional[str] = None,
                  detail: Optional[str] = None, text: Optional[str] = None,
                  paragraphs: Optional[List[Dict[str, Any]]] = None,
                  settings: Optional[TranscriptionSettings] = None,
                  device: Optional[str] = None,
                  duration: Optional[float] = None,
                  precheck: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """统一的转写结果载荷：失败与成功同构，绝不用假文本填空。"""
    return {
        "status": status,
        "model": FIXED_MODEL,
        # 表1 #8 步骤 2)：model_version 与 model 同值，保留 model 兼容旧消费方
        "model_version": FIXED_MODEL,
        "language": FIXED_LANGUAGE,
        "model_dir": settings.model_dir if settings else None,
        "device": device,
        "reason": reason,
        "detail": detail,
        "text": text,
        "duration": duration,
        # 预检命中时记录时长/信噪比证据；未命中为 None（正常转写路径）
        "precheck": precheck,
        "beam_size": int(settings.beam_size) if settings else None,
        "initial_prompt_enabled": bool(settings and settings.initial_prompt.strip()),
        "prompt_used": False,
        "prompt_fallback": False,
        "paragraph_count": len(paragraphs or []),
        "paragraphs": paragraphs or [],
    }


def run_transcription(transcriber: "WhisperTranscriber", source: Path, *,
                      sample_id: str, settings: TranscriptionSettings,
                      segment_dir: Path, relative_prefix: str) -> Dict[str, Any]:
    """转写 → 段落切分 → 裁片落盘，返回记录级载荷（表1 #8 步骤 2)+3)）。"""
    result = transcriber.transcribe(source)
    if result.get("status") != STATUS_OK:
        # 保留真实产出态：not_applicable（无音轨）/ no_speech（空转写或预检未通过）
        # / failed（解码、模型）
        return build_payload(result.get("status") or STATUS_FAILED,
                             reason=result.get("reason"),
                             detail=result.get("detail"), settings=settings,
                             device=transcriber.device,
                             duration=result.get("duration"),
                             precheck=result.get("precheck"))

    segments = finalize_segments(result.get("segments") or [])
    if not segments:
        return build_payload(STATUS_NO_SPEECH, reason=REASON_EMPTY_TEXT, settings=settings,
                             device=transcriber.device)

    paragraphs = group_paragraphs(
        segments, silence_seconds=settings.paragraph_silence_seconds,
        max_segment_seconds=settings.max_segment_seconds)
    waveform = result.get("audio")
    if waveform is not None and paragraphs:
        try:
            cut_paragraphs(waveform, result.get("sample_rate") or WHISPER_SAMPLE_RATE,
                           paragraphs, target_dir=segment_dir, sample_id=sample_id,
                           relative_prefix=relative_prefix)
        except Exception:  # noqa: BLE001 - 裁片失败不影响转写文本输出
            for paragraph in paragraphs:
                paragraph.pop("segment_path", None)
                paragraph.pop("segment_bytes", None)
    payload = build_payload(STATUS_OK, text=result.get("text"), paragraphs=paragraphs,
                            settings=settings, device=transcriber.device,
                            duration=result.get("duration"))
    payload["prompt_used"] = bool(result.get("prompt_used", False))
    payload["prompt_fallback"] = bool(result.get("prompt_fallback", False))
    return payload


#: 视觉时间索引条目上限（与 #17 默认 ``max_time_ranges=50`` 对齐）：
#: 超过上限时按「连续帧合组」降采样，保持时间段首尾相接，不做跳帧。
MAX_VISUAL_INDEX_ENTRIES = 50


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _segment_marks(segment: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取分段内的关键帧标记：优先 ``frames[].timestamp/path``，退回 ``keyframes[]`` 时间点。

    只做时间/路径搬用，不推断语义；时间点必须落在分段 ``[start, end]`` 内。
    """
    start = _number(segment.get("start"))
    end = _number(segment.get("end"))
    if start is None or end is None:
        return []
    marks: List[Dict[str, Any]] = []
    frames = segment.get("frames")
    if isinstance(frames, list):
        for order, frame in enumerate(frames):
            if not isinstance(frame, dict):
                continue
            timestamp = _number(frame.get("timestamp"))
            if timestamp is None:
                timestamp = _number(frame.get("time"))
            if timestamp is None:
                continue
            path = frame.get("path") or frame.get("file") or frame.get("relative_path")
            marks.append({
                "timestamp": timestamp,
                "path": path.strip() if isinstance(path, str) and path.strip() else None,
                "frame_index": frame.get("frame_index", order),
            })
    if not marks:
        keyframes = segment.get("keyframes")
        if isinstance(keyframes, list):
            for order, value in enumerate(keyframes):
                timestamp = _number(value)
                if timestamp is None and isinstance(value, dict):
                    timestamp = _number(value.get("timestamp"))
                    if timestamp is None:
                        timestamp = _number(value.get("time"))
                if timestamp is None:
                    continue
                path = value.get("path") if isinstance(value, dict) else None
                marks.append({
                    "timestamp": timestamp,
                    "path": path.strip() if isinstance(path, str) and path.strip() else None,
                    "frame_index": order,
                })
    marks = [mark for mark in marks if start <= mark["timestamp"] <= end]
    marks.sort(key=lambda item: item["timestamp"])
    unique: List[Dict[str, Any]] = []
    for mark in marks:
        if unique and abs(mark["timestamp"] - unique[-1]["timestamp"]) < 1e-6:
            if unique[-1]["path"] is None and mark["path"]:
                unique[-1] = mark
            continue
        unique.append(mark)
    return unique


def milliseconds(seconds: Any) -> Optional[int]:
    """秒 → 整数毫秒（四舍五入，负数按 0 处理）；非数值返回 ``None``。"""
    value = _number(seconds)
    if value is None:
        return None
    if value <= 0:
        return 0
    return int(math.floor(value * 1000.0 + 0.5))


def _segment_entry(index: int, start: float, end: float) -> Dict[str, Any]:
    return {
        "index": index,
        "start": round(start, 3),
        "end": round(end, 3),
        "start_ms": milliseconds(start),
        "end_ms": milliseconds(end),
        "text": None,
        "segment_path": None,
        "source": "visual_segment",
        "granularity": "segment",
    }


def _frame_entries(index: int, segment: Dict[str, Any],
                   marks: List[Dict[str, Any]], group: int) -> List[Dict[str, Any]]:
    """按关键帧把分段切成首尾相接的子时间段；``group``>1 时连续帧合组降采样。"""
    start = float(segment["start"])
    end = float(segment["end"])
    boundaries: List[Dict[str, Any]] = []
    for position in range(0, len(marks), group):
        chunk = marks[position:position + group]
        boundaries.append(chunk[0])
    entries: List[Dict[str, Any]] = []
    for order, mark in enumerate(boundaries):
        item_start = start if order == 0 else mark["timestamp"]
        item_end = boundaries[order + 1]["timestamp"] if order + 1 < len(boundaries) else end
        if item_end <= item_start:
            continue
        entries.append({
            "index": index + len(entries),
            "start": round(item_start, 3),
            "end": round(item_end, 3),
            "start_ms": milliseconds(item_start),
            "end_ms": milliseconds(item_end),
            "text": None,
            "segment_path": None,
            "source": "visual_frame",
            "granularity": "frame" if group == 1 else "frame_group",
            "frame_path": mark.get("path"),
            "frame_index": mark.get("frame_index"),
            "timestamp": round(float(mark["timestamp"]), 3),
            "segment_start": round(start, 3),
            "segment_end": round(end, 3),
        })
    return entries


def visual_time_index(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """无声视频的**视觉时间索引**：用 #6 已切好的视频分段与关键帧时间。

    粒度规则（不猜语义、不编文本）：

    * 分段内至少两个关键帧 → 按帧切分成**首尾相接**的子时间段，标
      ``source=visual_frame``、带 ``frame_path/frame_index/timestamp``；
    * 没有可用关键帧 → 退回分段级时间段，标 ``source=visual_segment``；
    * 条目总数超过 :data:`MAX_VISUAL_INDEX_ENTRIES` 时，连续帧合组降采样
      （``granularity=frame_group``），仍然首尾相接、不跳帧。

    条目结构与 ``time_index`` 同构，供 #17 在「转写时间索引缺失」时兜底预填；
    与 ``time_index`` 分开存放，避免 #11/#19 等既有消费方把视觉分段误当成语音时间轴。
    """
    video = record.get("video")
    if not isinstance(video, dict):
        return []
    segments: List[Dict[str, Any]] = []
    for segment in video.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        start = _number(segment.get("start"))
        end = _number(segment.get("end"))
        if start is None or end is None or end <= start:
            continue
        segments.append({"start": start, "end": end,
                         "marks": _segment_marks(segment)})
    if not segments:
        return []

    total_marks = sum(len(item["marks"]) for item in segments)
    group = 1
    if total_marks > MAX_VISUAL_INDEX_ENTRIES:
        group = (total_marks + MAX_VISUAL_INDEX_ENTRIES - 1) // MAX_VISUAL_INDEX_ENTRIES

    index: List[Dict[str, Any]] = []
    for segment in segments:
        marks = segment["marks"]
        if len(marks) >= 2:
            entries = _frame_entries(len(index), segment, marks, group)
            if entries:
                index.extend(entries)
                continue
        index.append(_segment_entry(len(index), segment["start"], segment["end"]))
    return index


def time_index_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """表1 #8 输出：时间索引（段落级起止时间 + 裁片路径）。

    时间口径：``start_ms``/``end_ms`` 为整数毫秒、左闭右开 ``[start, end)``，
    由秒值 ×1000 四舍五入而来，并保证按索引单调、相邻不重叠（重叠时后段起点前移
    到前段终点，等长时终点至少 +1ms）。``start``/``end`` 秒字段保留一版，
    供尚未升级的消费方（如 #17）使用。
    """
    if payload.get("status") != STATUS_OK:
        return []
    index: List[Dict[str, Any]] = []
    previous_end: Optional[int] = None
    for paragraph in payload.get("paragraphs") or []:
        start_ms = milliseconds(paragraph.get("start"))
        end_ms = milliseconds(paragraph.get("end"))
        if start_ms is not None and previous_end is not None and start_ms < previous_end:
            start_ms = previous_end
        if start_ms is not None and end_ms is not None and end_ms <= start_ms:
            end_ms = start_ms + 1
        if end_ms is not None:
            previous_end = end_ms
        index.append({
            "index": paragraph.get("index"),
            "start": paragraph.get("start"),
            "end": paragraph.get("end"),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": paragraph.get("text"),
            "segment_path": paragraph.get("segment_path"),
        })
    return index
