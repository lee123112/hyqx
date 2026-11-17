"""v4.2 池化层 detection 别名回归测试（2026-10-22）。

背景：平台按 ``POST /predict?detection=<算子标识>`` 调用池化入口，
``detection`` 取值是 v4.2 文档 §4.x 的“英文 ID”（= map.json ``englishName``
去掉 ``CMA_`` 前缀，如 ``receive_multimodal_batch``）。
历史缺陷：``inference._candidate_aliases`` 只登记了带 ``CMA_`` 前缀的
englishName / package，导致全部 26 个 detection 标识在池化路由层被拒
（HTTP 400 “缺少或无法识别的 operatorId”），平台工艺步 0 因此失败。
"""
import importlib.util
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

# 显式按路径加载包根 inference.py：同目录其它用例会导入算子级 inference.py，
# 裸 ``import inference`` 在整套 pytest 中可能命中已缓存的同名模块（测试隔离问题）。
_spec = importlib.util.spec_from_file_location(
    "crossmodal_root_inference_aliases", PACKAGE_ROOT / "inference.py")
inference = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = inference
_spec.loader.exec_module(inference)  # noqa: E402


def _detection_ids():
    for key, entry in inference.REGISTRY.items():
        english = str(entry.get("englishName") or "")
        detection = english[4:] if english.upper().startswith("CMA_") else english
        yield key, detection


def test_all_detection_ids_are_aliases():
    missing = ["%s=%s" % (key, det) for key, det in _detection_ids()
               if det and det.lower() not in inference.ALIASES]
    assert not missing, "池化层缺少 detection 别名: %s" % missing


def test_resolve_known_detection_ids():
    assert inference.resolve_operator_id("receive_multimodal_batch") == "M01"
    assert inference.resolve_operator_id("check_scene_id") == "M02"
    assert inference.resolve_operator_id("score_image_text_consistency") == "M12"
    assert inference.resolve_operator_id("generate_delivery_sample") == "M26"


def test_operator_id_aliases_still_work():
    assert inference.resolve_operator_id("multimodal_batch_intake") == "M01"
    assert inference.resolve_operator_id("M01") == "M01"
