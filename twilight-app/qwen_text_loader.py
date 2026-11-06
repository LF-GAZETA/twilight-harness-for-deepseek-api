"""Custom Qwen-Image-2.1 text loader for low-VRAM dual-INT4 mode.

The standard loader keeps the full Qwen3-VL text encoder: BF16 token
embeddings, a 1.16 GiB LM head, and a vision tower.  Text-to-image does not
need the LM head or the vision tower, and the token embedding can be stored as
INT8/INT4.  This loader trims those parts before moving the model to the GPU.

It is deliberately separate from the normal Qwen runtime: the trimmed model is
only valid for text-only prompt encoding.
"""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Literal

import torch
import torch.nn.functional as F
from transformers import BitsAndBytesConfig, Qwen3VLForConditionalGeneration


class QuantizedEmbedding(torch.nn.Module):
    """Asymmetric per-row quantized replacement for ``nn.Embedding``."""

    def __init__(self, source: torch.nn.Embedding, bits: Literal[4, 8] = 8):
        super().__init__()
        if bits not in (4, 8):
            raise ValueError("bits must be 4 or 8")
        self.bits = bits
        self.n_embd = source.embedding_dim
        weight = source.weight.detach().float()
        low = weight.amin(dim=1, keepdim=True)
        high = weight.amax(dim=1, keepdim=True)
        levels = (1 << bits) - 1
        scale = ((high - low) / levels).clamp_min(1e-8)
        zero = torch.round(-low / scale).clamp(0, levels)
        codes = torch.clamp(torch.round(weight / scale) + zero, 0, levels).to(torch.uint8)
        self.register_buffer("scale", scale)
        self.register_buffer("zero", zero)
        if bits == 8:
            self.register_buffer("codes", codes)
        else:
            self.register_buffer("codes", (codes[:, 0::2] << 4) | codes[:, 1::2])

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        packed = F.embedding(input_ids, self.codes)
        if self.bits == 4:
            codes = torch.stack((packed >> 4, packed & 0x0F), dim=-1).reshape(*input_ids.shape, -1)
        else:
            codes = packed
        zero = self.zero[input_ids]
        scale = self.scale[input_ids]
        return ((codes.float() - zero.float()) * scale.float()).to(self.scale.dtype)


class QwenTextLoader:
    """Load only the text-conditioning part of Qwen-Image 2.1."""

    def __init__(
        self,
        text_model_dir: str | Path,
        *,
        device: str | torch.device = "cuda",
        embedding_bits: Literal[4, 8] = 8,
        trim_lm_head: bool = True,
        trim_vision: bool = True,
    ) -> None:
        self.text_model_dir = Path(text_model_dir)
        self.device = torch.device(device)
        self.embedding_bits = embedding_bits
        self.trim_lm_head = trim_lm_head
        self.trim_vision = trim_vision

    def load(self) -> Qwen3VLForConditionalGeneration:
        if not (self.text_model_dir / "config.json").is_file():
            raise FileNotFoundError(f"text model not found: {self.text_model_dir}")

        quant = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        # Load on CPU first: trimming and embedding quantization must not
        # temporarily allocate the full model on the GPU.
        model = Qwen3VLForConditionalGeneration.from_pretrained(
            self.text_model_dir,
            torch_dtype=torch.bfloat16,
            quantization_config=quant,
            device_map="cpu",
            low_cpu_mem_usage=True,
            local_files_only=True,
        )

        if self.trim_lm_head:
            del model.lm_head
            model.lm_head = torch.nn.Identity()
        if self.trim_vision and hasattr(model.model, "visual"):
            del model.model.visual
            model.model.visual = None

        old_embedding = model.get_input_embeddings()
        if not isinstance(old_embedding, torch.nn.Embedding):
            raise TypeError(f"unexpected input embedding: {type(old_embedding)!r}")
        model.model.language_model.embed_tokens = QuantizedEmbedding(old_embedding, self.embedding_bits)
        del old_embedding
        gc.collect()

        model.to(self.device)
        model.eval()
        if torch.cuda.is_available() and self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        return model
