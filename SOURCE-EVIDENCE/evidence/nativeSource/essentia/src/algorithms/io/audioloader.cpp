/*
 * Copyright (C) 2006-2021  Music Technology Group - Universitat Pompeu Fabra
 *
 * This file is part of Essentia
 *
 * Essentia is free software: you can redistribute it and/or modify it under
 * the terms of the GNU Affero General Public License as published by the Free
 * Software Foundation (FSF), either version 3 of the License, or (at your
 * option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
 * FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
 * details.
 *
 * You should have received a copy of the Affero GNU General Public License
 * version 3 along with this program.  If not, see http://www.gnu.org/licenses/
 */

#include "audioloader.h"
#include "algorithmfactory.h"
#include <iomanip>  //  setw()

using namespace std;

namespace essentia {
namespace streaming {

const char* AudioLoader::name = essentia::standard::AudioLoader::name;
const char* AudioLoader::category = essentia::standard::AudioLoader::category;
const char* AudioLoader::description = essentia::standard::AudioLoader::description;


AudioLoader::~AudioLoader() {
    closeAudioFile();

    av_freep(&_buffer);
    av_freep(&_md5Encoded);

}

void AudioLoader::configure() {
    // set ffmpeg to be silent by default, so we don't have these annoying
    // "invalid new backstep" messages anymore, when everything is actually fine
    av_log_set_level(AV_LOG_QUIET);
    //av_log_set_level(AV_LOG_VERBOSE);
    _computeMD5 = parameter("computeMD5").toBool();
    _selectedStream = parameter("audioStream").toInt();
    reset();
}


void AudioLoader::openAudioFile(const string& filename) {
    E_DEBUG(EAlgorithm, "AudioLoader: opening file: " << filename);

    // Open file
    int errnum;
    if ((errnum = avformat_open_input(&_demuxCtx, filename.c_str(), NULL, NULL)) != 0) {
        char errorstr[128];
        string error = "Unknown error";
        if (av_strerror(errnum, errorstr, 128) == 0) error = errorstr;
        throw EssentiaException("AudioLoader: Could not open file \"", filename, "\", error = ", error);
    }

    // Retrieve stream information
    if ((errnum = avformat_find_stream_info(_demuxCtx, NULL)) < 0) {
        char errorstr[128];
        string error = "Unknown error";
        if (av_strerror(errnum, errorstr, 128) == 0) error = errorstr;
        avformat_close_input(&_demuxCtx);
        _demuxCtx = 0;
        throw EssentiaException("AudioLoader: Could not find stream information, error = ", error);
    }

    // Dump information about file onto standard error
    //dump_format(_demuxCtx, 0, filename.c_str(), 0);

    // Check that we have only 1 audio stream in the file
    _streams.clear();
    for (int i=0; i<(int)_demuxCtx->nb_streams; i++) {
        if (_demuxCtx->streams[i]->codecpar->codec_type == AVMEDIA_TYPE_AUDIO) {
            _streams.push_back(i);
        }
    }
    int nAudioStreams = _streams.size();
    
    if (nAudioStreams == 0) {
        avformat_close_input(&_demuxCtx);
        _demuxCtx = 0;
        throw EssentiaException("AudioLoader ERROR: found 0 streams in the file, expecting one or more audio streams");
    }

    if (_selectedStream >= nAudioStreams) {
        avformat_close_input(&_demuxCtx);
        _demuxCtx = 0;
        throw EssentiaException("AudioLoader ERROR: 'audioStream' parameter set to ", _selectedStream ,". It should be smaller than the audio streams count, ", nAudioStreams);
    }

    _streamIdx = _streams[_selectedStream];

    // Load corresponding audio codec
    _audioCtx = avcodec_alloc_context3(NULL);
    if (!_audioCtx || avcodec_parameters_to_context(
            _audioCtx, _demuxCtx->streams[_streamIdx]->codecpar) < 0) {
        closeAudioFile();
        throw EssentiaException("AudioLoader: Could not allocate codec context");
    }
    _audioCtx->pkt_timebase = _demuxCtx->streams[_streamIdx]->time_base;
    // Match the shared accepted codecs, including the explicit Zork exclusion.
    static const char* const compatibleCodecs[] = {"aac", "aac_latm", "ac3", "alac", "ape", "atrac1", "atrac3", "eac3", "flac", "gsm", "gsm_ms", "mp1", "mp2", "mp3", "mp3adu", "mp3on4", "mp4als", "musepack7", "musepack8", "pcm_alaw", "pcm_bluray", "pcm_dvd", "pcm_f32be", "pcm_f32le", "pcm_f64be", "pcm_f64le", "pcm_lxf", "pcm_mulaw", "pcm_s16be", "pcm_s16be_planar", "pcm_s16le", "pcm_s16le_planar", "pcm_s24be", "pcm_s24daud", "pcm_s24le", "pcm_s24le_planar", "pcm_s32be", "pcm_s32le", "pcm_s32le_planar", "pcm_s8", "pcm_s8_planar", "pcm_u16be", "pcm_u16le", "pcm_u24be", "pcm_u24le", "pcm_u32be", "pcm_u32le", "pcm_u8", "ra_144", "ra_288", "ralf", "shorten", "tak", "truehd", "tta", "vorbis", "wavpack", "wmalossless", "wmapro", "wmav1", "wmav2", "wmavoice"};
    const string codecName(avcodec_get_name(_audioCtx->codec_id));
    bool supportedCodec = false;
    for (const char* supportedName : compatibleCodecs) {
        if (codecName == supportedName) {
            supportedCodec = true;
            break;
        }
    }
    if (!supportedCodec) {
        closeAudioFile();
        throw EssentiaException("AudioLoader: Unsupported codec!");
    }
    // The wheel retains AAC leading samples and Ogg Vorbis packet tails.
    // FFmpeg 5 added Ogg tail trimming; Matroska DiscardPadding stays active.
    if (_audioCtx->codec_id == AV_CODEC_ID_AAC ||
        (_audioCtx->codec_id == AV_CODEC_ID_VORBIS && string(_demuxCtx->iformat->name) == "ogg")) {
        _audioCtx->flags2 |= AV_CODEC_FLAG2_SKIP_MANUAL;
    }
    _audioCodec = _audioCtx->codec_id == AV_CODEC_ID_MP3 ? avcodec_find_decoder_by_name("mp3") : avcodec_find_decoder(_audioCtx->codec_id);

    if (!_audioCodec) {
        throw EssentiaException("AudioLoader: Unsupported codec!");
    }

    if (avcodec_open2(_audioCtx, _audioCodec, NULL) < 0) {
        throw EssentiaException("AudioLoader: Unable to instantiate codec...");
    }
  
    // Configure format convertion  (no samplerate conversion yet)
    int64_t layout = av_get_default_channel_layout(_audioCtx->channels);

    /*
    const char* fmt = 0;
    get_format_from_sample_fmt(&fmt, _audioCtx->sample_fmt);
    E_DEBUG(EAlgorithm, "AudioLoader: converting from " << (fmt ? fmt : "unknown") << " to FLT");
    */

    E_DEBUG(EAlgorithm, "AudioLoader: using sample format conversion from libswresample");
    _convertCtxAv = swr_alloc();
        
    av_opt_set_int(_convertCtxAv, "in_channel_layout", layout, 0);
    av_opt_set_int(_convertCtxAv, "out_channel_layout", layout, 0);
    av_opt_set_int(_convertCtxAv, "in_sample_rate", _audioCtx->sample_rate, 0);
    av_opt_set_int(_convertCtxAv, "out_sample_rate", _audioCtx->sample_rate, 0);
    av_opt_set_int(_convertCtxAv, "in_sample_fmt", _audioCtx->sample_fmt, 0);
    av_opt_set_int(_convertCtxAv, "out_sample_fmt", AV_SAMPLE_FMT_FLT, 0);

    if (swr_init(_convertCtxAv) < 0) {
        throw EssentiaException("AudioLoader: Could not initialize swresample context");
    }

    av_packet_unref(&_packet);

    _decodedFrame = av_frame_alloc();
    if (!_decodedFrame) {
        throw EssentiaException("AudioLoader: Could not allocate audio frame");
    }

    av_md5_init(_md5Encoded);
}


void AudioLoader::closeAudioFile() {
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


void AudioLoader::pushChannelsSampleRateInfo(int nChannels, Real sampleRate) {
    if (nChannels > 2) {
        throw EssentiaException("AudioLoader: could not load audio. Audio file has more than 2 channels.");
    }
    if (sampleRate <= 0) {
        throw EssentiaException("AudioLoader: could not load audio. Audio sampling rate must be greater than 0.");
    }

    _nChannels = nChannels;

    _channels.push(nChannels);
    _sampleRate.push(sampleRate);
}


void AudioLoader::pushCodecInfo(std::string codec, int bit_rate) {
    _codec.push(codec);
    _bit_rate.push(bit_rate);
}


string uint8_t_to_hex(uint8_t* input, int size) {
    ostringstream result;
    for(int i=0; i<size; ++i) {
        result << setw(2) << setfill('0') << hex << (int) input[i];
    }
    return result.str();
}


AlgorithmStatus AudioLoader::process() {
    if (!parameter("filename").isConfigured()) {
        throw EssentiaException("AudioLoader: Trying to call process() on an AudioLoader algo which hasn't been correctly configured.");
    }

    // read frames until we get a good one
    do {
        // Release skipped non-audio packets before reading the next packet.
        av_packet_unref(&_packet);
        int result = av_read_frame(_demuxCtx, &_packet);
        //E_DEBUG(EAlgorithm, "AudioLoader: called av_read_frame(), got result = " << result);
        if (result != 0) {
            // 0 = OK, < 0 = error or EOF
            if (result != AVERROR_EOF) {
                char errstring[1204];
                av_strerror(result, errstring, sizeof(errstring));
                ostringstream msg;
                msg << "AudioLoader: Error reading frame: " << errstring;
                E_WARNING(msg.str());
            }
            // TODO: should try reading again on EAGAIN error?
            //       https://github.com/FFmpeg/FFmpeg/blob/master/ffmpeg.c
            shouldStop(true);
            flushPacket();
            closeAudioFile();
            if (_computeMD5) {
                av_md5_final(_md5Encoded, _checksum);
                _md5.push(uint8_t_to_hex(_checksum, 16));
            }
            else {
                string md5 = "";
                _md5.push(md5);
            }
            return FINISHED;
        }
    } while (_packet.stream_index != _streamIdx);

    // compute md5 first
    if (_computeMD5) {
        av_md5_update(_md5Encoded, _packet.data, _packet.size);
    }

    decodePacket();
    av_packet_unref(&_packet);
    
    return OK;
}


void AudioLoader::receiveFrames() {
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


void AudioLoader::copyFFmpegOutput() {
    int nsamples = _dataSize / (av_get_bytes_per_sample(AV_SAMPLE_FMT_FLT)  * _nChannels);
    if (nsamples == 0) return;

    // acquire necessary data
    bool ok = _audio.acquire(nsamples);
    if (!ok) {
        throw EssentiaException("AudioLoader: could not acquire output for audio");
    }

    vector<StereoSample>& audio = *((vector<StereoSample>*)_audio.getTokens());

    if (_nChannels == 1) {
        for (int i=0; i<nsamples; i++) {
          audio[i].left() = _buffer[i];
          //audio[i].left() = scale(_buffer[i]);
        }
    }
    else { // _nChannels == 2
      // The output format is always AV_SAMPLE_FMT_FLT, which is interleaved
      for (int i=0; i<nsamples; i++) {
        audio[i].left() = _buffer[2*i];
        audio[i].right() = _buffer[2*i+1];
        //audio[i].left() = scale(_buffer[2*i]);
        //audio[i].right() = scale(_buffer[2*i+1]);
      }
      /*
      // planar
      for (int i=0; i<nsamples; i++) {
          audio[i].left() = scale(_buffer[i]);
          audio[i].right() = scale(_buffer[nsamples+i]);
      }
      */
    }

    // release data
    _audio.release(nsamples);
}

void AudioLoader::reset() {
    Algorithm::reset();

    if (!parameter("filename").isConfigured()) return;

    string filename = parameter("filename").toString();

    closeAudioFile();
    openAudioFile(filename);

    pushChannelsSampleRateInfo(_audioCtx->channels, _audioCtx->sample_rate);
    pushCodecInfo(_audioCodec->name, _audioCtx->bit_rate);
}

} // namespace streaming
} // namespace essentia


namespace essentia {
namespace standard {

const char* AudioLoader::name = "AudioLoader";
const char* AudioLoader::category = "Input/output";
const char* AudioLoader::description = DOC("This algorithm loads the single audio stream contained in a given audio or video file. Supported formats are all those supported by the FFmpeg library including wav, aiff, flac, ogg and mp3.\n"
"\n"
"This algorithm will throw an exception if it was not properly configured which is normally due to not specifying a valid filename. Invalid names comprise those with extensions different than the supported  formats and non existent files. If using this algorithm on Windows, you must ensure that the filename is encoded as UTF-8\n\n"
"Note: ogg files are decoded in reverse phase, due to be using ffmpeg library.\n"
"\n"
"References:\n"
"  [1] WAV - Wikipedia, the free encyclopedia,\n"
"      http://en.wikipedia.org/wiki/Wav\n"
"  [2] Audio Interchange File Format - Wikipedia, the free encyclopedia,\n"
"      http://en.wikipedia.org/wiki/Aiff\n"
"  [3] Free Lossless Audio Codec - Wikipedia, the free encyclopedia,\n"
"      http://en.wikipedia.org/wiki/Flac\n"
"  [4] Vorbis - Wikipedia, the free encyclopedia,\n"
"      http://en.wikipedia.org/wiki/Vorbis\n"
"  [5] MP3 - Wikipedia, the free encyclopedia,\n"
"      http://en.wikipedia.org/wiki/Mp3");


void AudioLoader::createInnerNetwork() {
    _loader = streaming::AlgorithmFactory::create("AudioLoader");
    _audioStorage = new streaming::VectorOutput<StereoSample>();

    _loader->output("audio")           >>  _audioStorage->input("data");
    _loader->output("sampleRate")      >>  PC(_pool, "internal.sampleRate");
    _loader->output("numberChannels")  >>  PC(_pool, "internal.numberChannels");
    _loader->output("md5")             >>  PC(_pool, "internal.md5");
    _loader->output("codec")           >>  PC(_pool, "internal.codec");
    _loader->output("bit_rate")        >>  PC(_pool, "internal.bit_rate");
    _network = new scheduler::Network(_loader);
}

void AudioLoader::configure() {
    _loader->configure(INHERIT("filename"),
                       INHERIT("computeMD5"),
                       INHERIT("audioStream"));
}

void AudioLoader::compute() {
    if (!parameter("filename").isConfigured()) {
        throw EssentiaException("AudioLoader: Trying to call compute() on an "
                                "AudioLoader algo which hasn't been correctly configured.");
    }

    Real& sampleRate = _sampleRate.get();
    int& numberChannels = _channels.get();
    string& md5 = _md5.get();
    int& bit_rate = _bit_rate.get();
    string& codec = _codec.get();
    vector<StereoSample>& audio = _audio.get();

    _audioStorage->setVector(&audio);
    // TODO: is using VectorInput indeed faster than using Pool?

    // FIXME:
    // _audio.reserve(sth_meaningful);

    _network->run();

    sampleRate = _pool.value<Real>("internal.sampleRate");
    numberChannels = (int) _pool.value<Real>("internal.numberChannels");
    md5 = _pool.value<std::string>("internal.md5");
    bit_rate = (int) _pool.value<Real>("internal.bit_rate");
    codec = _pool.value<std::string>("internal.codec");

    // reset, so it is ready to load audio again
    reset();
}

void AudioLoader::reset() {
    _network->reset();
    _pool.remove("internal.md5");
    _pool.remove("internal.sampleRate");
    _pool.remove("internal.numberChannels");
    _pool.remove("internal.codec");
    _pool.remove("internal.bit_rate");
}

} // namespace standard
} // namespace essentia
