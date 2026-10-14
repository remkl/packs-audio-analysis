#!/bin/sh
set -eu

# Eigen is header-only. Preserve the pinned Debian patches and include path.
mkdir -p /usr/include/eigen3 /usr/share/pkgconfig
cp -a /eigen/Eigen /eigen/unsupported /usr/include/eigen3/
cat > /usr/share/pkgconfig/eigen3.pc <<'EOF'
prefix=/usr
includedir=${prefix}/include/eigen3
Name: Eigen3
Description: Eigen C++ template library
Version: 3.3.9
Cflags: -I${includedir}
EOF

# Preserve Debian 3.3.8 SIMD choices. No AVX2, FMA, native CPU, or fast-math.
cd /fftw
case "$(dpkg --print-architecture)" in
    amd64) simd='--enable-sse --enable-avx' ;;
    arm64) simd='--enable-neon --with-slow-timer' ;;
    *) echo 'Unsupported strict-compat FFTW architecture' >&2; exit 1 ;;
esac
# Autoconf 2.71 renamed this saved user-flags variable. The old macro
# otherwise discards Debian CFLAGS and silently tunes for the build CPU.
python3 - <<'PATCH'
from pathlib import Path
p = Path('m4/ax_cc_maxopt.m4')
s = p.read_text()
old = 'if test "$ac_test_CFLAGS" != "set"; then'
assert s.count(old) == 1, 'Unexpected pinned FFTW compiler macro'
p.write_text(s.replace(old, 'if test "$ac_cv_env_CFLAGS_set" != "set"; then'))
PATCH
autoreconf -fi
CFLAGS='-g -O2 -fstack-protector-strong -Wformat -Werror=format-security' \
CPPFLAGS='-Wdate-time -D_FORTIFY_SOURCE=2' LDFLAGS='-Wl,-z,relro -Wl,--as-needed' \
    ./configure --prefix=/opt/pax-fftw --enable-single --enable-shared \
    --disable-static --enable-threads --enable-openmp --disable-mpi --disable-dependency-tracking $simd
# Stop before compiling if configure discarded the portable arithmetic flags.
grep -Fx 'CFLAGS = -g -O2 -fstack-protector-strong -Wformat -Werror=format-security' Makefile
make -j"${BUILD_JOBS:-2}"
make install
