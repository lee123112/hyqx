"""CLIP-based video/caption alignment.

The supplied ``weight/clip/ViT-L-14.pt`` is a TorchScript export whose forward
graph was saved with CUDA device constants.  This module reads its state dict,
rebuilds the standard OpenAI CLIP ViT-L/14 module on the local device, and uses
that model only for the image/text alignment metric.  The other four video
metrics remain unchanged.

A raw CLIP cosine is not a 0--100 metric.  The fixed calibration below is
intentionally explicit and configurable; it is not a learned score:

    alignment = 100 * clamp((cosine - floor) / (ceiling - floor), 0, 1)

The default floor/ceiling are conservative for short, detailed video captions:
0.15 maps to 0, 0.40 maps to 100.  Every result keeps the raw cosine so a
different calibration can be applied later without rerunning CLIP.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from ..core.model_assets import bundled_model_path

DEFAULT_MODEL_PATH = bundled_model_path('video', required=False)
DEFAULT_BPE_PATH = bundled_model_path('video', 'clip/bpe_simple_vocab_16e6.txt.gz', required=False)
DEFAULT_COSINE_FLOOR = 0.15
DEFAULT_COSINE_CEILING = 0.40
DEFAULT_FRAME_SAMPLES = 8
MAX_CONTEXT_LENGTH = 77
DEFAULT_CHUNK_TOKENS = MAX_CONTEXT_LENGTH - 2
DEFAULT_CAPTION_MODE = "chunk"
_MODEL_CACHE: dict[tuple[str, str], "ClipAligner"] = {}
_MODEL_LOCK = threading.Lock()


def _sentence_parts(text: str) -> list[str]:
    """Split a long caption at sentence boundaries, keeping punctuation."""
    parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s+", text)
        if part.strip()
    ]
    return parts or [text]


def _split_long_sentence(sentence: str, tokenizer: Any, max_tokens: int) -> list[str]:
    """Word-boundary fallback when one sentence alone exceeds the text window."""
    words = sentence.split()
    if not words:
        return [sentence]
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and len(tokenizer.encode(candidate)) > max_tokens:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks or [sentence]


def _caption_chunks(text: str, tokenizer: Any, max_tokens: int = DEFAULT_CHUNK_TOKENS) -> list[str]:
    """Pack sentences into chunks no longer than ``max_tokens`` content tokens."""
    max_tokens = max(1, min(int(max_tokens), DEFAULT_CHUNK_TOKENS))
    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            chunks.append(" ".join(current))
            current.clear()

    for sentence in _sentence_parts(text):
        if len(tokenizer.encode(sentence)) > max_tokens:
            pieces = _split_long_sentence(sentence, tokenizer, max_tokens)
        else:
            pieces = [sentence]
        for piece in pieces:
            candidate = " ".join([*current, piece])
            if current and len(tokenizer.encode(candidate)) > max_tokens:
                flush()
            current.append(piece)
    flush()
    return chunks or [text]


class ClipAlignmentError(RuntimeError):
    """Raised when the local CLIP model cannot be loaded or run."""


def _safe_positive_int(value: Any, default: int, maximum: int = 24) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, number))


def _safe_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _normalise_caption(caption: str | None) -> str:
    if not isinstance(caption, str):
        return ""
    return " ".join(caption.split())


class ClipAligner:
    """Lazy, process-local CLIP aligner."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        bpe_path: str | Path | None = None,
        device: str | None = None,
        frame_samples: int = DEFAULT_FRAME_SAMPLES,
        cosine_floor: float = DEFAULT_COSINE_FLOOR,
        cosine_ceiling: float = DEFAULT_COSINE_CEILING,
        caption_mode: str = DEFAULT_CAPTION_MODE,
        chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
        combine_full: bool = True,
    ) -> None:
        self.model_path = Path(model_path or os.environ.get("SME_CLIP_MODEL") or DEFAULT_MODEL_PATH)
        self.bpe_path = Path(bpe_path or DEFAULT_BPE_PATH)
        self.device_name = device
        self.frame_samples = _safe_positive_int(frame_samples, DEFAULT_FRAME_SAMPLES, 12)
        self.cosine_floor = _safe_float(cosine_floor, DEFAULT_COSINE_FLOOR)
        self.cosine_ceiling = _safe_float(cosine_ceiling, DEFAULT_COSINE_CEILING)
        if self.cosine_ceiling <= self.cosine_floor:
            raise ValueError("cosine_ceiling must be greater than cosine_floor")
        mode = str(caption_mode or DEFAULT_CAPTION_MODE).strip().lower()
        self.caption_mode = mode if mode in {"chunk", "full"} else DEFAULT_CAPTION_MODE
        self.chunk_tokens = _safe_positive_int(
            chunk_tokens, DEFAULT_CHUNK_TOKENS, DEFAULT_CHUNK_TOKENS
        )
        self.combine_full = bool(combine_full)
        self._model = None
        self._tokenizer = None
        self._transform = None
        self._torch = None
        self._device = None
        self._lock = threading.Lock()

    @property
    def calibration(self) -> dict[str, float]:
        return {
            "floor": self.cosine_floor,
            "ceiling": self.cosine_ceiling,
        }

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            if not self.model_path.is_file():
                raise ClipAlignmentError(f"CLIP model not found: {self.model_path}")
            if not self.bpe_path.is_file():
                raise ClipAlignmentError(f"CLIP tokenizer BPE not found: {self.bpe_path}")
            try:
                import torch
                import torch.nn.functional as functional
                from torchvision import transforms
            except Exception as exc:  # pragma: no cover - dependency failure
                raise ClipAlignmentError(
                    f"CLIP runtime dependencies unavailable: {type(exc).__name__}: {exc}"
                ) from exc
            try:
                from . import _clip_model, _clip_tokenizer

                # torch.jit.load cannot open a narrow Windows path containing
                # non-ASCII characters; a binary file object bypasses that.
                with self.model_path.open("rb") as handle:
                    jit_model = torch.jit.load(handle, map_location="cpu")
                state_dict = jit_model.state_dict()
                del jit_model
                model = _clip_model.build_model(state_dict).cpu().eval()
                # build_model() applies OpenAI CLIP's fp16 conversion.  CPU
                # inference must be fp32; clip.load() does the same float()
                # conversion when the target device is CPU.
                model = model.float()
                tokenizer = _clip_tokenizer.SimpleTokenizer(str(self.bpe_path))
                transform = transforms.Compose([
                    transforms.Resize(
                        224, interpolation=transforms.InterpolationMode.BICUBIC
                    ),
                    transforms.CenterCrop(224),
                    transforms.ToTensor(),
                    transforms.Normalize(
                        (0.48145466, 0.4578275, 0.40821073),
                        (0.26862954, 0.26130258, 0.27577711),
                    ),
                ])
            except Exception as exc:
                raise ClipAlignmentError(
                    f"CLIP model load failed: {type(exc).__name__}: {exc}"
                ) from exc

            device = torch.device(self.device_name or ("cuda" if torch.cuda.is_available() else "cpu"))
            model = model.to(device)
            model.eval()
            self._torch = torch
            self._model = model
            self._tokenizer = tokenizer
            self._transform = transform
            self._device = device

    def _token_ids(self, caption: str) -> "Any":
        self._load()
        assert self._tokenizer is not None
        start = self._tokenizer.encoder["<|startoftext|>"]
        end = self._tokenizer.encoder["<|endoftext|>"]
        tokens = self._tokenizer.encode(caption)
        ids = [start, *tokens[: MAX_CONTEXT_LENGTH - 2], end]
        ids = ids[:MAX_CONTEXT_LENGTH]
        ids += [0] * (MAX_CONTEXT_LENGTH - len(ids))
        assert self._torch is not None
        return self._torch.tensor([ids], dtype=self._torch.long, device=self._device)

    def _frame_indices(self, count: int, take: int | None = None) -> list[int]:
        if count <= 0:
            return []
        take = min(take if take is not None else self.frame_samples, count)
        if take == 1:
            return [count // 2]
        return sorted({int(value) for value in np.linspace(0, count - 1, take)})

    def _caption_chunks(self, text: str) -> list[str]:
        self._load()
        assert self._tokenizer is not None
        return _caption_chunks(text, self._tokenizer, self.chunk_tokens)

    def score_frames(
        self,
        frames: Sequence[np.ndarray],
        caption: str | None,
        *,
        frame_samples: int | None = None,
    ) -> dict[str, Any]:
        """Return CLIP cosine and calibrated alignment for sampled video frames.

        In ``chunk`` mode the caption is split at sentence/word boundaries into
        <= ``chunk_tokens`` pieces, each frame is matched to its best caption
        chunk, and those per-frame maxima are averaged.  The final cosine is the
        higher of that chunk score and the truncated full-caption score, so a
        formerly strong short/first-sentence match is not lost by chunking.
        """
        text = _normalise_caption(caption)
        if not text:
            return {
                "alignment": 60.0,
                "cosine": None,
                "fullCosine": None,
                "chunkCosine": None,
                "framesUsed": 0,
                "frameIndices": [],
                "status": "caption_missing",
                "method": "clip-vit-l14-cosine-v1",
                "captionMode": self.caption_mode,
                "chunkCount": 0,
                "chunkTokens": [],
                "calibration": self.calibration,
            }
        if not frames:
            raise ClipAlignmentError("cannot score CLIP alignment without decoded frames")
        self._load()
        assert self._torch is not None and self._model is not None
        assert self._transform is not None

        take = _safe_positive_int(
            frame_samples if frame_samples is not None else self.frame_samples,
            self.frame_samples,
            12,
        )
        indices = self._frame_indices(len(frames), take)
        if not indices:
            raise ClipAlignmentError("no video frames selected for CLIP")
        import cv2
        from PIL import Image

        tensors = []
        for index in indices:
            rgb = cv2.cvtColor(frames[index], cv2.COLOR_BGR2RGB)
            tensors.append(self._transform(Image.fromarray(rgb)))
        batch = self._torch.stack(tensors).to(self._device)
        chunks = self._caption_chunks(text) if self.caption_mode == "chunk" else []
        with self._torch.inference_mode():
            image_features = self._model.encode_image(batch)
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)

            full_features = self._model.encode_text(self._token_ids(text))
            full_features = full_features / full_features.norm(dim=-1, keepdim=True)
            full_cosine = float((image_features @ full_features.T).mean().item())

            chunk_cosine = None
            if chunks:
                text_ids = self._torch.cat(
                    [self._token_ids(chunk) for chunk in chunks], dim=0
                )
                chunk_features = self._model.encode_text(text_ids)
                chunk_features = chunk_features / chunk_features.norm(dim=-1, keepdim=True)
                matrix = image_features @ chunk_features.T
                chunk_cosine = float(matrix.max(dim=1).values.mean().item())

            if chunk_cosine is None:
                cosine = full_cosine
            elif self.combine_full:
                cosine = max(full_cosine, chunk_cosine)
            else:
                cosine = chunk_cosine

        span = self.cosine_ceiling - self.cosine_floor
        alignment = 100.0 * max(0.0, min(1.0, (cosine - self.cosine_floor) / span))
        method = "clip-vit-l14-chunk-frame-max-v1" if chunks and self.caption_mode == "chunk" else "clip-vit-l14-cosine-v1"
        return {
            "alignment": round(alignment, 2),
            "cosine": round(cosine, 6),
            "fullCosine": round(full_cosine, 6),
            "chunkCosine": None if chunk_cosine is None else round(chunk_cosine, 6),
            "framesUsed": len(indices),
            "frameIndices": indices,
            "status": "ok",
            "method": method,
            "captionMode": self.caption_mode,
            "chunkCount": len(chunks),
            "chunkTokens": [
                len(self._tokenizer.encode(chunk)) for chunk in chunks
            ] if chunks else [],
            "combineFull": self.combine_full,
            "calibration": self.calibration,
        }


def get_aligner(
    model_path: str | Path | None = None,
    *,
    device: str | None = None,
    frame_samples: int = DEFAULT_FRAME_SAMPLES,
    cosine_floor: float = DEFAULT_COSINE_FLOOR,
    cosine_ceiling: float = DEFAULT_COSINE_CEILING,
    caption_mode: str = DEFAULT_CAPTION_MODE,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    combine_full: bool = True,
) -> ClipAligner:
    """Return a cached aligner for a model path and device."""
    resolved = str(Path(model_path or os.environ.get("SME_CLIP_MODEL") or DEFAULT_MODEL_PATH))
    key = (resolved, str(device or ""))
    mode = str(caption_mode or DEFAULT_CAPTION_MODE).strip().lower()
    mode = mode if mode in {"chunk", "full"} else DEFAULT_CAPTION_MODE
    with _MODEL_LOCK:
        aligner = _MODEL_CACHE.get(key)
        if aligner is None:
            aligner = ClipAligner(
                resolved,
                device=device,
                frame_samples=frame_samples,
                cosine_floor=cosine_floor,
                cosine_ceiling=cosine_ceiling,
                caption_mode=mode,
                chunk_tokens=chunk_tokens,
                combine_full=combine_full,
            )
            _MODEL_CACHE[key] = aligner
        else:
            aligner.frame_samples = _safe_positive_int(
                frame_samples, DEFAULT_FRAME_SAMPLES, 12
            )
            aligner.cosine_floor = _safe_float(cosine_floor, DEFAULT_COSINE_FLOOR)
            aligner.cosine_ceiling = _safe_float(cosine_ceiling, DEFAULT_COSINE_CEILING)
            aligner.caption_mode = mode
            aligner.chunk_tokens = _safe_positive_int(
                chunk_tokens, DEFAULT_CHUNK_TOKENS, DEFAULT_CHUNK_TOKENS
            )
            aligner.combine_full = bool(combine_full)
            if aligner.cosine_ceiling <= aligner.cosine_floor:
                raise ValueError("cosine_ceiling must be greater than cosine_floor")
        return aligner


def align_frames(
    frames: Sequence[np.ndarray],
    caption: str | None,
    *,
    model_path: str | Path | None = None,
    frame_samples: int = DEFAULT_FRAME_SAMPLES,
    cosine_floor: float = DEFAULT_COSINE_FLOOR,
    cosine_ceiling: float = DEFAULT_COSINE_CEILING,
    caption_mode: str = DEFAULT_CAPTION_MODE,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    combine_full: bool = True,
) -> dict[str, Any]:
    """Convenience entry point used by the video engine and test runner."""
    aligner = get_aligner(
        model_path,
        frame_samples=frame_samples,
        cosine_floor=cosine_floor,
        cosine_ceiling=cosine_ceiling,
        caption_mode=caption_mode,
        chunk_tokens=chunk_tokens,
        combine_full=combine_full,
    )
    return aligner.score_frames(frames, caption, frame_samples=frame_samples)
