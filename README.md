# PAX audio analysis — V10 candidate source

Exact service source: `ef69b73640d5cb3872ae00fb34f2d27a28f65f29`. Shared FFT is off. Production
has not been switched by this export. This source package does not switch or deploy the service.

`service/` contains all 76 tracked service files unchanged. The native
builder now uses Bookworm and pinned FFmpeg 5.1.9 source with compatibility
patches. FFTW 3.3.8 and Eigen 3.3.9 retain their pinned Debian patches and are
rebuilt on Bookworm. Essentia and libsamplerate retain their pinned inputs.

## Build and strict checks

Use Docker Buildx and Python 3.10 or later from this repository root:

```sh
docker buildx build --load --platform linux/amd64 --target runtime -f service/Dockerfile.vercel -t pax-audio-analysis:local service
python3 service/rust/check.py --architecture amd64 --build-python
```

The standalone build and strict gate passed from this exported tree. All 24
decoder PCM controls, 56 engine checks and 248 HTTP/config cases passed, along
with the runtime bounds and lifecycle checks. The runtime, probe and Python
image IDs match the canonical gate. Docker reused its local build cache; this
is not a claim of a fresh uncached or offline build. See STANDALONE-CHECKS.json.

The manual workflow runs the same gate, including exact decoder PCM controls,
audio/HTTP/config parity, resource bounds, lifecycle, and shared-FFT exclusion.
ARM-host AMD64 execution is emulated and is not a native performance result.

## Source archives

`pax-audio-analysis-ef69b73640d5-dependencies.tar.gz` contains 175 exact native, Cargo and copied
Debian source inputs, verified against source pins, Cargo.lock and authenticated
Debian source metadata. Its SHA-256 is
`a7cfb7df72c81381f919a4b78a65392f0539f8d1e98d87fe37832cec8066a817`. Sources are public build inputs; builds remain networked,
not fully offline or hermetic. Keep upstream licence files and Debian patches.
`SOURCE-COMPLETENESS.md` and `SOURCE-IMAGE-INVENTORY.json` record runtime and build-input scope.
No V9 binary-closure count is reused as V10 evidence.

The root Python files, Dockerfile and Render blueprint are legacy files with
unchanged bytes. Use `service/Dockerfile.vercel` for V10. Preserve repository
history; publication commits must contain `[skip render]`. CI is manual only.
The service remains AGPL-3.0-only; see LICENSE and service/rust/NOTICE.md.
