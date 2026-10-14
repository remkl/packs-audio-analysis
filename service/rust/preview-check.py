#!/usr/bin/env python3
"""Strict audio and bounded load checks on an explicitly selected protected preview.

Requires standard-library Python only; --expanded-python-image also needs Docker.
Credentials come from environment or a mode-0600 JSON file, never CLI values.
Only verified synthetic fixtures are uploaded. Each run owns a random R2 prefix.
No deployment, project, environment, alias or Convex setting is changed.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import tarfile
import tempfile
import threading
import time
from urllib.parse import quote, urlencode, urlsplit
import uuid
import wave

ROOT = Path(__file__).resolve().parent
EXACT = ("bpm", "key_standard", "key_camelot", "duration", "peaks", "preview", "playback")
CONFIDENCE = ("bpm_confidence", "key_confidence")
SECRET_NAMES = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "AUDIO_ANALYSIS_BASIC_AUTH_USERNAME", "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD",
                "VERCEL_AUTOMATION_BYPASS_SECRET", "PAX_PREVIEW_COOKIE")
DEFAULT_WORKLOAD = "loop-30.wav,pulse-120-a4.aif,pulse-120.mp3,stereo-48k.wav"


class CheckFailure(RuntimeError):
    """Only checker-owned, credential-free messages may use this exception."""


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    require(result.returncode == 0, "Local oracle command failed; output suppressed")
    return result.stdout


def credentials(path):
    values = {key: os.environ.get(key, "") for key in SECRET_NAMES}
    if path:
        require(path.stat().st_mode & 0o077 == 0, "Credential file must have mode 0600")
        data = json.loads(path.read_text())
        require(isinstance(data, dict), "Credential file must contain an object")
        values.update({key: data[key] for key in SECRET_NAMES if key in data})
    values["AUDIO_ANALYSIS_BASIC_AUTH_USERNAME"] = values["AUDIO_ANALYSIS_BASIC_AUTH_USERNAME"] or "analysis"
    require(all(isinstance(value, str) for value in values.values()), "Credential values must be strings")
    return values


class HTTPS:
    """One reusable connection per thread. No automatic retries or redirects."""
    def __init__(self, origin, timeout=130):
        parsed = urlsplit(origin)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.query and not parsed.fragment and parsed.path in {"", "/"},
                "An HTTPS origin is required")
        self.host, self.port = parsed.hostname, parsed.port or 443
        self.timeout = timeout
        self.local = threading.local()
        self.connections = []
        self.lock = threading.Lock()

    def request(self, method, path, body=None, headers=None, max_bytes=1024 * 1024):
        connection = getattr(self.local, "connection", None)
        if connection is None:
            connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout)
            self.local.connection = connection
            with self.lock:
                self.connections.append(connection)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read(max_bytes + 1)
            require(len(data) <= max_bytes, "Remote response exceeds the checker's byte limit")
            return response.status, {key.lower(): value for key, value in response.getheaders()}, data
        except Exception:
            connection.close()
            self.local.connection = None
            raise

    def close(self):
        for connection in self.connections:
            connection.close()


class R2:
    def __init__(self, config):
        self.http = HTTPS(config["R2_ENDPOINT"])
        require(self.http.host.endswith(".r2.cloudflarestorage.com"), "Expected an R2 S3 endpoint")
        self.bucket = config["R2_BUCKET"]
        self.access = config["R2_ACCESS_KEY_ID"]
        self.secret = config["R2_SECRET_ACCESS_KEY"]
        self.prefix = "analysis-preview-check/" + uuid.uuid4().hex + "/"
        self.keys = set()
        self.lock = threading.Lock()

    def key(self, suffix):
        require(not suffix.startswith("/") and ".." not in suffix.split("/"), "Invalid test object name")
        key = self.prefix + suffix
        with self.lock:
            self.keys.add(key)
        return key

    def path(self, key):
        require(key.startswith(self.prefix), "Refusing an object outside this test run")
        return "/" + quote(self.bucket, safe="") + "/" + quote(key, safe="/~")

    def signing(self, stamp):
        day = stamp[:8]
        key = ("AWS4" + self.secret).encode()
        for part in (day, "auto", "s3", "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        return day + "/auto/s3/aws4_request", key

    def signed_headers(self, method, path, body):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        scope, key = self.signing(stamp)
        headers = {"host": self.http.host, "x-amz-content-sha256": digest(body), "x-amz-date": stamp}
        names = ";".join(sorted(headers))
        canonical = method + "\n" + path + "\n\n" + "".join(name + ":" + headers[name] + "\n" for name in sorted(headers)) + "\n" + names + "\n" + digest(body)
        signature = hmac.new(key, ("AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + digest(canonical.encode())).encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = "AWS4-HMAC-SHA256 Credential=" + self.access + "/" + scope + ", SignedHeaders=" + names + ", Signature=" + signature
        return headers

    def request(self, method, key, body=b"", content_type=None):
        path = self.path(key)
        headers = self.signed_headers(method, path, body)
        if content_type:
            headers["Content-Type"] = content_type
        return self.http.request(method, path, body=body, headers=headers, max_bytes=16 * 1024 * 1024)

    def presign(self, method, key):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        scope, signing_key = self.signing(stamp)
        parameters = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": self.access + "/" + scope,
                      "X-Amz-Date": stamp, "X-Amz-Expires": "900", "X-Amz-SignedHeaders": "host"}
        query = urlencode(sorted(parameters.items()), quote_via=quote, safe="~")
        path = self.path(key)
        canonical = method + "\n" + path + "\n" + query + "\nhost:" + self.http.host + "\n\nhost\nUNSIGNED-PAYLOAD"
        signature = hmac.new(signing_key, ("AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + digest(canonical.encode())).encode(), hashlib.sha256).hexdigest()
        return "https://" + self.http.host + path + "?" + query + "&X-Amz-Signature=" + signature

    def cleanup(self):
        failed = []
        for key in sorted(self.keys):
            try:
                status, _, _ = self.request("DELETE", key)
                require(status in {200, 204}, "Object deletion failed")
                status, _, _ = self.request("HEAD", key)
                require(status == 404, "Deleted object still exists")
            except Exception:
                failed.append(key)
        self.http.close()
        return {"attempted": len(self.keys), "failed": len(failed), "remainingKeys": failed, "passed": not failed}


def extract_corpus(folder):
    manifest = json.loads((ROOT / "compat-data.json").read_text())
    archive_path = ROOT / "compat-data.tar.xz"
    require(digest(archive_path.read_bytes()) == manifest["archiveSha256"], "Frozen corpus archive hash changed")
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            require(member.isfile() and not relative.is_absolute() and ".." not in relative.parts, "Invalid corpus member")
            data = archive.extractfile(member).read()
            require(digest(data) == manifest["files"][member.name], "Frozen corpus member hash changed")
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    expected = json.loads((folder / "goldens/amd64-wheel-golden.json").read_text())["outputs"]
    require(len(expected) == 14, "Frozen golden count changed")
    cases = [{"name": name, "path": folder / "fixtures" / name, "status": 200, "expected": body,
              "target": body["playback"] is not None} for name, body in expected.items()]
    for name, status, stage, detail in [("corrupt.wav", 422, "probe", "Audio could not be decoded"),
                                       ("empty.wav", 422, "decode", "Audio could not be decoded"),
                                       ("overlong.wav", 413, "probe", "Audio is longer than 5 minutes")]:
        cases.append({"name": name, "path": folder / "fixtures" / name, "status": status,
                      "expected": {"stage": stage, "detail": detail}, "target": True})
    return cases, manifest["archiveSha256"]


def expanded_cases(image, corpus, reference_source):
    """Use the current Python HTTP service as the additional codec oracle locally."""
    immutable = command("docker", "image", "inspect", image, "--format", "{{.Id}}").strip()
    names = ["main.py", "analyzer.py", "models.py", "codec_compatibility.py", "requirements.lock"]
    probe = "import hashlib,json;from pathlib import Path;print(json.dumps({n:hashlib.sha256(Path('/app',n).read_bytes()).hexdigest() for n in " + repr(names) + "}))"
    observed = json.loads(command("docker", "run", "--rm", "--network", "none", "--entrypoint", "python3", immutable, "-c", probe))
    require(observed == {name: digest((reference_source / name).read_bytes()) for name in names}, "codec_oracle_source_hash_mismatch")
    fixtures = corpus / "fixtures"
    os.chmod(corpus, 0o755)
    os.chmod(fixtures, 0o777)
    paths = []
    for extension, codec, status in [("flac", "flac", 200), ("ogg", "libvorbis", 200), ("m4a", "aac", 200),
                                     ("webm", "libopus", 422), ("webm", "libvorbis", 200), ("ogg", "libopus", 422),
                                     ("wav", "adpcm_ima_wav", 422), ("wav", "adpcm_ms", 422)]:
        path = fixtures / ("container-" + codec + "." + extension)
        command("docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64", "-v", str(fixtures) + ":/fixtures",
                "--entrypoint", "ffmpeg", immutable, "-v", "error", "-i", "/fixtures/pulse-120-a4.wav", "-t", "2",
                "-map_metadata", "-1", "-c:a", codec, "/fixtures/" + path.name)
        paths.append((path, status))
    for channels, rate in [(1, 44100), (1, 22050), (2, 48000)]:
        path = fixtures / ("container-zork-%d-%d.wav" % (channels, rate))
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(1)
            wav.setframerate(rate)
            length = channels * rate * 2
            wav.writeframes((bytes(range(256)) * ((length + 255) // 256))[:length])
        data = bytearray(path.read_bytes())
        require(data[12:16] == b"fmt " and data[20:22] == b"\x01\x00", "Unexpected generated WAV header")
        data[20:22] = b"\x11\x00"
        path.write_bytes(data)
        paths.append((path, 422))
    spec = importlib.util.spec_from_file_location("preview_runtime", ROOT / "runtime-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = []
    with module.Run(immutable, immutable, corpus) as run:
        reference, _ = run.start(image=immutable)
        for index, (path, status) in enumerate(paths):
            response = run.analyze(reference, path.name, "preview-codec-oracle-%d" % index, target=True)
            require(response["status"] == status, "Local codec oracle status changed for " + path.name)
            body = response["body"]
            if status != 200:
                require(body["stage"] == "decode", "Local codec rejection stage changed")
                body = {key: value for key, value in body.items() if key != "requestId"}
            cases.append({"name": path.name, "path": path, "status": status, "expected": body, "target": True})
    return cases, {"imageId": immutable, "sourceSha256": observed}


def extra_cases(path):
    if not path:
        return []
    manifest = json.loads(path.read_text())
    require(manifest.get("synthetic") is True, "Extra case manifest must explicitly identify synthetic data")
    rows = []
    for row in manifest["cases"]:
        fixture = (path.parent / row["path"]).resolve()
        require(digest(fixture.read_bytes()) == row["sha256"], "Extra fixture hash mismatch")
        require(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", row["name"]), "Invalid extra fixture name")
        rows.append({**row, "path": fixture, "target": row.get("target", True)})
    return rows


def mismatches(actual, expected):
    if not isinstance(actual, dict) or set(actual) != set(EXACT + CONFIDENCE):
        return ["responseFields"]
    differences = [key for key in EXACT if actual[key] != expected[key]]
    for key in CONFIDENCE:
        value = actual[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not math.isclose(value, expected[key], rel_tol=0, abs_tol=1e-6):
            differences.append(key)
    return differences


def summary(rows, elapsed):
    successful = [row["latencyMs"] for row in rows if row["status"] == 200]
    ordered = sorted(successful)
    percentile = lambda p: ordered[max(0, math.ceil(len(ordered) * p) - 1)] if ordered else None
    counts = {str(status): sum(row["status"] == status for row in rows) for status in sorted({row["status"] for row in rows})}
    return {"wallSeconds": elapsed, "completed": len(rows), "statusCounts": counts, "successful": len(successful),
            "successfulRequestsPerSecond": len(successful) / elapsed, "allRequestsPerSecond": len(rows) / elapsed,
            "latencyMs": {"median": statistics.median(successful) if successful else None, "p95": percentile(.95), "max": max(successful) if successful else None}}


def memory_summary(path, started_ms, ended_ms):
    if not path:
        return {"available": False, "reason": "No hosted resource samples supplied"}
    instances = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            # Vercel exports can wrap the original JSON log in a message field.
            if isinstance(row.get("message"), str):
                row = json.loads(row["message"])
            if row.get("event") != "resource-sample":
                continue
            sampled_ms = row["sampledAtUnixMs"]
            window_ms = row.get("windowStartedAtUnixMs", sampled_ms)
            current_in_range = started_ms <= sampled_ms <= ended_ms
            peak_overlaps = window_ms <= ended_ms and sampled_ms >= started_ms and window_ms <= sampled_ms
            if not current_in_range and not peak_overlaps:
                continue
            item = instances.setdefault(row["instanceId"], {"samples": 0, "currentSamples": 0,
                                                           "peakWindowSamples": 0, "boundaryOverlapPeakSamples": 0})
            item["samples"] += 1
            item["currentSamples"] += int(current_in_range)
            item["peakWindowSamples"] += int(peak_overlaps)
            item["boundaryOverlapPeakSamples"] += int(peak_overlaps and (window_ms < started_ms or sampled_ms > ended_ms))
            keys = ["processTreeRssBytes", "cgroupMemoryCurrentBytes", "cgroupMemoryPeakBytes"] if current_in_range else []
            if peak_overlaps:
                keys.append("processTreePeakRssBytes")
            for key in keys:
                value = row.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    item[key] = max(item.get(key, 0), value)
        except (KeyError, TypeError, ValueError):
            continue
    return {"available": bool(instances), "instances": instances,
            "notes": ["Summed process RSS double-counts shared pages; it is not container memory.",
                      "Current RSS and cgroup samples use the exact emission timestamp within the selected interval.",
                      "RSS peaks include every sample window that overlaps the interval; boundary windows can include activity outside the batch.",
                      "Logs without a window start use the emission timestamp only for peak selection.",
                      "cgroupMemoryPeakBytes is a container-lifetime high-water mark, not a per-batch peak.",
                      "Sampling can miss short peaks. Missing cgroup fields do not mean zero memory."]}


def run(args, config, cases, report):
    report["phase"] = "preview-protection"
    parsed = urlsplit(args.deployment_url)
    require(parsed.hostname == args.allowed_preview_host and parsed.hostname.endswith(".vercel.app"), "Preview host must match the explicit allowed host")
    require(parsed.hostname not in {"pax-audio-analysis-r2.vercel.app", "pax-audio-analysis.vercel.app"}, "Refusing the shared live service alias")
    require(args.deployment_id.startswith("dpl_"), "An immutable preview deployment ID is required")
    for key in SECRET_NAMES[:4] + ("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD",):
        require(bool(config[key]), "Missing credential name: " + key)
    require(config["VERCEL_AUTOMATION_BYPASS_SECRET"] or config["PAX_PREVIEW_COOKIE"], "Missing reusable preview protection credential")
    api = HTTPS(args.deployment_url)
    headers = {"Content-Type": "application/json"}
    if config["VERCEL_AUTOMATION_BYPASS_SECRET"]:
        headers["x-vercel-protection-bypass"] = config["VERCEL_AUTOMATION_BYPASS_SECRET"]
    if config["PAX_PREVIEW_COOKIE"]:
        headers["Cookie"] = config["PAX_PREVIEW_COOKIE"]
    auth = config["AUDIO_ANALYSIS_BASIC_AUTH_USERNAME"] + ":" + config["AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD"]
    headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    anonymous, _, _ = api.request("GET", "/healthz")
    require(anonymous in {301, 302, 303, 307, 308, 401, 403}, "preview_anonymous_access_not_blocked_http_%d" % anonymous)
    status, _, body = api.request("GET", "/healthz", headers=headers)
    require(status == 200, "protected_preview_health_http_%d" % status)
    require(json.loads(body).get("status") == "ok", "protected_preview_health_body_invalid")
    report["health"] = json.loads(body)
    if args.expected_release:
        require(report["health"].get("release") == args.expected_release, "Preview release differs from the expected source")
    report["protectionChecked"] = True
    storage = R2(config)
    report["objectPrefix"] = storage.prefix
    sources = {}
    cleanup_after = [0.0]
    lock = threading.Lock()
    try:
        report["phase"] = "upload-synthetic-fixtures"
        for case in cases:
            key = storage.key("sources/" + case["name"])
            status, _, _ = storage.request("PUT", key, case["path"].read_bytes(), "application/octet-stream")
            require(status in {200, 201}, "synthetic_fixture_upload_http_%d" % status)
            sources[case["name"]] = key
        report["uploadedFixtureCount"] = len(cases)
        print("Synthetic fixtures uploaded; checking exact audio", flush=True)

        def prepare(case, ordinal):
            request_id = "pv-" + report["runId"][:12] + "-%05d" % ordinal
            target = storage.key("playback/" + request_id + ".mp3") if case["target"] else None
            payload = {"sourceUrl": storage.presign("GET", sources[case["name"]])}
            if target:
                payload["targetUrl"] = storage.presign("PUT", target)
            return case, request_id, target, json.dumps(payload).encode()

        def request(job):
            case, request_id, target, payload = job
            started = time.monotonic()
            row = {"fixture": case["name"], "requestId": request_id, "status": 0}
            try:
                status, returned, raw = api.request("POST", "/api/analyze-r2", body=payload,
                                                   headers={**headers, "x-pax-request-id": request_id})
                row.update(status=status, latencyMs=(time.monotonic() - started) * 1000,
                           requestIdMatched=returned.get("x-pax-request-id") == request_id,
                           retryAfter=returned.get("retry-after"))
                actual = json.loads(raw)
                # Never persist untrusted response text; only expected fields are compared.
                return row, actual, target, case
            except Exception as error:
                with lock:
                    cleanup_after[0] = max(cleanup_after[0], started + 120)
                row.update(latencyMs=(time.monotonic() - started) * 1000, errorClass=type(error).__name__)
                return row, None, target, case

        def verify(result, allow_overload=False):
            row, actual, target, case = result
            differences = []
            if not row.get("requestIdMatched"):
                differences.append("requestIdHeader")
            if allow_overload and row["status"] == 503:
                if row.get("retryAfter") != "15" or not isinstance(actual, dict) or actual.get("stage") != "queue" or actual.get("requestId") != row["requestId"]:
                    differences.append("overloadContract")
            elif row["status"] != case["status"]:
                differences.append("status")
            elif row["status"] == 200:
                differences.extend(mismatches(actual, case["expected"]))
            elif actual != {**case["expected"], "requestId": row["requestId"]}:
                differences.append("errorBody")
            if target:
                status, returned, data = storage.request("GET", target)
                expected_playback = case["expected"].get("playback") if row["status"] == 200 else None
                if expected_playback is None:
                    if status != 404:
                        differences.append("unexpectedPlaybackUpload")
                else:
                    observed = {"sizeBytes": len(data), "sha256": digest(data)}
                    row["uploadedPlayback"] = observed
                    if status != 200 or observed != expected_playback:
                        differences.append("uploadedPlaybackBytes")
                    if returned.get("content-type") != "audio/mpeg":
                        differences.append("uploadedPlaybackType")
            row.update(passed=not differences, differences=differences)
            return row

        report["phase"] = "strict-audio"
        report["strict"] = []
        for index, case in enumerate(cases):
            report["lastCase"] = case["name"]
            report["strict"].append(verify(request(prepare(case, index))))
        report["firstAnalysisLatencyMs"] = report["strict"][0]["latencyMs"]
        report["firstAnalysisNote"] = "Observed after health requests; cold state was not controlled."
        require(all(row["passed"] for row in report["strict"]), "Strict hosted audio comparison failed; load check skipped")
        selected = {case["name"]: case for case in cases if case["status"] == 200}
        workload = args.benchmark_fixtures.split(",")
        require(all(name in selected for name in workload), "Benchmark fixture is missing or has an error expectation")
        ordinal = len(cases)
        report["batches"] = []
        report["phase"] = "throughput"
        for concurrency in args.levels:
            jobs = [prepare(selected[workload[index % len(workload)]], ordinal + index) for index in range(args.requests_per_level)]
            ordinal += len(jobs)
            print("Benchmark concurrency " + str(concurrency), flush=True)
            started_ms = time.time() * 1000
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                results = list(executor.map(request, jobs))
            elapsed = time.monotonic() - started
            ended_ms = time.time() * 1000
            # R2 downloads and output comparisons happen outside the timed window.
            rows = [verify(result, allow_overload=True) for result in results]
            report["batches"].append({"concurrency": concurrency, "startedAtUnixMs": started_ms,
                                      "endedAtUnixMs": ended_ms, "requests": rows, **summary(rows, elapsed)})
        report["strictPassed"] = all(row["passed"] for row in report["strict"]) and all(row["passed"] for batch in report["batches"] for row in batch["requests"])
        report["phase"] = "complete"
    finally:
        while time.monotonic() < cleanup_after[0]:
            print("Waiting for admitted requests before object cleanup", flush=True)
            time.sleep(min(10, cleanup_after[0] - time.monotonic()))
        report["cleanup"] = storage.cleanup()
        api.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deployment-url")
    parser.add_argument("--allowed-preview-host", help="Explicit immutable preview hostname verified by the operator")
    parser.add_argument("--deployment-id")
    parser.add_argument("--expected-release")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expanded-python-image", help="Optional local pinned Python image for 11 extra codec controls")
    parser.add_argument("--python-reference-source", type=Path, default=ROOT.parent,
                        help="Source directory verified against the Python image; default is the current service")
    parser.add_argument("--extra-cases", type=Path, help="JSON: synthetic=true, cases=[name,path,sha256,status,expected,target]")
    parser.add_argument("--levels", default="1,2,4")
    parser.add_argument("--requests-per-level", type=int, default=12)
    parser.add_argument("--benchmark-fixtures", default=DEFAULT_WORKLOAD)
    parser.add_argument("--metrics-log", type=Path)
    parser.add_argument("--summarize-only", action="store_true", help="Attach memory samples to an existing output, with no network calls")
    parser.add_argument("--check-config", action="store_true", help="Print available credential names only; no network calls")
    args = parser.parse_args()
    if args.summarize_only:
        report = json.loads(args.output.read_text())
        report["memory"] = memory_summary(args.metrics_log, report["startedAtUnixMs"], report["endedAtUnixMs"])
        for batch in report.get("batches", []):
            batch["memory"] = memory_summary(args.metrics_log, batch["startedAtUnixMs"], batch["endedAtUnixMs"])
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return
    config = credentials(args.credentials)
    if args.check_config:
        print(json.dumps({"availableCredentialNames": sorted(key for key, value in config.items() if value)}))
        return
    require(args.deployment_url and args.allowed_preview_host and args.deployment_id, "Preview URL, allowed hostname and deployment ID are required")
    args.levels = [int(value) for value in args.levels.split(",")]
    require(args.levels and all(1 <= level <= 8 for level in args.levels), "Concurrency must be between 1 and 8")
    require(1 <= args.requests_per_level <= 120, "Requests per concurrency must be between 1 and 120")
    report = {"runId": uuid.uuid4().hex, "deploymentId": args.deployment_id, "deploymentHost": args.allowed_preview_host,
              "startedAtUnixMs": time.time() * 1000, "strictPassed": False, "runnerSha256": digest(Path(__file__).read_bytes()),
              "architecture": "amd64", "confidenceAbsoluteTolerance": 1e-6,
              "benchmarkWorkload": args.benchmark_fixtures.split(","),
              "measurement": "Warm mixed-workload closed-loop HTTPS batches; client/WAN latency included; no hidden retries. Playback downloads verified after timing.",
              "deployedServiceChanged": False}
    try:
        with tempfile.TemporaryDirectory(prefix="pax-preview-corpus-") as directory:
            report["phase"] = "prepare-fixtures"
            corpus = Path(directory)
            cases, report["corpusArchiveSha256"] = extract_corpus(corpus)
            if args.expanded_python_image:
                expanded, report["codecOracle"] = expanded_cases(args.expanded_python_image, corpus, args.python_reference_source)
                cases.extend(expanded)
            cases.extend(extra_cases(args.extra_cases))
            require(len({case["name"] for case in cases}) == len(cases), "Duplicate fixture names")
            report["fixtures"] = [{"name": case["name"], "sha256": digest(case["path"].read_bytes()), "expectedStatus": case["status"]} for case in cases]
            run(args, config, cases, report)
    except Exception as error:
        # Exceptions may contain request URLs. Persist only the class.
        report["errorClass"] = type(error).__name__
        if isinstance(error, CheckFailure):
            report["errorCode"] = str(error)
        print("Preview check failed (" + type(error).__name__ + "); details suppressed to protect credentials", flush=True)
    finally:
        report["endedAtUnixMs"] = time.time() * 1000
        report["memory"] = memory_summary(args.metrics_log, report["startedAtUnixMs"], report["endedAtUnixMs"])
        report["passed"] = report["strictPassed"] and report.get("cleanup", {}).get("passed", False) and "errorClass" not in report
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report.get(key) for key in ("passed", "strictPassed", "deploymentId", "cleanup", "errorClass", "errorCode")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Preview checker setup failed (" + type(error).__name__ + "); credential values suppressed")
        if isinstance(error, CheckFailure):
            print(str(error))
        raise SystemExit(1) from None
