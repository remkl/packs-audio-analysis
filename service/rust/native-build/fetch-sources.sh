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

# Keep DSP source/patch pins while rebuilding on the supported base. The native
# decoder uses the current Debian 12 security source, with a minimal closure.
fetch() {
    curl --fail --location --retry 3 --output "/tmp/$1" "$2"
    printf '%s  %s\n' "$3" "/tmp/$1" | sha256sum -c -
}
fetch fftw.tar.gz https://archive.debian.org/debian/pool/main/f/fftw3/fftw3_3.3.8.orig.tar.gz \
    6113262f6e92c5bd474f2875fa1b01054c4ad5040f6b0da7c03c98821d9ae303
fetch fftw-debian.tar.xz https://archive.debian.org/debian/pool/main/f/fftw3/fftw3_3.3.8-2.debian.tar.xz \
    684dede6b4124f309033d128dc7bdf1eb394984e6e8dd79e1fd5d73b95b12461
fetch eigen.tar.bz2 https://archive.debian.org/debian/pool/main/e/eigen3/eigen3_3.3.9.orig.tar.bz2 \
    0fa5cafe78f66d2b501b43016858070d52ba47bd9b1016b0165a7b8e04675677
fetch eigen-debian.tar.xz https://archive.debian.org/debian/pool/main/e/eigen3/eigen3_3.3.9-2.debian.tar.xz \
    f6a4ad0808cc879d6f59b1bd324ceccbd20d20dd58e9c1951c2c4728825b9fe9
fetch ffmpeg.tar.xz https://deb.debian.org/debian/pool/main/f/ffmpeg/ffmpeg_5.1.9.orig.tar.xz \
    d9b593bb2ba93d4b50f74177e0cdcd41747e708596367deed0c30348a71dd176
fetch ffmpeg-debian.tar.xz https://deb.debian.org/debian/pool/main/f/ffmpeg/ffmpeg_5.1.9-0+deb12u1.debian.tar.xz \
    71b20472ecd2764bc98e3229d34c0f7c8dcfe4a6cbacc23fd5604c23bac04632
mkdir /fftw /eigen /ffmpeg
tar -xzf /tmp/fftw.tar.gz --strip-components=1 -C /fftw
tar -xjf /tmp/eigen.tar.bz2 --strip-components=1 -C /eigen
tar -xJf /tmp/ffmpeg.tar.xz --strip-components=1 -C /ffmpeg
for component in fftw eigen ffmpeg; do
    tar -xJf "/tmp/$component-debian.tar.xz" -C "/$component"
    while IFS= read -r name; do
        case "$name" in ''|'#'*) continue ;; esac
        patch -d "/$component" -p1 < "/$component/debian/patches/$name"
    done < "/$component/debian/patches/series"
done
