"""Match the shared accepted codecs and pinned wheel samples; fail on unknown source."""
import hashlib
import json
from pathlib import Path

from codec_compatibility import COMPATIBLE_AUDIO_CODECS

p = Path('/essentia/src/algorithms/io/audioloader.cpp')
s = p.read_text()
expected = 'a23a53ad238d79c42fb5f375bd35368c22b95745ef6f4cddb94503f67c35fcf4'
if hashlib.sha256(s.encode()).hexdigest() != expected:
    raise SystemExit('Unexpected pinned AudioLoader source')
old = '_audioCodec = avcodec_find_decoder(_audioCtx->codec_id);'
new = '''// Match the shared accepted codecs, including the explicit Zork exclusion.
    static const char* const compatibleCodecs[] = {__COMPATIBLE_CODECS__};
    const string codecName(avcodec_get_name(_audioCtx->codec_id));
    bool supportedCodec = false;
    for (const char* supportedName : compatibleCodecs) {
        if (codecName == supportedName) {
            supportedCodec = true;
            break;
        }
    }
    if (!supportedCodec) {
        avformat_close_input(&_demuxCtx);
        _audioCtx = 0;
        throw EssentiaException("AudioLoader: Unsupported codec!");
    }
    // Its older AAC decoder retains the encoder's leading samples.
    if (_audioCtx->codec_id == AV_CODEC_ID_AAC) {
        _audioCtx->flags2 |= AV_CODEC_FLAG2_SKIP_MANUAL;
    }
    _audioCodec = _audioCtx->codec_id == AV_CODEC_ID_MP3 ? avcodec_find_decoder_by_name("mp3") : avcodec_find_decoder(_audioCtx->codec_id);'''
new = new.replace('__COMPATIBLE_CODECS__', ', '.join(json.dumps(name) for name in sorted(COMPATIBLE_AUDIO_CODECS)))
if s.count(old) != 1:
    raise SystemExit('Unexpected decoder call count')
p.write_text(s.replace(old, new))
