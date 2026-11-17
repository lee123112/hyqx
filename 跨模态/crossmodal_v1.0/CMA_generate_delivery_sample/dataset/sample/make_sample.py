"""生成算子 26 的仓库自带样例（真实、可复现、不联网、不含任何媒体二进制）。

产出：
    dataset/sample/demo.jsonl               #23 可交付包入口（含三路线与各类异常行）
    dataset/sample/manifest.json           #23 交付包清单（version_state=deliverable）
    dataset/sample/version.json            #23 版本说明（enabled_routes 等）
    dataset/sample/materials/*             #质量报告/抽样结论/隔离清单/版本说明
    dataset/sample/pending_package/*       #待发布包（用于验证 degraded 路径）

样例内容说明（全部为演示用合成文本，非真实用户数据）：
- 图文 6 条（含 1 条超长描述会被截断、1 条无描述）、音文 3 条、视文 2 条 → 共 11 条可交付；
- 另有 2 条隔离样本（1 条带 #21 隔离标记、1 条出现在包内隔离清单）、
  1 条缺 sample_id、1 条重复 sample_id、1 条媒体引用为绝对路径、
  1 行损坏 JSON、1 行非对象 JSON —— 全部进入独立清单，不静默丢弃。

用法：``python dataset/sample/make_sample.py``
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

SAMPLE_DIR = Path(__file__).resolve().parent
ROUTES = ("image_text", "audio_text", "video_text")
MODALITIES = {"image_text": ["image", "text"], "audio_text": ["audio", "text"],
              "video_text": ["video", "text"]}
SCORE_OPERATOR = {"image_text": 12, "audio_text": 13, "video_text": 14}
SCORE_MODEL = {"image_text": "CLIP-ViT/B-32", "audio_text": "wav2vec2-base-zh",
               "video_text": "CLIP-ViT/B-32+时序池化"}

#: (sample_id, route, 描述, 分数, 分档, 标注结论, 是否人工确认, 授权, 批次)
ITEMS = [
    ("cm-i-0001", "image_text", "一只橘猫坐在木质窗台上，午后光线斜照",
     0.412, "auto_aligned", "对齐", False, "可训练", "batch-20261001"),
    ("cm-i-0002", "image_text", "城市夜景航拍，高架桥车流形成光带",
     0.377, "auto_aligned", "对齐", False, "可训练", "batch-20261001"),
    ("cm-i-0003", "image_text", "蓝色帆船停靠在白色沙滩边，天空有积云",
     0.341, "auto_aligned", "存疑", False, "可训练", "batch-20261002"),
    ("cm-i-0004", "image_text", "手工拉面制作过程特写，案板上有面粉",
     0.298, "pending_manual", "存疑", False, "可训练", "batch-20261002"),
    ("cm-i-0005", "image_text",
     "一段用于验证描述摘要截断行为的长文本：" + "山间清晨雾气沿着山谷缓慢流动，" * 12,
     0.452, "auto_aligned", "对齐", False, "可训练", "batch-20261003"),
    ("cm-i-0006", "image_text", None, 0.310, "auto_aligned", "对齐", False,
     "可训练", "batch-20261003"),
    ("cm-a-0001", "audio_text", "一段城市早高峰路况播报，主持人语速偏快",
     0.286, "pending_manual", "对齐", False, "可训练", "batch-20261004"),
    ("cm-a-0002", "audio_text", "清晨菜市场的环境声，夹杂摊主招呼声",
     0.264, "pending_manual", "存疑", False, "可训练", "batch-20261004"),
    ("cm-a-0003", "audio_text", "一段钢琴独奏练习曲，节奏平稳",
     0.331, "auto_aligned", "对齐", True, "可训练", "batch-20261005"),
    ("cm-v-0001", "video_text", "雨天公交车窗外景物流动，带雨刷声",
     0.358, "auto_aligned", "对齐", False, "可训练", "batch-20261005"),
    ("cm-v-0002", "video_text", "烘焙店出炉可颂的短视频，蒸汽升起",
     0.322, "auto_aligned", "存疑", False, "可训练", "batch-20261006"),
]


def record(sample_id, route, description, score, tier, conclusion, human, license_value,
           batch_id) -> Dict[str, Any]:
    number = SCORE_OPERATOR[route]
    processing: Dict[str, Any] = {
        "22": {"operator": "crossmodal_token", "operator_number": 22,
               "operator_version": "v1.0.0", "status": "token_generated",
               "route": route, "routes": [route],
               "markers": ["<%s>" % route.split("_")[0]],
               "token_text": "<%s> %s" % (route.split("_")[0], description or "")[:60]},
        "23": {"operator": "delivery_package", "operator_number": 23,
               "operator_version": "v1.0.0", "status": "packaged",
               "decision": "deliverable", "member": "demo.jsonl", "route": route},
        str(number): {"operator": "%s_scoring" % route, "operator_number": number,
                      "operator_version": "v1.0.0", "status": "scored",
                      "decision": tier, "tier": tier, "score": score,
                      "label": "样本-评分：%s" % ("通过" if tier == "auto_aligned"
                                                 else "待人工"),
                      "route": route,
                      "thresholds": {"isolation": 0.25, "auto_align": 0.32},
                      "model": {"name": SCORE_MODEL[route], "version": "v1",
                                "degraded": False}},
        "16": {"operator": "whole_alignment", "operator_number": 16,
               "operator_version": "v1.0.0", "status": "aligned",
               "decision": conclusion, "conclusion": conclusion,
               "confirmation_method": "human" if human else "auto",
               "human_verified": bool(human), "editor": "人工" if human else "auto"},
    }
    content = {"sample_id": sample_id, "group_id": "grp-%s" % sample_id,
               "modalities": list(MODALITIES[route]), "routes": [route], "route": route,
               "admission": {"license": license_value, "license_source": "metadata"},
               "metadata": {"batch_id": batch_id, "source": "demo_dataset",
                            "retry_count": 0,
                            "collected_at": "2026-10-01T09:00:00+08:00"},
               "text": {"content": description, "language": "zh",
                        "description_source": "caption"},
               "processing": processing,
               "operator_22": {"operator": "crossmodal_token", "operator_number": 22,
                               "operator_version": "v1.0.0",
                               "status": "token_generated", "route": route,
                               "routes": [route], "sample_id": sample_id,
                               "modalities": list(MODALITIES[route]),
                               "token_text": "<%s> %s" % (route.split("_")[0],
                                                          description or "")[:60]},
               "operator_23": {"operator": "delivery_package",
                               "operator_number": 23, "operator_version": "v1.0.0",
                               "status": "packaged", "decision": "deliverable",
                               "member": "demo.jsonl", "sample_id": sample_id,
                               "route": route},
               "alignment": {"whole": dict(processing["16"])}}
    content[route.split("_")[0]] = {
        "path": "media/%s/%s.bin" % (route, sample_id),
        "modality": route.split("_")[0],
        "mime": {"image_text": "image/png", "audio_text": "audio/wav",
                 "video_text": "video/mp4"}[route]}
    return content


def isolated_records() -> List[Dict[str, Any]]:
    one = record("cm-iso-0001", "image_text", "一段与画面语义无关的描述", 0.104,
                 "isolated", "不对齐", False, "可训练", "batch-20261006")
    one["processing"]["21"] = {"operator": "mismatch_disposal", "operator_number": 21,
                               "operator_version": "v1.0.0", "status": "isolated",
                               "decision": "isolated", "isolated": True,
                               "action": "isolate", "label": "样本-语义不通",
                               "destination": "sample_isolation_library",
                               "reason_code": "score_below_threshold",
                               "retry_count": 2}
    one["processing"]["23"]["decision"] = "deliverable"   # 包内也不得被抽样
    two = record("cm-iso-0002", "audio_text", "隔离清单中登记的样本", 0.118,
                 "isolated", "不对齐", False, "可训练", "batch-20261006")
    return [one, two]


def broken_lines(duplicate: Dict[str, Any]) -> List[str]:
    missing = {"group_id": "grp-no-id", "routes": ["image_text"],
               "modalities": ["image", "text"], "text": {"content": "缺少样本标识的样例"},
               "operator_23": {"decision": "deliverable"}}
    unsafe = record("cm-esc-0001", "image_text", "媒体引用为绝对路径", 0.35,
                    "auto_aligned", "对齐", False, "可训练", "batch-20261006")
    unsafe["image"]["path"] = "C:/temp/escape.png"
    return [
        json.dumps(missing, ensure_ascii=False),
        json.dumps(duplicate, ensure_ascii=False),
        json.dumps(unsafe, ensure_ascii=False),
        "",                                                # 空行
        '{"sample_id": "cm-bad-0001", ',                    # 损坏 JSON
        '["not", "an", "object"]',                          # 非对象
    ]


def manifest(version_state: str, entrypoint: str, files: List[str]) -> Dict[str, Any]:
    return {"format": "crossmodal-delivery-package-v1",
            "operator": "delivery_package", "operator_number": 23,
            "operator_version": "v1.0.0", "entrypoint": entrypoint,
            "version_state": version_state,
            "status": "可交付" if version_state == "deliverable" else "待发布",
            "deliverable": version_state == "deliverable",
            "missing_requirements": [] if version_state == "deliverable"
            else ["sampling_conclusion_writeback"],
            "materials": {"quality_report": {"status": "found", "written_back": True},
                          "sampling_conclusion": {"status": "found",
                                                  "written_back": True},
                          "isolation_list": {"status": "found"},
                          "version_notes": {"status": "found"}},
            "files": [{"path": item, "role": "other"} for item in files],
            "notes": "仓库自带演示交付包（合成样例，非真实用户数据）"}


def version(version_state: str, count: int) -> Dict[str, Any]:
    return {"format": "crossmodal-delivery-version-v1",
            "operator": "delivery_package", "operator_number": 23,
            "operator_version": "v1.0.0", "dataset_version": "v1.0.0-demo",
            "enabled_routes": list(ROUTES), "annotation_granularity": "whole",
            "status": "可交付" if version_state == "deliverable" else "待发布",
            "version_state": version_state, "packaged_records": count,
            "isolation_excluded_from_package": True}


MATERIALS = {
    "quality_report.json": {
        "format": "crossmodal-quality-report-v1", "operator": "alignment_qc",
        "report_id": "qr-demo-0001", "status": "written_back", "written_back": True,
        "checks": {"openable": True, "records_match_manifest": True},
        "writeback": {"completed": True, "operator": "#24"}},
    "sampling_conclusion.json": {
        "format": "crossmodal-sampling-conclusion-v1", "operator": "#24",
        "sampling_rate": 0.1, "status": "written_back",
        "writeback": {"completed": True}},
    "version_notes.json": {
        "format": "crossmodal-version-notes-v1", "dataset_version": "v1.0.0-demo",
        "enabled_routes": list(ROUTES), "annotation_granularity": "whole",
        "routes_detail": {"image_text": {"granularity": "whole"},
                          "audio_text": {"granularity": "sentence"},
                          "video_text": {"granularity": "whole"}}},
}


def write_jsonl(path: Path, rows) -> None:
    lines = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
             for item in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def build_deliverable() -> None:
    records = [record(*item) for item in ITEMS]
    isolation_ids = ["cm-iso-0002"]
    write_jsonl(SAMPLE_DIR / "demo.jsonl",
                records + isolated_records() + broken_lines(records[0]))
    files = ["demo.jsonl", "manifest.json", "version.json",
             "materials/quality_report.json", "materials/sampling_conclusion.json",
             "materials/isolation_list.jsonl", "materials/version_notes.json"]
    write_json(SAMPLE_DIR / "manifest.json",
               manifest("deliverable", "demo.jsonl", files))
    write_json(SAMPLE_DIR / "version.json", version("deliverable", len(records)))
    materials = SAMPLE_DIR / "materials"
    materials.mkdir(exist_ok=True)
    for name, payload in MATERIALS.items():
        write_json(materials / name, payload)
    write_jsonl(materials / "isolation_list.jsonl",
                [{"sample_id": item, "label": "样本-语义不通",
                  "destination": "sample_isolation_library",
                  "reason_code": "score_below_threshold"} for item in isolation_ids])


def build_pending() -> None:
    base = SAMPLE_DIR / "pending_package"
    if base.exists():
        shutil.rmtree(base)
    (base / "train").mkdir(parents=True)
    records = [record(*item) for item in ITEMS[:3]]
    for item in records:
        item["processing"]["23"]["decision"] = "pending_release"
        item["operator_23"]["decision"] = "pending_release"
    write_jsonl(base / "train" / "samples.jsonl", records)
    files = ["train/samples.jsonl", "manifest.json", "version.json"]
    write_json(base / "manifest.json",
               manifest("pending_release", "train/samples.jsonl", files))
    write_json(base / "version.json", version("pending_release", len(records)))


def main() -> int:
    build_deliverable()
    build_pending()
    print("样例已生成：%s" % SAMPLE_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
