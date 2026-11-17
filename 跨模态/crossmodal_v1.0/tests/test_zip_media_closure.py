"""ZIP output must be self-contained without an engineering-side media adapter."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


def package_module():
    source = Path(__file__).parents[1] / 'CMA_score_image_text_consistency/utils/form1_package.py'
    spec = importlib.util.spec_from_file_location('zip_package_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_output_zip_preserves_native_video_and_nested_frame(tmp_path):
    task = tmp_path / 'task_file'
    run = task / 'scoring/run001'
    run.mkdir(parents=True)
    source = task / 'input'
    (source / 'media').mkdir(parents=True)
    (source / 'media/movie.avi').write_bytes(b'original-video-bytes')
    (source / 'media/frame.jpg').write_bytes(b'original-frame-bytes')
    record = {'pair_id': 'p1', 'left': {'record': {'text': {'content': 'A dog'}}},
              'right': {'record': {'video': {'path': 'media/movie.avi',
                  'segments': [{'frames': [{'path': 'media/frame.jpg'}]}]},
                  'subtitle': {'path': 'missing-optional.vtt'}}}}
    (run / 'flow.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')
    response = package_module().build_reference(run, 'flow.jsonl')
    with zipfile.ZipFile(response['zip']) as archive:
        assert archive.read('media/movie.avi') == b'original-video-bytes'
        assert archive.read('media/frame.jpg') == b'original-frame-bytes'
        assert json.loads(archive.read('manifest.json'))['entrypoint'] == 'flow.jsonl'


def test_missing_active_video_does_not_publish_chainable_zip(tmp_path):
    run = tmp_path / 'task_file/operator/run001'
    run.mkdir(parents=True)
    (run / 'flow.jsonl').write_text(json.dumps({'sample_id': 's1',
        'video': {'path': 'media/absent.avi'}}) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='媒体|media'):
        package_module().build_reference(run, 'flow.jsonl')


def test_original_path_and_model_path_are_not_active_media(tmp_path):
    run = tmp_path / 'task_file/operator/run001'
    run.mkdir(parents=True)
    (run / 'photo.jpg').write_bytes(b'photo')
    row = {'sample_id': 's1', 'image': {'path': 'photo.jpg', 'original_path': 'historical/missing.jpg'},
           'metadata': {'source_url': 'https://example.invalid'},
           'processing': {'12': {'model': {'path': 'clip/ViT-B-32.pt'}}}}
    (run / 'flow.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
    assert Path(package_module().build_reference(run, 'flow.jsonl')['zip']).is_file()


def test_reference_only_export_lists_unavailable_links_without_claiming_files(tmp_path):
    run = tmp_path / 'task_file/export/run001'
    run.mkdir(parents=True)
    (run / 'rows.jsonl').write_text(json.dumps({'sample_id': 's1',
        'image': {'path': 'media/external.jpg'}}) + '\n', encoding='utf-8')
    response = package_module().build_reference(run, 'rows.jsonl', media_policy='reference')
    assert response['missingMedia'] == ['media/external.jpg']
    with zipfile.ZipFile(response['zip']) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['media_policy'] == 'reference'
        assert 'media/external.jpg' not in archive.namelist()


def test_media_can_be_carried_from_current_task_archive(tmp_path):
    task = tmp_path / 'task_file'
    run = task / 'export/run001'
    run.mkdir(parents=True)
    (task / 'input').mkdir()
    with zipfile.ZipFile(task / 'input/delivery.zip', 'w') as archive:
        archive.writestr('media/p.png', b'original-source-pixels')
    (run / 'rows.jsonl').write_text(json.dumps({'sample_id': 's1',
        'image': {'path': 'media/p.png'}}) + '\n', encoding='utf-8')
    response = package_module().build_reference(run, 'rows.jsonl')
    with zipfile.ZipFile(response['zip']) as archive:
        assert archive.read('media/p.png') == b'original-source-pixels'


def test_schema_labels_are_not_paths_but_media_aliases_and_typed_maps_are():
    row = {'local_annotations': [{'modality': ['audio', 'text'], 'labels': ['dog'],
                                'audio_segment': {'file_path': 'part.wav'}}],
           'media': {'image': 'photo.jpg', 'audio': {'file': 'audio.wav'},
                     'video': {'uri': 'video.mp4'}, 'labels': ['ignore']},
           'image_path': 'root.jpg'}
    assert package_module().active_media_refs(row) == [
        'audio.wav', 'part.wav', 'photo.jpg', 'root.jpg', 'video.mp4']


def test_multiple_current_input_archives_are_not_chosen_by_filename(tmp_path):
    task = tmp_path / 'task'
    run = task / 'export/run001'
    run.mkdir(parents=True)
    (task / 'input').mkdir()
    for name in ['a.zip', 'b.zip']:
        with zipfile.ZipFile(task / 'input' / name, 'w') as archive:
            archive.writestr('photo.jpg', name.encode())
    (run / 'main.jsonl').write_text('{"image":{"path":"photo.jpg"}}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='归档.*歧义|ambiguous'):
        package_module().build_reference(run, 'main.jsonl')
    assert not (run / 'photo.jpg').exists()


def test_corrupt_archive_never_leaves_reusable_partial_media(tmp_path):
    task = tmp_path / 'task'
    run = task / 'export/run001'
    run.mkdir(parents=True)
    (task / 'input').mkdir()
    archive_path = task / 'input/source.zip'
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('photo.jpg', b'pixels' * 100)
    damaged = bytearray(archive_path.read_bytes())
    damaged[30 + len('photo.jpg') + 7] ^= 1
    archive_path.write_bytes(damaged)
    (run / 'main.jsonl').write_text('{"image":{"path":"photo.jpg"}}\n', encoding='utf-8')
    for _ in range(2):
        with pytest.raises(zipfile.BadZipFile):
            package_module().build_reference(run, 'main.jsonl')
        assert not (run / 'photo.jpg').exists()
        assert not list(run.glob('*.tmp'))


def test_archive_media_copy_enforces_aggregate_size_limit(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    run = task / 'export/run001'
    run.mkdir(parents=True)
    (task / 'input').mkdir()
    with zipfile.ZipFile(task / 'input/source.zip', 'w') as archive:
        archive.writestr('one.jpg', b'123456')
        archive.writestr('two.jpg', b'abcdef')
    (run / 'main.jsonl').write_text('{"media":{"image":["one.jpg","two.jpg"]}}\n', encoding='utf-8')
    monkeypatch.setenv('MAX_UNPACKED_BYTES', '10')
    with pytest.raises(ValueError, match='限制|limit'):
        package_module().build_reference(run, 'main.jsonl')


def test_explicit_media_in_input_named_directory_is_in_output_zip(tmp_path):
    task = tmp_path / 'task'
    run = task / 'export/run001'
    run.mkdir(parents=True)
    (task / 'input/input').mkdir(parents=True)
    (task / 'input/input/photo.jpg').write_bytes(b'actual-photo')
    (task / 'input/unrelated.txt').write_bytes(b'must-not-be-carried')
    (run / 'main.jsonl').write_text('{"image":{"path":"input/photo.jpg"}}\n', encoding='utf-8')
    response = package_module().build_reference(run, 'main.jsonl')
    with zipfile.ZipFile(response['zip']) as archive:
        assert archive.read('input/photo.jpg') == b'actual-photo'
        assert 'input/unrelated.txt' not in archive.namelist()


def test_supported_relative_path_images_and_safe_windows_separator_aliases():
    row = {'image': {'relative_path': 'media\\photo.jpg'},
           'images': [{'path': 'another.jpg'}]}
    assert package_module().active_media_refs(row) == ['another.jpg', 'media/photo.jpg']
    for unsafe in ['\\\\server\\photo.jpg', '\\absolute.jpg', 'media\\..\\secret.jpg']:
        with pytest.raises(ValueError, match='媒体|media'):
            package_module().active_media_refs({'image': {'relative_path': unsafe}})


@pytest.mark.parametrize('policy', ['required', 'reference'])
def test_optional_audit_archive_ambiguity_does_not_block_main(tmp_path, policy):
    task = tmp_path / 'task'
    run = task / 'operator/run001'
    run.mkdir(parents=True)
    (task / 'input').mkdir()
    for name in ['one.zip', 'two.zip']:
        with zipfile.ZipFile(task / 'input' / name, 'w') as archive:
            archive.writestr('unrelated.txt', 'unused')
    (run / 'main.jsonl').write_text('{"sample_id":"live"}\n', encoding='utf-8')
    (run / 'audit.jsonl').write_text('{"record":{"image":{"path":"optional.jpg"}}}\n', encoding='utf-8')
    response = package_module().build_reference(run, 'main.jsonl', media_policy=policy)
    assert Path(response['zip']).is_file()
    assert not (run / 'optional.jpg').exists()


def test_reference_only_main_declares_ambiguous_archive_link_missing(tmp_path):
    task = tmp_path / 'task'
    run = task / 'operator/run001'
    run.mkdir(parents=True)
    (task / 'input').mkdir()
    for name in ['one.zip', 'two.zip']:
        with zipfile.ZipFile(task / 'input' / name, 'w') as archive:
            archive.writestr('photo.jpg', name.encode())
    (run / 'main.jsonl').write_text('{"image":{"path":"photo.jpg"}}\n', encoding='utf-8')
    response = package_module().build_reference(run, 'main.jsonl', media_policy='reference')
    assert response['missingMedia'] == ['photo.jpg']


@pytest.mark.parametrize('relative', ['../outside.jpg', '/absolute.jpg', 'C:\\secret.jpg',
                                     'https://example.invalid/image.jpg'])
def test_unsafe_active_media_is_explicit_error(relative):
    with pytest.raises(ValueError, match='媒体|media'):
        package_module().active_media_refs({'image': {'uri': relative}})


def test_archive_only_missing_or_unsafe_media_is_optional(tmp_path):
    run = tmp_path / 'task/operator/run001'
    run.mkdir(parents=True)
    (run / 'flow.jsonl').write_text('{"sample_id":"live"}\n', encoding='utf-8')
    (run / 'audit.jsonl').write_text(json.dumps({'record': {
        'image': {'path': '../historical.jpg'}}}) + '\n', encoding='utf-8')
    assert Path(package_module().build_reference(run, 'flow.jsonl')['zip']).is_file()


def test_guessed_main_entrypoint_still_requires_active_media(tmp_path):
    run = tmp_path / 'task/operator/run001'
    run.mkdir(parents=True)
    (run / 'main.jsonl').write_text(json.dumps({'sample_id': 'live',
        'media': {'image': 'missing.jpg'}}) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='media_missing'):
        package_module().build_reference(run)


def test_alias_media_files_are_copied_into_official_package(tmp_path):
    task = tmp_path / 'task'
    run = task / 'operator/run001'
    run.mkdir(parents=True)
    source = task / 'input'
    source.mkdir()
    row = {'sample_id': 'live', 'media': {'image': {'file_path': 'one.jpg'},
           'audio': {'file': 'two.wav'}, 'video': {'uri': 'three.mp4'}},
           'image_path': 'four.jpg'}
    for name in ['one.jpg', 'two.wav', 'three.mp4', 'four.jpg']:
        (source / name).write_bytes(name.encode())
    (run / 'main.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
    reference = package_module().build_reference(run, 'main.jsonl')
    with zipfile.ZipFile(reference['zip']) as archive:
        for name in ['one.jpg', 'two.wav', 'three.mp4', 'four.jpg']:
            assert archive.read(name) == name.encode()
