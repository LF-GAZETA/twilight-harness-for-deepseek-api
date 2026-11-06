"""Python port of the NTSC-RS RNG / Simplex2D / edge-wave math.

Used to precompute per-frame row shifts and chroma-loss rows on the host; the
OpenCL kernels then only have to apply integer buffers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

MASK64 = (1 << 64) - 1
PHI = 0x9E3779B97F4A7C15
EDGE_WAVE = 5
CHROMA_LOSS = 7


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


class SplitMix64:
    def __init__(self, seed: int):
        self.state = seed & MASK64

    def random_u64(self) -> int:
        self.state = (self.state + PHI) & MASK64
        s = self.state
        s = ((s ^ (s >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        s = ((s ^ (s >> 27)) * 0x94D049BB133111EB) & MASK64
        return (s ^ (s >> 31)) & MASK64

    def random_u32(self) -> int:
        self.state = (self.state + PHI) & MASK64
        s = self.state
        s = ((s ^ (s >> 33)) * 0x62A9D9ED799705F5) & MASK64
        s = ((s ^ (s >> 28)) * 0xCB24D0A5C88C35B3) & MASK64
        return (s >> 32) & 0xFFFFFFFF

    def random_i32(self) -> int:
        v = self.random_u32()
        return v - (1 << 32) if v & 0x80000000 else v

    def random_f32(self) -> float:
        return (self.random_u32() >> 8) * (1.0 / (1 << 24))

    def random_f64(self) -> float:
        return (self.random_u64() >> 11) * (1.0 / (1 << 53))

    def mix(self, value: int) -> "SplitMix64":
        self.state = (self.state + value) & MASK64
        self.state = self.random_u64()
        return self

    def geometric(self, intensity: float) -> int:
        lam = math.log(1.0 - intensity)
        return int(math.log(self.random_f64()) / lam)


def _pcg3d(x: int, y: int, z: int):
    x, y, z = _u32(x), _u32(y), _u32(z)
    x = _u32(x * 1664525 + 1013904223)
    y = _u32(y * 1664525 + 1013904223)
    z = _u32(z * 1664525 + 1013904223)
    x = _u32(x + y * z)
    y = _u32(y + z * x)
    z = _u32(z + x * y)
    x ^= x >> 16
    y ^= y >> 16
    z ^= z >> 16
    x = _u32(x + y * z)
    y = _u32(y + z * x)
    z = _u32(z + x * y)
    return x, y, z


def _gradient_2d(h: int):
    h &= 7
    m = h < 4
    xm = 1.0 if m else 2.0
    ym = 2.0 if m else 1.0
    if m:
        gx = xm if (h & 1) == 0 else -xm
        gy = ym if (h & 2) == 0 else -ym
    else:
        gx = xm if (h & 2) == 0 else -xm
        gy = ym if (h & 1) == 0 else -ym
    return gx, gy


def simplex_2d(x: float, y: float, seed: int) -> float:
    skew = (math.sqrt(3.0) - 1.0) * 0.5
    unskew = -(1.0 / math.sqrt(3.0) - 1.0) * 0.5
    s = (x + y) * skew
    ips = math.floor(x + s)
    jps = math.floor(y + s)
    i, j = int(ips), int(jps)
    t = (i + j) * unskew
    x0 = x - (ips - t)
    y0 = y - (jps - t)
    i1 = 1 if x0 >= y0 else 0
    j1 = 1 if y0 > x0 else 0
    x1 = x0 - i1 + unskew
    y1 = y0 - j1 + unskew
    x2 = x0 - 1.0 + 2.0 * unskew
    y2 = y0 - 1.0 + 2.0 * unskew
    h0 = _pcg3d(i, j, seed)[0]
    h1 = _pcg3d(i + i1, j + j1, seed)[0]
    h2 = _pcg3d(i + 1, j + 1, seed)[0]
    g0 = _gradient_2d(h0)
    g1 = _gradient_2d(h1)
    g2 = _gradient_2d(h2)
    t0 = max(0.0, 0.5 - x0 * x0 - y0 * y0)
    t1 = max(0.0, 0.5 - x1 * x1 - y1 * y1)
    t2 = max(0.0, 0.5 - x2 * x2 - y2 * y2)
    n0 = (t0 ** 4) * (g0[0] * x0 + g0[1] * y0)
    n1 = (t1 ** 4) * (g1[0] * x1 + g1[1] * y1)
    n2 = (t2 ** 4) * (g2[0] * x2 + g2[1] * y2)
    return n0 + n1 + n2


def fbm_2d(x, y, seed, octaves, gain, frequency):
    px, py = x * frequency, y * frequency
    result = simplex_2d(px, py, seed)
    amp = gain
    for _ in range(1, max(1, octaves)):
        px *= 2.0
        py *= 2.0
        result += simplex_2d(px, py, seed) * amp
        amp *= gain
    return result


@dataclass
class EdgeWave:
    intensity: float = 0.5
    speed: float = 4.0
    frequency: float = 0.05
    detail: int = 2


def _pcg3d_np(x, y, z):
    x = np.asarray(x, dtype=np.uint32)
    y = np.asarray(y, dtype=np.uint32)
    z = np.asarray(z, dtype=np.uint32)
    x = x * np.uint32(1664525) + np.uint32(1013904223)
    y = y * np.uint32(1664525) + np.uint32(1013904223)
    z = z * np.uint32(1664525) + np.uint32(1013904223)
    x = x + y * z
    y = y + z * x
    z = z + x * y
    x ^= x >> np.uint32(16)
    y ^= y >> np.uint32(16)
    z ^= z >> np.uint32(16)
    x = x + y * z
    y = y + z * x
    z = z + x * y
    return x, y, z


def _gradient_np(h):
    h = h & np.uint32(7)
    m = h < np.uint32(4)
    xm = np.where(m, 1.0, 2.0)
    ym = np.where(m, 2.0, 1.0)
    gx = np.where(m, np.where((h & 1) == 0, xm, -xm),
                  np.where((h & 2) == 0, xm, -xm))
    gy = np.where(m, np.where((h & 2) == 0, ym, -ym),
                  np.where((h & 1) == 0, ym, -ym))
    return gx, gy


def simplex_2d_np(x, y, seed: int):
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    skew = (math.sqrt(3.0) - 1.0) * 0.5
    unskew = -(1.0 / math.sqrt(3.0) - 1.0) * 0.5
    s = (x + y) * skew
    ips = np.floor(x + s)
    jps = np.floor(y + s)
    i, j = ips.astype(np.int64), jps.astype(np.int64)
    t = (i + j) * unskew
    x0 = x - (ips - t)
    y0 = y - (jps - t)
    i1 = (x0 >= y0).astype(np.int64)
    j1 = (y0 > x0).astype(np.int64)
    x1 = x0 - i1 + unskew
    y1 = y0 - j1 + unskew
    x2 = x0 - 1.0 + 2.0 * unskew
    y2 = y0 - 1.0 + 2.0 * unskew
    h0 = _pcg3d_np(i, j, np.full_like(i, seed, dtype=np.uint32))[0]
    h1 = _pcg3d_np(i + i1, j + j1, np.full_like(i, seed, dtype=np.uint32))[0]
    h2 = _pcg3d_np(i + 1, j + 1, np.full_like(i, seed, dtype=np.uint32))[0]
    gx0, gy0 = _gradient_np(h0)
    gx1, gy1 = _gradient_np(h1)
    gx2, gy2 = _gradient_np(h2)
    t0 = np.maximum(0.0, 0.5 - x0 * x0 - y0 * y0)
    t1 = np.maximum(0.0, 0.5 - x1 * x1 - y1 * y1)
    t2 = np.maximum(0.0, 0.5 - x2 * x2 - y2 * y2)
    return (t0 ** 4) * (gx0 * x0 + gy0 * y0) + (t1 ** 4) * (
        gx1 * x1 + gy1 * y1
    ) + (t2 ** 4) * (gx2 * x2 + gy2 * y2)


def edge_shifts_np(seed: int, frame: int, height: int, hscale: float,
                   settings: EdgeWave | None = None) -> np.ndarray:
    st = settings or EdgeWave()
    rng = SplitMix64(seed).mix(EDGE_WAVE)
    noise_seed = rng.random_i32()
    offset = rng.random_f32() * height
    xs = offset + np.arange(height, dtype=np.float64)
    y = frame * st.speed
    freq = st.frequency
    px = xs * freq
    py = y * freq
    gain = 1.0 / math.sqrt(2.0)
    result = simplex_2d_np(px, py, noise_seed)
    amp = gain
    for _ in range(1, max(1, min(5, st.detail))):
        px *= 2.0
        py *= 2.0
        result += simplex_2d_np(px, py, noise_seed) * amp
        amp *= gain
    return ((result / 0.022) * st.intensity * 0.5 * hscale).astype(np.float32)


def edge_shifts(seed: int, frame: int, height: int, hscale: float,
                settings: EdgeWave | None = None) -> list[float]:
    st = settings or EdgeWave()
    rng = SplitMix64(seed).mix(EDGE_WAVE)
    noise_seed = rng.random_i32()
    offset = rng.random_f32() * height
    shifts = []
    for row in range(height):
        n = fbm_2d(offset + row, frame * st.speed, noise_seed,
                   st.detail, 1.0 / math.sqrt(2.0), st.frequency)
        shifts.append((n / 0.022) * st.intensity * 0.5 * hscale)
    return shifts


def chroma_loss_rows(seed: int, frame: int, height: int, intensity: float) -> list[int]:
    if intensity <= 0.0:
        return []
    rng = SplitMix64(seed).mix(CHROMA_LOSS).mix(frame)
    lam = math.log(1.0 - intensity)
    rows = []
    idx = 0
    while True:
        idx += int(math.log(rng.random_f64()) / lam)
        if idx >= height:
            break
        rows.append(idx)
        idx += 1
    return rows
