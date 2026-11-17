"""Audio measurement engine (``type = "audio"``).

WAV decoding uses only the standard library ``wave`` module plus numpy, so the
service never needs ffmpeg.  Other containers are attempted with
``soundfile``/``librosa`` when importable; otherwise the sample is an explicit
``ASSET_MISSING`` asset error.

Metrics
-------

``cer`` (measured_model, non-negative percent, lower is better)
    ``char_cer(reference, hypothesis)`` with character-level Levenshtein
    distance over NFKC-normalised letters/digits (whitespace and punctuation
    removed), divided by the reference length.  The reference comes from
    ``sample.transcript`` or ``sample.output``.  The hypothesis comes from an
    ASR model selected **without any network download**:

    * ``SME_WHISPER_MODEL`` points at a local whisper model directory, or
    * ``openai/whisper-tiny`` / ``openai/whisper-base`` is already present in
      the HuggingFace cache.

    When neither exists, strict mode raises HTTP 422 ``ASR_ASSET_MISSING``
    instead of inventing a number.

``speakerAccuracy`` (proxy, 0–100)
    Leave-one-out nearest-neighbour accuracy over per-utterance acoustic
    features.  Baseline uses zero-crossing/energy/centroid features; candidate
    uses an MFCC-like log-mel + DCT feature vector.  Per sample the value is
    ``100`` when the leave-one-out prediction is correct and ``0`` otherwise.

``domainF1`` (proxy, 0–100)
    Macro-F1 of the same leave-one-out predictions for the ``domain`` label,
    evaluated once per dataset and reported on every labelled sample.

Both label metrics require at least two label classes and two labelled
utterances per class; a single-class label set is reported as insufficient instead of a
meaningless ``100``.  Speaker/domain labels are read from ``sample.speaker`` /
``sample.parameters.speaker`` and ``sample.domain`` / ``sample.parameters.domain``.
"""
from __future__ import annotations

import io
import math
import os
import tempfile
import unicodedata
import wave
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from ..core.config import settings
from ..core.envelope import ApiError
from ..core.security import sanitize_text
from .base import BaseEngine, EngineResult
from .media import (
    AssetUnavailable,
    clamp,
    load_asset_bytes,
    missing_rows,
    round_metric,
    sample_asset_value,
    sample_dataset_id,
)

TARGET_SAMPLE_RATE = 16000
ASR_MESSAGE = "语音 CER 需要 ASR 模型，请配置 SME_PARAFORMER_MODEL 或 SME_WHISPER_MODEL，或导入指标"
METRICS = ("cer", "speakerAccuracy", "domainF1")

_ASR_CACHE: dict[str, Any] = {}


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def _pcm_to_float(raw: bytes, width: int, channels: int) -> np.ndarray:
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8)
        usable = octets.size - octets.size % 3
        triplets = octets[:usable].reshape(-1, 3).astype(np.int32)
        values = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        data = values.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported PCM width {width}")
    if channels > 1:
        if data.size % channels:
            data = data[:data.size - data.size % channels]
        data = data.reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32)


def _decode_wav(payload: bytes) -> tuple[np.ndarray, int] | None:
    try:
        with wave.open(io.BytesIO(payload), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            raw = handle.readframes(frames)
            if len(raw) != frames * channels * width:
                return None
    except (wave.Error, EOFError, OSError, ValueError):
        return None
    try:
        return _pcm_to_float(raw, width, max(1, channels)), int(sample_rate)
    except ValueError:
        return None


def _decode_with_soundfile(payload: bytes) -> tuple[np.ndarray, int] | None:
    try:
        import soundfile as sf  # type: ignore
    except ImportError:
        return None
    try:
        data, sample_rate = sf.read(io.BytesIO(payload), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - optional decoder failure degrades to missing asset
        return None
    return data.mean(axis=1).astype(np.float32), int(sample_rate)


def _decode_with_librosa(payload: bytes) -> tuple[np.ndarray, int] | None:
    try:
        import librosa  # type: ignore
    except ImportError:
        return None
    try:
        data, sample_rate = librosa.load(io.BytesIO(payload), sr=None, mono=True)
    except Exception:  # noqa: BLE001 - optional decoder failure degrades to missing asset
        return None
    return np.asarray(data, dtype=np.float32), int(sample_rate)


def _resample(audio: np.ndarray, sample_rate: int, target: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    if sample_rate == target or audio.size == 0:
        return audio.astype(np.float32)
    from scipy.signal import resample_poly
    divisor = math.gcd(sample_rate, target)
    return resample_poly(audio, target // divisor, sample_rate // divisor).astype(np.float32)


def decode_audio_bytes(payload: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV via stdlib, then optionally soundfile/librosa. 16 kHz mono."""
    decoded = _decode_wav(payload)
    # A malformed PCM WAV must not be rescued by a permissive optional decoder.
    if decoded is None and payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(payload), "rb") as handle:
                if len(handle.readframes(handle.getnframes())) != (
                        handle.getnframes() * handle.getnchannels() * handle.getsampwidth()):
                    raise AssetUnavailable("WAV PCM 数据截断")
        except (wave.Error, EOFError):
            pass
    if decoded is None:
        decoded = _decode_with_soundfile(payload)
    if decoded is None:
        decoded = _decode_with_librosa(payload)
    if decoded is None:
        raise AssetUnavailable(
            "音频无法解码：仅内置 WAV 解码，且 soundfile/librosa 不可用")
    audio, sample_rate = decoded
    if sample_rate <= 0:
        raise AssetUnavailable("音频采样率无效")
    if audio.size == 0:
        raise AssetUnavailable("音频为空")
    if not np.isfinite(audio).all():
        raise AssetUnavailable("音频包含 NaN/Inf")
    return _resample(audio, sample_rate), TARGET_SAMPLE_RATE


def _load_audio(sample: dict[str, Any]) -> tuple[np.ndarray, int]:
    payload, _label = load_asset_bytes(
        sample_asset_value(sample), dataset_id=sample_dataset_id(sample))
    return decode_audio_bytes(payload)


# --------------------------------------------------------------------------
# CER
# --------------------------------------------------------------------------

def normalize_text(text: Any) -> str:
    """NFKC + lowercase, keep letters/digits only (drops punctuation/spaces)."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(
        char for char in normalized
        if unicodedata.category(char)[0] in ("L", "N")
    )


def _edit_distance(reference: str, hypothesis: str) -> int:
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, ref_char in enumerate(reference, start=1):
        current = [row] + [0] * len(hypothesis)
        for column, hyp_char in enumerate(hypothesis, start=1):
            current[column] = min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + (ref_char != hyp_char),
            )
        previous = current
    return previous[-1]


def char_cer(reference: Any, hypothesis: Any) -> float:
    """Character error rate in percent; insertions can make CER exceed 100."""
    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    if not ref:
        raise ValueError("参考文本为空，无法计算 CER")
    return round_metric(100.0 * _edit_distance(ref, hyp) / len(ref), 2)


def _reference_text(sample: dict[str, Any]) -> str:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    for holder in (sample, parameters):
        for key in ("transcript", "reference", "output", "text"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _language(sample: dict[str, Any]) -> str:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    value = str(parameters.get("language") or sample.get("language") or "zh").strip().lower()
    return {"zh": "chinese", "cn": "chinese", "中文": "chinese",
            "en": "english", "英文": "english"}.get(value, value)


# --------------------------------------------------------------------------
# ASR selection (local only, never downloads)
# --------------------------------------------------------------------------

def _cache_roots() -> list[Path]:
    roots: list[Path] = []
    for name in ("HF_HOME", "TRANSFORMERS_CACHE", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(name)
        if value:
            root = Path(value)
            roots.append(root if root.name == "hub" else root / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return roots


def _cached_whisper() -> str | None:
    """Return the local snapshot directory of a cached whisper model."""
    for root in _cache_roots():
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                name = entry.name.lower()
                if not name.startswith("models--openai--whisper-"):
                    continue
                snapshots = entry / "snapshots"
                if snapshots.is_dir():
                    for snapshot in snapshots.iterdir():
                        if (snapshot / "config.json").is_file():
                            return str(snapshot)
                if (entry / "config.json").is_file():
                    return str(entry)
        except OSError:
            continue
    return None


def asr_model_ref() -> tuple[str | None, str | None]:
    """Return ``(model_ref, error_reason)`` without touching the network."""
    paraformer = (os.environ.get("SME_PARAFORMER_MODEL") or settings.paraformer_model or "").strip()
    if paraformer:
        path = Path(paraformer)
        if not path.is_absolute() and not path.is_dir():
            project_relative = settings.project_root / paraformer
            if project_relative.is_dir():
                path = project_relative
        if path.is_dir() and (path / "config.yaml").is_file() and (path / "model.pt").is_file():
            return f"funasr:{path}", None
        return None, "SME_PARAFORMER_MODEL 未指向包含 config.yaml/model.pt 的本地 Paraformer 目录"

    configured = (os.environ.get("SME_WHISPER_MODEL") or settings.whisper_model or "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute() and not path.is_dir():
            project_relative = settings.project_root / configured
            if project_relative.is_dir():
                path = project_relative
        if path.is_dir() and (path / "config.json").is_file():
            return str(path), None
        return None, "SME_WHISPER_MODEL 未指向包含 config.json 的本地模型目录"
    from ..core.model_assets import bundled_model_path
    bundled = bundled_model_path('audio', required=False)
    if bundled.is_dir() and (bundled / 'config.json').is_file():
        return str(bundled), None
    return None, "音频算子models/whisper-base缺失，请部署随算子提供的模型资产"


class _FunASRAdapter:
    """Small wrapper around FunASR's ``AutoModel`` for the audio engine."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def transcribe(self, audio: np.ndarray) -> str:
        # FunASR accepts a path reliably; keep the engine's numpy/WAV-free
        # decoder contract by materialising a temporary 16 kHz mono WAV.
        temporary = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        path = temporary.name
        temporary.close()
        try:
            pcm = np.clip(audio, -1.0, 1.0)
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(TARGET_SAMPLE_RATE)
                handle.writeframes((pcm * 32767.0).astype("<i2").tobytes())
            result = self.model.generate(input=path)
            return _asr_text(result)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


def _load_funasr(model_path: str) -> Any:
    try:
        from funasr import AutoModel
    except ImportError as exc:
        raise ApiError(
            422, "ASR_ASSET_MISSING",
            "Paraformer 需要 funasr，当前解释器未安装；请安装 requirements-asr.txt",
            {"model": _model_label(model_path)},
        ) from exc
    try:
        model = AutoModel(
            model=model_path,
            device="cpu",
            disable_update=True,
            trust_remote_code=True,
        )
    except Exception as exc:  # noqa: BLE001 - keep model loading errors explicit
        raise ApiError(
            422, "ASR_ASSET_MISSING",
            "Paraformer 模型加载失败，请检查 weight/paraformer-zh",
            {"model": _model_label(model_path), "reason": sanitize_text(exc)[:300]},
        ) from exc
    return _FunASRAdapter(model)


def _load_asr(model_ref: str) -> Any:
    if model_ref in _ASR_CACHE:
        return _ASR_CACHE[model_ref]
    if model_ref.startswith("funasr:"):
        pipe = _load_funasr(model_ref.split(":", 1)[1])
        _ASR_CACHE[model_ref] = pipe
        return pipe
    try:
        from transformers import pipeline
    except ImportError as exc:  # pragma: no cover - transformers is a hard dependency
        raise ApiError(422, "ASR_ASSET_MISSING", "ASR 模型加载失败：transformers 不可用",
                       {"model": _model_label(model_ref)}) from exc
    try:
        # model_ref is always an existing local directory, so the pipeline
        # cannot download anything.  Do not pass ``local_files_only`` here:
        # transformers 5 forwards unknown pipeline kwargs into generate().
        pipe = pipeline(
            "automatic-speech-recognition", model=model_ref, device=-1,
        )
    except Exception as exc:  # noqa: BLE001 - model asset problem must stay explicit
        raise ApiError(
            422, "ASR_ASSET_MISSING",
            "ASR 模型加载失败，请检查 SME_WHISPER_MODEL 指向的模型目录",
            {"model": _model_label(model_ref), "reason": sanitize_text(exc)[:300]},
        ) from exc
    _ASR_CACHE[model_ref] = pipe
    return pipe


def _model_label(model_ref: str) -> str:
    path = Path(model_ref[7:] if model_ref.startswith("funasr:") else model_ref)
    if path.is_dir():
        for part in path.parts:
            if part.startswith("models--"):
                return part.split("models--", 1)[1].replace("--", "/")
        return path.name
    return model_ref


def _asr_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return str(output.get("text") or "")
    if isinstance(output, list) and output:
        return _asr_text(output[0])
    return ""


def _transcribe(pipe: Any, audio: np.ndarray, language: str) -> str:
    if isinstance(pipe, _FunASRAdapter):
        return pipe.transcribe(audio)
    attempts = [
        (audio.astype(np.float32), {"language": language, "task": "transcribe"}),
        (audio.astype(np.float32), {}),
        ({"array": audio.astype(np.float32), "sampling_rate": TARGET_SAMPLE_RATE},
         {"language": language, "task": "transcribe"}),
        ({"array": audio.astype(np.float32), "sampling_rate": TARGET_SAMPLE_RATE}, {}),
    ]
    last_error: Exception | None = None
    for payload, kwargs in attempts:
        try:
            output = pipe(payload, generate_kwargs=kwargs) if kwargs else pipe(payload)
            return _asr_text(output)
        except Exception as exc:  # noqa: BLE001 - try next input shape, then surface
            last_error = exc
    raise ApiError(500, "INTERNAL_ERROR", "ASR 推理失败",
                   {"reason": sanitize_text(last_error)[:300]}) from last_error


# --------------------------------------------------------------------------
# Acoustic features
# --------------------------------------------------------------------------

def _frames(audio: np.ndarray, sample_rate: int,
            frame_ms: float = 25.0, hop_ms: float = 10.0) -> np.ndarray:
    length = max(1, int(sample_rate * frame_ms / 1000.0))
    hop = max(1, int(sample_rate * hop_ms / 1000.0))
    if audio.size < length:
        audio = np.pad(audio, (0, length - audio.size))
    count = 1 + (audio.size - length) // hop
    if count > 2000:
        starts = np.linspace(0, audio.size - length, 2000).astype(int)
        indices = starts[:, None] + np.arange(length)[None, :]
    else:
        indices = hop * np.arange(count)[:, None] + np.arange(length)[None, :]
    return audio[indices]


def _hz_to_mel(hz: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(n_filters: int, fft_size: int, sample_rate: int) -> np.ndarray:
    low, high = 20.0, min(8000.0, sample_rate / 2.0)
    mel_points = np.linspace(_hz_to_mel(np.array([low]))[0],
                             _hz_to_mel(np.array([high]))[0], n_filters + 2)
    bins = np.floor((fft_size + 1) * _mel_to_hz(mel_points) / sample_rate).astype(int)
    bins = np.clip(bins, 0, fft_size // 2)
    bank = np.zeros((n_filters, fft_size // 2 + 1), dtype=np.float32)
    for index in range(n_filters):
        left, center, right = int(bins[index]), int(bins[index + 1]), int(bins[index + 2])
        if center <= left:
            center = left + 1
        if right <= center:
            right = center + 1
        right = min(right, fft_size // 2)
        for position in range(left, min(center, bank.shape[1])):
            bank[index, position] = (position - left) / max(1, center - left)
        for position in range(center, min(right + 1, bank.shape[1])):
            bank[index, position] = (right - position) / max(1, right - center)
    return bank


def _dct_matrix(n_out: int, n_in: int) -> np.ndarray:
    rows = np.arange(n_out)[:, None]
    columns = np.arange(n_in)[None, :]
    matrix = np.cos(np.pi / n_in * (columns + 0.5) * rows) * math.sqrt(2.0 / n_in)
    matrix[0, :] /= math.sqrt(2.0)
    return matrix.astype(np.float32)


def _basic_features(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    frames = _frames(audio, sample_rate)
    sign_changes = np.signbit(frames)
    zcr = np.mean(sign_changes[:, 1:] != sign_changes[:, :-1], axis=1)
    energy = np.log(np.mean(frames ** 2, axis=1) + 1e-10)
    spectrum = np.abs(np.fft.rfft(frames * np.hamming(frames.shape[1]), n=512))
    frequencies = np.fft.rfftfreq(512, d=1.0 / sample_rate)
    total = spectrum.sum(axis=1) + 1e-10
    centroid = (spectrum * frequencies).sum(axis=1) / total
    return np.asarray([
        float(zcr.mean()), float(zcr.std()),
        float(energy.mean()), float(energy.std()),
        float(centroid.mean()), float(centroid.std()),
    ], dtype=np.float32)


def _mfcc_features(audio: np.ndarray, sample_rate: int, n_mfcc: int = 13) -> np.ndarray:
    fft_size = 512
    frames = _frames(audio, sample_rate)
    window = np.hamming(frames.shape[1]).astype(np.float32)
    power = np.abs(np.fft.rfft(frames * window, n=fft_size)) ** 2
    bank = _mel_filterbank(26, fft_size, sample_rate)
    energies = power @ bank.T
    log_mel = np.log(energies + 1e-10)
    coefficients = _dct_matrix(n_mfcc, 26) @ log_mel.T
    return np.concatenate([coefficients.mean(axis=1),
                           coefficients.std(axis=1)]).astype(np.float32)


def _label_of(sample: dict[str, Any], kind: str) -> str | None:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    aliases = {
        "speaker": ("speaker", "speakerId", "speaker_id", "spk"),
        "domain": ("domain", "domainLabel", "domain_label"),
    }[kind]
    for holder in (sample, parameters):
        for key in aliases:
            value = holder.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return None


def _loo_evaluate(features: dict[str, np.ndarray], labels: dict[str, str]
                  ) -> dict[str, Any] | None:
    """Leave-one-out nearest neighbour; returns None for degenerate label sets."""
    ids = [sample_id for sample_id in labels if sample_id in features]
    classes = sorted({labels[sample_id] for sample_id in ids})
    counts = Counter(labels[sample_id] for sample_id in ids)
    if len(ids) < 4 or len(classes) < 2 or min(counts.values()) < 2:
        return None
    matrix = np.stack([features[sample_id] for sample_id in ids])
    predictions: dict[str, str] = {}
    for index, sample_id in enumerate(ids):
        train = [position for position in range(len(ids)) if position != index]
        training = matrix[train]
        mean = training.mean(axis=0)
        scale = training.std(axis=0) + 1e-6
        distances = np.linalg.norm(
            (training - mean) / scale - (matrix[index] - mean) / scale, axis=1)
        predictions[sample_id] = labels[ids[train[int(np.argmin(distances))]]]
    correct = {sample_id: predictions[sample_id] == labels[sample_id]
               for sample_id in ids}
    accuracy = 100.0 * sum(correct.values()) / len(ids)
    f1_scores: list[float] = []
    for label in classes:
        true_positive = sum(1 for sample_id in ids
                            if predictions[sample_id] == label and labels[sample_id] == label)
        false_positive = sum(1 for sample_id in ids
                             if predictions[sample_id] == label and labels[sample_id] != label)
        false_negative = sum(1 for sample_id in ids
                             if predictions[sample_id] != label and labels[sample_id] == label)
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        f1_scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return {
        "correct": correct,
        "accuracy": round_metric(accuracy),
        "macroF1": round_metric(100.0 * sum(f1_scores) / len(f1_scores)),
        "classes": classes,
        "sampleCount": len(ids),
    }


class AudioEngine(BaseEngine):
    type = "audio"

    def measure(self, samples: list[dict[str, Any]], config: dict[str, Any], *,
                seed: int = 42, strict: bool = True) -> EngineResult:
        result = EngineResult(provenance={
            "cer": "measured_model",
            "speakerAccuracy": "proxy",
            "domainF1": "proxy",
        })
        result.notes.append(
            "CER 使用本地 ASR（Paraformer 优先，Whisper 回退，不下载模型）；"
            "说话人/领域为声学特征最近邻代理测量。")
        result.meta.update({
            "engine": "AudioEngine",
            "engineVersion": "audio-asr-v2",
            "sampleRate": TARGET_SAMPLE_RATE,
            "seed": seed,
            "sampleCount": len(samples),
        })
        result.meta["comparison"] = {
            "mode": "frozen_asr_diagnostic", "qualityConclusionEligible": False,
            "reason": "CER 两侧来自同一冻结 ASR；数据质量比较请使用 audio training protocol",
        }

        sample_ids: list[str] = []
        references: dict[str, str] = {}
        for index, sample in enumerate(samples):
            sample_id = str(sample.get("id") or f"S{index + 1:03d}")
            if sample_id in references:
                raise ApiError(422, "SAMPLE_INVALID", f"样本 ID 重复：{sample_id}")
            sample_ids.append(sample_id)
            references[sample_id] = _reference_text(sample)

        valid_references = {sid for sid, text in references.items() if normalize_text(text)}
        needs_asr = bool(valid_references)

        # Decode assets first so a missing/corrupt audio file is always reported
        # as ASSET_MISSING, even when the ASR model is also unavailable.
        audio_by_id: dict[str, np.ndarray] = {}
        for sample, sample_id in zip(samples, sample_ids):
            try:
                audio_by_id[sample_id], _rate = _load_audio(sample)
            except AssetUnavailable as exc:
                detail = str(exc)
                if strict:
                    raise ApiError(
                        422, "ASSET_MISSING",
                        f"样本 {sample_id} 的音频资产缺失或无法解码：{detail}",
                        {"sampleId": sample_id, "reason": "ASSET_MISSING"},
                    ) from exc
                for metric in METRICS:
                    result.missing.extend(
                        missing_rows(sample_id, metric, "ASSET_MISSING", detail))
                result.notes.append(f"样本 {sample_id} 跳过：{detail}")

        model_ref, model_error = asr_model_ref() if needs_asr else (None, None)
        if strict and needs_asr and model_ref is None:
            raise ApiError(
                422, "ASR_ASSET_MISSING", ASR_MESSAGE,
                {"reason": model_error,
                 "sampleIds": [sid for sid in sample_ids if references.get(sid)]},
            )

        cer_values: dict[str, float] = {}
        cer_counts: dict[str, dict[str, int]] = {}
        cer_problems: dict[str, tuple[str, str]] = {}
        for sid, text in references.items():
            if text and sid not in valid_references:
                cer_problems[sid] = ("TRANSCRIPT_INVALID", "参考转写归一化后为空")
        if needs_asr and model_ref is not None:
            pipe = _load_asr(model_ref)
            for sample, sample_id in zip(samples, sample_ids):
                reference = references.get(sample_id) or ""
                if sample_id not in valid_references or sample_id not in audio_by_id:
                    continue
                transcript = _transcribe(pipe, audio_by_id[sample_id], _language(sample))
                try:
                    cer_values[sample_id] = char_cer(reference, transcript)
                    cer_counts[sample_id] = {
                        "cerEdits": _edit_distance(normalize_text(reference), normalize_text(transcript)),
                        "cerReferenceChars": len(normalize_text(reference)),
                    }
                except ValueError as exc:
                    cer_problems[sample_id] = ("TRANSCRIPT_INVALID", str(exc))
        elif needs_asr:
            for sample_id in sample_ids:
                if sample_id in valid_references and sample_id in audio_by_id:
                    cer_problems[sample_id] = ("ASR_UNAVAILABLE", ASR_MESSAGE)
        for sample_id in sample_ids:
            if sample_id in audio_by_id and not references.get(sample_id):
                cer_problems[sample_id] = (
                    "TRANSCRIPT_MISSING", "样本缺少 output/transcript 参考文本")

        basic_features: dict[str, np.ndarray] = {}
        mfcc_features: dict[str, np.ndarray] = {}
        speaker_labels: dict[str, str] = {}
        domain_labels: dict[str, str] = {}
        for sample, sample_id in zip(samples, sample_ids):
            if sample_id not in audio_by_id:
                continue
            audio = audio_by_id[sample_id]
            basic_features[sample_id] = _basic_features(audio, TARGET_SAMPLE_RATE)
            mfcc_features[sample_id] = _mfcc_features(audio, TARGET_SAMPLE_RATE)
            speaker = _label_of(sample, "speaker")
            domain = _label_of(sample, "domain")
            if speaker:
                speaker_labels[sample_id] = speaker
            if domain:
                domain_labels[sample_id] = domain

        speaker_basic = _loo_evaluate(basic_features, speaker_labels)
        speaker_mfcc = _loo_evaluate(mfcc_features, speaker_labels)
        domain_basic = _loo_evaluate(basic_features, domain_labels)
        domain_mfcc = _loo_evaluate(mfcc_features, domain_labels)

        speaker_values: dict[str, float] = {}
        domain_values: dict[str, float] = {}
        if speaker_basic and speaker_mfcc:
            for sample_id in speaker_basic["correct"]:
                speaker_values[sample_id] = 100.0 if speaker_basic["correct"][sample_id] else 0.0
        if domain_basic and domain_mfcc:
            for sample_id in domain_basic["correct"]:
                domain_values[sample_id] = domain_basic["macroF1"]

        speaker_problem = None
        if not speaker_basic:
            if not speaker_labels:
                speaker_problem = ("SPEAKER_LABEL_MISSING",
                                   "至少有一个样本缺少 speaker 标签")
            else:
                speaker_problem = ("SPEAKER_LABELS_INSUFFICIENT",
                                   "speaker 标签至少需要 2 个类别且每类至少 2 条有标签语音")
        domain_problem = None
        if not domain_basic:
            if not domain_labels:
                domain_problem = ("DOMAIN_LABEL_MISSING",
                                  "至少有一个样本缺少 domain 标签")
            else:
                domain_problem = ("DOMAIN_LABELS_INSUFFICIENT",
                                  "domain 标签至少需要 2 个类别且每类至少 2 条有标签语音")

        incomplete: list[dict[str, str]] = []
        for sample_id in sample_ids:
            if sample_id not in audio_by_id:
                continue
            baseline: dict[str, float] = {}
            candidate: dict[str, float] = {}

            if sample_id in cer_values:
                baseline["cer"] = cer_values[sample_id]
                candidate["cer"] = cer_values[sample_id]
                baseline.update(cer_counts[sample_id])
                candidate.update(cer_counts[sample_id])
            elif sample_id in cer_problems:
                reason, detail = cer_problems[sample_id]
                result.missing.extend(missing_rows(sample_id, "cer", reason, detail))
                incomplete.append({"sampleId": sample_id, "metric": "cer",
                                   "reason": reason})

            if sample_id in speaker_values:
                baseline["speakerAccuracy"] = speaker_values[sample_id]
                candidate["speakerAccuracy"] = (
                    100.0 if speaker_mfcc["correct"][sample_id] else 0.0)
            elif speaker_problem:
                result.missing.extend(
                    missing_rows(sample_id, "speakerAccuracy", *speaker_problem))
                incomplete.append({"sampleId": sample_id, "metric": "speakerAccuracy",
                                   "reason": speaker_problem[0]})
            else:
                detail = f"样本 {sample_id} 缺少 speaker 标签"
                result.missing.extend(missing_rows(
                    sample_id, "speakerAccuracy", "SPEAKER_LABEL_MISSING", detail))
                incomplete.append({"sampleId": sample_id, "metric": "speakerAccuracy",
                                   "reason": "SPEAKER_LABEL_MISSING"})

            if sample_id in domain_values:
                baseline["domainF1"] = domain_values[sample_id]
                candidate["domainF1"] = (
                    domain_mfcc["macroF1"] if domain_mfcc else domain_values[sample_id])
            elif domain_problem:
                result.missing.extend(missing_rows(sample_id, "domainF1", *domain_problem))
                incomplete.append({"sampleId": sample_id, "metric": "domainF1",
                                   "reason": domain_problem[0]})
            else:
                detail = f"样本 {sample_id} 缺少 domain 标签"
                result.missing.extend(missing_rows(
                    sample_id, "domainF1", "DOMAIN_LABEL_MISSING", detail))
                incomplete.append({"sampleId": sample_id, "metric": "domainF1",
                                   "reason": "DOMAIN_LABEL_MISSING"})

            if baseline or candidate:
                result.measurements[sample_id] = {
                    "baseline": baseline, "candidate": candidate}

        if strict and incomplete:
            preview = "；".join(
                f"{item['sampleId']}.{item['metric']}({item['reason']})"
                for item in incomplete[:8])
            raise ApiError(
                422, "MEASUREMENTS_INCOMPLETE",
                f"语音指标不完整：{preview}",
                {"items": incomplete, "asrReason": model_error},
            )

        edits = sum(v["cerEdits"] for v in cer_counts.values())
        chars = sum(v["cerReferenceChars"] for v in cer_counts.values())
        result.meta["cer"] = {
            "normalization": "nfkc-lower-letters-numbers-v1",
            "aggregation": "corpus_edit_distance",
            "edits": edits, "referenceChars": chars,
            "corpusCER": round_metric(100 * edits / chars) if chars else None,
        }
        result.meta["asr"] = {
            "requested": needs_asr,
            "available": model_ref is not None,
            "model": _model_label(model_ref) if model_ref else None,
            "reason": model_error,
        }
        result.meta["labels"] = {
            "speaker": {
                "labelled": len(speaker_labels),
                "classes": len({*speaker_labels.values()}),
                "baselineAccuracy": speaker_basic["accuracy"] if speaker_basic else None,
                "candidateAccuracy": speaker_mfcc["accuracy"] if speaker_mfcc else None,
            },
            "domain": {
                "labelled": len(domain_labels),
                "classes": len({*domain_labels.values()}),
                "baselineMacroF1": domain_basic["macroF1"] if domain_basic else None,
                "candidateMacroF1": domain_mfcc["macroF1"] if domain_mfcc else None,
            },
            "cerSamples": len(cer_values),
        }
        return result
