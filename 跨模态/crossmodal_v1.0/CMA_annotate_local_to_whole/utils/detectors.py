"""检测后端：YOLOv8n 适配器、通用类别（COCO-80）与描述匹配、显式降级契约。

表1 #17 边界（本模块的实现约束）：
- 只使用 YOLOv8n 官方通用类别做**预填**；专用设备不预填（由标注员手画）；
- 只保留「通用类别 ∩ 与描述可对上」的框；
- **不做词-区域一一对应**（这里只做「类别名/别名是否出现在描述里」的过滤）；
- 不部署 Grounding DINO、不训练专用检测模型；
- 权重只从本地路径加载，绝不联网下载；不可用时显式降级或显式失败，
  绝不返回固定框、随机框或伪造置信度。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.schemas import DETECTOR_MODEL, GENERAL_CLASS_SOURCE, ConfigError

DETECTOR_BACKEND_SOURCE = "ultralytics.YOLO"

REASON_DISABLED = "detector_backend_is_none"
REASON_NO_WEIGHTS = "model_weights_not_configured"
REASON_WEIGHTS_MISSING = "model_weights_not_found"
REASON_LIBRARY_MISSING = "detector_library_not_installed"
REASON_LOAD_FAILED = "model_load_failed"
REASON_IMAGE_UNREADABLE = "image_unreadable"
WEIGHTS_FILENAME = "yolov8n.pt"
LIBRARY_NAME = "ultralytics"

#: YOLOv8n 官方通用类别（COCO 80 类，索引顺序即模型 class id）
COCO80_CLASSES: Tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)

#: 通用类别的中文别名，仅用于「描述是否提到该类别」的确定性匹配。
#: 顺序即优先级：更具体的别名在前，避免「车」把「卡车」错配成 car。
COCO_CLASS_ALIASES: Dict[str, Tuple[str, ...]] = {
    # 人物类：英文描述常用 man/woman/child/kid 等词，漏配会让「通用类别 ∩ 描述」
    # 全部落空（真机实测 flickr8k 描述里几乎不出现 person）。
    "person": ("操作员", "工人", "人员", "行人", "人物", "人",
               "person", "people", "man", "men", "woman", "women", "child",
               "children", "kid", "boy", "girl", "baby", "toddler", "guy",
               "lady", "gentleman", "human", "pedestrian", "crowd", "student",
               "worker", "player", "rider", "driver", "passenger", "singer",
               "dancer", "actor", "audience", "couple", "family", "hiker",
               "climber", "surfer", "photographer", "cameraman", "shopper",
               "performer", "chef", "cook", "officer", "soldier", "guard",
               "cyclist", "bicyclist", "biker", "wrestler", "boxer", "runner",
               "athlete", "skier", "swimmer", "jumper"),
    "bicycle": ("自行车", "单车", "bicycle", "bike", "cycling"),
    "car": ("小汽车", "汽车", "轿车", "车辆", "car", "automobile", "vehicle"),
    "motorcycle": ("摩托车", "摩托", "motorcycle", "motorbike"),
    "airplane": ("飞机", "客机", "airplane", "plane", "aircraft"),
    "bus": ("公交车", "巴士", "客车", "bus"),
    "train": ("火车", "列车", "train", "railway"),
    "truck": ("卡车", "货车", "载重车", "truck", "lorry"),
    "boat": ("船", "轮船", "小艇", "boat", "ship", "canoe", "kayak", "sailboat"),
    "traffic light": ("红绿灯", "交通灯", "信号灯", "traffic light"),
    "fire hydrant": ("消防栓", "fire hydrant"),
    "stop sign": ("停止标志", "stop sign"),
    "bench": ("长椅", "长凳", "bench"),
    "bird": ("鸟", "小鸟", "bird"),
    "cat": ("猫", "小猫", "cat", "kitten"),
    "dog": ("狗", "小狗", "犬", "dog", "puppy"),
    "horse": ("马", "horse", "pony"),
    "sheep": ("羊", "绵羊", "sheep", "lamb"),
    "cow": ("牛", "奶牛", "cow", "cattle"),
    "elephant": ("大象", "象", "elephant"),
    "bear": ("熊", "bear"),
    "backpack": ("背包", "双肩包", "backpack"),
    "umbrella": ("雨伞", "伞", "umbrella"),
    "handbag": ("手提包", "手袋", "handbag", "purse"),
    "suitcase": ("行李箱", "手提箱", "suitcase", "luggage"),
    "bottle": ("瓶子", "水瓶", "bottle"),
    "cup": ("杯子", "水杯", "cup", "mug"),
    "bowl": ("碗", "bowl"),
    "chair": ("椅子", "座椅", "chair"),
    "couch": ("沙发", "couch", "sofa"),
    "potted plant": ("盆栽", "绿植", "potted plant", "plant", "flowerpot"),
    "bed": ("床", "bed"),
    "dining table": ("餐桌", "桌子", "dining table", "table", "desk"),
    "toilet": ("马桶", "卫生间", "toilet"),
    "tv": ("电视", "显示器", "tv", "television", "monitor", "screen"),
    "laptop": ("笔记本电脑", "笔记本", "laptop", "computer"),
    "keyboard": ("键盘", "keyboard"),
    "cell phone": ("手机", "移动电话", "cell phone", "phone", "smartphone"),
    "microwave": ("微波炉", "microwave"),
    "oven": ("烤箱", "oven", "stove"),
    "sink": ("水槽", "洗手池", "sink"),
    "refrigerator": ("冰箱", "refrigerator", "fridge"),
    "book": ("书", "书本", "book"),
    "clock": ("钟表", "时钟", "clock"),
    "vase": ("花瓶", "vase"),
    "scissors": ("剪刀", "scissors"),
    "teddy bear": ("玩具熊", "teddy bear", "teddy"),
    # 以下类别原先没有别名表，英文描述只能靠类名本身命中；补上中文别名。
    "frisbee": ("飞盘", "frisbee"),
    "skis": ("滑雪板", "skis", "ski"),
    "snowboard": ("单板滑雪", "snowboard"),
    "sports ball": ("球", "sports ball", "ball"),
    "kite": ("风筝", "kite"),
    "baseball bat": ("棒球棒", "baseball bat", "bat"),
    "baseball glove": ("棒球手套", "baseball glove", "glove"),
    "skateboard": ("滑板", "skateboard"),
    "surfboard": ("冲浪板", "surfboard"),
    "tennis racket": ("网球拍", "tennis racket", "racket"),
    "fork": ("叉子", "餐叉", "fork"),
    "knife": ("刀", "餐刀", "knife"),
    "spoon": ("勺子", "汤匙", "spoon"),
    "banana": ("香蕉", "banana"),
    "apple": ("苹果", "apple"),
    "sandwich": ("三明治", "sandwich"),
    # 英文 orange 多为颜色（orange shirt），只保留中文水果别名，避免误配
    "orange": ("橙子", "橘子"),
    "broccoli": ("西兰花", "broccoli"),
    "carrot": ("胡萝卜", "carrot"),
    "hot dog": ("热狗", "hot dog"),
    "pizza": ("披萨", "pizza"),
    "donut": ("甜甜圈", "donut", "doughnut"),
    "cake": ("蛋糕", "cake"),
    "mouse": ("鼠标", "mouse"),
    "remote": ("遥控器", "remote"),
    "toaster": ("烤面包机", "toaster"),
    "toothbrush": ("牙刷", "toothbrush"),
    "hair drier": ("吹风机", "hair drier", "hair dryer"),
}


class DetectorError(RuntimeError):
    """检测推理期错误（图片不可读、返回结构异常等）。"""


class DetectorUnavailable(RuntimeError):
    """检测后端不可用且未允许降级：必须显式失败，不得伪造结果。"""


# --------------------------------------------------------------------------- #
# 通用类别与描述匹配
# --------------------------------------------------------------------------- #

def is_general_class(class_name: Any) -> bool:
    return isinstance(class_name, str) and class_name in COCO80_CLASSES


def class_aliases(class_name: str) -> Tuple[str, ...]:
    aliases = COCO_CLASS_ALIASES.get(class_name)
    if aliases:
        return aliases
    return (class_name,)


_ASCII_WORD_RE = re.compile(r"[a-z][a-z'\-]*")


def _ascii_forms(alias: str) -> Tuple[str, ...]:
    """英文别名 + 常见复数形式（描述里多为复数：dogs / buses / babies）。"""
    if not alias:
        return ()
    forms = [alias]
    if alias.endswith("y") and len(alias) > 1 and alias[-2] not in "aeiou":
        forms.append(alias[:-1] + "ies")
    elif alias.endswith(("s", "x", "ch", "sh")):
        forms.append(alias + "es")
    else:
        forms.append(alias + "s")
    return tuple(dict.fromkeys(forms))


def class_matches_description(class_name: str, description: Optional[str]) -> Optional[str]:
    """描述中是否提到该通用类别；命中返回命中的别名，否则 ``None``。

    只做**确定性词面**判定，不做词到区域的对应，也不做任何相似度打分：
    中文按子串包含；英文按词边界（``bus`` 不再命中 ``business``）并接受常见复数。
    """
    if not description or not isinstance(class_name, str):
        return None
    if not is_general_class(class_name):
        return None
    lowered = description.lower()
    words = set(_ASCII_WORD_RE.findall(lowered))
    for alias in class_aliases(class_name):
        if not alias:
            continue
        if alias.isascii():
            alias_low = alias.lower()
            if " " in alias_low or "-" in alias_low:
                pattern = r"(?<![a-z])%s(?![a-z])" % re.escape(alias_low)
                if re.search(pattern, lowered):
                    return alias
                continue
            if any(form in words for form in _ascii_forms(alias_low)):
                return alias
        elif alias in description:
            return alias
    return None


def filter_general_boxes(boxes: Iterable[Dict[str, Any]],
                         description: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """保留「通用类别 ∩ 与描述可对上」的框，返回 (命中, 丢弃数)。

    预填框一律标记 ``source=yolov8n`` 且 ``human_verified=false``：
    预填永远不等于人工确认。
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for box in boxes:
        class_name = str(box.get("class_name") or "")
        alias = class_matches_description(class_name, description)
        if alias is None:
            dropped += 1
            continue
        item = dict(box)
        item["source"] = "yolov8n"
        item["human_verified"] = False
        item.setdefault("coordinates", "normalized-xyxy")
        item["matched_alias"] = alias
        kept.append(item)
    return kept, dropped


# --------------------------------------------------------------------------- #
# 权重与库发现
# --------------------------------------------------------------------------- #

def model_root() -> Path:
    """权重根目录：``$MODEL_ROOT`` 优先，否则算子本地 ``<算子目录>/weight``。"""
    override = os.environ.get("MODEL_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "weight"


def resolve_model_path(value: Any) -> Any:
    """把配置中的权重/模型路径解析为绝对路径；``None``/空串原样返回。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    path = Path(text).expanduser()
    return path if path.is_absolute() else model_root() / path


def resolve_weights_path(value: Any) -> Optional[Path]:
    """解析本地权重：文件直接用；目录则找 ``yolov8n.pt``；一律不做下载。

    相对路径按 ``MODEL_ROOT``（默认算子本地 ``weight/``）解析，因此
    ``detector.weights: YOLOv8n/yolov8n.pt`` 与 ``YOLOv8n/`` 目录两种写法都可用。
    """
    if value is None:
        return None
    path = resolve_model_path(value)
    if not path:
        return None
    if path.is_file():
        return path
    if path.is_dir():
        for candidate in sorted(path.iterdir()):
            if candidate.is_file() and candidate.name.lower() == WEIGHTS_FILENAME:
                return candidate
    return None


def installed_libraries() -> List[str]:
    import importlib.util

    found = []
    for name in (LIBRARY_NAME, "torch", "PIL", "numpy"):
        try:
            if importlib.util.find_spec(name) is not None:
                found.append(name)
        except (ImportError, ValueError):
            continue
    return found


def _load_yolo(weights_path: Path):
    """真实加载本地权重；单独成函数以便测试注入失败路径。"""
    from ultralytics import YOLO

    return YOLO(str(weights_path))


def _probe(config) -> Tuple[Optional["Yolov8nBackend"], Optional[str], bool]:
    """探测后端：(检测器或 None, 不可用原因或 None, 是否强制真实后端)。不抛异常。"""
    detector_conf = config.detector
    backend = str(config.detector_backend).lower()
    if backend == "none":
        return None, REASON_DISABLED, False

    forced = backend == "yolov8n"
    weights = resolve_weights_path(detector_conf.weights)
    if weights is None:
        if detector_conf.weights:
            return None, "%s: %s" % (REASON_WEIGHTS_MISSING, detector_conf.weights), forced
        return None, REASON_NO_WEIGHTS, forced

    try:
        import importlib.util
        available = importlib.util.find_spec(LIBRARY_NAME) is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        return None, "%s: %s" % (REASON_LIBRARY_MISSING, LIBRARY_NAME), forced

    try:
        model = _load_yolo(weights)
        names = dict(getattr(model, "names", {}) or {})
    except Exception as exc:  # noqa: BLE001 - 统一转成显式不可用原因
        return None, "%s: %s" % (REASON_LOAD_FAILED, str(exc).splitlines()[0][:160]), forced
    return Yolov8nBackend(model, names, weights, detector_conf.device), None, forced


def build_detector(config) -> Tuple[Optional["Yolov8nBackend"], Optional[str], Optional[Dict[str, Any]]]:
    """按配置构建检测后端，返回 ``(检测器, 不可用原因, 模型身份)``。

    不可用时：显式指定 ``yolov8n`` 或 ``allow_degraded=false`` → 抛 ``DetectorUnavailable``；
    否则返回原因，由调用方进入 degraded/waiting_manual 路线（不产生任何框）。
    """
    detector, reason, forced = _probe(config)
    if detector is not None:
        return detector, None, detector.model_identity()
    if reason == REASON_DISABLED:
        return None, REASON_DISABLED, None
    if forced or not bool(config.allow_degraded):
        raise DetectorUnavailable(reason or REASON_LOAD_FAILED)
    return None, reason, None


def detector_availability(config=None) -> Dict[str, Any]:
    """给 ``/health`` 与血缘用的真实后端状态（不做任何伪造）。"""
    if config is None:
        from utils.schemas import load_config

        try:
            config = load_config()
        except ConfigError:
            config = None

    backend = str(getattr(config, "detector_backend", "auto")).lower() if config else "auto"
    weights_conf = None
    device = "cpu"
    if config is not None and getattr(config, "detector", None) is not None:
        weights_conf = config.detector.weights
        device = config.detector.device
    weights = resolve_weights_path(weights_conf)
    libraries = installed_libraries()
    has_library = LIBRARY_NAME in libraries

    if backend == "none":
        available, reason = False, REASON_DISABLED
    elif weights is None:
        available = False
        reason = ("%s: %s" % (REASON_WEIGHTS_MISSING, weights_conf)
                  if weights_conf else REASON_NO_WEIGHTS)
    elif not has_library:
        available, reason = False, "%s: %s" % (REASON_LIBRARY_MISSING, LIBRARY_NAME)
    else:
        available, reason = True, None

    return {
        "detector_model": DETECTOR_MODEL,
        "detector_backend": backend,
        "backend_source": DETECTOR_BACKEND_SOURCE,
        "library": LIBRARY_NAME if has_library else None,
        "libraries": libraries,
        "weights": str(weights) if weights else None,
        "weights_source": "local-only",
        "device": device,
        "general_classes_only": True,
        "class_source": GENERAL_CLASS_SOURCE,
        "general_class_count": len(COCO80_CLASSES),
        "available": available,
        "model_loaded": False,
        "degraded": not available,
        "reason": reason,
        "allow_degraded": bool(getattr(config, "allow_degraded", False)) if config else None,
        "network_access": False,
    }


def image_is_readable(path) -> bool:
    """判定图片能否真实解码（用于原始隔离判定）；Pillow 缺失时按不可校验处理。"""
    target = Path(str(path))
    if not target.is_file():
        return False
    try:
        from PIL import Image
    except ImportError:
        # 无法校验解码时，至少要求文件非空且带 PNG/JPEG 签名，避免误判为可读
        head = target.read_bytes()[:16]
        if len(head) < 8:
            return False
        return head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8")
    try:
        with Image.open(target) as image:
            image.verify()
        return True
    except Exception:  # noqa: BLE001 - 任何解码异常都视为不可读
        return False


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #

def _to_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        return list(value.tolist())
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


class Yolov8nBackend:
    """YOLOv8n 适配器：把 ultralytics ``Results`` 解析为归一化 xyxy 框。"""

    def __init__(self, model, names: Dict[Any, Any], weights_path=None,
                 device: str = "cpu", *, class_source: str = GENERAL_CLASS_SOURCE,
                 library: str = LIBRARY_NAME, test_double: bool = False) -> None:
        self._model = model
        self._names = dict(names or {})
        self.weights_path = Path(weights_path) if weights_path else None
        self.device = str(device or "cpu")
        self.class_source = class_source
        self.library = library
        self.test_double = bool(test_double)

    # ------------------------------------------------------------------ #
    def model_identity(self) -> Dict[str, Any]:
        return {
            "name": DETECTOR_MODEL,
            "library": self.library,
            "backend_source": DETECTOR_BACKEND_SOURCE,
            "weights": str(self.weights_path) if self.weights_path else None,
            "weights_source": "local-only",
            "device": self.device,
            "class_source": self.class_source,
            "general_classes_only": True,
            "general_class_count": len(COCO80_CLASSES),
            "degraded": False,
            "real_backend": not self.test_double,
            "test_double": self.test_double,
            "network_access": False,
        }

    # ------------------------------------------------------------------ #
    def _class_name(self, class_id: int) -> Optional[str]:
        """按模型自报的类别表取名；模型未给类别表时回落官方 COCO-80（索引即 class id）。"""
        if self._names:
            value = self._names.get(class_id)
        elif 0 <= class_id < len(COCO80_CLASSES):
            value = COCO80_CLASSES[class_id]
        else:
            value = None
        if value is None:
            return None
        return str(value)

    def detect(self, image_path, *, conf_threshold: float = 0.25,
               iou_threshold: float = 0.45, max_boxes: int = 50,
               image_size: int = 640) -> List[Dict[str, Any]]:
        path = Path(str(image_path))
        try:
            results = self._model.predict(
                source=str(path), conf=float(conf_threshold), iou=float(iou_threshold),
                imgsz=int(image_size), max_det=int(max_boxes), device=self.device,
                verbose=False)
        except DetectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - 推理失败必须显式上抛
            raise DetectorError("inference failed for %s: %s"
                                % (path.name, str(exc).splitlines()[0][:160])) from exc
        if results is None or len(results) == 0:
            return []
        return self._parse(results[0], conf_threshold=conf_threshold, max_boxes=max_boxes)

    def _parse(self, result, *, conf_threshold: float,
               max_boxes: int) -> List[Dict[str, Any]]:
        shape = getattr(result, "orig_shape", None)
        if not shape or len(shape) < 2:
            raise DetectorError("detector result has no image shape; cannot normalize boxes")
        height, width = int(shape[0]), int(shape[1])
        if height <= 0 or width <= 0:
            raise DetectorError("detector result reported an empty image shape")

        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return []
        try:
            total = len(boxes)
        except TypeError:
            return []
        if total == 0:
            return []

        coordinates = _to_list(getattr(boxes, "xyxy", None))
        confidences = _to_list(getattr(boxes, "conf", None))
        classes = _to_list(getattr(boxes, "cls", None))

        items: List[Dict[str, Any]] = []
        for coords, score, class_id in zip(coordinates, confidences, classes):
            values = _to_list(coords)
            if len(values) < 4:
                continue
            try:
                x1, y1, x2, y2 = (float(values[0]), float(values[1]),
                                  float(values[2]), float(values[3]))
                confidence = float(score)
                index = int(class_id)
            except (TypeError, ValueError):
                continue
            if confidence < float(conf_threshold):
                continue
            if x2 <= x1 or y2 <= y1:
                continue
            class_name = self._class_name(index)
            if not class_name:
                continue  # 未知类别不猜测、不预填
            items.append({
                "class_id": index,
                "class_name": class_name,
                "confidence": round(confidence, 6),
                "x_min": round(_clamp(x1 / width), 6),
                "y_min": round(_clamp(y1 / height), 6),
                "x_max": round(_clamp(x2 / width), 6),
                "y_max": round(_clamp(y2 / height), 6),
                "coordinates": "normalized-xyxy",
                "source": "yolov8n",
                "human_verified": False,
            })
        items.sort(key=lambda item: item["confidence"], reverse=True)
        return items[:max(1, int(max_boxes))]
