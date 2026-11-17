"""M19 compatibility must retain all four gates and the approved #15 fallback."""
import copy

import pytest

from utils import pool_filter as pf, upstream
from utils.schemas import OperatorConfig


def record():
    return {"sample_id": "gate-regression", "image_reference": {"path": "media/a.png"},
            "license": "可训练", "alignment": {"whole": {"decision": "auto_aligned"}},
            "processing": {"12": {"status": 1, "process_status": "scored", "tier": "auto_aligned"}}}


def evaluate(payload, **overrides):
    return pf.evaluate_record(pf.resolve_record(payload), config=OperatorConfig(**overrides),
                              event_time="2026-11-09T00:00:00+08:00")


@pytest.mark.parametrize("route", ["13", "14"])
def test_numeric_status_route_pass_is_consumed(route):
    payload = record()
    payload["processing"] = {route: {"status": 1, "process_status": "passed"}}
    assert evaluate(payload)["action"] == pf.ACTION_ADMIT


@pytest.mark.parametrize("route,reason", [("13", "no_audio_text_pair"), ("14", "no_video_text_pair")])
def test_absent_modality_route_does_not_block_a_real_pass(route, reason):
    payload = record()
    payload["processing"][route] = {"status": 1, "process_status": "pending", "route_enabled": True,
                                    "passed": None, "reason": reason}
    result = evaluate(payload)
    assert result["action"] == pf.ACTION_ADMIT
    assert result["node"]["gates"]["route"]["enabled_routes"] == ["12"]


@pytest.mark.parametrize("route,modality,reason", [("13", "audio", "no_audio_text_pair"),
                                                  ("14", "video", "no_video_text_pair")])
@pytest.mark.parametrize("location", ["direct", "reference", "typed17", "generic17", "declared"])
def test_present_modality_cannot_be_excused_by_no_pair_reason(route, modality, reason, location):
    payload = record()
    path = "media/a.wav" if modality == "audio" else "media/v.mp4"
    if location == "direct":
        payload[modality] = {"path": path}
    elif location == "reference":
        payload[modality + "_reference"] = {"path": path}
    elif location == "typed17":
        payload["processing"]["17"] = {"paths": {modality: path}}
    elif location == "generic17":
        payload["processing"]["17"] = {"path": path}
    else:
        payload["modalities"] = [modality, "text"]
    payload["processing"][route] = {"status": 1, "process_status": "pending", "route_enabled": True,
                                    "passed": None, "reason": reason}
    assert evaluate(payload)["action"] == pf.ACTION_PENDING


@pytest.mark.parametrize("reason", ["missing_description", "missing_transcript", "model_unavailable"])
@pytest.mark.parametrize("state", ["pending", "not_applicable"])
def test_actual_media_missing_evidence_stays_pending(reason, state):
    payload = record()
    payload["audio"] = {"path": "media/a.wav"}
    payload["processing"]["13"] = {"status": 1, "process_status": state,
                                  "passed": None, "reason": reason}
    assert evaluate(payload)["action"] == pf.ACTION_PENDING


@pytest.mark.parametrize("route,reason", [("13", "language_not_applicable"),
                                         ("13", "transcript_unreliable"),
                                         ("14", "temporal_route_not_applicable")])
def test_approved_not_applicable_gates_preserve_other_routes(route, reason):
    payload = record()
    payload["audio"] = {"path": "media/a.wav"}
    payload["video"] = {"path": "media/v.mp4"}
    payload["processing"][route] = {"status": 1, "process_status": "not_applicable", "route_enabled": True,
                                    "passed": None, "reason": reason}
    assert evaluate(payload)["action"] == pf.ACTION_ADMIT


def test_only_absent_modality_routes_do_not_create_a_pass():
    payload = record()
    payload["processing"] = {"13": {"status": 1, "process_status": "pending", "route_enabled": True,
                                    "reason": "no_audio_text_pair"}}
    result = evaluate(payload)
    assert result["action"] == pf.ACTION_PENDING
    assert result["node"]["reason_code"] == pf.REASON_NO_ROUTE


@pytest.mark.parametrize("route,status,reason", [("13", "not_applicable", "no_audio_text_pair"),
                                                ("14", "passed", "no_video_text_pair")])
def test_explicit_route_failure_is_not_excused(route, status, reason):
    payload = record()
    payload["processing"][route] = {"status": 1, "process_status": status, "route_enabled": True,
                                    "passed": False, "reason": reason}
    assert evaluate(payload)["action"] == pf.ACTION_ISOLATE


@pytest.mark.parametrize("route,state", [("17", "incomplete"), ("18", "isolated"), ("21", "isolated")])
def test_numeric_status_upstream_isolation_is_respected(route, state):
    payload = record()
    payload["processing"][route] = {"status": 0, "process_status": state}
    result = evaluate(payload)
    assert result["action"] == pf.ACTION_ISOLATE
    assert result["node"]["reason_code"] == pf.REASON_UPSTREAM_ISOLATED


def test_numeric_whole_state_is_read_and_not_applicable_stays_pending():
    payload = record()
    payload.pop("alignment")
    payload["processing"]["16"] = {"status": 1, "process_status": "auto_aligned"}
    assert evaluate(payload)["action"] == pf.ACTION_ADMIT
    payload["processing"]["16"]["process_status"] = "not_applicable"
    result = evaluate(payload)
    assert result["action"] == pf.ACTION_PENDING
    assert result["node"]["reason_code"] == pf.REASON_WHOLE_PENDING


@pytest.mark.parametrize("state", ["passed", "not_applicable"])
def test_temporal_explicit_false_overrides_status_even_without_inline_timeline(state):
    payload = record()
    payload["processing"]["15"] = {"status": 1, "process_status": state, "passed": False}
    result = evaluate(payload)
    assert result["action"] == pf.ACTION_ISOLATE
    assert result["node"]["gates"]["temporal"]["passed"] is False
    assert pf._temporal_route_pass(upstream.read_temporal(payload)) is False


def test_pair_empty_nodes_do_not_discard_media_or_upstream_isolation():
    payload = {"pair_id": "pair", "left": {"record": record()},
               "right": {"record": {"image_reference": None, "processing": None}},
               "processing": {"21": {"status": 0, "process_status": "isolated"}}}
    before = copy.deepcopy(payload)
    flat = upstream.flatten_record(payload)
    assert flat["image_reference"]["path"] == "media/a.png"
    assert flat["processing"]["12"]["tier"] == "auto_aligned"
    assert evaluate(payload)["action"] == pf.ACTION_ISOLATE
    assert payload == before


def test_temporal15_fallback_remains_conditional_and_configurable():
    payload = record()
    payload["processing"] = {"15": {"status": 1, "process_status": "passed"}}
    assert evaluate(payload)["node"]["gates"]["route"]["enabled_routes"] == ["15"]
    assert evaluate(payload, temporal_route_fallback=False)["action"] == pf.ACTION_PENDING


def test_temporal_failed_status_cannot_be_used_as_route_pass():
    assert pf._temporal_route_pass({"present": True, "applicable": True,
                                    "status": "failed", "passed": True}) is False


@pytest.mark.parametrize("status", ["not_applicable", "skipped"])
def test_temporal_nonapplicable_status_cannot_be_used_as_route_pass(status):
    payload = record()
    payload["processing"] = {"15": {"status": 1, "process_status": status, "passed": True}}
    assert evaluate(payload)["action"] == pf.ACTION_PENDING


@pytest.mark.parametrize("empty", [None, "", "   ", [], {}])
def test_pair_empty_media_and_nodes_do_not_erase_valid_evidence(empty):
    payload = {"pair_id": "p", "left": {"record": record()},
               "right": {"record": {"image_reference": empty, "processing": empty}},
               "processing": empty}
    flat = upstream.flatten_record(payload)
    assert flat["image_reference"]["path"] == "media/a.png"
    assert flat["processing"]["12"]["tier"] == "auto_aligned"


@pytest.mark.parametrize("field", ["process_status", "status"])
def test_whole_business_state_alias_inside_alignment_is_read(field):
    payload = record()
    payload["alignment"]["whole"] = {field: "auto_aligned"}
    assert evaluate(payload)["action"] == pf.ACTION_ADMIT
