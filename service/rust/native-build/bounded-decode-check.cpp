// Compile beside native.cpp so these checks exercise the production sink.
#include "native.cpp"
#include <streaming/algorithms/vectorinput.h>
#include <cstring>
#include <iostream>
#include <limits>

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

size_t check_duration_boundaries() {
    size_t checks = 0;
    for (size_t count : {size_t(1), size_t(17), size_t(4095), size_t(4096),
                         size_t(44100), size_t(13230000),
                         max_decoded_samples - 1, max_decoded_samples}) {
        const double exact = static_cast<double>(count) / analysis_sample_rate;
        for (double duration : {std::nextafter(exact, 0.0), exact,
                                std::nextafter(exact, std::numeric_limits<double>::infinity())}) {
            const DecodeLimit limit = decode_limit(duration);
            require(static_cast<double>(limit.samples) / analysis_sample_rate <= duration,
                    "Duration bound accepts too many samples");
            require(limit.samples == max_decoded_samples ||
                        static_cast<double>(limit.samples + 1) / analysis_sample_rate > duration,
                    "Duration bound rejects an accepted sample");
            ++checks;
        }
    }
    require(decode_limit(300).samples == 13230000, "Default duration bound changed");
    require(decode_limit(300).failure_code == 4, "Duration rejection code changed");
    require(decode_limit(std::numeric_limits<double>::max()).samples == max_decoded_samples,
            "Large duration bypasses the PCM ceiling");
    require(decode_limit(std::numeric_limits<double>::max()).failure_code == 5,
            "Memory rejection code changed");
    for (double invalid : {0.0, -1.0, std::numeric_limits<double>::infinity(),
                           std::numeric_limits<double>::quiet_NaN()}) {
        bool rejected = false;
        try { decode_limit(invalid); }
        catch (const EssentiaException&) { rejected = true; }
        require(rejected, "Invalid duration reached decoding");
    }
    return checks;
}

void check_sink_bounds() {
    std::vector<Real> input_audio(5000);
    for (size_t i = 0; i < input_audio.size(); ++i) {
        input_audio[i] = static_cast<Real>(i % 257) / 256.0f - 0.5f;
    }
    for (int code : {4, 5}) {
        for (size_t cap : {size_t(0), size_t(1), size_t(4097), size_t(5000)}) {
            std::vector<Real> output_audio;
            streaming::VectorInput<Real, 64> input(&input_audio);
            BoundedAudioOutput output(&output_audio, {cap, code});
            streaming::connect(input.output("data"), output.input("data"));
            scheduler::Network network(&input, false);
            bool rejected = false;
            try { network.run(); }
            catch (const DecodeLimitExceeded& error) {
                require(error.failure_code == code, "Sink changed the rejection reason");
                rejected = true;
            }
            require(rejected == (cap < input_audio.size()), "Sink boundary mismatch");
            require(output_audio.size() <= cap && output_audio.capacity() <= cap,
                    "Sink allocation exceeded its sample bound");
            require(output_audio.empty() ||
                        std::memcmp(output_audio.data(), input_audio.data(),
                                    output_audio.size() * sizeof(Real)) == 0,
                    "Sink changed sample bytes or order");
        }
    }
}

void check_fixture(const char* filename) {
    std::vector<Real> expected;
    std::unique_ptr<Algorithm> reference(AlgorithmFactory::create(
        "MonoLoader", "filename", std::string(filename), "sampleRate", analysis_sample_rate));
    reference->output("audio").set(expected);
    reference->compute();
    reference.reset();
    require(!expected.empty(), "Fixture must decode to nonempty audio");

    std::vector<Real> actual;
    decode_bounded(filename, actual, decode_limit(300));
    require(expected.size() == actual.size(), "Bounded decoder changed sample count");
    require(std::memcmp(expected.data(), actual.data(), expected.size() * sizeof(Real)) == 0,
            "Bounded decoder changed sample bytes");
    require(actual.capacity() <= decode_limit(300).samples, "Fixture exceeded capacity bound");

    const double duration = static_cast<double>(expected.size()) / analysis_sample_rate;
    std::vector<Real> at_limit;
    decode_bounded(filename, at_limit, decode_limit(duration));
    require(at_limit.size() == expected.size(), "Exact duration boundary was rejected");

    std::vector<Real> over_limit;
    const DecodeLimit limit = decode_limit(std::nextafter(duration, 0.0));
    bool rejected = false;
    try { decode_bounded(filename, over_limit, limit); }
    catch (const DecodeLimitExceeded& error) { rejected = error.failure_code == 4; }
    require(rejected, "Decode accepted samples beyond duration limit");
    require(over_limit.size() <= limit.samples && over_limit.capacity() <= limit.samples,
            "Rejected decode exceeded capacity bound");
}
} // namespace

int main(int argc, char** argv) {
    try {
        require(argc > 1, "Pass valid fixture file paths");
        pax_init();
        const size_t boundaries = check_duration_boundaries();
        check_sink_bounds();
        for (int i = 1; i < argc; ++i) check_fixture(argv[i]);
        std::cout << "{\"passed\":true,\"durationBoundaries\":" << boundaries
                  << ",\"sinkCases\":8,\"exactFixtures\":" << argc - 1
                  << ",\"maxPcmBytes\":" << max_decoded_pcm_bytes << "}" << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
    } catch (...) {
        std::cerr << "Unexpected bounded decoder failure" << std::endl;
    }
    return 1;
}
