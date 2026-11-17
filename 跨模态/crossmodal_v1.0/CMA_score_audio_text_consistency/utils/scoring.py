"""音文评分的真实算法与模型适配层。

包含两类能力：
1. 生产判定：文本规范化（含繁→简字符级归一化）、CER/WER 与 Levenshtein 编辑距离比例（纯算法，不需要模型）；
2. 兼容诊断：旧版 ``facebook/wav2vec2-base`` + ``bert-base-chinese`` 适配层。

生产路线不依赖第二类能力。裸余弦只允许作为显式注入的实验诊断，不能改变门禁结论。

严格边界：
- 旧模型适配权重**只从本地目录加载**（``local_files_only=True``），运行期绝不联网下载；
- 生产 v2 不调用该适配层；只有显式注入 scorer 的离线实验才会触发；
- **绝不产出固定/随机/规则伪造的模型分数**；未真正调用模型时模型字段为 null。
"""
from __future__ import annotations

import importlib
import importlib.util
import math
import os
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils import itn
from utils import t2s
from utils.schemas import DEFAULT_AUDIO_MODEL, DEFAULT_TEXT_MODEL, SUPPORTED_SAMPLE_RATE

try:  # numpy 缺失时仍允许关闭路线运行，只把需要数值计算的能力标为不可用
    import numpy as np
except ImportError:  # pragma: no cover - 环境无 numpy
    np = None  # type: ignore[assignment]

AUDIO_MODEL_ID = DEFAULT_AUDIO_MODEL
TEXT_MODEL_ID = DEFAULT_TEXT_MODEL
AUXILIARY_MODEL_ID = "whisper-base"

_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin", "tf_model.h5", "flax_model.msgpack")


class DependencyError(RuntimeError):
    """运行依赖（torch/transformers/numpy）或本地模型权重缺失。"""


class ScoreError(ValueError):
    """输入或模型输出不满足评分前置条件。"""


# --------------------------------------------------------------------------- #
# 文本与相似度（真实算法）
# --------------------------------------------------------------------------- #

def _traditional_enabled(traditional_to_simplified: Optional[bool]) -> bool:
    """归一转繁开关：``None`` 按默认（开启）处理。"""
    return t2s.is_enabled(traditional_to_simplified)


def _itn_enabled(inverse_text_normalization: Optional[bool]) -> bool:
    """中文 ITN 开关：``None`` 按默认（开启）处理；显式 False 恢复改造前口径。"""
    return inverse_text_normalization is not False


def normalize_text(text: Any, *,
                   traditional_to_simplified: Optional[bool] = None,
                   inverse_text_normalization: Optional[bool] = None) -> str:
    """NFKC 规范化后小写，并去掉空白、标点与符号（编辑距离比较口径）。

    ``traditional_to_simplified`` 为 ``None`` 时按默认开启处理：先把繁体字逐字符转
    简体，再做 NFKC/小写/去标点。显式传 ``False`` 可得到改造前的纯 NFKC 口径（用于
    对照回归）；英文/数字/标点行为不受影响。

    ``inverse_text_normalization`` 为 ``None`` 时按默认开启处理：t2s 之后把中文
    数字/百分比/数量单位表达归一为阿拉伯数字（见 ``utils/itn.py``），消除同一数值
    两种写法造成的 CER 虚高；显式传 ``False`` 关闭。门禁口径取 ITN/非 ITN 两者
    较小值（写法差异只豁免、不抬分），见 ``text_error_metrics``。
    """
    if text is None:
        return ""
    value = str(text)
    if _traditional_enabled(traditional_to_simplified):
        value = t2s.to_simplified(value)
    if _itn_enabled(inverse_text_normalization):
        value, _ = itn.to_arabic_numbers(value)
    value = unicodedata.normalize("NFKC", value).lower()
    kept: List[str] = []
    for char in value:
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category.startswith("P") or category.startswith("S") or category.startswith("Z"):
            continue
        kept.append(char)
    return "".join(kept)


def levenshtein(left: str, right: str) -> int:
    """字符级 Levenshtein 距离（真实 DP 实现）。"""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, start=1):
        current = [i]
        for j, char_right in enumerate(right, start=1):
            cost = 0 if char_left == char_right else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def _ratio_of(first: str, second: str) -> float:
    """两串已归一化文本的编辑距离比例；两侧都为空定义为 0。"""
    if not first and not second:
        return 0.0
    if not first or not second:
        return 1.0
    return levenshtein(first, second) / float(max(len(first), len(second)))


def edit_distance_ratio(left: Any, right: Any, *,
                        traditional_to_simplified: Optional[bool] = None,
                        inverse_text_normalization: Optional[bool] = None) -> float:
    """编辑距离比例 = 距离 / 归一化后较长文本长度；两侧都为空的定义为 0。

    ITN 开启时取「ITN 口径」与「非 ITN 口径」的较小值：数字写法差异只被豁免，
    绝不会把本来更低的错误率抬高（round9 复核确认写法差异属误判来源）。
    """
    itn_on = _itn_enabled(inverse_text_normalization)
    first = normalize_text(left, traditional_to_simplified=traditional_to_simplified,
                           inverse_text_normalization=itn_on)
    second = normalize_text(right, traditional_to_simplified=traditional_to_simplified,
                            inverse_text_normalization=itn_on)
    ratio = _ratio_of(first, second)
    if itn_on:
        first_plain = normalize_text(left, traditional_to_simplified=traditional_to_simplified,
                                     inverse_text_normalization=False)
        second_plain = normalize_text(right, traditional_to_simplified=traditional_to_simplified,
                                      inverse_text_normalization=False)
        ratio = min(ratio, _ratio_of(first_plain, second_plain))
    return ratio


def _wer_tokens(text: Any, *,
                traditional_to_simplified: Optional[bool] = None,
                inverse_text_normalization: Optional[bool] = None) -> List[str]:
    """生成稳定的 WER token。

    中文按字切分，拉丁字母/数字按连续片段切分；标点和空白只作为分隔符。
    该指标用于解释输出，生产门禁仍采用字符级 CER。
    """
    raw = "" if text is None else str(text)
    if _traditional_enabled(traditional_to_simplified):
        raw = t2s.to_simplified(raw)
    if _itn_enabled(inverse_text_normalization):
        raw, _ = itn.to_arabic_numbers(raw)
    value = unicodedata.normalize("NFKC", raw).lower()
    tokens: List[str] = []
    current: List[str] = []
    for char in value:
        category = unicodedata.category(char)
        if char.isspace() or category.startswith(("P", "S", "Z")):
            if current:
                tokens.append("".join(current))
                current = []
            continue
        # CJK ideographs are meaningful at character granularity. Keep other
        # scripts/numbers together so English WER remains word-like.
        codepoint = ord(char)
        is_cjk = (0x3400 <= codepoint <= 0x4DBF or
                  0x4E00 <= codepoint <= 0x9FFF or
                  0xF900 <= codepoint <= 0xFAFF)
        if is_cjk:
            if current:
                tokens.append("".join(current))
                current = []
            tokens.append(char)
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def token_error_ratio(left: Any, right: Any, *,
                      traditional_to_simplified: Optional[bool] = None,
                      inverse_text_normalization: Optional[bool] = None) -> float:
    """计算解释用 token error ratio（WER 口径）。"""
    first = _wer_tokens(left, traditional_to_simplified=traditional_to_simplified,
                        inverse_text_normalization=inverse_text_normalization)
    second = _wer_tokens(right, traditional_to_simplified=traditional_to_simplified,
                         inverse_text_normalization=inverse_text_normalization)
    if not first and not second:
        return 0.0
    if not first or not second:
        return 1.0
    previous = list(range(len(second) + 1))
    for index, token_left in enumerate(first, start=1):
        current = [index]
        for offset, token_right in enumerate(second, start=1):
            cost = 0 if token_left == token_right else 1
            current.append(min(previous[offset] + 1,
                               current[offset - 1] + 1,
                               previous[offset - 1] + cost))
        previous = current
    return previous[-1] / float(max(len(first), len(second)))


def text_error_metrics(left: Any, right: Any, *,
                       traditional_to_simplified: Optional[bool] = None,
                       inverse_text_normalization: Optional[bool] = None) -> Dict[str, Any]:
    """返回可解释的字符/词级一致性指标。

    ``edit_distance_ratio`` 与 ``cer`` 同值，作为生产门禁唯一指标；``wer``
    仅用于输出解释，避免把未校准的词级切分偷偷变成另一套门槛。
    繁体→简体归一化默认开启：两侧文本先逐字符转简体再做字符级距离；``*_normalized``
    是最终口径，``*_normalized_before_t2s`` 保留转换前口径，便于审计归一化前后。
    中文 ITN（数字/百分比/单位写法归一）默认开启，见 ``utils/itn.py``：
    门禁 CER 取「ITN 口径」与「非 ITN 口径」的较小值——数字写法差异只被豁免，
    不会把本来更低的错误率抬高；``itn_applied`` / ``itn_lowered_cer`` /
    ``cer_without_itn`` / ``number_tokens_normalized`` 记录实际生效情况。
    """
    enabled = _traditional_enabled(traditional_to_simplified)
    itn_on = _itn_enabled(inverse_text_normalization)
    plain_left = normalize_text(left, traditional_to_simplified=enabled,
                                inverse_text_normalization=False)
    plain_right = normalize_text(right, traditional_to_simplified=enabled,
                                 inverse_text_normalization=False)
    plain_distance = levenshtein(plain_left, plain_right)
    plain_denominator = max(len(plain_left), len(plain_right))
    plain_cer = (0.0 if plain_denominator == 0
                 else plain_distance / float(plain_denominator))
    if itn_on:
        itn_left = normalize_text(left, traditional_to_simplified=enabled,
                                  inverse_text_normalization=True)
        itn_right = normalize_text(right, traditional_to_simplified=enabled,
                                   inverse_text_normalization=True)
        itn_distance = levenshtein(itn_left, itn_right)
        itn_denominator = max(len(itn_left), len(itn_right))
        itn_cer = (0.0 if itn_denominator == 0
                   else itn_distance / float(itn_denominator))
    else:
        itn_left = plain_left
        itn_right = plain_right
        itn_distance = plain_distance
        itn_cer = plain_cer
    # 取两种口径的较小值：ITN 只豁免写法差异，绝不抬高错误率
    if itn_cer <= plain_cer:
        normalized_left, normalized_right = itn_left, itn_right
        distance, cer = itn_distance, itn_cer
        itn_won = bool(itn_on and itn_cer < plain_cer)
    else:
        normalized_left, normalized_right = plain_left, plain_right
        distance, cer = plain_distance, plain_cer
        itn_won = False
    before_left = (normalize_text(left, traditional_to_simplified=False,
                                  inverse_text_normalization=False)
                   if (enabled or itn_on) else normalized_left)
    before_right = (normalize_text(right, traditional_to_simplified=False,
                                   inverse_text_normalization=False)
                    if (enabled or itn_on) else normalized_right)
    snapshot = t2s.mapping_snapshot() if enabled else {"source": None, "entries": None}
    converted = (t2s.converted_char_count(left) + t2s.converted_char_count(right)
                 if enabled else 0)
    itn_tokens = 0
    if itn_on:
        itn_tokens = (itn.to_arabic_numbers(t2s.to_simplified(str(left or ""))
                                            if enabled else str(left or ""))[1]
                      + itn.to_arabic_numbers(t2s.to_simplified(str(right or ""))
                                              if enabled else str(right or ""))[1])
    return {
        "transcript_normalized": normalized_left,
        "description_normalized": normalized_right,
        "edit_distance": distance,
        "cer": cer,
        "wer": token_error_ratio(left, right, traditional_to_simplified=enabled,
                                 inverse_text_normalization=itn_on),
        "edit_distance_ratio": cer,
        "metric": "CER",
        # 繁→简归一化审计字段（不改变阈值语义）：开关 / 转换前口径 / 词典来源
        "traditional_to_simplified": enabled,
        "transcript_normalized_before_t2s": before_left,
        "description_normalized_before_t2s": before_right,
        "traditional_chars_converted": converted,
        "t2s_dictionary_source": snapshot.get("source"),
        "t2s_mapping_entries": snapshot.get("entries"),
        # 中文 ITN 审计字段：开关、是否因 ITN 取得更低 CER、非 ITN 口径 CER、改写词元数
        "itn_applied": itn_on,
        "itn_lowered_cer": itn_won,
        "cer_without_itn": plain_cer,
        "number_tokens_normalized": itn_tokens,
    }


def decide_edit(edit_ratio: Optional[float], *, edit_ratio_max: float) -> tuple:
    """生产门禁：编辑距离比例不超过任务阈值即通过。"""
    if edit_ratio is None:
        raise ScoreError("判定需要编辑距离比例")
    if edit_ratio > edit_ratio_max:
        return False, "edit_distance_exceeded"
    return True, "passed"


def cosine_similarity(left, right) -> float:
    """余弦相似度；维度不一致或出现零向量时显式失败。"""
    _require_numpy()
    first = np.asarray(left, dtype=np.float32).reshape(-1)
    second = np.asarray(right, dtype=np.float32).reshape(-1)
    if first.shape[0] != second.shape[0]:
        raise ScoreError("向量维度不一致：%d vs %d" % (first.shape[0], second.shape[0]))
    norm_first = float(np.linalg.norm(first))
    norm_second = float(np.linalg.norm(second))
    if norm_first <= 0.0 or norm_second <= 0.0:
        raise ScoreError("向量范数为 0，无法计算余弦相似度")
    return float(np.dot(first, second) / (norm_first * norm_second))


def validate_auxiliary_similarity(value: Any) -> float:
    """校验辅助模型归一化匹配分，拒绝 NaN/越界值。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoreError("辅助匹配分不是数字") from exc
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ScoreError("辅助匹配分必须在 0–1 之间，实际 %r" % value)
    return number


def decide(cosine: Optional[float], edit_ratio: Optional[float], *,
           cosine_threshold: float, edit_ratio_max: float) -> tuple:
    """表1 判定：余弦分 ≥ 阈值 **且** 编辑距离比例 < 上限 才通过。

    编辑距离超标即可判定不通过（无需模型）；判定顺序固定，保证归因唯一。
    """
    if cosine is None or edit_ratio is None:
        raise ScoreError("判定需要同时给出余弦分与编辑距离比例")
    if edit_ratio >= edit_ratio_max:
        return False, "edit_distance_exceeded"
    if cosine < cosine_threshold:
        return False, "cosine_below_threshold"
    return True, "passed"


# --------------------------------------------------------------------------- #
# 运行时/权重探测
# --------------------------------------------------------------------------- #

def _require_numpy() -> None:
    if np is None:
        raise DependencyError("缺少 numpy，无法进行数值计算")


def probe_runtime() -> Dict[str, Any]:
    """探测运行依赖是否可用（不导入重量级包，避免拖慢关闭路线）。"""
    availability: Dict[str, Any] = {}
    versions: Dict[str, str] = {}
    for name in ("numpy", "torch", "transformers"):
        found = importlib.util.find_spec(name) is not None
        availability[name] = found
        if found:
            try:
                versions[name] = importlib.metadata.version(name)
            except Exception:  # noqa: BLE001 - 版本读不到不影响可用性判断
                versions[name] = "unknown"
    availability["versions"] = versions
    return availability


def _model_root() -> Path:
    """权重基目录：``$MODEL_ROOT`` 优先，否则算子本地 ``<算子目录>``。

    配置里的 ``model_root``（默认 ``weight``）相对本目录解析，因此默认即
    ``<算子目录>/weight``；本算子生产路线不加载 wav2vec2/BERT，诊断探测只按
    该路径显式报 missing，绝不联网补采、也不回退到包根 ``weight/``。
    """
    override = os.environ.get("MODEL_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1]


def _resolve_model_root(model_root) -> Path:
    """把配置里的 ``model_root`` 解析为绝对目录。

    相对值（默认 ``weight``）挂到 ``$MODEL_ROOT``／算子本地 ``<算子目录>`` 之下，
    因此默认 ``model_root: weight`` + ``audio_model_dir: wav2vec2-base`` 即指向
    算子本地 ``weight/wav2vec2-base``（本算子不内置该权重，探测显式 missing）。
    """
    text = "" if model_root is None else str(model_root).strip()
    if not text:
        return _model_root()
    root = Path(text).expanduser()
    return root if root.is_absolute() else _model_root() / root


def resolve_model_path(model_root, name: str) -> Path:
    candidate = Path(name)
    return candidate if candidate.is_absolute() else _resolve_model_root(model_root) / candidate


def has_local_weights(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    if not (directory / "config.json").is_file():
        return False
    return any((directory / weight).is_file() for weight in _WEIGHT_FILES)


def probe_models(model_root, audio_model_dir: str,
                 text_model_dir: str) -> Dict[str, Any]:
    """探测本地权重与运行依赖；缺失项逐条列出，供显式失败或降级归因。"""
    audio_path = resolve_model_path(model_root, audio_model_dir)
    text_path = resolve_model_path(model_root, text_model_dir)
    missing: List[str] = []
    audio_ok = has_local_weights(audio_path)
    text_ok = has_local_weights(text_path)
    if not audio_ok:
        missing.append("audio_weights:%s" % audio_path)
    if not text_ok:
        missing.append("text_weights:%s" % text_path)
    runtime = probe_runtime()
    for name in ("numpy", "torch", "transformers"):
        if not runtime.get(name):
            missing.append("runtime:%s" % name)
    return {
        "available": not missing,
        "missing": missing,
        "audio_path": str(audio_path) if audio_ok else None,
        "text_path": str(text_path) if text_ok else None,
        "audio_weights_ok": audio_ok,
        "text_weights_ok": text_ok,
        "runtime": runtime,
    }


def _model_version(directory: Path, config: Any) -> str:
    """记录真实权重身份：优先 HF 的名字来源，否则用目录名（可搬运、可审计）。"""
    name = ""
    if config is not None:
        name = str(getattr(config, "_name_or_path", "") or "").strip()
    if not name or Path(name) == directory:
        name = directory.name
    return name


# --------------------------------------------------------------------------- #
# 模型适配
# --------------------------------------------------------------------------- #

def _read_weight_file(directory: Path):
    """读取本地权重文件；返回 ``(state_dict, 文件名)``，缺文件时显式失败。"""
    import torch
    safetensors_path = directory / "model.safetensors"
    if safetensors_path.is_file():
        from safetensors.torch import load_file
        return load_file(str(safetensors_path), device="cpu"), safetensors_path.name
    binary = directory / "pytorch_model.bin"
    if binary.is_file():
        return torch.load(binary, map_location="cpu", weights_only=True), binary.name
    raise DependencyError("本地缺少语音权重文件（model.safetensors / pytorch_model.bin）：%s"
                          % directory)


def load_audio_checkpoint(directory):
    """离线加载冻结权重；兼容旧 weight_norm 键，禁止缺权重时随机初始化。

    不调用 ``Wav2Vec2Model.from_pretrained(..., state_dict=...)``：transformers 4.5x
    起禁止「模型名/目录 + state_dict」混用。这里改为按 ``config.json`` 构造结构，再用
    ``load_state_dict(strict=False)`` 精确灌入权重，缺失键或形状不符一律显式失败。
    """
    from transformers import Wav2Vec2Config, Wav2Vec2Model
    directory = Path(directory)
    state, weight_file = _read_weight_file(directory)
    renamed = []
    mapped = {}
    for key, value in state.items():
        new_key = key.replace(".weight_g", ".parametrizations.weight.original0").replace(
            ".weight_v", ".parametrizations.weight.original1")
        if new_key != key:
            renamed.append({"from": key, "to": new_key})
        mapped[new_key] = value
    config = Wav2Vec2Config.from_pretrained(str(directory), local_files_only=True)
    model = Wav2Vec2Model(config)
    try:
        missing, unexpected = model.load_state_dict(mapped, strict=False)
    except RuntimeError as exc:  # 形状不符等硬错误
        raise DependencyError("冻结语音权重缺失或不兼容，拒绝随机初始化：%s" % exc) from exc
    if missing:
        raise DependencyError(
            "冻结语音权重缺失或不兼容，拒绝随机初始化：missing_keys=%s" % sorted(missing))
    model.checkpoint_integrity = {"weight_file": weight_file,
                                  "missing_keys": [],
                                  "unexpected_keys": sorted(unexpected),
                                  "legacy_keys_remapped": renamed}
    return model


class AudioTextScorer:
    """真实 wav2vec2 + bert 适配器；只本地加载权重，不联网。"""

    def __init__(self, audio_model, text_model, tokenizer, feature_extractor,
                 audio_model_version: str, text_model_version: str,
                 audio_model_path: str, text_model_path: str, device: str):
        self._audio_model = audio_model
        self._text_model = text_model
        self._tokenizer = tokenizer
        self._feature_extractor = feature_extractor
        self.audio_model_version = audio_model_version
        self.text_model_version = text_model_version
        self.audio_model_path = audio_model_path
        self.text_model_path = text_model_path
        self.device = device
        self.hidden_size = int(audio_model.config.hidden_size)
        self.audio_model_id = AUDIO_MODEL_ID
        self.text_model_id = TEXT_MODEL_ID

    # -- 加载 ------------------------------------------------------------- #

    @classmethod
    def load(cls, audio_dir, text_dir, device: str = "cpu") -> "AudioTextScorer":
        _require_numpy()
        runtime = probe_runtime()
        for name in ("torch", "transformers"):
            if not runtime.get(name):
                raise DependencyError("缺少运行依赖：%s" % name)
        audio_path = Path(audio_dir)
        text_path = Path(text_dir)
        if not has_local_weights(audio_path):
            raise DependencyError("本地缺少语音模型权重或 config.json：%s" % audio_path)
        if not has_local_weights(text_path):
            raise DependencyError("本地缺少文本模型权重或 config.json：%s" % text_path)

        import torch
        from transformers import AutoTokenizer, BertModel, Wav2Vec2FeatureExtractor, Wav2Vec2Model

        resolved_device = device
        if device == "auto":
            resolved_device = "cuda" if torch.cuda.is_available() else "cpu"
        if resolved_device == "cuda" and not torch.cuda.is_available():
            raise DependencyError("配置要求 cuda，但当前环境没有可用 GPU")

        try:
            feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(
                str(audio_path), local_files_only=True)
            audio_model = load_audio_checkpoint(audio_path)
            text_model = BertModel.from_pretrained(str(text_path), local_files_only=True)
            tokenizer = AutoTokenizer.from_pretrained(str(text_path), local_files_only=True)
        except Exception as exc:  # noqa: BLE001 - 统一转为可归因的依赖失败
            raise DependencyError("本地权重加载失败（%s）：%s" % (type(exc).__name__, exc)) from exc

        audio_hidden = int(audio_model.config.hidden_size)
        text_hidden = int(text_model.config.hidden_size)
        if audio_hidden != text_hidden:
            raise ScoreError("语音与文本模型隐藏维度不一致（%d vs %d），无法直接计算余弦"
                             % (audio_hidden, text_hidden))

        audio_model.to(resolved_device).eval()
        text_model.to(resolved_device).eval()
        return cls(audio_model, text_model, tokenizer, feature_extractor,
                   _model_version(audio_path, audio_model.config),
                   _model_version(text_path, text_model.config),
                   str(audio_path), str(text_path), resolved_device)

    # -- 前向 ------------------------------------------------------------- #

    def embed_audio(self, waveform, sample_rate: int):
        """返回音频段落的池化嵌入向量。"""
        _require_numpy()
        if int(sample_rate) != SUPPORTED_SAMPLE_RATE:
            raise ScoreError("音频采样率必须是 %d，实际 %s（上层需先重采样）"
                             % (SUPPORTED_SAMPLE_RATE, sample_rate))
        values = np.asarray(waveform, dtype=np.float32).reshape(-1)
        if values.size == 0:
            raise ScoreError("音频波形为空，无法提取特征")
        import torch

        with torch.no_grad():
            if self._feature_extractor is not None:
                encoded = self._feature_extractor(values, sampling_rate=SUPPORTED_SAMPLE_RATE,
                                                  return_tensors="pt")
                tensor = encoded["input_values"].to(self.device)
            else:  # pragma: no cover - 权重目录缺 preprocessor_config 时的等价归一化
                std = float(values.std())
                normalized = (values - float(values.mean())) / (std if std > 0 else 1.0)
                tensor = torch.from_numpy(normalized.astype(np.float32)).unsqueeze(0).to(self.device)
            outputs = self._audio_model(tensor)
            vector = outputs.last_hidden_state.mean(dim=1).squeeze(0)
        return vector.detach().cpu().numpy().astype(np.float32)

    def embed_text(self, text: str):
        """返回文本的掩码平均池化嵌入向量。"""
        _require_numpy()
        cleaned = str(text or "").strip()
        if not normalize_text(cleaned):
            raise ScoreError("文本为空或仅含标点，无法提取特征")
        import torch

        encoded = self._tokenizer(cleaned, return_tensors="pt", truncation=True, max_length=512)
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        with torch.no_grad():
            outputs = self._text_model(**encoded)
            hidden = outputs.last_hidden_state
            if "attention_mask" in encoded:
                mask = encoded["attention_mask"].unsqueeze(-1).float()
                summed = (hidden * mask).sum(dim=1)
                counts = mask.sum(dim=1).clamp(min=1e-6)
                vector = (summed / counts).squeeze(0)
            else:  # pragma: no cover - 分词器未返回掩码
                vector = hidden.mean(dim=1).squeeze(0)
        return vector.detach().cpu().numpy().astype(np.float32)

    def score(self, waveform, sample_rate: int, text: str) -> Dict[str, Any]:
        """真实计算音文余弦相似度，并回报真实模型身份。"""
        audio_vector = self.embed_audio(waveform, sample_rate)
        text_vector = self.embed_text(text)
        cosine = cosine_similarity(audio_vector, text_vector)
        return {
            "cosine": cosine,
            "audio_model": self.audio_model_id,
            "text_model": self.text_model_id,
            "audio_model_version": self.audio_model_version,
            "text_model_version": self.text_model_version,
            "audio_model_path": self.audio_model_path,
            "text_model_path": self.text_model_path,
            "hidden_size": self.hidden_size,
            "device": self.device,
            "audio_checkpoint_integrity": getattr(self._audio_model, "checkpoint_integrity", {}),
        }


def load_scorer(config) -> AudioTextScorer:
    """按配置加载真实模型；缺失时抛 ``DependencyError``。"""
    return AudioTextScorer.load(
        resolve_model_path(config.model_root, config.audio_model_dir),
        resolve_model_path(config.model_root, config.text_model_dir),
        device=config.device,
    )


class WhisperCompatibilityScorer:
    """Whisper-base 中文条件概率辅助层。

    该层只提供可审计的音频-文本匹配证据，不替换表1冻结的
    wav2vec2+BERT 原始余弦；调用方必须同时保留两者。
    """

    def __init__(self, model, processor, model_version: str, model_path: str,
                 device: str, language: str = "zh", task: str = "transcribe"):
        self._model = model
        self._processor = processor
        self.model_id = AUXILIARY_MODEL_ID
        self.model_version = model_version
        self.model_path = model_path
        self.device = device
        self.language = language
        self.task = task
        self._prompt = [model.config.decoder_start_token_id]
        self._prompt.extend(token_id for _position, token_id in
                            processor.get_decoder_prompt_ids(language=language, task=task))

    @classmethod
    def load(cls, model_dir, device: str = "cpu", language: str = "zh",
             task: str = "transcribe") -> "WhisperCompatibilityScorer":
        _require_numpy()
        runtime = probe_runtime()
        for name in ("torch", "transformers"):
            if not runtime.get(name):
                raise DependencyError("缺少运行依赖：%s" % name)
        directory = Path(model_dir)
        if not has_local_weights(directory):
            raise DependencyError("本地缺少 Whisper 权重或 config.json：%s" % directory)
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        resolved_device = device
        if device == "auto":
            resolved_device = "cuda" if torch.cuda.is_available() else "cpu"
        if resolved_device == "cuda" and not torch.cuda.is_available():
            raise DependencyError("配置要求 cuda，但当前环境没有可用 GPU")
        try:
            processor = WhisperProcessor.from_pretrained(str(directory), local_files_only=True)
            model = WhisperForConditionalGeneration.from_pretrained(
                str(directory), local_files_only=True)
        except Exception as exc:  # noqa: BLE001
            raise DependencyError("本地 Whisper 权重加载失败（%s）：%s" %
                                  (type(exc).__name__, exc)) from exc
        model.to(resolved_device).eval()
        version = _model_version(directory, model.config)
        return cls(model, processor, version, str(directory), resolved_device, language, task)

    def score(self, waveform, sample_rate: int, text: str) -> Dict[str, Any]:
        _require_numpy()
        if int(sample_rate) != SUPPORTED_SAMPLE_RATE:
            raise ScoreError("音频采样率必须是 %d，实际 %s" %
                             (SUPPORTED_SAMPLE_RATE, sample_rate))
        values = np.asarray(waveform, dtype=np.float32).reshape(-1)
        if values.size == 0:
            raise ScoreError("音频波形为空，无法计算辅助匹配分")
        cleaned = str(text or "").strip()
        if not normalize_text(cleaned):
            raise ScoreError("文本为空或仅含标点，无法计算辅助匹配分")
        import torch

        encoded = self._processor(values, sampling_rate=SUPPORTED_SAMPLE_RATE,
                                  return_tensors="pt")
        features = encoded["input_features"].to(self.device)
        target = self._processor.tokenizer(
            cleaned, add_special_tokens=False, return_tensors="pt").input_ids[0]
        if target.numel() == 0:
            raise ScoreError("文本未产生有效 Whisper token")
        full = torch.tensor(self._prompt, dtype=torch.long).tolist() + target.tolist()
        decoder_input_ids = torch.tensor(full[:-1], dtype=torch.long,
                                         device=self.device).unsqueeze(0)
        labels = torch.tensor(full[1:], dtype=torch.long,
                              device=self.device).unsqueeze(0)
        masked_labels = torch.full_like(labels, -100)
        masked_labels[0, len(self._prompt) - 1:] = labels[0, len(self._prompt) - 1:]
        with torch.no_grad():
            output = self._model(input_features=features,
                                 decoder_input_ids=decoder_input_ids,
                                 labels=masked_labels,
                                 use_cache=False)
        similarity = validate_auxiliary_similarity(float(torch.exp(-output.loss).cpu()))
        return {
            "similarity": similarity,
            "log_likelihood": float((-output.loss).cpu()),
            "token_count": int(target.numel()),
            "model": self.model_id,
            "model_version": self.model_version,
            "model_path": self.model_path,
            "device": self.device,
            "language": self.language,
            "task": self.task,
        }


def load_whisper_compatibility_scorer(config) -> WhisperCompatibilityScorer:
    return WhisperCompatibilityScorer.load(
        resolve_model_path(config.model_root, config.auxiliary_model_dir),
        device=config.device, language=config.auxiliary_language,
        task=config.auxiliary_task)
