"""算子 #2 数据初筛：图文关键词匹配、视频音频时长匹配。

只排除明显无关的数据组合，不承担最终语义一致性判断。
"""
from __future__ import annotations
import argparse
import re
import sys
import wave
import json
import math
import shutil
import os
from pathlib import Path

def _stage_output_name():
    """本阶段输出目录名：<NN>_<算子目录名>（编号读包根 map.json）。"""
    import json as _json
    here = Path(__file__).resolve().parent
    try:
        operators = _json.loads((here.parent / "map.json").read_text(encoding="utf-8"))["operators"]
        for item in operators.values():
            if item.get("package") == here.name:
                return "%02d_%s" % (int(item["formalOperatorNumber"]), here.name)
    except Exception:
        pass
    return here.name


def publish_stage_output(run_dir):
    """[2026-10-24] 设置 CM_RESULTS_ROOT 时，把本阶段最终产物直接发布到
    <root>/<NN>_<算子目录>/：JSONL/JSON（入口、台账、统计）+ 内容有变化的媒体；
    与输入完全相同（按内容哈希判定）的透传媒体不复制。未设置时行为不变。

    输入根识别兼容两种任务目录布局：`<task>/input/`（同机暂存）与
    `<task>/` 根目录直接铺开（/uploadFile 解压，入口 JSONL 在任务根）。
    """
    import hashlib
    import os
    import shutil
    base = os.environ.get("CM_RESULTS_ROOT")
    if not base:
        return None
    run_dir = Path(run_dir)
    roots = []
    cursor = run_dir
    for _ in range(6):
        if (cursor / "input").is_dir():
            roots = [cursor / "input"]
            break
        if cursor != run_dir and any(cursor.glob("*.jsonl")):
            roots = [cursor]
            break
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    dest = Path(base) / _stage_output_name()
    tmp = dest.parent / (dest.name + ".publishing")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    def _iter_files(root):
        for candidate in root.rglob("*"):
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if candidate == run_dir or run_dir in candidate.parents:
                continue
            yield candidate

    skip_names = {"manifest.json", "config.snapshot.json", "run.log",
                  "lineage.json", "stage_lineage.json", "stage_context.json"}
    skip_suffixes = {".tmp", ".part", ".partial", ".lock"}
    candidates = []
    for source in sorted(run_dir.rglob("*")):
        try:
            if not source.is_file():
                continue
        except OSError:
            continue
        if source.name in skip_names or source.suffix.lower() in skip_suffixes:
            continue
        candidates.append(source)

    sizes = set()
    for source in candidates:
        if source.suffix.lower() not in (".jsonl", ".json"):
            try:
                sizes.add(source.stat().st_size)
            except OSError:
                pass
    input_digests = {}
    for root in roots:
        for candidate in _iter_files(root):
            try:
                if candidate.stat().st_size not in sizes:
                    continue
                input_digests.setdefault(candidate.stat().st_size, set()).add(
                    hashlib.sha256(candidate.read_bytes()).digest())
            except OSError:
                continue

    for source in candidates:
        if source.suffix.lower() not in (".jsonl", ".json"):
            try:
                digests = input_digests.get(source.stat().st_size)
                if digests and hashlib.sha256(source.read_bytes()).digest() in digests:
                    continue
            except OSError:
                pass
        target = tmp / source.relative_to(run_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return dest

import cv2
import jieba
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils.logger import log
from utils.io import read_jsonl, write_jsonl, task_dir
# [v4.2] 血缘输出已停用：build_lineage/append_lineage 不再导入与调用（utils/lineage.py 保留存档）；
# now_iso 仅为时间工具，继续保留。
from utils.lineage import now_iso

BASE_DIR = Path(__file__).resolve().parent
OPERATOR = "data_prescreen"
VERSION = "v1.1.0"
CJK = re.compile(r"[一-鿿]")


def load_config(config_path: str | None = None, overrides: dict | None = None) -> dict:
    path = Path(config_path) if config_path else BASE_DIR / "configs" / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if overrides is not None:
        if not isinstance(overrides, dict) or set(overrides) - {'enabled', 'prescreen'}:
            raise ValueError('表1只允许总开关和时长秒差')
        cfg.update(overrides)
    cfg.setdefault('enabled', True)
    if not isinstance(cfg['enabled'], bool):
        raise ValueError('enabled 必须为布尔值')
    params = cfg.setdefault('prescreen', {})
    if set(params) - {'video_audio_max_duration_diff_seconds'}:
        raise ValueError('表1只允许配置音视频绝对秒差，禁止文件名/比例规则')
    seconds = params.setdefault('video_audio_max_duration_diff_seconds', 7.0)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 10:
        raise ValueError('时长差须为1到10秒')
    return cfg


def text_keywords(text: str) -> set:
    """文本关键词：jieba 分词，中文词长>=2、英文词长>=3。"""
    kws = set()
    for w in jieba.lcut(text or ""):
        w = w.strip().lower()
        if not w:
            continue
        if CJK.search(w):
            if len(w) >= 2:
                kws.add(w)
        elif len(w) >= 3:
            kws.add(w)
    return kws


def image_keywords(image_path: str, metadata: dict | None) -> set:
    """兼容旧调用签名；路径永不参与判断。只使用白名单描述。"""
    return text_keywords(_media_description({'metadata': metadata or {}}, 'image'))


def _media_description(rec: dict, modality: str) -> str:
    fields = ('title', 'alt', 'caption', 'description')
    obj = rec.get(modality)
    values = [obj.get(k) for k in fields] if isinstance(obj, dict) else []
    meta = rec.get('metadata') or {}
    if isinstance(meta, dict):
        values += [meta.get(modality + '_' + k) for k in fields]
    values += [rec.get(modality + '_' + k) for k in fields]
    return ' '.join(v for v in values if isinstance(v, str) and v.strip())


def _path(value):
    return value.get('path') if isinstance(value, dict) else value


def video_duration(path: Path) -> float | None:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    cap.release()
    return frames / fps if fps > 0 else None


def audio_duration(path: Path) -> float | None:
    """WAV 时长（stdlib）。其他格式需 ffmpeg/ffprobe，见 README 已知限制。"""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def prescreen_record(rec: dict, cfg: dict, media_root: Path) -> tuple[dict | None, str | None, dict]:
    """返回 (通过记录, 排除原因, 证据)。通过记录附带 prescreen 字段。"""
    sid = rec.get("sample_id")
    if not sid:
        return None, "missing_field: sample_id", {}
    if not cfg.get('enabled', True):
        # [v4.2] 输出记录 status 双字段：int status（判定型 1=保留）+ process_status 流程态
        return {**rec, 'prescreen': {'status': 1, 'process_status': 'disabled', 'rules': []}}, None, {}
    text = rec.get('text')
    if isinstance(text, dict):
        text = text.get('content') or text.get('original')
    image, video, audio = (_path(rec.get(k)) for k in ('image', 'video', 'audio'))
    if not any([text, image, video, audio]):
        return None, "no_modality", {}

    max_diff = cfg['prescreen']['video_audio_max_duration_diff_seconds']
    rules, evidence = [], {}

    for key, val in (("image", image), ("video", video), ("audio", audio)):
        if val and not (media_root / val).exists():
            return None, f"file_not_found: {val}", {}

    image_description = _media_description(rec, 'image')
    if text and image and image_description:
        tk, ik = text_keywords(text), text_keywords(image_description)
        matched = sorted(tk & ik)
        rules.append("text_image_keyword")
        evidence["matched_keywords"] = matched
        evidence['text_sources'] = ['text', 'image:title/alt/caption/description']
        if tk and ik and not matched:
            return None, 'no_common_words', evidence

    if video and audio:
        vd = rec.get('video', {}).get('duration') if isinstance(rec.get('video'), dict) else None
        ad = rec.get('audio', {}).get('duration') if isinstance(rec.get('audio'), dict) else None
        vd = vd if isinstance(vd, (int, float)) and math.isfinite(vd) else video_duration(media_root / video)
        ad = ad if isinstance(ad, (int, float)) and math.isfinite(ad) else audio_duration(media_root / audio)
        rules.append("video_audio_duration")
        if vd is None or ad is None:
            evidence['duration_status'] = 'insufficient_evidence'
        else:
            diff = abs(vd - ad)
            evidence.update({'video_duration': vd, 'audio_duration': ad, 'duration_diff_seconds': diff})
            if diff > max_diff:
                return None, f'duration_diff_seconds>{max_diff}', evidence

    if not rules:
        rules.append("no_applicable_rule")

    out = dict(rec)
    out["prescreen"] = {"rules": rules, **evidence}
    return out, None, evidence


def run(input_file: str, unique_id: str, config_path: str | None = None,
        output_root: str | None = None, overrides: dict | None = None) -> dict:
    cfg = load_config(config_path, overrides)
    started = now_iso()
    input_path = Path(input_file).resolve()
    out_root = Path(output_root) if output_root else BASE_DIR / cfg["io"]["output_root"]
    task = task_dir(out_root, unique_id)
    out_file = task / "02_screened.jsonl"
    rej_file = task / "02_rejects.jsonl"

    log("输入", f"加载输入文件：{input_path.name}")
    log('配置', '初筛已开启' if cfg['enabled'] else '初筛关闭，不执行')
    if not input_path.exists():
        log("失败", f"输入文件不存在：{input_path}")
        # [v4.2] 血缘输出已停用：失败路径不再构造/追加血缘记录，run() 返回也不再携带 lineage 键。
        # lineage = build_lineage(OPERATOR, VERSION, unique_id, str(input_path),
        #                         str(out_file), "failed", started, now_iso(),
        #                         cfg, {"error": "input_not_found"})
        # append_lineage(task, lineage)
        return {"status": "failed", "reason": "input_not_found"}

    media_root = input_path.parent
    kept, rejects, bad_lines = [], [], 0
    for lineno, rec, err in read_jsonl(input_path):
        if err:
            bad_lines += 1
            rejects.append({"line": lineno, "reason": err})
            continue
        out, reason, evidence = prescreen_record(rec, cfg, media_root)
        if reason:
            # [v4.2] 输出记录 status 双字段：判定型 0=过滤，原流程态写入 process_status
            rejects.append({"line": lineno, "sample_id": rec.get("sample_id"),
                            "status": 0, "process_status": "failed",
                            "reason": reason, 'record': rec,
                            'destination': '样本隔离库' if reason in ('no_common_words',) or reason.startswith('duration_diff_seconds') else '原始隔离区',
                            'tag': '样本-语义不通' if reason == 'no_common_words' or reason.startswith('duration_diff_seconds') else '原始文件-无法解析',
                            **({"evidence": evidence} if evidence else {})})
        else:
            # [v4.2] 输出记录 status 双字段：判定型 1=保留，原流程态写入 process_status
            out["status"] = 1
            out["process_status"] = "disabled" if not cfg.get('enabled', True) else "success"
            kept.append(out)

    write_jsonl(out_file, kept)
    write_jsonl(rej_file, rejects)
    # 本算子不解码/改写媒体；只搬运仍被放行记录引用的文件供下一环节使用。
    for rec in kept:
        for key in ('image', 'audio', 'video'):
            ref = _path(rec.get(key))
            if ref and (media_root / ref).is_file():
                target = task / ref
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(media_root / ref, target)
    stats = {"total": len(kept) + len(rejects), "kept": len(kept),
             "rejected": len(rejects), "bad_lines": bad_lines}
    log("读取", f"读取 {stats['total']} 条记录（坏行 {bad_lines} 条）")
    log("处理", f"初筛完成：保留 {len(kept)} 条，排除 {len(rejects)} 条")
    log("输出", f"写入输出文件：{out_file.name} / {rej_file.name}")

    status = "success"
    # [v4.2] 血缘输出已停用：不再构造/追加血缘记录，run() 返回不再携带 lineage 键。
    # lineage = build_lineage(OPERATOR, VERSION, unique_id, str(input_path),
    #                         str(out_file), status, started, now_iso(), cfg, stats)
    # append_lineage(task, lineage)
    (task/'run.log').write_text(('初筛已开启' if cfg['enabled'] else '初筛关闭，不执行')+'\n'+json.dumps(stats,ensure_ascii=False),encoding='utf-8')
    log("完成", f"算子执行完成，状态 {status}")
    return {"status": status, "outputFile": str(out_file),
            "rejectsFile": str(rej_file), "statistics": stats}


def main():
    ap = argparse.ArgumentParser(description="算子 #2 数据初筛")
    ap.add_argument("--input", required=True, help="跨模态样本清单 JSONL")
    ap.add_argument("--unique-id", required=True, help="任务id_文件id")
    ap.add_argument("--config", default=None)
    ap.add_argument("--output-root", default=None)
    args = ap.parse_args()
    result = run(args.input, args.unique_id, args.config, args.output_root)
    print(f"result: {result['status']} kept={result.get('statistics', {}).get('kept')}")
    sys.exit(0 if result["status"] != "failed" else 1)


if __name__ == "__main__":
    main()
