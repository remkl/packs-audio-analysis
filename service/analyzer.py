import essentia.standard as es
import numpy as np

from models import AnalysisRejected, AudioTooLong, DecodedAudioTooLarge, UndecodableAudio


MAX_WAVEFORM_BUCKETS = 2048
WAVEFORM_PREVIEW_BUCKETS = 96
MAX_DECODED_AUDIO_BYTES = 256 * 1024 * 1024


def waveform_peaks(audio, bucket_count: int = MAX_WAVEFORM_BUCKETS) -> list[float]:
    samples = np.asarray(audio, dtype=np.float32)
    buckets = min(max(1, bucket_count), len(samples))
    if len(samples) == 0:
        return []
    magnitudes = [float(np.max(np.abs(bucket))) for bucket in np.array_split(samples, buckets)]
    maximum = max(magnitudes)
    if maximum <= 0:
        return [0.0] * len(magnitudes)
    return [round(min(1.0, magnitude / maximum), 4) for magnitude in magnitudes]


def waveform_preview(
    peaks: list[float], bucket_count: int = WAVEFORM_PREVIEW_BUCKETS
) -> list[float]:
    if len(peaks) <= bucket_count:
        return list(peaks)
    bucket_size = max(1, (len(peaks) + bucket_count - 1) // bucket_count)
    return [max(peaks[index : index + bucket_size]) for index in range(0, len(peaks), bucket_size)]


class AudioAnalyzer:
    def __init__(self):
        self.camelot_map = {
            "B Major": "1B",
            "F# Major": "2B",
            "Gb Major": "2B",
            "Db Major": "3B",
            "C# Major": "3B",
            "Ab Major": "4B",
            "G# Major": "4B",
            "Eb Major": "5B",
            "D# Major": "5B",
            "Bb Major": "6B",
            "A# Major": "6B",
            "F Major": "7B",
            "C Major": "8B",
            "G Major": "9B",
            "D Major": "10B",
            "A Major": "11B",
            "E Major": "12B",
            "Ab Minor": "1A",
            "G# Minor": "1A",
            "Eb Minor": "2A",
            "D# Minor": "2A",
            "Bb Minor": "3A",
            "A# Minor": "3A",
            "F Minor": "4A",
            "C Minor": "5A",
            "G Minor": "6A",
            "D Minor": "7A",
            "A Minor": "8A",
            "E Minor": "9A",
            "B Minor": "10A",
            "F# Minor": "11A",
            "Gb Minor": "11A",
            "Db Minor": "12A",
            "C# Minor": "12A",
        }

    def _get_camelot_key(self, key: str, scale: str) -> str:
        formatted_key = f"{key} {scale.capitalize()}"
        return self.camelot_map.get(formatted_key, "Unknown")

    def analyze_file(self, file_path: str, max_duration_seconds: float = float("inf")) -> dict:
        # A MemoryError is a system fault, not a property of the file, so it
        # passes through and the caller can retry it.
        try:
            audio = es.MonoLoader(filename=file_path, sampleRate=44100)()
        except MemoryError:
            raise
        except Exception as exc:
            raise UndecodableAudio("Audio could not be decoded") from exc
        if len(audio) == 0:
            raise UndecodableAudio("Audio could not be decoded")
        # Match Rust's first crossed limit even when the completed Python buffer
        # exceeds both. Python has already allocated the audio at this point.
        memory_exceeded = len(audio) * 4 > MAX_DECODED_AUDIO_BYTES
        memory_limit_first = memory_exceeded and (
            (MAX_DECODED_AUDIO_BYTES // 4 + 1) / 44100.0 <= max_duration_seconds
        )
        if len(audio) / 44100.0 > max_duration_seconds and not memory_limit_first:
            raise AudioTooLong("Audio exceeds the configured duration limit")
        # MonoLoader returns mono float32. Python checks after allocation; Rust
        # enforces the same cap while decoding to bound its PCM allocation.
        if memory_exceeded:
            raise DecodedAudioTooLarge("Decoded audio exceeds memory limit")
        # Bound the temporary boolean array while checking float audio samples.
        for offset in range(0, len(audio), 65536):
            if not np.isfinite(audio[offset:offset + 65536]).all():
                raise UndecodableAudio("Audio could not be decoded")

        # Peaks, duration, tempo and key all use the full decoded signal.
        peaks = waveform_peaks(audio)
        try:
            bpm, _, beats_confidence, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
            key, scale, key_strength = es.KeyExtractor()(audio)
        except MemoryError:
            raise
        except Exception as exc:
            raise AnalysisRejected("Audio could not be analyzed") from exc

        return {
            "bpm": round(bpm, 1),
            "bpm_confidence": float(beats_confidence),
            "key_standard": f"{key} {scale.capitalize()}",
            "key_camelot": self._get_camelot_key(key, scale),
            "key_confidence": float(key_strength),
            "duration": len(audio) / 44100.0,
            "peaks": peaks,
            "preview": waveform_preview(peaks),
        }
