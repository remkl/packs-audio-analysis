#include <essentia.h>
#include <algorithmfactory.h>
#include <scheduler/network.h>
#include <streaming/algorithms/vectoroutput.h>
#include <chrono>
#include <cmath>
#include <algorithm>
#include <memory>
#include <new>
#include <cstdio>
#include <cstdlib>
#include <malloc.h>
using namespace essentia;
using namespace essentia::standard;
using Clock = std::chrono::steady_clock;

namespace {
constexpr double analysis_sample_rate = 44100.0;
constexpr size_t max_decoded_pcm_bytes = 256 * 1024 * 1024;
static_assert(sizeof(Real) == 4, "The PCM bound requires float32 Essentia");
constexpr size_t max_decoded_samples = max_decoded_pcm_bytes / sizeof(Real);

struct DecodeLimit {
    size_t samples;
    int failure_code;
};

struct DecodeLimitExceeded {
    int failure_code;
};

DecodeLimit decode_limit(double max_duration_seconds) {
    if (!std::isfinite(max_duration_seconds) || max_duration_seconds <= 0) {
        throw EssentiaException("Invalid maximum audio duration");
    }
    // Search with the existing duration comparison itself. Multiplication and
    // floor can move a floating-point boundary by one accepted sample.
    size_t low = 0;
    size_t high = max_decoded_samples;
    while (low < high) {
        const size_t middle = low + (high - low + 1) / 2;
        if (static_cast<double>(middle) / analysis_sample_rate <= max_duration_seconds) {
            low = middle;
        } else {
            high = middle - 1;
        }
    }
    // Code 5 distinguishes the absolute PCM ceiling from configured duration.
    const bool memory_is_first =
        static_cast<double>(max_decoded_samples + 1) / analysis_sample_rate <= max_duration_seconds;
    return {low, memory_is_first ? 5 : 4};
}

class BoundedAudioOutput : public streaming::VectorOutput<Real> {
    const DecodeLimit limit_;

public:
    BoundedAudioOutput(std::vector<Real>* audio, DecodeLimit limit)
        : streaming::VectorOutput<Real>(audio), limit_(limit) {}

    streaming::AlgorithmStatus process() override {
        // Match VectorOutput's token acquisition exactly. This leaves the
        // pinned decoder, downmix, resampler and their scheduling unchanged.
        int ntokens = std::min(_data.available(), _data.buffer().bufferInfo().maxContiguousElements);
        ntokens = std::max(1, ntokens);
        if (!_data.acquire(ntokens)) return streaming::NO_INPUT;

        const size_t count = static_cast<size_t>(ntokens);
        if (_v->size() > limit_.samples || count > limit_.samples - _v->size()) {
            throw DecodeLimitExceeded{limit_.failure_code};
        }
        const size_t current = _v->size();
        const size_t required = current + count;
        if (required > _v->capacity()) {
            // Grow lazily, without vector::resize's unchecked geometric growth.
            // During reserve, old and new allocations can coexist temporarily.
            // Start with one pinned Resample input block (4096 samples), then
            // double. At the 256 MiB ceiling, relocation needs at most 384 MiB.
            size_t capacity = std::min(limit_.samples, std::max(size_t(4096), _v->capacity()));
            while (capacity < required) capacity = std::min(limit_.samples, capacity * 2);
            _v->reserve(capacity);
        }
        _v->resize(required);
        fastcopy(_v->data() + current, &_data.firstToken(), ntokens);
        _data.release(ntokens);
        return streaming::OK;
    }
};

void decode_bounded(const char* filename, std::vector<Real>& audio, DecodeLimit limit) {
    std::unique_ptr<streaming::Algorithm> loader(streaming::AlgorithmFactory::create(
        "MonoLoader", "filename", std::string(filename), "sampleRate", analysis_sample_rate));
    BoundedAudioOutput output(&audio, limit);
    streaming::connect(loader->output("audio"), output.input("data"));
    // Explicit owners remain alive until the scheduler and all connections have
    // been destroyed, including when the sink stops an over-limit decode.
    scheduler::Network network(loader.get(), false);
    network.run();
}
} // namespace

struct Metrics {
    const float* samples;
    size_t count;
    void* owner;
    float bpm, bpm_confidence, key_confidence;
    char key[64], scale[16];
    double decode_ms, rhythm_ms, key_ms;
};
extern "C" void pax_init() {
    // Fix the measured allocation policy before native DSP allocations.
    mallopt(M_MMAP_THRESHOLD, 128 * 1024);
    mallopt(M_TRIM_THRESHOLD, 128 * 1024);
    mallopt(M_ARENA_MAX, 1);
    essentia::init();
}
extern "C" void pax_free(void* owner) { delete static_cast<std::vector<Real>*>(owner); }
extern "C" int pax_analyze(const char* filename, double max_duration_seconds, Metrics* result) {
    int failure_code = 1;
    // Keep request audio alive until all algorithm destructors have run.
    std::unique_ptr<std::vector<Real>> audio;
    try {
        // Fresh native objects only; experimental environment flags are ignored.
        audio.reset(new std::vector<Real>);
        auto& factory = AlgorithmFactory::instance();
        auto start = Clock::now();
        decode_bounded(filename, *audio, decode_limit(max_duration_seconds));
        auto loaded = Clock::now();
        if (audio->empty()) return 1;
        if (!std::all_of(audio->begin(), audio->end(), [](Real sample) { return std::isfinite(sample); })) return 1;
        failure_code = 2;
        Real bpm = 0, confidence = 0;
        std::vector<Real> ticks, estimates, intervals;
        std::unique_ptr<Algorithm> rhythm(factory.create("RhythmExtractor2013", "method", "multifeature"));
        rhythm->input("signal").set(*audio);
        rhythm->output("bpm").set(bpm);
        rhythm->output("ticks").set(ticks);
        rhythm->output("confidence").set(confidence);
        rhythm->output("estimates").set(estimates);
        rhythm->output("bpmIntervals").set(intervals);
        rhythm->compute();
        auto rhythmic = Clock::now();
        rhythm.reset();
        std::vector<Real>().swap(ticks);
        std::vector<Real>().swap(estimates);
        std::vector<Real>().swap(intervals);
        std::string key, scale;
        Real strength = 0;
        std::unique_ptr<Algorithm> key_extractor(factory.create("KeyExtractor"));
        key_extractor->input("audio").set(*audio);
        key_extractor->output("key").set(key);
        key_extractor->output("scale").set(scale);
        key_extractor->output("strength").set(strength);
        key_extractor->compute();
        auto keyed = Clock::now();
        key_extractor.reset();
        result->bpm = bpm;
        result->bpm_confidence = confidence;
        result->key_confidence = strength;
        std::snprintf(result->key, sizeof(result->key), "%s", key.c_str());
        std::snprintf(result->scale, sizeof(result->scale), "%s", scale.c_str());
        result->decode_ms = std::chrono::duration<double, std::milli>(loaded-start).count();
        result->rhythm_ms = std::chrono::duration<double, std::milli>(rhythmic-loaded).count();
        result->key_ms = std::chrono::duration<double, std::milli>(keyed-rhythmic).count();
        result->samples = audio->data();
        result->count = audio->size();
        result->owner = audio.release();
        return 0;
    } catch (const DecodeLimitExceeded& limit) {
        return limit.failure_code;
    } catch (const std::bad_alloc&) {
        return 3;
    } catch (...) {
        return failure_code;
    }
}

extern "C" int pax_decode(const char* filename, Metrics* result) {
    try {
        auto audio = std::unique_ptr<std::vector<Real>>(new std::vector<Real>);
        decode_bounded(filename, *audio, {max_decoded_samples, 5});
        if (audio->empty()) return 1;
        result->samples = audio->data();
        result->count = audio->size();
        result->owner = audio.release();
        return 0;
    } catch (const DecodeLimitExceeded& limit) { return limit.failure_code; }
    catch (...) { return 1; }
}
