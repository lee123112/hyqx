"""幂等重建算子 15 的可重复样例（不下载任何外部大数据）。

产物：
- ``demo.jsonl``      18 条非空记录 + 1 条空行，覆盖通过与不通过、边界值、
                      不适用（缺时间轴/纯图文/无绑定）、时间戳非法、sidecar 缺失/损坏、
                      无锚点未校准、重复 sample_id、缺 sample_id、坏 JSON、非对象行；
- ``manifest.json``   素材包清单（``crossmodal-manifest-v1``）；
- ``media/tl_ok.json``    合法 sidecar 时间轴（供 ``timeline_file`` 引用）；
- ``media/tl_broken.json`` 非法 sidecar（验证原始隔离路径）；
- ``media/subtitles.srt``  随包搬运的字幕 sidecar。

用法：``python dataset/sample/make_sample.py``
"""
from __future__ import annotations

import json
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = SAMPLE_DIR / "media"
DEMO_PATH = SAMPLE_DIR / "demo.jsonl"
MANIFEST_PATH = SAMPLE_DIR / "manifest.json"

#: 缺时间轴的示例记录引用的、故意不存在的媒体（验证 media_missing 计数）
ABSENT_MEDIA = "media/absent_clip.mp4"


def _timelines(video_times, audio_times, text_times):
    return {
        "video": [{"id": "f%d" % index, "timestamp_sec": value}
                  for index, value in enumerate(video_times)],
        "audio": [{"id": "a%d" % index, "start_sec": value, "end_sec": round(value + 0.9, 6)}
                  for index, value in enumerate(audio_times)],
        "text": [{"id": "t%d" % index, "start_sec": value, "end_sec": round(value + 0.85, 6)}
                 for index, value in enumerate(text_times)],
    }


def _bindings(count):
    return [
        {"binding_id": "b%d" % index, "video": "f%d" % index,
         "audio": "a%d" % index, "text": "t%d" % index}
        for index in range(count)
    ]


def _anchors(source_offset):
    """音视频/字幕共用 source 时钟；锚点对把 source 时钟线性映射回视频时钟。"""
    return [
        {"video_sec": 0.0, "source_sec": round(0.0 + source_offset, 6)},
        {"video_sec": 2.0, "source_sec": round(2.0 + source_offset, 6)},
    ]


def _record(sample_id, *, timelines=None, bindings=None, anchors=None,
            timeline_file=None, modality=None, media=None, extra=None):
    record = {"sample_id": sample_id, "modality": modality or ["video", "audio", "text"]}
    if timelines is not None:
        record["timeline"] = timelines
    if timeline_file is not None:
        record["timeline_file"] = timeline_file
    if bindings is not None:
        record["temporal_bindings"] = bindings
    if anchors is not None:
        record["temporal_anchors"] = anchors
    if media is not None:
        record["media"] = media
    if extra:
        record.update(extra)
    return record


def build_records():
    """返回样例行（字符串）列表；其中包含故意写坏的原始行。"""
    lines = []

    # 1) 正常通过：三条时间轴在同一时钟，锚点为恒等映射（applied=true）
    lines.append(_record(
        "tl-normal",
        timelines=_timelines([0.0, 1.0, 2.0], [0.05, 1.02, 2.03], [0.02, 1.0, 2.0]),
        bindings=_bindings(3), anchors=_anchors(0.0),
        media=["media/subtitles.srt", ABSENT_MEDIA],
        extra={"processing": {"12": {"status": "passed"}},
               "alignment": {"overall": "aligned"},
               "batch_id": "batch-a", "source_id": "src-1", "license": "可训练"}))

    # 2) 语音/字幕时钟整体偏移 0.9s：原始差值 0.9>0.5，线性插值校准后 ≈0.02 → 通过
    lines.append(_record(
        "tl-shifted",
        timelines=_timelines([0.0, 1.0, 2.0], [0.9, 1.9, 2.9], [0.88, 1.88, 2.88]),
        bindings=_bindings(3), anchors=_anchors(0.9),
        extra={"batch_id": "batch-a", "license": "可训练"}))

    # 3) 时间差 2.0s 超过 0.5s 上限 → 样本隔离库
    lines.append(_record(
        "tl-over-threshold",
        timelines=_timelines([0.0, 1.0, 2.0], [2.0, 3.0, 4.0], [0.0, 1.0, 2.0]),
        bindings=_bindings(3), anchors=_anchors(0.0),
        extra={"batch_id": "batch-a", "license": "可训练"}))

    # 4) 边界值：时间差恰好 0.5s（等于上限）→ 通过
    lines.append(_record(
        "tl-at-threshold",
        timelines=_timelines([0.0, 1.0, 2.0], [0.5, 1.5, 2.5], [0.0, 1.0, 2.0]),
        bindings=_bindings(3), anchors=_anchors(0.0),
        extra={"batch_id": "batch-a"}))

    # 5) 纯图文：完全没有时间戳 → not_applicable（不隔离，也不能写成通过）
    lines.append(_record("tl-image-text", modality=["image", "text"],
                         extra={"batch_id": "batch-b", "license": "可训练"}))

    # 6) 缺语音时间轴
    partial = _timelines([0.0, 1.0], [0.0, 1.0], [0.0, 1.0])
    del partial["audio"]
    lines.append(_record("tl-missing-audio", timelines=partial, bindings=_bindings(2),
                         anchors=_anchors(0.0)))

    # 7) 缺字幕时间轴
    partial = _timelines([0.0, 1.0], [0.0, 1.0], [0.0, 1.0])
    del partial["text"]
    lines.append(_record("tl-missing-text", timelines=partial, bindings=_bindings(2),
                         anchors=_anchors(0.0)))

    # 8) 缺视频时间轴
    partial = _timelines([0.0, 1.0], [0.0, 1.0], [0.0, 1.0])
    del partial["video"]
    lines.append(_record("tl-missing-video", timelines=partial, bindings=_bindings(2),
                         anchors=_anchors(0.0)))

    # 9) 三条时间轴齐全但没有绑定关系 → not_applicable
    lines.append(_record(
        "tl-no-binding",
        timelines=_timelines([0.0, 1.0], [0.0, 1.0], [0.0, 1.0]), anchors=_anchors(0.0)))

    # 10) 时间戳不可解析 → 原始隔离区
    broken = _timelines([0.0, 1.0, 2.0], [0.0, 1.0, 2.0], [0.0, 1.0, 2.0])
    broken["video"][1]["timestamp_sec"] = "abc"
    lines.append(_record("tl-bad-timestamp", timelines=broken, bindings=_bindings(3),
                         anchors=_anchors(0.0)))

    # 11) 时间轴来自合法 sidecar → 通过（同时验证 sidecar 随包搬运）
    lines.append(_record("tl-sidecar-ok", timeline_file="media/tl_ok.json"))

    # 12) sidecar 不存在 → 原始隔离区
    lines.append(_record("tl-sidecar-missing", timeline_file="media/tl_absent.json"))

    # 13) sidecar 不是合法 JSON → 原始隔离区
    lines.append(_record("tl-sidecar-broken", timeline_file="media/tl_broken.json"))

    # 14) 无锚点：显式标 applied=false，按原始时间轴计算（未校准但真实）
    lines.append(_record(
        "tl-no-anchors",
        timelines=_timelines([0.0, 1.0, 2.0], [0.1, 1.1, 2.1], [0.1, 1.1, 2.1]),
        bindings=_bindings(3)))

    # 15) 重复 sample_id：照常判定（表1 #15 未要求去重）
    lines.append(_record(
        "tl-normal",
        timelines=_timelines([0.0, 1.0], [0.03, 1.03], [0.01, 1.01]),
        bindings=_bindings(2), anchors=_anchors(0.0)))

    # 16) 缺 sample_id → 原始隔离区
    lines.append({"modality": ["video", "audio", "text"],
                  "timeline": _timelines([0.0, 1.0], [0.0, 1.0], [0.0, 1.0]),
                  "temporal_bindings": _bindings(2),
                  "temporal_anchors": _anchors(0.0)})

    return lines


#: 故意写坏的原始行（验证坏行不静默丢弃）
RAW_LINES = [
    '{"sample_id": "tl-broken-json", ',   # JSON 解析失败
    '"not-an-object"',                    # 合法 JSON 但不是对象
]


def build_media():
    """返回 {相对路径: 字节} 的媒体/sidecar 内容。"""
    ok_payload = {
        "video": [{"id": "f0", "timestamp_sec": 0.0},
                  {"id": "f1", "timestamp_sec": 1.0},
                  {"id": "f2", "timestamp_sec": 2.0}],
        "audio": [{"id": "a0", "start_sec": 0.04, "end_sec": 0.94},
                  {"id": "a1", "start_sec": 1.03, "end_sec": 1.93},
                  {"id": "a2", "start_sec": 2.01, "end_sec": 2.91}],
        "text": [{"id": "t0", "start_sec": 0.01, "end_sec": 0.86},
                 {"id": "t1", "start_sec": 1.0, "end_sec": 1.85},
                 {"id": "t2", "start_sec": 2.0, "end_sec": 2.85}],
        "temporal_anchors": [{"video_sec": 0.0, "source_sec": 0.0},
                             {"video_sec": 2.0, "source_sec": 2.0}],
        "temporal_bindings": [{"binding_id": "b0", "video": "f0", "audio": "a0", "text": "t0"},
                              {"binding_id": "b1", "video": "f1", "audio": "a1", "text": "t1"},
                              {"binding_id": "b2", "video": "f2", "audio": "a2", "text": "t2"}],
    }
    subtitles = (
        "1\n00:00:00,020 --> 00:00:00,900\n示例字幕一\n\n"
        "2\n00:00:01,000 --> 00:00:01,900\n示例字幕二\n\n"
        "3\n00:00:02,000 --> 00:00:02,900\n示例字幕三\n"
    )
    return {
        "media/tl_ok.json": json.dumps(ok_payload, ensure_ascii=False, indent=2).encode("utf-8"),
        "media/tl_broken.json": b"{oops",
        "media/subtitles.srt": subtitles.encode("utf-8"),
    }


def build_manifest():
    return {
        "format": "crossmodal-manifest-v1",
        "entrypoint": "demo.jsonl",
        "artifacts": [
            {"path": "demo.jsonl", "role": "main"},
            {"path": "media/tl_ok.json", "role": "media"},
            {"path": "media/tl_broken.json", "role": "media"},
            {"path": "media/subtitles.srt", "role": "media"},
        ],
    }


def main() -> int:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for relative, payload in build_media().items():
        target = SAMPLE_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    body = [json.dumps(record, ensure_ascii=False) for record in build_records()]
    body.extend(RAW_LINES)
    # 末尾保留一条空行：验证 blank_lines 计数
    DEMO_PATH.write_text("\n".join(body) + "\n\n", encoding="utf-8")

    MANIFEST_PATH.write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("样例已重建：%s（%d 行记录 + %d 条坏行 + 1 空行）"
          % (DEMO_PATH, len(build_records()), len(RAW_LINES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
