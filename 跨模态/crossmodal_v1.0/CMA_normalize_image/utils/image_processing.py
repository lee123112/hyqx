"""算子 4 图像标准化业务算法（CPU 规则，无模型）。

职责与边界（TECHNICAL_DESIGN §4）：
1. Pillow 解码校验、像素上限、EXIF 方向归一，记录原始尺寸与方向；
2. ``ratio = min(1, max_side / max(w, h))`` 等比缩放，至少 1 像素；仅
   ``allow_upscale=true`` 才允许放大；
3. 统一转 RGB；带 alpha 的图按 ``alpha_background`` 合成到不透明背景；
4. 对归一后的 8bit 灰度图计算 Laplacian 方差作为清晰度启发式；
5. 纯函数：不访问其他任务目录、不联网、不读写本算子以外的路径。

清晰度后端说明：优先使用 OpenCV（``opencv_laplacian``）；OpenCV 缺失或
调用失败时使用数学等价的 NumPy 实现（``numpy_laplacian``）。两者都是真实的
Laplacian 方差，实际使用的后端会写入 ``blur_backend``，不属于伪造结果。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 输出格式 -> 允许的扩展名
FORMAT_SUFFIXES = {"PNG": (".png",), "JPEG": (".jpg", ".jpeg")}
#: 支持读取的输入格式（仅做校验提示；实际解码以 Pillow 为准）
SUPPORTED_INPUT_FORMATS = ("JPEG", "PNG", "BMP", "GIF", "WEBP", "TIFF", "MPO")
#: EXIF 方向标记中表示“需要翻转/旋转”的取值
_ORIENTATION_WITH_TRANSFORM = (2, 3, 4, 5, 6, 7, 8)

_PIL_CACHE: Optional[Tuple[Any, Any]] = None


class ImageProcessingError(Exception):
    """图像处理失败：``reason`` 为稳定机器可读代码，``detail`` 为可解释补充。"""

    def __init__(self, reason: str, detail: Optional[str] = None):
        super().__init__("%s%s" % (reason, (":%s" % detail) if detail else ""))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #

def _load_pillow() -> Tuple[Any, Any]:
    """惰性导入 Pillow；缺失时给出结构化失败原因，不伪装成功。"""
    global _PIL_CACHE
    if _PIL_CACHE is None:
        try:
            from PIL import Image, ImageOps
        except Exception as exc:  # noqa: BLE001 - 缺包或安装损坏
            raise ImageProcessingError("dependency_missing:pillow",
                                       type(exc).__name__) from exc
        _PIL_CACHE = (Image, ImageOps)
    return _PIL_CACHE


def _load_numpy() -> Any:
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        raise ImageProcessingError("dependency_missing:numpy",
                                   type(exc).__name__) from exc
    return np


def pillow_available() -> bool:
    try:
        _load_pillow()
    except ImageProcessingError:
        return False
    return True


def available_blur_backend() -> str:
    """报告当前环境实际使用的清晰度后端，供 README/血缘如实记录。"""
    try:
        import cv2  # noqa: F401
    except Exception:  # noqa: BLE001
        return "numpy_laplacian"
    return "opencv_laplacian"


# --------------------------------------------------------------------------- #
# 纯函数：配置与尺寸
# --------------------------------------------------------------------------- #

def normalize_output_format(value: Any) -> str:
    fmt = str(value).strip().upper()
    if fmt not in FORMAT_SUFFIXES:
        raise ImageProcessingError("format_unsupported", str(value))
    return fmt


def suffix_for_format(fmt: str) -> str:
    suffixes = FORMAT_SUFFIXES.get(fmt)
    if not suffixes:
        raise ImageProcessingError("format_unsupported", str(fmt))
    return suffixes[0]


def parse_background(channels: Any) -> Tuple[int, int, int]:
    """背景色取前 3 通道（RGB）；第 4 通道为 alpha 预留，不参与不透明底色。"""
    if not isinstance(channels, (list, tuple)) or len(channels) not in (3, 4):
        raise ImageProcessingError("invalid_background", str(channels))
    values: List[int] = []
    for index, item in enumerate(list(channels)[:3]):
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255:
            raise ImageProcessingError("invalid_background", "channel_%d" % index)
        values.append(int(item))
    return values[0], values[1], values[2]


def plan_scale(width: int, height: int, max_side: int,
               allow_upscale: bool) -> Tuple[int, int, float, bool]:
    """等比缩放计划：返回 ``(新宽, 新高, 比例, 是否发生缩放)``。"""
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ImageProcessingError("invalid_size", "%sx%s" % (width, height))
    if isinstance(max_side, bool) or not isinstance(max_side, int) or max_side <= 0:
        raise ImageProcessingError("invalid_max_side", str(max_side))

    longest = max(width, height)
    ratio = max_side / float(longest)
    if ratio > 1.0 and not allow_upscale:
        ratio = 1.0
    new_width = max(1, int(round(width * ratio)))
    new_height = max(1, int(round(height * ratio)))
    resized = (new_width, new_height) != (width, height)
    return new_width, new_height, ratio, resized


# --------------------------------------------------------------------------- #
# 纯函数：清晰度
# --------------------------------------------------------------------------- #

def _numpy_laplacian_variance(array: Any) -> float:
    """4 邻域 Laplacian（边缘复制）的方差，与 OpenCV ksize=3 数学等价。"""
    np = _load_numpy()
    data = np.asarray(array, dtype=np.float64)
    if data.size == 0:
        return 0.0
    padded = np.pad(data, 1, mode="edge")
    laplacian = (padded[:-2, 1:-1] + padded[2:, 1:-1]
                 + padded[1:-1, :-2] + padded[1:-1, 2:]
                 - 4.0 * padded[1:-1, 1:-1])
    return float(laplacian.var())


def compute_laplacian_variance(gray_image: Any) -> Tuple[float, str]:
    """对 8bit 灰度图计算 Laplacian 方差，返回 ``(分数, 实际后端)``。

    分数越大越清晰；它是启发式指标，不是 0—1 概率。
    """
    np = _load_numpy()
    array = np.array(gray_image, dtype=np.uint8)
    if array.ndim != 2:
        raise ImageProcessingError("invalid_gray_image", "ndim_%d" % array.ndim)
    try:
        import cv2
    except Exception:  # noqa: BLE001 - 缺 OpenCV：使用 NumPy 等价实现
        return _numpy_laplacian_variance(array), "numpy_laplacian"
    try:
        laplacian = cv2.Laplacian(np.ascontiguousarray(array), cv2.CV_64F, ksize=3)
        return float(laplacian.var()), "opencv_laplacian"
    except Exception:  # noqa: BLE001 - OpenCV 运行异常：退回等价实现并记录
        return _numpy_laplacian_variance(array), "numpy_laplacian_fallback"


# --------------------------------------------------------------------------- #
# 主算法
# --------------------------------------------------------------------------- #

def _read_orientation(image: Any) -> Optional[int]:
    try:
        exif = image.getexif()
    except Exception:  # noqa: BLE001 - 无 EXIF 或读取失败
        return None
    try:
        value = exif.get(0x0112)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, int) and value > 0:
        return int(value)
    return None


def _composite_to_rgb(image: Any, background: Tuple[int, int, int]) -> Any:
    """把带 alpha 的图合成到不透明 RGB 背景上；无 alpha 直接转 RGB。"""
    Image, _ImageOps = _load_pillow()
    if image.mode in ("RGBA", "LA", "PA") or "A" in image.getbands():
        rgba = image.convert("RGBA")
        base = Image.new("RGB", rgba.size, background)
        base.paste(rgba, mask=rgba.split()[3])
        return base
    return image.convert("RGB")


def _save_atomic(image: Any, destination: Path, fmt: str, jpeg_quality: int) -> None:
    """临时文件保存后原子替换，避免失败时留下半截文件。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(destination.parent),
                                        prefix=destination.name + ".", suffix=".tmp")
    os.close(handle)
    try:
        if fmt == "JPEG":
            image.save(tmp_name, format="JPEG", quality=int(jpeg_quality), optimize=True)
        else:
            image.save(tmp_name, format="PNG", optimize=True)
        os.replace(tmp_name, destination)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def standardize_image(source: Path, destination: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    """方向归一 + 等比尺寸/格式标准化，返回归一结果与原始属性。

    ``path`` 为本地目标路径；相对包路径由 ``inference.py`` 在 run 层改写。
    纯函数不写入任务目录以外的位置，也不访问网络或其他任务。
    """
    if any(k in config for k in ("blur_threshold", "blur_action")) or config.get("allow_upscale", False):
        raise ValueError("表1禁止清晰度评分和放大图像")
    Image, ImageOps = _load_pillow()
    source = Path(source)
    destination = Path(destination)

    fmt = normalize_output_format(config.get("output_format", "JPEG"))
    max_side = int(config.get("max_side", 1024))
    allow_upscale = bool(config.get("allow_upscale", False))
    background = parse_background(config.get("alpha_background", [255, 255, 255]))
    max_pixels = int(config.get("max_pixels", 40000000))
    jpeg_quality = int(config.get("jpeg_quality", 95))

    suffix = destination.suffix.lower()
    if suffix not in FORMAT_SUFFIXES[fmt]:
        raise ImageProcessingError("suffix_mismatch",
                                   "format=%s suffix=%s" % (fmt, suffix or "(none)"))

    try:
        with Image.open(source) as opened:
            original_size = opened.size
            original_format = (opened.format or "").upper() or None
            original_mode = opened.mode
            if original_size[0] * original_size[1] > max_pixels:
                raise ImageProcessingError(
                    "pixel_limit_exceeded",
                    "%dx%d>%d" % (original_size[0], original_size[1], max_pixels))
            opened.load()
            orientation = _read_orientation(opened)
            oriented = opened
            if orientation in _ORIENTATION_WITH_TRANSFORM:
                transposed = ImageOps.exif_transpose(opened)
                if transposed is not None:
                    oriented = transposed
            oriented_size = oriented.size

            rgb = _composite_to_rgb(oriented, background)
            new_width, new_height, ratio, resized = plan_scale(
                rgb.size[0], rgb.size[1], max_side, allow_upscale)
            if resized:
                resampling = getattr(Image, "Resampling", Image).LANCZOS
                rgb = rgb.resize((new_width, new_height), resampling)

            _save_atomic(rgb, destination, fmt, jpeg_quality)
    except ImageProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow 解码/保存异常统一为可解释失败
        raise ImageProcessingError("decode_failed", type(exc).__name__) from exc

    transform: Dict[str, Any] = {
        "original_width": original_size[0],
        "original_height": original_size[1],
        "original_mode": original_mode,
        "original_format": original_format,
        "exif_orientation": orientation,
        "orientation_applied": bool(orientation in _ORIENTATION_WITH_TRANSFORM),
        "oriented_width": oriented_size[0],
        "oriented_height": oriented_size[1],
        "scale": round(ratio, 6),
        "resized": bool(resized),
        "upscaled": bool(resized and ratio > 1.0),
        "background": list(background),
        "output_format": fmt,
        "jpeg_quality": jpeg_quality if fmt == "JPEG" else None,
        "original_pixel_count": original_size[0] * original_size[1],
        "output_pixel_count": new_width * new_height,
    }
    return {
        "path": str(destination),
        "width": new_width,
        "height": new_height,
        "format": fmt,
        "transform": transform,
    }
