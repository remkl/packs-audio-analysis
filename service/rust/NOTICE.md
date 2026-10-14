# Native analysis build notices

The service is licensed under AGPL-3.0. It links to Essentia (AGPL-3.0),
commit `36ec3d929402d491cbbb1120c54631e8c205b0ff` from
https://github.com/MTG/essentia. Original source headers and licences remain
in the downloaded source. The local patch scripts record our changes:
integer MP3 decoder selection, AAC leading samples, the accepted-codec guard,
fresh-child configuration guards, and scoped
borrowing of immutable full-signal input. Shared FFT and algorithm reuse are
not enabled in this candidate. The DSP is C++, with a Rust HTTP server.

The build also uses libsamplerate 0.1.8 (original COPYING file in its pinned
source archive), Debian FFmpeg 4.3.9 for native decoding, FFmpeg/ffprobe 5.1.9 for
encoding and metadata, and FFTW 3.3.8. Runtime dependency licence
files remain under `/usr/share/doc`. Public source archive URLs and SHA-256
hashes are in `native-build/fetch-sources.sh`. Rust dependencies are recorded
in `Cargo.lock`.

ADR 0048 requires publication of the complete corresponding service source
before public launch and keeping it current. Publish this folder, the parent
service contract tests and fixtures, Dockerfile, Cargo.lock, all patch scripts,
and the exact pinned upstream sources. Record the public source URL in
`handbook/decisions/0048-essentia-agpl-analysis-service.md` and the licence
register. This candidate does not alter the deployed Python entrypoint.

Build from a clean checkout:

```
docker buildx build --load \
  -f services/audio-analysis/Dockerfile.vercel \
  --target runtime -t pax-audio-analysis:rust-candidate services/audio-analysis
docker buildx build --load \
  -f services/audio-analysis/Dockerfile.vercel \
  --target test -t pax-audio-analysis:rust-engine-test services/audio-analysis
```

The test target contains a separate test-only engine, which permits baseline
and buffers comparisons. Run it with the complete 14-file valid fixture corpus
mounted read-only. It rejects `shared_fft` and refuses to pass with a missing
fixture corpus. The runtime engine always borrows, regardless of environment.
Native decoding uses integer MP3 and libsamplerate 0.1.8 for pinned wheel PCM
parity. Existing golden tests remain the acceptance gate; source version
matching alone does not prove decoder or architecture parity.
