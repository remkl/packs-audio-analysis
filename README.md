# PAX audio analysis

Source for the Rust/Axum audio-analysis candidate and its retained Python
reference. Essentia supplies the native analysis engine. Shared FFT is off.

This release corresponds to PAX source commit
`435c4b2c829300a398f2ec83d7eb7592b742bfdb`. Production has not been switched to
this candidate. The source is published for review and reproducible comparison;
publication is not deployment approval.

## Source and dependency archives

`service/` contains all 62 tracked service files from that commit, unchanged.
It includes the Rust server, C++ bridge, Python reference, native patches,
Dockerfiles, tests, synthetic fixtures and licence notices.

[Dependency source release](https://github.com/remkl/packs-audio-analysis/releases/tag/source-435c4b2c8293)
contains the exact Essentia and libsamplerate sources, Debian native dependency
sources and patches, playback FFmpeg source, build-header sources, and all 156
locked Rust crate archives. Verify its SHA-256 with
`DEPENDENCY-ARCHIVE-SHA256SUMS`, then extract it beside `service/`.
The archive digest is `a35f81cff13e76d75f83e147728dd5570688138a0848152174d05646ca347eb7`.

`EXPORT-PROVENANCE.json` binds every service file to the source commit.
`DEPENDENCY-SOURCES.json` and the package inventories record source locations,
versions and integrity checks. `SOURCE-COMPLETENESS.md` states the scope and
remaining base/system-library review; this publication does not assert that
all legal review is complete. No private monorepo history is included.

## Build and check

Use Docker Buildx and Python 3.10 or later. From this repository root:

```sh
docker buildx build --load --platform linux/amd64 --target runtime \
  -f service/Dockerfile.vercel -t pax-audio-analysis:local service
python3 service/rust/check.py --architecture amd64 --build-python
```

The check builds the Python reference and runs strict audio, request/config,
process-lifecycle and resource-bound tests. No R2 or production credentials are
needed. On an ARM Mac, AMD64 tests use emulation and do not measure native
AMD64 speed. The Dockerfiles fetch public dependencies; the archive preserves
source inputs but does not make the build fully offline or hermetic.

The manual GitHub workflow runs the same local gate. It does not deploy.
See [service documentation](service/README.md) and
[native build details](service/rust/README.md).

## Release status

The frozen local compatibility gate passed. Runtime, native, Python, Cargo
and pip checks have no fixable high/critical findings under the existing gate.
This is not a zero-vulnerability claim: unfixed advisories and the maintenance
path for the Debian 11 / FFmpeg 4.3 native bundle still need review before a
production cutover. No baseline tolerance was relaxed.

The historical root Python files, `Dockerfile` and `render.yaml` remain unchanged
to preserve the old Render blueprint during source publication. Use the explicit
`service/Dockerfile.vercel` build above. Source publication commits use
`[skip render]`; do not trigger a Render or Vercel deployment from this repository
without explicit operator approval.

## Licence

The service is AGPL-3.0-only. See [LICENSE](LICENSE),
[service licence](service/LICENSE), and [native notices](service/rust/NOTICE.md).
Dependency archives retain their upstream licence files and Debian patches.
