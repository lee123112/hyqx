"""算子 5 业务算法：按时间间隔抽取可复现关键帧，输出真实帧索引与时间信息。

范围（TECHNICAL_DESIGN §1、§4）：
- 输入单条短视频，输出关键帧文件与帧清单（``path``/``timestamp``/``frame_index``/
  ``width``/``height``）；原视频与音频交回上层搬运，本模块不改写视频容器；
- 固定采样（``sample_fps``）与可选保守裁边（多帧一致的近黑外缘）；
- 不做水印去除、事件分段、文本—视频语义标注，不生成任何声画语义结论。

纯函数（``select_frame_indices``、``parse_ffprobe_output`` 等）不访问网络、
不读写其他任务的目录，便于独立测试。
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

#: 帧产物目录名（相对本次 run 目录）
FRAMES_DIR_NAME = "05_frames"
#: 帧文件保存质量（JPEG）
JPEG_QUALITY = 90
#: 8bit 灰度近黑阈值：行/列均值不高于该值视为近黑外缘
BLACK_LEVEL = 16.0
#: 单边近黑比例上限：超过该比例认为不是黑边而是整幅暗图，放弃裁剪
BLACK_BORDER_MAX_RATIO = 0.4
#: 裁剪后最少保留面积比例
MIN_KEEP_AREA_RATIO = 0.25
#: 解码 PTS 与 CFR 期望时间戳的最大允许偏差（秒）；超过即判为可变帧率
VFR_PTS_TOLERANCE_SECONDS = 0.05
#: 判定 VFR 所需的最少超差帧数（避免单帧抖动误判）
VFR_MIN_OUTLIER_FRAMES = 2
#: 帧文件名安全字符
_UNSAFE_COMPONENT = re.compile(r"[^0-9A-Za-z._-]+")
#: 采样步数硬上限：超过即明确 resource_limit，不做静默截断
MAX_SELECTION_STEPS = 100002


class VideoProcessingError(Exception):
    """本算子业务错误的基类。"""


class MediaUnreadableError(VideoProcessingError):
    """视频容器打不开或没有任何可解码帧。"""


class VideoMetadataError(VideoProcessingError):
    """FPS / 总帧数 / 时长不是正有限值。"""


class ResourceLimitError(VideoProcessingError):
    """超过 max_frames / max_duration_seconds 等资源限额。"""


class UnsupportedVFRError(VideoProcessingError):
    """可变帧率且无可靠时间戳，按 ``unsupported_vfr_policy`` 拒绝。"""


class FrameSaveError(VideoProcessingError):
    """帧文件写入后无法重新打开验证。"""


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

def select_frame_indices(frame_count: int, fps: float, sample_fps: float,
                         max_frames: int) -> List[int]:
    """选择采样时间点 ``t = 0, 1/sample_fps, ...`` 且 ``t < duration`` 的帧索引。

    舍入规则：CFR 视频取 ``index = ceil(t * fps)``，即"不早于 t 的第一帧栅格"；
    因浮点误差留 1e-9 容差，使 ``t = 1/fps`` 正好落在整数栅格上。
    越界索引剔除，重复索引（``sample_fps > fps`` 时出现）去重。

    :raises ValueError: 入参不是正有限值
    :raises ResourceLimitError: 采样的帧数超过 ``max_frames``
    """
    if isinstance(frame_count, bool) or not isinstance(frame_count, int):
        raise ValueError("frame_count 必须是整数")
    if frame_count <= 0:
        raise ValueError("frame_count 必须大于 0")
    for name, value in (("fps", fps), ("sample_fps", sample_fps)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s 必须是数值" % name)
        if not math.isfinite(float(value)):
            raise ValueError("%s 必须是有限值" % name)
        if float(value) <= 0:
            raise ValueError("%s 必须大于 0" % name)
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1:
        raise ValueError("max_frames 必须是大于等于 1 的整数")

    fps_value = float(fps)
    step = 1.0 / float(sample_fps)
    duration = frame_count / fps_value
    # 采样步数上限：覆盖整段时长所需步数（+2 容错），且不超过硬上限，避免任何情况下无限循环
    needed_steps = int(math.ceil(duration * float(sample_fps))) + 2
    step_budget = min(needed_steps, MAX_SELECTION_STEPS)

    indices: List[int] = []
    seen = set()
    reached_end = False
    for step_index in range(step_budget):
        timestamp = step_index * step
        if timestamp >= duration - 1e-9:
            reached_end = True
            break
        index = int(math.ceil(timestamp * fps_value - 1e-9))
        if index < 0:
            index = 0
        if index >= frame_count or index in seen:
            continue
        seen.add(index)
        indices.append(index)
        if len(indices) >= max_frames:
            return indices  # 表1：多出的帧不保留，不把长视频误判为损坏。
    if not reached_end:
        # 未能覆盖整段时长：宁可明确失败，也不静默截断为"完整成功"
        raise ResourceLimitError(
            "selection_budget_exceeded：时长 %.3f 秒按 %.3f 帧/秒需要超过 %d 次采样步进"
            % (duration, float(sample_fps), MAX_SELECTION_STEPS))
    return indices


def parse_ffprobe_output(text: str) -> Dict[str, Any]:
    """解析 ``ffprobe -print_format json -show_streams`` 输出中的视频流信息（纯函数）。

    只读取帧率与时长，用于辅助判断可变帧率；解析失败抛 ``ValueError``。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("ffprobe 输出为空")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("ffprobe 输出不是合法 JSON") from exc
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ValueError("ffprobe 输出缺少 streams")
    video = None
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            video = stream
            break
    if video is None:
        raise ValueError("ffprobe 输出中没有视频流")

    def _ratio_to_float(value: Any) -> Optional[float]:
        if not isinstance(value, str) or "/" not in value:
            return None
        numerator, _, denominator = value.partition("/")
        try:
            top, bottom = float(numerator), float(denominator)
        except ValueError:
            return None
        if bottom == 0 or not math.isfinite(top) or not math.isfinite(bottom) or top <= 0:
            return None
        return top / bottom

    def _float_or_none(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    average = _ratio_to_float(video.get("avg_frame_rate"))
    raw = _ratio_to_float(video.get("r_frame_rate"))
    duration = _float_or_none(video.get("duration"))
    if duration is None:
        duration = _float_or_none((payload.get("format") or {}).get("duration"))
    vfr_hint: Optional[bool] = None
    if average is not None and raw is not None:
        vfr_hint = abs(average - raw) > max(0.01, 0.02 * raw)
    return {
        "backend": "ffprobe",
        "avg_frame_rate": average,
        "r_frame_rate": raw,
        "duration": duration,
        "nb_frames": _float_or_none(video.get("nb_frames")),
        "vfr_hint": vfr_hint,
    }


def probe_video(source: Path, timeout_seconds: float) -> Dict[str, Any]:
    """用 ffprobe 读容器元数据；ffprobe 缺失/超时/输出非法时返回可解释的不可用状态。

    本函数只提供旁证，不覆盖 OpenCV 的解码结果；任何情况下都不抛异常。
    """
    executable = shutil.which("ffprobe")
    if not executable:
        return {"backend": "unavailable", "reason": "ffprobe_not_found"}
    command = [executable, "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", str(source)]
    try:
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=float(timeout_seconds), check=False)
    except subprocess.TimeoutExpired:
        return {"backend": "unavailable", "reason": "ffprobe_timeout"}
    except OSError as exc:
        return {"backend": "unavailable", "reason": "ffprobe_exec_error:%s" % type(exc).__name__}
    if completed.returncode != 0:
        return {"backend": "unavailable", "reason": "ffprobe_exit_%d" % completed.returncode}
    try:
        text = completed.stdout.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 解码失败按不可用处理
        return {"backend": "unavailable", "reason": "ffprobe_output_undecodable"}
    try:
        return parse_ffprobe_output(text)
    except ValueError as exc:
        return {"backend": "unavailable", "reason": "ffprobe_parse_error:%s" % exc}


def detect_black_borders(gray_frames: Sequence[np.ndarray], black_level: float = BLACK_LEVEL
                         ) -> Optional[Tuple[int, int, int, int]]:
    """检测多帧一致的近黑外缘，返回保守的 ``(top, bottom, left, right)``（像素）。

    保守含义：对每帧各自统计近黑行/列，取所有帧的最小值，避免把画面内容裁掉。
    单边比例超过 ``BLACK_BORDER_MAX_RATIO`` 或裁剪后保留面积不足时返回 ``None``
    （表示放弃裁剪，而不是裁成空图）。
    """
    if not gray_frames:
        return None
    tops: List[int] = []
    bottoms: List[int] = []
    lefts: List[int] = []
    rights: List[int] = []
    for frame in gray_frames:
        if frame.ndim != 2:
            return None
        height, width = frame.shape[:2]
        row_mean = frame.mean(axis=1)
        col_mean = frame.mean(axis=0)
        top = 0
        while top < height and row_mean[top] <= black_level:
            top += 1
        bottom = 0
        while bottom < height and row_mean[height - 1 - bottom] <= black_level:
            bottom += 1
        left = 0
        while left < width and col_mean[left] <= black_level:
            left += 1
        right = 0
        while right < width and col_mean[width - 1 - right] <= black_level:
            right += 1
        tops.append(top)
        bottoms.append(bottom)
        lefts.append(left)
        rights.append(right)

    top, bottom = min(tops), min(bottoms)
    left, right = min(lefts), min(rights)
    if top + bottom >= gray_frames[0].shape[0] or left + right >= gray_frames[0].shape[1]:
        return None
    if max(top, bottom) / gray_frames[0].shape[0] > BLACK_BORDER_MAX_RATIO:
        return None
    if max(left, right) / gray_frames[0].shape[1] > BLACK_BORDER_MAX_RATIO:
        return None
    cropped_height = gray_frames[0].shape[0] - top - bottom
    cropped_width = gray_frames[0].shape[1] - left - right
    keep_ratio = (cropped_height * cropped_width) / float(
        gray_frames[0].shape[0] * gray_frames[0].shape[1])
    if keep_ratio < MIN_KEEP_AREA_RATIO:
        return None
    if top == bottom == left == right == 0:
        return None
    return top, bottom, left, right


def safe_component(name: str, fallback: str = "sample") -> str:
    """把 sample_id 映射为安全的单层目录名（不改变可追溯性，仅替换非法字符）。"""
    if not isinstance(name, str) or not name.strip():
        return fallback
    value = _UNSAFE_COMPONENT.sub("_", name.strip()).strip("._")
    if not value:
        return fallback
    return value[:60]


# --------------------------------------------------------------------------- #
# 抽帧主流程
# --------------------------------------------------------------------------- #

def _open_capture(source: Path) -> Tuple["cv2.VideoCapture", Optional[Path], bool]:
    """打开视频容器；非 ASCII 路径打不开时复制到 ASCII 临时目录再打开。

    返回 ``(capture, 待清理的临时目录, 是否走了回退)``。
    """
    capture = cv2.VideoCapture(str(source))
    if capture.isOpened():
        return capture, None, False
    capture.release()
    try:
        str(source).encode("ascii")
    except UnicodeEncodeError:
        pass
    else:
        raise MediaUnreadableError("OpenCV 无法打开视频容器：%s" % source.name)

    temp_dir = Path(tempfile.mkdtemp(prefix="vkf_ascii_"))
    fallback = temp_dir / ("input" + (source.suffix or ".mp4"))
    shutil.copyfile(source, fallback)
    capture = cv2.VideoCapture(str(fallback))
    if not capture.isOpened():
        capture.release()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise MediaUnreadableError("OpenCV 无法打开视频容器：%s" % source.name)
    return capture, temp_dir, True


def _read_metadata(capture: "cv2.VideoCapture",
                   max_duration_seconds: float) -> Dict[str, float]:
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
    width = float(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0)
    height = float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0)
    if not math.isfinite(fps) or fps <= 0:
        raise VideoMetadataError("invalid_fps：容器报告的 FPS 不是正有限值")
    if not math.isfinite(frame_count) or frame_count <= 0:
        raise VideoMetadataError("invalid_frame_count：容器报告的总帧数不是正有限值")
    duration = frame_count / fps
    if not math.isfinite(duration) or duration <= 0:
        raise VideoMetadataError("invalid_duration：由帧数与 FPS 推导的时长不是正有限值")
    if duration > float(max_duration_seconds):
        raise ResourceLimitError(
            "duration=%.3f 秒超过 max_duration_seconds=%.3f 秒"
            % (duration, float(max_duration_seconds)))
    return {"fps": fps, "frame_count": frame_count, "duration": duration,
            "width": width, "height": height}


def _decode_selected(capture: "cv2.VideoCapture", indices: Sequence[int],
                     ) -> Tuple[List[Dict[str, Any]], List[int]]:
    """按索引解码所选帧，同时读取容器给出的解码时间戳（PTS）。

    返回 ``(成功帧列表, 解码失败帧索引列表)``；失败帧不会被静默跳过。
    """
    decoded: List[Dict[str, Any]] = []
    failed: List[int] = []
    for index in indices:
        try:
            positioned = bool(capture.set(cv2.CAP_PROP_POS_FRAMES, float(index)))
        except Exception:  # noqa: BLE001 - 个别后端设置失败按解码失败计
            positioned = False
        if not positioned:
            failed.append(index)
            continue
        ok, frame = capture.read()
        if not ok or frame is None:
            failed.append(index)
            continue
        try:
            pts_ms = float(capture.get(cv2.CAP_PROP_POS_MSEC))
        except Exception:  # noqa: BLE001
            pts_ms = float("nan")
        decoded.append({"frame_index": index, "frame": frame,
                        "pts_seconds": pts_ms / 1000.0 if math.isfinite(pts_ms) else None})
    return decoded, failed


def _assess_timestamps(decoded: Sequence[Dict[str, Any]], fps: float,
                       policy: str) -> Dict[str, Any]:
    """判定时间戳来源：CFR 用索引/FPS，VFR 用解码 PTS，无可靠时间戳按策略拒绝。"""
    deviations: List[float] = []
    outliers: List[int] = []
    usable = True
    for item in decoded:
        pts = item["pts_seconds"]
        if pts is None or not math.isfinite(pts):
            usable = False
            continue
        if item["frame_index"] > 0 and pts <= 0:
            usable = False
            continue
        expected = item["frame_index"] / fps
        deviation = abs(pts - expected)
        deviations.append(deviation)
        if deviation > VFR_PTS_TOLERANCE_SECONDS:
            outliers.append(item["frame_index"])
    if not usable:
        raise UnsupportedVFRError(
            "unreliable_pts：容器未给出可用的解码时间戳，无法验证帧率类型，"
            "不能用零值或猜测时间填充")
    if len(outliers) >= VFR_MIN_OUTLIER_FRAMES:
        if policy != "use_pts":
            raise UnsupportedVFRError(
                "vfr_unsupported：%d 帧的解码时间戳偏离 CFR 期望超过 %.3f 秒（最大 %.3f 秒），"
                "unsupported_vfr_policy=%s"
                % (len(outliers), VFR_PTS_TOLERANCE_SECONDS, max(deviations), policy))
        return {"timestamp_source": "decoded_pts", "vfr_detected": True,
                "max_pts_deviation": round(max(deviations), 4),
                "outlier_frames": len(outliers), "pts_reliable": True}
    return {"timestamp_source": "cfr_index_over_fps", "vfr_detected": False,
            "max_pts_deviation": round(max(deviations), 4) if deviations else None,
            "outlier_frames": len(outliers), "pts_reliable": True}


def _timestamps_for(decoded: Sequence[Dict[str, Any]], assessment: Dict[str, Any],
                    fps: float) -> List[float]:
    values: List[float] = []
    for item in decoded:
        if assessment["timestamp_source"] == "decoded_pts":
            values.append(float(item["pts_seconds"]))
        else:
            values.append(item["frame_index"] / fps)
    for previous, current in zip(values, values[1:]):
        if current <= previous:
            raise UnsupportedVFRError(
                "unreliable_pts：解码时间戳不是严格递增（%.3f -> %.3f），"
                "不能给出可复现时间信息" % (previous, current))
    return values


def _save_frame(frame: np.ndarray, target: Path) -> Tuple[int, int]:
    """写帧文件并用 Pillow 重新打开验证；返回实际 (width, height)。

    用 ``cv2.imencode`` + Python 文件 IO 落盘：``cv2.imwrite`` 在 Windows 上遇到
    非 ASCII（中文）路径会静默失败，而运行目录路径可能含中文。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    except Exception as exc:  # noqa: BLE001 - 编码器异常按保存失败处理
        raise FrameSaveError("帧编码失败：%s（%s）" % (target.name, type(exc).__name__)) from exc
    if not ok:
        raise FrameSaveError("帧编码失败：%s" % target.name)
    with open(target, "wb") as stream:
        stream.write(buffer.tobytes())
    try:
        from PIL import Image

        with Image.open(target) as image:
            image.load()
            size = image.size
    except FrameSaveError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow 缺失或图片损坏都算保存失败
        raise FrameSaveError("帧文件无法重新打开：%s（%s）" % (target.name, type(exc).__name__)) from exc
    if size[0] <= 0 or size[1] <= 0:
        raise FrameSaveError("帧文件尺寸非法：%s" % target.name)
    return int(size[0]), int(size[1])


def _ffmpeg_executable(config):
    executable = config.get('ffmpeg_path') or shutil.which('ffmpeg')
    if not executable:
        try:
            import imageio_ffmpeg
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise MediaUnreadableError('FFmpeg依赖缺失，不退回OpenCV解码') from exc
    return str(executable)


def extract_frames(source: Path, output_dir: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    """表1 #6：FFmpeg按秒抽帧，保留真实PTS，每段最多8帧。"""
    from PIL import Image
    source=Path(source)
    if not source.is_file() or not source.stat().st_size:
        raise MediaUnreadableError('视频不存在或为空')
    rate=float(config.get('sample_fps',1))
    limit=int(config.get('max_frames',8))
    if not 1 <= rate <= 2 or not 1 <= limit <= 8 or config.get('crop_black_borders',False):
        raise ValueError('表1只允许每秒1到2帧、每段1到8帧；不裁边')
    folder=safe_component(str(config.get('sample_id','')), 'sample')
    dest=Path(output_dir)/FRAMES_DIR_NAME/folder
    dest.mkdir(parents=True,exist_ok=True)
    # select以输入t选择帧，showinfo保留原容器PTS；不制造CFR时间戳。
    filt="select='isnan(prev_selected_t)+gte(t-prev_selected_t,%s)',showinfo" % (1/rate)
    cmd=[_ffmpeg_executable(config), '-hide_banner','-nostdin','-y','-i',str(source),
         '-an','-vf',filt,'-fps_mode','vfr','-frames:v',str(limit),str(dest/'frame_%06d.jpg')]
    try:
        proc=subprocess.run(cmd,capture_output=True,timeout=float(config.get('probe_timeout_seconds',15))*4)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise MediaUnreadableError('FFmpeg不可用或解码超时') from exc
    log=proc.stderr.decode('utf-8',errors='replace')
    if proc.returncode:
        raise MediaUnreadableError('FFmpeg解码失败: '+log[-1200:])
    pts=[float(v) for v in re.findall(r'\bn:\s*\d+.*?pts_time:([0-9.eE+-]+)',log)]
    paths=sorted(dest.glob('frame_*.jpg'))
    if not paths or len(pts)<len(paths):
        raise MediaUnreadableError('未抽出有效帧或没有真实时间戳')
    frames=[]
    for index,(path,timestamp) in enumerate(zip(paths,pts)):
        with Image.open(path) as image:
            image.load();width,height=image.size
        frames.append({'path':path.relative_to(output_dir).as_posix(),'timestamp':timestamp,
                       'frame_index':index,'frame_index_source':'selected_sequence',
                       'width':width,'height':height})
    duration_match=re.search(r'Duration: (\d+):(\d+):([\d.]+)',log)
    if not duration_match:
        raise VideoMetadataError('FFmpeg未给出可复核时长')
    hours,minutes,seconds=map(float,duration_match.groups())
    duration=hours*3600+minutes*60+seconds
    fps_match=re.search(r'(\d+(?:\.\d+)?) fps',log)
    fps=float(fps_match.group(1)) if fps_match else None
    return {'frames':frames,'duration':duration,'fps':fps,'frame_count':None,
            'width':frames[0]['width'],'height':frames[0]['height'],
            'planned_frames':len(frames),'decoded_frames':len(frames),'timestamp_source':'ffmpeg_input_pts',
            'vfr_detected':None,'max_pts_deviation':None,'pts_reliable':True,
            'sample_fps':rate,'crop':None,'probe':{'backend':'ffmpeg','available':True},
            'warnings':[], 'decoder_backend':'ffmpeg'}
