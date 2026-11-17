"""A passthrough route cannot report ZIP execution success after packaging fails."""
import io
import json
import zipfile

from fastapi.testclient import TestClient
import app as api


def test_missing_passthrough_media_returns_execution_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('OUTPUT_DIR', str(tmp_path / 'output'))
    row = {'sample_id': 'video1', 'video': {'path': 'media/absent.avi'},
           'text': {'content': 'two dogs are playing'}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'format': 'crossmodal-manifest-v1', 'entrypoint': 'records.jsonl'}))
        archive.writestr('records.jsonl', json.dumps(row) + '\n')
    payload = TestClient(api.app).post('/predict?detection=M13&uniqueId=zip_failure',
        files={'file': ('input.zip', buffer.getvalue(), 'application/zip')}).json()
    assert payload['status'] == 0
    assert 'package_error' in payload
    assert not isinstance(payload.get('outputFile'), dict)
