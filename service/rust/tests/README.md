# Python HTTP compatibility gate

`http_parity.py` uses only the Python standard library on the host and Docker.
Build the **current** Python service, including its existing test stage. Do not
substitute an earlier Python image: framework and URL parsing versions form part
of this gate.

```sh
docker build --platform linux/amd64 -f services/audio-analysis/Dockerfile.python \
  -t pax-audio-analysis:python-reference services/audio-analysis
python3 services/audio-analysis/rust/tests/http_parity.py \
  --python-image pax-audio-analysis:python-reference \
  --rust-image pax-audio-analysis:rust-candidate \
  --output /tmp/audio-analysis-http-parity.json
```

The runner creates an internal Docker network, a local fixture source, and one
container for each service. The runner publishes no ports. The client runs inside the isolated network. The
standard fixture returns 404. Transport cases return synthetic non-audio bodies
with compression, size, or framing conditions. These cases stop at download or
probe and do not reach Essentia. No external request is possible. It compares the response
status, parsed JSON body, contract headers, source request path, and one redacted
JSON log for each request. Duration and generated request IDs are variable; their
type/format and response/log correspondence are checked separately.

The corpus covers authentication, validation order, JSON media types/encodings,
field aliases, ignored fields, URL normalization, compressed-body limits,
malformed/truncated downloads, router redirects, 404/405
responses, request IDs, environment parsing, startup rejection, and health
release values. `--scope request` and `--scope config` permit focused reruns.
Image IDs and the runner SHA-256 are recorded with all results. Failure returns a
nonzero exit status. Temporary containers and the network are removed in `finally`.

This bounded corpus complements the audio golden fixtures and shutdown tests. It
does not prove all possible HTTP inputs, reproduce Vercel routing, or measure
performance. The reference can run under x86 emulation for this functional check.
