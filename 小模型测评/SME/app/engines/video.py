"""Video measurement engine (``type = "video"``).

Frames are decoded with OpenCV ``cv2.VideoCapture`` (no ffmpeg CLI).  Twelve
frames are sampled at uniform interval midpoints, avoiding the first/last frame
(which are often black or duplicated).  A file that cannot be opened or yields
zero decodable frames is an explicit ``ASSET_MISSING`` asset error in strict
mode.

Metrics
-------

``clarity`` (measured_rule, 0–100)
    Per-frame image clarity is ``100 * var / (var + 500)`` where ``var`` is the
    Laplacian variance of the grayscale frame resized to at most 512 px on the
    longest side.  The video score is a robust blend of the frame distribution:
    ``0.55 * mean + 0.30 * p20 + 0.15 * median``.  The lower percentile stops
    one sharp frame from hiding a mostly blurred clip; the median keeps a single
    bad transition frame from taking over.

``alignment`` (proxy, 0–100)
    Default: caption/keyword versus visual-attribute rule.  The caption is the
    first non-empty value among ``output``, ``caption``, ``title``,
    ``description``; visual attributes are inferred from brightness, saturated
    hue fractions and motion between sampled frames.  If the caption contains
    no recognised attribute keyword the neutral score is 60 (no evidence).
    Otherwise ``alignment = 30 + 70 * matched_terms / recognised_terms``.
    With ``config["alignmentMode"] == "clip"`` / ``SME_ALIGNMENT_MODE=clip``,
    the local CLIP ViT-L/14 model at ``weight/clip/ViT-L-14.pt`` is used instead;
    by default it samples 8 frames, chunks the caption at 75 tokens, and averages
    the best matching chunk per frame (while also keeping the truncated full
    caption cosine).  The keyword implementation remains available as an
    explicit fallback.

``trust`` (measured_rule, 0–100)
    Source domain rule: local path/``file://``/``local``/``demo://`` = 80,
    whitelisted domain = 100, known short-link/abuse domain = 40, unknown or
    missing = 60.  If no explicit ``source`` is supplied, the sample's
    ``input``/``uri`` locator is used, so a local file is not mislabelled as an
    unknown web source.  Whitelist, blocklist and scores are configurable
    through ``config["trust"]`` (``whitelist``/``blocked``/``scores``) or the
    legacy ``config["trustDomains"]``/``config["blockedDomains"]`` keys.

``safety`` (measured_rule, 0–100)
    ``max(0, 100 - penalty * hits)`` over violence/illegal/weapon keyword hits
    in caption/title text.  ``penalty`` defaults to 20 and can be overridden by
    ``config["safetyPenalty"]``; extra terms come from
    ``config["safetyKeywords"]``.

``integrity`` (measured_rule, 0–100)
    ``100 * (0.6 * decode_ratio + 0.2 * frame_ratio + 0.2 * duration_ratio)``
    where ``decode_ratio`` is decoded sample attempts / attempted reads,
    ``frame_ratio`` compares container frame count with an optional expected
    frame count (``parameters.frameCount``/``expectedFrames``) and
    ``duration_ratio`` compares container duration with an optional expected
    duration (``parameters.duration``/``expectedDuration``).  Missing expected
    metadata contributes 1.0.  If the container exposes neither frame count nor
    fps, the score is capped at 85 because full-file integrity cannot be proven.

VQA diagnostics
---------------
The engine also stores a **heuristic** technical-quality vector in
``meta.videos[id].quality``: sharpness, exposure, contrast, temporal stability,
resolution, and a weighted ``vqaScore``.  It is not a trained MOS predictor and
does not replace the five contract metrics.  A separate offline tool can compare
``vqaScore`` with real per-video MOS and report SRCC/PLCC only when labels
exist; without labels it must report ``MOS_MISSING``.

Both ``baseline`` and ``candidate`` describe the delivered input video.
Without an independent control this is a same-input diagnostic, not evidence
of training gains. Artificial degradation is opt-in metadata only.
"""
from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import cv2
import numpy as np
from PIL import Image

from ..core.envelope import ApiError
from .base import BaseEngine, EngineResult
from .image import clarity_score
from .media import (
    AssetUnavailable,
    clamp,
    missing_rows,
    read_asset_bytes,
    resolve_asset,
    round_metric,
    sample_asset_value,
    sample_dataset_id,
)

SAMPLED_FRAMES = 12
MIN_SAMPLED_FRAMES = 4
MAX_SAMPLED_FRAMES = 24
DESCRIPTOR_MAX_SIDE = 256
MAX_REMOTE_VIDEO_BYTES = 80 * 1024 * 1024
METRICS = ("clarity", "alignment", "trust", "safety", "integrity")
VQA_WEIGHTS = {
    "sharpness": 0.45,
    "exposure": 0.20,
    "contrast": 0.15,
    "temporal": 0.15,
    "resolution": 0.05,
}

DEFAULT_TRUST_DOMAINS = (
    "gov.cn", "edu.cn", "ac.cn", "nasa.gov", "who.int", "un.org",
    "europa.eu", "wikipedia.org", "archive.org",
)
DEFAULT_TRUST_SCORES = {"local": 80, "whitelisted": 100, "unknown": 60, "blocked": 40}

ALIGNMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "dark": ("dark", "night", "nighttime", "low light", "dim", "夜晚", "夜间",
             "低光", "黑暗", "昏暗"),
    "bright": ("bright", "sunny", "daylight", "daytime", "晴朗", "晴天",
               "明亮", "白天"),
    "green": ("green", "grass", "forest", "plant", "tree", "garden",
              "绿色", "草", "森林", "植物", "树"),
    "blue": ("blue", "sky", "sea", "ocean", "water", "天空", "蓝色", "海", "水"),
    "red": ("red", "红色", "红"),
    "motion": ("walk", "walking", "run", "running", "dance", "dancing",
               "move", "moving", "jump", "swim", "走", "跑", "跳", "舞",
               "移动"),
}

SAFETY_TERMS: dict[str, tuple[str, ...]] = {
    "violence": ("暴力", "血腥", "血", "枪击", "枪", "爆炸", "打斗", "尸体",
                 "blows up", "blow up", "war", "violence", "blood", "bloody",
                 "gun", "shoot", "shooting", "explosion", "explode", "kill",
                 "killing", "corpse", "fight"),
    "illegal": ("毒品", "吸毒", "赌博", "色情", "恐怖", "自杀", "drug", "drugs",
                "porn", "pornography", "gambling", "terror", "terrorist",
                "suicide"),
}


def _contains_term(caption: str, term: str) -> bool:
    if any("\u4e00" <= char <= "\u9fff" for char in term):
        return term in caption
    return re.search(rf"\b{re.escape(term)}\b", caption) is not None


def _caption_text(sample: dict[str, Any]) -> str:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    parts: list[str] = []
    for holder in (sample, parameters):
        for key in ("caption", "output", "title", "description"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                if text not in parts:
                    parts.append(text)
    return " ".join(parts)


def _source_value(sample: dict[str, Any]) -> str | None:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    # An explicit source/origin always wins.  Falling back to the media locator
    # is important: a local video with only ``input`` must not be scored as an
    # unknown web source.
    for holder in (sample, parameters):
        for key in ("source", "sourceUrl", "source_url", "origin", "url"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for holder in (sample, parameters):
        for key in ("input", "uri", "assetPath", "fileName", "sourceFile"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def trust_score(source: str | None, *, whitelist: tuple[str, ...] | list[str] | None = None,
                blocked: tuple[str, ...] | list[str] | None = None,
                scores: dict[str, float] | None = None) -> float:
    """Domain trust rule documented in the module docstring."""
    effective = dict(DEFAULT_TRUST_SCORES)
    if scores:
        for key, value in scores.items():
            if key in effective and isinstance(value, (int, float)):
                effective[key] = float(value)
    if not source:
        return round_metric(clamp(effective["unknown"]))
    text = source.strip()
    lowered = text.lower()
    if lowered in ("local", "file", "demo") or lowered.startswith(
            ("file://", "local://", "demo://")):
        return round_metric(clamp(effective["local"]))
    if not lowered.startswith(("http://", "https://")):
        if re.match(r"^[A-Za-z]:[\\/]", text) or lowered.startswith(("/", "\\", "./", "../")):
            return round_metric(clamp(effective["local"]))
        return round_metric(clamp(effective["unknown"]))
    hostname = (urlsplit(text).hostname or "").lower().removeprefix("www.")
    allowed = tuple(item.lower().lstrip(".") for item in (whitelist or DEFAULT_TRUST_DOMAINS))
    denied = tuple(item.lower().lstrip(".") for item in (blocked or ()))
    for domain in denied:
        if hostname == domain or hostname.endswith("." + domain):
            return round_metric(clamp(effective["blocked"]))
    for domain in allowed:
        if hostname == domain or hostname.endswith("." + domain):
            return round_metric(clamp(effective["whitelisted"]))
    return round_metric(clamp(effective["unknown"]))


def safety_score(text: str | None, *, extra_terms: tuple[str, ...] | list[str] | None = None,
                 penalty: float = 20.0) -> float:
    """100 minus keyword-hit penalty, floored at 0."""
    caption = str(text or "").lower()
    hits: set[str] = set()
    for terms in SAFETY_TERMS.values():
        for term in terms:
            if _contains_term(caption, term):
                hits.add(term)
    for term in extra_terms or ():
        if isinstance(term, str) and term.strip() and _contains_term(caption, term.strip().lower()):
            hits.add(term.strip().lower())
    return round_metric(clamp(100.0 - float(penalty) * len(hits)))


def _visual_descriptors(frames: list[np.ndarray]) -> dict[str, float]:
    brightness: list[float] = []
    saturation: list[float] = []
    green: list[float] = []
    blue: list[float] = []
    red: list[float] = []
    edges: list[float] = []
    previous_gray: np.ndarray | None = None
    motion: list[float] = []
    for frame in frames:
        height, width = frame.shape[:2]
        scale = min(1.0, DESCRIPTOR_MAX_SIDE / float(max(height, width)))
        small = cv2.resize(frame, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA) if scale < 1.0 else frame
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        brightness.append(float(hsv[:, :, 2].mean()) / 255.0 * 100.0)
        saturation.append(float(hsv[:, :, 1].mean()) / 255.0 * 100.0)
        mask = hsv[:, :, 1] > 60
        total = max(1, int(mask.sum()))
        hue = hsv[:, :, 0]
        green.append(float(((hue >= 35) & (hue <= 85) & mask).sum()) / total)
        blue.append(float(((hue >= 86) & (hue <= 130) & mask).sum()) / total)
        red.append(float((((hue <= 10) | (hue >= 160)) & mask).sum()) / total)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gx = np.zeros_like(gray)
        gy = np.zeros_like(gray)
        gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
        gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
        magnitude = np.hypot(gx, gy)
        edges.append(float((magnitude > 24).mean()))
        if previous_gray is not None and previous_gray.shape == gray.shape:
            motion.append(float(np.abs(gray - previous_gray).mean()))
        previous_gray = gray
    return {
        "brightness": float(np.mean(brightness)) if brightness else 0.0,
        "saturation": float(np.mean(saturation)) if saturation else 0.0,
        "green": float(np.mean(green)) if green else 0.0,
        "blue": float(np.mean(blue)) if blue else 0.0,
        "red": float(np.mean(red)) if red else 0.0,
        "edgeDensity": float(np.mean(edges)) if edges else 0.0,
        "motion": float(np.mean(motion)) if motion else 0.0,
    }


def _visual_attributes(descriptors: dict[str, float]) -> set[str]:
    attributes: set[str] = set()
    if descriptors["brightness"] < 75:
        attributes.add("dark")
    if descriptors["brightness"] > 135:
        attributes.add("bright")
    for color in ("green", "blue", "red"):
        others = [descriptors[key] for key in ("green", "blue", "red") if key != color]
        if descriptors[color] >= 0.25 and descriptors[color] > max(others):
            attributes.add(color)
    if descriptors["motion"] > 8.0 or descriptors["edgeDensity"] > 0.08:
        attributes.add("motion")
    return attributes


def alignment_score(caption: str, descriptors: dict[str, float]) -> float:
    """Keyword/visual-attribute proxy described in the module docstring."""
    text = caption.strip().lower()
    if not text:
        return 60.0
    recognised = [
        attribute for attribute, terms in ALIGNMENT_KEYWORDS.items()
        if any(_contains_term(text, term) for term in terms)
    ]
    if not recognised:
        return 60.0
    attributes = _visual_attributes(descriptors)
    matched = sum(1 for attribute in recognised if attribute in attributes)
    return round_metric(clamp(30.0 + 70.0 * matched / len(recognised)))


def _alignment_mode(config: dict[str, Any]) -> str:
    raw = config.get("alignmentMode") or os.environ.get("SME_ALIGNMENT_MODE") or "keyword"
    mode = str(raw).strip().lower()
    if mode in {"clip", "model", "clip-vit-l14"}:
        return "clip"
    if mode in {"keyword", "legacy", "proxy", "old"}:
        return "keyword"
    return "keyword"


def _clip_alignment_options(config: dict[str, Any]) -> dict[str, Any] | None:
    if _alignment_mode(config) != "clip":
        return None
    options: dict[str, Any] = {}
    model = config.get("clipModel") or os.environ.get("SME_CLIP_MODEL")
    if isinstance(model, str) and model.strip():
        options["model_path"] = model.strip()
    raw_frames = config.get("clipFrameSamples", config.get("clipFrames", 8))
    try:
        options["frame_samples"] = max(1, min(12, int(float(raw_frames))))
    except (TypeError, ValueError):
        options["frame_samples"] = 8
    raw_mode = config.get("clipCaptionMode") or os.environ.get(
        "SME_CLIP_CAPTION_MODE", "chunk"
    )
    mode = str(raw_mode).strip().lower()
    options["caption_mode"] = mode if mode in {"chunk", "full"} else "chunk"
    raw_chunk_tokens = config.get("clipChunkTokens", 75)
    try:
        options["chunk_tokens"] = max(1, min(75, int(float(raw_chunk_tokens))))
    except (TypeError, ValueError):
        options["chunk_tokens"] = 75
    raw_combine = config.get("clipCombineFull", os.environ.get("SME_CLIP_COMBINE_FULL", "1"))
    if isinstance(raw_combine, bool):
        options["combine_full"] = raw_combine
    else:
        options["combine_full"] = str(raw_combine).strip().lower() not in {
            "0", "false", "no", "off", ""
        }
    default_floor = 0.15
    default_ceiling = 0.40
    try:
        default_floor = float(os.environ.get("SME_CLIP_COS_FLOOR", "0.15"))
        default_ceiling = float(os.environ.get("SME_CLIP_COS_CEILING", "0.40"))
    except (TypeError, ValueError):
        default_floor, default_ceiling = 0.15, 0.40
    try:
        options["cosine_floor"] = float(config.get("clipCosineFloor", default_floor))
    except (TypeError, ValueError):
        options["cosine_floor"] = default_floor
    try:
        options["cosine_ceiling"] = float(config.get("clipCosineCeiling", default_ceiling))
    except (TypeError, ValueError):
        options["cosine_ceiling"] = default_ceiling
    if options["cosine_ceiling"] <= options["cosine_floor"]:
        options["cosine_floor"], options["cosine_ceiling"] = 0.15, 0.40
    return options


def _frame_clarity(frame: np.ndarray) -> float:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return clarity_score(Image.fromarray(rgb))


def _clarity_components(frames: list[np.ndarray]) -> dict[str, float]:
    values = np.asarray([_frame_clarity(frame) for frame in frames], dtype=np.float64)
    if values.size == 0:
        return {"mean": 0.0, "p20": 0.0, "median": 0.0, "min": 0.0}
    return {
        "mean": float(values.mean()),
        "p20": float(np.percentile(values, 20)),
        "median": float(np.median(values)),
        "min": float(values.min()),
    }


def _robust_clarity(frames: list[np.ndarray]) -> float:
    """Blend sharpness across frames instead of trusting one sharp frame."""
    components = _clarity_components(frames)
    if len(frames) <= 1:
        return round_metric(clamp(components["mean"]))
    score = (0.55 * components["mean"]
             + 0.30 * components["p20"]
             + 0.15 * components["median"])
    return round_metric(clamp(score))


def _frame_luma_stats(frame: np.ndarray) -> tuple[float, float, float, float]:
    height, width = frame.shape[:2]
    scale = min(1.0, DESCRIPTOR_MAX_SIDE / float(max(height, width)))
    small = (cv2.resize(frame, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_AREA)
             if scale < 1.0 else frame)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return (
        float(gray.mean()),
        float(gray.std()),
        float((gray <= 8.0).mean()),
        float((gray >= 247.0).mean()),
    )


def _quality_diagnostics(frames: list[np.ndarray], info: dict[str, Any]) -> dict[str, Any]:
    """Heuristic VQA-style diagnostics; not a trained MOS model.

    The vector is intentionally kept outside the five contract metrics.  Its
    purpose is to make an audit/report explain *why* clarity is low (blur,
    exposure, contrast, freeze/flicker, resolution) and to give the offline
    VQA tool one stable prediction to correlate with real MOS labels.
    """
    if not frames:
        return {
            "method": "heuristic-vqa-v1",
            "vqaScore": 0.0,
            "dimensions": {key: 0.0 for key in VQA_WEIGHTS},
            "stats": {},
            "flags": ["no_frames"],
            "weights": dict(VQA_WEIGHTS),
        }

    luma_stats = [_frame_luma_stats(frame) for frame in frames]
    means = np.asarray([row[0] for row in luma_stats], dtype=np.float64)
    stds = np.asarray([row[1] for row in luma_stats], dtype=np.float64)
    dark_clip = np.asarray([row[2] for row in luma_stats], dtype=np.float64)
    bright_clip = np.asarray([row[3] for row in luma_stats], dtype=np.float64)
    mean_luma = float(means.mean())
    mean_std = float(stds.mean())

    gray_frames: list[np.ndarray] = []
    for frame in frames:
        height, width = frame.shape[:2]
        scale = min(1.0, DESCRIPTOR_MAX_SIDE / float(max(height, width)))
        small = (cv2.resize(frame, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_AREA)
                 if scale < 1.0 else frame)
        gray_frames.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32))
    diffs = [
        float(np.abs(gray_frames[index] - gray_frames[index - 1]).mean())
        for index in range(1, len(gray_frames))
        if gray_frames[index].shape == gray_frames[index - 1].shape
    ]
    freeze_ratio = float(np.mean(np.asarray(diffs) < 0.25)) if diffs else 0.0
    # Scene cuts are not flicker.  Trim the largest 20% of frame differences
    # before estimating temporal instability.
    trimmed = diffs
    if len(diffs) >= 5:
        cutoff = float(np.percentile(diffs, 80))
        trimmed = [value for value in diffs if value <= cutoff]
    flicker = (float(np.std(trimmed) / (np.mean(trimmed) + 1e-6))
               if len(trimmed) > 1 else 0.0)

    sharpness = float(np.mean([_frame_clarity(frame) for frame in frames]))
    exposure = 100.0
    exposure -= min(45.0, max(0.0, 40.0 - mean_luma) * 1.5)
    exposure -= min(45.0, max(0.0, mean_luma - 220.0) * 1.5)
    exposure -= min(20.0, float(dark_clip.mean() + bright_clip.mean()) * 100.0)
    contrast = 100.0 * min(1.0, mean_std / 60.0)
    temporal = 100.0
    temporal -= 40.0 * min(1.0, freeze_ratio)
    temporal -= 25.0 * min(1.0, flicker / 3.0)
    min_side = min(int(info.get("width") or 0), int(info.get("height") or 0))
    resolution = 100.0 * min(1.0, min_side / 480.0) if min_side > 0 else 0.0
    dimensions = {
        "sharpness": round_metric(clamp(sharpness)),
        "exposure": round_metric(clamp(exposure)),
        "contrast": round_metric(clamp(contrast)),
        "temporal": round_metric(clamp(temporal)),
        "resolution": round_metric(clamp(resolution)),
    }
    vqa_score = round_metric(clamp(sum(
        dimensions[key] * weight for key, weight in VQA_WEIGHTS.items())))

    black_ratio = float(np.mean(means < 8.0))
    flags: list[str] = []
    if black_ratio >= 0.5:
        flags.append("black_frames")
    if freeze_ratio >= 0.6:
        flags.append("frozen")
    if mean_luma < 40.0:
        flags.append("low_light")
    if mean_luma > 220.0 or float(bright_clip.mean()) > 0.2:
        flags.append("overexposed")
    if mean_std < 20.0:
        flags.append("low_contrast")
    if 0 < min_side < 240:
        flags.append("low_resolution")
    if flicker > 3.0:
        flags.append("flicker")
    return {
        "method": "heuristic-vqa-v1",
        "vqaScore": vqa_score,
        "dimensions": dimensions,
        "stats": {
            "frames": len(frames),
            "meanLuma": round(mean_luma, 2),
            "lumaStd": round(mean_std, 2),
            "blackFrameRatio": round(black_ratio, 4),
            "darkClipRatio": round(float(dark_clip.mean()), 4),
            "brightClipRatio": round(float(bright_clip.mean()), 4),
            "freezeRatio": round(freeze_ratio, 4),
            "flickerIndex": round(flicker, 4),
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
        },
        "flags": flags,
        "weights": dict(VQA_WEIGHTS),
    }


def _frame_sample_count(config: dict[str, Any] | None) -> int:
    raw: Any = SAMPLED_FRAMES
    if isinstance(config, dict):
        raw = config.get("frameSamples", config.get("sampledFrames", raw))
        sampling = config.get("sampling")
        if isinstance(sampling, dict) and sampling.get("frames") is not None:
            raw = sampling["frames"]
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = SAMPLED_FRAMES
    return max(MIN_SAMPLED_FRAMES, min(MAX_SAMPLED_FRAMES, value))


def _sample_positions(frame_count: int, count: int) -> list[int]:
    """Uniform interval midpoints in frame-index space."""
    if frame_count <= 0 or count <= 0:
        return []
    count = min(count, frame_count)
    if count == 1:
        return [frame_count // 2]
    raw = (np.arange(count, dtype=np.float64) + 0.5) / count * (frame_count - 1)
    return sorted({int(round(value)) for value in raw})


def _expected_number(sample: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    parameters = sample.get("parameters") if isinstance(sample.get("parameters"), dict) else {}
    for holder in (sample, parameters):
        for key in keys:
            value = holder.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
    return None


def integrity_score(info: dict[str, Any], sample: dict[str, Any]) -> float:
    """``100 * (0.6*decode + 0.2*frames + 0.2*duration)``."""
    decoded = len(info["frames"])
    attempts = max(1, int(info["attempts"]))
    decode_ratio = min(1.0, decoded / attempts)
    container_frames = int(info.get("frameCount") or 0)
    expected_frames = _expected_number(sample, ("frameCount", "expectedFrames", "frames"))
    if expected_frames:
        available = float(container_frames) if container_frames > 0 else float(decoded)
        frame_ratio = min(available, expected_frames) / max(available, expected_frames)
    else:
        frame_ratio = 1.0 if container_frames > 0 else decode_ratio
    fps = float(info.get("fps") or 0.0)
    expected_duration = _expected_number(
        sample, ("duration", "expectedDuration", "durationSeconds"))
    if expected_duration and fps > 0 and container_frames > 0:
        actual_duration = container_frames / fps
        duration_ratio = min(actual_duration, expected_duration) / max(
            actual_duration, expected_duration)
    else:
        duration_ratio = 1.0
    score = 100.0 * (0.6 * decode_ratio + 0.2 * frame_ratio + 0.2 * duration_ratio)
    # If the container reports neither frame count nor fps, we cannot prove that
    # the decode covered the whole file.  Keep a visible cap instead of calling
    # a first-frames-only read "100% intact".
    if container_frames <= 0 or fps <= 0:
        score = min(score, 85.0)
    return round_metric(clamp(score))


def _decode_video(path: Path, frame_samples: int = SAMPLED_FRAMES) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise AssetUnavailable("视频无法打开或解码")
    try:
        frame_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        frames: list[np.ndarray] = []
        attempts = 0
        positions: list[int] = []
        seek_fallback = False
        if frame_count > 0:
            positions = _sample_positions(frame_count, frame_samples)
            for position in positions:
                attempts += 1
                capture.set(cv2.CAP_PROP_POS_FRAMES, position)
                ok, frame = capture.read()
                if ok and frame is not None:
                    frames.append(frame)
            if not frames:
                # Some containers report a frame count but reject seeks; fall
                # back to sequential decoding of the first frames.
                seek_fallback = True
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                for _ in range(frame_samples):
                    attempts += 1
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break
                    frames.append(frame)
        else:
            for _ in range(frame_samples):
                attempts += 1
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                frames.append(frame)
        if not frames:
            raise AssetUnavailable("视频没有可解码帧")
        return {
            "frames": frames,
            "attempts": attempts,
            "frameCount": frame_count,
            "fps": fps,
            "width": width,
            "height": height,
            "samplePositions": positions,
            "seekFallback": seek_fallback,
            "requestedFrames": frame_samples,
        }
    finally:
        capture.release()


def _degraded_frames(frames: list[np.ndarray], seed: int) -> tuple[list[np.ndarray], float]:
    scale = round(0.55 + (int(seed) % 5) * 0.02, 2)
    degraded: list[np.ndarray] = []
    for frame in frames:
        height, width = frame.shape[:2]
        small = cv2.resize(frame, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA)
        restored = cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)
        degraded.append(cv2.GaussianBlur(restored, (3, 3), 0.6))
    return degraded, scale


@contextmanager
def _video_file(sample: dict[str, Any]) -> Iterator[Path]:
    reference = resolve_asset(sample_asset_value(sample),
                              dataset_id=sample_dataset_id(sample))
    if reference.kind == "local":
        yield Path(reference.location)
        return
    payload = read_asset_bytes(reference, max_bytes=MAX_REMOTE_VIDEO_BYTES)
    suffix = Path(urlsplit(reference.location).path).suffix or ".mp4"
    handle = tempfile.NamedTemporaryFile(
        prefix="sme-video-", suffix=suffix, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
        yield temporary
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _trust_options(config: dict[str, Any]) -> dict[str, Any]:
    trust = config.get("trust") if isinstance(config.get("trust"), dict) else {}
    scores = dict(DEFAULT_TRUST_SCORES)
    raw_scores = trust.get("scores") if isinstance(trust.get("scores"), dict) else {}
    scores.update({key: value for key, value in raw_scores.items()
                   if key in scores and isinstance(value, (int, float))})
    return {
        "whitelist": trust.get("whitelist") or config.get("trustDomains"),
        "blocked": trust.get("blocked") or config.get("blockedDomains"),
        "scores": scores,
    }


class VideoEngine(BaseEngine):
    type = "video"

    def measure(self, samples: list[dict[str, Any]], config: dict[str, Any], *,
                seed: int = 42, strict: bool = True) -> EngineResult:
        frame_samples = _frame_sample_count(config)
        clip_options = _clip_alignment_options(config)
        result = EngineResult(provenance={
            "clarity": "measured_rule",
            "alignment": "proxy",
            "trust": "measured_rule",
            "safety": "measured_rule",
            "integrity": "measured_rule",
        })
        result.notes.append(
            "视频五维为规则/代理测量：清晰度按 12 帧分位稳健合成，完整性实测容器/解码，"
            "图文对齐默认关键词+画面属性代理；alignmentMode=clip 时改用本地 CLIP ViT-L/14，"
            "默认 8 帧并按 75 token 分块、帧-块取最高后平均；"
            "来源可信度为规则，安全仅检查文本关键词，不评估画面安全。")
        result.notes.append("baseline/candidate 均评价输入原视频（same_input_diagnostic），不代表训练收益。")
        result.notes.append(
            "quality 是启发式技术质量诊断（清晰/曝光/对比/时域/分辨率），不是训练好的 MOS 模型；"
            "没有逐视频 MOS 标签就不能计算 SRCC/PLCC。")
        result.meta.update({
            "engine": "VideoEngine",
            "engineVersion": "video-vqa-v3",
            "framesSampled": frame_samples,
            "sampling": {
                "strategy": "uniform-midpoint",
                "requestedFrames": frame_samples,
                "minFrames": MIN_SAMPLED_FRAMES,
                "maxFrames": MAX_SAMPLED_FRAMES,
            },
            "vqa": {
                "method": "heuristic-vqa-v1",
                "weights": dict(VQA_WEIGHTS),
                "labelStatus": "not_consumed_by_engine",
                "assessmentTarget": "input_video",
            },
            "seed": seed,
            "sampleCount": len(samples),
            "assessmentTarget": "input_video",
            "comparisonMode": "same_input_diagnostic",
            "trainingGainEvidence": False,
            "degradationDiagnosticEnabled": config.get("degradationDiagnostic") is True,
            "alignment": {
                "mode": "clip" if clip_options else "keyword",
                "model": (clip_options or {}).get("model_path", "operator-model://SME_EVAL_VIDEO_001/clip/ViT-L-14.pt"),
                "frameSamples": (clip_options or {}).get("frame_samples"),
                "captionMode": (clip_options or {}).get("caption_mode"),
                "chunkTokens": (clip_options or {}).get("chunk_tokens"),
                "combineFull": (clip_options or {}).get("combine_full"),
                "calibration": {
                    "floor": (clip_options or {}).get("cosine_floor"),
                    "ceiling": (clip_options or {}).get("cosine_ceiling"),
                } if clip_options else None,
            },
        })
        trust_options = _trust_options(config)
        extra_safety = config.get("safetyKeywords") if isinstance(
            config.get("safetyKeywords"), list) else None
        safety_penalty = config.get("safetyPenalty", 20)
        if not isinstance(safety_penalty, (int, float)) or safety_penalty < 0:
            safety_penalty = 20
        video_meta: dict[str, Any] = {}
        clip_used = 0
        clip_failed = 0

        for index, sample in enumerate(samples):
            sample_id = str(sample.get("id") or f"S{index + 1:03d}")
            try:
                with _video_file(sample) as path:
                    info = _decode_video(path, frame_samples)
            except AssetUnavailable as exc:
                detail = str(exc)
                if strict:
                    raise ApiError(
                        422, "ASSET_MISSING",
                        f"样本 {sample_id} 的视频资产缺失或无法解码：{detail}",
                        {"sampleId": sample_id, "reason": "ASSET_MISSING"},
                    ) from exc
                for metric in METRICS:
                    result.missing.extend(
                        missing_rows(sample_id, metric, "ASSET_MISSING", detail))
                result.notes.append(f"样本 {sample_id} 跳过：{detail}")
                continue

            frames = info["frames"]
            caption = _caption_text(sample)
            descriptors = _visual_descriptors(frames)
            trust = trust_score(_source_value(sample), **trust_options)
            safety = safety_score(
                caption, extra_terms=extra_safety, penalty=float(safety_penalty))
            integrity = integrity_score(info, sample)
            quality = _quality_diagnostics(frames, info)
            clarity = _robust_clarity(frames)
            # Legacy keyword alignment call, kept for rollback.  It is no
            # longer the active path when alignmentMode=clip:
            # alignment = alignment_score(caption, descriptors)
            clip_info: dict[str, Any] | None = None
            if clip_options:
                try:
                    from .clip_alignment import align_frames
                    clip_info = align_frames(frames, caption, **clip_options)
                    alignment = float(clip_info["alignment"])
                    clip_used += 1
                except Exception as exc:  # optional model must not fail a batch
                    clip_failed += 1
                    alignment = alignment_score(caption, descriptors)
                    clip_info = {
                        "status": "fallback_keyword",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    result.notes.append(
                        f"样本 {sample_id} CLIP 对齐不可用，已退回关键词代理："
                        f"{type(exc).__name__}: {exc}")
            else:
                alignment = alignment_score(caption, descriptors)
            result.measurements[sample_id] = {
                "baseline": {
                    "clarity": clarity,
                    "alignment": alignment,
                    "trust": trust,
                    "safety": safety,
                    "integrity": integrity,
                },
                "candidate": {
                    "clarity": clarity,
                    "alignment": alignment,
                    "trust": trust,
                    "safety": safety,
                    "integrity": integrity,
                },
            }
            video_meta[sample_id] = {
                "framesSampled": len(frames),
                "requestedFrames": frame_samples,
                "attempts": info["attempts"],
                "samplePositions": info.get("samplePositions", []),
                "seekFallback": bool(info.get("seekFallback")),
                "frameCount": info["frameCount"],
                "fps": round(info["fps"], 3),
                "width": info["width"],
                "height": info["height"],
                "durationSeconds": (round(info["frameCount"] / info["fps"], 3)
                                    if info["fps"] > 0 and info["frameCount"] > 0 else None),
                "assessmentTarget": "input_video",
                "comparisonMode": "same_input_diagnostic",
                "evidence": {
                    "captionStatus": "provided" if caption else "missing",
                    "alignmentStatus": (
                        "clip_image_text" if clip_info and clip_info.get("status") == "ok"
                        else "keyword_visual_proxy" if any(
                            _contains_term(caption.lower(), term)
                            for terms in ALIGNMENT_KEYWORDS.values() for term in terms)
                        else "no_recognised_keywords" if caption else "caption_missing"),
                    "alignmentMethod": (
                        clip_info.get("method", "clip-vit-l14-cosine-v1")
                        if clip_info and clip_info.get("status") == "ok"
                        else "keyword_visual_proxy"),
                    "captionMode": (clip_info or {}).get("captionMode"),
                    "clipCosine": (clip_info or {}).get("cosine"),
                    "clipFullCosine": (clip_info or {}).get("fullCosine"),
                    "clipChunkCosine": (clip_info or {}).get("chunkCosine"),
                    "clipChunkCount": (clip_info or {}).get("chunkCount"),
                    "clipChunkTokens": (clip_info or {}).get("chunkTokens"),
                    "clipCalibration": (clip_info or {}).get("calibration"),
                    "safetyMethod": "caption_keyword_rule",
                    "visualSafetyAssessed": False,
                    "mosStatus": "not_consumed_by_engine",
                },
                "quality": quality,
            }

            if config.get("degradationDiagnostic") is True:
                degraded, scale = _degraded_frames(frames, seed)
                video_meta[sample_id]["degradationDiagnostic"] = {
                    "assessmentTarget": "synthetically_degraded_input",
                    "scale": scale,
                    "clarity": _robust_clarity(degraded),
                    "quality": _quality_diagnostics(degraded, info),
                    "usedForScoring": False,
                }

        result.meta["videos"] = video_meta
        if clip_options:
            result.meta["alignment"].update({
                "usedSamples": clip_used,
                "fallbackSamples": clip_failed,
                "status": "used" if clip_used and not clip_failed else
                          "partial_fallback" if clip_used else "fallback_keyword",
            })
            if clip_used:
                result.provenance["alignment"] = "measured_model"
            if clip_failed:
                result.notes.append(
                    f"CLIP 对齐失败 {clip_failed} 条，已按关键词代理回退；成功 {clip_used} 条。")
        vqa_scores = [row["quality"]["vqaScore"]
                      for row in video_meta.values() if row.get("quality")]
        if vqa_scores:
            result.meta["vqa"].update({
                "scoredVideos": len(vqa_scores),
                "meanVqaScore": round(float(np.mean(vqa_scores)), 4),
                "minVqaScore": round(float(np.min(vqa_scores)), 4),
                "maxVqaScore": round(float(np.max(vqa_scores)), 4),
            })
        return result
