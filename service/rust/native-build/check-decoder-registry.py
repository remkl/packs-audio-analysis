"""Fail the build unless its actual registry contains exactly accepted audio IDs."""
from pathlib import Path
import subprocess
import sys

from codec_compatibility import COMPATIBLE_AUDIO_CODECS

prefix = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/opt/pax-ffmpeg')

source = r'''
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavcodec/codec_desc.h>
}
#include <iostream>
int main() {
    void* cursor = nullptr;
    const AVCodec* codec;
    while ((codec = av_codec_iterate(&cursor))) {
        if (!av_codec_is_decoder(codec)) continue;
        const AVCodecDescriptor* descriptor = avcodec_descriptor_get(codec->id);
        if (!descriptor || codec->type != AVMEDIA_TYPE_AUDIO) return 2;
        std::cout << descriptor->name << '\t' << codec->name << '\n';
    }
}
'''
Path('/tmp/decoder-registry.cpp').write_text(source)
subprocess.run(['g++', '-std=c++11', '-I' + str(prefix / 'include'),
                '/tmp/decoder-registry.cpp', '-L' + str(prefix / 'lib'),
                '-lavcodec', '-o', '/tmp/decoder-registry'], check=True)
result = subprocess.run(['/tmp/decoder-registry'], check=True, text=True, capture_output=True)
rows = [row.split('\t') for row in result.stdout.splitlines()]
actual = {row[0] for row in rows}
if actual != COMPATIBLE_AUDIO_CODECS:
    raise SystemExit('Native decoder registry differs: missing=' + str(sorted(COMPATIBLE_AUDIO_CODECS - actual)) +
                     ' extra=' + str(sorted(actual - COMPATIBLE_AUDIO_CODECS)))
if ['mp3', 'mp3'] not in rows:
    raise SystemExit('Required integer MP3 decoder is absent')
(prefix / 'decoder-registry.tsv').write_text(result.stdout)
print('Native decoder registry: exactly ' + str(len(actual)) + ' accepted audio codec IDs; no video decoders')
