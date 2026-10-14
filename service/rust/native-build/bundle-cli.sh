#!/bin/sh
set -eu
prefix=/opt/pax-cli
export LD_LIBRARY_PATH="$prefix/lib"
ldd "$prefix/bin/ffmpeg" > "$prefix/share/pax-build/library-paths.txt"
ldd "$prefix/bin/ffprobe" >> "$prefix/share/pax-build/library-paths.txt"
if grep -q 'not found' "$prefix/share/pax-build/library-paths.txt"; then exit 1; fi
awk '/=> \// {print $3}' "$prefix/share/pax-build/library-paths.txt" | sort -u > /tmp/cli-libraries.txt
mkdir -p "$prefix/share/pax-build/debian"
: > "$prefix/share/pax-build/copied-packages.tsv"
while IFS= read -r lib; do
    case "$(basename "$lib")" in
        libc.so.*|libm.so.*|libdl.so.*|libpthread.so.*|librt.so.*|libresolv.so.*|libstdc++.so.*|libgcc_s.so.*) continue ;;
    esac
    case "$lib" in "$prefix"/*) continue ;; esac
    cp --dereference "$lib" "$prefix/lib/"
    # Debian's merged-/usr aliases can differ from the package database path.
    package=$(dpkg-query --search "$lib" 2>/dev/null | head -n 1 | cut -d: -f1)
    if [ -z "$package" ]; then
        package=$(dpkg-query --search "$(readlink -f "$lib")" | head -n 1 | cut -d: -f1)
    fi
    test -n "$package"
    dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' "$package" >> "$prefix/share/pax-build/copied-packages.tsv"
    if [ -d "/usr/share/doc/$package" ]; then cp -aL "/usr/share/doc/$package" "$prefix/share/pax-build/debian/"; fi
done < /tmp/cli-libraries.txt
find "$prefix/lib" -type f -name '*.so*' -exec patchelf --force-rpath --set-rpath "$prefix/lib" {} \;
patchelf --force-rpath --set-rpath "$prefix/lib" "$prefix/bin/ffmpeg" "$prefix/bin/ffprobe"
rm -rf "$prefix/lib/pkgconfig"
find "$prefix/lib" -type f ! -name '*.so*' -delete
