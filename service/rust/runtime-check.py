#!/usr/bin/env python3
"""Exact audio corpus and process-lifecycle proof on an internal Docker network."""
import argparse
import base64
import concurrent.futures
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid
import wave

ROOT = Path(__file__).resolve().parent
PASSWORD = "local-runtime-test"
AUTH = "Basic " + base64.b64encode(("analysis:" + PASSWORD).encode()).decode()
EXACT = ("bpm", "key_standard", "key_camelot", "duration", "peaks", "preview", "playback")
CONFIDENCE = ("bpm_confidence", "key_confidence")


def command(*args, check=True, input_data=None):
    return subprocess.run(args, input=input_data, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def extract_corpus():
    manifest = json.loads((ROOT / "compat-data.json").read_text())
    archive_path = ROOT / "compat-data.tar.xz"
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == manifest["archiveSha256"]
    folder = ROOT / ".compat-data"
    folder.mkdir(exist_ok=True)
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            assert member.isfile() and not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            target = folder / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
    return folder


class Fixture(BaseHTTPRequestHandler):
    lock = threading.Lock()
    entered = set()
    release = threading.Event()
    uploads = {}

    def log_message(self, *_args):
        pass

    def respond(self, status, body=b"", content_type="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/stats":
            with self.lock:
                data = {"entered": sorted(self.entered), "uploads": dict(self.uploads)}
            return self.respond(200, json.dumps(data).encode(), "application/json")
        if route == "/release":
            self.release.set()
            return self.respond(200, b"{}", "application/json")
        if route == "/reset":
            with self.lock:
                self.entered.clear()
                self.uploads.clear()
                self.release.clear()
            return self.respond(200, b"{}", "application/json")
        name = Path(route).name
        path = Path("/corpus/fixtures") / name
        if not path.is_file():
            return self.respond(404)
        with self.lock:
            self.entered.add(route)
        if route.startswith("/hold/"):
            self.release.wait(110)
        return self.respond(200, path.read_bytes())

    def do_PUT(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        record = {"sizeBytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                  "contentType": self.headers.get("Content-Type"), "contentLength": self.headers.get("Content-Length")}
        with self.lock:
            self.uploads[self.path] = record
            self.entered.add(self.path)
        if self.path.startswith("/hold-put/"):
            self.release.wait(110)
        return self.respond(200)


def local_request(args):
    conn = http.client.HTTPConnection(args["host"], args.get("port", 8080), timeout=120)
    body = None if args.get("body") is None else json.dumps(args["body"]).encode()
    headers = {"Content-Type": "application/json", "Authorization": AUTH,
               "x-pax-request-id": args.get("requestId", "runtime-control-0001")}
    conn.request(args.get("method", "POST"), args.get("path", "/api/analyze-r2"), body, headers)
    if args.get("disconnect"):
        # Close only after the fixture observes the admitted download. Closing
        # immediately can cancel the body before the server accepts any job.
        source = urlsplit(args["body"]["sourceUrl"])
        until = time.monotonic() + 10
        while time.monotonic() < until:
            probe = http.client.HTTPConnection(source.hostname, source.port, timeout=2)
            try:
                probe.request("GET", "/stats")
                entered = json.loads(probe.getresponse().read())["entered"]
                if source.path in entered:
                    conn.close()
                    return {"disconnected": True}
            finally:
                probe.close()
            time.sleep(0.03)
        conn.close()
        raise AssertionError("Disconnect fixture did not observe an admitted job")
    response = conn.getresponse()
    raw = response.read()
    result = {"status": response.status, "body": json.loads(raw) if raw else None,
              "headers": {k.lower(): v for k, v in response.getheaders()}}
    conn.close()
    return result


class Run:
    def __init__(self, image, oracle, corpus):
        self.image = command("docker", "image", "inspect", image, "--format", "{{.Id}}").stdout.strip()
        self.oracle = command("docker", "image", "inspect", oracle, "--format", "{{.Id}}").stdout.strip()
        self.network = "pax-dropin-runtime-" + uuid.uuid4().hex[:10]
        self.fixture = self.network + "-fixture"
        self.names = []
        self.scratch = Path(tempfile.mkdtemp(prefix="pax-dropin-runtime-"))
        self.scratch.chmod(0o755)
        self.corpus = corpus

    def __enter__(self):
        command("docker", "network", "create", "--internal", self.network)
        command("docker", "run", "-d", "--name", self.fixture, "--network", self.network,
                "--network-alias", "fixture.local", "-v", str(ROOT) + ":/checks:ro",
                "-v", str(self.corpus) + ":/corpus:ro", "--entrypoint", "python3", self.oracle,
                "/checks/runtime-check.py", "fixture")
        self.names.append(self.fixture)
        self.wait(lambda: self.remote(self.fixture, port=9000, path="/stats", method="GET"), 15)
        return self

    def __exit__(self, *_):
        for name in reversed(self.names):
            command("docker", "rm", "-f", name, check=False)
        command("docker", "network", "rm", self.network, check=False)
        shutil.rmtree(self.scratch)

    @staticmethod
    def wait(predicate, seconds=10):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            try:
                value = predicate()
                if value:
                    return value
            except (subprocess.CalledProcessError, ConnectionError, OSError):
                pass
            time.sleep(0.03)
        raise AssertionError("Observable state did not arrive before timeout")

    def remote(self, host, **kwargs):
        payload = json.dumps({"host": host, **kwargs})
        result = command("docker", "exec", "-i", self.fixture, "python3", "/checks/runtime-check.py",
                         "request", input_data=payload)
        return json.loads(result.stdout)

    def stats(self):
        return self.remote(self.fixture, port=9000, path="/stats", method="GET")["body"]

    def control(self, path):
        return self.remote(self.fixture, port=9000, path=path, method="GET")

    def start(self, *, changes=None, wrapper=None, image=None, memory="1g"):
        name = self.network + "-" + uuid.uuid4().hex[:8]
        folder = self.scratch / name
        folder.mkdir(mode=0o777)
        folder.chmod(0o777)
        env = {"AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD": PASSWORD, "AUDIO_ANALYSIS_ALLOWED_HOSTS": "fixture.local",
               "AUDIO_ANALYSIS_ALLOW_HTTP": "true", "AUDIO_ANALYSIS_CONCURRENCY": "2",
               "AUDIO_ANALYSIS_DATA_DIR": "/data", "PORT": "8080",
               # These old experiment flags must have no effect on production behavior.
               "PAX_ENGINE_MODE": "shared_fft", "REUSE_ALGORITHMS": "1", "OVERLAP_ENCODING": "1"}
        env.update(changes or {})
        args = ["docker", "run", "-d", "--name", name, "--network", self.network, "--memory", memory, "--cpus", "2",
                "-v", str(folder) + ":/data"]
        if wrapper:
            script = folder / (wrapper + "-wrapper")
            script.write_text("#!/bin/sh\nprintf '%s\\n' \"$$\" > /data/command-entered\n"
                              "cat /proc/$$/limits > /data/command-limits\nexec /bin/sleep 110\n")
            script.chmod(0o755)
            args += ["-v", str(script) + ":/usr/local/bin/" + wrapper + ":ro"]
        for key, value in env.items():
            args += ["-e", key + "=" + value]
        command(*args, image or self.image)
        self.names.append(name)
        self.wait(lambda: self.remote(name, port=int(env["PORT"]), path="/healthz", method="GET")["status"] == 200, 30)
        return name, folder

    def analyze(self, name, fixture, request_id, *, target=False, source_route="source", target_route="put", disconnect=False):
        body = {"sourceUrl": "http://fixture.local:9000/" + source_route + "/" + fixture}
        if target:
            body["targetUrl"] = "http://fixture.local:9000/" + target_route + "/" + request_id
        return self.remote(name, body=body, requestId=request_id, disconnect=disconnect)

    @staticmethod
    def clean(folder):
        return not list((folder / "audio-analysis/tmp").glob("*"))

    def logs(self, name):
        raw = command("docker", "logs", name).stdout
        assert PASSWORD not in raw and "sourceUrl" not in raw and "fixture.local" not in raw
        return [json.loads(line) for line in raw.splitlines() if line.startswith('{"')]

    def stopped(self, name):
        state = json.loads(command("docker", "inspect", name).stdout)[0]["State"]
        if state["Running"]:
            return False
        assert state["ExitCode"] == 0 and not state["OOMKilled"], state
        return True


def check_audio(run, architecture, reference=False):
    golden_name = "native-golden.json" if architecture == "arm64" else "amd64-wheel-golden.json"
    golden = json.loads((run.corpus / "goldens" / golden_name).read_text())["outputs"]
    name, folder = run.start(image=run.oracle if reference else None)
    files = list(golden)
    assert len(files) == 14
    shuffled = list(files)
    random.Random(141030).shuffle(shuffled)
    results = []

    def one(fixture, ordinal):
        request_id = "audio-golden-%04d" % ordinal
        expected = golden[fixture]
        response = run.analyze(name, fixture, request_id, target=expected["playback"] is not None)
        assert response["status"] == 200, (fixture, response)
        assert response["headers"]["x-pax-request-id"] == request_id
        actual = response["body"]
        assert set(actual) == set(EXACT + CONFIDENCE)
        for field in EXACT:
            assert actual[field] == expected[field], (fixture, field, actual[field], expected[field])
        for field in CONFIDENCE:
            assert math.isfinite(actual[field]) and math.isclose(actual[field], expected[field], rel_tol=0, abs_tol=1e-6), (fixture, field)
        if expected["playback"] is not None:
            upload = run.stats()["uploads"]["/put/" + request_id]
            assert upload == {**expected["playback"], "contentType": "audio/mpeg", "contentLength": str(expected["playback"]["sizeBytes"])}
        return {"fixture": fixture, "requestId": request_id, "passed": True}

    order = files if reference else files + list(reversed(files)) + shuffled
    for index, fixture in enumerate(order):
        results.append(one(fixture, index))
    if not reference:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            jobs = [executor.submit(one, fixture, 42 + index) for index, fixture in enumerate(["pulse-120-a4.aif", "silence-8.wav", "stereo-48k.wav", "mono-22050.wav"] * 2)]
            results.extend(job.result() for job in jobs)
    run.wait(lambda: run.clean(folder))
    logs = run.logs(name)
    for result in results:
        assert len([line for line in logs if line.get("requestId") == result["requestId"]]) == 1
    command("docker", "kill", "--signal=TERM", name)
    run.wait(lambda: run.stopped(name), 30)
    assert run.clean(folder)
    return results


def check_audio_errors(run):
    python, python_folder = run.start(image=run.oracle)
    rust, rust_folder = run.start()
    results = []
    for fixture, status in [("corrupt.wav", 422), ("empty.wav", 422), ("overlong.wav", 413)]:
        request_id = "audio-failure-" + fixture.replace(".", "-")
        expected = run.analyze(python, fixture, request_id)
        actual = run.analyze(rust, fixture, request_id)
        assert actual["status"] == expected["status"] == status, (fixture, expected, actual)
        assert actual["body"] == expected["body"], (fixture, expected, actual)
        for name, folder in [(python, python_folder), (rust, rust_folder)]:
            run.wait(lambda: run.clean(folder))
            lines = [line for line in run.logs(name) if line.get("requestId") == request_id]
            assert len(lines) == 1
        expected_log = next(line for line in run.logs(python) if line.get("requestId") == request_id)
        actual_log = next(line for line in run.logs(rust) if line.get("requestId") == request_id)
        for key in ["status", "stage", "errorClass"]:
            assert expected_log[key] == actual_log[key], (fixture, key, expected_log, actual_log)
        results.append({"fixture": fixture, "status": status, "stage": actual["body"]["stage"], "passed": True})
    for name in [python, rust]:
        command("docker", "kill", "--signal=TERM", name)
        run.wait(lambda: run.stopped(name), 30)
    return results


def check_input_rules(run):
    """Adversarial bytes stay on the internal fixture network and temporary disk."""
    fixtures = run.corpus / "fixtures"
    generated = []

    def write(name, data):
        path = fixtures / name
        path.write_bytes(data)
        generated.append(path)
        return name

    def float_wav(value):
        pcm = struct.pack("<f", value) * 44100
        return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 3, 1, 44100, 176400, 4, 32)
                + b"data" + struct.pack("<I", len(pcm)) + pcm)

    results = []
    try:
        write("security-hls.wav", b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:8\n"
              b"#EXTINF:8,\n/data/private.mp3\n#EXT-X-ENDLIST\n")
        write("security-hls-network.wav", b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:8\n"
              b"#EXTINF:8,\nhttp://fixture.local:9000/segments/private.mp3\n#EXT-X-ENDLIST\n")
        write("security-concat.wav", b"ffconcat version 1.0\nfile private.mp3\n")
        for label, value in [("nan", math.nan), ("positive-inf", math.inf), ("negative-inf", -math.inf)]:
            write("security-" + label + ".wav", float_wav(value))
        write("security-finite.wav", float_wav(0.125))
        flac = bytearray(subprocess.check_output([
            "docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
            "--entrypoint", "ffmpeg", run.oracle, "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=44100:duration=2", "-c:a", "flac", "-f", "flac", "pipe:1"
        ], stderr=subprocess.DEVNULL))
        assert flac[:4] == b"fLaC" and (flac[4] & 127) == 0
        packed = int.from_bytes(flac[18:26], "big")
        flac[18:26] = ((packed & ~((1 << 36) - 1)) | 4410).to_bytes(8, "big")
        write("security-understated.flac", flac)
        python, python_folder = run.start(image=run.oracle, changes={"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "1"})
        rust, rust_folder = run.start(changes={"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "1"})
        for folder in [python_folder, rust_folder]:
            shutil.copyfile(fixtures / "pulse-120.mp3", folder / "private.mp3")
        cases = [("security-hls.wav", 422, "probe"), ("security-hls-network.wav", 422, "probe"),
                 ("security-concat.wav", 422, "probe"), ("security-understated.flac", 413, "decode")]
        cases += [("security-" + label + ".wav", 422, "decode")
                  for label in ["nan", "positive-inf", "negative-inf"]]
        for index, (fixture, status, stage) in enumerate(cases):
            request_id = "input-rule-%04d" % index
            expected = run.analyze(python, fixture, request_id, target=True)
            actual = run.analyze(rust, fixture, request_id, target=True)
            assert expected["status"] == actual["status"] == status, (fixture, expected, actual)
            assert expected["body"] == actual["body"], (fixture, expected, actual)
            assert actual["body"]["stage"] == stage, (fixture, actual)
            logs = []
            for name, folder in [(python, python_folder), (rust, rust_folder)]:
                run.wait(lambda: run.clean(folder))
                entries = [line for line in run.logs(name) if line.get("requestId") == request_id]
                assert len(entries) == 1, entries
                logs.append(entries[0])
                assert run.remote(name, path="/healthz", method="GET")["status"] == 200
            for key in ["status", "stage", "errorClass"]:
                assert logs[0][key] == logs[1][key], (fixture, key, logs)
            assert "/put/" + request_id not in run.stats()["uploads"]
            results.append({"fixture": fixture, "status": status, "stage": stage, "passed": True})
        assert "/segments/private.mp3" not in run.stats()["entered"]
        expected = run.analyze(python, "security-finite.wav", "input-finite-control")
        actual = run.analyze(rust, "security-finite.wav", "input-finite-control")
        assert expected["status"] == actual["status"] == 200, (expected, actual)
        for key in EXACT:
            assert expected["body"][key] == actual["body"][key], (key, expected, actual)
        for key in CONFIDENCE:
            assert math.isclose(expected["body"][key], actual["body"][key], rel_tol=0, abs_tol=1e-6)
        results.append({"fixture": "security-finite.wav", "status": 200, "passed": True})
        for name in [python, rust]:
            command("docker", "kill", "--signal=TERM", name)
            run.wait(lambda: run.stopped(name), 30)
        return results
    finally:
        for path in generated:
            path.unlink(missing_ok=True)


def check_decode_memory(run):
    """An understated 30-minute FLAC must stop at 1 s inside a 128 MiB container."""
    name = "security-memory-understated.flac"
    path = run.corpus / "fixtures" / name
    control_path = path.with_name("security-memory-control.wav")
    flac = bytearray(subprocess.check_output([
        "docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
        "--entrypoint", "ffmpeg", run.oracle, "-v", "error", "-f", "lavfi", "-i",
        "anullsrc=r=44100:cl=mono", "-t", "1800", "-c:a", "flac", "-f", "flac", "pipe:1"
    ], stderr=subprocess.DEVNULL))
    assert flac[:4] == b"fLaC" and (flac[4] & 127) == 0
    packed = int.from_bytes(flac[18:26], "big")
    flac[18:26] = ((packed & ~((1 << 36) - 1)) | 4410).to_bytes(8, "big")
    path.write_bytes(flac)
    pcm = struct.pack("<f", 0.125) * 44100
    control_path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                             + struct.pack("<IHHIIHH", 16, 3, 1, 44100, 176400, 4, 32)
                             + b"data" + struct.pack("<I", len(pcm)) + pcm)
    try:
        rust, folder = run.start(memory="128m", changes={
            "AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "1", "AUDIO_ANALYSIS_CONCURRENCY": "1",
            "AUDIO_ANALYSIS_RESOURCE_PROFILE": "1",
        })
        address_limits = checked_worker_limits(rust)
        response = run.analyze(rust, name, "bounded-memory", target=True)
        assert response["status"] == 413 and response["body"]["stage"] == "decode", response
        run.wait(lambda: run.clean(folder))
        assert "/put/bounded-memory" not in run.stats()["uploads"]
        # The full float32 signal would need 302.8 MiB, before vector growth/DSP.
        peak = int(command("docker", "exec", rust, "cat", "/sys/fs/cgroup/memory.peak").stdout)
        # File-backed library pages can fill the cgroup and be reclaimed at its
        # limit. Reaching that value is valid; an OOM event is not.
        assert peak <= 128 * 1024 * 1024, peak
        events = command("docker", "exec", rust, "cat", "/sys/fs/cgroup/memory.events").stdout
        assert all(int(line.split()[1]) == 0 for line in events.splitlines()
                   if line.split()[0] in {"oom", "oom_kill"}), events
        assert run.remote(rust, path="/healthz", method="GET")["status"] == 200
        control = run.analyze(rust, control_path.name, "bounded-memory-next")
        assert control["status"] == 200 and control["body"]["duration"] == 1.0, control
        command("docker", "kill", "--signal=TERM", rust)
        run.wait(lambda: run.stopped(rust), 30)
        results = [{"case": "understated-1800s-at-1s-limit", "status": 413,
                 "containerLimitBytes": 128 * 1024 * 1024, "cgroupPeakBytes": peak,
                 "workerVirtualLimitsBytes": address_limits,
                 "unboundedPcmBytes": 1800 * 44100 * 4, "passed": True}]
        rust, folder = run.start(changes={"AUDIO_ANALYSIS_MAX_DURATION_SECONDS": "3600",
                                         "AUDIO_ANALYSIS_CONCURRENCY": "1"})
        checked_worker_limits(rust)
        response = run.analyze(rust, name, "bounded-pcm-cap", target=True)
        assert response["status"] == 413 and response["body"] == {
            "detail": "Decoded audio exceeds memory limit", "stage": "decode",
            "requestId": "bounded-pcm-cap"}, response
        run.wait(lambda: run.clean(folder))
        assert "/put/bounded-pcm-cap" not in run.stats()["uploads"]
        control = run.analyze(rust, control_path.name, "bounded-pcm-cap-next")
        assert control["status"] == 200 and control["body"]["duration"] == 1.0, control
        command("docker", "kill", "--signal=TERM", rust)
        run.wait(lambda: run.stopped(rust), 30)
        results.append({"case": "absolute-256mib-pcm-cap-and-recovery", "status": 413, "passed": True})
        return results
    finally:
        path.unlink(missing_ok=True)
        control_path.unlink(missing_ok=True)


def address_limit(text):
    return int(next(line for line in text.splitlines() if line.startswith("Max address space")).split()[3])


def checked_worker_limits(name):
    logs = command("docker", "logs", name)
    rows = [json.loads(line) for line in (logs.stdout + "\n" + logs.stderr).splitlines()
            if line.startswith('{"')]
    limits = [row for row in rows if row.get("event") == "worker-memory-limit"]
    assert limits, "No installed worker address-space limits"
    result = []
    for row in limits:
        assert row["additionalVirtualBytes"] == 512 * 1024 * 1024, row
        assert row["virtualLimitBytes"] <= row["baselineVirtualBytes"] + row["additionalVirtualBytes"], row
        actual = address_limit(command("docker", "exec", name, "cat",
                                       "/proc/%d/limits" % row["workerPid"]).stdout)
        assert actual == row["virtualLimitBytes"], row
        result.append(actual)
    return result


def check_extra_containers(run):
    """Compare advertised containers absent from the frozen WAV/AIFF/MP3 corpus."""
    output = run.scratch / "encoded-controls"
    output.mkdir(mode=0o777)
    output.chmod(0o777)
    formats = [("flac", "flac", 200), ("ogg", "libvorbis", 200), ("m4a", "aac", 200),
               ("webm", "libopus", 422), ("webm", "libvorbis", 200), ("ogg", "libopus", 422),
               ("wav", "adpcm_ima_wav", 422), ("wav", "adpcm_ms", 422)]
    generated = []
    reference_statuses = {}
    results = []
    try:
        for extension, codec, reference_status in formats:
            name = "container-" + codec + "." + extension
            command("docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
                    "-v", str(run.corpus / "fixtures") + ":/input:ro", "-v", str(output) + ":/output",
                    "--entrypoint", "ffmpeg", run.oracle, "-v", "error", "-i", "/input/pulse-120-a4.wav",
                    "-t", "2", "-map_metadata", "-1", "-c:a", codec, "/output/" + name)
            path = run.corpus / "fixtures" / name
            shutil.copyfile(output / name, path)
            generated.append(path)
            reference_statuses[path.name] = reference_status
        for channels, rate in [(1, 44100), (1, 22050), (2, 48000)]:
            path = run.corpus / "fixtures" / ("container-zork-%d-%d.wav" % (channels, rate))
            generated.append(path)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(channels)
                wav.setsampwidth(1)
                wav.setframerate(rate)
                length = channels * rate * 2
                wav.writeframes((bytes(range(256)) * ((length + 255) // 256))[:length])
            data = bytearray(path.read_bytes())
            assert data[12:16] == b"fmt " and data[20:22] == b"\x01\x00"
            data[20:22] = b"\x11\x00"
            path.write_bytes(data)
            reference_statuses[path.name] = 422
        python, python_folder = run.start(image=run.oracle)
        rust, rust_folder = run.start()
        for path in generated:
            run.control("/reset")
            request_id = path.name.replace(".", "-")
            expected = run.analyze(python, path.name, request_id, target=True)
            actual = run.analyze(rust, path.name, request_id, target=True)
            differences = []
            reference_status = reference_statuses[path.name]
            if expected["status"] != reference_status:
                differences.append("referenceStatus")
            if reference_status == 422 and expected["body"].get("stage") != "decode":
                differences.append("referenceStage")
            if reference_status == 422 and run.stats()["uploads"]:
                differences.append("unexpectedUpload")
            if expected["status"] != actual["status"]:
                differences.append("status")
            elif expected["status"] == 200:
                differences += [key for key in EXACT if expected["body"][key] != actual["body"][key]]
                differences += [key for key in CONFIDENCE if not math.isclose(
                    expected["body"][key], actual["body"][key], rel_tol=0, abs_tol=1e-6)]
            elif expected["body"] != actual["body"]:
                differences.append("errorBody")
            for folder in [python_folder, rust_folder]:
                run.wait(lambda: run.clean(folder))
            logs = []
            for name in [python, rust]:
                lines = [line for line in run.logs(name) if line.get("requestId") == request_id]
                assert len(lines) == 1, lines
                logs.append({key: lines[0][key] for key in ["status", "stage", "errorClass"]})
            if logs[0] != logs[1]:
                differences.append("log")
            result = {"fixture": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "referenceStatus": expected["status"], "passed": not differences,
                      "differences": differences}
            if differences:
                result.update(python=expected, rust=actual, logs=logs)
                print("Container mismatch: " + path.name + " / " + ", ".join(differences), flush=True)
            results.append(result)
        for name in [python, rust]:
            command("docker", "kill", "--signal=TERM", name)
            run.wait(lambda: run.stopped(name), 30)
        return results
    finally:
        for path in generated:
            path.unlink(missing_ok=True)


def check_lifecycle(run):
    results = []
    # Drain active transfers, including a client that has already disconnected.
    for stage in ["download", "upload", "disconnect"]:
        print("Lifecycle: drain-" + stage, flush=True)
        run.control("/reset")
        name, folder = run.start()
        request_id = "drain-" + stage + "-0001"
        route = "/hold-put/" + request_id if stage == "upload" else "/hold/pulse-120-a4.wav"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(run.analyze, name, "pulse-120-a4.wav", request_id,
                                      target=stage == "upload", source_route="source" if stage == "upload" else "hold",
                                      target_route="hold-put", disconnect=stage == "disconnect")
            run.wait(lambda: route in run.stats()["entered"])
            started = time.monotonic()
            command("docker", "kill", "--signal=TERM", name)
            run.control("/release")
            response = pending.result()
            assert response.get("disconnected") or response["status"] == 200, response
        run.wait(lambda: run.stopped(name), 30)
        assert run.clean(folder)
        lines = [line for line in run.logs(name) if line.get("requestId") == request_id]
        assert len(lines) == 1 and lines[0]["status"] == 200, lines
        results.append({"case": "drain-" + stage, "passed": True, "seconds": time.monotonic() - started})

    print("Lifecycle: queue shutdown", flush=True)
    # Fill the active slot and queue. A third request proves overflow before
    # SIGTERM; then the queued request must be released without a download.
    run.control("/reset")
    name, folder = run.start(changes={"AUDIO_ANALYSIS_CONCURRENCY": "1", "AUDIO_ANALYSIS_QUEUE_LIMIT": "1"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        active = executor.submit(run.analyze, name, "pulse-120-a4.wav", "queue-drain-active", source_route="hold")
        run.wait(lambda: "/hold/pulse-120-a4.wav" in run.stats()["entered"])
        waiting = [executor.submit(run.analyze, name, "silence-8.wav", "queue-drain-%d" % i) for i in range(2)]
        run.wait(lambda: any(job.done() for job in waiting), 3)
        completed = [job for job in waiting if job.done()]
        assert len(completed) == 1 and completed[0].result()["status"] == 503
        command("docker", "kill", "--signal=TERM", name)
        run.control("/release")
        assert active.result()["status"] == 200
        for job in waiting:
            response = job.result()
            assert response["status"] == 503 and response["body"]["stage"] == "queue"
            assert response["headers"]["retry-after"] == "15"
    run.wait(lambda: run.stopped(name), 30)
    assert "/source/silence-8.wav" not in run.stats()["entered"]
    assert run.clean(folder)
    results.append({"case": "shutdown-releases-queued-work-before-download", "passed": True})

    print("Lifecycle: worker crash", flush=True)
    # A worker crash must retire its IPC stream and recover the next request.
    run.control("/reset")
    name, folder = run.start(changes={"AUDIO_ANALYSIS_CONCURRENCY": "1"})
    worker = command("docker", "exec", name, "cat", "/proc/1/task/1/children").stdout.strip().split()[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run.analyze, name, "pulse-120-a4.wav", "worker-crash-0001", source_route="hold")
        run.wait(lambda: "/hold/pulse-120-a4.wav" in run.stats()["entered"])
        command("docker", "exec", name, "sh", "-c", 'kill -KILL "$1"', "signal-worker", worker)
        response = pending.result()
    assert response["status"] == 500 and response["body"]["stage"] == "download", response
    command("docker", "exec", name, "sh", "-c", 'test ! -e /proc/"$1"', "check", worker)
    run.control("/release")
    run.wait(lambda: run.clean(folder))
    assert run.analyze(name, "pulse-120-a4.wav", "worker-crash-recovery")["status"] == 200
    command("docker", "kill", "--signal=TERM", name)
    run.wait(lambda: run.stopped(name), 30)
    assert run.clean(folder)
    results.append({"case": "worker-crash-retires-ipc-and-recovers", "passed": True})

    # A stalled external tool must die and be reaped while the service stays alive.
    for tool, stage in [("ffprobe", "probe"), ("ffmpeg", "encode")]:
        print("Lifecycle: deadline-" + stage, flush=True)
        run.control("/reset")
        name, folder = run.start(wrapper=tool, changes={"AUDIO_ANALYSIS_DEADLINE_SECONDS": "1", "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS": "0.2"})
        limits = checked_worker_limits(name)
        response = run.analyze(name, "pulse-120-a4.aif", "deadline-" + stage + "-0001", target=tool == "ffmpeg")
        assert response["status"] == 504 and response["body"]["stage"] == stage, response
        assert (folder / "command-entered").is_file()
        assert address_limit((folder / "command-limits").read_text()) in limits
        pid = (folder / "command-entered").read_text().strip()
        command("docker", "exec", name, "sh", "-c", 'test ! -e /proc/"$1"', "check", pid)
        run.wait(lambda: run.clean(folder))
        assert run.remote(name, path="/healthz", method="GET")["status"] == 200
        command("docker", "kill", "--signal=TERM", name)
        run.wait(lambda: run.stopped(name), 30)
        results.append({"case": "deadline-reaps-" + tool, "passed": True})

    print("Lifecycle: native deadline", flush=True)
    # Stop a native worker after it has used CPU with no media child. A deadline
    # must replace it before the next job, with no stale IPC reply or temp file.
    run.control("/reset")
    name, folder = run.start(changes={"AUDIO_ANALYSIS_CONCURRENCY": "1", "AUDIO_ANALYSIS_DEADLINE_SECONDS": "3", "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS": "0.2"})
    worker = command("docker", "exec", name, "cat", "/proc/1/task/1/children").stdout.strip().split()[0]
    def cpu_ticks():
        fields = command("docker", "exec", name, "cat", "/proc/" + worker + "/stat").stdout.split()
        return int(fields[13]) + int(fields[14])
    before = cpu_ticks()
    def native_work_started():
        children = command("docker", "exec", name, "cat", "/proc/" + worker + "/task/" + worker + "/children").stdout.strip()
        return cpu_ticks() - before >= 5 and not children
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run.analyze, name, "track-120.wav", "stopped-native-0001")
        run.wait(native_work_started, 3)
        command("docker", "exec", name, "sh", "-c", 'kill -STOP "$1"', "signal-worker", worker)
        response = pending.result()
    assert response["status"] == 504, response
    command("docker", "exec", name, "sh", "-c", 'test ! -e /proc/"$1"', "check", worker)
    run.wait(lambda: run.clean(folder))
    recovered = run.analyze(name, "pulse-120-a4.wav", "native-recovery-0001")
    assert recovered["status"] == 200, recovered
    command("docker", "kill", "--signal=TERM", name)
    run.wait(lambda: run.stopped(name), 30)
    results.append({"case": "native-deadline-retires-ipc-and-recovers", "passed": True})

    print("Lifecycle: native drain", flush=True)
    # SIGTERM while native computation is in flight must drain that admitted job.
    run.control("/reset")
    name, folder = run.start(changes={"AUDIO_ANALYSIS_CONCURRENCY": "1"})
    worker = command("docker", "exec", name, "cat", "/proc/1/task/1/children").stdout.strip().split()[0]
    before = cpu_ticks()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run.analyze, name, "track-120.wav", "drain-native-0001")
        run.wait(native_work_started, 10)
        command("docker", "exec", name, "sh", "-c", 'kill -STOP "$1"', "signal-worker", worker)
        command("docker", "kill", "--signal=TERM", name)
        command("docker", "exec", name, "sh", "-c", 'kill -CONT "$1"', "signal-worker", worker)
        response = pending.result()
    assert response["status"] == 200, response
    run.wait(lambda: run.stopped(name), 30)
    assert run.clean(folder)
    results.append({"case": "drain-native-analysis", "passed": True})

    print("Lifecycle: forced shutdown", flush=True)
    # Exercise the actual 25 s drain limit, not a test-only production setting.
    run.control("/reset")
    name, folder = run.start(wrapper="ffmpeg")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run.analyze, name, "pulse-120-a4.aif", "forced-shutdown-0001", target=True)
        run.wait(lambda: (folder / "command-entered").is_file(), 30)
        started = time.monotonic()
        command("docker", "kill", "--signal=TERM", name)
        response = pending.result()
        assert response["status"] == 503, response
        run.wait(lambda: run.stopped(name), 5)
    elapsed = time.monotonic() - started
    assert 24 <= elapsed < 30, elapsed
    assert run.clean(folder)
    results.append({"case": "forced-shutdown-reaps-encoder", "passed": True, "seconds": elapsed})
    return results


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "fixture":
        ThreadingHTTPServer(("0.0.0.0", 9000), Fixture).serve_forever()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "request":
        print(json.dumps(local_request(json.load(sys.stdin))))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--architecture", required=True, choices=["arm64", "amd64"])
    parser.add_argument("--scope", choices=["audio", "inputs", "memory", "lifecycle", "all"], default="all")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    corpus = extract_corpus()
    results = {"architecture": args.architecture, "scope": args.scope, "runnerSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    errors = {}
    with Run(args.image, args.python_image, corpus) as run:
        results.update(imageId=run.image, pythonImageId=run.oracle)
        checks = []
        if args.scope in {"all", "audio"}:
            checks += [("pythonReferenceAudio", lambda: check_audio(run, "amd64", reference=True)),
                       ("audio", lambda: check_audio(run, args.architecture)),
                       ("audioErrors", lambda: check_audio_errors(run))]
        if args.scope in {"all", "inputs"}:
            checks += [("inputRules", lambda: check_input_rules(run)),
                       ("extraContainers", lambda: check_extra_containers(run))]
        if args.scope in {"all", "lifecycle"}:
            checks += [("lifecycle", lambda: check_lifecycle(run))]
        if args.scope in {"all", "memory"}:
            checks += [("decodeMemory", lambda: check_decode_memory(run))]
        for name, check in checks:
            try:
                results[name] = check()
            except (AssertionError, subprocess.CalledProcessError, OSError) as error:
                errors[name] = type(error).__name__ + ": " + str(error)[:4000]
                print(name + " FAILED: " + errors[name], flush=True)
    results["errors"] = errors
    results["passed"] = not errors and all(
        row.get("passed", False) for name, _ in checks for row in results.get(name, []))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"passed": results["passed"], "audio": len(results.get("audio", [])), "pythonReferenceAudio": len(results.get("pythonReferenceAudio", [])), "audioErrors": len(results.get("audioErrors", [])), "inputRules": len(results.get("inputRules", [])), "extraContainers": len(results.get("extraContainers", [])), "lifecycle": len(results.get("lifecycle", []))}))
    if not results["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
