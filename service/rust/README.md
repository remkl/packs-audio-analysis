# Rust audio-analysis replacement candidate

This folder contains a standalone Axum service for the current PAX analysis
contract. It downloads the source once, uses Essentia for analysis, and runs
FFmpeg in the same container when playback needs an MP3 derivative. The DSP
remains C++. Only the HTTP and process control layers are Rust.

The native engine borrows the full audio signal between the rhythm algorithms.
It keeps the existing computation order. Shared FFT code and its runtime switch
are absent. Algorithms are fresh for each request. The worker writes each JSON
response once; the server sends those bytes without parsing them again.

The current compatibility scope and intentional shared limits are recorded in
[COMPATIBILITY.md](COMPATIBILITY.md).

The parent `Dockerfile.vercel` is the canonical Rust deployment build. Its
context is the complete `services/audio-analysis` folder. `Dockerfile.python`
preserves the Python reference and its test stage. This source change does not
move the live alias or change any Convex setting.

## Build and prove

Docker, a running Linux Docker engine, and Python 3.10 or later on the host are
required. Python on the host runs the checks only. The final image has no Python
or Rust build tools. No local experiment image is a build input.

From the repository root:

```sh
# Full service gate: Python checks, Rust checks and strict compatibility.
bun run check:audio-analysis

# Optional native ARM64 functional proof on an ARM machine.
python3 services/audio-analysis/rust/check.py --architecture arm64
```

The service gate defaults to Linux AMD64, the pinned Python wheel platform.
On an ARM Mac, that proof uses emulation and does not measure AMD64 performance.
The ARM64 gate uses the native engine goldens. It still compares the HTTP
contract against the pinned AMD64 Python service. The first source build takes
longer; later runs use the Docker layer cache.
ARM64 currently has known cross-architecture differences in the expanded OGG
and playback checks. The gate reports them as failures. Use AMD64 for the
strict replacement preview; do not use ARM64 performance numbers as hosted
AMD64 measurements.

For an image only:

```sh
docker build --platform linux/amd64 --target runtime \
  -f services/audio-analysis/Dockerfile.vercel \
  -t pax-audio-analysis:rust-candidate services/audio-analysis
```

The gate runs these checks:

- Rust formatting, release tests and Clippy with warnings treated as errors.
- Python and Rust HTTP responses, headers and request logs on a local fixture
  server. It checks authentication, JSON parsing, URL rules, aliases, request
  ids, errors, queue overload, deadlines and configuration boundaries.
- Transport fixtures for compressed downloads, wire and decoded size limits,
  truncated bodies, and steady or stalled downloads and uploads.
- Exact native float32 comparisons between the original buffer ownership and
  shared buffers, including reuse of the probe objects, reconfiguration,
  recovery after errors and unwinding of borrowed references.
- On AMD64, byte-for-byte decoded PCM comparison with the frozen Python wheel
  for the 14 original fixtures and ten additional container/gapless controls.
  This checks decoder version changes separately from DSP buffer ownership.
- Fixed audio output across repeated, reversed, shuffled and concurrent jobs.
  BPM, keys, duration, peaks, preview and playback hash/size must match exactly.
  Confidence keeps the existing absolute tolerance of `1e-6`. The current
  Python image must also match its frozen audio reference. Corrupt, empty and
  overlong audio errors are compared directly against that image.
- Shutdown during download, analysis and upload; a disconnected client;
  stopped native workers; stalled media processes; deadline recovery; and
  forced shutdown after the real 25-second drain interval.
- UID/GID `10001`, port 80 with only the bind capability, and isolation of the
  native decoder libraries from the FFmpeg command libraries.

Tests use synthetic audio and local fixture servers. They do not call R2 or the
live service. The checked-in corpus archive and each file have SHA-256 hashes
in `compat-data.json`. Extraction and test output are ignored by Git.

The gate finishes independent HTTP and runtime checks even after a mismatch,
then exits with failure if either check failed.

The gate writes `.test-results/<architecture>/manifest.json`, image IDs,
source hashes and detailed results. It fails if candidate source changes
during the run, including the Python reference and canonical Dockerfiles.
CI enables the Buildx cache with `PAX_AUDIO_BUILD_CACHE=gha` (the same as
`--cache-gha`) and keeps its evidence for 14 days.
Both test and security workflows remain manual. The security workflow keeps
the Python audit and image scan, scans the Rust runtime image, and separately
scans `Cargo.lock` for fixable high and critical findings.
These are finite regression checks, not proof for every
possible input. Keep the existing goldens and tolerance unchanged.

## Configuration and request contract

Use the parent [.env.example](../.env.example). Names, defaults, accepted values
and validation order follow the pinned Python service. The exact PAX R2 account
host remains allowed. `AUDIO_ANALYSIS_ALLOWED_HOSTS` must also contain at least
one startup value, as it does in the current Python service.

| Setting | Default |
|---|---|
| `PORT` | `80` |
| `AUDIO_ANALYSIS_BASIC_AUTH_USERNAME` | `analysis` |
| `AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD` | Required |
| `AUDIO_ANALYSIS_ALLOWED_HOSTS` | Required |
| `AUDIO_ANALYSIS_ALLOW_HTTP` | `false` |
| `AUDIO_ANALYSIS_CONCURRENCY` | `2`, range 1–4 |
| `AUDIO_ANALYSIS_QUEUE_LIMIT` | Twice the worker count |
| `AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS` | `20` |
| `AUDIO_ANALYSIS_DEADLINE_SECONDS` | `100`, maximum 110 |
| `AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES` | `157286400` |
| `AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS` | `90` |
| `AUDIO_ANALYSIS_MAX_DURATION_SECONDS` | `300` |
| `AUDIO_ANALYSIS_DATA_DIR` | System temporary directory |
| `VERCEL_GIT_COMMIT_SHA` | Health release; `local` when absent or blank |
| `AUDIO_ANALYSIS_RESOURCE_PROFILE` | Off; `1` enables preview memory logs |

Temporary jobs use `<data dir>/audio-analysis/tmp`. The outer directory must be
writable by UID 10001. Each job directory is private. Worker processes receive
only the numeric limits and PATH; media processes receive only PATH.

`GET /healthz` and `POST /api/analyze-r2` keep their existing contract. Source
and target URLs pass validation before admission. Redirects stay disabled.
The bounded queue is before download. Queue wait counts against the total job
deadline. Overload returns 503 with `Retry-After: 15`.

An admitted job survives a client disconnect and writes one scrubbed completion
log. SIGTERM or SIGINT closes admission, then drains active jobs for up to
25 seconds. At that limit, the server kills each worker process group and reaps
its media children before it removes job files. It allows up to one more second
for final HTTP responses. A request deadline retires its worker and IPC stream
before the next request can use that slot. This also stops stuck native DSP.

## Decode memory limits

The decoder keeps the pinned MonoLoader, downmix and resampler. Its final sink
stops before it appends samples beyond the configured duration or 256 MiB of
mono float32 PCM, whichever comes first. At the default 300 seconds, retained
PCM is at most 52,920,000 bytes (50.5 MiB). Buffer capacity has the same bound.
Growth can briefly hold the old and new buffers; this is not a total RSS limit.
Duration configuration must be finite and positive in both services. The Python
reference has the same admission rules but checks PCM size after allocation.

Each Rust worker also sets a fixed `RLIMIT_AS` after native initialization:
startup virtual memory plus 512 MiB, or an existing stricter limit. This bounds
later virtual allocations in the worker and each media subprocess. The budget
does not grow between jobs. It is a per-process virtual limit, not a cap on the
combined RSS of all workers. Keep the host/container memory limit as the final
aggregate bound. A native allocation failure or worker exit returns an error;
an exited worker is replaced before the next job.

The local gate compares raw decoded float32 samples and exact duration
boundaries. It also rejects a 30-minute FLAC with false 0.1-second metadata at
a one-second limit inside a 128 MiB container, then checks a valid request and
cleanup. A second request checks the absolute 256 MiB PCM limit. The gate reads
the kernel's worker limits and checks that media children inherit them.
No output or confidence baseline is relaxed.

For protected previews only, `AUDIO_ANALYSIS_RESOURCE_PROFILE=1` samples summed
process-tree RSS every 50 ms and writes a one-second peak window. It also reads
cgroup current/peak memory when exposed by the host; absent counters remain
null. Summed RSS counts shared pages more than once and sampling can miss brief
peaks. Peak windows can overlap benchmark boundaries; reports mark this overlap.
Cgroup peak is a lifetime value. These logs contain no source URLs,
credentials or audio. The public response is unchanged.

`preview-check.py --help` describes the explicit preview-only audio and load
runner. It uses synthetic fixtures, short-lived signed URLs and a unique R2
prefix, checks uploaded MP3 bytes, and deletes all test objects. It needs a
private credential file and an explicitly allowed immutable preview hostname.
Its end-to-end throughput includes network, download and upload time; it does
not measure DSP time alone. Resource logs can be attached without rerunning load.

## Pinned native dependencies

The Dockerfile pins public base images by digest. Source archives have fixed
SHA-256 hashes. Cargo uses its committed lockfile. The build uses Essentia
commit `36ec3d929402d491cbbb1120c54631e8c205b0ff`, libsamplerate 0.1.8 and FFTW
3.3.8. The decoder patch selects the integer MP3 decoder used by the pinned
Python wheel and retains AAC leading samples and Ogg/Vorbis trailing samples.
Explicit Matroska/Vorbis end padding still applies.
Python and the native decoder
share 62 accepted codec names in `codec_compatibility.py`. The Python tests
verify the wheel's 63 registered names and the explicit Zork exclusion.
Unsupported codecs, including Opus and IMA/MS ADPCM, return a decode error.
Python rejects them before MonoLoader because its unsupported-codec cleanup can
corrupt the heap. WebM/Vorbis remains supported. Legacy Zork WAV is deliberately
rejected in both services; there is no custom decoder adapter. Three generated
mono/stereo Zork files check that rejection. Eight more FLAC, OGG, M4A, WebM
and ADPCM WAV controls compare full responses and playback hashes against Python,
with explicit success/error expectations and no error uploads.

Native decode uses Debian FFmpeg 5.1.9 source, rebuilt on Bookworm. The adapter
owns each codec context and drains the send/receive API. The native build contains
only accepted audio decoders and input containers; unused native writers are
excluded. System C/C++ libraries come from the final Debian image. FFTW retains
its portable Debian arithmetic flags. The resampler fixes CVE-2017-7697 with an
endpoint guard that preserves the original buffer schedule. Its sanitizer check
covers every interpolation path. These changes require the unchanged strict goldens.

The FFmpeg and FFprobe commands use a separate minimal build of the same supported
source. Playback keeps the existing libmp3lame encoder and MP3 output. Both builds
retain their configuration, decoder registry and source hashes in the image.
The build fails if a component required to be absent by `ffmpeg-policy.json`
is enabled. This covers the reviewed advisories, not all possible vulnerabilities.

The release gate scans the build and runtime OS packages. Custom source-built
libraries also need their source inventory and advisory review: absence from
the package database is not evidence of security. See [SUPPORT.md](SUPPORT.md).
Signed Debian packages can change in the upstream repository; the build pins
compatibility inputs but does not promise identical image bytes forever.

See [NOTICE.md](NOTICE.md) and the parent AGPL licence. ADR 0048 and Plan 139
OPS-11 own source publication. Publish the complete corresponding source and
patches for the chosen release before public launch.

## Protected preview and later cutover

1. Freeze the intended service contract. Plan 133 BLEND-6 adds chroma, density
   and downbeat fields. If it lands first, port those fields and their unchanged
   goldens before this candidate replaces Python. The HTTP gate also checks
   that its Python oracle matches the checked-out source.
2. Build the canonical `Dockerfile.vercel` from the existing service context.
   Keep the currently deployed Python image and deployment ID for rollback.
   `Dockerfile.python` is the current compatibility reference; rebuilding it
   later is not the same as restoring an immutable production image.
3. Deploy a protected preview with the same architecture, region, environment,
   port, memory, CPU and duration settings as the target service. Confirm those
   settings from the actual project. Local Docker cannot prove hosting limits,
   cold starts or cost.
4. Use a separate test endpoint. The current analysis alias is shared by
   development, preview and production: moving it changes all callers at once.
   Check WAV, AIFF and MP3, upload hashes, overload/retry behavior, shutdown,
   cold/warm latency, CPU time and peak container memory there.
5. Keep the current Convex adapter and workflow steps. A contract-preserving
   replacement needs no schema change. It uses `AUDIO_ANALYSIS_URL`, the same
   Basic Auth values and the existing Vercel bypass header. Keep the current
   workflow parallelism of four and service defaults for the first release.
6. After hosted proof, choose the cutover and rollback procedure. Route callers
   to the verified Rust deployment; restore the previous endpoint/image if its
   error rate or latency rises. Record the release and source publication URL.

Durable batch admission remains in Convex. The local service queue only bounds
work in one container. Do not add a second durable queue for this replacement.
Before increasing throughput, put backfills under the same admission limit,
measure queue age and retries, and align claim leases with retry duration and
stale-run fencing. Vercel scaling and a higher request limit do not replace
these controls. No new capacity or cost result is claimed by this build gate.
