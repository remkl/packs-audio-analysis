# Audio Analysis Service

FastAPI service for BPM and musical key analysis using Essentia.

## API

- `GET /healthz` - unauthenticated health check.
- `POST /api/analyze-upload` - authenticated multipart upload and analysis.
- `POST /api/upload` then `POST /api/analyze` - authenticated compatibility flow.

All `/api/*` endpoints require HTTP Basic Auth.

## Environment

```bash
AUDIO_ANALYSIS_BASIC_AUTH_USERNAME=analysis
AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD=dev-analysis-secret
AUDIO_ANALYSIS_VERSION=essentia-sidecar-v1
PORT=8000
```

`AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD` is required at startup.

## Local Docker

```bash
docker compose up --build
curl -u analysis:dev-analysis-secret \
  -F "file=@/path/to/audio.wav" \
  http://localhost:8000/api/analyze-upload
```

## Render

Create a Git-backed Render Docker web service from this repo, or use `render.yaml`.
Set `AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD` in Render as a secret environment variable.

## Tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```
