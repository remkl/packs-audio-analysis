#!/usr/bin/env python3
"""Pinned-Python oracle for the ordinary worker.rs transport boundary tests.

Run in the current Python service image with this file mounted read-only. All
traffic stays on loopback; there are no service credentials or external URLs.
The matching Rust tests run normally in `cargo test --release`, with no hooks.
"""
import asyncio
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import zlib

os.environ.setdefault("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", "local-transport-oracle")
os.environ.setdefault("AUDIO_ANALYSIS_ALLOWED_HOSTS", "127.0.0.1")
import httpx
import main


def head(connection):
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        byte = connection.recv(1)
        if not byte:
            raise RuntimeError("fixture request ended early")
        data += byte
    return data.decode("ascii")


def server(handler):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    url = "http://127.0.0.1:%d/audio.wav" % listener.getsockname()[1]
    failures = []

    def run():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                handler(connection)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Timed-out clients must close their socket.
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            listener.close()

    thread = threading.Thread(target=run)
    thread.start()
    return url, thread, failures


def response(connection, encoding, body, declared=None):
    length = len(body) if declared is None else declared
    connection.sendall(("HTTP/1.1 200 OK\r\nContent-Length: %d\r\nContent-Encoding: %s\r\nConnection: close\r\n\r\n" % (length, encoding)).encode() + body)


async def check_download(name, handler, expected, limit=1000, timeout=2, data=None):
    url, thread, failures = server(handler)
    main.MAX_DOWNLOAD_BYTES = limit
    main.DOWNLOAD_TIMEOUT_SECONDS = timeout
    actual = None
    with tempfile.TemporaryDirectory() as directory:
        try:
            path = await main.download_source(url, Path(directory))
            if data is not None:
                assert path.read_bytes() == data
        except main.ServiceError as error:
            actual = (error.status_code, type(error.__cause__ or error).__name__)
    thread.join(5)
    assert not thread.is_alive() and not failures, (name, failures)
    assert actual == expected, (name, actual, expected)
    return {"case": name, "statusAndErrorClass": actual, "passed": True}


async def check_upload(mode):
    def handle(connection):
        headers = head(connection)
        length = int(next(line.split(":", 1)[1] for line in headers.splitlines() if line.lower().startswith("content-length:")))
        if mode == "write-stall":
            time.sleep(.9)
            return
        count = 0
        while count < length:
            chunk = connection.recv(min(262144, length - count))
            if not chunk:
                return
            count += len(chunk)
            if mode == "steady":
                time.sleep(.012)
        if mode == "read-stall":
            time.sleep(.9)
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

    url, thread, failures = server(handle)
    async def chunks():
        for _ in range(16):
            yield bytes(1024 * 1024)
    actual = None
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(.35), follow_redirects=False) as client:
            await client.put(url, headers={"Content-Type": "audio/mpeg", "Content-Length": str(16 * 1024 * 1024)}, content=chunks())
    except httpx.HTTPError as error:
        actual = type(error).__name__
    elapsed = time.monotonic() - started
    thread.join(5)
    assert not thread.is_alive() and not failures, (mode, failures)
    expected = {"steady": None, "write-stall": "WriteTimeout", "read-stall": "ReadTimeout"}[mode]
    assert actual == expected, (mode, actual, expected)
    if mode == "steady":
        assert elapsed > .35
    return {"case": "upload-" + mode, "errorClass": actual, "passed": True}


async def run():
    rows = []
    data = bytes([42]) * 16384
    raw = zlib.compressobj(wbits=-15)
    encodings = {"gzip": gzip.compress(data), "deflate": zlib.compress(data), "raw-deflate": raw.compress(data) + raw.flush(), "gzip, deflate": zlib.compress(gzip.compress(data))}
    for encoding, wire in encodings.items():
        label = "deflate" if encoding == "raw-deflate" else encoding
        for limit in [4, 4096, 32768]:
            def handler(connection, label=label, wire=wire):
                head(connection)
                response(connection, label, wire)
            rows.append(await check_download("compressed-%s-%d" % (encoding, limit), handler, (413, "ServiceError") if limit < len(data) else None, limit=limit, data=data))
    for encoding, wire, declared, expected in [("gzip", b"not gzip", 8, "DecodingError"), ("identity", b"short", 100, "RemoteProtocolError")]:
        def handler(connection, encoding=encoding, wire=wire, declared=declared):
            head(connection)
            response(connection, encoding, wire, declared)
        rows.append(await check_download("broken-" + encoding, handler, (502, expected)))
    for stalled in [False, True]:
        def handler(connection, stalled=stalled):
            head(connection)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\nConnection: close\r\n\r\n")
            for _ in range(20):
                connection.sendall(b"x")
                time.sleep(.4 if stalled else .025)
        rows.append(await check_download("download-" + ("stalled" if stalled else "steady"), handler, (502, "ReadTimeout") if stalled else None, timeout=.15, data=None if stalled else b"x" * 20))
    for mode in ["steady", "write-stall", "read-stall"]:
        rows.append(await check_upload(mode))
    print(json.dumps({"passed": True, "scope": "loopback transport only; no audio DSP or full API timing", "caseCount": len(rows), "httpx": importlib.metadata.version("httpx"), "scriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "cases": rows}, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(run())
