#!/bin/sh
set -eu
cd /samplerate
flags='-O1 -g -ffp-contract=off -fsanitize=address,undefined -fno-omit-frame-pointer'
cc $flags -DHAVE_CONFIG_H -I/samplerate -I/samplerate/src \
    /tmp/samplerate-endpoint-check.c -lm -o /tmp/samplerate-endpoint-safe
cc $flags -DHAVE_CONFIG_H -I/samplerate -I/samplerate/src \
    -DSINC_SOURCE='"/tmp/src_sinc_original.c"' \
    /tmp/samplerate-endpoint-check.c -lm -o /tmp/samplerate-endpoint-original
for channels in 1 2 4 6 8; do
    ASAN_OPTIONS=detect_leaks=0 /tmp/samplerate-endpoint-safe "$channels"
done
# A vulnerable source control proves that this case reaches the original bug.
# Neither sanitizer executable nor the original source is copied into runtime.
set +e
ASAN_OPTIONS=detect_leaks=0 /tmp/samplerate-endpoint-original 1 > /tmp/samplerate-endpoint-original.log 2>&1
status=$?
set -e
test "$status" -ne 0
grep -m 1 'ERROR: AddressSanitizer: heap-buffer-overflow' /tmp/samplerate-endpoint-original.log
