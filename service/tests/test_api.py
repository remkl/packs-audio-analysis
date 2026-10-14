import asyncio
import aifc
import hashlib
import io
import json
import os
import random
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AUDIO_ANALYSIS_BASIC_AUTH_USERNAME", "analysis")
os.environ.setdefault("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", "dev-analysis-secret")
os.environ.setdefault("AUDIO_ANALYSIS_ALLOWED_HOSTS", "127.0.0.1")
os.environ.setdefault("AUDIO_ANALYSIS_ALLOW_HTTP", "true")
os.environ.setdefault("AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES", "32")

from fastapi.testclient import TestClient

import main
from models import AnalysisResult, UndecodableAudio


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
    "peaks": [0.5] * 64,
    "preview": [0.5] * 64,
}
CONTRACT_DIR = Path(__file__).parent / "contract"
LOG_KEYS = {
    "service",
    "route",
    "method",
    "status",
    "durationMs",
    "stage",
    "errorClass",
    "requestId",
}


def fake_analyzer_with(side_effect=None, return_value=None):
    fake_analyzer = MagicMock()
    if side_effect is not None:
        fake_analyzer.analyze_file.side_effect = side_effect
    else:
        fake_analyzer.analyze_file.return_value = return_value
    return fake_analyzer


def silent_wav(seconds: float, sample_rate: int = 8_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\0\0" * int(seconds * sample_rate))
    return buffer.getvalue()


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr(main, "TEMP_DIR", temp_dir)
    return temp_dir


@pytest.fixture
def known_duration(monkeypatch):
    # The fake bytes are not audio, so the real ffprobe would reject them.
    async def probe(_file_path):
        return 1.0

    monkeypatch.setattr(main, "probe_duration", probe)


@pytest.fixture
def source_server():
    state = {
        "body": FAKE_AUDIO_CONTENT,
        "status": 200,
        "content_length": None,
        "redirect": False,
        "chunked": False,
        "gets": 0,
        "uploaded": None,
        "upload_content_type": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["gets"] += 1
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/other.wav")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(state["status"])
            body = state["body"]
            if not state["chunked"]:
                content_length = state["content_length"]
                if content_length is None:
                    content_length = len(body)
                self.send_header("Content-Length", str(content_length))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            state["uploaded"] = self.rfile.read(content_length)
            state["upload_content_type"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/track.wav"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def analyze(source_url, target_url=None, headers=None):
    body = {"sourceUrl": source_url}
    if target_url is not None:
        body["targetUrl"] = target_url
    return client.post("/api/analyze-r2", auth=AUTH, json=body, headers=headers)


def test_healthz_does_not_require_auth(monkeypatch):
    monkeypatch.delenv("VERCEL_GIT_COMMIT_SHA", raising=False)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "release": "local"}


def test_healthz_reports_the_deployment_commit(monkeypatch):
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abc123")
    assert client.get("/healthz").json() == {"status": "ok", "release": "abc123"}


@pytest.mark.parametrize("path", ["/api/analyze-upload", "/api/upload", "/api/analyze"])
def test_legacy_upload_routes_are_removed(path):
    # No route parses a multipart body, so an unauthenticated upload never reaches a parser.
    response = client.post(path, files={"file": ("loop.wav", b"x", "audio/wav")})
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_are_removed(path):
    assert client.get(path).status_code == 404


def test_exact_pax_r2_host_is_allowed():
    main.validate_media_url(
        "https://"
        "0f254469eceb1b88f5b5ff50405617cd.r2.cloudflarestorage.com"
        "/v2/verification/tone.wav",
        "Source",
    )


def test_analyze_r2_requires_auth(source_server):
    _, source_url = source_server
    response = client.post("/api/analyze-r2", json={"sourceUrl": source_url})
    assert response.status_code == 401


def test_analyze_r2_rejects_unapproved_host():
    response = analyze("https://example.com/track.wav")
    assert response.status_code == 400
    assert response.json()["detail"] == "Source host is not allowed"
    assert response.json()["stage"] == "validate"


def test_analyze_r2_rejects_unapproved_target_before_download(source_server):
    state, source_url = source_server
    response = analyze(source_url, "https://example.com/x.mp3")
    assert response.status_code == 400
    assert response.json()["detail"] == "Target host is not allowed"
    assert response.json()["stage"] == "validate"
    assert state["gets"] == 0


def test_invalid_body_does_not_echo_the_input():
    response = client.post(
        "/api/analyze-r2", auth=AUTH, json={"sourceUrl": "not a url?secret=signature"}
    )
    assert response.status_code == 422
    assert response.json()["stage"] == "validate"
    assert "signature" not in response.text


def test_analyze_r2_returns_analysis_and_deletes_temp_file(
    temp_dir, source_server, monkeypatch, known_duration
):
    _, source_url = source_server
    seen_path = {}

    def fake_analyze(file_path, max_duration_seconds):
        assert max_duration_seconds == main.MAX_DURATION_SECONDS
        path = Path(file_path)
        assert path.exists()
        assert path.read_bytes() == FAKE_AUDIO_CONTENT
        seen_path["path"] = path
        return FAKE_RESULT

    monkeypatch.setattr(main, "get_analyzer", lambda: fake_analyzer_with(fake_analyze))
    response = analyze(source_url)

    assert response.status_code == 200
    assert response.json()["bpm"] == 120.0
    assert response.json()["key_camelot"] == "8B"
    assert response.json()["peaks"] == [0.5] * 64
    assert response.json()["preview"] == [0.5] * 64
    assert not seen_path["path"].exists()
    assert list(temp_dir.iterdir()) == []


def test_analyze_r2_can_create_playback_from_the_downloaded_source(
    temp_dir, source_server, monkeypatch, known_duration
):
    _, source_url = source_server
    seen = {}

    async def fake_playback(file_path, target_url, *_args):
        path = Path(file_path)
        assert path.exists()
        assert path.read_bytes() == FAKE_AUDIO_CONTENT
        seen["path"] = path
        seen["target"] = target_url
        return {"sizeBytes": 1234, "sha256": "a" * 64}

    monkeypatch.setattr(main, "get_analyzer", lambda: fake_analyzer_with(return_value=FAKE_RESULT))
    monkeypatch.setattr(main, "create_playback_derivative", fake_playback)
    response = analyze(source_url, source_url + ".mp3")

    assert response.status_code == 200
    assert response.json()["playback"] == {"sizeBytes": 1234, "sha256": "a" * 64}
    assert seen["target"] == source_url + ".mp3"
    assert not seen["path"].exists()
    assert list(temp_dir.iterdir()) == []


def test_playback_derivative_transcodes_aiff_and_uploads_mp3(
    temp_dir, source_server
):
    state, target_url = source_server
    source_path = temp_dir / "source.aif"
    with aifc.open(str(source_path), "wb") as source:
        source.setnchannels(2)
        source.setsampwidth(2)
        source.setframerate(44_100)
        source.writeframes(b"\0\0\0\0" * 4_410)

    result = asyncio.run(
        main.create_playback_derivative(source_path, target_url + ".mp3")
    )

    assert result["sizeBytes"] == len(state["uploaded"])
    assert result["sha256"] == hashlib.sha256(state["uploaded"]).hexdigest()
    assert state["upload_content_type"] == "audio/mpeg"
    assert state["uploaded"][:3] == b"ID3"
    assert [path.name for path in temp_dir.iterdir()] == ["source.aif"]


def test_job_slots_bound_concurrency_and_reject_a_full_queue():
    async def scenario():
        slots = main.JobSlots(2, 1)
        first = await slots.acquire(1)
        second = await slots.acquire(1)
        third = asyncio.ensure_future(slots.acquire(1))
        await asyncio.sleep(0)

        with pytest.raises(main.ServiceError) as full:
            await slots.acquire(1)
        assert full.value.status_code == 503
        assert full.value.retry_after == 15
        assert slots.active == 2
        assert not third.done()

        first.release()
        first.release()  # A second release is a no-op.
        lease = await third
        assert slots.active == 2

        with pytest.raises(main.ServiceError) as waited:
            await slots.acquire(0.01)
        assert waited.value.status_code == 503

        second.release()
        lease.release()
        assert slots.active == 0

    asyncio.run(scenario())


def test_analyze_r2_returns_503_with_retry_after_when_busy(source_server, monkeypatch):
    state, source_url = source_server
    full = main.JobSlots(1, 0)
    asyncio.run(full.acquire(1))
    monkeypatch.setattr(main, "job_slots", full)

    response = analyze(source_url)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "15"
    assert response.json()["stage"] == "queue"
    assert state["gets"] == 0


def test_analyze_r2_rejects_declared_oversize_download(source_server):
    state, source_url = source_server
    state["content_length"] = 64
    response = analyze(source_url)
    assert response.status_code == 413
    assert response.json()["detail"] == "Audio file exceeds size limit"
    assert response.json()["stage"] == "download"


def test_analyze_r2_rejects_streamed_oversize_download(temp_dir, source_server):
    state, source_url = source_server
    state["chunked"] = True
    state["body"] = b"x" * 64
    response = analyze(source_url)
    assert response.status_code == 413
    assert response.json()["stage"] == "download"
    assert list(temp_dir.iterdir()) == []


def test_analyze_r2_does_not_follow_redirects(temp_dir, source_server):
    state, source_url = source_server
    state["redirect"] = True
    response = analyze(source_url)
    assert response.status_code == 502
    assert response.json()["stage"] == "download"
    assert state["gets"] == 1


def test_analyze_r2_maps_source_failure(source_server):
    state, source_url = source_server
    state["status"] = 404
    state["body"] = b"missing"
    response = analyze(source_url)
    assert response.status_code == 502
    assert response.json()["detail"] == "Could not download audio source"
    assert response.json()["stage"] == "download"


def test_analyze_r2_returns_422_for_undecodable_audio(temp_dir, source_server, monkeypatch):
    state, source_url = source_server
    state["body"] = random.Random(8).randbytes(24)
    monkeypatch.setattr(main, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    fake_analyzer = fake_analyzer_with(return_value=FAKE_RESULT)
    monkeypatch.setattr(main, "get_analyzer", lambda: fake_analyzer)

    response = analyze(source_url)

    assert response.status_code == 422
    assert response.json()["stage"] == "probe"
    fake_analyzer.analyze_file.assert_not_called()
    assert list(temp_dir.iterdir()) == []


def test_analyze_r2_rejects_audio_longer_than_the_limit(temp_dir, source_server, monkeypatch):
    state, source_url = source_server
    state["body"] = silent_wav(2)
    monkeypatch.setattr(main, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    monkeypatch.setattr(main, "MAX_DURATION_SECONDS", 1)
    fake_analyzer = fake_analyzer_with(return_value=FAKE_RESULT)
    monkeypatch.setattr(main, "get_analyzer", lambda: fake_analyzer)

    response = analyze(source_url)

    assert response.status_code == 413
    assert response.json()["stage"] == "probe"
    assert response.json()["detail"] == "Audio is longer than 1 seconds"
    fake_analyzer.analyze_file.assert_not_called()
    assert list(temp_dir.iterdir()) == []


def test_analyze_r2_maps_a_decoder_failure_to_422(
    temp_dir, source_server, monkeypatch, known_duration
):
    _, source_url = source_server
    monkeypatch.setattr(
        main,
        "get_analyzer",
        lambda: fake_analyzer_with(UndecodableAudio("Audio could not be decoded")),
    )
    response = analyze(source_url)
    assert response.status_code == 422
    assert response.json()["stage"] == "decode"
    assert list(temp_dir.iterdir()) == []


def test_analyze_r2_returns_504_and_kills_ffmpeg_at_the_deadline(
    temp_dir, source_server, monkeypatch, known_duration
):
    _, source_url = source_server
    started = []
    original_exec = asyncio.create_subprocess_exec

    async def slow_encode(*args, **kwargs):
        command = ("sleep", "30") if args[0] == "ffmpeg" else args
        process = await original_exec(*command, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", slow_encode)
    monkeypatch.setattr(main, "DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(main, "get_analyzer", lambda: fake_analyzer_with(return_value=FAKE_RESULT))

    response = analyze(source_url, source_url + ".mp3")

    assert response.status_code == 504
    assert response.json()["stage"] == "encode"
    assert len(started) == 1
    assert started[0].returncode is not None
    assert list(temp_dir.iterdir()) == []


def test_request_log_line_shape(temp_dir, source_server, monkeypatch, known_duration, capsys):
    _, source_url = source_server
    monkeypatch.setattr(
        main,
        "get_analyzer",
        lambda: fake_analyzer_with(UndecodableAudio("Audio could not be decoded")),
    )
    capsys.readouterr()

    response = analyze(source_url, headers={"x-pax-request-id": "w8-test-0001"})

    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert set(line) == LOG_KEYS
    assert line["service"] == "audio-analysis"
    assert line["route"] == "/api/analyze-r2"
    assert line["status"] == 422
    assert line["stage"] == "decode"
    assert line["errorClass"] == "UndecodableAudio"
    assert line["requestId"] == "w8-test-0001"
    assert "127.0.0.1" not in output
    assert response.headers["x-pax-request-id"] == "w8-test-0001"
    assert response.json()["requestId"] == "w8-test-0001"


def test_invalid_request_id_is_replaced():
    response = client.get("/healthz", headers={"x-pax-request-id": "bad id"})
    assert main.REQUEST_ID_PATTERN.match(response.headers["x-pax-request-id"])
    assert response.headers["x-pax-request-id"] != "bad id"


def test_analyze_r2_deletes_temp_file_after_analysis_failure(
    temp_dir, source_server, monkeypatch, known_duration
):
    _, source_url = source_server
    monkeypatch.setattr(
        main,
        "get_analyzer",
        lambda: fake_analyzer_with(RuntimeError("/tmp/private-audio.wav: decoder boom")),
    )
    response = analyze(source_url)
    assert response.status_code == 500
    assert response.json()["detail"] == "Audio analysis failed"
    assert "/tmp/private-audio.wav" not in response.text
    assert "decoder boom" not in response.text
    assert list(temp_dir.iterdir()) == []


def test_contract_fixture_matches_the_route(temp_dir, source_server, monkeypatch, known_duration):
    _, source_url = source_server
    fixture = json.loads((CONTRACT_DIR / "analyze-r2.json").read_text())
    analysis = {key: value for key, value in fixture.items() if key != "playback"}

    async def fake_playback(*_args):
        return fixture["playback"]

    monkeypatch.setattr(main, "get_analyzer", lambda: fake_analyzer_with(return_value=analysis))
    monkeypatch.setattr(main, "create_playback_derivative", fake_playback)

    response = analyze(source_url, source_url + ".mp3")

    assert response.status_code == 200
    assert set(response.json()) == set(fixture)
    assert response.json() == fixture
    AnalysisResult.model_validate(fixture)


def test_error_fixture_matches_a_real_error(source_server, monkeypatch, known_duration):
    _, source_url = source_server
    fixture = json.loads((CONTRACT_DIR / "analyze-r2.error.json").read_text())
    monkeypatch.setattr(
        main,
        "get_analyzer",
        lambda: fake_analyzer_with(UndecodableAudio("Audio could not be decoded")),
    )

    response = analyze(source_url)

    assert response.status_code == 422
    assert set(response.json()) == set(fixture)
    assert response.json()["detail"] == fixture["detail"]


@pytest.mark.parametrize(
    ("content", "content_type", "with_auth", "status", "detail", "stage"),
    [
        (b"{", "application/json", False, 422, "Invalid request body", "validate"),
        (b"{}", "application/json", False, 401, "Invalid audio analysis credentials", "request"),
        (b"{", "text/plain", False, 401, "Invalid audio analysis credentials", "request"),
        (b"{}", "text/plain", True, 422, "Invalid request body", "validate"),
        (b"null", "application/json", True, 422, "Invalid request body", "validate"),
        (b"[]", "application/json", True, 422, "Invalid request body", "validate"),
    ],
)
def test_body_parse_and_auth_precedence(content, content_type, with_auth, status, detail, stage):
    response = client.post(
        "/api/analyze-r2",
        content=content,
        headers={"Content-Type": content_type, "x-pax-request-id": "parity-body-0001"},
        auth=AUTH if with_auth else None,
    )
    assert response.status_code == status
    assert response.json() == {
        "detail": detail, "stage": stage, "requestId": "parity-body-0001"
    }


def nested_json_body(depth, mixed=False, leaf="0"):
    # Count the root request object as one container.
    openings = ["[" if not mixed or level % 2 else '{"child":' for level in range(depth - 1)]
    closings = ["]" if opening == "[" else "}" for opening in reversed(openings)]
    return ('{"sourceUrl":"https://denied.invalid/track.wav","ignored":'
            + "".join(openings) + leaf + "".join(closings) + "}")


@pytest.mark.parametrize("depth", [63, 64, 65, 991, 1011, 9983, 9984])
@pytest.mark.parametrize("mixed", [False, True])
def test_json_depth_limit_counts_the_root_and_ignored_containers(depth, mixed):
    response = client.post(
        "/api/analyze-r2", auth=AUTH,
        headers={"Content-Type": "application/json", "x-pax-request-id": "json-depth-0001"},
        content=nested_json_body(depth, mixed).encode(),
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Source host is not allowed" if depth <= 64 else "There was an error parsing the body",
        "stage": "validate" if depth <= 64 else "request",
        "requestId": "json-depth-0001",
    }


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"])
@pytest.mark.parametrize("depth", [64, 65])
def test_json_depth_limit_respects_json_encoding_and_escaped_strings(encoding, depth):
    leaf = json.dumps('[]{}\\"' * 128)
    response = client.post(
        "/api/analyze-r2", auth=AUTH,
        headers={"Content-Type": "application/problem+json; charset=ignored"},
        content=nested_json_body(depth, mixed=True, leaf=leaf).encode(encoding),
    )
    assert response.status_code == 400
    assert response.json()["stage"] == ("validate" if depth == 64 else "request")


@pytest.mark.parametrize("authorization", [None, "Basic eDp5", "Basic !!!"])
@pytest.mark.parametrize("content_type", ["application/json", "application/problem+json", "text/plain", None])
def test_json_depth_limit_keeps_media_type_and_auth_precedence(authorization, content_type, capsys):
    headers = {"x-pax-request-id": "json-depth-0002"}
    if authorization is not None:
        headers["Authorization"] = authorization
    if content_type is not None:
        headers["Content-Type"] = content_type
    capsys.readouterr()
    response = client.post("/api/analyze-r2", content=nested_json_body(65).encode(), headers=headers)
    is_json = content_type in {"application/json", "application/problem+json"}
    assert response.status_code == (400 if is_json else 401)
    assert response.json()["stage"] == "request"
    if is_json:
        assert response.json()["detail"] == "There was an error parsing the body"
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 1
    assert lines[0]["errorClass"] == "HTTPException"


@pytest.mark.parametrize(
    ("body", "status", "stage"),
    [
        ('{"broken":?,"ignored":' + "[" * 64 + "0" + "]" * 64 + "}", 400, "request"),
        ('{"ignored":"' + "[" * 128, 422, "validate"),
        ('{"ignored":"\\\\", "nested":' + "[" * 64 + "0" + "]" * 64 + "}", 400, "request"),
    ],
)
def test_json_depth_preflight_handles_malformed_syntax_without_recursive_parsing(body, status, stage):
    response = client.post(
        "/api/analyze-r2", auth=AUTH, content=body.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == status
    assert response.json()["stage"] == stage


@pytest.mark.parametrize("size", [65536, 65537])
@pytest.mark.parametrize("with_auth", [True, False])
@pytest.mark.parametrize("content_type", ["application/json", "text/plain"])
def test_request_body_limit_precedes_parsing_and_auth(size, with_auth, content_type):
    base = '{"sourceUrl":"https://denied.invalid/track.wav","ignored":""}'
    body = base.replace('"ignored":""', '"ignored":"' + "x" * (size - len(base)) + '"')
    response = client.post(
        "/api/analyze-r2", auth=AUTH if with_auth else None,
        content=body.encode(), headers={"Content-Type": content_type},
    )
    expected_status = 413 if size > 65536 else (401 if not with_auth else 400 if content_type == "application/json" else 422)
    assert response.status_code == expected_status
    if size > 65536:
        assert response.json()["detail"] == "Request body is too large"
        assert response.json()["stage"] == "request"


@pytest.mark.parametrize("content_length", [None, b"1", b"65537"])
def test_request_body_stream_stops_at_limit_without_trusting_content_length(content_length):
    received = 0

    async def receive():
        nonlocal received
        received += 1
        assert received <= 2, "Must stop reading once the body exceeds the limit"
        return {"type": "http.request", "body": b"x" * (65536 if received == 1 else 1), "more_body": True}

    headers = [] if content_length is None else [(b"content-length", content_length)]
    request = main.BoundedAnalysisRequest({"type": "http", "headers": headers}, receive)
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(request.body())
    assert error.value.status_code == 413
    assert error.value.detail == "Request body is too large"
    assert received == 2
    assert not hasattr(request, "_body")


def test_large_malformed_request_is_rejected_before_json_parsing():
    response = client.post(
        "/api/analyze-r2", content=b"{" * 65537,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"


@pytest.mark.parametrize(
    ("authorization", "detail"),
    [
        ("Basic !!!", "Not authenticated"),
        ("Basic bm9jb2xvbg==", "Not authenticated"),
        ("Bearer invalid", "Invalid audio analysis credentials"),
    ],
)
def test_malformed_basic_auth_uses_the_security_error(authorization, detail):
    response = client.post(
        "/api/analyze-r2", json={}, headers={"Authorization": authorization}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == detail
    assert response.json()["stage"] == "request"
    assert response.headers["www-authenticate"] == "Basic"


@pytest.mark.parametrize("alias_value", [None, 4, "invalid-url"])
def test_source_alias_has_priority_over_a_valid_field_name(source_server, alias_value):
    state, source_url = source_server
    response = client.post(
        "/api/analyze-r2", auth=AUTH,
        json={"sourceUrl": alias_value, "source_url": source_url},
    )
    assert response.status_code == 422
    assert response.json()["stage"] == "validate"
    assert state["gets"] == 0


def test_field_names_and_extra_fields_are_accepted(source_server):
    state, source_url = source_server
    state["status"] = 404
    response = client.post(
        "/api/analyze-r2", auth=AUTH,
        json={"source_url": source_url, "target_url": None, "ignored": {"value": 1}},
    )
    assert response.status_code == 502
    assert response.json()["stage"] == "download"
    assert state["gets"] == 1


@pytest.mark.parametrize(
    ("content_type", "expected_status", "expected_stage", "expected_gets"),
    [(None, 422, "validate", 0), ("application/json", 502, "download", 1),
     ("application/problem+json", 502, "download", 1)],
)
def test_json_media_types_reach_validation(
    source_server, content_type, expected_status, expected_stage, expected_gets
):
    state, source_url = source_server
    state["status"] = 404
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = client.post(
        "/api/analyze-r2", auth=AUTH, headers=headers,
        content=json.dumps({"sourceUrl": source_url}).encode(),
    )
    assert response.status_code == expected_status
    assert response.json()["stage"] == expected_stage
    assert state["gets"] == expected_gets


@pytest.mark.parametrize(
    ("path", "method", "status", "allow", "location"),
    [
        ("/healthz/", "GET", 307, None, "http://testserver/healthz"),
        ("/api/analyze-r2/?x=1", "POST", 307, None, "http://testserver/api/analyze-r2?x=1"),
        ("/healthz", "POST", 405, "GET", None),
        ("/api/analyze-r2", "GET", 405, "POST", None),
        ("/missing", "GET", 404, None, None),
    ],
)
def test_router_responses_have_request_ids_and_safe_logs(path, method, status, allow, location, capsys):
    capsys.readouterr()
    response = client.request(
        method, path, follow_redirects=False,
        headers={"x-pax-request-id": "parity-router-0001"},
    )
    assert response.status_code == status
    assert response.headers.get("allow") == allow
    assert response.headers.get("location") == location
    assert response.headers["x-pax-request-id"] == "parity-router-0001"
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 1
    assert set(lines[0]) == LOG_KEYS
    assert lines[0]["requestId"] == "parity-router-0001"
    assert lines[0]["status"] == status
    assert lines[0]["route"] == (path if path in main.LOGGED_ROUTES else "other")
    assert lines[0]["stage"] == "request"
    assert lines[0]["errorClass"] == (None if status == 307 else "HTTPException")
