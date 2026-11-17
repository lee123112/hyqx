"""算子 13「评分音文语义一致性」测试辅助。

本模块只使用标准库合成真实 WAV（16kHz 单声道 PCM），并提供：
- 候选配对/记录构造器（与 #11 `11_candidate_pairs.jsonl` 结构一致）；
- 微型**真实** transformers 本地模型构造器（``build_tiny_models``）。

``build_tiny_models`` 使用真实的 ``Wav2Vec2Model`` / ``BertModel`` 前向代码，只是
把隐藏维度和层数压到最小，用于验证适配层契约（本地加载、掩码池化、余弦相似度、
维度一致性），**不代表任何模型精度**，也不用于生产路径。
"""
from __future__ import annotations

import io
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SAMPLE_RATE = 16000
TINY_HIDDEN = 16

TINY_VOCAB_CHARS = (
    "音频文本一致性测试样例语音转写对齐描述"
    "今天天气很好我们一起去公园散步吧"
    "金毛犬草地接飞盘红色方块蓝天白云室内室外"
)
TINY_SPECIALS = ("[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]")


# --------------------------------------------------------------------------- #
# 真实 WAV 合成
# --------------------------------------------------------------------------- #

def wav_bytes(duration: float = 1.2, sample_rate: int = SAMPLE_RATE, freq: float = 180.0,
              amplitude: float = 0.35, channels: int = 1, silent: bool = False,
              bits: int = 16) -> bytes:
    """合成一段可解码的 PCM WAV（默认 16kHz 单声道 16bit）。"""
    frames = int(round(duration * sample_rate))
    data = bytearray()
    for index in range(frames):
        moment = index / float(sample_rate)
        if silent:
            value = 0.0
        else:
            fade = 0.5 * (1.0 - math.cos(2.0 * math.pi * min(moment / 0.05, 1.0)))
            value = amplitude * fade * (
                math.sin(2.0 * math.pi * freq * moment)
                + 0.5 * math.sin(2.0 * math.pi * 2.0 * freq * moment)
                + 0.25 * math.sin(2.0 * math.pi * 3.0 * freq * moment)
            )
        value = max(-1.0, min(1.0, value))
        sample = int(round(value * 32767))
        for _ in range(channels):
            data += struct.pack("<h", sample)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(bits // 8)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(data))
    return buffer.getvalue()


def write_wav(path, **kwargs) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(wav_bytes(**kwargs))
    return target


# --------------------------------------------------------------------------- #
# 记录构造
# --------------------------------------------------------------------------- #

def audio_object(path: str, *, transcript: Optional[str] = None, duration: float = 1.2,
                 segments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "path": path,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "duration": duration,
        "segments": list(segments or []),
    }
    if transcript is not None:
        payload["transcript"] = transcript
    return payload


def make_pair(pair_id: str, *, transcript: str, description: str,
              audio_path: str = "media/seg_01.wav", left_id: str = "s-left",
              right_id: str = "s-right", duration: float = 1.2,
              audio_side: str = "left", **extra: Any) -> Dict[str, Any]:
    """构造 #11 形态的音文候选配对（一对一一对）。"""
    audio_record = {
        "sample_id": left_id,
        "source_id": "src-A",
        "batch_id": "batch-01",
        "audio": audio_object(audio_path, transcript=transcript, duration=duration),
        "text": None,
        "processing": {},
        "metadata": {},
    }
    text_record = {
        "sample_id": right_id,
        "source_id": "src-A",
        "batch_id": "batch-01",
        "audio": None,
        "text": {"content": description, "original": description, "lang": "zh"},
        "processing": {},
        "metadata": {"description_source": "caption"},
    }
    left, right = (audio_record, text_record) if audio_side == "left" else (text_record, audio_record)
    record = {
        "pair_id": pair_id,
        "group_id": "grp-src-src-A",
        "modality_pair": ["text", "audio"],
        "evidence": {"type": "A", "criterion": "same_source_id", "value": {"source_id": "src-A"}},
        "evidence_priority": 1,
        "left": {"sample_id": left["sample_id"], "modalities": ["text", "audio"], "record": left},
        "right": {"sample_id": right["sample_id"], "modalities": ["text", "audio"], "record": right},
        "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
                     "uniqueId": "task_file"},
    }
    record.update(extra)
    return record


def make_flat_record(sample_id: str, *, transcript: str, description: str,
                     audio_path: str = "media/seg_01.wav", **extra: Any) -> Dict[str, Any]:
    """构造单记录形态（同一记录同时含音频与对齐描述）。"""
    record = {
        "sample_id": sample_id,
        "source_id": "src-B",
        "batch_id": "batch-01",
        "audio": audio_object(audio_path, transcript=transcript),
        "text": {"content": description, "original": description, "lang": "zh"},
        "processing": {},
        "metadata": {"description_source": "caption"},
    }
    record.update(extra)
    return record


def write_jsonl(path, records: Iterable[Dict[str, Any]], *, raw_lines: Optional[List[str]] = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw_lines is not None:
        target.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        return target
    with open(target, "w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


def build_input_tree(root, records, *, media: Optional[Dict[str, bytes]] = None,
                     entrypoint: str = "demo.jsonl", raw_lines=None,
                     encoding: str = "utf-8") -> Path:
    """构造一个「输入素材目录」：入口 JSONL + media/。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    if raw_lines is not None:
        payload = "\n".join(raw_lines) + "\n"
    else:
        payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    (base / entrypoint).write_bytes(payload.encode(encoding))
    for name, payload in (media or {}).items():
        target = base / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return base


def default_media() -> Dict[str, bytes]:
    return {
        "media/seg_01.wav": wav_bytes(duration=1.2, freq=180.0),
        "media/seg_02.wav": wav_bytes(duration=1.0, freq=220.0),
        "media/seg_03.wav": wav_bytes(duration=0.8, freq=260.0),
        "media/seg_silent.wav": wav_bytes(duration=0.5, silent=True),
    }


def zip_bytes(members: Dict[str, bytes]) -> bytes:
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def read_jsonl(path) -> List[Dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


# --------------------------------------------------------------------------- #
# 微型真实本地模型
# --------------------------------------------------------------------------- #

def build_tiny_models(root) -> Dict[str, str]:
    """用真实 transformers 代码构造最小本地权重目录（仅用于契约验证）。"""
    import torch
    from transformers import (BertConfig, BertModel, BertTokenizer, Wav2Vec2Config,
                              Wav2Vec2FeatureExtractor, Wav2Vec2Model)

    base = Path(root)
    audio_dir = base / "wav2vec2-base"
    text_dir = base / "bert-base-chinese"
    audio_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    torch.manual_seed(20261006)
    audio_config = Wav2Vec2Config(
        hidden_size=TINY_HIDDEN, num_hidden_layers=1, num_attention_heads=2,
        intermediate_size=TINY_HIDDEN, conv_dim=(8,), conv_stride=(2,), conv_kernel=(3,),
        num_conv_pos_embeddings=8, num_conv_pos_embedding_groups=2,
    )
    Wav2Vec2Model(audio_config).save_pretrained(str(audio_dir))
    Wav2Vec2FeatureExtractor(
        feature_size=1, sampling_rate=SAMPLE_RATE, do_normalize=True,
        return_attention_mask=False,
    ).save_pretrained(str(audio_dir))

    tokens = list(TINY_SPECIALS) + list(TINY_VOCAB_CHARS) + list(
        "abcdefghijklmnopqrstuvwxyz0123456789")
    vocab_file = text_dir / "vocab.txt"
    vocab_file.write_text("\n".join(tokens) + "\n", encoding="utf-8")
    BertTokenizer(vocab_file=str(vocab_file), do_lower_case=True).save_pretrained(str(text_dir))
    text_config = BertConfig(hidden_size=TINY_HIDDEN, num_hidden_layers=1, num_attention_heads=2,
                             intermediate_size=TINY_HIDDEN, vocab_size=len(tokens))
    BertModel(text_config).save_pretrained(str(text_dir))

    return {
        "root": str(base),
        "audio_dir": "wav2vec2-base",
        "text_dir": "bert-base-chinese",
        "audio_path": str(audio_dir),
        "text_path": str(text_dir),
    }


class StubScorer:
    """测试替身：只用于验证判定/路由，绝不进入生产路径。

    ``model_used`` 会显式写成 ``test-double``，避免任何「模型不可用仍标 model」的伪降级。
    """

    audio_model_id = "test-double-audio"
    text_model_id = "test-double-text"
    audio_model_version = "test-double"
    text_model_version = "test-double"
    hidden_size = TINY_HIDDEN

    def __init__(self, cosine: float = 1.0, fail: bool = False):
        self.cosine = float(cosine)
        self.fail = fail
        self.calls = 0

    def score(self, waveform, sample_rate: int, text: str) -> Dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("stub scorer failure")
        return {
            "cosine": self.cosine,
            "audio_model": self.audio_model_id,
            "text_model": self.text_model_id,
            "audio_model_version": self.audio_model_version,
            "text_model_version": self.text_model_version,
            "hidden_size": self.hidden_size,
        }
