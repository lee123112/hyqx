"""Integration regressions: M11 pairs, normalized aliases and v4.2 states."""
import copy

import pytest

from utils import localization as loc


def test_image_reference_is_read_and_carried():
    record = {"sample_id": "image-ref", "image_reference": {"path": "media/a.png"}}
    spec = loc.resolve_input_spec(record, [])
    assert spec["images"] == ["media/a.png"]
    assert loc.collect_media_refs(record) == ["media/a.png"]


def test_pair_null_media_and_processing_do_not_hide_real_side():
    record = {"pair_id": "pair-1",
              "left": {"record": {"sample_id": "s", "image": None, "processing": None}},
              "right": {"record": {"image_reference": {"path": "media/a.png"},
                                    "processing": {"08": {"process_status": "transcribed"}},
                                    "time_index": [{"start_ms": 12, "end_ms": 1234,
                                                    "text": "a person"}]}},
              "processing": {"16": {"status": 1, "process_status": "auto_aligned"}}}
    original = copy.deepcopy(record)
    spec = loc.resolve_input_spec(record, [])
    assert spec["sample_id"] == "s"
    assert spec["images"] == ["media/a.png"]
    assert spec["record"]["processing"]["16"]["process_status"] == "auto_aligned"
    assert spec["transcript_segments"][0]["start_sec"] == 0.012
    assert loc.collect_media_refs(record) == ["media/a.png"]
    assert record == original


def test_image_aliases_are_normalized_once_before_detection():
    record = {"sample_id": "aliases", "image": "media/a.png",
              "images": ["media\\a.png", "media/a.png"],
              "image_reference": {"path": "media/a.png"}}
    assert loc.resolve_input_spec(record, [])["images"] == ["media/a.png"]


@pytest.mark.parametrize("empty", [None, "", "   ", [], {}])
def test_pair_empty_media_does_not_erase_real_reference(empty):
    record = {"pair_id": "p", "left": {"record": {"sample_id": "s", "image": "media/a.png"}},
              "right": {"record": {"image": empty}}, "image": empty}
    assert loc.resolve_input_spec(record, [])["images"] == ["media/a.png"]


@pytest.mark.parametrize("holder", [
    {"alignment": {"whole": {"status": 0, "decision": "not_aligned"}}},
    {"alignment": {"whole": {"status": 0, "process_status": "not_aligned"}}},
    {"processing": {"16": {"status": 0, "process_status": "not_aligned"}}},
])
def test_whole_alignment_business_state_is_read(holder):
    assert loc.whole_alignment_not_aligned(holder)


@pytest.mark.parametrize("modality,path", [("video", "media/v.mp4"), ("audio", "media/a.wav")])
def test_downstream_generic_path_remains_primary_and_typed(modality, path):
    record = {"sample_id": "av", modality: {"path": path}}
    fields = loc.downstream_fields(record, {})
    assert fields["path"] == path
    assert fields["paths"][modality] == path
    assert fields["paths"]["image"] is None
