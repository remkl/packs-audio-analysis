# PAX audio analysis

Stateless audio-analysis service for the Vercel container project
`pax-audio-analysis`. The canonical `Dockerfile.vercel` now builds the Rust/Axum
replacement candidate. `Dockerfile.python` retains the FastAPI reference.
The deployed Python service stays in use until an explicit cutover. R2 owns
audio. The service receives a short-lived,
object-scoped R2 GET URL and returns BPM, key, confidence, duration and
normalized waveform data from the same decoded samples.

## Contract

- `GET /healthz` needs no app Basic Auth. The protected Vercel alias still
  needs the Vercel bypass header.
- `POST /api/analyze-r2` is Basic Auth protected and accepts
  `{"sourceUrl":"https://..."}`.
- Analysis responses contain 64–2048 normalized peaks and a 64–96 point
  preview.
- The service has no multipart or file-upload route. Every analysis request
  names a short-lived source URL.
- The service has no `/docs`, `/redoc` or `/openapi.json` route.

The exact PAX R2 account host is allowed in code. `AUDIO_ANALYSIS_ALLOWED_HOSTS`
adds non-production hosts for local tests and must contain at least one startup
value. Redirects are off, downloads are bounded, temporary files are deleted,
and source URLs must never be logged.

### Errors and request ids

The service validates the source URL and the target URL before it takes a
slot or downloads a byte. Each error body has this shape:

```json
{"detail": "Audio could not be decoded", "stage": "probe", "requestId": "w8-contract-0001"}
```

`tests/contract/` holds one example success body and one example error body.
The Convex tests load the same files.

| Status | Stage | Meaning | Convex retries it |
|---|---|---|---|
| 400 | `request` | Invalid JSON or more than 64 nested containers | No |
| 400 | `validate` | Bad URL scheme or host | No |
| 401 | `request` | Bad credentials | No |
| 413 | `request` | The request body is larger than 64 KiB | No |
| 413 | `download` | The file is larger than the byte limit | No |
| 413 | `probe`, `decode` | The audio is longer than the duration limit | No |
| 422 | `validate` | The request body is not valid | No |
| 422 | `probe`, `decode` | Invalid audio, unsupported codec or nonfinite samples | No |
| 422 | `analyze`, `encode` | Essentia or ffmpeg failed on a decoded signal | No |
| 500 | any | Unknown failure | Yes |
| 502 | `download`, `upload` | R2 failed | Yes |
| 503 | `queue` | The queue is full or the queue wait ended (`Retry-After: 15`) | Yes |
| 504 | the stage at the deadline | The total deadline passed | Yes |

The stages are `request`, `validate`, `queue`, `download`, `probe`, `decode`,
`analyze`, `encode` and `upload`. Convex sends `x-pax-request-id` (8 to 64
characters from `A-Z`, `a-z`, `0-9`, `_` and `-`). The service makes its own id
when the value is missing or not valid. It returns the id in the
`x-pax-request-id` response header and in each error body.

The service writes one JSON line to stdout for each request, with the keys
`service`, `route`, `method`, `status`, `durationMs`, `stage`, `errorClass`
and `requestId`. The line never holds a URL, a file path, a credential or
subprocess output. The Rust server owns its request log. The Python reference
runs Uvicorn with
`--no-access-log`. Each request writes exactly one JSON completion line.

## Local test

Use Python 3.12, the version of the service image. From the repository root,
`mise install` installs it. Then, in this folder:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install -r requirements-dev.txt
python -m pytest -q
```

`requirements-dev.txt` has no hashes, so it installs in a second command. If
pip cannot install a pinned wheel on your platform, use the Docker build as the
local loop for Python:

```bash
docker build --platform linux/amd64 -f Dockerfile.python .
```

The full repository gate is `bun run check:audio-analysis`. It builds both
services and checks strict HTTP, audio, native buffer, transport, shutdown and
process-cleanup compatibility. See [rust/README.md](rust/README.md).

## Runtime environment

Copy `.env.example` and replace the password. Add non-PAX source hosts only for
local tests. The Vercel image listens on the platform container port 80, or on
`PORT` when it is set. The container does not receive R2 credentials. It runs
as the non-root user `10001`; the Rust binary has only the
`cap_net_bind_service` file capability, so it can bind port 80.

`AUDIO_ANALYSIS_CONCURRENCY` defaults to `2` and accepts `1` through `4`.
Rust uses bounded worker processes so folder uploads can process two sounds
at once. Each worker runs the pinned Essentia engine.

| Setting | Default | Rule |
|---|---|---|
| `AUDIO_ANALYSIS_DEADLINE_SECONDS` | `100` | Above 0 and at most 110. One deadline covers the queue, download, probe, analysis, encode and upload. |
| `AUDIO_ANALYSIS_QUEUE_LIMIT` | 2 x concurrency | 0 or more. More waiting requests get 503. |
| `AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS` | `20` | Above 0 and below the deadline. |
| `AUDIO_ANALYSIS_MAX_DURATION_SECONDS` | `300` | Above 0. Convex `MAX_ANALYZED_AUDIO_MINUTES` must match. |

A request takes its slot before the download. At the deadline the service
kills the Rust worker process group, including `ffprobe` and `ffmpeg`, and
reaps its child processes before it removes temporary files. On shutdown it
stops admission and drains jobs for up to 25 seconds. The Python reference
cannot stop an Essentia thread; a thread past its deadline keeps its slot
until it ends. Media subprocesses get only `PATH` from the environment.

## Capacity

| Value | Setting |
|---|---|
| Analysis slots | 2 |
| Queue limit | 4 |
| Queue wait | 20 s |
| Deadline | 100 s |
| Maximum audio length | 300 s |
| Vercel maximum duration | 120 s (set in the project settings) |
| Cold start, first-analysis latency, p99 latency, peak memory | Measured on a preview deployment: pending (operator task 1) |

The live service uses the protected alias
`https://pax-audio-analysis-r2.vercel.app`. Convex sends Basic Auth and the
`x-vercel-protection-bypass` header. Its value comes from the Convex-only
`VERCEL_AUTOMATION_BYPASS_SECRET` environment variable. Vercel Preview
Protection stays enabled. Do not send this header from a browser client.

## Dependency lock

`requirements.txt` holds the top-level pins. `requirements.lock` is generated
output with a hash for each package, and the image installs it with
`pip install --require-hashes`. `Dockerfile.python` pins the Python base image
by digest. The Rust build
uses a Cargo lockfile and pinned native source archives; see its README.
To change a pin, edit `requirements.txt`, then generate the lock inside the
pinned base image, so that the Python version and the platform match:

```sh
docker run --rm --platform linux/amd64 \
  -v "$PWD/services/audio-analysis":/work -w /work \
  python:3.12-slim-bookworm@sha256:<digest from Dockerfile.python> \
  sh -c 'python3 -m pip install --no-cache-dir pip-tools==7.5.3 >/dev/null && \
         pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras \
           --output-file requirements.lock requirements.txt'
```

The Python reference pins the official `essentia==2.1b6.dev1389` CPython 3.12
Linux wheel. Python
3.13 removes `aifc`, which the tests use. `requirements.txt` pins `starlette`
because `main.py` imports it.

`tests/test_output_parity.py` analyzes a fixed fixture set and compares the
result with `tests/fixtures/output-baseline.json`. BPM, key, duration, peaks
and preview must be identical; the confidences may differ by 1e-6. The
baseline came from the Python 3.9 image, and the Python 3.12 image gives the
same output. A change to Python, Essentia or NumPy must pass this test with no
baseline change. Record a new baseline only with an owner decision, in the
runtime stage that the baseline describes, with `PAX_WRITE_OUTPUT_BASELINE=1`.

## Rust replacement candidate

[rust/README.md](rust/README.md) describes the standalone Axum candidate,
the full local compatibility gate and the protected preview procedure.
It shares native audio buffers and keeps shared FFT disabled. The build context
stays at `services/audio-analysis`; the image has no Python or Rust build tools.
CI uses the same full gate and retains evidence. Test and security workflows
remain manual. Keep the shared live alias and Convex URL unchanged while
checking the preview.
