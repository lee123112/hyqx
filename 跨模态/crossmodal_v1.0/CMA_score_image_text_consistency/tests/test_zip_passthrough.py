"""A scoring route must preserve native candidates for the following routes."""
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as api


def video_pair():
    return {'sample_id': 'pair-video', 'pair_id': 'pair-video', 'modality_pair': ['text', 'video'],
            'left': {'sample_id': 'text1', 'modalities': ['text'],
                     'record': {'sample_id': 'text1', 'text': {'content': 'two puppies are playing'}}},
            'right': {'sample_id': 'video1', 'modalities': ['video'],
                      'record': {'sample_id': 'video1', 'video': {'path': 'media/video.avi',
                          'segments': [{'frames': [{'path': 'media/frame.jpg'}]}]}}}}


@pytest.mark.parametrize('pair_id', ['pair-video', ' pair-video '])
def test_native_video_pair_survives_image_scoring_zip(tmp_path, monkeypatch, pair_id):
    monkeypatch.setattr(api, 'OUTPUT_ROOT', tmp_path / 'output')
    monkeypatch.setattr(api.inference, 'build_encoder', lambda cfg: (None, 'unused_for_video', None))
    monkeypatch.setattr(api.inference, 'build_detector', lambda cfg: (None, None))
    original = video_pair()
    original['pair_id'] = pair_id
    original['right']['record']['timeline_file'] = 'media/timeline.json'
    original['right']['record']['local_annotations'] = [{'modality': ['audio', 'text'],
                                                        'labels': ['dog']}]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'format': 'crossmodal-manifest-v1', 'entrypoint': 'pairs.jsonl'}))
        archive.writestr('pairs.jsonl', json.dumps(original) + '\n')
        archive.writestr('media/video.avi', b'original-video')
        archive.writestr('media/frame.jpg', b'original-frame')
        archive.writestr('media/timeline.json', b'{"video":[],"audio":[],"text":[]}')
    response = TestClient(api.app).post('/predict?detection=M12&uniqueId=flow_video',
        files={'file': ('input.zip', buffer.getvalue(), 'application/zip')})
    payload = response.json()
    assert payload['status'] == 1
    with zipfile.ZipFile(payload['outputFile']['zip']) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        rows = [json.loads(line) for line in archive.read(manifest['entrypoint']).decode().splitlines()]
        assert len(rows) == 1
        assert rows[0]['pair_id'] == original['pair_id']
        assert rows[0]['left'] == original['left'] and rows[0]['right'] == original['right']
        assert rows[0]['processing']['12']['process_status'] == 'not_applicable'
        assert rows[0]['processing']['12']['route_enabled'] is False
        assert rows[0]['image_text_score']['clip_score'] is None
        assert archive.read('12_image_text_scores.jsonl') == b''
        assert archive.read('media/video.avi') == b'original-video'
        assert archive.read('media/frame.jpg') == b'original-frame'
        assert archive.read('media/timeline.json') == b'{"video":[],"audio":[],"text":[]}'


def test_disabled_image_route_preserves_video_pair_and_other_namespaces(tmp_path, monkeypatch):
    from utils import scoring
    import yaml
    cfg = yaml.safe_load((Path(api.__file__).parent / 'configs/config.yaml').read_text(encoding='utf-8'))
    cfg['image_text_scoring']['enabled'] = False
    pair = video_pair()
    pair['processing'] = {'11': {'process_status': 'candidate'}, '14': {'score': 0.31}}
    result = scoring.plan_scoring([(1, pair, None)], cfg, unique_id='flow_disabled',
                                 media_root=tmp_path, now='2026-11-11T00:00:00+08:00')
    assert len(result.pipeline) == 1
    assert result.pipeline[0]['processing']['11'] == pair['processing']['11']
    assert result.pipeline[0]['processing']['14'] == pair['processing']['14']
    assert '12' not in pair['processing']


def test_flow_lookup_trims_pair_id_without_rewriting_native_record():
    from utils.zip_flow import build_flow_records
    pair = video_pair()
    pair['pair_id'] = ' p '
    rows = build_flow_records([], [], [{'record': pair, 'reason': 'no_image'}], ['p'], True)
    assert rows[0]['pair_id'] == ' p '
    assert rows[0]['processing']['12']['route_enabled'] is False


def test_missing_description_remains_enabled_pending_for_route_reader():
    from utils.zip_flow import build_flow_records
    pair = video_pair()
    rows = build_flow_records([], [], [{'record': pair, 'reason': 'no_description'}],
                              [pair['pair_id']], True)
    assert rows[0]['processing']['12']['route_enabled'] is True
    assert rows[0]['image_text_score']['clip_score'] is None


@pytest.mark.parametrize('image_record', [
    {'image': {'relative_path': 'images/photo.png'}},
    {'images': [{'relative_path': 'images/photo.png'}]},
])
def test_relative_path_image_remains_pending_model_through_official_zip(
        tmp_path, monkeypatch, image_record):
    from conftest import png_bytes
    monkeypatch.setattr(api, 'OUTPUT_ROOT', tmp_path / 'output')
    monkeypatch.setattr(api.inference, 'build_encoder', lambda cfg: (None, 'model_unavailable', None))
    monkeypatch.setattr(api.inference, 'build_detector', lambda cfg: (None, None))
    pair = video_pair()
    pair['modality_pair'] = ['text', 'image']
    pair['right']['record'] = image_record
    buffer = io.BytesIO()
    pixels = png_bytes()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('pairs.jsonl', json.dumps(pair) + '\n')
        archive.writestr('images/photo.png', pixels)
    body = TestClient(api.app).post('/predict?detection=M12&uniqueId=relative_image',
        files={'file': ('input.zip', buffer.getvalue(), 'application/zip')}).json()
    assert body['status'] == 1, body
    with zipfile.ZipFile(body['outputFile']['zip']) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        row = json.loads(archive.read(manifest['entrypoint']).splitlines()[0])
        assert row['processing']['12'].get('route_enabled') is not False
        assert row['image_text_score']['process_status'] == 'pending_model'
        assert row['image_text_score']['clip_score'] is None
        assert row['right']['record'] == image_record
        assert archive.read('images/photo.png') == pixels
