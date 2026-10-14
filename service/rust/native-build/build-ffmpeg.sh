#!/bin/sh
set -eu
case "${1:-}" in
    native) prefix=/opt/pax-ffmpeg; programs='--disable-programs --disable-avfilter' ;;
    cli) prefix=/opt/pax-cli; programs='--disable-ffplay --enable-ffmpeg --enable-ffprobe --enable-avfilter --disable-filters --enable-filter=aresample --enable-libmp3lame --enable-encoder=libmp3lame --enable-muxer=mp3' ;;
    *) echo 'Expected native or cli build stage' >&2; exit 1 ;;
esac
cd /ffmpeg
if [ -f ffbuild/config.mak ]; then make distclean; fi
decoders=$(python3 /tmp/configure-decoders.py)
# Keep Debian's compiler hardening and internal audio implementations. Zlib and
# bzip2 preserve compressed MOV metadata and Matroska audio blocks.
./configure --prefix="$prefix" --extra-version=0+deb12u1-pax1 --toolchain=hardened \
    --enable-shared --disable-static --disable-autodetect --disable-doc --disable-debug \
    --disable-network --disable-avdevice --disable-swscale --disable-postproc \
    --disable-encoders --disable-muxers --disable-hwaccels \
    --disable-v4l2-m2m --disable-vaapi --disable-vdpau --disable-vulkan \
    --disable-protocols --enable-protocol=file --enable-pic --enable-zlib --enable-bzlib \
    --disable-decoders --enable-decoder="$decoders" \
    --disable-demuxers --enable-demuxer=wav,aiff,mp3,flac,ogg,mov,matroska \
    --disable-parser=dvbsub $programs
make -j"${BUILD_JOBS:-2}"
make install
LD_LIBRARY_PATH="$prefix/lib" python3 /tmp/check-decoder-registry.py "$prefix"
python3 /tmp/check-ffmpeg-config.py "$prefix"
