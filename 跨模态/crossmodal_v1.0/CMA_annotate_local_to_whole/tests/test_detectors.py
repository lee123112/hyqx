"""检测后端测试：YOLOv8n 适配器真实前向/解析路径 + 通用类别匹配 + 降级契约。

``test_yolov8n_backend_parses_ultralytics_protocol`` 用**真实适配器代码**解析
一个实现 ultralytics ``Results`` 协议的小替身对象，验证 xyxy/conf/cls → 归一化框
的解析、置信度过滤、类别名映射与裁剪；它**只验证协议与管线，不代表模型精度**。
"""
from __future__ import annotations

import pytest
import torch

from utils import detectors
from utils.schemas import ConfigError, OperatorConfig


# --------------------------------------------------------------------------- #
# 通用类别与描述匹配
# --------------------------------------------------------------------------- #

def test_coco80_general_classes_complete():
    assert len(detectors.COCO80_CLASSES) == 80
    assert detectors.COCO80_CLASSES[0] == "person"
    assert "car" in detectors.COCO80_CLASSES
    assert len(set(detectors.COCO80_CLASSES)) == 80


@pytest.mark.parametrize("class_name,description,expected", [
    ("person", "画面中有一名操作员在检查设备", "操作员"),
    ("person", "a person walking in the street", "person"),
    ("person", "画面里只有设备仪表", None),
    ("car", "路边停着一辆汽车", "汽车"),
    ("truck", "有一辆卡车在行驶", "卡车"),
    ("dog", "桌上放着一只猫", None),
])
def test_class_matches_description(class_name, description, expected):
    assert detectors.class_matches_description(class_name, description) == expected


def test_general_class_names_are_the_only_prefilled_source():
    # 专用设备（非 COCO 通用类别）不预填：不在通用类别表中即无法匹配
    assert detectors.class_matches_description("excavator", "工地上有挖掘机") is None
    assert detectors.is_general_class("person") is True
    assert detectors.is_general_class("excavator") is False


# --------------------------------------------------------------------------- #
# YOLOv8n 适配器协议解析（真实解析代码 + 协议替身）
# --------------------------------------------------------------------------- #

class _FakeBoxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy = xyxy
        self.conf = conf
        self.cls = cls

    def __len__(self):
        return int(self.xyxy.shape[0])


class _FakeResult:
    def __init__(self, xyxy, conf, cls, orig_shape):
        self.boxes = _FakeBoxes(xyxy, conf, cls)
        self.orig_shape = orig_shape


class _FakeModel:
    """实现 ultralytics ``model.names`` / ``model.predict(...)`` 协议的最小替身。"""

    def __init__(self, results, names=None):
        self._results = results
        self.names = names if names is not None else {0: "person", 2: "car", 7: "truck"}
        self.calls = []

    def predict(self, **kwargs):
        self.calls.append(kwargs)
        return list(self._results)


def _model_with(rows, orig_shape=(100, 200)):
    """``rows`` 为 ``([x1, y1, x2, y2], confidence, class_id)`` 三元的列表。"""
    xyxy = torch.tensor([row[0] for row in rows], dtype=torch.float32)
    conf = torch.tensor([row[1] for row in rows], dtype=torch.float32)
    cls = torch.tensor([row[2] for row in rows], dtype=torch.float32)
    return _FakeModel([_FakeResult(xyxy, conf, cls, orig_shape)])


def test_yolov8n_backend_parses_ultralytics_protocol():
    model = _model_with([
        ([10.0, 20.0, 80.0, 90.0], 0.91, 0),   # person
        ([100.0, 40.0, 180.0, 70.0], 0.55, 2),  # car
    ])
    backend = detectors.Yolov8nBackend(model, model.names, None, "cpu")

    boxes = backend.detect("demo.png", conf_threshold=0.25, max_boxes=10, image_size=640)
    assert len(boxes) == 2
    assert boxes[0]["class_name"] == "person"
    assert boxes[0]["class_id"] == 0
    assert boxes[0]["x_min"] == pytest.approx(10.0 / 200.0, abs=1e-6)
    assert boxes[0]["y_min"] == pytest.approx(20.0 / 100.0, abs=1e-6)
    assert boxes[0]["x_max"] == pytest.approx(80.0 / 200.0, abs=1e-6)
    assert boxes[0]["y_max"] == pytest.approx(90.0 / 100.0, abs=1e-6)
    assert boxes[0]["confidence"] == pytest.approx(0.91, abs=1e-6)
    assert boxes[1]["class_name"] == "car"
    # 模型返回名称按 id 映射，不能凭空造类别
    assert {item["class_name"] for item in boxes} == {"person", "car"}


def test_yolov8n_backend_applies_conf_and_box_limits():
    model = _model_with([
        ([0.0, 0.0, 10.0, 10.0], 0.99, 0),
        ([10.0, 10.0, 20.0, 20.0], 0.40, 0),
        ([20.0, 20.0, 30.0, 30.0], 0.10, 2),
    ])
    backend = detectors.Yolov8nBackend(model, model.names, None, "cpu")

    kept = backend.detect("demo.png", conf_threshold=0.50, max_boxes=10, image_size=640)
    assert [item["confidence"] for item in kept] == [0.99]
    limited = backend.detect("demo.png", conf_threshold=0.10, max_boxes=1, image_size=640)
    assert len(limited) == 1
    assert limited[0]["confidence"] == 0.99


def test_yolov8n_backend_skips_degenerate_and_clamps():
    model = _model_with([
        ([50.0, 50.0, 50.0, 60.0], 0.80, 0),      # 宽为 0 → 丢弃
        ([-5.0, -5.0, 250.0, 120.0], 0.70, 0),    # 越界 → 裁剪到 [0,1]
    ])
    backend = detectors.Yolov8nBackend(model, model.names, None, "cpu")
    boxes = backend.detect("demo.png", conf_threshold=0.25, max_boxes=10, image_size=640)
    assert len(boxes) == 1
    assert boxes[0]["x_min"] == 0.0
    assert boxes[0]["y_min"] == 0.0
    assert boxes[0]["x_max"] == 1.0
    assert boxes[0]["y_max"] == 1.0


def test_yolov8n_backend_handles_empty_and_missing_shape():
    empty = _FakeModel([_FakeResult(torch.zeros((0, 4)), torch.zeros((0,)),
                                    torch.zeros((0,)), (10, 10))])
    backend = detectors.Yolov8nBackend(empty, {0: "person"}, None, "cpu")
    assert backend.detect("demo.png", conf_threshold=0.25) == []

    no_shape = _FakeModel([_FakeResult(torch.tensor([[1.0, 1.0, 5.0, 5.0]]),
                                       torch.tensor([0.9]), torch.tensor([0.0]), None)])
    backend = detectors.Yolov8nBackend(no_shape, {0: "person"}, None, "cpu")
    with pytest.raises(detectors.DetectorError):
        backend.detect("demo.png", conf_threshold=0.25)


def test_yolov8n_backend_identity_is_honest():
    model = _model_with([([1.0, 1.0, 5.0, 5.0], 0.9, 0)])
    backend = detectors.Yolov8nBackend(model, model.names, None, "cpu")
    identity = backend.model_identity()
    assert identity["name"] == "YOLOv8n"
    assert identity["real_backend"] is True
    assert identity["test_double"] is False
    assert identity["general_classes_only"] is True
    assert identity["class_source"] == "coco80"
    assert identity["degraded"] is False


# --------------------------------------------------------------------------- #
# 构建与降级契约
# --------------------------------------------------------------------------- #

def _config(**overrides) -> OperatorConfig:
    payload = {
        "operator": {"number": 17, "name": "local_to_whole_annotation", "version": "v1.0.0"},
        "stage_id": "S3",
        "enabled": True,
        "detector_backend": "auto",
        "detector": {"weights": None, "device": "cpu"},
    }
    payload.update(overrides)
    return OperatorConfig.model_validate(payload)


def test_resolve_weights_path_handles_dir_file_and_missing(tmp_path):
    assert detectors.resolve_weights_path(None) is None
    assert detectors.resolve_weights_path(str(tmp_path / "none.pt")) is None
    weight_file = tmp_path / "yolov8n.pt"
    weight_file.write_bytes(b"not-a-real-checkpoint")
    assert detectors.resolve_weights_path(str(weight_file)) == weight_file
    directory = tmp_path / "model_dir"
    directory.mkdir()
    nested = directory / "yolov8n.pt"
    nested.write_bytes(b"x")
    assert detectors.resolve_weights_path(str(directory)) == nested
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert detectors.resolve_weights_path(str(empty_dir)) is None


def test_build_detector_disabled_backend():
    detector, reason, identity = detectors.build_detector(_config(detector_backend="none"))
    assert detector is None
    assert reason == detectors.REASON_DISABLED
    assert identity is None


def test_build_detector_missing_weights_with_degraded_allowed():
    detector, reason, identity = detectors.build_detector(
        _config(allow_degraded=True))
    assert detector is None
    assert reason == detectors.REASON_NO_WEIGHTS
    assert identity is None


def test_build_detector_missing_weights_without_degraded_raises():
    with pytest.raises(detectors.DetectorUnavailable):
        detectors.build_detector(_config(allow_degraded=False))


def test_forced_backend_never_silently_degrades(tmp_path):
    # 显式指定 yolov8n 时，即使 allow_degraded=true 也必须显式失败
    with pytest.raises(detectors.DetectorUnavailable):
        detectors.build_detector(_config(detector_backend="yolov8n", allow_degraded=True))
    missing = tmp_path / "absent.pt"
    with pytest.raises(detectors.DetectorUnavailable):
        detectors.build_detector(
            _config(detector_backend="yolov8n", allow_degraded=True,
                    detector={"weights": str(missing), "device": "cpu"}))


def test_auto_backend_reports_weights_missing_reason(tmp_path):
    detector, reason, identity = detectors.build_detector(
        _config(allow_degraded=True,
                detector={"weights": str(tmp_path / "absent.pt"), "device": "cpu"}))
    assert detector is None
    assert reason.startswith(detectors.REASON_WEIGHTS_MISSING)
    assert identity is None


def test_auto_backend_load_failure_is_degraded_not_faked(tmp_path, monkeypatch):
    weights = tmp_path / "yolov8n.pt"
    weights.write_bytes(b"bogus")

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("checkpoint unreadable")

    monkeypatch.setattr(detectors, "_load_yolo", lambda path: _Boom())
    detector, reason, identity = detectors.build_detector(
        _config(allow_degraded=True,
                detector={"weights": str(weights), "device": "cpu"}))
    assert detector is None
    assert reason.startswith(detectors.REASON_LOAD_FAILED)
    assert identity is None


def test_detector_availability_never_fakes_success():
    report = detectors.detector_availability(_config())
    assert report["available"] is False
    assert report["detector_model"] == "YOLOv8n"
    assert report["weights"] is None
    assert report["reason"] == detectors.REASON_NO_WEIGHTS
    assert report["general_classes_only"] is True


def test_installed_libraries_reports_ultralytics():
    assert "ultralytics" in detectors.installed_libraries()


def test_unknown_detector_backend_rejected():
    with pytest.raises(ConfigError):
        _config(detector_backend="grounding_dino")


@pytest.mark.parametrize("class_name,description,expected", [
    ("person", "A black woman and two black children are sitting in a thatched tent .",
     "woman"),
    ("person", "A couple walks with their kids", "kid"),
    ("dog", "Two dogs running across the grass", "dog"),
    ("bus", "A business meeting in a room", None),
    ("car", "A carpet on the floor", None),
    ("orange", "A boy wearing an orange shirt", None),
])
def test_class_matches_description_english_synonyms_and_word_boundary(
        class_name, description, expected):
    """真机 flickr8k 描述用 man/woman/kid 等词；同时不能把 business 当成 bus。"""
    assert detectors.class_matches_description(class_name, description) == expected


def test_class_matches_description_accepts_plural_forms():
    assert detectors.class_matches_description("bird", "three birds on a wire") == "bird"
    assert detectors.class_matches_description("bus", "two buses at the stop") == "bus"
    assert detectors.class_matches_description("baby", None) is None
