"""Pure-PyTorch CPU Real-ESRGAN (RRDBNet) fallback."""
from __future__ import annotations

import os
from pathlib import Path
import numpy as np
import torch
from torch import nn as nn
from torch.nn import functional as F


class ResidualDenseBlock(nn.Module):
    def __init__(self, num_feat=64, num_grow_ch=32):
        super().__init__()
        self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
        self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x


class RRDB(nn.Module):
    def __init__(self, num_feat, num_grow_ch=32):
        super().__init__()
        self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

    def forward(self, x):
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        return out * 0.2 + x


class RRDBNet(nn.Module):
    def __init__(self, num_in_ch=3, num_out_ch=3, scale=4, num_feat=64,
                 num_block=6, num_grow_ch=32):
        super().__init__()
        self.scale = scale
        self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
        self.body = nn.Sequential(*[RRDB(num_feat, num_grow_ch) for _ in range(num_block)])
        self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        feat = self.conv_first(x)
        feat = feat + self.conv_body(self.body(feat))
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
        feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
        return self.conv_last(self.lrelu(self.conv_hr(feat)))


def model_path() -> Path | None:
    explicit = os.environ.get("TWILIGHT_CPU_ESRGAN_MODEL", "").strip()
    candidates = [Path(explicit)] if explicit else []
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        candidates.append(Path(ai_home) / "models" / "realesrgan" / "RealESRGAN_x4plus_anime_6B.pth")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "_ai_install" / "TwilightHarnessPortable" / "ai" / "models" / "realesrgan" / "RealESRGAN_x4plus_anime_6B.pth")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_model():
    path = model_path()
    if path is None:
        raise FileNotFoundError("CPU Real-ESRGAN model not found")
    model = RRDBNet(num_block=6)
    state = torch.load(path, map_location="cpu", weights_only=False)
    state = state.get("params_ema", state.get("params", state))
    model.load_state_dict(state, strict=False)
    model.eval()
    return model


def upscale_image(model, image):
    from PIL import Image
    array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    tensor = torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0)
    with torch.no_grad():
        output = model(tensor)
    output = output.clamp(0, 1).squeeze(0).permute(1, 2, 0).numpy()
    return Image.fromarray((output * 255.0).round().astype(np.uint8))


def upscale_file(model, source: Path, target: Path) -> None:
    from PIL import Image
    image = Image.open(source).convert("RGB")
    upscale_image(model, image).save(target)
