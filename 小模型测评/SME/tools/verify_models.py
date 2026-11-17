"""Offline SHA-256 verification; only the Python standard library is needed."""
from pathlib import Path
import hashlib
import json

ROOT=Path(__file__).resolve().parents[1]


def main():
    checked=0;total=0
    for manifest in sorted(ROOT.glob('SME_EVAL_*/models/manifest.json')):
        for item in json.loads(manifest.read_text(encoding='utf-8'))['files']:
            path=(manifest.parent/item['path']).resolve()
            assert path.is_relative_to(manifest.parent.resolve()),item['path']
            assert path.is_file() and not path.is_symlink(),item['path']
            assert path.stat().st_size==item['bytes'],item['path']
            with path.open('rb') as f:
                assert hashlib.file_digest(f,'sha256').hexdigest()==item['sha256'],item['path']
            checked+=1;total+=item['bytes']
    assert checked==47,(checked,'Expected 47 model files')
    print(json.dumps({'status':'passed','modelFiles':checked,'bytes':total},ensure_ascii=False))


if __name__=='__main__':
    main()
