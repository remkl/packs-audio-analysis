# V3 source completeness

Frozen service commit: `435c4b2c829300a398f2ec83d7eb7592b742bfdb`. Local export only; no publication or deployed
service change. Native maintenance remains an open release item.

Included and verified:

- All 62 tracked files from the service tree, unchanged; no private history.
- Exact Essentia and libsamplerate archives, verified against the fetch script.
- All 156 Cargo registry archives, verified against Cargo.lock.
- All 92 source packages for the 111 copied native Debian binary packages:
  300 files, 423,024,626 bytes, with upstream sources and Debian patch sets.
- Exact playback FFmpeg 5.1.9 source package and exact Eigen/autotools-dev source
  inputs. This includes the source that supplies copied config.guess/config.sub.
- Six Debian source indexes and signature records. All indexed artifact hashes
  and sizes were checked. Bullseye signatures use the matching native-builder
  keyring; Bookworm uses the matching runtime keyring. No signature check was
  bypassed. Current native source-version mapping has no unresolved entry.

The separate Bookworm runtime/base inventory has 291 installed binaries mapped
to 206 exact source packages in signed indexes. Playback FFmpeg plus ten exact
source versions shared with the native closure are bundled: 11 of 206 sets.
The other 195 sets are outside this collection. The JSON inventory names the
covered sets. This is a technical source-scope record, not a
legal conclusion or a claim that every base/system source must be published.

Other open scope items: compiler/sysroot and general build-tool sources;
Python reference dependency sources and the wheel's embedded native sources;
anonymous public source delivery; final source-to-release linkage; and the
owner's licence/publication review. The Python reference is included for tests;
the Rust Essentia archive is not asserted to rebuild the pinned Python wheel.

Builds fetch public dependencies and are not fully offline or hermetic. The
export script checked source identity, hashes, Python syntax, Docker COPY inputs
and gate CLI parsing. The already-completed strict AMD64 gate passed, and all
54 gate input hashes match the export. `STRICT-GATE.json` records those image
IDs and hashes. The export did not repeat that long gate.

Debian11 LTS ended on 2026-08-31, and upstream FFmpeg4.3 is an old release branch.
Archive signatures establish source provenance, not future maintenance. Do not
claim the current source set resolves that release risk. A supported-source
migration needs a separate strict PCM experiment; no ABI change is in this V3.

The original V1 archive and V2 working set were preserved. No missing source or
compatibility result was waived to create this export.
