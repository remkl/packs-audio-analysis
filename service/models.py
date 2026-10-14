from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class PlaybackDerivative(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    size_bytes: int = Field(alias="sizeBytes")
    sha256: str


class AnalysisResult(BaseModel):
    bpm: float
    bpm_confidence: float
    key_standard: str
    key_camelot: str
    key_confidence: float
    duration: float
    peaks: list[float]
    preview: list[float]
    playback: Optional[PlaybackDerivative] = None


class AnalyzeR2Request(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_url: HttpUrl = Field(alias="sourceUrl")
    target_url: Optional[HttpUrl] = Field(default=None, alias="targetUrl")


class UndecodableAudio(Exception):
    """The decoder cannot read the file as audio. A retry gives the same result."""


class AnalysisRejected(Exception):
    """Essentia cannot analyze a decoded signal. A retry gives the same result."""


class AudioTooLong(Exception):
    """The decoded sample count exceeds the configured duration limit."""


class DecodedAudioTooLarge(Exception):
    """The decoded mono float32 signal exceeds the shared memory limit."""
