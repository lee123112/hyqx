"""A declared unresolved scoring modality must survive the native pair view."""
import pytest

from conftest import make_record, rec_12, rec_15, rec_16, rec_license
from tests.test_pool_filter import config
from utils import pool_filter, upstream


@pytest.mark.parametrize("route,kind,reason", [
    ("14", "video", "no_video_text_pair"),
    ("13", "audio", "no_audio_text_pair")])
@pytest.mark.parametrize("native", [False, True])
def test_declared_pair_modality_pending_cannot_borrow_image_score(route, kind, reason, native):
    record = make_record("declared-pair", rec_12("auto_aligned"),
                         rec_16("auto_aligned"), rec_license("可训练"),
                         rec_15("not_applicable", None))
    record["modality_pair"] = ["text", kind]
    record["processing"][route] = {"route_enabled": True, "passed": None,
                                   "process_status": "pending", "reason": reason}
    if native:
        record.update(pair_id="declared-pair", left={"record": {
            "sample_id": "text-side", "text": "Expected actual media."}},
            right={"record": {"sample_id": "media-side", kind: {}}})
    resolved = pool_filter.resolve_record(record)
    routes = upstream.read_routes(resolved["record"])
    assert routes[route]["enabled"] is True
    assert routes[route]["passed"] is None
    result = pool_filter.evaluate_record(resolved, config=config(),
                                         event_time="2026-11-11T16:00:00+08:00")
    assert result["action"] == pool_filter.ACTION_PENDING
    assert result["gates"]["route"]["reason"] == "route_pending"


def test_explicit_non_video_na_still_does_not_require_video_score():
    record = make_record("image-pair", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    record["modality_pair"] = ["text", "image"]
    record["processing"]["14"] = {"route_enabled": False, "passed": None,
                                    "process_status": "not_applicable",
                                    "reason": "non_video_route_not_applicable"}
    result = pool_filter.evaluate_record(pool_filter.resolve_record(record), config=config(),
                                         event_time="2026-11-11T16:00:00+08:00")
    assert result["action"] == pool_filter.ACTION_ADMIT
    assert result["gates"]["route"]["enabled_routes"] == ["12"]
