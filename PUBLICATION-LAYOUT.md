# Source publication layout

`service/` is the unchanged source at PAX commit435c4b2c829300a398f2ec83d7eb7592b742bfdb.
The root Python files and Render blueprint retain the existing repository history.
Use `service/Dockerfile.vercel` for the Rust candidate.

`PUBLIC-SHA256SUMS` covers this Git publication, excluding itself.
`EXPORT-*`, `FILES.json`, `SHA256SUMS` and `FULL-ARCHIVE-SHA256SUMS` retain the
original export provenance and describe the full release archive. They do not
claim the thin Git clone contains dependency archives.

The source release contains the full source archive and a separate dependency
archive. Both preserve upstream sources and their licence files.
