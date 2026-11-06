"""OpenCL full-resolution YIQ VHS renderer.

This is the opt-in GPU VHS path. It is an approximate renderer, not bit-exact
NTSC-RS parity; the CPU NTSC-RS path remains the default/reference.

The module is imported lazily so a release can still start on machines without
OpenCL/pyopencl.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from .ntsc_rng import EdgeWave, chroma_loss_rows, edge_shifts_np

# pyopencl's cache-error handler reads this key directly. An unset key turns a
# benign stale-cache-lock warning into a KeyError, which would incorrectly
# disable the whole GPU path. An empty value means "warn and build from source".
os.environ.setdefault("PYOPENCL_CACHE_FAILURE_FATAL", "")

NTSC_RATE = (315_000_000.0 / 88.0) * 4.0


class OpenCLVhsError(RuntimeError):
    pass


class OpenCLVhsCancelled(OpenCLVhsError):
    pass


KERNEL = r"""
inline uint hash1d(uint v)
{
    v = (v ^ 2747636419u) * 2654435769u;
    v = (v ^ (v >> 16)) * 2654435769u;
    v = (v ^ (v >> 16)) * 2654435769u;
    return v;
}

inline float grad1d(uint h)
{
    float mag = (float)(((h >> 28) & 7u) + 1u);
    return (h & 0x80000000u) ? -mag : mag;
}

// Port of NTSC-RS's simplex_1d + ESGTSA hash.
inline float simplex1d(float x, uint seed)
{
    float i = floor(x);
    int i0 = (int)i;
    int i1 = i0 + 1;
    float x0 = x - i;
    float x1 = x0 - 1.0f;
    uint g0 = hash1d((uint)i0 ^ seed);
    uint g1 = hash1d((uint)i1 ^ seed);
    float t0 = 1.0f - x0 * x0;
    float t1 = 1.0f - x1 * x1;
    float n0 = t0 * t0 * t0 * t0 * grad1d(g0) * x0;
    float n1 = t1 * t1 * t1 * t1 * grad1d(g1) * x1;
    return n0 + n1;
}

inline float fbm1(float x, uint seed, int octaves, float freq)
{
    float p = x * freq;
    float result = simplex1d(p, seed);
    float amp = 1.0f;
    for (int o = 1; o < octaves; ++o) {
        p *= 2.0f;
        result += simplex1d(p, seed) * amp;
        amp *= 1.0f;
    }
    return result;
}

__kernel void rgb_to_yiq(
    __global const uchar *rgb,
    __global float *y_out, __global float *i_out, __global float *q_out,
    const int w, const int h)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int p = y * w + x;
    int rp = p * 3;
    float r = (float)rgb[rp];
    float g = (float)rgb[rp + 1];
    float b = (float)rgb[rp + 2];
    y_out[p] = 0.299f * r + 0.587f * g + 0.114f * b;
    i_out[p] = 0.5959f * r - 0.2746f * g - 0.3213f * b;
    q_out[p] = 0.2115f * r - 0.5227f * g + 0.3112f * b;
}

__kernel void iir_plane(
    __global const float *src, __global float *dst,
    const int w, const int h, const float alpha, const int delay)
{
    int y = get_global_id(0);
    if (y >= h) return;
    int base = y * w;
    float s1 = 0.0f, s2 = 0.0f, s3 = 0.0f;
    float a = alpha, b = 1.0f - alpha;
    for (int x = 0; x < w; ++x) {
        float in = src[base + x];
        s1 = a * in + b * s1;
        s2 = a * s1 + b * s2;
        s3 = a * s2 + b * s3;
        dst[base + x] = (x >= delay) ? s3 : 0.0f;
    }
}

__kernel void biquad_plane(
    __global const float *src, __global float *dst,
    const int w, const int h,
    const float b0, const float b1, const float b2,
    const float a1, const float a2,
    const int delay, const int first_sample)
{
    int y = get_global_id(0);
    if (y >= h) return;
    int base = y * w;
    float x1 = first_sample ? src[base] : 0.0f;
    float x2 = x1;
    float y1 = 0.0f, y2 = 0.0f;
    for (int i = 0; i < w + delay; ++i) {
        int xi = i < w ? i : (w - 1);
        float xn = src[base + xi];
        float yn = b0 * xn + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
        if (i >= delay) {
            dst[base + i - delay] = yn;
        }
        x2 = x1; x1 = xn; y2 = y1; y1 = yn;
    }
}

__kernel void preemphasis_plane(
    __global const float *src, __global float *dst,
    const int w, const int h, const float alpha, const float scale)
{
    int y = get_global_id(0);
    if (y >= h) return;
    int base = y * w;
    float s = 0.0f;
    float a = alpha, b = 1.0f - alpha;
    for (int x = 0; x < w; ++x) {
        s = a * src[base + x] + b * s;
        dst[base + x] = src[base + x] + scale * (s - src[base + x]);
    }
}

__kernel void edge_shift(
    __global const float *sy, __global const float *si, __global const float *sq,
    __global float *dy, __global float *di, __global float *dq,
    __global const float *shifts, const int w, const int h)
{
    int y = get_global_id(0);
    if (y >= h) return;
    int base = y * w;
    float shift = shifts[y];
    int sint = (int)floor(shift);
    float frac = shift - (float)sint;
    float by = sint >= 0 ? sy[base] : sy[base + w - 1];
    float bi = sint >= 0 ? si[base] : si[base + w - 1];
    float bq = sint >= 0 ? sq[base] : sq[base + w - 1];
    for (int x = 0; x < w; ++x) {
        int li = x - sint - 1;
        int ri = li + 1;
        float ly = (li >= 0 && li < w) ? sy[base + li] : by;
        float ry = (ri >= 0 && ri < w) ? sy[base + ri] : by;
        float lii = (li >= 0 && li < w) ? si[base + li] : bi;
        float rii = (ri >= 0 && ri < w) ? si[base + ri] : bi;
        float lq = (li >= 0 && li < w) ? sq[base + li] : bq;
        float rq = (ri >= 0 && ri < w) ? sq[base + ri] : bq;
        dy[base + x] = ly * frac + ry * (1.0f - frac);
        di[base + x] = lii * frac + rii * (1.0f - frac);
        dq[base + x] = lq * frac + rq * (1.0f - frac);
    }
}

__kernel void blank_rows(
    __global float *i_plane, __global float *q_plane,
    __global const uchar *mask, const int w, const int h)
{
    int y = get_global_id(0);
    if (y >= h || mask[y] == 0) return;
    int base = y * w;
    for (int x = 0; x < w; ++x) {
        i_plane[base + x] = 0.0f;
        q_plane[base + x] = 0.0f;
    }
}

__kernel void iir1_plane(
    __global const float *src, __global float *dst,
    const int w, const int h, const float alpha)
{
    int y = get_global_id(0);
    if (y >= h) return;
    int base = y * w;
    float s = 0.0f;
    float a = alpha, b = 1.0f - alpha;
    for (int x = 0; x < w; ++x) {
        s = a * src[base + x] + b * s;
        dst[base + x] = s;
    }
}

__kernel void iir3_plane(
    __global const float *src, __global float *dst,
    const int w, const int h, const float alpha, const int delay)
{
    int y = get_global_id(0);
    if (y >= h) return;
    int base = y * w;
    float s1 = 0.0f, s2 = 0.0f, s3 = 0.0f;
    float a = alpha, b = 1.0f - alpha;
    for (int i = 0; i < w + delay; ++i) {
        int xi = i < w ? i : (w - 1);
        float xn = src[base + xi];
        s1 = a * xn + b * s1;
        s2 = a * s1 + b * s2;
        s3 = a * s2 + b * s3;
        if (i >= delay) {
            dst[base + i - delay] = s3;
        }
    }
}

__kernel void modulate(
    __global const float *y_in, __global const float *i_in,
    __global const float *q_in, __global float *comp,
    const int w, const int h, const int frame)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int p = y * w + x;
    int phase = (frame + (y << 1)) & 2;
    int ph = (phase + x) & 3;
    float im = (ph == 0) ? 1.0f : ((ph == 2) ? -1.0f : 0.0f);
    float qm = (ph == 1) ? 1.0f : ((ph == 3) ? -1.0f : 0.0f);
    comp[p] = y_in[p] + i_in[p] * im + q_in[p] * qm;
}

__kernel void composite_noise(
    __global float *comp, const int w, const int h, const int frame,
    const uint seed, const float grain, const float snow_p,
    const float tracking, const float head_shift)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int p = y * w + x;
    uint hsh = (uint)(x * 73856093) ^ (uint)(y * 19349663)
             ^ (uint)(frame * 83492791) ^ seed;
    hsh ^= hsh >> 13; hsh *= 1274126177u; hsh ^= hsh >> 16;
    comp[p] += ((float)(hsh & 255u) / 255.0f - 0.5f) * grain;

    if (tracking > 0.0f) {
        int band_y = (int)((frame * 5 + 19) % (uint)max(1, h));
        int band_h = max(3, h / 45);
        if (abs(y - band_y) < band_h) {
            float tr = ((float)(hsh & 1023u) / 1023.0f - 0.5f) * tracking;
            // Keep this additive. Blending composite with a neighbouring
            // pixel introduces a strong DC/phase error, which shows up as a
            // bright horizontal band after demodulation.
            comp[p] += tr;
        }
    }
    int band = max(3, h / 45);
    if (y > h - band) {
        int shift = (int)head_shift * ((frame % 3) - 1);
        int xx = clamp(x + shift, 0, w - 1);
        comp[p] = 0.55f * comp[p] + 0.45f * comp[y * w + xx];
    }
}

__kernel void snow_luma(
    __global float *y_plane, const int w, const int h, const int frame,
    const uint seed, const float density)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int p = y * w + x;
    // Short horizontal transients, like VHS snow. Use 8-pixel cells so the
    // speckles read as white streaks rather than single-pixel chroma noise.
    int cell = x >> 3;
    uint hsh = (uint)cell * 2246822519u ^ (uint)y * 3266489917u
             ^ (uint)frame * 668265263u ^ seed;
    hsh ^= hsh >> 15; hsh *= 2654435761u; hsh ^= hsh >> 13;
    if ((hsh & 0xFFFFu) < (uint)(density * 65535.0f)) {
        float add = 40.0f + (float)((hsh >> 16) & 127u);
        y_plane[p] = min(255.0f, y_plane[p] + add);
    }
}

__kernel void demod(
    __global const float *y_in, __global const float *comp,
    __global float *i_out, __global float *q_out,
    const int w, const int h, const int frame)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int p = y * w + x;
    int phase = (frame + (y << 1)) & 2;
    int pc = (phase + x) & 3;
    int pl = (phase + max(0, x - 1)) & 3;
    int pr = (phase + min(w - 1, x + 1)) & 3;
    float cc = y_in[p] - comp[p];
    float cl = y_in[y * w + max(0, x - 1)] - comp[y * w + max(0, x - 1)];
    float cr = y_in[y * w + min(w - 1, x + 1)] - comp[y * w + min(w - 1, x + 1)];
    float ic = (pc == 0) ? -1.0f : ((pc == 2) ? 1.0f : 0.0f);
    float qc = (pc == 1) ? -1.0f : ((pc == 3) ? 1.0f : 0.0f);
    float il = (pl == 0) ? -1.0f : ((pl == 2) ? 1.0f : 0.0f);
    float ql = (pl == 1) ? -1.0f : ((pl == 3) ? 1.0f : 0.0f);
    float ir = (pr == 0) ? -1.0f : ((pr == 2) ? 1.0f : 0.0f);
    float qr = (pr == 1) ? -1.0f : ((pr == 3) ? 1.0f : 0.0f);
    i_out[p] = cc * ic + cl * il * 0.5f + cr * ir * 0.5f;
    q_out[p] = cc * qc + cl * ql * 0.5f + cr * qr * 0.5f;
}

__kernel void vert_blend(
    __global const float *i_in, __global const float *q_in,
    __global float *i_out, __global float *q_out, const int w, const int h)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int p = y * w + x;
    if (y == 0) { i_out[p] = i_in[p]; q_out[p] = q_in[p]; }
    else {
        i_out[p] = 0.5f * (i_in[p] + i_in[p - w]);
        q_out[p] = 0.5f * (q_in[p] + q_in[p - w]);
    }
}

__kernel void luma_smear_row(
    __global const float *src, __global float *dst,
    const int w, const int h, const int frame,
    const int radius, const float mix, const uint seed,
    const float row_amp, const float pixel_amp)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    float acc = 0.0f;
    int count = 0;
    for (int dx = -radius; dx <= radius; ++dx) {
        acc += src[y * w + clamp(x + dx, 0, w - 1)];
        ++count;
    }
    float avg = acc / (float)count;
    uint hsh = (uint)(x >> 2) * 2246822519u ^ (uint)y * 3266489917u
             ^ (uint)frame * 668265263u ^ seed;
    hsh ^= hsh >> 15; hsh *= 2654435761u; hsh ^= hsh >> 13;
    float rn = ((float)(hsh & 255u) / 255.0f - 0.5f) * row_amp;
    uint ph = (uint)x * 1597334677u ^ (uint)y * 3812015801u
            ^ (uint)frame * 2246822519u ^ (seed + 7u);
    ph ^= ph >> 15; ph *= 2654435761u; ph ^= ph >> 13;
    float pn = ((float)(ph & 255u) / 255.0f - 0.5f) * pixel_amp;
    dst[y * w + x] = clamp(mix * avg + (1.0f - mix) * src[y * w + x] + rn + pn,
                           0.0f, 255.0f);
}

__kernel void chroma_row_noise(
    __global float *i_plane, __global float *q_plane,
    const int w, const int h, const int frame, const uint seed,
    const float amp, const float gain)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    uint hsh_i = (uint)(x >> 2) * 3266489917u ^ (uint)y * 668265263u
               ^ (uint)frame * 2246822519u ^ seed;
    uint hsh_q = (uint)(x >> 2) * 2246822519u ^ (uint)(y + 17) * 3266489917u
               ^ (uint)frame * 668265263u ^ (seed + 1u);
    hsh_i ^= hsh_i >> 14; hsh_i *= 2246822519u; hsh_i ^= hsh_i >> 13;
    hsh_q ^= hsh_q >> 14; hsh_q *= 3266489917u; hsh_q ^= hsh_q >> 13;
    float ni = ((float)(hsh_i & 255u) / 255.0f - 0.5f) * amp;
    float nq = ((float)(hsh_q & 255u) / 255.0f - 0.5f) * amp;
    i_plane[y * w + x] = clamp(i_plane[y * w + x] * gain + ni, -180.0f, 180.0f);
    q_plane[y * w + x] = clamp(q_plane[y * w + x] * gain + nq, -180.0f, 180.0f);
}

__kernel void to_yuv(
    __global const float *y_in, __global const float *i_in,
    __global const float *q_in, __global uchar *y_out,
    __global uchar *u_out, __global uchar *v_out,
    const int w, const int h, const int uw)
{
    int x = get_global_id(0), y = get_global_id(1);
    if (x >= w || y >= h) return;
    int p = y * w + x;
    y_out[p] = (uchar)clamp(y_in[p], 0.0f, 255.0f);
    if ((x & 1) == 0 && (y & 1) == 0) {
        int up = (y >> 1) * uw + (x >> 1);
        float yy = 0.25f * (y_in[p] + y_in[p + 1] + y_in[p + w] + y_in[p + w + 1]);
        float ii = 0.25f * (i_in[p] + i_in[p + 1] + i_in[p + w] + i_in[p + w + 1]);
        float qq = 0.25f * (q_in[p] + q_in[p + 1] + q_in[p + w] + q_in[p + w + 1]);
        // YIQ -> RGB (NTSC-RS matrix), then RGB -> YUV 4:2:0.
        float rr = yy + 0.956f * ii + 0.621f * qq;
        float gg = yy - 0.272f * ii - 0.647f * qq;
        float bb = yy - 1.106f * ii + 1.703f * qq;
        u_out[up] = (uchar)clamp(-0.169f * rr - 0.331f * gg + 0.500f * bb + 128.0f, 0.0f, 255.0f);
        v_out[up] = (uchar)clamp(0.500f * rr - 0.419f * gg - 0.081f * bb + 128.0f, 0.0f, 255.0f);
    }
}
"""


def opencl_devices(include_cpu: bool | None = None):
    """Return [(name, device)] for usable OpenCL devices.

    GPUs are preferred. Set BEOLD_OPENCL_INCLUDE_CPU=1 to also include CPU
    devices (useful for testing the multi-device scheduler on a single-GPU
    machine).
    """
    try:
        import pyopencl as cl
    except Exception:
        return []
    if include_cpu is None:
        include_cpu = os.environ.get("BEOLD_OPENCL_INCLUDE_CPU") == "1"
    result = []
    try:
        for platform in cl.get_platforms():
            gpus = list(platform.get_devices(device_type=cl.device_type.GPU))
            for device in gpus:
                result.append((device.name.strip(), device))
            if include_cpu:
                for device in platform.get_devices(device_type=cl.device_type.CPU):
                    result.append((device.name.strip(), device))
    except Exception:
        return []
    if os.environ.get("BEOLD_OPENCL_FORCE_MULTI") == "1" and result:
        result = [result[0], result[0]]
    return result


def available() -> bool:
    return bool(opencl_devices())


def _alpha(cutoff: float, rate: float) -> float:
    dt = 1.0 / rate
    tau = 1.0 / (2.0 * math.pi * max(1.0, cutoff))
    return dt / (tau + dt)


def _butterworth(cutoff: float, rate: float):
    freq = min(2.0 * cutoff, rate) / rate
    freq *= math.pi
    omega_s, omega_c = math.sin(freq), math.cos(freq)
    alpha = omega_s / (2.0 * (1.0 / math.sqrt(2.0)))
    gain = 1.0 / (1.0 + alpha)
    return (
        (1.0 - omega_c) * 0.5 * gain,
        (1.0 - omega_c) * gain,
        (1.0 - omega_c) * 0.5 * gain,
        -2.0 * omega_c * gain,
        (1.0 - alpha) * gain,
    )


def _notch(freq: float, quality: float):
    bandwidth = (freq / quality) * math.pi
    freq *= math.pi
    beta = math.tan(bandwidth * 0.5)
    gain = 1.0 / (1.0 + beta)
    return (
        gain,
        -2.0 * math.cos(freq) * gain,
        gain,
        -2.0 * math.cos(freq) * gain,
        2.0 * gain - 1.0,
    )


def _read_exact(stream, size: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def render(
    source: Path,
    destination: Path,
    level: int,
    height: int,
    src_width: int,
    src_height: int,
    fps: float,
    duration: float,
    progress: Optional[Callable[[int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    noise: bool = True,
    dump_dir: Optional[Path] = None,
    device_index: int = 0,
    start_time: float = 0.0,
) -> str:
    """Render video-only H.264 through the full-res YIQ OpenCL path."""
    import numpy as np
    import pyopencl as cl
    import subprocess as sp

    devices = opencl_devices()
    if not devices:
        raise OpenCLVhsError("没有可用的 OpenCL GPU 设备")
    device_name, device = devices[device_index % len(devices)]
    ctx = cl.Context([device])
    queue = cl.CommandQueue(ctx)
    # Bypass PyOpenCL's user cache. A stale cache lock made packaged startup
    # wait 60 seconds and then fall back to CPU.
    try:
        raw_program = cl._cl._Program(ctx, KERNEL)
        raw_program.build(b"")
        program = cl.Program(raw_program)
    except Exception:
        program = cl.Program(ctx, KERNEL).build()
    krgb, kiir, kmod, knoise, kdem, kvb, kout = (
        program.rgb_to_yiq, program.iir_plane, program.modulate,
        program.composite_noise, program.demod, program.vert_blend,
        program.to_yuv,
    )
    kbiquad = program.biquad_plane
    kiir3 = program.iir3_plane
    kiir1 = program.iir1_plane
    kpre = program.preemphasis_plane
    kedge = program.edge_shift
    kblank = program.blank_rows
    ksnow = program.snow_luma
    ksmear = program.luma_smear_row
    kchroma = program.chroma_row_noise

    q = max(0.0, min(1.0, (level - 1) / 99.0))
    noise_enabled = bool(noise)
    if dump_dir is not None:
        dump_dir = Path(dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)
    h = max(2, height - height % 2)
    w = max(2, int(round(src_width * h / max(1, src_height) / 2.0)) * 2)
    uw, uh = w // 2, h // 2
    ysz, uvsz = w * h, uw * uh
    frame_size = ysz * 3
    total = max(1, int(round(duration * fps)))
    if duration <= 0 or fps <= 0:
        raise OpenCLVhsError("无法确定视频时长/帧率")

    def fbuf(n):
        return cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=n * 4)

    Y, Yn, I, Q = fbuf(ysz), fbuf(ysz), fbuf(ysz), fbuf(ysz)
    It, Qt = fbuf(ysz), fbuf(ysz)
    Cpre = fbuf(ysz)
    Ey, Ei, Eq = fbuf(ysz), fbuf(ysz), fbuf(ysz)
    shifts_buf = cl.Buffer(ctx, cl.mem_flags.READ_ONLY, size=h * 4)
    mask_buf = cl.Buffer(ctx, cl.mem_flags.READ_ONLY, size=h)
    edge_cfg = EdgeWave()
    C = fbuf(ysz)
    Io, Qo = fbuf(ysz), fbuf(ysz)
    rgb_in = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=ysz * 3)
    y_out = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=ysz)
    u_out = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=uvsz)
    v_out = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=uvsz)

    local = (16, 16)
    g = (((w + 15) // 16) * 16, ((h + 15) // 16) * 16)
    hscale = max(0.1, w / 760.0)
    rate = NTSC_RATE * hscale
    a_i, a_q = _alpha(1_300_000.0, rate), _alpha(600_000.0, rate)
    tape = {"SP": (2_400_000.0, 320_000.0, 4),
            "LP": (1_900_000.0, 300_000.0, 5),
            "EP": (1_400_000.0, 280_000.0, 6)}
    name = "LP" if 0.3 <= q <= 0.7 else ("SP" if q < 0.3 else "EP")
    luma_cut, chroma_cut, chroma_delay = tape[name]
    chroma_delay = max(0, int(round(chroma_delay * hscale)))
    a_y, a_c = _alpha(luma_cut, rate), _alpha(chroma_cut, rate)
    notch_c = _notch(0.5, 2.0)
    # NTSC-RS's legacy settings default to ConstantK (three cascaded
    # one-pole lowpasses), not Butterworth.
    in_i_a = _alpha(1_300_000.0, rate)
    in_q_a = _alpha(600_000.0, rate)
    tape_y_a = _alpha(luma_cut, rate)
    tape_c_a = _alpha(chroma_cut, rate)
    tape_sharp_a = _alpha(luma_cut, rate)
    tape_sharpen_a = _alpha(luma_cut, rate)
    out_i_a = in_i_a
    out_q_a = in_q_a
    seed = np.uint32(1337)
    grain, snow = 2.0 + q * 12.0, 0.001 + q * 0.015
    # Composite-domain tracking noise must be small; large additive offsets
    # become a bright horizontal band after demodulation.
    tracking, head_shift = q * 18.0, q * 4.0
    smear_cut = math.exp2(-4.0 * (0.5 + 0.5 * q)) * 0.25
    smear_alpha = (1.0 / hscale) / ((1.0 / (2.0 * math.pi * smear_cut)) + (1.0 / hscale))
    pre_alpha = _alpha((315_000_000.0 / 88.0 / 2.0), rate)
    pre_scale = 0.045
    luma_row = 40.0 + q * 80.0
    luma_pixel = 0.0
    chroma_row = 10.0 + q * 22.0
    chroma_gain = 1.0
    if not noise_enabled:
        luma_row = 0.0
        luma_pixel = 0.0
        chroma_row = 0.0
        tracking = 0.0
        snow = 0.0

    tools_root = Path(os.environ.get(
        "BEOLD_TOOLS_DIR",
        str(Path(__file__).resolve().parents[1] / "tools"),
    ))
    ffmpeg = tools_root / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not ffmpeg.exists():
        raise OpenCLVhsError("找不到 ffmpeg")

    dec = sp.Popen(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error",
         *(["-ss", f"{start_time:.6f}"] if start_time > 0 else []),
         "-i", str(source),
         *(["-t", f"{duration:.6f}"] if start_time > 0 else []),
         "-vf", f"scale=-2:{h}:flags=bicubic,format=rgb24",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-an", "-"],
        stdin=sp.DEVNULL, stdout=sp.PIPE, stderr=sp.PIPE,
    )
    enc = sp.Popen(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
         "-r", f"{fps:.6f}", "-i", "-", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", str(destination)],
        stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=sp.PIPE,
    )

    frames = 0

    def dump(name: str, buf, count: int) -> None:
        if dump_dir is None or frames != 0:
            return
        arr = np.empty(count, dtype=np.float32)
        cl.enqueue_copy(queue, arr, buf)
        queue.finish()
        np.save(dump_dir / f"{name}.npy", arr)

    try:
        while frames < total:
            if cancel_event is not None and cancel_event.is_set():
                raise OpenCLVhsCancelled("任务已取消。")
            raw = _read_exact(dec.stdout, frame_size)
            if raw is None:
                break
            rgb = np.frombuffer(raw, dtype=np.uint8)
            if dump_dir is not None and frames == 0:
                np.save(dump_dir / "00_rgb.npy", rgb.reshape(h, w, 3))
            cl.enqueue_copy(queue, rgb_in, rgb)
            krgb(queue, g, local, rgb_in, Y, I, Q,
                 np.int32(w), np.int32(h))
            dump("01_yiq_y", Y, ysz)
            dump("01_yiq_i", I, ysz)
            dump("01_yiq_q", Q, ysz)

            def bq(src, dst, c, delay=0, first=0):
                kbiquad(queue, (h,), None, src, dst,
                        np.int32(w), np.int32(h),
                        np.float32(c[0]), np.float32(c[1]), np.float32(c[2]),
                        np.float32(c[3]), np.float32(c[4]),
                        np.int32(delay), np.int32(first))

            def i3(src, dst, alpha, delay=0):
                kiir3(queue, (h,), None, src, dst,
                      np.int32(w), np.int32(h),
                      np.float32(alpha), np.int32(delay))

            # Input luma notch + full chroma lowpass (NTSC-RS defaults).
            bq(Y, Yn, notch_c, 0, 1)
            i3(I, It, in_i_a, 2)
            i3(Q, Qt, in_q_a, 4)
            dump("02_luma_notch", Yn, ysz)
            dump("03_chroma_in_i", It, ysz)
            dump("03_chroma_in_q", Qt, ysz)
            kmod(queue, g, local, Yn, It, Qt, C,
                 np.int32(w), np.int32(h), np.int32(frames))
            dump("04_composite", C, ysz)
            if noise_enabled:
                knoise(queue, g, local, C, np.int32(w), np.int32(h), np.int32(frames),
                       seed, np.float32(grain), np.float32(snow),
                       np.float32(tracking), np.float32(head_shift))
            kpre(queue, (h,), None, C, Cpre,
                 np.int32(w), np.int32(h), np.float32(pre_alpha), np.float32(pre_scale))
            dump("04_preemphasis", Cpre, ysz)
            bq(Cpre, Yn, notch_c, 0, 0)
            kdem(queue, g, local, Yn, Cpre, Io, Qo,
                 np.int32(w), np.int32(h), np.int32(frames))
            dump("05_demod_i", Io, ysz)
            dump("05_demod_q", Qo, ysz)
            kiir1(queue, (h,), None, Yn, C,
                  np.int32(w), np.int32(h), np.float32(smear_alpha))
            dump("06_luma_smear", C, ysz)
            shifts = edge_shifts_np(0, frames, h, hscale, edge_cfg)
            mask = np.zeros(h, dtype=np.uint8)
            for row in chroma_loss_rows(0, frames, h, 0.000025):
                mask[row] = 1
            cl.enqueue_copy(queue, shifts_buf, shifts)
            cl.enqueue_copy(queue, mask_buf, mask)
            kedge(queue, (h,), None, C, Io, Qo, Ey, Ei, Eq,
                  shifts_buf, np.int32(w), np.int32(h))
            i3(Ey, Ey, tape_y_a, 0)
            kpre(queue, (h,), None, Ey, Ey,
                 np.int32(w), np.int32(h), np.float32(tape_sharp_a), np.float32(-1.6))
            if noise_enabled:
                ksnow(queue, g, local, Ey, np.int32(w), np.int32(h),
                      np.int32(frames), seed, np.float32(snow))
            i3(Ei, Ei, tape_c_a, chroma_delay)
            i3(Eq, Eq, tape_c_a, chroma_delay)
            dump("07_tape_y", Ey, ysz)
            dump("07_tape_i", Ei, ysz)
            dump("07_tape_q", Eq, ysz)
            kblank(queue, (h,), None, Ei, Eq, mask_buf, np.int32(w), np.int32(h))
            kchroma(queue, g, local, Ei, Eq, np.int32(w), np.int32(h),
                    np.int32(frames), seed, np.float32(chroma_row),
                    np.float32(chroma_gain))
            kvb(queue, g, local, Ei, Eq, I, Q, np.int32(w), np.int32(h))
            dump("09_blend_i", I, ysz)
            dump("09_blend_q", Q, ysz)
            i3(I, I, out_i_a, 2)
            i3(Q, Q, out_q_a, 4)
            dump("10_out_i", I, ysz)
            dump("10_out_q", Q, ysz)
            kout(queue, g, local, Ey, I, Q, y_out, u_out, v_out,
                 np.int32(w), np.int32(h), np.int32(uw))
            yo = np.empty(ysz, dtype=np.uint8)
            uo = np.empty(uvsz, dtype=np.uint8)
            vo = np.empty(uvsz, dtype=np.uint8)
            cl.enqueue_copy(queue, yo, y_out)
            cl.enqueue_copy(queue, uo, u_out)
            cl.enqueue_copy(queue, vo, v_out)
            queue.finish()
            if dump_dir is not None and frames == 0:
                np.save(dump_dir / "11_yuv_y.npy", yo.reshape(h, w))
                np.save(dump_dir / "11_yuv_u.npy", uo.reshape(uh, uw))
                np.save(dump_dir / "11_yuv_v.npy", vo.reshape(uh, uw))
            try:
                enc.stdin.write(yo.tobytes() + uo.tobytes() + vo.tobytes())
            except OSError as exc:
                err = enc.stderr.read().decode("utf-8", "replace") if enc.stderr else ""
                raise OpenCLVhsError(f"GPU VHS 编码管道关闭：{err[-800:]}") from exc
            frames += 1
            if progress and (frames == 1 or frames % 5 == 0 or frames == total):
                progress(min(100, int(frames / total * 100)))
    finally:
        if cancel_event is not None and cancel_event.is_set():
            for proc in (dec, enc):
                try:
                    proc.terminate()
                except Exception:
                    pass
        if dec.stdout:
            dec.stdout.close()
        if enc.stdin:
            enc.stdin.close()
        try:
            dec.wait(timeout=5)
        except Exception:
            dec.kill()
        try:
            enc.wait(timeout=5)
        except Exception:
            enc.kill()

    if cancel_event is not None and cancel_event.is_set():
        raise OpenCLVhsCancelled("任务已取消。")
    if enc.returncode != 0:
        err = enc.stderr.read().decode("utf-8", "replace") if enc.stderr else ""
        raise OpenCLVhsError("GPU VHS 编码失败：" + err[-800:])
    if frames == 0:
        raise OpenCLVhsError("GPU VHS 没有生成帧")
    return device_name


def render_multi(
    source: Path,
    destination: Path,
    level: int,
    height: int,
    src_width: int,
    src_height: int,
    fps: float,
    duration: float,
    progress: Optional[Callable[[int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    noise: bool = True,
    device_count: int = 0,
) -> str:
    """Render a clip using all available GPUs.

    Frames are split into contiguous time segments; each segment is rendered by
    one device and the encoded segments are concatenated. This keeps the
    per-frame kernels unchanged and gives independent devices independent
    command queues.
    """
    devices = opencl_devices()
    if device_count > 0:
        devices = devices[:device_count]
    total_frames = max(1, int(round(duration * fps)))
    count = min(len(devices), total_frames)
    if count <= 1:
        return render(source, destination, level, height, src_width, src_height,
                      fps, duration, progress, cancel_event, noise)

    tools_root = Path(os.environ.get(
        "BEOLD_TOOLS_DIR",
        str(Path(__file__).resolve().parents[1] / "tools"),
    ))
    ffmpeg = tools_root / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not ffmpeg.exists():
        raise OpenCLVhsError("找不到 ffmpeg")

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="beold-gpu-", dir=str(destination.parent)))
    segments = []
    lock = threading.Lock()

    def worker(i: int) -> tuple[int, str]:
        start = total_frames * i // count
        end = total_frames * (i + 1) // count
        seg_start = start / fps
        seg_duration = (end - start) / fps
        seg = tmp / f"seg{i:02d}.mp4"
        name = render(
            source, seg, level, height, src_width, src_height,
            fps, seg_duration, None, cancel_event, noise,
            device_index=i, start_time=seg_start,
        )
        if progress is not None:
            with lock:
                progress(int((i + 1) * 100 / count))
        return i, name

    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(worker, i) for i in range(count)]
            names = []
            for fut in futures:
                i, name = fut.result()
                segments.append((i, tmp / f"seg{i:02d}.mp4"))
                names.append(name)
        if cancel_event is not None and cancel_event.is_set():
            raise OpenCLVhsCancelled("任务已取消。")
        listing = tmp / "segments.txt"
        listing.write_text(
            "".join(f"file '{p.as_posix()}'\n" for _, p in sorted(segments)),
            encoding="utf-8",
        )
        cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(listing),
               "-c", "copy", "-an", str(destination)]
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise OpenCLVhsError(
                "多 GPU 拼接失败：" + proc.stderr.decode("utf-8", "replace")[-800:]
            )
        return " + ".join(names)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
