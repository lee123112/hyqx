"""Create portable artifact copies without changing executable task state.

Data referenced by an output is retained under ``inputs/`` in the output ZIP.
Model files remain dependencies of the operator package and are identified by
operator-model URIs, never copied into every result. Uploaded ZIPs are hashed
input evidence, not nested into the output bundle.
"""
from __future__ import annotations

import hashlib
import csv
import io
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
from typing import Any

from .config import settings

_PATH_KEYS = {
    'file', 'input', 'audio', 'uri', 'path', 'assetpath', 'sourcefile', 'filename',
    'oursdir', 'refdir', 'evaldir', 'ourscorpus', 'refcorpus', 'trainfile', 'evalfile',
    'oursmanifest', 'refmanifest', 'evalmanifest',
}
_MODEL_KEYS = {'modelpath', 'modeldir', 'modelweight', 'tokenizerpath'}
_TRAINING_PATH_KEYS = {
    'oursdir','refdir','evaldir','ourscorpus','refcorpus','trainfile','evalfile',
    'oursmanifest','refmanifest','evalmanifest',
}
_MEDIA_SUFFIXES = {'.png','.jpg','.jpeg','.webp','.bmp','.wav','.mp3','.flac',
                   '.mp4','.mov','.webm','.avi','.mkv'}
_CONTENT_KEYS = {
    'id', 'sampleid', 'title', 'scene', 'text', 'text1', 'text2', 'content',
    'sentence', 'sentence1', 'sentence2', 'output', 'answer', 'question',
    'instruction', 'caption', 'description', 'transcript', 'reference', 'hypothesis',
}
_PACKAGES = {
    'text': 'SME_EVAL_TEXT_001', 'image': 'SME_EVAL_IMAGE_001',
    'audio': 'SME_EVAL_AUDIO_001', 'video': 'SME_EVAL_VIDEO_001',
    'benchmark': 'SME_EVAL_BENCHMARK_001',
}
_HOST = re.compile(r'''[A-Za-z]:[\\/][^\s"'<>|;,\)\]\}]+|/(?:home|root|tmp|var|Users|mnt|app)(?:/[^\s"'<>|;,\)\]\}]+)?''')


class BundleExporter:
    def __init__(self, out_dir: Path, unique_id: str, type_: str, *, training_protocol: bool = False):
        self.out_dir = out_dir.resolve()
        self.unique_id = unique_id
        self.type = type_
        self.training_protocol = training_protocol
        self.input_root = (settings.workspace_dir/unique_id/'input').resolve()
        self.paths: dict[Path, str] = {self.out_dir: '.'}
        self.copied: dict[str, Path] = {}
        if self.input_root.is_dir():
            self._copy(self.input_root, 'inputs')

    def _model(self, path: Path, key: str) -> str | None:
        for package in _PACKAGES.values():
            root = (settings.project_root/package/'models').resolve()
            if path.is_relative_to(root):
                return 'operator-model://'+package+'/'+path.relative_to(root).as_posix()
        # Historical explicit model overrides still must not expose their host.
        old = (settings.project_root/'weight').resolve()
        if path.is_relative_to(old):
            rel = path.relative_to(old)
            first = rel.parts[0] if rel.parts else path.name
            kind = ('text' if first.startswith('bert') else 'audio' if
                    first.startswith(('whisper','paraformer')) else 'image' if
                    first.startswith(('resnet','mobilenet','deit')) else self.type)
            return 'operator-model://'+_PACKAGES[kind]+'/'+rel.as_posix()
        if key in _MODEL_KEYS:
            return 'operator-model://'+_PACKAGES[self.type]+'/'+path.name
        return None

    def _copy(self, source: Path, relative: str) -> None:
        source = source.resolve()
        if self._model(source,''):
            return
        target = self.out_dir/relative
        if source == self.out_dir or source.is_relative_to(self.out_dir):
            return
        self.paths[source] = relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in sorted(source.iterdir()):
                if not child.is_symlink():
                    self._copy(child, relative+'/'+child.name)
        elif source.is_file() and source.suffix.lower() != '.zip':
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            self.copied[relative] = source

    def _path(self, path: Path, key: str) -> str:
        path = path.resolve()
        model = self._model(path, key)
        if model:
            return model
        if path.suffix.lower() == '.zip' and path.is_relative_to(self.input_root):
            return 'input-archive://'+path.name
        for source, relative in sorted(self.paths.items(), key=lambda item:len(str(item[0])), reverse=True):
            if path == source:
                return relative
            if source.is_dir() and path.is_relative_to(source):
                tail = path.relative_to(source).as_posix()
                return tail if relative == '.' else relative+'/'+tail
        allowed = (settings.workspace_dir, settings.data_dir, settings.dataset_dir, settings.project_root.parent)
        external_training = self.training_protocol and key in _TRAINING_PATH_KEYS
        external_media = key in _PATH_KEYS and path.is_file() and path.suffix.lower() in _MEDIA_SUFFIXES
        if ((external_training or external_media) and path.exists() and not path.is_symlink()
                and any(path.is_relative_to(root.resolve()) for root in allowed)):
            token = hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]
            relative = 'inputs/external/'+token+'/'+path.name
            self._copy(path, relative)
            return relative
        return '<host-path>/'+path.name

    def text(self, value: str, key: str = '') -> str:
        key = key.replace('_','').lower()
        if key in _CONTENT_KEYS or (key == 'input' and self.type in ('text','benchmark')):
            return value
        if key == 'input' and not value.startswith('/workspace/'):
            # A media input can also be descriptive user content; only rewrite
            # an actual existing asset, not any string shaped like a path.
            if not Path(value).is_absolute() or not Path(value).exists():
                return value
        if value.startswith(('http://','https://','operator-model://','input-archive://')):
            return value
        if value.startswith('/workspace/'):
            raw = settings.workspace_dir/value[len('/workspace/'):]
            return self._path(raw, key)
        if value.startswith('/api/'):
            return value
        if Path(value).is_absolute():
            return self._path(Path(value), key)
        if PureWindowsPath(value).is_absolute():
            # Foreign Windows paths on a POSIX deployment cannot be resolved.
            return '<host-path>/'+PureWindowsPath(value).name
        for source, relative in sorted(self.paths.items(), key=lambda item:len(str(item[0])), reverse=True):
            for spelling in {str(source), source.as_posix()}:
                value = value.replace(spelling, relative)
        for package in _PACKAGES.values():
            root = settings.project_root/package/'models'
            for spelling in {str(root), root.as_posix()}:
                value = value.replace(spelling, 'operator-model://'+package)
        for root in (settings.workspace_dir, settings.data_dir, settings.project_root, settings.dataset_dir):
            for spelling in {str(root), root.as_posix()}:
                value = value.replace(spelling, '<host-path>')
        return _HOST.sub('<host-path>', value)

    def convert(self, value: Any, key: str = '', user_content: bool = False) -> Any:
        normalized_key = key.replace('_','').lower()
        user_content = user_content or normalized_key in ('samples','trainingsamples','parameters')
        if isinstance(value, Path):
            return self._path(value, key.replace('_','').lower())
        if isinstance(value, str):
            if user_content and normalized_key not in (_PATH_KEYS | _MODEL_KEYS):
                return value
            return self.text(value, key)
        if isinstance(value, dict):
            # Sample IDs can be arbitrary keys in per-ID diagnostic mappings.
            return {k:self.convert(v,str(k),user_content) for k,v in value.items()}
        if isinstance(value, (list,tuple)):
            return [self.convert(v,key,user_content) for v in value]
        return value

    def _input_copy(self, value: Any) -> Any:
        """Change only explicit execution references in retained input data."""
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized_key = str(key).replace('_','').lower()
                if normalized_key in (_PATH_KEYS | _MODEL_KEYS) and isinstance(item, str):
                    result[key] = self.text(item,str(key))
                elif isinstance(item, (dict,list)):
                    result[key] = self._input_copy(item)
                else:
                    result[key] = item
            return result
        if isinstance(value, list):
            return [self._input_copy(item) for item in value]
        return value

    def _finish_inputs(self) -> None:
        completed = set()
        while pending := set(self.copied)-completed:
            for relative in sorted(pending):
                path = self.out_dir/relative
                completed.add(relative)
                if path.suffix.lower() not in ('.json','.jsonl','.ndjson','.csv'):
                    continue
                try:
                    text = path.read_text(encoding='utf-8-sig')
                    if path.suffix.lower() == '.json':
                        original = json.loads(text)
                        changed = self._input_copy(original)
                        if changed != original:
                            path.write_text(json.dumps(changed,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                    elif path.suffix.lower() in ('.jsonl','.ndjson'):
                        original = [json.loads(line) for line in text.splitlines() if line.strip()]
                        changed = self._input_copy(original)
                        if changed != original:
                            path.write_text(''.join(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n'
                                                    for row in changed),encoding='utf-8')
                    else:
                        reader = csv.DictReader(io.StringIO(text))
                        fields, original = reader.fieldnames, list(reader)
                        changed = self._input_copy(original)
                        if changed != original:
                            with path.open('w',encoding='utf-8',newline='') as handle:
                                writer = csv.DictWriter(handle,fieldnames=fields)
                                writer.writeheader(); writer.writerows(changed)
                except (ValueError, TypeError, UnicodeDecodeError):
                    pass
        files = []
        for relative, source in sorted(self.copied.items()):
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            exported = hashlib.sha256((self.out_dir/relative).read_bytes()).hexdigest()
            files.append({'path':relative,'originalSha256':original,'exportedSha256':exported,
                          'transformed':original != exported})
        (self.out_dir/'input_files.json').write_text(
            json.dumps({'files':files},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    def finish(self) -> None:
        """Rewrite artifact copies, including nested lineage and copied manifests."""
        for path in sorted(self.out_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in ('.json','.jsonl','.ndjson','.log'):
                continue
            try:
                text = path.read_text(encoding='utf-8-sig')
            except UnicodeDecodeError:
                continue
            try:
                if path.suffix.lower() == '.json':
                    text = json.dumps(self.convert(json.loads(text)),ensure_ascii=False,indent=2)+'\n'
                elif path.suffix.lower() in ('.jsonl','.ndjson'):
                    text = ''.join(json.dumps(self.convert(json.loads(line)),ensure_ascii=False,separators=(',',':'))+'\n'
                                   for line in text.splitlines() if line.strip())
                else:
                    text = self.text(text)
            except (ValueError, TypeError):
                text = self.text(text)
            path.write_text(text,encoding='utf-8')
        self._finish_inputs()
        # Serialization above changes bytes (and sometimes metadata paths).
        # Freeze output evidence only after those changes have completed.
        for path in self.out_dir.glob('lineage_*.jsonl'):
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
            for row in rows:
                if row.get('kind') != 'output' or not isinstance(row.get('path'), str):
                    continue
                target = (self.out_dir/row['path']).resolve()
                if target.is_relative_to(self.out_dir) and target.is_file() and target != path:
                    row.update(size=target.stat().st_size,
                               sha256=hashlib.sha256(target.read_bytes()).hexdigest())
            path.write_text(''.join(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n'
                                    for row in rows),encoding='utf-8')

