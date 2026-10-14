#!/usr/bin/env python3
"""Bounded HTTP contract comparison against the current Python image.

Only an isolated Docker network is used; no ports are published. The fixture
returns local errors or small synthetic non-audio bodies; no request can reach
R2 or another external host. Audio
value/encoding parity has a separate fixture gate. Python 3.9+ stdlib only.
"""
import argparse
import base64
import hashlib
import gzip
import struct
import zlib
import http.client
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
import uuid

LOG_KEYS = {"service", "route", "method", "status", "durationMs", "stage", "errorClass", "requestId"}
AUTH = "Basic " + base64.b64encode(b"analysis:contract-test-password").decode()
SOURCE = "http://fixture.local:9000/missing.wav"
BASE_ENV = {
    "AUDIO_ANALYSIS_BASIC_AUTH_USERNAME": "analysis",
    "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD": "contract-test-password",
    "AUDIO_ANALYSIS_ALLOWED_HOSTS": "fixture.local",
    "AUDIO_ANALYSIS_ALLOW_HTTP": "true",
    "AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES": "1024",
    "AUDIO_ANALYSIS_CONCURRENCY": "1",
    "AUDIO_ANALYSIS_DEADLINE_SECONDS": "8",
    "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS": "1",
    "AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS": "1",
    "PAX_ENGINE_MODE": "buffers",
    "PORT": "8080",
}


def command(*args, check=True, input_data=None):
    return subprocess.run(args, check=check, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def case(name, value=None, *, raw=None, auth=AUTH, content_type="application/json", method="POST", path="/api/analyze-r2", headers=None):
    body = json.dumps({"sourceUrl": SOURCE} if value is None else value).encode() if raw is None else raw
    request_headers = {} if headers is None else dict(headers)
    if auth is not None:
        request_headers["Authorization"] = auth
    if content_type is not None:
        request_headers["Content-Type"] = content_type
    return {"name": name, "body": body, "headers": request_headers, "method": method, "path": path}


def request_cases():
    cases = [case("health", method="GET", path="/healthz", raw=b"", auth=None)]
    cases.append(case("body-large-ignored-field", value={"sourceUrl": SOURCE, "ignored": "x" * (2 * 1024 * 1024 + 1)}))
    base = json.dumps({"sourceUrl": SOURCE, "ignored": ""}, separators=(",", ":"))
    for size in (65536, 65537):
        raw = base.replace('"ignored":""', '"ignored":"' + "x" * (size - len(base)) + '"').encode()
        assert len(raw) == size
        for name, auth, content_type in [("valid", AUTH, "application/json"), ("no-auth", None, "application/json"), ("plain", AUTH, "text/plain")]:
            cases.append(case(f"body-bytes-{size}-{name}", raw=raw, auth=auth, content_type=content_type))
        cases.append(case(f"body-bytes-{size}-chunked", raw=raw) | {"chunked": True})
    cases.append(case("body-too-large-malformed", raw=b"{" * 65537, auth=None))
    for depth in (990, 1010, 9982, 9983):
        cases.append(case("body-nesting-%d" % depth, raw=('{"sourceUrl":"' + SOURCE + '","ignored":' + "[" * depth + "0" + "]" * depth + "}").encode()))
    for depth in (63, 64, 65):
        for mixed in (False, True):
            value = json.dumps('[]{}\\"' * 128)
            for level in range(depth - 1):
                value = '{"child":' + value + "}" if mixed and level % 2 else "[" + value + "]"
            text = '{"sourceUrl":"' + SOURCE + '","ignored":' + value + "}"
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
                cases.append(case(f"body-depth-{depth}-{'mixed' if mixed else 'array'}-{encoding}", raw=text.encode(encoding)))
    over_depth = ('{"sourceUrl":"' + SOURCE + '","ignored":' + "[" * 64 + "0" + "]" * 64 + "}").encode()
    for auth_name, auth in [("none", None), ("wrong", "Basic eDp5"), ("malformed", "Basic !!!")]:
        for type_name, content_type in [("json", "application/json"), ("vendor", "application/problem+json"), ("plain", "text/plain"), ("absent", None)]:
            cases.append(case(f"depth-precedence-{auth_name}-{type_name}", raw=over_depth, auth=auth, content_type=content_type))
    for name, text in [
        ("invalid-before-deep", '{"broken":?,"ignored":' + "[" * 64 + "0" + "]" * 64 + "}"),
        ("unclosed-string", '{"ignored":"' + "[" * 128),
        ("escaped-backslash", '{"ignored":"\\\\", "nested":' + "[" * 64 + "0" + "]" * 64 + "}"),
    ]:
        cases.append(case("depth-syntax-" + name, raw=text.encode()))
    cases += [case("body-" + name, raw=value) for name, value in [
        ("empty", b""), ("malformed", b"{"), ("null", b"null"), ("array", b"[]"),
        ("string", b'"source"'), ("number", b"42"), ("boolean", b"true"),
        ("invalid-utf8", b'\xff'), ("nan", b'{"sourceUrl":NaN}'),
        ("ignored-nonfinite", ('{"sourceUrl":"' + SOURCE + '","ignored":[NaN,Infinity,-Infinity]}').encode()),
        ("ignored-lone-surrogate", ('{"sourceUrl":"' + SOURCE + '","ignored":"\\ud800"}').encode()),
        ("source-lone-surrogate", b'{"sourceUrl":"http://fixture.local:9000/\\ud800"}'),
        ("duplicate-key-last-invalid", ('{"sourceUrl":"' + SOURCE + '","sourceUrl":false}').encode()),
        ("duplicate-key-last-valid", ('{"sourceUrl":false,"sourceUrl":"' + SOURCE + '"}').encode()),
        ("utf8-bom", b'\xef\xbb\xbf' + json.dumps({"sourceUrl": SOURCE}).encode()),
        ("utf16", json.dumps({"sourceUrl": SOURCE}).encode("utf-16")),
        ("utf16-le-no-bom", json.dumps({"sourceUrl": SOURCE}).encode("utf-16-le")),
        ("utf32", json.dumps({"sourceUrl": SOURCE}).encode("utf-32")),
        ("utf32-be-no-bom", json.dumps({"sourceUrl": SOURCE}).encode("utf-32-be")),
    ]]
    for raw_name, raw in [("malformed", b"{"), ("missing", b"{}"), ("valid", json.dumps({"sourceUrl": SOURCE}).encode())]:
        for auth_name, auth in [("none", None), ("wrong", "Basic eDp5"), ("malformed", "Basic !!!")]:
            cases.append(case("precedence-" + raw_name + "-" + auth_name, raw=raw, auth=auth))
    for name, auth in [
        ("lower-scheme", AUTH.replace("Basic", "basic")), ("mixed-scheme", AUTH.replace("Basic", "bAsIc")),
        ("bearer", "Bearer token"), ("bare-basic", "Basic"), ("empty-basic", "Basic "),
        ("no-colon", "Basic bm9jb2xvbg=="), ("bad-base64", "Basic ###"),
        ("utf8", "Basic " + base64.b64encode("é:pass".encode()).decode()),
        ("extra-space", AUTH.replace("Basic ", "Basic  ")),
        ("tab-scheme", AUTH.replace("Basic ", "Basic\t")),
    ]:
        cases.append(case("auth-" + name, auth=auth))
    for name, content_type in [
        ("none", None), ("json", "application/json"), ("problem-json", "application/problem+json"),
        ("vendor-json", "application/vnd.pax+json; charset=utf-8"), ("json-parameters", "application/json; charset=utf-8"),
        ("upper-json", "APPLICATION/JSON"), ("plain", "text/plain"), ("text-json", "text/json"),
        ("form", "application/x-www-form-urlencoded"), ("invalid", "not-a-type"),
    ]:
        cases.append(case("content-type-" + name, content_type=content_type))
    for name, value in [
        ("missing-source", {}), ("field-name", {"source_url": SOURCE}),
        ("extra-fields", {"sourceUrl": SOURCE, "unrelated": {"ignored": True}}),
        ("null-source", {"sourceUrl": None}), ("number-source", {"sourceUrl": 123}),
        ("bool-source", {"sourceUrl": True}), ("array-source", {"sourceUrl": [SOURCE]}),
        ("object-source", {"sourceUrl": {"url": SOURCE}}),
        ("null-target", {"sourceUrl": SOURCE, "targetUrl": None}),
        ("null-target-alias-wins", {"sourceUrl": SOURCE, "targetUrl": None, "target_url": "invalid"}),
        ("invalid-target-alias-wins", {"sourceUrl": SOURCE, "targetUrl": "invalid", "target_url": None}),
        ("valid-source-alias-wins", {"sourceUrl": SOURCE, "source_url": "invalid"}),
        ("null-source-alias-wins", {"sourceUrl": None, "source_url": SOURCE}),
        ("invalid-source-alias-wins", {"sourceUrl": "invalid", "source_url": SOURCE}),
        ("denied-target-before-download", {"sourceUrl": SOURCE, "targetUrl": "https://denied.invalid/output.mp3"}),
        ("invalid-target-before-host-check", {"sourceUrl": "https://denied.invalid/source", "targetUrl": "invalid"}),
        ("both-invalid-types", {"sourceUrl": 12, "targetUrl": False}),
        ("built-in-r2-source-host", {"sourceUrl": "https://0f254469eceb1b88f5b5ff50405617cd.r2.cloudflarestorage.com/input.wav", "targetUrl": "https://denied.invalid/output.mp3"}),
    ]:
        cases.append(case("fields-" + name, value=value))
    for name, url in [
        ("missing", ""), ("relative", "/missing"), ("scheme-relative", "//fixture.local/missing"),
        ("ftp", "ftp://fixture.local/missing"), ("bare-host", "fixture.local"),
        ("host-case", "HTTP://FIXTURE.LOCAL:9000/missing.wav"),
        ("space-padding", "  " + SOURCE + "  "), ("newline-padding", "\n" + SOURCE + "\t"),
        ("dot-segments", "http://fixture.local:9000/a/../missing.wav"),
        ("escaped-dot", "http://fixture.local:9000/a/%2e%2e/missing.wav"),
        ("spaces", "http://fixture.local:9000/a b.wav?token=hidden-signature"),
        ("unicode-path", "http://fixture.local:9000/café.wav"),
        ("fragment", SOURCE + "#hidden-fragment"),
        ("encoded-slash", "http://fixture.local:9000/a%2fb.wav?x=%2f"),
        ("empty-path", "http://fixture.local:9000"),
        ("userinfo", "http://fixture-user:fixture-password@fixture.local:9000/missing.wav"),
        ("backslash", "http://fixture.local:9000\\missing.wav"),
        ("tab-in-path", "http://fixture.local:9000/mis\tsing.wav"),
        ("nul-in-path", "http://fixture.local:9000/missing\u0000.wav"),
        ("port-overflow", "http://fixture.local:65536/missing.wav"),
        ("trailing-host-dot", "http://fixture.local.:9000/missing.wav"),
        ("denied-host", "https://denied.invalid/hidden-signature"),
        ("oversize-url", SOURCE + "?" + "x" * 2100),
        ("2083-byte-url", SOURCE + "?" + "x" * (2083 - len(SOURCE) - 1)),
        ("2084-byte-url", SOURCE + "?" + "x" * (2084 - len(SOURCE) - 1)),
        ("unicode-byte-url", SOURCE + "?" + "é" * 1100),
    ]:
        cases.append(case("url-" + name, value={"sourceUrl": url}))
    for method, path in [
        ("GET", "/healthz/"), ("POST", "/api/analyze-r2/?key=hidden-signature"),
        ("GET", "/%68ealthz"), ("POST", "/api/%61nalyze-r2"), ("GET", "/healthz//"),
        ("HEAD", "/missing"),
        ("HEAD", "/healthz"), ("POST", "/healthz"), ("OPTIONS", "/healthz"),
        ("GET", "/api/analyze-r2"), ("OPTIONS", "/api/analyze-r2"),
        ("GET", "/api/analyze-r2/"), ("GET", "/missing/hidden-signature"),
        ("POST", "/api/analyze-upload"), ("GET", "/docs"), ("GET", "/openapi.json"),
        ("GET", "/healthz?x=hidden-signature"),
    ]:
        cases.append(case("route-" + method + "-" + path, method=method, path=path, auth=None, raw=b""))
    for name, request_id in [("short", "abc"), ("space", "bad id"), ("long", "a" * 65), ("empty", ""), ("min", "a" * 8), ("max", "a" * 64), ("allowed", "Aa0_-x12")]:
        cases.append(case("request-id-" + name, value={}, headers={"x-pax-request-id": request_id}))
    for name in transport_fixtures():
        cases.append(case("transport-" + name, value={"sourceUrl": "http://fixture.local:9000/transport/" + name}))
    return cases


def transport_fixtures():
    body = b"not an audio container"
    encoder = zlib.compressobj(wbits=-15)
    raw_deflate = encoder.compress(body) + encoder.flush()
    # This valid gzip stream has a long optional header but a small decoded body.
    corrupt_crc = bytearray(gzip.compress(b"x" * 4096, mtime=0))
    corrupt_crc[-8] ^= 1
    wire_oversize = gzip.compress(body, mtime=0)
    wire_oversize = wire_oversize[:3] + bytes([wire_oversize[3] | 4]) + wire_oversize[4:10] + struct.pack("<H", 2048) + b"x" * 2048 + wire_oversize[10:]
    return {
        "gzip-small": (gzip.compress(body, mtime=0), "gzip", None),
        "deflate-small": (zlib.compress(body), "deflate", None),
        "raw-deflate-small": (raw_deflate, "deflate", None),
        "gzip-decoded-oversize": (gzip.compress(b"x" * 4096, mtime=0), "gzip", None),
        "deflate-decoded-oversize": (zlib.compress(b"x" * 4096), "deflate", None),
        "gzip-wire-oversize": (wire_oversize, "gzip", None),
        "gzip-multiple-members": (gzip.compress(b"x" * 500, mtime=0) + gzip.compress(b"y" * 700, mtime=0), "gzip", None),
        "gzip-corrupt": (b"not a gzip stream", "gzip", None),
        "gzip-corrupt-crc-oversize": (bytes(corrupt_crc), "gzip", None),
        "deflate-corrupt": (b"not a deflate stream", "deflate", None),
        "truncated-body": (body, None, str(len(body) + 5)),
    }


def http_request(base_url, request, request_id, internal=False):
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or not (parsed.hostname in {"127.0.0.1", "localhost"} or (internal and parsed.hostname.startswith("pax-http-contract-"))):
        raise ValueError("Service endpoints must be loopback HTTP URLs")
    headers = dict(request["headers"])
    headers.setdefault("x-pax-request-id", request_id)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=12)
    try:
        chunked = request.get("chunked", False)
        request_body = request["body"]
        if chunked:
            raw = request_body
            request_body = (raw[offset:offset + 8192] for offset in range(0, len(raw), 8192))
        send_error = None
        try:
            connection.request(request["method"], request["path"], body=request_body, headers=headers, encode_chunked=chunked)
        except (BrokenPipeError, ConnectionResetError) as error:
            # A bounded server may send 413 and close before a large upload ends.
            # Read that response; a write error alone is never a passing result.
            send_error = error
        response = connection.getresponse()
        if send_error is not None and response.status != 413:
            raise send_error
        body = response.read()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        actual_id = response_headers.get("x-pax-request-id", "")
        supplied_id = headers["x-pax-request-id"]
        expected_id = supplied_id if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", supplied_id) else None
        assert (actual_id == expected_id) if expected_id else bool(re.fullmatch(r"[0-9a-f]{32}", actual_id)), "request ID policy differs"
        normalized = {}
        for name in ["content-type", "www-authenticate", "allow", "retry-after", "location", "x-pax-request-id"]:
            value = response_headers.get(name)
            if name == "location" and value:
                origin = "http://" + parsed.netloc
                value = value.replace(origin, "http://SERVICE")
            normalized[name] = "REQUEST_ID" if name == "x-pax-request-id" else value
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and "requestId" in payload:
                assert payload["requestId"] == actual_id
                payload["requestId"] = "REQUEST_ID"
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = body.hex()
        return {"status": response.status, "headers": normalized, "body": payload}, actual_id
    finally:
        connection.close()


class Fixture:
    @staticmethod
    def serve():
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                print(json.dumps({"fixture": True, "path": self.path, "method": self.command}), flush=True)
                fixture = transport_fixtures().get(self.path.removeprefix("/transport/")) if self.path.startswith("/transport/") else None
                if fixture is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body, encoding, declared_length = fixture
                self.send_response(200)
                self.send_header("Content-Length", declared_length or str(len(body)))
                if encoding is not None:
                    self.send_header("Content-Encoding", encoding)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()


class ContainerSet:
    def __init__(self, python_image, rust_image):
        self.images = {"python": python_image, "rust": rust_image}
        self.network = "pax-http-contract-" + uuid.uuid4().hex[:12]
        self.names = []

    def __enter__(self):
        command("docker", "network", "create", "--internal", self.network)
        name = self.network + "-fixture"
        self.names.append(name)
        command("docker", "run", "-d", "--name", name, "--network", self.network, "--network-alias", "fixture.local", "--entrypoint", "python3", "-v", str(Path(__file__).resolve()) + ":/checks/http_parity.py:ro", self.images["python"], "/checks/http_parity.py", "--serve-fixture")
        self.fixture_name = name
        return self

    def __exit__(self, *_args):
        for name in reversed(self.names):
            command("docker", "rm", "-f", name, check=False)
        command("docker", "network", "rm", self.network, check=False)

    def start(self, kind, changes=None):
        environment = dict(BASE_ENV)
        for key, value in (changes or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        name = self.network + "-" + kind + "-" + uuid.uuid4().hex[:8]
        self.names.append(name)
        args = ["docker", "run", "-d", "--name", name, "--network", self.network, "--memory", "768m", "--cpus", "1"]
        for key, value in environment.items():
            args += ["-e", key + "=" + value]
        args += [self.images[kind]]
        command(*args)
        url = "http://" + name + ":8080"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if command("docker", "inspect", "-f", "{{.State.Running}}", name).stdout.strip() == "false":
                return name, None
            try:
                result, _ = self.request(url, case("ready", method="GET", path="/healthz", raw=b"", auth=None), "ready-check-0001")
                if result["status"] == 200:
                    return name, url
            except (ConnectionError, OSError, http.client.HTTPException, subprocess.CalledProcessError):
                pass
            time.sleep(0.1)
        raise AssertionError(kind + " did not become ready within 30 seconds")

    def request(self, url, request, request_id):
        encoded = dict(request)
        encoded["body"] = request["body"].hex()
        args = json.dumps({"url": url, "request": encoded, "requestId": request_id})
        try:
            result = command("docker", "exec", "-i", self.fixture_name, "python3", "/checks/http_parity.py", "--single-request", input_data=args)
        except subprocess.CalledProcessError as error:
            if request["name"] != "ready":
                print(f"HTTP case {request['name']!r} on {urlsplit(url).hostname} failed (exit {error.returncode}):\n{(error.stderr or '')[-4096:]}", file=sys.stderr)
            raise
        return json.loads(result.stdout)

    def remove(self, name):
        command("docker", "rm", "-f", name)
        self.names.remove(name)

    def fixture_lines(self):
        return [json.loads(line) for line in command("docker", "logs", self.fixture_name).stdout.splitlines() if line.startswith('{"fixture":')]

    def logs(self, name):
        raw = command("docker", "logs", name).stdout
        for secret in ["contract-test-password", "hidden-signature", "fixture-password", SOURCE]:
            assert secret not in raw, "Sensitive input appeared in service logs"
        parsed = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("service") == "audio-analysis":
                parsed.append(value)
        return parsed


def check_logs(lines, requests):
    checked = []
    for request, response, request_id in requests:
        try:
            matches = [line for line in lines if line.get("requestId") == request_id]
            assert len(matches) == 1, "must log exactly one request line"
            line = matches[0]
            assert set(line) == LOG_KEYS, "log schema differs"
            assert isinstance(line["durationMs"], (int, float)) and line["durationMs"] >= 0
            assert line["service"] == "audio-analysis"
            assert line["method"] == request["method"] and line["status"] == response["status"]
            path = unquote(urlsplit(request["path"]).path)
            assert line["route"] == (path if path in {"/healthz", "/api/analyze-r2"} else "other")
            checked.append({key: value for key, value in line.items() if key not in {"durationMs", "requestId"}})
        except AssertionError as exc:
            checked.append({"gateError": request["name"] + ": " + str(exc)})
    return checked


def compare_requests(containers):
    servers = {kind: containers.start(kind) for kind in ("python", "rust")}
    assert all(url for _, url in servers.values()), "Both images must start"
    requests = {kind: [] for kind in servers}
    results = []
    for index, request in enumerate(request_cases()):
        outputs = {}
        for kind, (_, url) in servers.items():
            before = len(containers.fixture_lines())
            response, request_id = containers.request(url, request, "contract-case-%04d" % index)
            access = containers.fixture_lines()[before:]
            outputs[kind] = {"response": response, "fixtureRequests": access}
            requests[kind].append((request, response, request_id))
        results.append({"case": request["name"], "passed": outputs["python"] == outputs["rust"], **outputs})
    time.sleep(0.2)
    logs = {kind: check_logs(containers.logs(name), requests[kind]) for kind, (name, _) in servers.items()}
    for index, result in enumerate(results):
        result["pythonLog"] = logs["python"][index]
        result["rustLog"] = logs["rust"][index]
        result["passed"] = result["passed"] and logs["python"][index] == logs["rust"][index] and all("gateError" not in logs[kind][index] for kind in ("python", "rust"))
    for name, _ in servers.values():
        containers.remove(name)
    return results


def config_cases():
    return [
        ("default-settings", {key: None for key in BASE_ENV if key not in {"AUDIO_ANALYSIS_BASIC_AUTH_USERNAME", "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", "AUDIO_ANALYSIS_ALLOWED_HOSTS", "PORT", "PAX_ENGINE_MODE"}}),
        ("empty-password", {"AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD": ""}),
        ("whitespace-password", {"AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD": " "}),
        ("empty-username", {"AUDIO_ANALYSIS_BASIC_AUTH_USERNAME": ""}),
        ("default-username", {"AUDIO_ANALYSIS_BASIC_AUTH_USERNAME": None}),
        ("missing-password", {"AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD": None}),
        ("empty-hosts", {"AUDIO_ANALYSIS_ALLOWED_HOSTS": ""}),
        ("blank-hosts", {"AUDIO_ANALYSIS_ALLOWED_HOSTS": " , "}),
        ("missing-hosts", {"AUDIO_ANALYSIS_ALLOWED_HOSTS": None}),
        ("trim-lower-hosts", {"AUDIO_ANALYSIS_ALLOWED_HOSTS": " FIXTURE.LOCAL , denied.invalid "}),
        ("http-uppercase", {"AUDIO_ANALYSIS_ALLOW_HTTP": "TRUE"}),
        ("http-mixed-case", {"AUDIO_ANALYSIS_ALLOW_HTTP": "TrUe"}),
        ("http-whitespace", {"AUDIO_ANALYSIS_ALLOW_HTTP": " true "}),
        ("http-false", {"AUDIO_ANALYSIS_ALLOW_HTTP": "false"}),
        ("zero-workers", {"AUDIO_ANALYSIS_CONCURRENCY": "0"}),
        ("too-many-workers", {"AUDIO_ANALYSIS_CONCURRENCY": "5"}),
        ("integer-whitespace", {"AUDIO_ANALYSIS_CONCURRENCY": " 1 "}),
        ("integer-plus", {"AUDIO_ANALYSIS_CONCURRENCY": "+1"}),
        ("integer-underscores", {"AUDIO_ANALYSIS_CONCURRENCY": "0_1"}),
        ("integer-unicode-digits", {"AUDIO_ANALYSIS_CONCURRENCY": "١"}),
        ("large-download-limit", {"AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES": "10000000000000000000000000000000000000000000000"}),
        ("large-queue-limit", {"AUDIO_ANALYSIS_QUEUE_LIMIT": "10000000000000000000000000000000000000000000000"}),
        ("zero-queue", {"AUDIO_ANALYSIS_QUEUE_LIMIT": "0"}),
        ("negative-queue", {"AUDIO_ANALYSIS_QUEUE_LIMIT": "-1"}),
        ("zero-wait", {"AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS": "0"}),
        ("wait-at-deadline", {"AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS": "8"}),
        ("zero-deadline", {"AUDIO_ANALYSIS_DEADLINE_SECONDS": "0"}),
        ("large-deadline", {"AUDIO_ANALYSIS_DEADLINE_SECONDS": "111"}),
        ("zero-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "0"}),
        ("negative-size", {"AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES": "-1"}),
        ("negative-download-timeout", {"AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS": "-1"}),
        ("nan-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "nan"}),
        ("infinite-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "inf"}),
        ("negative-nan-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "-NaN"}),
        ("positive-infinity-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "+Infinity"}),
        ("negative-infinity-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "-inf"}),
        ("overflow-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "1e309"}),
        ("large-finite-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "1e300"}),
        ("small-finite-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "0.000000001"}),
        ("negative-duration", {"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "-1"}),
        ("trim-release", {"VERCEL_GIT_COMMIT_SHA": " abc123 "}),
        ("empty-release", {"VERCEL_GIT_COMMIT_SHA": " "}),
        ("custom-data-dir", {"AUDIO_ANALYSIS_DATA_DIR": "/tmp/parity-custom"}),
    ]


def compare_config(containers):
    results = []
    for index, (name, changes) in enumerate(config_cases()):
        outputs = {}
        for kind in ("python", "rust"):
            container, url = containers.start(kind, changes)
            output = {"started": url is not None}
            if url is not None:
                health, _ = containers.request(url, case("health", method="GET", path="/healthz", raw=b"", auth=None), "config-health-%04d" % index)
                response, _ = containers.request(url, case("source"), "config-source-%04d" % index)
                output.update(health=health, source=response)
            outputs[kind] = output
            containers.remove(container)
        results.append({"case": name, "passed": outputs["python"] == outputs["rust"], **outputs})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-image")
    parser.add_argument("--rust-image")
    parser.add_argument("--scope", choices=["request", "config", "all"], default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve-fixture", action="store_true")
    parser.add_argument("--single-request", action="store_true")
    args = parser.parse_args()
    if args.single_request:
        request = json.load(sys.stdin)
        request["request"]["body"] = bytes.fromhex(request["request"]["body"])
        print(json.dumps(http_request(request["url"], request["request"], request["requestId"], internal=True)))
        return
    if args.serve_fixture:
        Fixture.serve()
        return
    if not args.python_image or not args.rust_image or not args.output:
        parser.error("--python-image, --rust-image and --output are required")
    manifest = {kind: json.loads(command("docker", "image", "inspect", image).stdout)[0]["Id"] for kind, image in [("python", args.python_image), ("rust", args.rust_image)]}
    service_root = Path(__file__).resolve().parents[2]
    reference_files = {name: hashlib.sha256((service_root / name).read_bytes()).hexdigest() for name in ("main.py", "models.py", "codec_compatibility.py", "requirements.lock", "Dockerfile.python")}
    oracle_script = "import hashlib,json,importlib.metadata; from pathlib import Path; print(json.dumps({'files':{n:hashlib.sha256(Path('/app',n).read_bytes()).hexdigest() for n in ['main.py','models.py','codec_compatibility.py','requirements.lock']},'versions':{n:importlib.metadata.version(n) for n in ['fastapi','starlette','pydantic','httpx']}}))"
    oracle = json.loads(command("docker", "run", "--rm", "--network", "none", "--entrypoint", "python3", manifest["python"], "-c", oracle_script).stdout)
    assert all(oracle["files"][name] == reference_files[name] for name in oracle["files"]), "Reference image does not contain current Python sources/lock"
    results = {"images": manifest, "oracle": oracle, "referenceSourceHashes": reference_files, "runnerSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "scope": args.scope}
    with ContainerSet(manifest["python"], manifest["rust"]) as containers:
        if args.scope in {"request", "all"}:
            results["requests"] = compare_requests(containers)
        if args.scope in {"config", "all"}:
            results["configuration"] = compare_config(containers)
    cases = results.get("requests", []) + results.get("configuration", [])
    results["passed"] = all(result["passed"] for result in cases)
    results["caseCount"] = len(cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"passed": results["passed"], "cases": len(cases), "failures": [result["case"] for result in cases if not result["passed"]]}))
    if not results["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
