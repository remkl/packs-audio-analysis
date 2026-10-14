#!/bin/sh
set -eu
# Capture the old FFmpeg/FFTW ABI; use the final Debian's libc and C++ runtime.
mkdir -p /native-runtime /native-runtime-notices/debian
ldd /usr/local/lib/libessentia.so > /tmp/essentia-ldd.txt
if grep -q 'not found' /tmp/essentia-ldd.txt; then cat /tmp/essentia-ldd.txt; exit 1; fi
awk '/=> \// {print $3}' /tmp/essentia-ldd.txt | sort -u > /tmp/native-libraries.txt
while IFS= read -r lib; do
    case "$(basename "$lib")" in
        libc.so.*|libm.so.*|libdl.so.*|libpthread.so.*|librt.so.*|libresolv.so.*|libstdc++.so.*|libgcc_s.so.*) continue ;;
    esac
    case "$lib" in /opt/pax-samplerate/*) continue ;; esac
    cp --parents --dereference "$lib" /native-runtime/
    package=$(dpkg-query --search "$lib" | head -n 1 | cut -d: -f1)
    if [ -d "/usr/share/doc/$package" ]; then
        cp -a "/usr/share/doc/$package" /native-runtime-notices/debian/
    fi
done < /tmp/native-libraries.txt
triplet=$(gcc -dumpmachine)
rpath="/opt/compat-lib:/opt/control/lib/$triplet:/opt/control/usr/lib/$triplet:/usr/local/lib"
# DT_RPATH covers transitive dependencies and is effective in secure execution.
# A clean ffmpeg exec sees only its Bookworm system libraries.
find /native-runtime -type f -exec patchelf --force-rpath --set-rpath "$rpath" {} \;
patchelf --force-rpath --set-rpath "$rpath" /usr/local/lib/libessentia.so
mkdir -p /native-runtime-notices
cp /tmp/essentia-ldd.txt /native-runtime-notices/native-library-paths.txt
dpkg-query -W > /native-runtime-notices/builder-packages.txt
# Preserve the dependency source package licence files alongside the private ABI.
