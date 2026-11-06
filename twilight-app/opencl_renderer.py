"""Independent multi-GPU OpenCL renderer for 包浆 / 压片.

This intentionally does not use the legacy SDK renderers: patina is a per-pixel
kernel and squeeze is an OpenCL downscale followed by the video encoder.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

_KERNELS = r'''
inline float pseudo(float x) { return x - floor(x); }

__kernel void patina(__global const uchar *src, __global uchar *dst,
                     int w, int h, float level, uint seed, int y_offset, int full_h) {
    int x = get_global_id(0);
    int y = get_global_id(1);
    if (x >= w || y >= h) return;
    int gy = y + y_offset;
    int i = (y * w + x) * 4;
    float r = src[i] / 255.0f;
    float g = src[i + 1] / 255.0f;
    float b = src[i + 2] / 255.0f;
    float lum = 0.299f * r + 0.587f * g + 0.114f * b;
    float sr = 0.393f * r + 0.769f * g + 0.189f * b;
    float sg = 0.349f * r + 0.686f * g + 0.168f * b;
    float sb = 0.272f * r + 0.534f * g + 0.131f * b;
    float k = 0.25f + 0.30f * level;
    r = mix(r, sr, k);
    g = mix(g, sg, k);
    b = mix(b, sb, k);
    float fade = 0.08f + 0.22f * level;
    r = r * (1.0f - fade) + fade * 0.72f;
    g = g * (1.0f - fade) + fade * 0.62f;
    b = b * (1.0f - fade) + fade * 0.46f;
    float contrast = 1.0f - 0.20f * level;
    r = (r - 0.5f) * contrast + 0.5f;
    g = (g - 0.5f) * contrast + 0.5f;
    b = (b - 0.5f) * contrast + 0.5f;
    float dx = (x - w * 0.5f) / (w * 0.5f);
    float dy = (gy - full_h * 0.5f) / (full_h * 0.5f);
    float vig = 1.0f - level * 0.38f * (dx * dx + dy * dy);
    r *= vig; g *= vig; b *= vig;
    uint n = (uint)(x * 374761393u + gy * 668265263u + seed * 1442695041u);
    float grain = (pseudo(sin((float)n * 0.0001f) * 43758.5453f) - 0.5f) * (0.035f + 0.10f * level);
    r += grain; g += grain; b += grain;
    float scratch = pseudo(sin((float)(x * 17 + gy * 13 + (int)seed) * 0.017f) * 43758.5453f);
    if (scratch > 0.997f) {
        float dust = 0.25f + 0.45f * level;
        r += dust; g += dust; b += dust;
    }
    uchar4 out;
    out.x = (uchar)clamp(r * 255.0f, 0.0f, 255.0f);
    out.y = (uchar)clamp(g * 255.0f, 0.0f, 255.0f);
    out.z = (uchar)clamp(b * 255.0f, 0.0f, 255.0f);
    out.w = src[i + 3];
    vstore4(out, 0, dst + i);
}

__kernel void downscale(__global const uchar *src, __global uchar *dst,
                        int sw, int sh, int dw, int dh) {
    int x = get_global_id(0);
    int y = get_global_id(1);
    if (x >= dw || y >= dh) return;
    int x0 = x * sw / dw;
    int y0 = y * sh / dh;
    int x1 = min(sw, max(x0 + 1, (x + 1) * sw / dw));
    int y1 = min(sh, max(y0 + 1, (y + 1) * sh / dh));
    int rr = 0, gg = 0, bb = 0, aa = 0, count = 0;
    for (int sy = y0; sy < y1; ++sy) {
        for (int sx = x0; sx < x1; ++sx) {
            int i = (sy * sw + sx) * 4;
            rr += src[i]; gg += src[i + 1]; bb += src[i + 2]; aa += src[i + 3];
            ++count;
        }
    }
    int j = (y * dw + x) * 4;
    dst[j] = (uchar)(rr / max(1, count));
    dst[j + 1] = (uchar)(gg / max(1, count));
    dst[j + 2] = (uchar)(bb / max(1, count));
    dst[j + 3] = (uchar)(aa / max(1, count));
}
'''


def _devices():
    try:
        from effects_core.opencl_vhs import opencl_devices
        return opencl_devices()
    except Exception:
        return []


class _Program:
    def __init__(self, device):
        import pyopencl as cl
        self.cl = cl
        self.context = cl.Context([device])
        self.queue = cl.CommandQueue(self.context)
        self.program = cl.Program(self.context, _KERNELS).build()
        self.patina_kernel = cl.Kernel(self.program, "patina")
        self.downscale_kernel = cl.Kernel(self.program, "downscale")

    def run(self, kernel: str, source: np.ndarray, width: int, height: int,
            out_width: int, out_height: int, level: float = 0.0, seed: int = 1,
            y_offset: int = 0, full_height: int = 0) -> np.ndarray:
        cl = self.cl
        mf = cl.mem_flags
        source = np.ascontiguousarray(source, dtype=np.uint8)
        output = np.empty((out_height, out_width, 4), dtype=np.uint8)
        src_buf = cl.Buffer(self.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=source)
        dst_buf = cl.Buffer(self.context, mf.WRITE_ONLY, output.nbytes)
        if kernel == "patina":
            fn = self.patina_kernel
            fn(self.queue, (width, height), None, src_buf, dst_buf, np.int32(width), np.int32(height),
               np.float32(level), np.uint32(seed), np.int32(y_offset), np.int32(full_height or height))
        else:
            fn = self.downscale_kernel
            fn(self.queue, (out_width, out_height), None, src_buf, dst_buf,
               np.int32(width), np.int32(height), np.int32(out_width), np.int32(out_height))
        cl.enqueue_copy(self.queue, output, dst_buf)
        self.queue.finish()
        return output


class OpenCLRenderer:
    def __init__(self, max_devices: int = 0):
        devices = _devices()
        if max_devices > 0:
            devices = devices[:max_devices]
        self.devices = devices
        self.programs = []

    def _programs(self) -> list[_Program]:
        if not self.devices:
            raise RuntimeError("没有可用的 OpenCL GPU")
        if not self.programs:
            self.programs = [_Program(device) for _name, device in self.devices]
        return self.programs

    def patina_image(self, source: Path, target: Path, level: float) -> Path:
        from PIL import Image
        image = Image.open(source).convert("RGBA")
        array = np.asarray(image, dtype=np.uint8)
        height, width = array.shape[:2]
        programs = self._programs()
        count = min(len(programs), height)
        strips = []

        def render_strip(index: int):
            top = height * index // count
            bottom = height * (index + 1) // count
            strip = np.ascontiguousarray(array[top:bottom])
            rendered = programs[index].run("patina", strip, width, bottom - top,
                                           width, bottom - top, level=level, seed=17, y_offset=top,
                                           full_height=height)
            return top, rendered

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            for top, rendered in pool.map(render_strip, range(count)):
                strips.append((top, rendered))
        output = np.empty_like(array)
        for top, rendered in strips:
            output[top:top + rendered.shape[0]] = rendered
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(output, "RGBA").convert("RGB").save(target, quality=88)
        return target

    def squeeze_video(self, source: Path, target: Path, level: float, ffmpeg: Path,
                      fps: float = 0.0) -> Path:
        from PIL import Image
        import local_actions
        programs = self._programs()
        target_height = int(round(144 + (480 - 144) * max(0.0, min(1.0, level))))
        target_height = max(144, target_height // 2 * 2)
        fps = fps or local_actions._video_fps(source)
        bitrate = int(round(80 + 3000 * max(0.0, min(1.0, level))))
        with tempfile.TemporaryDirectory(prefix=".twilight-opencl-squeeze-", dir=str(target.parent)) as temp:
            root = Path(temp)
            frames_in = root / "frames"
            frames_out = root / "out"
            frames_in.mkdir(); frames_out.mkdir()
            subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                            "-vsync", "0", str(frames_in / "%06d.png")],
                           check=True, timeout=7200, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            frames = sorted(frames_in.glob("*.png"))
            if not frames:
                raise RuntimeError("无法提取视频帧")
            with Image.open(frames[0]) as first_image:
                source_width, source_height = first_image.size
            target_width = max(2, int(round(target_height * source_width / source_height)) // 2 * 2)
            def render_frame(index: int, frame: Path):
                with Image.open(frame) as source_image:
                    image = source_image.convert("RGBA")
                    array = np.ascontiguousarray(np.asarray(image, dtype=np.uint8))
                program = programs[index % len(programs)]
                rendered = program.run("downscale", array, source_width, source_height,
                                       target_width, target_height)
                Image.fromarray(rendered, "RGBA").convert("RGB").save(frames_out / frame.name)

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(programs)) as pool:
                list(pool.map(lambda pair: render_frame(*pair), enumerate(frames)))
            audio = root / "audio.m4a"
            subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                            "-vn", "-c:a", "aac", "-b:a", "128k", str(audio)], timeout=600,
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                       "-framerate", f"{fps:g}", "-i", str(frames_out / "%06d.png"),
                       "-i", str(audio if audio.is_file() else source),
                       "-map", "0:v:0", "-map", "1:a:0?",
                       "-c:v", "libx264", "-b:v", f"{bitrate}k", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "96k", "-shortest", str(target)]
            subprocess.run(command, check=True, timeout=7200, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return target
