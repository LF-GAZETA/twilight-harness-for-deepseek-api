"""Vendored NTSC-QT VHS simulation core for Twilight Harness (numpy + Pillow only).

Provenance / license
--------------------
This module is a **modified port** of ``app/ntsc.py`` from NTSC-QT:
    project : NTSC-QT ("ntscqt") by JargeZ and contributors
    url     : https://github.com/JargeZ/ntscqt  (master, file ``app/ntsc.py``)
    license : Apache License 2.0
The complete upstream license text ships with this application in
``docs/third-party/ntscqt-LICENSE.txt``; the upstream ``ringPattern.npy`` data
file (also Apache-2.0) is embedded below as base64 so no data file is needed.

What was modified for the port
------------------------------
* Runtime dependencies reduced from numpy + scipy + OpenCV + PyQt5 to
  **numpy + Pillow only**.  Nothing in this module imports scipy, cv2, PyQt5,
  ffmpeg or the app's logging framework.
* ``scipy.signal.lfilter`` (single pole IIR in ``LowpassFilter``) replaced by a
  numerically stable, vectorised numpy recurrence (blocked exponential-scaled
  cumulative sum; see ``_iir_lowpass``).  Verified to agree with scipy's
  ``lfilter``/``lfiltic`` to ~1e-11 absolute, i.e. identical after the int32
  truncation the algorithm applies.
* ``scipy.ndimage.interpolation.shift(arr, 1)`` replaced by
  ``_shift_one(arr)`` (zero padded one-sample delay; identical for an integer
  shift).
* ``cv2.dft``/``cv2.idft`` replaced by numpy FFTs in ``_cv2_dft_complex`` /
  ``_cv2_idft_scale``.  The upstream code runs ``np.fft.fftshift`` on the
  3-channel OpenCV DFT result, which also rolls (and therefore swaps) the
  complex real/imag channel axis; that quirk is preserved bit-for-bit here.
  Masks are built exactly as upstream (a real mask applied independently to the
  real and imaginary parts, matching OpenCV's 2-channel multiply).
* ``cv2.resize(..., INTER_LINEAR)`` used for the ringing2 pattern replaced by
  ``_resize_linear_1d`` (OpenCV half-pixel bilinear sampling; agrees with cv2
  to ~4e-8).
* ``cv2.resize(..., INTER_LANCZOS4)`` in ``_blur_chroma`` replaced by
  ``_resize_lanczos4``: a numpy implementation of the same 8 tap Lanczos-4
  windowed-sinc kernel (support scaled per output pixel, border replicate,
  weights renormalised over the 8 taps).  This agrees with cv2 to float32
  rounding (<0.001) on the exact 2x down/up-sampling used here, whereas
  ``PIL.Image.resize(LANCZOS)`` differs from cv2 by up to hundreds of LSB on
  high frequency chroma data - see ``tests/artifacts/vhs-dev/parity_check.py``.
* ``cv2.convertScaleAbs`` replaced by ``_convert_scale_abs`` (abs, saturate to
  0..255, round-half-to-even - the same ``cv2Round`` semantics).
* Input frame scaling (upstream ``DefaultRenderer.prepare_frame`` used
  ``cv2.resize``) uses ``PIL.Image.resize`` with BILINEAR via
  ``_resize_frame_bgr``.  The headless pipeline feeds frames that are already
  at render size, so this path is only used by direct API callers.
* Removed: ``random_ntsc`` (the GUI randomiser - replaced by the explicit
  per-mode presets below), the module level ring-pattern file loader, and all
  PyQt5/ffmpeg/logger plumbing.
* Kept byte-for-byte in behaviour: every ``Ntsc`` field default, method order
  and in-place numpy arithmetic, the ``VHSSpeed`` table, the composite layer
  pipeline order, and the interlaced output blend in ``apply_main_effect``.
* Added: per-mode VHS tuning presets, ``make_renderer``, ``VHSRenderer`` and
  ``apply_main_effect`` (a headless re-implementation of
  ``DefaultRenderer.apply_main_effect``).
"""

from __future__ import annotations

import base64
import io
import math
import random
from typing import List

import numpy

__all__ = [
    "VHS_MODES",
    "MODE_TUNING",
    "DEFAULT_SEED",
    "Ntsc",
    "NumpyRandom",
    "XorWowRandom",
    "VHSSpeed",
    "LowpassFilter",
    "RingPattern",
    "apply_main_effect",
    "apply_mode_tuning",
    "make_renderer",
    "VHSRenderer",
    "ringing",
    "ringing2",
]

# ---------------------------------------------------------------------------
# Embedded upstream data file: app/ringPattern.npy (720 x float32, 3008 bytes,
# exactly the Apache-2.0 licensed NTSC-QT asset).  Decoded with np.load() so
# the resulting array is identical to the upstream file.
# ---------------------------------------------------------------------------
_RING_PATTERN_NPY_B64 = (
    "k05VTVBZAQB2AHsnZGVzY3InOiAnPGY0JywgJ2ZvcnRyYW5fb3JkZXInOiBGYWxzZSwgJ3NoYXBlJzogKDcyMCwpLCB9ICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIAoAAAAAoaAgPZGQED2BgAA90dBQPbGw"
    "MD3BwEA90dBQPcHAQD3R0FA9gYCAPfHwcD2BgIA9gYCAPZmYmD2ZmJg9oaCgPamoqD2xsLA9sbCwPcHAwD2JiAg+iYiIPbGwsD25"
    "uLg9wcDAPeHg4D3R0NA90dDQPeHg4D3Z2Ng96ejoPfHw8D3x8PA9gYAAPoWEBD6JiAg+jYwMPomICD6RkBA+jYwMPomICD6JiAg+"
    "mZgYPpWUFD6ZmBg+lZQUPpWUFD6ZmBg+nZwcPp2cHD6dnBw+kZAQPpWUFD6RkBA+lZQUPo2MDD6NjAw+jYwMPpGQED6JiAg+iYgI"
    "Pvn4+D3x8PA94eDgPeno6D3BwMA9sbCwPbGwsD2RkJA9gYCAPYGAgD3h4GA9sbAwPaGgoD2xsDA9wcBAPeHgYD3h4GA94eBgPeHg"
    "YD2RkJA9oaCgPbGwsD25uLg90dDQPeHg4D3p6Og9gYAAPpWUFD7BwMA9kZAQPp2cHD6hoCA+qagoPqGgID6pqCg+sbAwPq2sLD65"
    "uDg+ubg4PrGwMD61tDQ+rawsPq2sLD6pqCg+paQkPqmoKD6lpCQ+rawsPpmYGD6dnBw+nZwcPp2cHD6dnBw+qagoPqGgID6VlBQ+"
    "nZwcPqmoKD6hoCA+qagoPpmYGD6hoCA+tbQ0PrW0ND65uDg+vbw8PvHwcD79/Hw+jYyMPouKij6RkJA+m5qaPqempj69vLw+4eDg"
    "PoiHBz+VlBQ/rawsP7OyMj+hoCA/qKcnP6WkJD+Miws/kI8PP+Pi4j7HxsY+v76+PtnYWD7p6Gg+6ehoPs3MTD7V1FQ+h4aGPp2c"
    "nD6XlpY+2djYPvPy8j7//v4+sK8vP6SjIz/CwUE/2dhYP9XUVD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8A"
    "AIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAP+XkZD/GxUU/x8ZGP5eWFj+Ylxc/9/b2Pp+enj6trKw+gYCAPv38fD6BgIA+"
    "0dDQPsXExD7Z2Ng+np0dP5iXFz+trCw/xMNDP7KxMT/Ew0M/yMdHP8fGRj/Ew0M/srExP8PCQj+wry8/oaAgP5eWFj+UkxM/iIcH"
    "P4SDAz+EgwM/9fT0Pvf29j7b2to+7ezsPuXk5D7l5OQ+6ejoPuvq6j79/Pw+gYAAP/Py8j79/Pw+8/LyPvX09D7p6Og+6+rqPvPy"
    "8j7Pzs4+09LSPtPS0j65uLg+v76+Pru6uj6/vr4+wcDAPs3MzD7DwsI+ycjIPt3c3D7n5uY+397ePt3c3D7r6uo+5+bmPtfW1j7x"
    "8PA+6ejoPoKBAT/39vY+19bWPuvq6j7r6uo+6+rqPoKBAT+Pjg4/oJ8fP6alJT++vT0/1dRUP87NTT/29XU/AACAPwAAgD8AAIA/"
    "AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAP+zraz8AAIA/6OdnP7W0ND/S0VE/sK8vP+PiYj/FxEQ/7u1t"
    "PwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAA"
    "gD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8A"
    "AIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/"
    "AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACA"
    "PwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAA"
    "gD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8A"
    "AIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/"
    "AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAP+7tbT/FxEQ/4+Ji"
    "P7CvLz/S0VE/tbQ0P+jnZz8AAIA/7OtrPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAA"
    "gD/29XU/zs1NP9XUVD++vT0/pqUlP6CfHz+Pjg4/goEBP+vq6j7r6uo+6+rqPtfW1j739vY+goEBP+no6D7x8PA+19bWPufm5j7r"
    "6uo+3dzcPt/e3j7n5uY+3dzcPsnIyD7DwsI+zczMPsHAwD6/vr4+u7q6Pr++vj65uLg+09LSPtPS0j7Pzs4+8/LyPuvq6j7p6Og+"
    "9fT0PvPy8j79/Pw+8/LyPoGAAD/9/Pw+6+rqPuno6D7l5OQ+5eTkPu3s7D7b2to+9/b2PvX09D6EgwM/hIMDP4iHBz+UkxM/l5YW"
    "P6GgID+wry8/w8JCP7KxMT/Ew0M/x8ZGP8jHRz/Ew0M/srExP8TDQz+trCw/mJcXP56dHT/Z2Ng+xcTEPtHQ0D6BgIA+/fx8PoGA"
    "gD6trKw+n56ePvf29j6Ylxc/l5YWP8fGRj/GxUU/5eRkPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD8A"
    "AIA/AACAPwAAgD8AAIA/AACAPwAAgD8AAIA/1dRUP9nYWD/CwUE/pKMjP7CvLz///v4+8/LyPtnY2D6XlpY+nZycPoeGhj7V1FQ+"
    "zcxMPunoaD7p6Gg+2dhYPr++vj7HxsY+4+LiPpCPDz+Miws/paQkP6inJz+hoCA/s7IyP62sLD+VlBQ/iIcHP+Hg4D69vLw+p6am"
    "Ppuamj6RkJA+i4qKPo2MjD79/Hw+8fBwPr28PD65uDg+tbQ0PrW0ND6hoCA+mZgYPqmoKD6hoCA+qagoPp2cHD6VlBQ+oaAgPqmo"
    "KD6dnBw+nZwcPp2cHD6dnBw+mZgYPq2sLD6lpCQ+qagoPqWkJD6pqCg+rawsPq2sLD61tDQ+sbAwPrm4OD65uDg+rawsPrGwMD6p"
    "qCg+oaAgPqmoKD6hoCA+nZwcPpGQED7BwMA9lZQUPoGAAD7p6Og94eDgPdHQ0D25uLg9sbCwPaGgoD2RkJA94eBgPeHgYD3h4GA9"
    "4eBgPcHAQD2xsDA9oaCgPbGwMD3h4GA9gYCAPYGAgD2RkJA9sbCwPbGwsD3BwMA96ejoPeHg4D3x8PA9+fj4PYmICD6JiAg+kZAQ"
    "Po2MDD6NjAw+jYwMPpWUFD6RkBA+lZQUPpGQED6dnBw+nZwcPp2cHD6ZmBg+lZQUPpWUFD6ZmBg+lZQUPpmYGD6JiAg+iYgIPo2M"
    "DD6RkBA+iYgIPo2MDD6JiAg+hYQEPoGAAD7x8PA98fDwPeno6D3Z2Ng94eDgPdHQ0D3R0NA94eDgPcHAwD25uLg9sbCwPYmIiD2J"
    "iAg+wcDAPbGwsD2xsLA9qaioPaGgoD2ZmJg9mZiYPYGAgD2BgIA98fBwPYGAgD3R0FA9wcBAPdHQUD3BwEA9sbAwPdHQUD2BgAA9"
    "kZAQPaGgID0="
)

RingPattern = numpy.load(io.BytesIO(base64.b64decode(_RING_PATTERN_NPY_B64)))

M_PI = math.pi

Int_MIN_VALUE = -2147483648
Int_MAX_VALUE = 2147483647


# ---------------------------------------------------------------------------
# numpy / Pillow replacements for the OpenCV and scipy primitives used upstream
# ---------------------------------------------------------------------------
def _cv2_dft_complex(img2d: numpy.ndarray) -> numpy.ndarray:
    """Equivalent of ``cv2.dft(x, flags=cv2.DFT_COMPLEX_OUTPUT)``.

    Returns a real valued ``(rows, cols, 2)`` array holding the real and
    imaginary parts of the unnormalised 2-D DFT (matches OpenCV's layout and
    normalisation).
    """
    transformed = numpy.fft.fft2(numpy.asarray(img2d, dtype=numpy.float32))
    return numpy.stack((transformed.real, transformed.imag), axis=-1)


def _cv2_idft_scale(spectrum: numpy.ndarray) -> numpy.ndarray:
    """Equivalent of ``cv2.idft(x, flags=cv2.DFT_SCALE)`` channel 0.

    ``spectrum`` is a ``(rows, cols, 2)`` real/imaginary array; the inverse DFT
    is normalised by ``1 / (rows * cols)`` which is OpenCV's ``DFT_SCALE``.
    Only the real part is returned (upstream always reads ``img_back[:, :, 0]``).
    """
    spectrum = numpy.asarray(spectrum)
    complex_spectrum = spectrum[..., 0] + 1j * spectrum[..., 1]
    return numpy.fft.ifft2(complex_spectrum).real


def _lanczos4_kernel(distance: numpy.ndarray) -> numpy.ndarray:
    """Lanczos-4 windowed sinc: ``sinc(d) * sinc(d / 4)``."""
    distance = numpy.asarray(distance, dtype=numpy.float64)
    out = numpy.ones_like(distance)
    nonzero = numpy.abs(distance) > 1e-9
    d = numpy.where(nonzero, distance, 1.0)
    out[nonzero] = (
        4.0
        * numpy.sin(numpy.pi * d[nonzero])
        * numpy.sin(numpy.pi * d[nonzero] / 4.0)
        / (numpy.pi * numpy.pi * d[nonzero] * d[nonzero])
    )
    return out


def _lanczos4_taps(dst_len: int, src_len: int):
    """Build source indices + normalised Lanczos-4 weights for one axis."""
    scale = src_len / dst_len
    position = (numpy.arange(dst_len) + 0.5) * scale - 0.5
    base = numpy.floor(position).astype(numpy.int64)
    index = base[:, None] + numpy.arange(-3, 5)[None, :]
    index = numpy.clip(index, 0, src_len - 1)
    distance = position[:, None] - (base[:, None] + numpy.arange(-3, 5)[None, :])
    weights = _lanczos4_kernel(distance)
    weights /= weights.sum(axis=1, keepdims=True)
    return index, weights


def _resize_lanczos4(arr: numpy.ndarray, dst_w: int, dst_h: int) -> numpy.ndarray:
    """2-D float32 resampling equivalent to ``cv2.resize(..., INTER_LANCZOS4)``.

    Separable 8 tap Lanczos-4 with border replication and per-output-pixel
    weight normalisation (verified against cv2 to float32 rounding for the
    exact 2x down/up-sampling NTSC-QT uses in ``_blur_chroma``).
    """
    source = numpy.asarray(arr, dtype=numpy.float32)
    src_h, src_w = source.shape
    dst_w = max(1, int(dst_w))
    dst_h = max(1, int(dst_h))
    if (dst_w, dst_h) == (src_w, src_h):
        return source.copy()
    out = source
    if dst_w != src_w:
        index, weights = _lanczos4_taps(dst_w, src_w)
        gathered = out[:, index]  # (src_h, dst_w, 8)
        out = numpy.einsum(
            "hdo,do->hd", gathered, weights.astype(numpy.float32), optimize=True
        ).astype(numpy.float32)
    if dst_h != src_h:
        index, weights = _lanczos4_taps(dst_h, src_h)
        gathered = out[index]  # (dst_h, 8, dst_w)
        out = numpy.einsum(
            "dow,do->dw", gathered, weights.astype(numpy.float32), optimize=True
        ).astype(numpy.float32)
    return out


def _resize_linear_1d(src: numpy.ndarray, dst_len: int) -> numpy.ndarray:
    """1-D equivalent of ``cv2.resize(src[None, :], (dst_len, 1), INTER_LINEAR)``."""
    source = numpy.asarray(src, dtype=numpy.float64)
    src_len = source.shape[0]
    dst_len = max(1, int(dst_len))
    if dst_len == src_len:
        return source.copy()
    if src_len == 1:
        return numpy.full(dst_len, float(source[0]))
    scale = src_len / dst_len
    position = (numpy.arange(dst_len) + 0.5) * scale - 0.5
    index = numpy.floor(position).astype(numpy.int64)
    fraction = position - index
    index0 = numpy.clip(index, 0, src_len - 1)
    index1 = numpy.clip(index + 1, 0, src_len - 1)
    return source[index0] * (1.0 - fraction) + source[index1] * fraction


def _shift_one(arr: numpy.ndarray) -> numpy.ndarray:
    """``scipy.ndimage.interpolation.shift(arr, 1)`` for an integer shift."""
    arr = numpy.asarray(arr)
    if arr.size == 0:
        return arr.copy()
    out = numpy.empty_like(arr)
    out[0] = 0
    out[1:] = arr[:-1]
    return out


def _iir_lowpass_1d(samples: numpy.ndarray, alpha: float, beta: float, prev: float) -> numpy.ndarray:
    """One pole IIR ``y[n] = alpha*x[n] + beta*y[n-1]`` (``beta = 1 - alpha``).

    ``prev`` is the previous *output* sample (``y[-1]``), i.e. the state used by
    ``scipy.signal.lfilter`` with ``lfiltic``.  Implemented as a blocked,
    exponentially scaled cumulative sum which is numerically stable for any
    ``beta`` in ``(0, 1)``:

        y[m] = alpha * beta**m * sum_{j<=m} x[j] * beta**(-j)
               + beta**(m+1) * prev
    """
    x = numpy.asarray(samples, dtype=numpy.float64)
    n = x.shape[0]
    out = numpy.empty(n, dtype=numpy.float64)
    if n == 0:
        return out
    if alpha == 0.0:
        out.fill(prev)
        return out
    if beta == 0.0:
        return alpha * x
    if beta >= 1.0:
        out.fill(prev)
        return out

    # Block length so that beta ** -(block-1) never overflows float64.  With a
    # 600 nat budget the largest scaled value is ~e**600 (~3.8e260), leaving
    # plenty of headroom for the int32 sample magnitudes used here.
    block = int(600.0 / -math.log(beta))
    block = max(1, min(block, n))
    for start in range(0, n, block):
        chunk = x[start:start + block]
        size = chunk.shape[0]
        exponents = numpy.arange(size, dtype=numpy.float64)
        scaled = numpy.cumsum(chunk * numpy.power(beta, -exponents))
        out[start:start + size] = (
            alpha * numpy.power(beta, exponents) * scaled
            + numpy.power(beta, exponents + 1.0) * prev
        )
        prev = out[start + size - 1]
    return out


def _iir_lowpass(samples: numpy.ndarray, alpha: float, beta: float, prev: float) -> numpy.ndarray:
    source = numpy.asarray(samples, dtype=numpy.float64)
    if source.ndim <= 1:
        return _iir_lowpass_1d(source, alpha, beta, prev)
    rows = source.reshape(-1, source.shape[-1])
    result = numpy.empty_like(rows)
    for row in range(rows.shape[0]):
        result[row] = _iir_lowpass_1d(rows[row], alpha, beta, prev)
    return result.reshape(source.shape)


class LowpassFilter:
    """Ported from NTSC-QT ``app/ntsc.py`` (behaviour unchanged)."""

    def __init__(self, rate: float, hz: float, value: float = 0.0):
        self.timeInterval: float = 1.0 / rate
        self.tau: float = 1 / (hz * 2.0 * M_PI)
        self.alpha: float = self.timeInterval / (self.tau + self.timeInterval)
        self.prev: float = value

    def lowpass(self, sample: float) -> float:
        stage1 = sample * self.alpha
        stage2 = self.prev - self.prev * self.alpha
        self.prev = stage1 + stage2
        return self.prev

    def highpass(self, sample: float) -> float:
        stage1 = sample * self.alpha
        stage2 = self.prev - self.prev * self.alpha
        self.prev = stage1 + stage2
        return sample - self.prev

    def lowpass_array(self, samples: numpy.ndarray) -> numpy.ndarray:
        return _iir_lowpass(samples, self.alpha, 1.0 - self.alpha, self.prev)

    def highpass_array(self, samples: numpy.ndarray) -> numpy.ndarray:
        lowpassed = self.lowpass_array(samples)
        return numpy.asarray(samples, dtype=numpy.float64) - lowpassed


def lowpassFilters(cutoff: float, reset: float, rate: float = 0.0) -> List[LowpassFilter]:
    if rate == 0.0:
        rate = Ntsc.NTSC_RATE
    return [LowpassFilter(rate, cutoff, reset) for _ in range(0, 3)]


# ---------------------------------------------------------------------------
# Artifact functions ported from upstream
# ---------------------------------------------------------------------------
def ringing(img2d, alpha=0.5, noiseSize=0, noiseValue=2, clip=True, seed=None):
    """
    https://bavc.github.io/avaa/artifacts/ringing.html
    :param img2d: 2d image
    :param alpha: float, reconstruction quality (0-1) optimal values for tv ringing modeling is 0.3-0.99
    :param noiseSize: float, noise size  (0-1) optimal values  is 0.5-0.99 if noiseSize=0 - no noise
    :param noiseValue: float, noise amplitude  (0-5) optimal values  is 0.5-2
    :return: 2d image
    """
    source = numpy.asarray(img2d)
    dft = _cv2_dft_complex(source)
    dft_shift = numpy.fft.fftshift(dft)

    rows, cols = source.shape
    crow, ccol = int(rows / 2), int(cols / 2)
    mask = numpy.zeros((rows, cols, 2), numpy.uint8)

    maskH = min(crow, int(1 + alpha * crow))
    mask[:, ccol - maskH:ccol + maskH] = 1

    if noiseSize > 0:
        noise = numpy.ones((mask.shape[0], mask.shape[1], mask.shape[2])) * noiseValue - noiseValue / 2.0
        start = int(ccol - ((1 - noiseSize) * ccol))
        stop = int(ccol + ((1 - noiseSize) * ccol))
        noise[:, start:stop, :] = 0
        rnd = numpy.random.RandomState(seed)
        mask = mask.astype(numpy.float64) + rnd.rand(mask.shape[0], mask.shape[1], mask.shape[2]) * noise - noise / 2.0

    img_back = _cv2_idft_scale(numpy.fft.ifftshift(dft_shift * mask))
    if clip:
        _min, _max = source.min(), source.max()
        return numpy.clip(img_back, _min, _max)
    return img_back


def ringing2(img2d, power=4, shift=0, clip=True):
    """
    https://bavc.github.io/avaa/artifacts/ringing.html
    :param img2d: 2d image
    :param power: int, ringing parrern poser (optimal 2 - 6)
    :return: 2d image
    """
    source = numpy.asarray(img2d)
    dft = _cv2_dft_complex(source)
    dft_shift = numpy.fft.fftshift(dft)

    rows, cols = source.shape

    scalecols = int(cols * (1 + shift))
    mask = _resize_linear_1d(RingPattern, scalecols)

    mask = mask[(scalecols // 2) - (cols // 2):(scalecols // 2) + (cols // 2)]
    mask = mask ** power
    img_back = _cv2_idft_scale(numpy.fft.ifftshift(dft_shift * mask[None, :, None]))
    if clip:
        _min, _max = source.min(), source.max()
        return numpy.clip(img_back, _min, _max)
    return img_back


def fmod(x: float, y: float) -> float:
    return x % y


class NumpyRandom:
    def __init__(self, seed=None):
        self.rnd = numpy.random.RandomState(seed)

    def nextInt(self, _from: int = Int_MIN_VALUE, until: int = Int_MAX_VALUE) -> int:
        return self.rnd.randint(_from, until)

    def nextIntArray(self, size: int, _from: int = Int_MIN_VALUE, until: int = Int_MAX_VALUE) -> numpy.ndarray:
        return self.rnd.randint(_from, until, size, dtype=numpy.int32)


class XorWowRandom:
    """Ported verbatim; kept for API completeness.

    ``VHSRenderer`` always uses :class:`NumpyRandom`, matching the upstream GUI
    default (``random_ntsc``), so this class is not exercised by the app.  On
    numpy >= 2 its int32 bit twiddling emits overflow RuntimeWarnings; behaviour
    is the same as upstream under numpy < 2.
    """

    def __init__(self, seed1: int, seed2: int):
        self.x: int = numpy.int32(seed1)
        self.y: int = numpy.int32(seed2)
        self.z: int = numpy.int32(0)
        self.w: int = numpy.int32(0)
        self.v: int = -numpy.int32(seed1) - 1
        self.addend: int = numpy.int32((numpy.int32(seed1) << 10) ^ (numpy.uint32(seed2) >> 4))
        [self._nextInt() for _ in range(0, 64)]

    def _nextInt(self) -> int:
        t = self.x
        t = numpy.int32(t ^ (numpy.uint32(t) >> 2))
        self.x = numpy.int32(self.y)
        self.y = numpy.int32(self.z)
        self.z = numpy.int32(self.w)
        v0 = numpy.int32(self.v)
        self.w = numpy.int32(v0)
        t = (t ^ (t << 1)) ^ v0 ^ (v0 << 4)
        self.v = numpy.int32(t)
        self.addend += 362437
        return t + numpy.int32(self.addend)

    def nextInt(self, _from: int = Int_MIN_VALUE, until: int = Int_MAX_VALUE) -> numpy.int32:
        n = until - _from
        if n > 0 or n == Int_MIN_VALUE:
            if (n & -n) == n:
                assert False, "not implemented"
            else:
                v: int = 0
                while True:
                    bits = numpy.uint32(self._nextInt()) >> 1
                    v = bits % n
                    if bits - v + (n - 1) >= 0:
                        break
                return numpy.int32(_from + v)
        else:
            r = range(_from, until)
            while True:
                rnd = self._nextInt()
                if rnd in r:
                    return numpy.int32(rnd)

    def nextIntArray(self, size: int, _from: int = Int_MIN_VALUE, until: int = Int_MAX_VALUE) -> numpy.ndarray:
        zeros = numpy.zeros(size, dtype=numpy.int32)
        for i in range(0, size):
            zeros[i] = self.nextInt(_from=_from, until=until)
        return zeros


# interleaved uint8 HWC BGR to -> planar int32 CHW YIQ
def bgr2yiq(bgrimg: numpy.ndarray) -> numpy.ndarray:
    planar = numpy.transpose(bgrimg, (2, 0, 1))
    b, g, r = planar
    dY = 0.30 * r + 0.59 * g + 0.11 * b

    Y = (dY * 256).astype(numpy.int32)
    I = (256 * (-0.27 * (b - dY) + 0.74 * (r - dY))).astype(numpy.int32)
    Q = (256 * (0.41 * (b - dY) + 0.48 * (r - dY))).astype(numpy.int32)
    return numpy.stack([Y, I, Q], axis=0).astype(numpy.int32)


# one field of planar int32 CHW YIQ -> one field of interleaved uint8 HWC BGR to
def yiq2bgr(yiq: numpy.ndarray, dst_bgr: numpy.ndarray = None, field: int = 0) -> numpy.ndarray:
    c, h, w = yiq.shape
    dst_bgr = dst_bgr if dst_bgr is not None else numpy.zeros((h, w, c))
    Y, I, Q = yiq
    if field == 0:
        Y, I, Q = Y[::2], I[::2], Q[::2]
    else:
        Y, I, Q = Y[1::2], I[1::2], Q[1::2]

    r = ((1.000 * Y + 0.956 * I + 0.621 * Q) / 256).astype(numpy.int32)
    g = ((1.000 * Y + -0.272 * I + -0.647 * Q) / 256).astype(numpy.int32)
    b = ((1.000 * Y + -1.106 * I + 1.703 * Q) / 256).astype(numpy.int32)
    r = numpy.clip(r, 0, 255)
    g = numpy.clip(g, 0, 255)
    b = numpy.clip(b, 0, 255)
    planarBGR = numpy.stack([b, g, r])
    interleavedBGR = numpy.transpose(planarBGR, (1, 2, 0))
    if field == 0:
        dst_bgr[::2] = interleavedBGR
    else:
        dst_bgr[1::2] = interleavedBGR
    return dst_bgr


def cut_black_line_border(image: numpy.ndarray, bordersize: int = None) -> None:
    h, w, _ = image.shape
    if bordersize is None:
        line_width = int(w * 0.017)  # 1.7%
    else:
        line_width = bordersize  # TODO: value to settings

    image[:, -1 * line_width:] = 0  # 0 set to black


def composite_lowpass(yiq: numpy.ndarray, field: int, fieldno: int):
    _, height, width = yiq.shape
    fY, fI, fQ = yiq
    for p in range(1, 3):
        cutoff = 1300000.0 if p == 1 else 600000.0
        delay = 2 if (p == 1) else 4
        P = fI if (p == 1) else fQ
        P = P[field::2]
        lp = lowpassFilters(cutoff, reset=0.0)
        for i, f in enumerate(P):
            f = lp[0].lowpass_array(f)
            f = lp[1].lowpass_array(f)
            f = lp[2].lowpass_array(f)
            P[i, 0:width - delay] = f.astype(numpy.int32)[delay:]


# lighter-weight filtering, probably what your old CRT does to reduce color fringes a bit
def composite_lowpass_tv(yiq: numpy.ndarray, field: int, fieldno: int):
    _, height, width = yiq.shape
    fY, fI, fQ = yiq
    for p in range(1, 3):
        delay = 1
        P = fI if (p == 1) else fQ
        P = P[field::2]
        lp = lowpassFilters(2600000.0, reset=0.0)
        for i, f in enumerate(P):
            f = lp[0].lowpass_array(f)
            f = lp[1].lowpass_array(f)
            f = lp[2].lowpass_array(f)
            P[i, 0:width - delay] = f.astype(numpy.int32)[delay:]


def composite_preemphasis(yiq: numpy.ndarray, field: int, composite_preemphasis: float,
                          composite_preemphasis_cut: float):
    fY, fI, fQ = yiq
    pre = LowpassFilter(Ntsc.NTSC_RATE, composite_preemphasis_cut, 16.0)
    fields = fY[field::2]
    for i, samples in enumerate(fields):
        filtered = samples + pre.highpass_array(samples) * composite_preemphasis
        fields[i] = filtered.astype(numpy.int32)


class VHSSpeed:
    """Ported ``VHSSpeed`` enum; plain class keeps numpy-only imports.

    The upstream ``Enum`` members are reproduced as class attributes so that
    ``VHSSpeed.VHS_SP`` etc. compare and read identically.
    """

    def __init__(self, name: str, luma_cut: float, chroma_cut: float, chroma_delay: int):
        self.name = name
        self.luma_cut = luma_cut
        self.chroma_cut = chroma_cut
        self.chroma_delay = chroma_delay

    def __repr__(self) -> str:
        return f"VHSSpeed.{self.name}"

    def __eq__(self, other) -> bool:
        return isinstance(other, VHSSpeed) and self is other

    def __hash__(self) -> int:
        return hash(self.name)


VHSSpeed.VHS_SP = VHSSpeed("VHS_SP", 2400000.0, 320000.0, 9)
VHSSpeed.VHS_LP = VHSSpeed("VHS_LP", 1900000.0, 300000.0, 12)
VHSSpeed.VHS_EP = VHSSpeed("VHS_EP", 1400000.0, 280000.0, 14)


class Ntsc:
    # https://en.wikipedia.org/wiki/NTSC
    NTSC_RATE = 315000000.00 / 88 * 4  # 315/88 Mhz rate * 4

    def __init__(self, precise=False, random=None):
        self.precise = precise
        self.random = random if random is not None else XorWowRandom(31374242, 0)
        self._composite_preemphasis_cut = 1000000.0
        # analog artifacts related to anything that affects the raw composite signal i.e. CATV modulation
        self._composite_preemphasis = 0.0  # values 0..8 look realistic

        self._vhs_out_sharpen = 1.5  # 1.0..5.0

        self._vhs_edge_wave = 0  # 0..10

        self._vhs_head_switching = False  # turn this on only on frames height 486 pixels or more
        self._head_switching_speed = 0  # 0..100 this is /1000 increment for _vhs_head_switching_point 0 is static
        self._vhs_head_switching_point = 1.0 - (4.5 + 0.01) / 262.5  # 4 scanlines NTSC up from vsync
        self._vhs_head_switching_phase = (1.0 - 0.01) / 262.5  # 4 scanlines NTSC up from vsync
        self._vhs_head_switching_phase_noise = 1.0 / 500 / 262.5  # 1/500th of a scanline

        self._color_bleed_before = True  # color bleed comes before other degradations if True or after otherwise
        self._color_bleed_horiz = 0  # horizontal color bleeding 0 = no color bleed, 1..10 sane values
        self._color_bleed_vert = 0  # vertical color bleeding  0 = no color bleed, 1..10 sane values
        self._ringing = 1.0  # 1 = no ringing, 0.3..0.99 = sane values
        self._enable_ringing2 = False
        self._ringing_power = 2
        self._ringing_shift = 0
        self._freq_noise_size = 0  # (0-1) optimal values  is 0.5..0.99 if noiseSize=0 - no noise
        self._freq_noise_amplitude = 2  # noise amplitude  (0-5) optimal values  is 0.5-2
        self._composite_in_chroma_lowpass = True  # apply chroma lowpass before composite encode
        self._composite_out_chroma_lowpass = True
        self._composite_out_chroma_lowpass_lite = True

        self._video_chroma_noise = 0  # 0..16384
        self._video_chroma_phase_noise = 0  # 0..50
        self._video_chroma_loss = 0  # 0..100_000
        self._video_noise = 2  # 0..4200
        self._subcarrier_amplitude = 50
        self._subcarrier_amplitude_back = 50
        self._emulating_vhs = False
        self._nocolor_subcarrier = False  # if set, emulate subcarrier but do not decode back to color (debug)
        self._vhs_chroma_vert_blend = True  # if set, and VHS, blend vertically the chroma scanlines (as the VHS format does)
        self._vhs_svideo_out = False  # if not set, and VHS, video is recombined as if composite out on VCR

        self._output_ntsc = True  # NTSC color subcarrier emulation
        self._video_scanline_phase_shift = 180
        self._video_scanline_phase_shift_offset = 0  # 0..4
        self._output_vhs_tape_speed = VHSSpeed.VHS_SP

        self._black_line_cut = False  # Add black line glitch (credits to @rgm89git)

    def rand(self) -> numpy.int32:
        return self.random.nextInt(_from=0)

    def rand_array(self, size: int) -> numpy.ndarray:
        return self.random.nextIntArray(size, 0, Int_MAX_VALUE)

    def video_noise(self, yiq: numpy.ndarray, field: int, video_noise: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        noise_mod = video_noise * 2 + 1
        fields = fY[field::2]
        fh, fw = fields.shape
        if not self.precise:  # this one works FAST
            lp = LowpassFilter(1, 1, 0)
            lp.alpha = 0.5
            rnds = self.rand_array(fw * fh) % noise_mod - video_noise
            noises = _shift_one(lp.lowpass_array(rnds).astype(numpy.int32))
            fields += noises.reshape(fields.shape)
        else:  # this one works EXACTLY like original code
            noise = 0
            for field1 in fields:
                rnds = self.rand_array(fw) % noise_mod - video_noise
                for x in range(0, fw):
                    field1[x] += noise
                    noise += rnds[x]
                    noise = int(noise / 2)

    # https://bavc.github.io/avaa/artifacts/chrominance_noise.html
    def video_chroma_noise(self, yiq: numpy.ndarray, field: int, video_chroma_noise: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq

        noise_mod = video_chroma_noise * 2 + 1
        U = fI[field::2]
        V = fQ[field::2]
        fh, fw = U.shape
        if not self.precise:
            lp = LowpassFilter(1, 1, 0)
            lp.alpha = 0.5
            rndsU = self.rand_array(fw * fh) % noise_mod - video_chroma_noise
            noisesU = _shift_one(lp.lowpass_array(rndsU).astype(numpy.int32))

            rndsV = self.rand_array(fw * fh) % noise_mod - video_chroma_noise
            noisesV = _shift_one(lp.lowpass_array(rndsV).astype(numpy.int32))

            U += noisesU.reshape(U.shape)
            V += noisesV.reshape(V.shape)
        else:
            noiseU = 0
            noiseV = 0
            for y in range(0, fh):
                for x in range(0, fw):
                    U[y][x] += noiseU
                    noiseU += self.rand() % noise_mod - video_chroma_noise
                    noiseU = int(noiseU / 2)

                    V[y][x] += noiseV
                    noiseV += self.rand() % noise_mod - video_chroma_noise
                    noiseV = int(noiseV / 2)

    def video_chroma_phase_noise(self, yiq: numpy.ndarray, field: int, video_chroma_phase_noise: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        noise_mod = video_chroma_phase_noise * 2 + 1
        U = fI[field::2]
        V = fQ[field::2]
        fh, fw = U.shape
        noise = 0
        for y in range(0, fh):
            noise += self.rand() % noise_mod - video_chroma_phase_noise
            noise = int(noise / 2)
            pi = noise * M_PI / 100
            sinpi = math.sin(pi)
            cospi = math.cos(pi)
            u = U[y] * cospi - V[y] * sinpi
            v = U[y] * sinpi + V[y] * cospi
            U[y, :] = u
            V[y, :] = v

    def vhs_head_switching(self, yiq: numpy.ndarray, field: int = 0):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        twidth = width + width // 10
        shy = 0
        noise = 0.0
        if self._vhs_head_switching_phase_noise != 0.0:
            x = numpy.int32(random.randint(1, 2000000000))
            noise = x / 1000000000.0 - 1.0
            noise *= self._vhs_head_switching_phase_noise

        t = twidth * (262.5 if self._output_ntsc else 312.5)
        p = int(fmod(self._vhs_head_switching_point + noise, 1.0) * t)
        self._vhs_head_switching_point += self._head_switching_speed / 1000
        y = int(p // twidth * 2) + field
        p = int(fmod(self._vhs_head_switching_phase + noise, 1.0) * t)
        x = p % twidth
        y -= (262 - 240) * 2 if self._output_ntsc else (312 - 288) * 2
        tx = x
        ishif = x - twidth if x >= twidth // 2 else x
        shif = 0
        while y < height:
            if y >= 0:
                Y = fY[y]
                if shif != 0:
                    tmp = numpy.zeros(twidth)
                    x2 = (tx + twidth + shif) % twidth
                    tmp[:width] = Y

                    x = tx
                    while x < width:
                        Y[x] = tmp[x2]
                        x2 += 1
                        if x2 == twidth:
                            x2 = 0
                        x += 1

            shif = ishif if shy == 0 else int(shif * 7 / 8)
            tx = 0
            y += 2
            shy += 1

    _Umult = numpy.array([1, 0, -1, 0], dtype=numpy.int32)
    _Vmult = numpy.array([0, 1, 0, -1], dtype=numpy.int32)

    def _chroma_luma_xi(self, fieldno: int, y: int):
        if self._video_scanline_phase_shift == 90:
            return int(fieldno + self._video_scanline_phase_shift_offset + (y >> 1)) & 3
        elif self._video_scanline_phase_shift == 180:
            return int(((((fieldno + y) & 2) + self._video_scanline_phase_shift_offset) & 3))
        elif self._video_scanline_phase_shift == 270:
            return int(((fieldno + self._video_scanline_phase_shift_offset) & 3))
        else:
            return int(self._video_scanline_phase_shift_offset & 3)

    def chroma_into_luma(self, yiq: numpy.ndarray, field: int, fieldno: int, subcarrier_amplitude: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        y = field
        umult = numpy.tile(Ntsc._Umult, int((width / 4) + 1))
        vmult = numpy.tile(Ntsc._Vmult, int((width / 4) + 1))
        while y < height:
            Y = fY[y]
            I = fI[y]
            Q = fQ[y]
            xi = self._chroma_luma_xi(fieldno, y)

            chroma = I * subcarrier_amplitude * umult[xi:xi + width]
            chroma += Q * subcarrier_amplitude * vmult[xi:xi + width]
            Y[:] = Y + chroma.astype(numpy.int32) // 50
            I[:] = 0
            Q[:] = 0
            y += 2

    def chroma_from_luma(self, yiq: numpy.ndarray, field: int, fieldno: int, subcarrier_amplitude: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        chroma = numpy.zeros(width, dtype=numpy.int32)
        for y in range(field, height, 2):
            Y = fY[y]
            I = fI[y]
            Q = fQ[y]
            sum: int = Y[0] + Y[1]
            y2 = numpy.pad(Y[2:], (0, 2))
            yd4 = numpy.pad(Y[:-2], (2, 0))
            sums = y2 - yd4
            sums0 = numpy.concatenate([numpy.array([sum], dtype=numpy.int32), sums])
            acc = numpy.add.accumulate(sums0, dtype=numpy.int32)[1:]
            acc4 = acc // 4
            chroma = y2 - acc4
            Y[:] = acc4

            xi = self._chroma_luma_xi(fieldno, y)

            x = 4 - xi & 3
            # // flip the part of the sine wave that would correspond to negative U and V values
            chroma[x + 2::4] = -chroma[x + 2::4]
            chroma[x + 3::4] = -chroma[x + 3::4]

            chroma = (chroma * 50 / subcarrier_amplitude)

            # decode the color right back out from the subcarrier we generated
            cxi = -chroma[xi::2]
            cxi1 = -chroma[xi + 1::2]
            I[::2] = numpy.pad(cxi, (0, width // 2 - cxi.shape[0]))
            Q[::2] = numpy.pad(cxi1, (0, width // 2 - cxi1.shape[0]))

            I[1:width - 2:2] = (I[:width - 2:2] + I[2::2]) >> 1
            Q[1:width - 2:2] = (Q[:width - 2:2] + Q[2::2]) >> 1
            I[width - 2:] = 0
            Q[width - 2:] = 0

    def vhs_luma_lowpass(self, yiq: numpy.ndarray, field: int, luma_cut: float):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        for Y in fY[field::2]:
            pre = LowpassFilter(Ntsc.NTSC_RATE, luma_cut, 16.0)
            lp = lowpassFilters(cutoff=luma_cut, reset=16.0)
            f0 = lp[0].lowpass_array(Y)
            f1 = lp[1].lowpass_array(f0)
            f2 = lp[2].lowpass_array(f1)
            f3 = f2 + pre.highpass_array(f2) * 1.6
            Y[:] = f3

    def vhs_chroma_lowpass(self, yiq: numpy.ndarray, field: int, chroma_cut: float, chroma_delay: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        for U in fI[field::2]:
            lpU = lowpassFilters(cutoff=chroma_cut, reset=0.0)
            f0 = lpU[0].lowpass_array(U)
            f1 = lpU[1].lowpass_array(f0)
            f2 = lpU[2].lowpass_array(f1)
            U[:width - chroma_delay] = f2[chroma_delay:]

        for V in fQ[field::2]:
            lpV = lowpassFilters(cutoff=chroma_cut, reset=0.0)
            f0 = lpV[0].lowpass_array(V)
            f1 = lpV[1].lowpass_array(f0)
            f2 = lpV[2].lowpass_array(f1)
            V[:width - chroma_delay] = f2[chroma_delay:]

    # VHS decks also vertically smear the chroma subcarrier using a delay line
    # to add the previous line's color subcarrier to the current line's color subcarrier.
    # note that phase changes in NTSC are compensated for by the VHS deck to make the
    # phase line up per scanline (else summing the previous line's carrier would
    # cancel it out).
    def vhs_chroma_vert_blend(self, yiq: numpy.ndarray, field: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        U2 = fI[field + 2::2, ]
        V2 = fQ[field + 2::2, ]
        delayU = numpy.pad(U2[:-1, ], [[1, 0], [0, 0]])
        delayV = numpy.pad(V2[:-1, ], [[1, 0], [0, 0]])
        fI[field + 2::2, ] = (delayU + U2 + 1) >> 1
        fQ[field + 2::2, ] = (delayV + V2 + 1) >> 1

    def vhs_sharpen(self, yiq: numpy.ndarray, field: int, luma_cut: float):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        for Y in fY[field::2]:
            lp = lowpassFilters(cutoff=luma_cut * 4, reset=0.0)
            s = Y
            ts = lp[0].lowpass_array(Y)
            ts = lp[1].lowpass_array(ts)
            ts = lp[2].lowpass_array(ts)
            Y[:] = (s + (s - ts) * self._vhs_out_sharpen * 2.0)

    # http://www.michaeldvd.com.au/Articles/VideoArtefacts/VideoArtefactsColourBleeding.html
    # https://bavc.github.io/avaa/artifacts/yc_delay_error.html
    def color_bleed(self, yiq: numpy.ndarray, field: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq

        field_ = fI[field::2]
        h, w = field_.shape
        fI[field::2] = numpy.pad(field_, ((self._color_bleed_vert, 0), (self._color_bleed_horiz, 0)))[0:h, 0:w]

        field_ = fQ[field::2]
        h, w = field_.shape
        fQ[field::2] = numpy.pad(field_, ((self._color_bleed_vert, 0), (self._color_bleed_horiz, 0)))[0:h, 0:w]

    def vhs_edge_wave(self, yiq: numpy.ndarray, field: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        rnds = self.random.nextIntArray(height // 2, 0, self._vhs_edge_wave)
        lp = LowpassFilter(Ntsc.NTSC_RATE, self._output_vhs_tape_speed.luma_cut,
                           0)  # no real purpose to initialize it with ntsc values
        rnds = lp.lowpass_array(rnds).astype(numpy.int32)

        for y, Y in enumerate(fY[field::2]):
            if rnds[y] != 0:
                shift = rnds[y]
                Y[:] = numpy.pad(Y, (shift, 0))[:-shift]
        for y, I in enumerate(fI[field::2]):
            if rnds[y] != 0:
                shift = rnds[y]
                I[:] = numpy.pad(I, (shift, 0))[:-shift]
        for y, Q in enumerate(fQ[field::2]):
            if rnds[y] != 0:
                shift = rnds[y]
                Q[:] = numpy.pad(Q, (shift, 0))[:-shift]

    def vhs_chroma_loss(self, yiq: numpy.ndarray, field: int, video_chroma_loss: int):
        _, height, width = yiq.shape
        fY, fI, fQ = yiq
        for y in range(field, height, 2):
            U = fI[y]
            V = fQ[y]
            if self.rand() % 100000 < video_chroma_loss:
                U[:] = 0
                V[:] = 0

    def emulate_vhs(self, yiq: numpy.ndarray, field: int, fieldno: int):
        vhs_speed = self._output_vhs_tape_speed
        if self._vhs_edge_wave != 0:
            self.vhs_edge_wave(yiq, field)

        self.vhs_luma_lowpass(yiq, field, vhs_speed.luma_cut)

        self.vhs_chroma_lowpass(yiq, field, vhs_speed.chroma_cut, vhs_speed.chroma_delay)

        if self._vhs_chroma_vert_blend and self._output_ntsc:
            self.vhs_chroma_vert_blend(yiq, field)

        if True:  # TODO: make option
            self.vhs_sharpen(yiq, field, vhs_speed.luma_cut)

        if not self._vhs_svideo_out:
            self.chroma_into_luma(yiq, field, fieldno, self._subcarrier_amplitude)
            self.chroma_from_luma(yiq, field, fieldno, self._subcarrier_amplitude)

    def composite_layer(self, dst: numpy.ndarray, src: numpy.ndarray, field: int, fieldno: int):
        assert dst.shape == src.shape, "dst and src images must be of same shape"

        if self._black_line_cut:
            cut_black_line_border(src)

        yiq = bgr2yiq(src)
        if self._color_bleed_before and (self._color_bleed_vert != 0 or self._color_bleed_horiz != 0):
            self.color_bleed(yiq, field)

        if self._composite_in_chroma_lowpass:
            composite_lowpass(yiq, field, fieldno)

        if self._ringing != 1.0:
            self.ringing(yiq, field)

        self.chroma_into_luma(yiq, field, fieldno, self._subcarrier_amplitude)

        if self._composite_preemphasis != 0.0 and self._composite_preemphasis_cut > 0:
            composite_preemphasis(yiq, field, self._composite_preemphasis, self._composite_preemphasis_cut)

        if self._video_noise != 0:
            self.video_noise(yiq, field, self._video_noise)

        if self._vhs_head_switching:
            self.vhs_head_switching(yiq, field)

        if not self._nocolor_subcarrier:
            self.chroma_from_luma(yiq, field, fieldno, self._subcarrier_amplitude_back)

        if self._video_chroma_noise != 0:
            self.video_chroma_noise(yiq, field, self._video_chroma_noise)

        if self._video_chroma_phase_noise != 0:
            self.video_chroma_phase_noise(yiq, field, self._video_chroma_phase_noise)

        if self._emulating_vhs:
            self.emulate_vhs(yiq, field, fieldno)

        if self._video_chroma_loss != 0:
            self.vhs_chroma_loss(yiq, field, self._video_chroma_loss)

        if self._composite_out_chroma_lowpass:
            if self._composite_out_chroma_lowpass_lite:
                composite_lowpass_tv(yiq, field, fieldno)
            else:
                composite_lowpass(yiq, field, fieldno)

        if not self._color_bleed_before and (self._color_bleed_vert != 0 or self._color_bleed_horiz != 0):
            self.color_bleed(yiq, field)

        # if self._ringing != 1.0:
        #     self.ringing(yiq, field)

        Y, I, Q = yiq

        # simulate 2x less bandwidth for chroma components, just like yuv420
        I[field::2] = self._blur_chroma(I[field::2])
        Q[field::2] = self._blur_chroma(Q[field::2])

        return yiq2bgr(yiq)

    def _blur_chroma(self, chroma: numpy.ndarray) -> numpy.ndarray:
        h, w = chroma.shape
        if w < 2 or h < 2:
            return chroma.astype(numpy.int32)
        down2 = _resize_lanczos4(chroma.astype(numpy.float32), w // 2, h // 2)
        return _resize_lanczos4(down2, w, h).astype(numpy.int32)

    def ringing(self, yiq: numpy.ndarray, field: int):
        Y, I, Q = yiq
        sz = self._freq_noise_size
        amp = self._freq_noise_amplitude
        shift = self._ringing_shift
        if not self._enable_ringing2:
            Y[field::2] = ringing(Y[field::2], self._ringing, noiseSize=sz, noiseValue=amp, clip=False)
            I[field::2] = ringing(I[field::2], self._ringing, noiseSize=sz, noiseValue=amp, clip=False)
            Q[field::2] = ringing(Q[field::2], self._ringing, noiseSize=sz, noiseValue=amp, clip=False)
        else:
            Y[field::2] = ringing2(Y[field::2], power=self._ringing_power, shift=shift, clip=False)
            I[field::2] = ringing2(I[field::2], power=self._ringing_power, shift=shift, clip=False)
            Q[field::2] = ringing2(Q[field::2], power=self._ringing_power, shift=shift, clip=False)


# ---------------------------------------------------------------------------
# Headless renderer: re-implementation of NTSC-QT DefaultRenderer
# (``prepare_frame`` + ``produce_frame`` + ``apply_main_effect``)
# ---------------------------------------------------------------------------
def _convert_scale_abs(frame: numpy.ndarray) -> numpy.ndarray:
    """Equivalent of ``cv2.convertScaleAbs(frame)`` (alpha=1, beta=0)."""
    array = numpy.asarray(frame)
    if array.dtype.kind == "f":
        return numpy.clip(numpy.rint(numpy.abs(array)), 0.0, 255.0).astype(numpy.uint8)
    return numpy.clip(numpy.abs(array.astype(numpy.int64)), 0, 255).astype(numpy.uint8)


def _expand_to_4width(img: numpy.ndarray) -> numpy.ndarray:
    """Ported from upstream ``app/funcs.py::expand_to_4width``."""
    height, width = img.shape[:2]
    delta = width % 4
    if delta != 0:
        img = numpy.concatenate((img, img[:, -1:(delta + 1) * -1:-1]), axis=1)
    return img


def _resize_frame_bgr(frame: numpy.ndarray, width: int, height: int) -> numpy.ndarray:
    """Bilinear BGR frame resize (Pillow replacement for ``cv2.resize``)."""
    from PIL import Image

    image = Image.fromarray(numpy.ascontiguousarray(frame))
    resample = getattr(Image, "Resampling", Image).BILINEAR
    return numpy.asarray(image.resize((int(width), int(height)), resample), dtype=numpy.uint8)


def _scaled_width(width: int, height: int, target_height: int) -> int:
    """Even render width keeping the aspect ratio (upstream ``resize_to_height``)."""
    if height <= 0:
        raise ValueError("frame height must be positive")
    value = int(round(width * (target_height / height)))
    value -= value % 2
    return max(2, value)


def apply_main_effect(ntsc: Ntsc, frame1: numpy.ndarray, frame2: numpy.ndarray = None) -> numpy.ndarray:
    """Headless equivalent of NTSC-QT ``DefaultRenderer.apply_main_effect``.

    Kept identical, including the fact that upstream passes ``dst=frame1,
    src=frame2`` to ``composite_layer`` (which only reads ``src``) and the
    interlaced half-line blend below.
    """
    if frame2 is None:
        frame2 = frame1

    frame = ntsc.composite_layer(frame1, frame2, field=0, fieldno=1)
    frame = _convert_scale_abs(frame)
    frame[1:-1:2] = frame[0:-2:2] / 2 + frame[2::2] / 2
    return frame


# ---------------------------------------------------------------------------
# Per-mode "smart filter tuning" presets (simplified in the AI build)
# ---------------------------------------------------------------------------
# Only values that actually differ from the ported Ntsc defaults are stored;
# everything else inherits the upstream defaults (verified attribute-for-
# attribute against the previous explicit preset dump and frame-hash parity).
#   _emulating_vhs/_enable_ringing2 -> turn the whole VHS deck + ringing2 on
#   _output_vhs_tape_speed          -> SP/LP/EP deck (luma/chroma cutoff, delay)
#   _vhs_out_sharpen, _vhs_edge_wave, _ringing/_ringing_power -> tape sharpness,
#                                     scanline tearing, frequency-domain halo
#   _color_bleed_horiz/_vert        -> Y/C delay chroma smear
#   _video_noise/_video_chroma_noise/_video_chroma_phase_noise/_video_chroma_loss
#   _composite_preemphasis          -> composite signal high-frequency boost
# ``_vhs_head_switching`` stays False on purpose: upstream warns it is only safe
# for frames >= 486 px tall and all VHS modes render at 240-288 px.
_MODE_COMMON = {"_emulating_vhs": True, "_enable_ringing2": True}

MODE_TUNING = {
    # 240p: heaviest artifacts, EP (extended play) tape
    "240p": {
        "_output_vhs_tape_speed": "VHS_EP",
        "_vhs_out_sharpen": 2.2,
        "_vhs_edge_wave": 4,
        "_ringing": 0.55,
        "_ringing_power": 3,
        "_color_bleed_horiz": 3,
        "_color_bleed_vert": 2,
        "_video_noise": 1800,
        "_video_chroma_noise": 6000,
        "_video_chroma_phase_noise": 18,
        "_video_chroma_loss": 1200,
        "_composite_preemphasis": 3.2,
    },
    # 288p: medium artifacts, LP tape
    "288p": {
        "_output_vhs_tape_speed": "VHS_LP",
        "_vhs_out_sharpen": 1.9,
        "_vhs_edge_wave": 2,
        "_ringing": 0.70,
        "_color_bleed_horiz": 2,
        "_color_bleed_vert": 1,
        "_video_noise": 1100,
        "_video_chroma_noise": 3800,
        "_video_chroma_phase_noise": 10,
        "_video_chroma_loss": 600,
        "_composite_preemphasis": 2.0,
    },
    # 360p: lightest artifacts, SP tape (pipeline renders "360p" at 288 px)
    "360p": {
        "_output_vhs_tape_speed": "VHS_SP",
        "_vhs_out_sharpen": 1.6,
        "_ringing": 0.82,
        "_color_bleed_horiz": 1,
        "_color_bleed_vert": 1,
        "_video_noise": 600,
        "_video_chroma_noise": 2000,
        "_video_chroma_phase_noise": 5,
        "_video_chroma_loss": 250,
        "_composite_preemphasis": 1.2,
    },
}

VHS_MODES = ("240p", "288p", "360p")

# Fixed default seed: the effect is reproducible for the same input and mode.
# Pass an explicit integer seed to vary the noise/tearing pattern.
DEFAULT_SEED = 31374242


def apply_mode_tuning(ntsc: Ntsc, mode: str) -> Ntsc:
    """Apply the named mode preset to an ``Ntsc`` instance (in place)."""
    if mode not in MODE_TUNING:
        raise ValueError(f"unknown VHS mode {mode!r}; expected one of {VHS_MODES}")
    for name, value in _MODE_COMMON.items():
        setattr(ntsc, name, value)
    for name, value in MODE_TUNING[mode].items():
        if name == "_output_vhs_tape_speed":
            value = getattr(VHSSpeed, value)
        setattr(ntsc, name, value)
    return ntsc


class VHSRenderer:
    """Stateful frame renderer reproducing NTSC-QT's ``DefaultRenderer``.

    ``process_frame(frame_bgr, next_frame_bgr=None)`` accepts an interleaved
    uint8 BGR frame and returns the processed frame at render size (height =
    ``height``, width = first frame's width, or the even aspect-correct width if
    the first frame is not already at render height).

    ``next_frame_bgr`` mirrors NTSC-QT's "next frame context": when supplied,
    the upstream renderer feeds it as ``src`` to ``Ntsc.composite_layer`` (the
    effect is computed from the *next* frame's pixels), exactly like
    ``DefaultRenderer.produce_frame``.  The pipeline in ``vhs_video`` passes
    ``None`` so that each output frame corresponds to its own input frame.
    """

    def __init__(self, mode: str, height: int, seed: int | None = None):
        if mode not in MODE_TUNING:
            raise ValueError(f"unknown VHS mode {mode!r}; expected one of {VHS_MODES}")
        self.mode = mode
        self.height = int(height)
        if self.height < 2:
            raise ValueError("render height must be >= 2")
        self.seed = DEFAULT_SEED if seed is None else int(seed)
        self.ntsc = Ntsc(random=NumpyRandom(self.seed))
        apply_mode_tuning(self.ntsc, self.mode)
        self.render_width: int | None = None

    @property
    def render_size(self) -> tuple[int, int] | None:
        """``(width, height)`` after the first frame, else ``None``."""
        if self.render_width is None:
            return None
        return (self.render_width, self.height)

    def _prepare(self, frame) -> numpy.ndarray:
        array = numpy.asarray(frame)
        if array.ndim != 3 or array.shape[2] != 3:
            raise ValueError(f"expected an HxWx3 BGR frame, got shape {array.shape}")
        if array.dtype != numpy.uint8:
            raise TypeError(f"expected a uint8 BGR frame, got dtype {array.dtype}")
        height, width = array.shape[:2]
        if self.render_width is None:
            self.render_width = width if height == self.height else _scaled_width(width, height, self.height)
        if (height, width) != (self.height, self.render_width):
            array = _resize_frame_bgr(array, self.render_width, self.height)
        if self.render_width % 4 != 0:
            array = _expand_to_4width(array)
        return numpy.ascontiguousarray(array)

    def process_frame(self, frame_bgr, next_frame_bgr=None) -> numpy.ndarray:
        frame1 = self._prepare(frame_bgr)
        if next_frame_bgr is None:
            frame2 = None
        else:
            frame2 = self._prepare(next_frame_bgr)
            if frame2.shape != frame1.shape:
                raise ValueError("next frame does not match the render size")
        frame = apply_main_effect(self.ntsc, frame1, frame2)
        return frame[:, 0:self.render_width]


def make_renderer(mode: str, height: int, seed: int | None = None) -> VHSRenderer:
    """Create a :class:`VHSRenderer` for ``mode`` at ``height`` pixels.

    :param mode: one of :data:`VHS_MODES` (``"240p"``, ``"288p"``, ``"360p"``)
    :param height: render height in pixels (240 / 288 for the app modes)
    :param seed: deterministic noise seed, ``None`` uses :data:`DEFAULT_SEED`
    """
    return VHSRenderer(mode, height, seed)
