"""Future provider boundary; phase 1 never uploads media or falls back to local ASR."""
from typing import Protocol
from pathlib import Path


class TranscriptionProvider(Protocol):
    def transcribe(self, audio_path: Path) -> list[dict]:
        """Return [{start: seconds, end: seconds, text: str}], or raise on failure."""
        ...


def status(library):
    # Model keys are isolated from Bilibili cookies. No provider is enabled in phase 1.
    return {'enabled': False, 'reason': '第一阶段仅预留云端转写接口；不会上传音视频或生成替代字幕'}
