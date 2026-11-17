"""幂等生成算子 13 的样例数据集（真实 WAV + 候选配对 JSONL）。

用法：``python dataset/sample/make_sample.py``
产物：
- ``media/seg_*.wav``：真实的 16kHz 单声道 PCM 合成语音（非静音，可被真实解码）；
- ``demo.jsonl``：默认路线（``enabled=false``）透传样例，含坏行与流程状态样例；
- ``demo_enabled.jsonl``：音文路线开启样例（含编辑距离超标、缺转写、缺描述、无音文对）；
- ``manifest.json``、``README.md``。
"""
from __future__ import annotations

import io
import json
import math
import struct
import wave
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
SAMPLE_RATE = 16000


def synth_wav(path: Path, duration: float, base_freq: float, *,
              channels: int = 1, silent: bool = False) -> None:
    """合成一段类语音的谐波信号（含起止淡入淡出），保证可被真实解码且非静音。"""
    frames = int(round(duration * SAMPLE_RATE))
    payload = bytearray()
    for index in range(frames):
        moment = index / float(SAMPLE_RATE)
        if silent:
            value = 0.0
        else:
            attack = min(moment / 0.06, 1.0)
            release = min(max((frames - index) / (0.09 * SAMPLE_RATE), 0.0), 1.0)
            envelope = 0.5 * (1.0 - math.cos(math.pi * attack * release))
            value = 0.34 * envelope * (
                math.sin(2.0 * math.pi * base_freq * moment)
                + 0.55 * math.sin(2.0 * math.pi * 2.0 * base_freq * moment)
                + 0.30 * math.sin(2.0 * math.pi * 3.0 * base_freq * moment)
                + 0.20 * math.sin(2.0 * math.pi * 5.0 * base_freq * moment)
            )
        value = max(-1.0, min(1.0, value))
        sample = int(round(value * 32767))
        for _ in range(channels):
            payload += struct.pack("<h", sample)
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(payload))
    path.write_bytes(buffer.getvalue())


def audio_object(relative: str, transcript: str | None, duration: float,
                 segments=None) -> dict:
    payload = {
        "path": relative,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "duration": duration,
        "segments": list(segments or []),
        "transcript_source": "whisper-base",
    }
    if transcript is not None:
        payload["transcript"] = transcript
    return payload


def pair(pair_id: str, transcript: str, description: str, audio: str,
         duration: float, source_id: str) -> dict:
    """与 #11 ``11_candidate_pairs.jsonl`` 同构的音文候选配对。"""
    return {
        "pair_id": pair_id,
        "group_id": "grp-%s" % source_id,
        "modality_pair": ["text", "audio"],
        "evidence": {"type": "A", "criterion": "same_source_id", "value": {"source_id": source_id}},
        "evidence_priority": 1,
        "left": {
            "sample_id": "%s-audio" % pair_id,
            "modalities": ["audio"],
            "record": {
                "sample_id": "%s-audio" % pair_id,
                "source_id": source_id,
                "batch_id": "batch-demo",
                "audio": audio_object(audio, transcript, duration),
                "text": None,
                "processing": {"08": {"status": "success", "transcript": transcript}},
                "metadata": {"parent_asset_id": "asset-%s" % source_id},
            },
        },
        "right": {
            "sample_id": "%s-text" % pair_id,
            "modalities": ["text"],
            "record": {
                "sample_id": "%s-text" % pair_id,
                "source_id": source_id,
                "batch_id": "batch-demo",
                "audio": None,
                "text": {"content": description, "original": description, "lang": "zh"},
                "processing": {"09": {"status": "success", "description": description}},
                "metadata": {"description_source": "caption"},
            },
        },
        "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
                     "uniqueId": "batch-demo_file-demo"},
    }


def main() -> int:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    media_files = [
        (MEDIA_DIR / "seg_01.wav", 1.4, 178.0, {}),
        (MEDIA_DIR / "seg_02.wav", 1.2, 205.0, {}),
        (MEDIA_DIR / "seg_03.wav", 1.0, 232.0, {}),
        (MEDIA_DIR / "seg_04.wav", 1.6, 158.0, {"channels": 2}),
        (MEDIA_DIR / "seg_silent.wav", 0.6, 200.0, {"silent": True}),
    ]
    for path, duration, freq, kwargs in media_files:
        synth_wav(path, duration, freq, **kwargs)

    # ---- 默认路线样例（enabled=false：原样透传，开关语义可被直接观察） ----
    demo_records = [
        pair("pair-01", "今天天气很好我们一起去公园散步吧", "今天天气很好我们一起去公园散步吧",
             "media/seg_01.wav", 1.4, "src-demo-A"),
        pair("pair-02", "音频文本一致性测试样例语音转写", "音频文本一致性测试样例语音转写",
             "media/seg_02.wav", 1.2, "src-demo-B"),
        pair("pair-03", "金毛犬在草地上接飞盘", "金毛犬在草地上接飞盘",
             "media/seg_03.wav", 1.0, "src-demo-C"),
    ]
    # 无音文对：两侧都没有音频 → 流程状态，不隔离
    no_audio = pair("pair-04", "室内室外场景描述", "室内室外场景描述",
                    "media/seg_silent.wav", 0.6, "src-demo-D")
    for side in ("left", "right"):
        no_audio[side]["record"]["audio"] = None
    demo_records.append(no_audio)
    # 缺转写 → 流程待转写
    missing_transcript = pair("pair-05", "对齐描述已就绪", "对齐描述已就绪",
                              "media/seg_02.wav", 1.2, "src-demo-E")
    missing_transcript["left"]["record"]["audio"].pop("transcript", None)
    missing_transcript["left"]["record"]["processing"] = {}
    demo_records.append(missing_transcript)

    with open(BASE_DIR / "demo.jsonl", "w", encoding="utf-8", newline="\n") as stream:
        for record in demo_records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.write("{这不是合法 JSON 的一行\n")
        stream.write("\n")
        stream.write(json.dumps([1, 2, 3], ensure_ascii=False) + "\n")

    # ---- 开启路线样例（enabled=true，用于阈值与台账语义验证） ----
    enabled_records = [
        pair("pair-e01", "今天天气很好我们一起去公园散步吧", "今天天气很好我们一起去公园散步吧",
             "media/seg_01.wav", 1.4, "src-demo-A"),
        # 编辑距离超标：转写与描述几乎不重叠 → 判不通过，无需模型
        pair("pair-e02", "金毛犬在草地上接飞盘", "一段完全无关的室内静物描述",
             "media/seg_02.wav", 1.2, "src-demo-B"),
        pair("pair-e03", "音频文本一致性测试样例", "音频文本一致性测试样例",
             "media/seg_03.wav", 1.0, "src-demo-C"),
        # 立体声输入：验证真实下混
        pair("pair-e04", "红色方块在蓝天白云下", "红色方块在蓝天白云下",
             "media/seg_04.wav", 1.6, "src-demo-F"),
    ]
    # 整段无声 → 原始隔离区（原始文件-空）
    silent_pair = pair("pair-e05", "静音片段描述", "静音片段描述",
                       "media/seg_silent.wav", 0.6, "src-demo-G")
    enabled_records.append(silent_pair)
    # 音频文件缺失 → 原始隔离区
    enabled_records.append(pair("pair-e06", "缺失音频", "缺失音频",
                                "media/not_exist.wav", 1.0, "src-demo-H"))
    # 缺描述 → 流程待补描述
    missing_desc = pair("pair-e07", "描述缺失样例", "描述缺失样例",
                        "media/seg_01.wav", 1.4, "src-demo-I")
    missing_desc["right"]["record"]["text"] = None
    missing_desc["right"]["record"]["processing"] = {}
    enabled_records.append(missing_desc)

    with open(BASE_DIR / "demo_enabled.jsonl", "w", encoding="utf-8", newline="\n") as stream:
        for record in enabled_records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "format": "crossmodal-manifest-v1",
        "entrypoint": "demo.jsonl",
        "artifacts": [{"path": "demo.jsonl", "role": "main"}]
                     + [{"path": "media/%s" % path.name, "role": "media"} for path, _d, _f, _k
                        in media_files],
    }
    (BASE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("样例生成完成：demo.jsonl=%d 行（含 1 坏 JSON 行、1 非对象行）、"
          "demo_enabled.jsonl=%d 行、media=%d 个"
          % (len(demo_records) + 2, len(enabled_records), len(media_files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
