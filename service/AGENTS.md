# Audio analysis service

This surface owns the stateless audio-analysis Vercel container.
`Dockerfile.vercel` builds the Rust replacement. `Dockerfile.python` preserves
the FastAPI reference for strict compatibility checks.

## Boundaries

- R2 owns audio. The service receives only a short-lived presigned GET URL.
- Convex is the only production caller of authenticated analysis routes.
- Never log source URLs, credentials, or audio content.
- Write one JSON log line for each request with the route, status, duration,
  stage, error class and request id.
- Keep the source-host allowlist exact. Do not follow redirects.
- Use ephemeral files only and delete them in `finally` paths.
- Do not add persistent state, queues, databases, or R2 credentials here.

## Verification

Run `bun run check:audio-analysis` from the repository root. Public HTTP
behavior belongs in `tests/test_api.py`; add a failing contract test first.

The Rust replacement is in `rust/`. The repository gate runs
`python3 services/audio-analysis/rust/check.py --build-python` from the root.
It builds from public pinned inputs and checks the current
Python contract, native/audio parity, transport, process cleanup and shutdown.
See `rust/README.md`. Keep the live alias and Convex service URL unchanged
until an explicit cutover is requested. A preview must remain protected.
Do not enable shared FFT or loosen output baselines to make this gate pass.
