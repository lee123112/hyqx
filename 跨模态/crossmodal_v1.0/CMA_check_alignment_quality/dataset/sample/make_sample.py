"""生成算子 20 的可复现样例数据（**全部本地合成，不下载任何外部数据**）。

产物：
- ``dataset/sample/demo.jsonl``：13 行候选池样本（含重复标识）；
- ``dataset/sample/manifest.json``：运行清单（入口 ``demo.jsonl``）；
- ``dataset/sample/media/*``：真实可解码的 PNG / WAV / MP4，以及故意损坏的文件。

运行::

    python dataset/sample/make_sample.py

脚本完全确定性：重复执行得到字节一致的媒体与 JSONL。
"""
from __future__ import annotations

import json
import struct
import wave
import zlib
from pathlib import Path

import numpy as np

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
DEMO_PATH = SAMPLE_DIR / "demo.jsonl"
MANIFEST_PATH = SAMPLE_DIR / "manifest.json"

ENTRYPOINT = "demo.jsonl"


# --------------------------------------------------------------------------- #
# 真实媒体合成
# --------------------------------------------------------------------------- #

def write_png(path: Path, width: int = 96, height: int = 64, rgb=(198, 62, 48)) -> None:
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_wav(path: Path, seconds: float = 0.4, freq: float = 440.0,
              samplerate: int = 16000, silent: bool = False,
              amplitude: int = 9000, quiet_seconds: float = 0.0,
              quiet_amplitude: int = 20) -> None:
    total = max(1, int(seconds * samplerate))
    if silent:
        samples = [0] * total
    else:
        samples = [int(amplitude * np.sin(2 * np.pi * freq * i / samplerate))
                   for i in range(total)]
        tail = int(quiet_seconds * samplerate)
        for index in range(max(0, total - tail), total):
            samples[index] = int(quiet_amplitude
                                 * np.sin(2 * np.pi * freq * index / samplerate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(samplerate)
        handle.writeframes(struct.pack("<%dh" % len(samples), *samples))


def write_mp4(path: Path, frames: int = 8, width: int = 64, height: int = 48) -> bool:
    """用 OpenCV ``mp4v`` 写一段真实可解码的视频；本机无编码器时返回 False。"""
    try:
        import cv2
    except Exception:  # pragma: no cover
        return False
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0,
                             (width, height))
    if not writer.isOpened():  # pragma: no cover
        writer.release()
        return False
    try:
        for index in range(frames):
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame[:, :, 0] = (index * 31) % 256
            frame[:, :, 1] = (index * 57) % 256
            frame[:, :, 2] = 160
            writer.write(frame)
    finally:
        writer.release()
    return True


# --------------------------------------------------------------------------- #
# 样例记录
# --------------------------------------------------------------------------- #

def build_records(has_video: bool) -> list:
    video_path = "media/clip_a.mp4"
    broken_video = "media/broken.mp4"

    def text_image(sample_id, text, image, ppl, brisque, reverse_passed,
                   with_reverse=True):
        record = {
            "sample_id": sample_id,
            "source": "crossmodal_candidate_pool",
            "text": {"content": text, "lang": "zh"},
            "image": {"path": image, "width": 96, "height": 64, "format": "PNG",
                      "brisque": brisque},
            "semantic_unit": {"unit_id": "%s_u1" % sample_id, "type": "scene"},
            "metadata": {"batch": "demo_10_6", "owner": "opus20"},
            "qc": {
                "text_fluency": {"ppl": ppl, "check_status": "checked",
                                 "model": {"name": "upstream-ppl-pipeline"}},
                "media_quality": {"brisque": brisque, "check_status": "checked"},
            },
        }
        if with_reverse:
            record["qc"]["reverse_verify"] = {
                "passed": reverse_passed,
                "score": 0.71 if reverse_passed else 0.12,
                "check_status": "checked",
                "model": {"name": "CLIP-ViT/B-32", "weights": "shared-instance"},
            }
        return record

    records = [
        text_image("vlm-0001", "金毛犬在草地上接飞盘", "media/ok_a.png",
                   38.5, 30.2, True),
        text_image("vlm-0002", "红色方块放在白色桌面上", "media/ok_b.png",
                   42.1, 28.7, True),
        # 上游缺少反向验证结论 → 需要本地整池 CLIP 反向检索
        text_image("vlm-0003", "蓝色杯子在窗边", "media/ok_c.png",
                   45.8, 31.5, True, with_reverse=False),
        # 落在 10% 补测子集且无上游困惑度/BRISQUE → 会真实尝试本地补测
        # （本机无语言模型与 opencv-contrib，如实记 insufficient_evidence）
        {
            "sample_id": "vlm-0004",
            "text": {"content": "绿色瓶子放在木桌上"},
            "image": {"path": "media/ok_c.png", "width": 96, "height": 64,
                      "format": "PNG"},
            "qc": {"reverse_verify": {"passed": True, "score": 0.68,
                                      "check_status": "checked",
                                      "model": {"name": "CLIP-ViT/B-32"}}},
            "metadata": {"batch": "demo_10_6",
                         "fallback_demo": "命中 10% 补测子集"},
        },
    ]

    audio_video = {
        "sample_id": "av-0004",
        "text": {"content": "视频里有人在讲解产品包装"},
        "video": {"path": video_path if has_video else "media/clip_a.mp4",
                  "duration": 0.8, "fps": 10,
                  "frames": [{"path": "media/ok_a.png", "index": 0}]},
        "audio": {"path": "media/tone.wav", "sample_rate": 16000, "channels": 1,
                  "duration": 0.4, "snr_db": 21.4},
        "qc": {"media_quality": {"snr_db": 21.4, "check_status": "checked"}},
        "metadata": {"batch": "demo_10_6"},
    }
    records.append(audio_video)

    # 无上游 SNR 且命中 10% 补测子集 → 真实解码后本地计算 SNR
    records.append({
        "sample_id": "av-0011",
        "video": {"path": "media/clip_a.mp4", "frames": [{"path": "media/ok_b.png"}]},
        "audio": {"path": "media/mixed.wav", "sample_rate": 16000},
        "metadata": {"batch": "demo_10_6",
                     "fallback_demo": "命中 10% 补测子集，本地真实计算 SNR"},
    })

    records.extend([
        # 引用文件不存在 → 原始隔离区【原始文件-无法解析】
        {"sample_id": "origin-0006", "text": {"content": "引用了不存在的图片"},
         "image": {"path": "media/missing.png"}},
        # 文件无法打开 → 原始隔离区【原始文件-损坏】
        {"sample_id": "origin-0007", "text": {"content": "图片字节已损坏"},
         "image": {"path": "media/broken.png"}},
        # 视频解不出 → 原始隔离区【原始文件-损坏】
        {"sample_id": "origin-0008", "video": {"path": broken_video},
         "audio": {"path": "media/tone.wav"}},
        # 音频整段无声 → 原始隔离区【原始文件-损坏】
        {"sample_id": "origin-0009", "video": {"path": "media/clip_a.mp4"},
         "audio": {"path": "media/silent.wav"}},
        # 只有单一模态 → 样本隔离库【样本-语义不通】
        {"sample_id": "sample-0010", "text": {"content": "只有文本一个模态"}},
        # 文本为空 → 样本隔离库【样本-语义不通】
        {"sample_id": "sample-0011", "text": {"content": "   \n  "},
         "image": {"path": "media/ok_a.png"}},
        # 上游反向验证不一致 → flagged（不拦样本，进人工复核队列）
        text_image("flag-0012", "描述与图像内容明显不符", "media/ok_a.png",
                   52.0, 33.4, False),
    ])
    return records


def main() -> int:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    write_png(MEDIA_DIR / "ok_a.png", rgb=(198, 62, 48))
    write_png(MEDIA_DIR / "ok_b.png", rgb=(52, 132, 196))
    write_png(MEDIA_DIR / "ok_c.png", rgb=(66, 158, 92))
    (MEDIA_DIR / "broken.png").write_bytes(b"this-is-not-a-valid-png")
    write_wav(MEDIA_DIR / "tone.wav")
    write_wav(MEDIA_DIR / "mixed.wav", quiet_seconds=0.15)
    write_wav(MEDIA_DIR / "silent.wav", silent=True)
    (MEDIA_DIR / "broken.wav").write_bytes(b"this-is-not-a-valid-wav")
    has_video = write_mp4(MEDIA_DIR / "clip_a.mp4")
    (MEDIA_DIR / "broken.mp4").write_bytes(b"this-is-not-a-valid-mp4")
    if not has_video:  # pragma: no cover - 环境缺 mp4v 编码器
        print("[warn] 本机 OpenCV 无法写出 mp4v，视频样本将落到原始隔离区（如实反映）")

    records = build_records(has_video)
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    # 故意加入一行坏 JSON 与一个重复标识，用于演示 rejects 与去重台账
    lines.append('{"sample_id": "vlm-0001", "note": "重复标识示例", "text": {"content": "重复样本"}}')
    lines.append('{"sample_id": "broken-line", "text": ')   # 坏 JSON
    lines.insert(6, "")                                       # 空行
    DEMO_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    MANIFEST_PATH.write_text(json.dumps({
        "format": "crossmodal-manifest-v1",
        "entrypoint": ENTRYPOINT,
        "artifacts": [{"path": ENTRYPOINT, "role": "candidate_pool"}],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("已生成样例：")
    print("  ", DEMO_PATH)
    print("  ", MANIFEST_PATH)
    for path in sorted(MEDIA_DIR.iterdir()):
        print("   media/%s  %d bytes" % (path.name, path.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
