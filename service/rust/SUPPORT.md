# Native dependency support

The PAX audio-analysis maintainer owns this dependency set. A successful upload
proves a working path. It does not prove that a decoder receives security fixes
or that a malformed file cannot reach another parser.

## Supported base and decoder

Build and runtime use Debian 12 Bookworm. Debian lists AMD64 and ARM64 LTS
support through 2028-06-30. The previous Debian 11 LTS period ended on
2026-08-31. Do not restore archived Bullseye binaries to make a golden pass.

FFmpeg uses the pinned Debian 5.1.9 source and patch archive. The native build
registers exactly the 62 accepted audio codec IDs. Keep unused video decoders,
hardware, network inputs and output formats out of the build. The build checks
the registry; the release review must also check the actual compiled configuration
of the metadata and playback commands. A source version alone is insufficient.

FFmpeg may probe streams before it selects audio. A command-line codec allowlist
is not a drop-in substitute: it changes error stages and can reject an accepted
file with an extra video stream. The strict container test covers that case.

## Compatibility pins

Essentia, FFTW 3.3.8, Eigen 3.3.9 and libsamplerate 0.1.8 retain their pinned
sources and required patches. They are owned compatibility dependencies, not
an assertion of vendor LTS. The resampler addresses CVE-2017-7697 with an endpoint
guard. The upstream fix, commit `c3b66186656de44da18b7058aec099dbe782dd0b`, also
changes buffer scheduling and fails exact PCM checks. The local guard preserves
that schedule and skips the invalid coefficient read. An AddressSanitizer and
UndefinedBehaviorSanitizer check covers all ten interpolation sites; the original
code fails the endpoint control. Keep version 0.1.8 in inventories and list the
local patch separately.

Before each release and during the monthly dependency review:

1. Check Debian and upstream advisories for every retained source component.
2. Update the supported decoder branch or backport a specific upstream fix.
   Record the source hash, patch and reason for each compatibility pin.
3. Run the exact PCM, API, playback, decoded-memory and lifecycle gates. Shared
   FFT remains off. Never replace a golden or add a tolerance to accept drift.
4. Scan the exact runtime OS and locked Rust dependencies. Include custom
   libraries in a separate source inventory with build flags and ELF hashes;
   an OS-only scan does not cover them. Record any source-scanner coverage limit.
5. Review new advisories against the compiled components. A component omitted
   from the binary is different from an advisory whose trigger was not tested.
   Keep this evidence separate from scanner results; do not add broad ignores.
6. Publish the exact corresponding source and build inputs, then verify the
   immutable protected preview. Record the image and rollback deployment.

Do not call the entire image vulnerability-free. If a relevant fix cannot
retain the contract, keep the existing deployment and make the compatibility
decision explicit before a production release.

## Sources

- [Debian Bookworm support](https://www.debian.org/releases/bookworm/)
- [Debian Bullseye support](https://www.debian.org/releases/bullseye/)
- [FFmpeg releases](https://ffmpeg.org/download.html)
- [Debian FFmpeg advisories](https://security-tracker.debian.org/tracker/source-package/ffmpeg)
- [libsamplerate CVE-2017-7697](https://security-tracker.debian.org/tracker/CVE-2017-7697)
- [Upstream resampler bounds fix](https://github.com/libsndfile/libsamplerate/commit/c3b66186656de44da18b7058aec099dbe782dd0b)
