import torch
import pytest
from utils import scoring


def test_legacy_weight_norm_checkpoint_is_loaded_exactly(tiny_models, tmp_path):
    from transformers import Wav2Vec2Model
    original=Wav2Vec2Model.from_pretrained(tiny_models['audio_path'],local_files_only=True)
    state=original.state_dict()
    renamed={k.replace('parametrizations.weight.original0','weight_g').replace(
        'parametrizations.weight.original1','weight_v'):v for k,v in state.items()}
    root=tmp_path/'legacy_audio';root.mkdir()
    original.config.save_pretrained(str(root))
    torch.save(renamed,root/'pytorch_model.bin')
    model=scoring.load_audio_checkpoint(root)
    for key,value in original.state_dict().items():
        assert torch.equal(value,model.state_dict()[key]),key


def test_incomplete_audio_checkpoint_fails_instead_of_random_initialization(tiny_models, tmp_path):
    from transformers import Wav2Vec2Model
    model=Wav2Vec2Model.from_pretrained(tiny_models['audio_path'],local_files_only=True)
    state=model.state_dict();state.pop('feature_projection.projection.weight')
    root=tmp_path/'missing_parameter';root.mkdir();model.config.save_pretrained(str(root))
    torch.save(state,root/'pytorch_model.bin')
    with pytest.raises(scoring.DependencyError,match='缺失'):
        scoring.load_audio_checkpoint(root)
