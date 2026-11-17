"""业务字段解析与候选框规范化测试（表1 #18 输入契约）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from conftest import box, record, write_png  # noqa: E402
from utils import phrase_region as pr  # noqa: E402


# --------------------------------------------------------------------------- #
# 描述解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload,expected_source", [
    ({"text": {"content": "金毛犬"}}, "text.content"),
    ({"text": "金毛犬"}, "text"),
    ({"text": {"description": "金毛犬"}}, "text.description"),
    ({"metadata": {"description": "金毛犬"}}, "metadata.description"),
    ({"description": "金毛犬"}, "description"),
])
def test_resolve_description_sources(payload, expected_source):
    result = pr.resolve_description(payload)
    assert result["value"] == "金毛犬"
    assert result["source"] == expected_source


def test_resolve_description_ignores_operator_18_node():
    payload = {"processing": {"18": {"description": "不应被当作描述"}},
               "text": {"description": "金毛犬"}}
    assert pr.resolve_description(payload)["source"] == "text.description"


def test_resolve_description_missing():
    assert pr.resolve_description({})["value"] is None
    assert pr.resolve_description({"text": {"content": "   "}})["value"] is None


def test_resolve_description_rejects_filename_only():
    """描述整体等于图像文件名时视为无效描述（文件名不能当词）。"""
    result = pr.resolve_description({"text": {"content": "shot_001.png"}},
                                    image_path="media/shot_001.png")
    assert result["value"] is None
    assert result["reason"] == "description_is_filename"


# --------------------------------------------------------------------------- #
# 图像解析
# --------------------------------------------------------------------------- #

def test_resolve_image_from_string():
    assert pr.resolve_image_reference({"image": "media/a.png"}) == {
        "path": "media/a.png", "source": "image"}


def test_resolve_image_from_object():
    assert pr.resolve_image_reference(
        {"image": {"path": "media/a.png"}})["path"] == "media/a.png"


def test_resolve_image_from_processing_17():
    payload = {"processing": {"17": {"modality": "image",
                                     "paths": {"image": "media/a.png"}}}}
    assert pr.resolve_image_reference(payload)["path"] == "media/a.png"


def test_resolve_image_absent():
    assert pr.resolve_image_reference({"text": "x"}) is None


# --------------------------------------------------------------------------- #
# 候选框解析（#17 局部框）
# --------------------------------------------------------------------------- #

def test_resolve_boxes_from_regions():
    payload = {"regions": [box("b1"), box("b2")]}
    result = pr.resolve_candidate_boxes(payload)
    assert result["source"] == "regions"
    assert len(result["boxes"]) == 2


def test_resolve_boxes_from_processing_17():
    payload = {"processing": {"17": {"regions": [box("b1")]}}}
    result = pr.resolve_candidate_boxes(payload)
    assert result["source"] == "processing.17.regions"
    assert len(result["boxes"]) == 1


def test_resolve_boxes_from_image_object():
    payload = {"image": {"path": "a.png", "regions": [box("b1")]}}
    result = pr.resolve_candidate_boxes(payload)
    assert result["source"] == "image.regions"


def test_resolve_boxes_absent_is_empty_list():
    result = pr.resolve_candidate_boxes({"text": "x"})
    assert result["boxes"] == []
    assert result["source"] is None


# --------------------------------------------------------------------------- #
# 框规范化
# --------------------------------------------------------------------------- #

def test_normalize_box_xywh_pixel():
    result = pr.normalize_box(box("b1", x=1, y=2, w=30, h=40), 0)
    assert result["box_id"] == "b1"
    assert result["bbox"] == {"x": 1, "y": 2, "w": 30, "h": 40,
                              "coord": "xywh-pixel"}


def test_normalize_box_xyxy_pixel_sequence():
    raw = {"box_id": "b1", "bbox": [10, 20, 40, 70]}
    result = pr.normalize_box(raw, 0)
    assert result["bbox"]["coord"] == "xyxy-pixel"
    assert (result["bbox"]["x"], result["bbox"]["y"]) == (10, 20)
    assert (result["bbox"]["w"], result["bbox"]["h"]) == (30, 50)


def test_normalize_box_flat_xywh():
    result = pr.normalize_box({"id": "b9", "x": 0, "y": 0, "w": 5, "h": 6}, 0)
    assert result["box_id"] == "b9"
    assert result["bbox"]["coord"] == "xywh-pixel"


def test_normalize_box_normalized_sequence():
    raw = {"box_id": "b1", "bbox": [0.1, 0.2, 0.5, 0.8], "coord": "xyxy-normalized"}
    result = pr.normalize_box(raw, 0)
    assert result["bbox"]["coord"] == "xyxy-normalized"
    assert result["bbox"]["w"] == pytest.approx(0.4)


def test_normalize_box_generates_stable_id_when_absent():
    result = pr.normalize_box({"bbox": [0, 0, 4, 4]}, 2)
    assert result["box_id"] == "box-003"


@pytest.mark.parametrize("raw", [
    {"bbox": [0, 0, 0, 4]},                 # 宽为 0
    {"bbox": [0, 0, 4, -1]},                # 高为负
    {"bbox": ["a", 0, 4, 4]},               # 非数字
    {"bbox": [0, 0, 4]},                    # 长度不足
    {"bbox": {"x": 0, "y": 0}},             # 缺 w/h
    {"bbox": [float("nan"), 0, 4, 4]},      # NaN
    {"bbox": [-5, 0, 4, 4]},                # 负坐标
    {},                                     # 完全没有坐标
])
def test_normalize_box_rejects_invalid(raw):
    with pytest.raises(pr.BoxError):
        pr.normalize_box(raw, 0)


def test_crop_rect_pixel_returns_int_box():
    normalized = pr.normalize_box(box("b1", x=1, y=2, w=30, h=40), 0)
    assert pr.crop_rect(normalized, 64, 48) == (1, 2, 31, 42)


def test_crop_rect_clamps_into_image():
    """超出图像右下边界时按图像范围夹紧（原点必须非负，负原点见 reject 用例）。"""
    normalized = pr.normalize_box({"bbox": [0, 0, 200, 200]}, 0)
    rect = pr.crop_rect(normalized, 64, 48)
    assert rect == (0, 0, 64, 48)


def test_crop_rect_empty_after_clamp_raises():
    """框合法但完全落在图像之外时，裁剪结果为空必须报错（不得静默产出空图）。"""
    normalized = pr.normalize_box({"bbox": {"x": 100, "y": 0, "w": 20, "h": 20}}, 0)
    with pytest.raises(pr.BoxError):
        pr.crop_rect(normalized, 64, 48)


def test_normalize_box_rejects_negative_origin():
    with pytest.raises(pr.BoxError):
        pr.normalize_box({"bbox": {"x": -1, "y": 0, "w": 4, "h": 4}}, 0)


def test_crop_rect_normalized_scales_to_pixels():
    normalized = pr.normalize_box({"bbox": [0.5, 0.5, 1.0, 1.0],
                                   "coord": "xyxy-normalized"}, 0)
    assert pr.crop_rect(normalized, 64, 48) == (32, 24, 64, 48)


# --------------------------------------------------------------------------- #
# 记录解析
# --------------------------------------------------------------------------- #

def test_resolve_record_flat():
    spec = pr.resolve_record(record("sample-0001", regions=[box("b1")]))
    assert spec["identifier"] == "sample-0001"
    assert spec["sample_id"] == "sample-0001"
    assert spec["image"]["path"] == "media/shot_001.png"
    assert spec["description"]["value"] == "金毛犬在草地上接飞盘"
    assert len(spec["boxes"]) == 1


def test_resolve_record_requires_identifier():
    spec = pr.resolve_record({"text": "x", "image": "a.png"})
    assert spec["identifier"] is None


def test_resolve_record_reads_upstream_tokens():
    spec = pr.resolve_record(record(tokens=["金毛犬", "草地"]))
    assert spec["upstream_tokens"] == ["金毛犬", "草地"]


def test_resolve_record_pair_form():
    payload = {
        "pair_id": "pair-001",
        "left": {"sample_id": "s1", "record": {"text": {"content": "金毛犬"}}},
        "right": {"sample_id": "s2",
                  "record": {"image": "media/shot_001.png", "regions": [box("b1")]}},
    }
    spec = pr.resolve_record(payload)
    assert spec["identifier"] == "pair-001"
    assert spec["image"]["path"] == "media/shot_001.png"
    assert spec["description"]["value"] == "金毛犬"
    assert len(spec["boxes"]) == 1


# --------------------------------------------------------------------------- #
# 无图视频：按帧裁图（v1.0.1）
# --------------------------------------------------------------------------- #

def test_resolve_frame_reference_prefers_processing_17_video_frames():
    payload = {"processing": {"17": {"video_frames": [
        {"path": "media/vid/frame_000001.jpg", "timestamp": 0.0},
        {"path": "media/vid/frame_000002.jpg", "timestamp": 1.0}]}}}
    ref = pr.resolve_frame_reference(payload)
    assert ref == {"path": "media/vid/frame_000001.jpg",
                   "source": "processing.17.video_frames.path"}


def test_resolve_frame_reference_from_video_frames_ignores_non_images():
    payload = {"video": {"path": "media/vid/silent.mp4", "frames": [
        {"path": "media/vid/silent.mp4"}, {"path": "media/vid/f.png"}]}}
    assert pr.resolve_frame_reference(payload)["path"] == "media/vid/f.png"
    assert pr.resolve_frame_reference({"video": {"path": "media/vid/a.mp4"}}) is None


def test_resolve_image_reference_skips_video_path_and_falls_back_to_frame():
    """#17 对视频记录的 path 是视频容器：不能当图像打开，必须退回关键帧。"""
    payload = {"processing": {"17": {"path": "media/vid/silent.mp4",
                                     "video_frames": [
                                         {"path": "media/vid/frame_000001.jpg"}]}}}
    assert pr.resolve_image_reference(payload)["source"] == "processing.17.video_frames.path"
    assert pr.resolve_image_reference(
        {"processing": {"17": {"path": "media/shot.png"}}})["source"] == "processing.17.path"


def test_normalize_box_keeps_frame_path():
    raw = dict(box("b1"), frame_path="media/vid/frame_000001.jpg", frame_timestamp=1.5)
    normalized = pr.normalize_box(raw, 0)
    assert normalized["frame_path"] == "media/vid/frame_000001.jpg"
    assert normalized["frame_timestamp"] == 1.5
    assert "frame_path" not in pr.normalize_box(box("b2"), 0)


# --------------------------------------------------------------------------- #
# 裁图扩边 / 背景抑制（v1.0.1 效果优化，默认关闭）
# --------------------------------------------------------------------------- #

def test_crop_rects_without_padding_returns_same_rect():
    normalized = pr.normalize_box(box("b1", x=2, y=2, w=20, h=16), 0)
    outer, inner = pr.crop_rects(normalized, 64, 48, 0.0)
    assert outer == inner == (2, 2, 22, 18)


def test_crop_rects_padding_expands_outer_only():
    normalized = pr.normalize_box(box("b1", x=10, y=10, w=20, h=16), 0)
    outer, inner = pr.crop_rects(normalized, 64, 48, 0.10)
    assert inner == (10, 10, 30, 26)
    assert outer == (8, 8, 32, 28)          # 2px = 20*0.1 / 16*0.1（四舍五入）


def test_crop_rects_padding_is_clamped_to_image():
    normalized = pr.normalize_box(box("b1", x=0, y=0, w=20, h=16), 0)
    outer, inner = pr.crop_rects(normalized, 64, 48, 0.25)
    assert outer[0] == 0 and outer[1] == 0  # 左上不会越界成负数
    assert inner == (0, 0, 20, 16)


def test_crop_rect_padding_backward_compatible():
    normalized = pr.normalize_box(box("b1", x=10, y=10, w=20, h=16), 0)
    assert pr.crop_rect(normalized, 64, 48) == (10, 10, 30, 26)
    assert pr.crop_rect(normalized, 64, 48, 0.10) == (8, 8, 32, 28)


def test_encode_crops_by_source_omits_default_crop_options(tmp_path):
    """默认（紧贴框、无抑制）时保持旧调用签名，测试替身/旧后端不受影响。"""
    write_png(tmp_path / "media" / "f1.png")
    boxes = [pr.normalize_box(box("b1"), 0)]
    encoder = pr_test_stub()
    pr.encode_crops_by_source(encoder, boxes, ["media/f1.png"],
                              {"media/f1.png": tmp_path / "media" / "f1.png"})
    assert encoder.crop_options_calls == [None]


def test_encode_crops_by_source_forwards_custom_crop_options(tmp_path):
    write_png(tmp_path / "media" / "f1.png")
    boxes = [pr.normalize_box(box("b1"), 0)]

    class Options:
        padding_ratio = 0.05
        background_suppression = "mean"

    encoder = pr_test_stub()
    pr.encode_crops_by_source(encoder, boxes, ["media/f1.png"],
                              {"media/f1.png": tmp_path / "media" / "f1.png"},
                              crop_options=Options())
    assert encoder.crop_options_calls == [{"padding_ratio": 0.05,
                                           "background_suppression": "mean"}]


def test_suppress_background_keeps_inner_pixels():
    Image = pytest.importorskip("PIL.Image")
    from utils.backends import _suppress_background

    crop = Image.new("RGB", (10, 10), (200, 40, 40))
    outer, inner = (0, 0, 10, 10), (2, 2, 8, 8)
    masked = _suppress_background(crop, outer, inner, "gray")
    assert masked.size == (10, 10)
    assert masked.getpixel((0, 0)) == (114, 114, 114)   # 框外统一中性灰
    assert masked.getpixel((5, 5)) == (200, 40, 40)     # 框内保持原样


# --------------------------------------------------------------------------- #
# 关系条目与节点字段（v1.0.1）
# --------------------------------------------------------------------------- #

def test_relation_entry_records_word_variant_by_default():
    entry = pr.relation_entry("小汽车", pr.normalize_box(box("b1"), 0), 0.31,
                              "auto_prefill", None, "prefilled")
    assert entry["phrase"] == "小汽车"
    assert entry["matched_text"] == "小汽车"
    assert entry["variant"] == "word"
    assert entry["phrase_tokens"] == ["小汽车"]


def test_relation_entry_records_noun_phrase_variant():
    entry = pr.relation_entry("小汽车", pr.normalize_box(box("b1"), 0), 0.31,
                              "auto_prefill", None, "prefilled",
                              best_score=0.33, matched_text="红色小汽车",
                              variant="noun_phrase",
                              phrase_tokens=["红色", "小汽车"])
    assert entry["phrase"] == "小汽车"          # 一词一框：phrase 仍是词
    assert entry["matched_text"] == "红色小汽车"
    assert entry["variant"] == "noun_phrase"
    assert entry["phrase_tokens"] == ["红色", "小汽车"]
    assert entry["best_score"] == pytest.approx(0.33)


def test_base_node_exposes_optimization_fields():
    node = pr.base_node(status="pending_manual", action="review", reason="x",
                        label="待人工", destination="待人工确认清单",
                        route_enabled=True, min_clip_score=0.25,
                        tokenizer_mode="pretrained")
    for key in ("phrases_total", "phrases_used", "token_variants",
                "crop_padding_ratio", "background_suppression", "class_profile",
                "tokens_dropped_detail"):
        assert key in node
    assert node["crop_padding_ratio"] == 0.0
    assert node["background_suppression"] == "none"


def pr_test_stub():
    from conftest import StubEncoder
    return StubEncoder()


def test_encode_crops_by_source_groups_and_marks_missing(tmp_path):
    from conftest import StubEncoder, write_png

    write_png(tmp_path / "media" / "f1.png")
    boxes = [pr.normalize_box(dict(box("b1"), frame_path="media/f1.png"), 0),
             pr.normalize_box(dict(box("b2"), frame_path="media/missing.png"), 1),
             pr.normalize_box(box("b3"), 2)]          # 无 frame_path → 主图像
    encoder = StubEncoder()
    source_paths = [pr.box_image_path(item, "media/main.png") for item in boxes]
    assert source_paths == ["media/f1.png", "media/missing.png", "media/main.png"]
    resolved = {"media/f1.png": tmp_path / "media" / "f1.png",
                "media/missing.png": tmp_path / "media" / "missing.png",
                "media/main.png": tmp_path / "media" / "f1.png"}
    vectors = pr.encode_crops_by_source(encoder, boxes, source_paths, resolved)
    assert len(vectors) == 3
    assert vectors[0] is not None and vectors[2] is not None
    assert vectors[1] is None                      # 缺失帧整组记 None，不借别的图顶替
    assert len(encoder.crop_calls) == 2            # 两个可用分组各编码一次
