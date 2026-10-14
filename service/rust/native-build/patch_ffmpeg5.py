"""Port the pinned decoder to FFmpeg 5's owned context and send/receive API.

DSP algorithms, sample conversion, decoder choices and codec policy stay in
their owning files. The unused native writers are excluded; playback uses CLI.
"""
import hashlib
from pathlib import Path


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise SystemExit('Unexpected pinned FFmpeg adapter source: ' + old[:80])
    return source.replace(old, new)


root = Path('/essentia')
header_path = root / 'src/algorithms/io/audioloader.h'
header = header_path.read_text()
if hashlib.sha256(header.encode()).hexdigest() != '2a068e94a4531081569d0e1909b0ea775387dafba17e14365332deba515a9435':
    raise SystemExit('Unexpected pinned AudioLoader header')
header = replace_once(header, '  AVCodec* _audioCodec;', '  const AVCodec* _audioCodec;')
header = replace_once(header, '_audioCtx(0), _audioCodec(0), _decodedFrame(0),',
                      '_audioCtx(0), _audioCodec(0), _packet(), _decodedFrame(0),')
header = replace_once(header, '    av_register_all();', '    // FFmpeg 5 registers codecs and formats automatically.')
header = replace_once(header, '''  int decode_audio_frame(AVCodecContext* audioCtx, float* output,
                         int* outputSize, AVPacket* packet);
  int decodePacket();''', '''  void receiveFrames();
  void decodePacket();''')
header_path.write_text(header)

p = root / 'src/algorithms/io/audioloader.cpp'
s = p.read_text()  # patch_decoder.py already checks the complete original hash.
s = replace_once(s, '    av_freep(&_decodedFrame);', '')
s = replace_once(s, '_demuxCtx->streams[i]->codec->codec_type', '_demuxCtx->streams[i]->codecpar->codec_type')
s = replace_once(s, '    _audioCtx = _demuxCtx->streams[_streamIdx]->codec;', '''    _audioCtx = avcodec_alloc_context3(NULL);
    if (!_audioCtx || avcodec_parameters_to_context(
            _audioCtx, _demuxCtx->streams[_streamIdx]->codecpar) < 0) {
        closeAudioFile();
        throw EssentiaException("AudioLoader: Could not allocate codec context");
    }
    _audioCtx->pkt_timebase = _demuxCtx->streams[_streamIdx]->time_base;''')
s = replace_once(s, '''        avformat_close_input(&_demuxCtx);
        _audioCtx = 0;
        throw EssentiaException("AudioLoader: Unsupported codec!");''', '''        closeAudioFile();
        throw EssentiaException("AudioLoader: Unsupported codec!");''')
s = replace_once(s, '    av_init_packet(&_packet);', '    av_packet_unref(&_packet);')
start = s.index('void AudioLoader::closeAudioFile() {')
end = s.index('\n\nvoid AudioLoader::pushChannelsSampleRateInfo', start)
s = s[:start] + '''void AudioLoader::closeAudioFile() {
    // Context, packet and frame ownership is independent of the demuxer. This
    // also cleans partial open failures and makes repeated reset safe.
    av_packet_unref(&_packet);
    av_frame_free(&_decodedFrame);
    swr_free(&_convertCtxAv);
    avcodec_free_context(&_audioCtx);
    avformat_close_input(&_demuxCtx);
    _audioCodec = 0;
    _streams.clear();
}
''' + s[end:]
s = replace_once(s, '''    do {
        int result = av_read_frame(_demuxCtx, &_packet);''', '''    do {
        // Release skipped non-audio packets before reading the next packet.
        av_packet_unref(&_packet);
        int result = av_read_frame(_demuxCtx, &_packet);''')
s = replace_once(s, '''    // decode frames in packet
    while(_packet.size > 0) {
        if (!decodePacket()) break;
        copyFFmpegOutput();
    }
    // neds to be freed !!
    av_free_packet(&_packet);''', '''    decodePacket();
    av_packet_unref(&_packet);''')
start = s.index('int AudioLoader::decode_audio_frame(')
end = s.index('void AudioLoader::copyFFmpegOutput()', start)
s = s[:start] + '''void AudioLoader::receiveFrames() {
    for (;;) {
        av_frame_unref(_decodedFrame);
        const int result = avcodec_receive_frame(_audioCtx, _decodedFrame);
        if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return;
        if (result < 0) {
            E_WARNING("AudioLoader: error while decoding, skipping frame");
            return;
        }
        const int inputSamples = _decodedFrame->nb_samples;
        if (inputSamples == 0) continue;
        if (inputSamples < 0 || _decodedFrame->channels != _nChannels ||
            _decodedFrame->format != _audioCtx->sample_fmt) {
            throw EssentiaException("AudioLoader: Invalid decoded frame format");
        }
        const int inputPlaneSize = av_samples_get_buffer_size(
            NULL, _nChannels, inputSamples, _audioCtx->sample_fmt, 1);
        const int outputPlaneSize = av_samples_get_buffer_size(
            NULL, _nChannels, inputSamples, AV_SAMPLE_FMT_FLT, 1);
        const int outputBufferSamples = FFMPEG_BUFFER_SIZE /
            (av_get_bytes_per_sample(AV_SAMPLE_FMT_FLT) * _nChannels);
        if (inputPlaneSize < 0 || outputPlaneSize < 0 ||
            outputBufferSamples < inputSamples) {
            throw EssentiaException("AudioLoader: Insufficient buffer size for format conversion");
        }
        if (_audioCtx->sample_fmt == AV_SAMPLE_FMT_FLT) {
            memcpy(_buffer, _decodedFrame->data[0], inputPlaneSize);
        }
        else {
            float* output = _buffer;
            const int written = swr_convert(_convertCtxAv, (uint8_t**) &output,
                outputBufferSamples, (const uint8_t**) _decodedFrame->data, inputSamples);
            if (written < inputSamples) {
                throw EssentiaException("AudioLoader: Incomplete format conversion (some samples missing)");
            }
        }
        _dataSize = outputPlaneSize;
        copyFFmpegOutput();
    }
}


void AudioLoader::decodePacket() {
    int result = avcodec_send_packet(_audioCtx, &_packet);
    if (result == AVERROR(EAGAIN)) {
        receiveFrames();
        result = avcodec_send_packet(_audioCtx, &_packet);
    }
    if (result < 0) {
        E_WARNING("AudioLoader: error while decoding, skipping packet");
        return;
    }
    // One packet can emit multiple frames. Drain it before accepting another.
    receiveFrames();
}


void AudioLoader::flushPacket() {
    int result = avcodec_send_packet(_audioCtx, NULL);
    if (result == AVERROR(EAGAIN)) {
        receiveFrames();
        result = avcodec_send_packet(_audioCtx, NULL);
    }
    if (result < 0 && result != AVERROR_EOF) {
        E_WARNING("AudioLoader: decoding error while flushing a packet");
        return;
    }
    receiveFrames();
}


''' + s[end:]
p.write_text(s)

wscript_path = root / 'src/wscript'
wscript = wscript_path.read_text()
if hashlib.sha256(wscript.encode()).hexdigest() != '6b081a4f223d4572a81ec5351656462b0a8f39a35d8520c5a09249cdc7b51c12':
    raise SystemExit('Unexpected pinned Essentia build definition')
wscript = replace_once(wscript, '    # MonoLoader, EqloudLoader, and EasyLoader are dependent on Resample',
    '''    # PAX encodes playback with the isolated system CLI, never AudioContext.
    ctx.env.ALGOIGNORE += ['MonoWriter', 'AudioWriter']

    # MonoLoader, EqloudLoader, and EasyLoader are dependent on Resample''')
wscript = replace_once(wscript, "    if 'AVCODEC' not in ctx.env.USE_LIBS:\n        sources = [ s for s in sources if 'audiocontext' not in str(s) ]",
    "    sources = [ s for s in sources if 'audiocontext' not in str(s) ]")
wscript_path.write_text(wscript)
print('Pinned FFmpeg 5 decoder adapter applied; native writers excluded')
