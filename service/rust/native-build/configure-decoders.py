"""Translate codec IDs to internal FFmpeg configure names without video codecs."""
from pathlib import Path
import re

from codec_compatibility import COMPATIBLE_AUDIO_CODECS

aliases = {'mp4als': 'als', 'musepack7': 'mpc7', 'musepack8': 'mpc8'}
decoders = {aliases.get(codec, codec) for codec in COMPATIBLE_AUDIO_CODECS}
# Retain both built-in implementations so avcodec_find_decoder keeps its normal
# preference. The loader deliberately selects integer mp3 by name.
decoders.update({'aac_fixed', 'ac3_fixed', 'mp1float', 'mp2float', 'mp3float',
                 'mp3adufloat', 'mp3on4float'})
registered = set(re.findall(r'extern const FFCodec ff_(\w+)_decoder;',
                          Path('/ffmpeg/libavcodec/allcodecs.c').read_text()))
if decoders - registered:
    raise SystemExit('Unknown FFmpeg decoder aliases: ' + ', '.join(sorted(decoders - registered)))
print(','.join(sorted(decoders)))
