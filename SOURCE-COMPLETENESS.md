# V10 technical source scope

Service commit `ef69b73640d5cb3872ae00fb34f2d27a28f65f29`. All service files are exact. Native,
Cargo and copied Debian source archives are hash-verified. The passed AMD64
gate and final runtime inventory identify the exact checked image.

The custom native and CLI components are included: Essentia, libsamplerate,
FFTW, Eigen, FFmpeg, LAME, zlib and bzip2, with source patches and build scripts.
The copied config.guess/config.sub inputs are included via autotools-dev.
SOURCE-IMAGE-INVENTORY.json records the copied-file hashes, package versions,
licence evidence and source-scope classification. SOURCE-EVIDENCE preserves
those public configuration and licence records. V9 counts are not reused.

Unmodified general-purpose build tools and ordinary system libraries are
classified separately in that inventory. This service-source export does not
claim a separate licence review of distribution of a full base container image.
Sources remain networked build inputs, not an offline/hermetic build system.

Standalone verification passed. The pinned redacted secret scan found only
verified file hashes; the scope scan also found a synthetic HTTP test URL.
No actionable private-input findings remain. The results are recorded in
EXPORT-CHECKS.json. Source inventories do not establish
source-level vulnerability-scanner coverage of manually compiled native code.
The 16-advisory FFmpeg review and libsamplerate guard proof are separate.
This helper performs no commit, push, upload, or deployment.
