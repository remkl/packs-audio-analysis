"""Analysis output parity across runtime changes (Python, Essentia, NumPy).

The fixture set is fixed: synthesized WAV and AIFF files and one committed MP3,
so a Debian ffmpeg update cannot change the input. BPM, key, duration, peaks
and preview must equal the recorded baseline exactly; the two confidences may
differ by 1e-6. Set PAX_WRITE_OUTPUT_BASELINE=1 to record a new baseline, and
only in the runtime that the baseline describes.
"""

import json
import math
import os
from pathlib import Path

import numpy as np

from analyzer import AudioAnalyzer
from tests.test_analyzer import SAMPLE_RATE, pulsed_tone, write_aiff, write_wav

FIXTURES = Path(__file__).parent / "fixtures"
BASELINE = FIXTURES / "output-baseline.json"
EXACT_FIELDS = ("bpm", "key_standard", "key_camelot", "duration", "peaks", "preview")
CONFIDENCE_FIELDS = ("bpm_confidence", "key_confidence")
CONFIDENCE_TOLERANCE = 1e-6


def pulsed_chord(seconds: float, bpm: float, frequencies: tuple) -> np.ndarray:
    # A sustained chord with a sharp pulse on each beat.
    times = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    envelope = 0.25 + 0.75 * np.exp(-40 * (times % (60.0 / bpm)))
    chord = sum(np.sin(2 * np.pi * frequency * times) for frequency in frequencies)
    return 0.3 * chord / len(frequencies) * envelope


def fixture_files(directory: Path) -> dict:
    c_major = (261.63, 329.63, 392.00)
    a_minor = (220.00, 261.63, 329.63)
    return {
        "pulse-120-a4.wav": write_wav(directory / "pulse-120-a4.wav", pulsed_tone(7)),
        "triad-90-c-major.wav": write_wav(
            directory / "triad-90-c-major.wav", pulsed_chord(8, 90, c_major)
        ),
        "triad-140-a-minor-24.wav": write_wav(
            directory / "triad-140-a-minor-24.wav",
            pulsed_chord(6, 140, a_minor),
            sample_width=3,
        ),
        "pulse-120-a4.aif": write_aiff(directory / "pulse-120-a4.aif", pulsed_tone(7)),
        "pulse-120.mp3": FIXTURES / "pulse-120.mp3",
    }


def analyze_fixtures(directory: Path) -> dict:
    analyzer = AudioAnalyzer()
    return {
        name: analyzer.analyze_file(str(path))
        for name, path in fixture_files(directory).items()
    }


def test_analysis_output_matches_the_recorded_baseline(tmp_path):
    results = analyze_fixtures(tmp_path)
    if os.environ.get("PAX_WRITE_OUTPUT_BASELINE") == "1":
        # default=float stores a NumPy scalar as the exact Python float.
        BASELINE.write_text(json.dumps(results, indent=2, sort_keys=True, default=float) + "\n")
        return

    assert BASELINE.exists(), "record the baseline with PAX_WRITE_OUTPUT_BASELINE=1"
    baseline = json.loads(BASELINE.read_text())
    assert sorted(results) == sorted(baseline)
    for name, result in results.items():
        expected = baseline[name]
        for field in EXACT_FIELDS:
            assert result[field] == expected[field], f"{name}: {field} changed"
        for field in CONFIDENCE_FIELDS:
            assert math.isclose(
                result[field], expected[field], rel_tol=0, abs_tol=CONFIDENCE_TOLERANCE
            ), f"{name}: {field} {result[field]} != {expected[field]}"
