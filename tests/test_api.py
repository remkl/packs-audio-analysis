import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AUDIO_ANALYSIS_BASIC_AUTH_USERNAME", "analysis")
os.environ.setdefault("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", "dev-analysis-secret")

sys.modules["essentia"] = MagicMock()
sys.modules["essentia.standard"] = MagicMock()

from fastapi.testclient import TestClient

import main


client = TestClient(main.app)
AUTH = ("analysis", "dev-analysis-secret")
FAKE_AUDIO_CONTENT = b"fake audio content"
FAKE_RESULT = {
    "bpm": 120.0,
    "bpm_confidence": 0.9,
    "key_standard": "C Major",
    "key_camelot": "8B",
    "key_confidence": 0.8,
    "duration": 180.0,
}


@pytest.fixture
def temp_service_dirs(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    temp_dir = tmp_path / "tmp"
    input_dir.mkdir()
    temp_dir.mkdir()

    monkeypatch.setattr(main, "INPUT_DIR", input_dir)
    monkeypatch.setattr(main, "TEMP_DIR", temp_dir)

    return input_dir, temp_dir


def test_healthz_does_not_require_auth():
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_analyze_upload_requires_auth():
    response = client.post(
        "/api/analyze-upload",
        files={"file": ("track.mp3", FAKE_AUDIO_CONTENT, "audio/mpeg")},
    )

    assert response.status_code == 401


def test_analyze_upload_rejects_wrong_auth():
    response = client.post(
        "/api/analyze-upload",
        auth=("analysis", "wrong"),
        files={"file": ("track.mp3", FAKE_AUDIO_CONTENT, "audio/mpeg")},
    )

    assert response.status_code == 401


def test_analyze_upload_returns_analysis_and_deletes_temp_file(temp_service_dirs, monkeypatch):
    seen_path = {}

    def fake_analyze(file_path):
        path = Path(file_path)
        assert path.exists()
        seen_path["path"] = path

        return FAKE_RESULT

    monkeypatch.setattr(main.analyzer, "analyze_file", fake_analyze)

    response = client.post(
        "/api/analyze-upload",
        auth=AUTH,
        files={"file": ("track.mp3", FAKE_AUDIO_CONTENT, "audio/mpeg")},
    )

    assert response.status_code == 200
    assert response.json()["bpm"] == 120.0
    assert response.json()["key_camelot"] == "8B"
    assert not seen_path["path"].exists()


def test_analyze_upload_deletes_temp_file_after_failure(temp_service_dirs, monkeypatch):
    seen_path = {}

    def fake_analyze(file_path):
        path = Path(file_path)
        assert path.exists()
        seen_path["path"] = path

        raise RuntimeError("analysis failed")

    monkeypatch.setattr(main.analyzer, "analyze_file", fake_analyze)

    response = client.post(
        "/api/analyze-upload",
        auth=AUTH,
        files={"file": ("track.mp3", FAKE_AUDIO_CONTENT, "audio/mpeg")},
    )

    assert response.status_code == 500
    assert not seen_path["path"].exists()


def test_compatibility_endpoints_require_auth():
    upload_response = client.post(
        "/api/upload",
        files={"file": ("track.mp3", FAKE_AUDIO_CONTENT, "audio/mpeg")},
    )
    analyze_response = client.post("/api/analyze", json={"filename": "track.mp3"})

    assert upload_response.status_code == 401
    assert analyze_response.status_code == 401


def test_compatibility_upload_then_analyze(temp_service_dirs, monkeypatch):
    input_dir, _ = temp_service_dirs
    monkeypatch.setattr(main.analyzer, "analyze_file", lambda _: FAKE_RESULT)

    upload_response = client.post(
        "/api/upload",
        auth=AUTH,
        files={"file": ("track.mp3", FAKE_AUDIO_CONTENT, "audio/mpeg")},
    )
    analyze_response = client.post("/api/analyze", auth=AUTH, json={"filename": "track.mp3"})

    assert upload_response.status_code == 200
    assert upload_response.json() == {"filename": "track.mp3"}
    assert (input_dir / "track.mp3").exists()
    assert analyze_response.status_code == 200
    assert analyze_response.json()["key_standard"] == "C Major"
