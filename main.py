import asyncio
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from analyzer import AudioAnalyzer
from models import AnalysisResult, AnalyzeRequest, UploadResponse


AUTH_USERNAME = os.getenv("AUDIO_ANALYSIS_BASIC_AUTH_USERNAME", "analysis")
AUTH_PASSWORD = os.getenv("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD")

if not AUTH_PASSWORD:
    raise RuntimeError("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD must be set")

DATA_DIR = Path(os.getenv("AUDIO_ANALYSIS_DATA_DIR", tempfile.gettempdir())) / "audio-analysis"
INPUT_DIR = DATA_DIR / "input"
TEMP_DIR = DATA_DIR / "tmp"

INPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

security = HTTPBasic(auto_error=False)
analyzer = AudioAnalyzer()
analyzer_lock = asyncio.Lock()

app = FastAPI(title="Audio Analysis Service")


def require_basic_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    if credentials is None:
        raise_auth_error()

    username_matches = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    password_matches = secrets.compare_digest(credentials.password, AUTH_PASSWORD)

    if not (username_matches and password_matches):
        raise_auth_error()


def raise_auth_error() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid audio analysis credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


api = APIRouter(prefix="/api", dependencies=[Depends(require_basic_auth)])


async def run_analysis(file_path: Path) -> dict:
    async with analyzer_lock:
        loop = asyncio.get_running_loop()

        return await loop.run_in_executor(None, analyzer.analyze_file, str(file_path))


async def save_upload(file: UploadFile, directory: Path, filename: Optional[str] = None) -> Path:
    suffix = Path(file.filename or "upload").suffix
    if filename is None:
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=directory)
        file_path = Path(handle.name)
    else:
        safe_name = Path(filename).name
        file_path = directory / safe_name
        handle = open(file_path, "wb")

    try:
        with handle:
            shutil.copyfileobj(file.file, handle)
    except Exception:
        if file_path.exists():
            file_path.unlink()

        raise

    return file_path


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@api.post("/analyze-upload", response_model=AnalysisResult)
async def analyze_upload(file: UploadFile = File(...)) -> AnalysisResult:
    file_path = await save_upload(file, TEMP_DIR)

    try:
        return AnalysisResult(**await run_analysis(file_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if file_path.exists():
            file_path.unlink()


@api.post("/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...)) -> UploadResponse:
    for existing_file in INPUT_DIR.iterdir():
        if existing_file.is_file() or existing_file.is_symlink():
            existing_file.unlink()
        elif existing_file.is_dir():
            shutil.rmtree(existing_file)

    saved_path = await save_upload(file, INPUT_DIR, file.filename or "upload")

    return UploadResponse(filename=saved_path.name)


@api.post("/analyze", response_model=AnalysisResult)
async def analyze_audio(request: AnalyzeRequest) -> AnalysisResult:
    file_path = INPUT_DIR / Path(request.filename).name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        return AnalysisResult(**await run_analysis(file_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


app.include_router(api)
