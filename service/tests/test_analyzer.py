import aifc
import json
import math
import random
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

import analyzer as analyzer_module
from analyzer import AudioAnalyzer
from models import AnalysisResult, AudioTooLong, DecodedAudioTooLarge, UndecodableAudio

SAMPLE_RATE = 44100
CONTRACT_DIR = Path(__file__).parent / "contract"


def pulsed_tone(seconds: float) -> np.ndarray:
    # An A4 tone with a sharp pulse every 0.5 s: 120 BPM, key root A.
    times = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    envelope = 0.25 + 0.75 * np.exp(-40 * (times % 0.5))
    return 0.3 * np.sin(2 * np.pi * 440 * times) * envelope


def write_wav(path: Path, samples: np.ndarray, channels: int = 1, sample_width: int = 2) -> Path:
    frames = np.repeat(samples, channels)
    if sample_width == 3:
        ints = (frames * (2**23 - 1)).astype("<i4")
        data = b"".join(value.to_bytes(4, "little", signed=True)[:3] for value in ints.tolist())
    else:
        data = (frames * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(data)
    return path


def write_aiff(path: Path, samples: np.ndarray) -> Path:
    with aifc.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes((samples * 32767).astype(">i2").tobytes())
    return path


def write_mp3(path: Path, samples: np.ndarray) -> Path:
    source = write_wav(path.with_suffix(".wav"), samples)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source), "-b:a", "192k", str(path)],
        check=True,
    )
    return path


def analyze(path: Path) -> AnalysisResult:
    return AnalysisResult.model_validate(AudioAnalyzer().analyze_file(str(path)))


def assert_finite_and_bounded(result: AnalysisResult) -> None:
    assert all(math.isfinite(value) for value in (
        result.duration, result.bpm, result.bpm_confidence, result.key_confidence
    ))
    assert result.key_standard and result.key_camelot
    assert 0 < len(result.peaks) <= 2048
    assert 0 < len(result.preview) <= 96
    assert all(0 <= value <= 1 for value in result.peaks + result.preview)


def test_real_waveform_analysis_finds_tempo_and_key(tmp_path):
    result = analyze(write_wav(tmp_path / "pulsed-tone.wav", pulsed_tone(8)))

    assert_finite_and_bounded(result)
    assert abs(result.duration - 8.0) <= 0.05
    assert abs(result.bpm - 120) <= 2
    assert result.key_standard.split(" ")[0] == "A"


def test_leading_silence_keeps_the_full_duration(tmp_path):
    samples = np.concatenate([np.zeros(SAMPLE_RATE * 2), pulsed_tone(6)])
    result = analyze(write_wav(tmp_path / "late-start.wav", samples))

    assert abs(result.duration - 8.0) <= 0.05


@pytest.mark.parametrize(
    "name, write",
    [
        ("stereo-24.wav", lambda path, samples: write_wav(path, samples, channels=2, sample_width=3)),
        ("mono-16.aif", write_aiff),
        ("mono.mp3", write_mp3),
    ],
)
def test_supported_formats(tmp_path, name, write):
    result = analyze(write(tmp_path / name, pulsed_tone(8)))

    assert_finite_and_bounded(result)
    assert abs(result.duration - 8.0) <= 0.1


def test_corrupt_bytes_raise_undecodable_audio(tmp_path):
    source = tmp_path / "corrupt.wav"
    source.write_bytes(random.Random(8).randbytes(64))

    with pytest.raises(UndecodableAudio):
        AudioAnalyzer().analyze_file(str(source))


@pytest.mark.parametrize(
    "name, samples",
    [
        ("short.wav", pulsed_tone(0.2)),
        ("silent.wav", np.zeros(SAMPLE_RATE * 2)),
    ],
)
def test_edge_inputs_return_a_finite_result(tmp_path, name, samples):
    # Observed on Essentia 2.1b6.dev1389: both give a finite result, not an error.
    result = analyze(write_wav(tmp_path / name, samples))

    assert_finite_and_bounded(result)


def test_result_keys_match_the_contract_fixture(tmp_path):
    fixture = json.loads((CONTRACT_DIR / "analyze-r2.json").read_text())
    result = AudioAnalyzer().analyze_file(str(write_wav(tmp_path / "tone.wav", pulsed_tone(2))))

    assert set(result) | {"playback"} == set(fixture)


def test_decoded_duration_limit_stops_before_waveform_or_dsp(monkeypatch):
    decoded = np.zeros(SAMPLE_RATE + 1, dtype=np.float32)
    monkeypatch.setattr(analyzer_module.es, "MonoLoader", lambda **_kwargs: lambda: decoded)

    def unexpected(*_args, **_kwargs):
        pytest.fail("An overlong decoded signal must not enter waveform or DSP work")

    monkeypatch.setattr(analyzer_module, "waveform_peaks", unexpected)
    monkeypatch.setattr(analyzer_module.es, "RhythmExtractor2013", unexpected)
    monkeypatch.setattr(analyzer_module.es, "KeyExtractor", unexpected)
    with pytest.raises(AudioTooLong):
        AudioAnalyzer().analyze_file("metadata-can-understate-duration.wav", 1.0)


def test_decoded_duration_accepts_exact_limit(monkeypatch):
    decoded = np.zeros(SAMPLE_RATE, dtype=np.float32)
    monkeypatch.setattr(analyzer_module.es, "MonoLoader", lambda **_kwargs: lambda: decoded)
    monkeypatch.setattr(analyzer_module, "waveform_peaks", lambda _audio: [0.0])
    monkeypatch.setattr(analyzer_module.es, "RhythmExtractor2013", lambda **_kwargs: lambda _audio: (120, [], 0.9, [], []))
    monkeypatch.setattr(analyzer_module.es, "KeyExtractor", lambda: lambda _audio: ("C", "major", 0.8))
    assert AudioAnalyzer().analyze_file("exact-limit.wav", 1.0)["duration"] == 1.0


@pytest.mark.parametrize("sample", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_decoded_samples_stop_before_waveform_or_dsp(monkeypatch, sample):
    decoded = np.zeros(65537, dtype=np.float32)
    decoded[-1] = sample
    monkeypatch.setattr(analyzer_module.es, "MonoLoader", lambda **_kwargs: lambda: decoded)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Nonfinite samples must not enter waveform or DSP work")

    monkeypatch.setattr(analyzer_module, "waveform_peaks", unexpected)
    monkeypatch.setattr(analyzer_module.es, "RhythmExtractor2013", unexpected)
    monkeypatch.setattr(analyzer_module.es, "KeyExtractor", unexpected)
    with pytest.raises(UndecodableAudio):
        AudioAnalyzer().analyze_file("nonfinite.wav")


@pytest.mark.parametrize("excess_samples", [-1, 0, 1])
def test_decoded_memory_limit_boundary_before_sample_scan(monkeypatch, excess_samples):
    class SampleScanReached(Exception):
        pass

    class LengthOnlyAudio:
        def __len__(self):
            return 256 * 1024 * 1024 // 4 + excess_samples

        def __getitem__(self, _slice):
            raise SampleScanReached

    # No large PCM allocation: the loader's returned length alone selects the
    # admission branch. Only an accepted length can reach the first sample scan.
    monkeypatch.setattr(analyzer_module.es, "MonoLoader", lambda **_kwargs: LengthOnlyAudio)
    expected = DecodedAudioTooLarge if excess_samples > 0 else SampleScanReached
    with pytest.raises(expected):
        AudioAnalyzer().analyze_file("oversized-decoded-signal.wav", 1e300)


@pytest.mark.parametrize(("limit", "expected"), [
    (300, AudioTooLong),
    ((256 * 1024 * 1024 // 4 + 0.5) / SAMPLE_RATE, AudioTooLong),
    ((256 * 1024 * 1024 // 4 + 1) / SAMPLE_RATE, DecodedAudioTooLarge),
    ((256 * 1024 * 1024 // 4 + 2) / SAMPLE_RATE, DecodedAudioTooLarge),
])
def test_first_crossed_decode_limit_determines_the_error(monkeypatch, limit, expected):
    class LengthOnlyAudio:
        def __len__(self):
            return 256 * 1024 * 1024 // 4 + 10

    monkeypatch.setattr(analyzer_module.es, "MonoLoader", lambda **_kwargs: LengthOnlyAudio)
    with pytest.raises(expected):
        AudioAnalyzer().analyze_file("overlong-and-large.wav", limit)
