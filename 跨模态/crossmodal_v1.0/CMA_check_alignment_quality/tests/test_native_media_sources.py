"""Native media aliases and per-modality upstream metric provenance."""
import copy
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as api
from tests.conftest import png_bytes, write_png, write_wav
from tests.test_zip_pair_input import native_pair
from utils import alignment_qc as qc
from utils import upstream_metrics as um
from utils.media_carry import MediaCarrier


IMAGE_RECORDS = [
    {'image': {'file_path': 'media/pair.png'}},
    {'image': {'uri': 'media/pair.png'}},
    {'image': {'relative_path': 'media/pair.png'}},
    {'images': [{'relative_path': 'media/pair.png'}]},
    {'images': ['media/pair.png']},
    {'image_reference': {'path': 'media/pair.png'}},
    {'image_path': 'media/pair.png'},
    {'media': {'images': [{'file_path': 'media/pair.png'}]}},
    {'media': {'image': {'uri': 'media/pair.png'}}},
    {'image': {'file_path': 'media\\pair.png'}},
]


@pytest.mark.parametrize('image_record', IMAGE_RECORDS)
def test_native_image_aliases_reach_qc_through_public_zip(tmp_path, monkeypatch, image_record):
    monkeypatch.setenv('OUTPUT_DIR', str(tmp_path / 'output'))
    monkeypatch.setenv('MODEL_ROOT', str(tmp_path / 'no-models'))
    original = native_pair()
    original['right']['record'].pop('image')
    original['right']['record'].update(image_record)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('pairs.jsonl', json.dumps(original) + '\n')
        archive.writestr('media/pair.png', png_bytes())
    body = TestClient(api.app).post('/predict?detection=M20&uniqueId=alias_qc',
        files={'file': ('input.zip', buffer.getvalue(), 'application/zip')}).json()
    assert body['status'] == 1, body
    assert body['hard_failures'] == [], body
    with zipfile.ZipFile(body['outputFile']['zip']) as archive:
        row = json.loads(archive.read(body['outputFile']['entrypoint']).splitlines()[0])
        assert row['right'] == original['right']
        assert row['processing']['20']['checks'][qc.CHECK_FILE_OPENABLE]['status'] == qc.STATUS_OK
        assert archive.read('media/pair.png') == png_bytes()


def _evaluate(pair, tmp_path):
    business = qc.default_business()
    business['reverse_verify']['enabled'] = False
    business['metrics'].update(ppl='upstream_only', brisque='upstream_only', snr='upstream_only')
    return qc.evaluate_record(pair, sample_id=pair['sample_id'], media_root=tmp_path,
        carrier=MediaCarrier(tmp_path, tmp_path / 'carried'), business=business)


def test_old_image_metric_is_not_attached_to_selected_new_image(tmp_path):
    pair = native_pair()
    pair['left']['record']['image'] = {'path': 'old.jpg', 'brisque': 90.0}
    pair['left']['record']['qc']['media_quality'] = {'brisque': 90.0}
    pair['right']['record']['image'] = {'path': 'new.jpg'}
    write_png(tmp_path / 'new.jpg')
    outcome = _evaluate(pair, tmp_path)
    assert outcome.record['image'] == {'path': 'new.jpg'}
    assert outcome.metrics['brisque']['value'] is None
    assert outcome.metrics['brisque']['reason'] == 'upstream_missing'
    assert um.resolve_upstream_metric(outcome.record, 'brisque') is None
    assert pair['left']['record']['image']['brisque'] == 90.0


def test_metrics_follow_selected_text_image_and_audio_sources(tmp_path):
    pair = native_pair()
    left, right = pair['left']['record'], pair['right']['record']
    left['image'] = {'path': 'old.jpg', 'brisque': 90.0}
    left['audio'] = {'path': 'old.wav', 'snr_db': 99.0}
    left['qc'] = {'text_fluency': {'ppl': 38.0},
                  'media_quality': {'brisque': 90.0, 'snr_db': 99.0}}
    right['image'] = {'path': 'new.jpg', 'brisque': 20.0}
    right['audio'] = {'path': 'new.wav', 'snr_db': 22.0}
    right['qc'] = {'text_fluency': {'ppl': 900.0}}
    write_png(tmp_path / 'new.jpg')
    write_wav(tmp_path / 'new.wav')
    before = copy.deepcopy(pair)
    outcome = _evaluate(pair, tmp_path)
    assert {kind: item['value'] for kind, item in outcome.metrics.items()} == {
        'ppl': 38.0, 'brisque': 20.0, 'snr': 22.0}
    assert outcome.metrics['ppl']['path'].startswith('left.record.')
    assert outcome.metrics['brisque']['path'].startswith('right.record.')
    assert outcome.metrics['snr']['path'].startswith('right.record.')
    assert pair == before


def test_explicit_root_media_has_no_metrics_from_replaced_side_media(tmp_path):
    pair = native_pair()
    pair['text'] = {'content': 'A new root caption.'}
    pair['image_reference'] = {'path': 'root.jpg'}
    pair['audio_path'] = 'root.wav'
    pair['right']['record']['audio'] = {'path': 'old.wav', 'snr_db': 99.0}
    write_png(tmp_path / 'root.jpg')
    write_wav(tmp_path / 'root.wav')
    outcome = _evaluate(pair, tmp_path)
    assert all(item['value'] is None for item in outcome.metrics.values())
    assert qc.resolve_modalities(outcome.record)['image']['path'] == 'root.jpg'


def test_explicit_root_metric_evidence_overrides_selected_side_metrics(tmp_path):
    pair = native_pair()
    pair['qc'] = {'text_fluency': {'ppl': 45.0}, 'media_quality': {'brisque': 17.0}}
    write_png(tmp_path / 'media/pair.png')
    outcome = _evaluate(pair, tmp_path)
    assert outcome.metrics['ppl']['value'] == 45.0
    assert outcome.metrics['brisque']['value'] == 17.0
    assert outcome.metrics['ppl']['path'] == 'qc.text_fluency.ppl'


@pytest.mark.parametrize('origin', ['root', 'side'])
def test_quality_inside_selected_image_alias_belongs_to_that_asset(tmp_path, origin):
    pair = native_pair()
    record = pair if origin == 'root' else pair['right']['record']
    if origin == 'side':
        record.pop('image')
    record['images'] = [{'relative_path': 'selected.jpg', 'brisque': 17.0}]
    write_png(tmp_path / 'selected.jpg')
    outcome = _evaluate(pair, tmp_path)
    assert outcome.metrics['brisque']['value'] == 17.0
    assert outcome.metrics['brisque']['source'] == 'upstream'
    assert outcome.record['image'] == record['images'][0]


def test_typed_media_objects_keep_path_and_quality_from_one_source():
    pair = native_pair()
    pair['left']['record']['media'] = {'image': {'path': 'old.jpg', 'brisque': 90.0}}
    pair['right']['record'].pop('image')
    pair['right']['record']['media'] = {'image': {'path': 'new.jpg'}}
    projected = qc._native_record(pair)
    assert projected['media']['image'] == {'path': 'new.jpg'}
    assert projected['image'] == {'path': 'new.jpg'}


def test_relative_windows_media_path_is_normalized_without_rewriting_input():
    record = {'image': {'file_path': 'media\\pair.png'}}
    assert qc.resolve_modalities(record)['image']['path'] == 'media/pair.png'
    assert record['image']['file_path'] == 'media\\pair.png'
