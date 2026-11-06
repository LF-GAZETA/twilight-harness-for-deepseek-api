"""VHS audio degradation chain built on Spotify's ``pedalboard``.

The chain reproduces the "old tape / worn VHS" audio processing used by the
video pipeline:

    Distortion(drive_db=0.8)
        -> HighpassFilter(cutoff_frequency_hz=100)   # drop tape rumble
        -> LowpassFilter(cutoff_frequency_hz=6000)   # tape bandwidth limit
        -> Gain(gain_db=12.0)                        # hot, saturated output

Pedalboard is GPL-3.0 licensed; see ``docs/third-party/pedalboard-LICENSE.txt``.
It intentionally imported lazily so that importing this module (or the video
pipeline) never fails on installs/builds without pedalboard: a clear
RuntimeError is raised only when audio processing is actually requested.
"""

from __future__ import annotations

from pathlib import Path

#: Ordered chain definition (kept as data so it is easy to document/test).
VHS_AUDIO_CHAIN_SPEC = (
    ("Distortion", {"drive_db": 0.8}),
    ("HighpassFilter", {"cutoff_frequency_hz": 100}),
    ("LowpassFilter", {"cutoff_frequency_hz": 6000}),
    ("Gain", {"gain_db": 12.0}),
)

AUDIO_CHAIN_DESCRIPTION = (
    "Distortion(drive_db=0.8) -> HighpassFilter(100 Hz) -> "
    "LowpassFilter(6000 Hz) -> Gain(12 dB)"
)


def _pedalboard():
    try:
        import pedalboard  # noqa: PLC0415  (lazy: keeps app import light)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "pedalboard is not available; the VHS audio chain is disabled"
        ) from exc
    return pedalboard


def build_chain():
    """Build and return the ``pedalboard.Pedalboard`` VHS audio chain."""
    pedalboard = _pedalboard()
    plugins = []
    for plugin_name, params in VHS_AUDIO_CHAIN_SPEC:
        plugin_class = getattr(pedalboard, plugin_name)
        plugins.append(plugin_class(**params))
    return pedalboard.Pedalboard(plugins)


def process_audio_wav(in_wav: str | Path, out_wav: str | Path) -> Path:
    """Apply the VHS audio chain to ``in_wav`` and write ``out_wav``.

    ``in_wav`` must be a PCM WAV file (the video pipeline extracts it with
    ffmpeg; 16-bit stereo PCM is what ``run_vhs_video`` produces).  Returns the
    output path.  The chain is applied in one pass over the whole file, keeping
    the source sample rate and channel count.  Output is written as 32-bit float
    WAV so the +12 dB gain stage is not clipped by the intermediate file (the
    AAC muxing step is where the final, saturated level is encoded).
    """
    pedalboard = _pedalboard()  # noqa: F841  (validates availability early)
    from pedalboard.io import AudioFile

    source_path = Path(in_wav)
    target_path = Path(out_wav)

    with AudioFile(str(source_path)) as source:
        samplerate = source.samplerate
        frames = source.frames
        audio = source.read(frames)

    if frames > 0 and audio.size > 0:
        board = build_chain()
        audio = board(audio, samplerate, reset=True)

    with AudioFile(str(target_path), "w", samplerate, audio.shape[0]) as target:
        target.write(audio)

    return target_path
