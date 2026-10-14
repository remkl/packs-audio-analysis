# Compatibility contract

The Python reference and Rust candidate enforce the same explicit input rules.
The user approved a shared depth rule on 2026-10-14; the input review also found
file cases that require the following common rejection rules.

- Analysis POST bodies have a 64 KiB limit, measured while reading the stream.
  The limit applies before JSON parsing and authentication, including chunked
  requests. Larger bodies return 413 at `request`.
- JSON has at most 64 nested containers, including the root object. The scanner
  ignores brackets in quoted strings and runs before recursive Python parsing.
  Deeper bodies return 400 at `request`. UTF-8, UTF-16 and UTF-32 use the same
  rule. Authentication and media-type behavior otherwise stay the same.
- Probe and encode accept WAV, AIFF, MP3, FLAC, OGG, MOV/M4A and Matroska/WebM
  containers. File and network playlists are rejected at `probe`, even when
  renamed as an audio file. The existing file-only CLI protocol rule remains.
- The decoded sample count must meet the duration limit even when metadata
  understates it. This check runs before waveform, tempo and key computation.
  Excess duration returns 413 at `decode`.
- Decoded samples must be finite. NaN and either infinity return 422 at `decode`
  before DSP. Finite float WAV files remain supported.
- Both services accept 62 of the pinned wheel's 63 registered audio codec names. Python
  checks the first audio stream before MonoLoader because the old wheel's
  unsupported-codec cleanup can corrupt the heap. The Rust native loader uses
  the same list. Opus and IMA/MS ADPCM WAV return 422 at `decode`; overlong container
  metadata still takes precedence and returns 413 at `probe`.
  Zork WAV is explicitly excluded in both services, as requested on 2026-10-14.
  Its old `pcm_zork` and modern `adpcm_zork` names both return 422 at `decode`.
  Mono/stereo controls check this rejection and prove that no playback is uploaded.

These are intentional shared rejection rules, not output-tolerance changes.
The deployed Python service does not acquire them until a separate release.

The earlier ignored-value case with 9,982 nested arrays is retained in the HTTP
suite. Both implementations now reject it deterministically. The Rust parser
also rejects 100,000 levels on a 64 KiB thread stack. No recursive runtime
boundary is used as the request contract.

The strict gate compares HTTP status, body, headers, source access and scrubbed
request logs. Existing audio goldens and MP3 hash/size stay exact; confidence
keeps its existing absolute tolerance of `1e-6`. Generated FLAC, OGG, M4A and
WebM controls compare directly with the current Python reference. Shared FFT
code and its runtime switch remain absent.

The replacement target is Linux AMD64, the Python wheel's architecture. The
expanded codec checks also run on ARM64 without weaker assertions. ARM64 has
known cross-architecture differences in OGG waveform peaks and encoded MP3
hashes; a passing native-engine probe alone does not make that image a strict
replacement. AAC keeps the Python wheel's leading samples. Opus returns the
same 422 decode error as Python, including in otherwise valid WebM and OGG
containers; WebM/Vorbis is accepted.

## Remaining limits

These are finite regression checks, not proof for every possible audio file.
MonoLoader still allocates the whole decoded signal before the duration and
finite-sample checks. Compressed input size does not bound that allocation.
A future decoder sample cap and codec-isolation work need separate proof.
Rust can terminate a worker at its deadline; the Python reference cannot stop a
running Essentia thread. Container memory limits remain the final memory bound.
Keep the preview isolated from live callers and measure its memory before a
cutover. No throughput improvement is claimed for this hardening change.
