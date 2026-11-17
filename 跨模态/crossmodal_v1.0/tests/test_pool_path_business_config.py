"""filePath mode must not silently close routes by dropping JSON config."""
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pool_path_business_regression", ROOT / "inference.py")
pool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pool
spec.loader.exec_module(pool)


def test_scalar_config_fields_survive_file_path_conversion():
    actual = pool._path_forward_form({"filePath": "/data/input.zip", "config": {"enabled": True, "max_frames_per_segment": 8}})
    assert actual == {"enabled": "true", "max_frames_per_segment": "8"}


def test_scalar_overrides_fields_survive_file_path_conversion():
    actual = pool._path_forward_form({"overrides": {"enabled": True, "min_clip_score": 0.25}})
    assert actual == {"enabled": "true", "min_clip_score": "0.25"}


def test_m08_asr_json_flag_uses_actual_form_alias():
    actual = pool._path_forward_form({"config": {"enabled": True, "transcription_enabled": True}}, operator_key="M08")
    assert actual == {"enabled": "true", "transcribe": "true"}


def test_conflicting_top_level_and_wrapped_values_fail_explicitly():
    with pytest.raises(ValueError, match="enabled"):
        pool._path_forward_form({"enabled": False, "config": {"enabled": True}})


def test_unsupported_deep_configuration_is_not_silently_ignored():
    with pytest.raises(ValueError, match="detector"):
        pool._path_forward_form({"config": {"detector": {"conf_threshold": 0.25}}})


def test_existing_flat_parameters_and_strict_false_are_preserved():
    assert pool._path_forward_form({"filePath": "/data/input.zip", "enabled": False,
                                    "uniqueId": "actual-id", "min_clip_score": 0.25}) == {
        "enabled": "false", "uniqueId": "actual-id", "min_clip_score": "0.25"}


def test_urlencoded_json_wrapper_is_understood():
    assert pool._path_forward_form({"config": '{"enabled":true}'}) == {"enabled": "true"}


@pytest.mark.parametrize("config", [True, 1, "broken", ["enabled"]])
def test_invalid_wrappers_fail_instead_of_becoming_flat_form_noise(config):
    with pytest.raises(ValueError, match="config"):
        pool._path_forward_form({"config": config})


def test_real_pool_handler_forwards_the_m08_flag(tmp_path, monkeypatch):
    source = tmp_path / "input.zip"
    source.write_bytes(b"fixture-file")
    observed = {}

    class CapturingTransport:
        async def request(self, method, url, **kwargs):
            observed.update(kwargs)
            return httpx.Response(200, json={"status": 1})

    async def client():
        return CapturingTransport()

    async def ensure():
        return None

    monkeypatch.setattr(pool, "get_client", client)
    monkeypatch.setattr(pool.PROCESSES["M08"], "ensure_started", ensure)
    client = TestClient(pool.app)
    response = client.post("/predict?operatorId=M08", json={"filePath": str(source),
                           "config": {"enabled": True, "transcription_enabled": True}})
    assert response.status_code == 200
    assert observed["data"]["enabled"] == "true"
    assert observed["data"]["transcribe"] == "true"
    assert "config" not in observed["data"]


def test_real_pool_handler_rejects_conflicts_before_forwarding(tmp_path, monkeypatch):
    source = tmp_path / "input.zip"
    source.write_bytes(b"fixture-file")

    async def ensure():
        return None

    monkeypatch.setattr(pool.PROCESSES["M14"], "ensure_started", ensure)
    response = TestClient(pool.app).post("/predict?operatorId=M14", json={
        "filePath": str(source), "enabled": False, "config": {"enabled": True}})
    assert response.status_code == 400
    assert "enabled" in response.json()["msg"]


def test_pool_lifespan_closes_and_recreates_shared_client(monkeypatch):
    monkeypatch.setattr(pool, "AUTOSTART", False)
    with TestClient(pool.app) as client:
        assert client.get("/health").status_code == 200
        first = pool._client
        assert first is not None and not first.is_closed
    assert pool._client is None
    assert first.is_closed
    with TestClient(pool.app) as client:
        assert client.get("/health").status_code == 200
        second = pool._client
        assert second is not first
    assert second.is_closed and pool._client is None
