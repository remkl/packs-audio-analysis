"""Startup bounds for the shared Python/Rust configuration contract."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def import_service(overrides):
    environment = dict(os.environ)
    environment.update({
        "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD": "config-test-secret",
        "AUDIO_ANALYSIS_ALLOWED_HOSTS": "127.0.0.1",
    })
    environment.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("value", ["nan", "+NaN", "-NaN", "inf", "+Infinity", "-inf", "1e309", "0", "-0", "-1"])
def test_duration_must_be_finite_and_positive(value):
    result = import_service({"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": value})
    assert result.returncode != 0
    assert result.stderr.splitlines()[-1] == "RuntimeError: AUDIO_ANALYSIS_MAX_DURATION_SECONDS must be above 0"


@pytest.mark.parametrize("value", ["0.000000001", "300", "1e300"])
def test_positive_finite_duration_remains_valid(value):
    result = import_service({"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": value})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("overrides", "error"), [
    ({"AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD": ""}, "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD must be set"),
    ({"AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS": "0"}, "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS must be above 0 and below the deadline"),
])
def test_nonfinite_duration_preserves_startup_error_order(overrides, error):
    result = import_service({"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "nan", **overrides})
    assert result.returncode != 0
    assert result.stderr.splitlines()[-1] == "RuntimeError: " + error
