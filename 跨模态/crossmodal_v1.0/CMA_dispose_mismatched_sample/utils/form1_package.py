# -*- coding: utf-8 -*-
"""[v4.3] 形态一（``/predict?detection=``）压缩包模式的输出引用辅助。

背景：业务平台只调用 ``POST /predict?detection=<算子标识>``，以 multipart
``file`` 字段上传一个压缩包（入口 JSONL + 媒体 + 可选 ``manifest.json``）。
形态一响应为 JSON，除 ``status``/业务指标外，还需要给出**输出产物的容器内路径**
（``outputFile`` 路径引用），平台按该路径取产物；同时把输出目录打包成一个
``*_form1.zip``，可直接作为下一个算子形态一的 ``file`` 输入。

本模块只依赖标准库，按算子自包含复制到各算子 ``utils/form1_package.py``。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional

REFS_FORMAT = "crossmodal-files-v1"
MANIFEST_FORMAT = "crossmodal-manifest-v1"
MANIFEST_NAME = "manifest.json"

#: 不进入输出包/引用的目录名（任务输入目录、缓存目录）
EXCLUDE_DIRS = {"input", "__pycache__", ".git", ".zip_staging", "_form1_bundle"}
#: 不进入输出包/引用的文件名（运行日志、配置快照、锁文件、临时包）
EXCLUDE_NAMES = {
    "run.log", "config.snapshot.json", "task.lock", ".lock",
    "transport_limits.patch.json",
}
#: 运行日志统一不进包（各算子日志名可能是 run001.log / 14_run.log 等）
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".lock", ".tmp", ".log"}


def _is_excluded(relative: str, explicit_media=()) -> bool:
    parts = Path(relative).parts
    if any(part in EXCLUDE_DIRS and not (part == 'input' and relative in explicit_media)
           for part in parts):
        return True
    name = Path(relative).name
    if name in EXCLUDE_NAMES:
        return True
    if name.endswith("_form1.zip"):
        return True
    if Path(relative).suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def _relative(value: Any, root: Path) -> Optional[str]:
    """把入口提示统一成相对 root 的 posix 路径；不在 root 内则返回 None。"""
    if not isinstance(value, (str, Path)):
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            return None
    # 先按原样相对 root 处理，再按文件名在 root 下搜索
    direct = (root / candidate)
    if direct.is_file():
        return candidate.as_posix()
    matches = sorted(path for path in root.rglob(candidate.name) if path.is_file())
    if len(matches) == 1:
        return matches[0].relative_to(root).as_posix()
    return None


def _guess_entrypoint(files: Iterable[str], root: Path) -> Optional[str]:
    files = list(files)
    top_jsonl = [item for item in files
                 if "/" not in item and item.lower().endswith((".jsonl", ".json"))]
    if len(top_jsonl) == 1:
        return top_jsonl[0]
    # 多入口时优先主清单命名（各算子 MAIN_NAME 常见形态），否则取字典序第一个 JSONL
    for item in top_jsonl:
        lowered = item.lower()
        if any(token in lowered for token in
               ("screened", "cleaned", "index", "candidates", "pairs", "scored",
                "annotated", "confidence", "quality", "disposed", "report",
                "result", "output", "main")):
            return item
    if top_jsonl:
        return sorted(top_jsonl)[0]
    if files:
        return sorted(files)[0]
    return None


def _merge_manifest(root: Path, files: list, entrypoint: Optional[str]) -> Dict[str, Any]:
    manifest: Dict[str, Any] = {}
    path = root / MANIFEST_NAME
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, ValueError):
            manifest = {}
    manifest["format"] = MANIFEST_FORMAT
    if entrypoint:
        manifest["entrypoint"] = entrypoint
    manifest["files"] = list(files)
    if MANIFEST_NAME not in manifest["files"]:
        manifest["files"].append(MANIFEST_NAME)
    return manifest


def active_media_refs(record: Any, *, strict: bool = True) -> list:
    """Current business references only; provenance and model diagnostics are not files to carry."""
    found = set()

    def add(value):
        if not isinstance(value, str) or not value.strip():
            return
        value = value.strip().replace('\\', '/')
        path = PurePosixPath(value)
        if ':' in value or path.is_absolute() or '..' in path.parts:
            if strict:
                raise ValueError('不支持的活动媒体路径（media_path_unsupported）：%s' % value)
            return
        normalized = path.as_posix()
        if normalized not in ('', '.'):
            found.add(normalized)

    def media(value):
        if isinstance(value, str):
            add(value)
        elif isinstance(value, dict):
            for key in ('path', 'relative_path', 'file_path', 'file', 'uri', 'segment_path', 'frame_path',
                        'image_path', 'audio_path', 'video_path'):
                add(value.get(key))
            # Only schema media containers may contain paths. Lists of modality,
            # label, keyword or provenance strings are ordinary business data.
            for key in ('image', 'images', 'audio', 'video', 'media', 'image_reference',
                        'audio_segment', 'video_reference', 'segments', 'frames',
                        'local_annotations', 'regions', 'paragraphs'):
                media(value.get(key))
        elif isinstance(value, list):
            for child in value:
                media(child)

    def visit(value):
        if not isinstance(value, dict):
            return
        for key in ('image_path', 'audio_path', 'video_path', 'timeline_file'):
            add(value.get(key))
        for key in ('image', 'images', 'audio', 'video', 'media', 'image_reference', 'audio_segment',
                    'video_reference', 'local_annotations', 'regions'):
            media(value.get(key))
        transcription = value.get('transcription')
        if isinstance(transcription, dict):
            media(transcription.get('paragraphs'))
        for key in ('left', 'right', 'record'):
            visit(value.get(key))

    visit(record)
    return sorted(found)


def _current_input_roots(root: Path) -> list:
    # Current task input only. Never search prior runs, other tasks or the deployment directory.
    for parent in [root] + list(root.parents)[:2]:
        source = parent / 'input'
        if source.is_dir():
            candidates = [source / 'content', source]
            return [candidate.resolve() for candidate in candidates if candidate.is_dir()]
    return []


def _jsonl_records(path: Path):
    with path.open('r', encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row


def _publish_media(source, target: Path) -> None:
    """A failed read/CRC check must never leave a file that a retry can reuse."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as sink:
            shutil.copyfileobj(source, sink)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _carry_current_media(root: Path, entrypoint: Optional[str], required_media: bool = True) -> tuple:
    required = set()
    referenced = set()
    for path in sorted(root.rglob('*.jsonl')):
        relative = path.relative_to(root).as_posix()
        if _is_excluded(relative):
            continue
        refs = set()
        for record in _jsonl_records(path):
            refs.update(active_media_refs(record, strict=required_media and relative == entrypoint))
        referenced.update(refs)
        if relative == entrypoint:
            required.update(refs)
    sources = _current_input_roots(root)
    archives = sorted({path.resolve() for source in sources for path in source.glob('*.zip')
                       if zipfile.is_zipfile(path)})
    missing = []
    copied_archive_bytes = 0
    for relative in sorted(referenced, key=lambda item: (item not in required, item)):
        hard_required = required_media and relative in required
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError('媒体输出路径越界：%s' % relative)
        if target.is_file():
            continue
        for source_root in sources:
            candidate = (source_root / relative).resolve()
            try:
                candidate.relative_to(source_root)
            except ValueError:
                continue
            if candidate.is_file():
                try:
                    with candidate.open('rb') as source:
                        _publish_media(source, target)
                    break
                except OSError:
                    if hard_required:
                        raise
        if not target.is_file():
            # Some system operators keep the original delivery archive intact.
            # Read only that current task's input ZIP, never historical task/run directories.
            try:
                if len(archives) > 1:
                    raise ValueError('当前输入归档身份歧义（ambiguous_input_archives）')
                if archives:
                    with zipfile.ZipFile(archives[0], 'r') as archive:
                        members = [info for info in archive.infolist() if info.filename in referenced]
                        budget_members = [info for info in members if info.filename in required]
                        limit = int(os.getenv('MAX_UNPACKED_BYTES', str(4 * 1024 ** 3)))
                        count_limit = int(os.getenv('MAX_UPLOAD_FILES', '2000'))
                        if (sum(info.file_size for info in budget_members) > limit
                                or len(budget_members) > count_limit):
                            raise ValueError('输入归档媒体总量超出限制（archive_media_limit）')
                        matches = [info for info in members if info.filename == relative]
                        if len(matches) > 1:
                            raise ValueError('输入归档媒体重名（ambiguous_archive_member）：%s' % relative)
                        if matches:
                            member = matches[0]
                            if member.is_dir() or copied_archive_bytes + member.file_size > limit:
                                raise ValueError('输入归档媒体超出限制或不是文件：%s' % relative)
                            with archive.open(member) as source:
                                _publish_media(source, target)
                            copied_archive_bytes += member.file_size
            except (OSError, ValueError, zipfile.BadZipFile):
                if hard_required:
                    raise
        if not target.is_file() and relative in required:
            missing.append(relative)
    if missing and required_media:
        raise ValueError('串联包缺少活动媒体（media_missing）：%s' % ', '.join(missing[:8]))
    protected = [relative for relative in required if _is_excluded(relative, referenced)]
    if protected:
        raise ValueError('活动媒体使用受保护的输出文件名：%s' % ', '.join(sorted(protected)))
    return missing, referenced


def build_reference(run_root: Any, entrypoint_hint: Any = None,
                    zip_path: Any = None, *, media_policy: str = 'required') -> Dict[str, Any]:
    """构建形态一输出路径引用，并生成可直传下一算子的 ``*_form1.zip``。

    参数
    ----
    run_root: 算子本次运行的输出根目录（``OUTPUT_DIR/<uniqueId>/.../runNNN``
        或任务根目录）。
    entrypoint_hint: 主入口文件名/绝对路径；缺省时读 ``manifest.json`` 或猜测。
    zip_path: 压缩包输出路径；缺省为 ``run_root 同级/<run_root.name>_form1.zip``。
    """
    root = Path(run_root).expanduser().resolve()
    if media_policy not in ('required', 'reference'):
        raise ValueError('未知媒体策略：%s' % media_policy)
    if not root.is_dir():
        raise FileNotFoundError("输出目录不存在：%s" % root)

    hint = _relative(entrypoint_hint, root)
    if hint is None:
        existing = root / MANIFEST_NAME
        if existing.is_file():
            metadata = json.loads(existing.read_text(encoding='utf-8'))
            hint = _relative(metadata.get('entrypoint'), root)
    if hint is None:
        hint = _guess_entrypoint([
            path.relative_to(root).as_posix() for path in sorted(root.rglob('*'))
            if path.is_file() and not _is_excluded(path.relative_to(root).as_posix())
            and path.name != MANIFEST_NAME], root)
    missing_media, explicit_media = _carry_current_media(root, hint, required_media=media_policy == 'required')

    files: list[str] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _is_excluded(relative, explicit_media):
            continue
        files.append(relative)
        total_bytes += path.stat().st_size

    entrypoint = _relative(entrypoint_hint, root)
    if entrypoint is None:
        entrypoint = _relative(_merge_manifest(root, files, None).get("entrypoint"), root)
    if entrypoint is None:
        entrypoint = _guess_entrypoint(files, root)

    manifest = _merge_manifest(root, files, entrypoint)
    if media_policy == 'reference':
        # Export/review rows contain links, not claims that the original media was revalidated.
        manifest['media_policy'] = media_policy
        manifest['unavailable_media_links'] = missing_media

    target = Path(zip_path).expanduser().resolve() if zip_path else (
        root.parent / (root.name + "_form1.zip"))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=str(target.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative in files:
                if relative == MANIFEST_NAME:
                    continue
                archive.write(root / relative, relative)
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    zip_bytes = target.stat().st_size

    return {
        "format": REFS_FORMAT,
        "mode": "path-reference",
        "runDir": str(root),
        "entrypoint": entrypoint,
        "manifest": MANIFEST_NAME,
        "files": files + ([MANIFEST_NAME] if MANIFEST_NAME not in files else []),
        "fileCount": len(files) + (0 if MANIFEST_NAME in files else 1),
        "totalBytes": total_bytes,
        "zip": str(target),
        "zipBytes": zip_bytes,
        "missingMedia": missing_media,
    }


def package_payload(result: Dict[str, Any], reference: Dict[str, Any], *,
                    operator: str, unique_id: str,
                    business: Optional[Dict[str, Any]] = None,
                    status: Optional[int] = None) -> Dict[str, Any]:
    """组装形态一压缩包模式的统一 JSON 响应。

    响应首字段 ``status``：1=执行成功，0=执行失败；业务判定明细在
    ``result``/``process_status`` 中，平台按需读取。
    """
    result = result or {}
    failed = str(result.get("status") or "").lower() == "failed"
    payload: Dict[str, Any] = {
        "status": 0 if failed else (1 if status is None else int(status)),
        "process_status": result.get("status") or "success",
        "uniqueId": unique_id,
        "operator": operator,
        "outputFile": reference,
    }
    try:
        payload["result"] = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        payload["result"] = {"status": result.get("status")}
    if business:
        # 形态一原有业务字段放顶层；process_status/msg 等业务态允许覆盖运行态默认值，
        # 但 status/uniqueId/operator/outputFile/result 属于包模式契约字段，不允许被覆盖。
        reserved = {"status", "uniqueId", "operator", "outputFile", "result"}
        for key, value in business.items():
            if key in reserved:
                continue
            payload[key] = value
    return payload


#: 解压安全限额（可用环境变量调整；默认远大于正常阶段包）
MAX_ZIP_FILES = int(os.environ.get("FORM1_MAX_ZIP_FILES", "50000"))
MAX_UNPACKED_BYTES = int(os.environ.get("FORM1_MAX_UNPACKED_BYTES",
                                        str(8 * 1024 * 1024 * 1024)))


def extract_zip_into(content: bytes, input_dir: Path,
                     max_files: Optional[int] = None,
                     max_unpacked_bytes: Optional[int] = None) -> None:
    """把压缩包内容安全解压到 ``input_dir``。

    拒绝绝对路径/目录穿越/符号链接，并限制成员数与解压总字节数（防 zip 炸弹）。
    """
    import io
    import shutil
    import stat as stat_module

    file_limit = MAX_ZIP_FILES if max_files is None else int(max_files)
    byte_limit = (MAX_UNPACKED_BYTES if max_unpacked_bytes is None
                  else int(max_unpacked_bytes))
    staging = input_dir / ".zip_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    count = 0
    total_bytes = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            count += 1
            if count > file_limit:
                shutil.rmtree(staging, ignore_errors=True)
                raise ValueError("压缩包成员数超过上限 %d" % file_limit)
            total_bytes += int(member.file_size or 0)
            if total_bytes > byte_limit:
                shutil.rmtree(staging, ignore_errors=True)
                raise ValueError("压缩包解压后总字节数超过上限 %d" % byte_limit)
            mode = (member.external_attr >> 16) & 0o170000
            if mode == stat_module.S_IFLNK:
                raise ValueError("压缩包包含符号链接：%s" % name)
            normalized = Path(name)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise ValueError("压缩包包含非法路径：%s" % name)
            destination = staging / normalized
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)
    for child in sorted(staging.rglob("*")):
        if child.is_file():
            target = input_dir / child.relative_to(staging)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                continue
            shutil.move(str(child), str(target))
    shutil.rmtree(staging, ignore_errors=True)


def resolve_entrypoint(input_dir: Path) -> Path:
    """解析入口：优先 ``manifest.json`` 的 ``entrypoint``，否则唯一的顶层 JSONL。"""
    input_dir = Path(input_dir)
    manifest = input_dir / MANIFEST_NAME
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        entry = data.get("entrypoint") if isinstance(data, dict) else None
        if isinstance(entry, str) and entry.strip():
            candidate = (input_dir / entry.strip()).resolve()
            try:
                candidate.relative_to(input_dir.resolve())
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                return candidate
    jsonls = [path for path in input_dir.rglob("*.jsonl") if path.is_file()]
    if len(jsonls) == 1:
        return jsonls[0]
    # 多个 JSONL 且没有 manifest：顶层唯一 JSONL 优先，否则报错
    top = [path for path in input_dir.glob("*.jsonl") if path.is_file()]
    if len(top) == 1:
        return top[0]
    names = ", ".join(sorted(path.name for path in jsonls)) or "无"
    raise ValueError("压缩包内入口不唯一（%s），请在包内提供 manifest.json 的 entrypoint 字段" % names)
