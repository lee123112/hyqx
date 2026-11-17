"""测试用的可重复数据构造工具（不依赖网络与真实媒体解码）。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def timelines(video_times, audio_times, text_times) -> Dict[str, Any]:
    return {
        "video": [{"id": "f%d" % i, "timestamp_sec": v} for i, v in enumerate(video_times)],
        "audio": [{"id": "a%d" % i, "start_sec": v, "end_sec": round(v + 0.9, 6)}
                  for i, v in enumerate(audio_times)],
        "text": [{"id": "t%d" % i, "start_sec": v, "end_sec": round(v + 0.85, 6)}
                 for i, v in enumerate(text_times)],
    }


def bindings(count: int) -> List[Dict[str, str]]:
    return [{"binding_id": "b%d" % i, "video": "f%d" % i, "audio": "a%d" % i, "text": "t%d" % i}
            for i in range(count)]


def anchors(offset: float = 0.0, span: float = 2.0) -> List[Dict[str, float]]:
    return [{"video_sec": 0.0, "source_sec": round(offset, 6)},
            {"video_sec": span, "source_sec": round(span + offset, 6)}]


def make_record(sample_id: Optional[str] = "s-1", *, video=(0.0, 1.0, 2.0),
                audio=(0.01, 1.01, 2.01), text=(0.01, 1.01, 2.01),
                anchor_offset: Optional[float] = 0.0, with_bindings: bool = True,
                with_timeline: bool = True,
                timeline_file: Optional[str] = None, media: Optional[list] = None,
                timeline: Optional[Dict[str, Any]] = None, modality=None,
                **extra) -> Dict[str, Any]:
    """构造一条记录；``anchor_offset=None`` 表示不带锚点，``with_timeline=False`` 表示无时间轴。"""
    record: Dict[str, Any] = {"modality": modality or ["video", "audio", "text"]}
    if sample_id is not None:
        record["sample_id"] = sample_id
    if timeline is not None:
        record["timeline"] = timeline
    elif with_timeline and timeline_file is None:
        record["timeline"] = timelines(list(video), list(audio), list(text))
    if timeline_file is not None:
        record["timeline_file"] = timeline_file
    if with_bindings and timeline is None and timeline_file is None:
        record["temporal_bindings"] = bindings(len(video))
    if anchor_offset is not None:
        record["temporal_anchors"] = anchors(anchor_offset)
    if media is not None:
        record["media"] = media
    record.update(extra)
    return record


def default_media() -> Dict[str, bytes]:
    return {
        "media/tl_ok.json": json.dumps(
            {"video": [{"id": "f0", "timestamp_sec": 0.0}],
             "audio": [{"id": "a0", "start_sec": 0.02}],
             "text": [{"id": "t0", "start_sec": 0.01}],
             "temporal_anchors": anchors(0.0, 1.0),
             "temporal_bindings": [{"binding_id": "b0", "video": "f0", "audio": "a0",
                                    "text": "t0"}]},
            ensure_ascii=False).encode("utf-8"),
        "media/tl_broken.json": b"{oops",
        "media/subtitles.srt": "1\n00:00:00,010 --> 00:00:00,900\n测试字幕\n".encode("utf-8"),
    }


def zip_bytes(members: Dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def write_jsonl(path: Path, records: Iterable[Any], *, raw_tail: Optional[str] = None,
                trailing_blank: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [json.dumps(record, ensure_ascii=False) if isinstance(record, dict) else str(record)
            for record in records]
    if raw_tail is not None:
        body.append(raw_tail)
    text = "\n".join(body) + "\n"
    if trailing_blank:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def bundle_zip(records: Optional[Iterable[Dict[str, Any]]] = None,
               media: Optional[Dict[str, bytes]] = None) -> bytes:
    """标准素材包：demo.jsonl + manifest.json + media/*。"""
    records = list(records) if records is not None else [
        make_record("api-1", video=(0.0, 1.0), audio=(0.01, 1.01), text=(0.01, 1.01)),
        make_record("api-2", video=(0.0, 1.0, 2.0), audio=(2.0, 3.0, 4.0),
                    text=(0.0, 1.0, 2.0)),
    ]
    members: Dict[str, bytes] = {
        "demo.jsonl": ("\n".join(json.dumps(r, ensure_ascii=False) for r in records)
                       + "\n").encode("utf-8"),
        "manifest.json": json.dumps({
            "format": "crossmodal-manifest-v1",
            "entrypoint": "demo.jsonl",
            "artifacts": [{"path": "demo.jsonl", "role": "main"},
                          {"path": "media/tl_ok.json", "role": "media"}],
        }, ensure_ascii=False).encode("utf-8"),
    }
    members.update(media if media is not None else default_media())
    return zip_bytes(members)


def read_jsonl(path) -> List[Dict[str, Any]]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
