"""Where ffmpeg is and which audio containers it may read: shared by render, voice and polish.

Dependency-light on purpose (standard library only), so that voice and polish find ffmpeg without importing the
renderer, and the renderer does not import them back for it. Running ffmpeg with deadlines lives in proclife.py.
"""
from __future__ import annotations

import shutil

# ffmpeg sniffs the format from content, so a file named .mp3 could really be a concat/playlist script that pulls in
# other files. Only these real audio containers are accepted for audio inputs, local files only.
AUDIO_FORMATS = "wav,mp3,ogg,flac,mov,aac,matroska,webm"


def ffmpeg_exe() -> str | None:
    """ffmpeg on PATH, else the static build from the optional imageio-ffmpeg package (`pip install imageio-ffmpeg`,
    or the `ffmpeg` extra), which works where no system package is available."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None
