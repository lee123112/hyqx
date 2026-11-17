"""M17/M18 integration contracts; no CLIP weights are loaded."""
import copy

import pytest

from utils import phrase_region as pr


def test_image_reference_path_is_consumed():
    assert pr.resolve_image_reference({"image_reference": {"path": "media/a.png"}}) == {
        "path": "media/a.png", "source": "image_reference.path"}


@pytest.mark.parametrize("empty", [None, "", "   ", [], {}])
def test_pair_empty_media_does_not_erase_real_reference(empty):
    record = {"pair_id": "p", "left": {"record": {"image": "media/a.png"}},
              "right": {"record": {"image": empty}}, "image": empty}
    assert pr.resolve_image_reference(record)["path"] == "media/a.png"


@pytest.mark.parametrize("empty", [None, {}, {"17": None}])
def test_pair_empty_processing_does_not_mask_root_m17_boxes(empty):
    boxes = [{"box_id": "b", "bbox": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}}]
    record = {"pair_id": "pair", "left": {"record": {"image": None, "processing": empty}},
              "right": {"record": {"sample_id": "s", "image_reference": {"path": "media/a.png"},
                                    "processing": {"16": {"decision": "auto_aligned"}}}},
              "processing": {"17": {"regions": boxes, "paths": {"image": "media/a.png"}}}}
    before = copy.deepcopy(record)
    assert pr.resolve_candidate_boxes(record)["boxes"] == boxes
    assert pr.resolve_image_reference(record)["path"] == "media/a.png"
    assert record == before


@pytest.mark.parametrize("path", ["media/v.mp4", "media/a.wav"])
def test_generic_av_path_is_never_used_as_image(path):
    assert pr.resolve_image_reference({"processing": {"17": {"path": path}}}) is None


def test_legacy_untyped_m17_path_without_extension_remains_supported():
    assert pr.resolve_image_reference({"processing": {"17": {"path": "media/image_blob"}}})["path"] == "media/image_blob"


def test_typed_av_path_does_not_reappear_as_legacy_image():
    assert pr.resolve_image_reference({"processing": {"17": {
        "path": "media/video_blob", "paths": {"image": None, "video": "media/video_blob"}}}}) is None


def test_video_frame_fallback_is_preserved_with_typed_av_path():
    record = {"processing": {"17": {"path": "media/v.mp4", "paths": {"video": "media/v.mp4"},
                                    "video_frames": [{"path": "media/frame.png"}]}}}
    assert pr.resolve_image_reference(record)["path"] == "media/frame.png"
