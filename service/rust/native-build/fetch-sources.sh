#!/bin/sh
set -eu
# Content hashes cover the complete public source archives.
curl --fail --location --retry 3 --output /tmp/essentia.tar.gz \
  https://codeload.github.com/MTG/essentia/tar.gz/36ec3d929402d491cbbb1120c54631e8c205b0ff
printf '%s  %s\n' 2a6457ff2fc6624fc795714578496639123c14151e6bd85e4aa1879899d0dceb /tmp/essentia.tar.gz | sha256sum -c -
curl --fail --location --retry 3 --output /tmp/samplerate.tar.gz \
  https://archive.debian.org/debian/pool/main/libs/libsamplerate/libsamplerate_0.1.8.orig.tar.gz
printf '%s  %s\n' 93b54bdf46d5e6d2354b7034395fe329c222a966790de34520702bb9642f1c06 /tmp/samplerate.tar.gz | sha256sum -c -
mkdir /essentia /samplerate
tar -xzf /tmp/essentia.tar.gz --strip-components=1 -C /essentia
tar -xzf /tmp/samplerate.tar.gz --strip-components=1 -C /samplerate
