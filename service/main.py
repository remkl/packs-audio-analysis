import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, NoReturn, Optional, Tuple
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException

from codec_compatibility import COMPATIBLE_AUDIO_CODECS

# models.py does not import Essentia, so the analyzer still loads lazily.
from models import (
    AnalysisRejected, AnalysisResult, AnalyzeR2Request, AudioTooLong,
    DecodedAudioTooLarge, UndecodableAudio,
)


AUTH_USERNAME = os.getenv("AUDIO_ANALYSIS_BASIC_AUTH_USERNAME", "analysis")
AUTH_PASSWORD = os.getenv("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", "")
ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.getenv("AUDIO_ANALYSIS_ALLOWED_HOSTS", "").split(",")
    if host.strip()
}
ALLOW_HTTP = os.getenv("AUDIO_ANALYSIS_ALLOW_HTTP", "false").lower() == "true"
MAX_DOWNLOAD_BYTES = int(os.getenv("AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES", "157286400"))
ANALYSIS_CONCURRENCY = int(os.getenv("AUDIO_ANALYSIS_CONCURRENCY", "2"))
DOWNLOAD_TIMEOUT_SECONDS = float(
    os.getenv("AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS", "90")
)
# One deadline covers the queue, download, probe, analysis, encode and upload.
# It stays below the 115 s Convex client timeout.
DEADLINE_SECONDS = float(os.getenv("AUDIO_ANALYSIS_DEADLINE_SECONDS", "100"))
QUEUE_LIMIT = int(os.getenv("AUDIO_ANALYSIS_QUEUE_LIMIT", str(2 * ANALYSIS_CONCURRENCY)))
QUEUE_WAIT_SECONDS = float(os.getenv("AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS", "20"))
# Convex MAX_ANALYZED_AUDIO_MINUTES must match this value.
MAX_DURATION_SECONDS = float(os.getenv("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", "300"))
UPLOAD_CHUNK_BYTES = 1024 * 1024
# PAX accepts these audio containers. Exclude playlists and file-reference demuxers.
MEDIA_FORMATS = "wav,aiff,mp3,flac,ogg,mov,matroska,webm"
RETRY_AFTER_SECONDS = 15
# Shared with the Rust service. Count the root JSON object as one container.
MAX_REQUEST_BODY_BYTES = 64 * 1024
JSON_MAX_CONTAINERS = 64

if not AUTH_PASSWORD:
    raise RuntimeError("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD must be set")
if not ALLOWED_HOSTS:
    raise RuntimeError("AUDIO_ANALYSIS_ALLOWED_HOSTS must be set")
if ANALYSIS_CONCURRENCY < 1 or ANALYSIS_CONCURRENCY > 4:
    raise RuntimeError("AUDIO_ANALYSIS_CONCURRENCY must be between 1 and 4")
if not 0 < DEADLINE_SECONDS <= 110:
    raise RuntimeError("AUDIO_ANALYSIS_DEADLINE_SECONDS must be above 0 and at most 110")
if QUEUE_LIMIT < 0:
    raise RuntimeError("AUDIO_ANALYSIS_QUEUE_LIMIT must be 0 or more")
if not 0 < QUEUE_WAIT_SECONDS < DEADLINE_SECONDS:
    raise RuntimeError("AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS must be above 0 and below the deadline")
if not math.isfinite(MAX_DURATION_SECONDS) or MAX_DURATION_SECONDS <= 0:
    raise RuntimeError("AUDIO_ANALYSIS_MAX_DURATION_SECONDS must be above 0")

DATA_DIR = Path(os.getenv("AUDIO_ANALYSIS_DATA_DIR", tempfile.gettempdir())) / "audio-analysis"
TEMP_DIR = DATA_DIR / "tmp"

TEMP_DIR.mkdir(parents=True, exist_ok=True)

PAX_R2_HOSTNAME = "0f254469eceb1b88f5b5ff50405617cd" + ".r2." + "cloudflare" + "storage.com"
REQUEST_ID_HEADER = "x-pax-request-id"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LOGGED_ROUTES = {"/healthz", "/api/analyze-r2"}
# Decoders get PATH only, never the service credentials.
SUBPROCESS_ENV = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}

security = HTTPBasic(auto_error=False)
analyzer = None

app = FastAPI(
    title="Audio Analysis Service",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class ServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        stage: str,
        detail: str,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.stage = stage
        self.detail = detail
        self.retry_after = retry_after


def set_stage(request: Request, stage: str) -> None:
    request.state.stage = stage


def error_response(
    request: Request,
    status_code: int,
    stage: str,
    detail: str,
    error_class: str,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    request.state.stage = stage
    request.state.error_class = request.state.error_class or error_class
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "stage": stage, "requestId": request.state.request_id},
        headers=headers,
    )


@app.middleware("http")
async def request_log(request: Request, call_next):
    header = request.headers.get(REQUEST_ID_HEADER, "")
    request.state.request_id = header if REQUEST_ID_PATTERN.match(header) else uuid.uuid4().hex
    request.state.stage = "request"
    request.state.error_class = None
    started = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response
    except Exception as exc:
        request.state.error_class = request.state.error_class or type(exc).__name__
        raise
    finally:
        # One line for each request. Never write a URL, a file path, a header
        # value other than the request id, or subprocess output.
        path = request.url.path
        line = {
            "service": "audio-analysis",
            "route": path if path in LOGGED_ROUTES else "other",
            "method": request.method,
            "status": status_code,
            "durationMs": round((time.monotonic() - started) * 1000),
            "stage": request.state.stage,
            "errorClass": request.state.error_class,
            "requestId": request.state.request_id,
        }
        sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        sys.stdout.flush()


@app.exception_handler(ServiceError)
async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
    headers = None if exc.retry_after is None else {"Retry-After": str(exc.retry_after)}
    # The log names the original exception, for example UndecodableAudio.
    error_class = type(exc.__cause__ or exc).__name__
    return error_response(request, exc.status_code, exc.stage, exc.detail, error_class, headers)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return error_response(
        request,
        exc.status_code,
        request.state.stage,
        str(exc.detail),
        type(exc).__name__,
        getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    # The default body echoes the input, and the input holds signed URLs.
    return error_response(request, 422, "validate", "Invalid request body", type(exc).__name__)


def get_analyzer():
    global analyzer
    if analyzer is None:
        from analyzer import AudioAnalyzer

        analyzer = AudioAnalyzer()
    return analyzer


def require_basic_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    if credentials is None:
        raise_auth_error()

    username_matches = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    password_matches = secrets.compare_digest(credentials.password, AUTH_PASSWORD)

    if not (username_matches and password_matches):
        raise_auth_error()


def raise_auth_error() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid audio analysis credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


def check_json_depth(text: str) -> None:
    """Reject excess nesting before the recursive JSON decoder allocates objects.

    This lexical preflight runs before syntax validation. Quoted delimiters and
    escaped quotes do not count; malformed shallow JSON keeps its usual error.
    """
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > JSON_MAX_CONTAINERS:
                raise HTTPException(400, "There was an error parsing the body")
        elif character in "]}":
            depth = max(0, depth - 1)


class BoundedAnalysisRequest(Request):
    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            chunks = []
            size = 0
            async for chunk in self.stream():
                size += len(chunk)
                if size > MAX_REQUEST_BODY_BYTES:
                    raise HTTPException(413, "Request body is too large")
                chunks.append(chunk)
            self._body = b"".join(chunks)
        return self._body

    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            body = await self.body()
            # Match json.loads(bytes), including BOMs and lone surrogates.
            text = body.decode(json.detect_encoding(body), "surrogatepass")
            check_json_depth(text)
            self._json = json.loads(text)
        return self._json


class BoundedAnalysisRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded_handler(request: Request) -> Response:
            return await handler(BoundedAnalysisRequest(request.scope, request.receive))

        return bounded_handler


api = APIRouter(
    prefix="/api", dependencies=[Depends(require_basic_auth)], route_class=BoundedAnalysisRoute
)


def busy() -> ServiceError:
    return ServiceError(503, "queue", "Audio analysis is busy", retry_after=RETRY_AFTER_SECONDS)


def too_long(stage: str) -> ServiceError:
    if MAX_DURATION_SECONDS % 60 == 0:
        limit = f"{MAX_DURATION_SECONDS / 60:g} minutes"
    else:
        limit = f"{MAX_DURATION_SECONDS:g} seconds"
    return ServiceError(413, stage, f"Audio is longer than {limit}")


class SlotLease:
    """One analysis slot. Release it once, or keep it until a thread ends."""

    def __init__(self, slots: "JobSlots") -> None:
        self._slots = slots
        self._done = False
        self.kept = False

    def keep_until(self, future: "asyncio.Future", cleanup: Callable[[], None]) -> None:
        # Python cannot stop an Essentia thread. The slot stays taken until
        # the thread ends, so the capacity count stays true.
        self.kept = True

        def finish(done: "asyncio.Future") -> None:
            if not done.cancelled():
                done.exception()
            cleanup()
            self.release(force=True)

        future.add_done_callback(finish)

    def release(self, force: bool = False) -> None:
        if self._done or (self.kept and not force):
            return
        self._done = True
        self._slots.release()


class JobSlots:
    """A bounded queue in front of the analysis slots.

    It binds no event loop while it is idle: a waiter future exists only
    while a request waits.
    """

    def __init__(self, size: int, queue_limit: int) -> None:
        self.size = size
        self.queue_limit = queue_limit
        self.active = 0
        self.waiters: "deque[asyncio.Future]" = deque()

    def _waiting(self) -> int:
        return sum(1 for waiter in self.waiters if not waiter.done())

    async def acquire(self, wait_seconds: float) -> SlotLease:
        if self.active < self.size and self._waiting() == 0:
            self.active += 1
            return SlotLease(self)
        if self._waiting() >= self.queue_limit:
            raise busy()
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), wait_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if waiter.done() and not waiter.cancelled():
                self.release()  # The slot arrived as the wait ended; hand it on.
            else:
                waiter.cancel()
            if isinstance(exc, asyncio.TimeoutError):
                raise busy() from exc
            raise
        return SlotLease(self)

    def release(self) -> None:
        while self.waiters:
            waiter = self.waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)  # The slot moves to this waiter.
                return
        self.active -= 1


job_slots = JobSlots(ANALYSIS_CONCURRENCY, QUEUE_LIMIT)
ANALYSIS_EXECUTOR = ThreadPoolExecutor(
    max_workers=ANALYSIS_CONCURRENCY, thread_name_prefix="essentia"
)


def validate_media_url(url: str, label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (ALLOW_HTTP and parsed.scheme == "http"):
        raise ServiceError(400, "validate", f"{label} URL must use HTTPS")
    hostname = (parsed.hostname or "").lower()
    if hostname != PAX_R2_HOSTNAME and hostname not in ALLOWED_HOSTS:
        raise ServiceError(400, "validate", f"{label} host is not allowed")


def source_suffix(source_url: str) -> str:
    suffix = Path(unquote(urlparse(source_url).path)).suffix.lower()
    if suffix and len(suffix) <= 10 and suffix[1:].isalnum():
        return suffix
    return ".audio"


async def run_media_command(args: List[str]) -> Tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=SUBPROCESS_ENV,
    )
    try:
        stdout, _ = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode is None:
        raise RuntimeError("The media command has no exit status")
    return process.returncode, stdout


async def download_source(source_url: str, directory: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=source_suffix(source_url),
        dir=directory,
    )
    file_path = Path(handle.name)

    try:
        try:
            timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("GET", source_url) as response:
                    # A redirect is a non-200 status, so no second request goes out.
                    if response.status_code != 200:
                        raise ServiceError(502, "download", "Could not download audio source")

                    content_length = response.headers.get("content-length")
                    if content_length is not None and int(content_length) > MAX_DOWNLOAD_BYTES:
                        raise ServiceError(413, "download", "Audio file exceeds size limit")

                    downloaded_bytes = 0
                    with handle:
                        async for chunk in response.aiter_bytes():
                            downloaded_bytes += len(chunk)
                            if downloaded_bytes > MAX_DOWNLOAD_BYTES:
                                raise ServiceError(
                                    413, "download", "Audio file exceeds size limit"
                                )
                            handle.write(chunk)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ServiceError(502, "download", "Could not download audio source") from exc
    except BaseException:
        # Also on a deadline cancel: no partial file stays on disk.
        handle.close()
        file_path.unlink(missing_ok=True)
        raise

    return file_path


async def probe_duration(file_path: Path) -> Optional[float]:
    returncode, stdout = await run_media_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            MEDIA_FORMATS,
            "-select_streams",
            "a:0",
            "-show_entries",
            "format=duration:stream=codec_name",
            "-of",
            "json",
            str(file_path),
        ]
    )
    if returncode != 0 or len(stdout) > 64 * 1024:
        raise ServiceError(422, "probe", "Audio could not be decoded")
    try:
        text = stdout.decode("utf-8")
        check_json_depth(text)
        metadata = json.loads(text)
        if not isinstance(metadata, dict):
            raise ValueError("Invalid probe response")
        format_metadata = metadata.get("format", {})
        streams = metadata.get("streams", [])
        if not isinstance(format_metadata, dict) or not isinstance(streams, list):
            raise ValueError("Invalid probe response")
        if any(not isinstance(stream, dict) for stream in streams):
            raise ValueError("Invalid probe response")
    except (ValueError, UnicodeError, HTTPException):
        raise ServiceError(422, "probe", "Audio could not be decoded") from None
    try:
        parsed_duration = float(str(format_metadata.get("duration", "")))
        duration = parsed_duration if math.isfinite(parsed_duration) else None
    except ValueError:
        duration = None  # Some containers have no duration in their metadata.
    # Preserve the duration error before rejecting a codec: run_job reports 413.
    if duration is not None and duration > MAX_DURATION_SECONDS:
        return duration
    codec = streams[0].get("codec_name") if len(streams) == 1 else None
    if not isinstance(codec, str) or codec not in COMPATIBLE_AUDIO_CODECS:
        # Unsupported codecs can corrupt the pinned wheel's heap. The native
        # candidate imports the same allowlist and returns the same decode error.
        raise ServiceError(422, "decode", "Audio could not be decoded") from UndecodableAudio(
            "Audio could not be decoded"
        )
    return duration


async def run_analysis(file_path: Path, lease: SlotLease) -> dict:
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        ANALYSIS_EXECUTOR, get_analyzer().analyze_file, str(file_path), MAX_DURATION_SECONDS
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        lease.keep_until(future, lambda: file_path.unlink(missing_ok=True))
        raise
    except UndecodableAudio as exc:
        raise ServiceError(422, "decode", "Audio could not be decoded") from exc
    except AudioTooLong as exc:
        raise too_long("decode") from exc
    except DecodedAudioTooLarge as exc:
        raise ServiceError(413, "decode", "Decoded audio exceeds memory limit") from exc
    except AnalysisRejected as exc:
        raise ServiceError(422, "analyze", "Audio could not be analyzed") from exc


def ignore_stage(_stage: str) -> None:
    return None


async def create_playback_derivative(
    file_path: Path,
    target_url: str,
    on_stage: Callable[[str], None] = ignore_stage,
) -> dict:
    on_stage("encode")
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", dir=TEMP_DIR)
    playback_path = Path(handle.name)
    handle.close()
    try:
        returncode, _ = await run_media_command(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                MEDIA_FORMATS,
                "-i",
                str(file_path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(playback_path),
            ]
        )
        if returncode != 0:
            raise ServiceError(422, "encode", "Playback encoding failed")

        def playback_metadata() -> Tuple[int, str]:
            digest = hashlib.sha256()
            size_bytes = 0
            with playback_path.open("rb") as playback_file:
                while chunk := playback_file.read(UPLOAD_CHUNK_BYTES):
                    size_bytes += len(chunk)
                    digest.update(chunk)
            return size_bytes, digest.hexdigest()

        size_bytes, sha256 = await asyncio.to_thread(playback_metadata)

        async def playback_chunks():
            with playback_path.open("rb") as playback_file:
                while chunk := await asyncio.to_thread(
                    playback_file.read, UPLOAD_CHUNK_BYTES
                ):
                    yield chunk

        on_stage("upload")
        timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.put(
                    target_url,
                    content=playback_chunks(),
                    headers={
                        "Content-Type": "audio/mpeg",
                        "Content-Length": str(size_bytes),
                    },
                )
        except httpx.HTTPError as exc:
            raise ServiceError(502, "upload", "Playback upload failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ServiceError(502, "upload", "Playback upload failed")
        return {
            "sizeBytes": size_bytes,
            "sha256": sha256,
        }
    finally:
        playback_path.unlink(missing_ok=True)


async def run_job(request: Request, source_url: str, target_url: Optional[str]) -> AnalysisResult:
    # Take the slot before the download, so a full queue costs no transfer.
    set_stage(request, "queue")
    lease = await job_slots.acquire(QUEUE_WAIT_SECONDS)
    file_path: Optional[Path] = None
    try:
        set_stage(request, "download")
        file_path = await download_source(source_url, TEMP_DIR)

        set_stage(request, "probe")
        probed_duration = await probe_duration(file_path)
        if probed_duration is not None and probed_duration > MAX_DURATION_SECONDS:
            raise too_long("probe")

        set_stage(request, "analyze")
        analysis = await run_analysis(file_path, lease)
        if analysis["duration"] > MAX_DURATION_SECONDS:
            raise too_long("decode")

        # Pydantic validates this dict into PlaybackDerivative.
        playback: Any = None
        if target_url is not None:
            playback = await create_playback_derivative(
                file_path, target_url, lambda stage: set_stage(request, stage)
            )
        return AnalysisResult(**analysis, playback=playback)
    finally:
        # A kept lease belongs to a running analysis thread. That thread
        # releases the slot and deletes the file when it ends.
        if not lease.kept:
            lease.release()
            if file_path is not None:
                file_path.unlink(missing_ok=True)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    # Vercel exposes the deployment commit; each service reports its own.
    release = os.environ.get("VERCEL_GIT_COMMIT_SHA", "").strip() or "local"
    return {"status": "ok", "release": release}


@api.post("/analyze-r2", response_model=AnalysisResult)
async def analyze_r2(payload: AnalyzeR2Request, request: Request) -> AnalysisResult:
    # Validate both URLs before the slot and the download.
    set_stage(request, "validate")
    source_url = str(payload.source_url)
    validate_media_url(source_url, "Source")
    target_url = None if payload.target_url is None else str(payload.target_url)
    if target_url is not None:
        validate_media_url(target_url, "Target")

    try:
        return await asyncio.wait_for(
            run_job(request, source_url, target_url), DEADLINE_SECONDS
        )
    except asyncio.TimeoutError as exc:
        raise ServiceError(504, request.state.stage, "Audio analysis took too long") from exc
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError(500, request.state.stage, "Audio analysis failed") from exc


app.include_router(api)
