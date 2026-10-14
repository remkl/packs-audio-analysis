#include <essentia.h>
#include <algorithmfactory.h>
#include <onsetdetectionglobal.h>
#include <beattrackermultifeature.h>
#include <paxenginescope.h>
#include <poolstorage.h>
#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <dirent.h>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <vector>

using essentia::Real;
using essentia::standard::Algorithm;
using essentia::standard::AlgorithmFactory;
typedef std::vector<Real> Signal;
typedef std::unique_ptr<Algorithm> Owned;
static_assert(sizeof(Real) == 4, "Probe requires float32 Essentia");

struct Counts {
  size_t cases = 0, fixtures = 0, synthetic = 0, fftFrames = 0;
  size_t modeComparisons = 0, candidateVectors = 0, errorComparisons = 0;
  size_t reconfigurationComparisons = 0, recoveryComparisons = 0;
  size_t boundScopeUnwindComparisons = 0;
} counts;

std::string quoted(const std::string& s) {
  std::ostringstream out;
  out << '"';
  for (unsigned char c : s) {
    if (c == '"' || c == '\\') out << '\\' << c;
    else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c);
    else out << c;
  }
  return out.str() + '"';
}
void require(bool value, const std::string& message) {
  if (!value) throw std::runtime_error(message);
}
void mode(const char* name) { require(setenv("PAX_ENGINE_MODE", name, 1) == 0, "setenv failed"); }

template<class T> void exact(const std::vector<T>& a, const std::vector<T>& b, const std::string& label) {
  require(a.size() == b.size(), label + " length mismatch");
  if (!a.empty() && std::memcmp(a.data(), b.data(), a.size() * sizeof(T))) {
    size_t i = 0;
    while (i < a.size() && !std::memcmp(&a[i], &b[i], sizeof(T))) ++i;
    throw std::runtime_error(label + " bit mismatch at " + std::to_string(i));
  }
}
void exact(Real a, Real b, const std::string& label) {
  require(std::memcmp(&a, &b, sizeof(Real)) == 0, label + " scalar bit mismatch");
}
uint64_t hashBytes(uint64_t h, const void* data, size_t bytes) {
  const unsigned char* p = static_cast<const unsigned char*>(data);
  for (size_t i=0; i<bytes; ++i) { h ^= p[i]; h *= UINT64_C(1099511628211); }
  return h;
}
template<class T> uint64_t hashVector(uint64_t h, const std::vector<T>& v) {
  uint64_t n = v.size();
  h = hashBytes(h, &n, sizeof(n));
  return v.empty() ? h : hashBytes(h, v.data(), v.size()*sizeof(T));
}
std::string hex(uint64_t n) {
  std::ostringstream out; out << std::hex << std::setw(16) << std::setfill('0') << n; return out.str();
}
const uint64_t initialHash = UINT64_C(14695981039346656037);

// Independent reference chains match the two existing global-onset algorithms:
// FrameCutter -> Windowing -> Spectrum versus FrameCutter -> Windowing -> FFT -> CartesianToPolar.
uint64_t fftParity(const Signal& audio, size_t& frameCount) {
  Owned cutA(AlgorithmFactory::create("FrameCutter", "frameSize", 2048, "hopSize", 512, "startFromZero", true));
  Owned cutB(AlgorithmFactory::create("FrameCutter", "frameSize", 2048, "hopSize", 512, "startFromZero", true));
  Owned winA(AlgorithmFactory::create("Windowing", "size", 2048, "zeroPadding", 0, "type", "hann"));
  Owned winB(AlgorithmFactory::create("Windowing", "size", 2048, "zeroPadding", 0, "type", "hann"));
  Owned fftA(AlgorithmFactory::create("FFT", "size", 2048));
  Owned fftB(AlgorithmFactory::create("FFT", "size", 2048));
  Owned spectrumA(AlgorithmFactory::create("Spectrum", "size", 2048));
  Owned polarB(AlgorithmFactory::create("CartesianToPolar"));
  Signal frameA, frameB, windowA, windowB, spectrum, magnitude, phase;
  std::vector<std::complex<Real> > complexA, complexB;
  cutA->input("signal").set(audio); cutA->output("frame").set(frameA);
  cutB->input("signal").set(audio); cutB->output("frame").set(frameB);
  winA->input("frame").set(frameA); winA->output("frame").set(windowA);
  winB->input("frame").set(frameB); winB->output("frame").set(windowB);
  fftA->input("frame").set(windowA); fftA->output("fft").set(complexA);
  fftB->input("frame").set(windowB); fftB->output("fft").set(complexB);
  spectrumA->input("frame").set(windowA); spectrumA->output("spectrum").set(spectrum);
  polarB->input("complex").set(complexB); polarB->output("magnitude").set(magnitude); polarB->output("phase").set(phase);
  uint64_t h = initialHash;
  frameCount = 0;
  for (;;) {
    cutA->compute(); cutB->compute(); exact(frameA, frameB, "frame");
    if (frameA.empty()) break;
    winA->compute(); winB->compute(); exact(windowA, windowB, "window");
    fftA->compute(); fftB->compute(); exact(complexA, complexB, "complex FFT");
    spectrumA->compute(); polarB->compute(); exact(spectrum, magnitude, "Spectrum/CartesianToPolar magnitude");
    h = hashVector(hashVector(hashVector(h, frameB), windowB), complexB);
    h = hashVector(hashVector(h, magnitude), phase);
    ++frameCount;
  }
  counts.fftFrames += frameCount;
  return h;
}

struct Onsets { Signal beat, info; };
Onsets globalOnsets(const Signal& audio) {
  Owned beat(AlgorithmFactory::create("OnsetDetectionGlobal", "method", "beat_emphasis", "frameSize", 2048, "hopSize", 512, "sampleRate", 44100));
  Owned info(AlgorithmFactory::create("OnsetDetectionGlobal", "method", "infogain", "frameSize", 2048, "hopSize", 512, "sampleRate", 44100));
  Onsets out;
  beat->input("signal").set(audio); beat->output("onsetDetections").set(out.beat);
  info->input("signal").set(audio); info->output("onsetDetections").set(out.info);
  beat->compute(); info->compute();
  return out;
}

struct Rhythm {
  Real bpm = 0, confidence = 0;
  Signal ticks, estimates, intervals;
  std::string failure;
};
Rhythm rhythm(Algorithm& algorithm, const Signal& audio) {
  Rhythm out;
  algorithm.input("signal").set(audio);
  algorithm.output("bpm").set(out.bpm); algorithm.output("confidence").set(out.confidence);
  algorithm.output("ticks").set(out.ticks); algorithm.output("estimates").set(out.estimates);
  algorithm.output("bpmIntervals").set(out.intervals);
  try { algorithm.compute(); }
  catch (const std::exception& e) { out.failure = e.what(); algorithm.reset(); }
  return out;
}
Rhythm rhythm(const Signal& audio) {
  Owned algorithm(AlgorithmFactory::create("RhythmExtractor2013", "method", "multifeature"));
  return rhythm(*algorithm, audio);
}
void exact(const Rhythm& a, const Rhythm& b, const std::string& label) {
  require(a.failure == b.failure, label + " failure mismatch: " + a.failure + " | " + b.failure);
  if (!a.failure.empty()) { ++counts.errorComparisons; return; }
  exact(a.bpm, b.bpm, label + " bpm"); exact(a.confidence, b.confidence, label + " confidence");
  exact(a.ticks, b.ticks, label + " ticks"); exact(a.estimates, b.estimates, label + " estimates");
  exact(a.intervals, b.intervals, label + " intervals");
}
struct Beats { Real confidence = 0; Signal ticks; std::vector<Signal> candidates; std::string failure; };
Beats beats(const Signal& audio) {
  Owned algorithm(AlgorithmFactory::create("BeatTrackerMultiFeature"));
  Beats out;
  algorithm->input("signal").set(audio); algorithm->output("ticks").set(out.ticks);
  algorithm->output("confidence").set(out.confidence);
  try {
    algorithm->compute();
    // Standard BeatTracker does not reset in compute; capture before explicit reset.
    out.candidates = static_cast<essentia::standard::BeatTrackerMultiFeature*>(algorithm.get())->paxCandidates();
  } catch (const std::exception& e) { out.failure = e.what(); }
  algorithm->reset();
  return out;
}
void exact(const Beats& a, const Beats& b, const std::string& label) {
  require(a.failure == b.failure, label + " beat failure mismatch");
  if (!a.failure.empty()) { ++counts.errorComparisons; return; }
  exact(a.confidence, b.confidence, label + " beat confidence"); exact(a.ticks, b.ticks, label + " beat ticks");
  require(a.candidates.size() == 5 && b.candidates.size() == 5, label + " requires five candidates");
  for (size_t i=0; i<5; ++i) { exact(a.candidates[i], b.candidates[i], label + " candidate " + std::to_string(i)); ++counts.candidateVectors; }
}

void oneCase(const std::string& name, const Signal& audio, bool fixture) {
  mode("baseline");
  size_t frames = 0;
  uint64_t fftHash = fftParity(audio, frames);
  Onsets original = globalOnsets(audio);
  Rhythm reference = rhythm(audio); Beats referenceBeats = beats(audio);
  size_t nonempty = 0;
  for (const auto& v : referenceBeats.candidates) nonempty += !v.empty();
  if (name.find("loop-30") != std::string::npos || name.find("track-120") != std::string::npos)
    require(referenceBeats.candidates.size() == 5 &&
            !referenceBeats.candidates[3].empty() && !referenceBeats.candidates[4].empty() &&
            !referenceBeats.ticks.empty(), "Rhythmic fixture requires both optimized candidates to be nonempty");
  for (const char* value : {"buffers"}) {
    mode(value); exact(reference, rhythm(audio), name + " " + value);
    exact(referenceBeats, beats(audio), name + " " + value); ++counts.modeComparisons;
  }
  uint64_t h = hashVector(hashVector(initialHash, reference.ticks), reference.estimates);
  h = hashVector(h, reference.intervals); h = hashBytes(h, &reference.bpm, sizeof(Real));
  h = hashBytes(h, &reference.confidence, sizeof(Real));
  uint64_t candidatesHash = initialHash;
  for (const auto& v : referenceBeats.candidates) candidatesHash = hashVector(candidatesHash, v);
  ++counts.cases; if (fixture) ++counts.fixtures; else ++counts.synthetic;
  std::cout << "{\"case\":" << quoted(name) << ",\"samples\":" << audio.size()
    << ",\"pcmBitChecksum\":" << quoted(hex(hashVector(initialHash, audio)))
    << ",\"fftFrames\":" << frames << ",\"fftBitChecksum\":" << quoted(hex(fftHash))
    << ",\"onsetFrames\":" << original.beat.size() << ",\"onsetBitChecksum\":"
    << quoted(hex(hashVector(hashVector(initialHash, original.beat), original.info)))
    << ",\"rhythmBitChecksum\":" << quoted(hex(h)) << ",\"candidateBitChecksum\":" << quoted(hex(candidatesHash))
    << ",\"nonemptyCandidates\":" << nonempty << ",\"expectedFailure\":" << (reference.failure.empty() ? "false" : "true")
    << ",\"passed\":true}" << std::endl;
}

Signal synthetic(size_t n, int kind) {
  Signal audio(n, 0);
  uint32_t state = UINT32_C(0x572afe31);
  for (size_t i=0; i<n; ++i) {
    state ^= state << 13; state ^= state >> 17; state ^= state << 5;
    Real noise = Real(int64_t(state) - INT64_C(2147483648)) / Real(2147483648.0);
    if (kind == 1) audio[i] = noise * Real(0.35);
    if (kind == 2) {
      // Silence/very quiet transitions plus deterministic transients at frame boundaries.
      if ((i / 997) % 3 == 1) audio[i] = noise * Real(1e-7);
      if ((i / 997) % 3 == 2) audio[i] = Real(0.25 * std::sin(double(i) * 0.06268937721449021));
      if (i % 4096 == 511 || i % 4096 == 512) audio[i] = i % 2 ? Real(-0.8) : Real(0.8);
    }
  }
  return audio;
}

void reconfigureAndRecover() {
  Signal a = synthetic(132311, 2), b = synthetic(44101, 1);
  std::vector<Rhythm> references;
  const int mins[] = {40, 60, 80, 40}; const int maxs[] = {208, 180, 160, 208};
  mode("baseline");
  for (int i=0; i<4; ++i) {
    Owned original(AlgorithmFactory::create("RhythmExtractor2013", "minTempo", mins[i], "maxTempo", maxs[i]));
    references.push_back(rhythm(*original, i % 2 ? b : a));
  }
  for (const char* value : {"baseline", "buffers"}) {
    mode(value); Owned reused(AlgorithmFactory::create("RhythmExtractor2013"));
    for (int i=0; i<4; ++i) {
      reused->configure("minTempo", mins[i], "maxTempo", maxs[i]);
      exact(references[i], rhythm(*reused, i % 2 ? b : a), std::string(value) + " repeated/reconfigured");
      ++counts.reconfigurationComparisons;
    }
    Rhythm empty = rhythm(*reused, Signal());
    require(!empty.failure.empty(), "Empty standard graph must retain its original error");
    exact(references.back(), rhythm(*reused, b), std::string(value) + " after empty graph failure");
    ++counts.recoveryComparisons;
    mode("invalid-test-mode"); Rhythm invalid = rhythm(*reused, a);
    require(!invalid.failure.empty(), "Invalid mode must fail before installing a borrow");
    mode(value); exact(references.back(), rhythm(*reused, b), std::string(value) + " after invalid configuration");
    ++counts.recoveryComparisons;
  }
}

void boundScopeUnwind() {
  // This exception belongs to the probe, not to a production fault-injection hook.
  // It is thrown only after the real graph has accepted the request borrow.
  struct IntentionalProbeFailure {};
  const Signal recovery = synthetic(441017, 2);
  mode("baseline");
  const Beats reference = beats(recovery);
  require(reference.failure.empty() && reference.candidates.size() == 5 &&
          !reference.candidates[3].empty() && !reference.candidates[4].empty(),
          "Unwind recovery signal requires nonempty optimized candidates");
  for (const char* value : {"buffers"}) {
    essentia::Pool pool;
    essentia::streaming::VectorInput<Real>* input = new essentia::streaming::VectorInput<Real>();
    essentia::streaming::BeatTrackerMultiFeature* graph =
        static_cast<essentia::streaming::BeatTrackerMultiFeature*>(
            essentia::streaming::AlgorithmFactory::create("BeatTrackerMultiFeature"));
    *input >> graph->input("signal");
    graph->output("ticks") >> PC(pool, "ticks");
    graph->output("confidence") >> PC(pool, "confidence");
    // Network owns the connected graph and source, as in standard BeatTracker.
    essentia::scheduler::Network network(input);
    Signal previous = synthetic(88217, 1);
    mode(value);
    bool caught = false;
    try {
      essentia::PaxBorrowScope<essentia::streaming::BeatTrackerMultiFeature> scope(graph, previous);
      throw IntentionalProbeFailure();
    } catch (const IntentionalProbeFailure&) { caught = true; }
    require(caught, "Probe exception was not observed");
    // Keep the old vector object alive but make any stale borrow deterministically
    // wrong. Its empty content cannot produce the expected nonempty candidates.
    // This avoids relying on allocator reuse or undefined use-after-free behavior.
    previous.clear(); previous.shrink_to_fit();
    network.reset();
    input->setVector(&recovery);
    mode("baseline");
    network.run();  // A pure streaming call: no new standard borrow can hide a leak.
    Beats actual;
    if (pool.contains<Signal>("ticks")) actual.ticks = pool.value<Signal>("ticks");
    actual.confidence = pool.value<Real>("confidence");
    actual.candidates = graph->paxCandidates();
    exact(reference, actual, std::string(value) + " bound-scope unwind recovery");
    ++counts.boundScopeUnwindComparisons;
  }
}

std::vector<std::string> inputs(int argc, char** argv) {
  std::vector<std::string> paths;
  for (int i=1; i<argc; ++i) {
    struct stat info;
    require(stat(argv[i], &info) == 0, "Missing fixture path");
    if (!S_ISDIR(info.st_mode)) { paths.push_back(argv[i]); continue; }
    DIR* dir = opendir(argv[i]); require(dir != 0, "Cannot list fixture directory");
    while (dirent* e = readdir(dir)) {
      std::string name = e->d_name;
      if (name == "corrupt.wav" || name == "empty.wav" || name == "overlong.wav") continue;
      size_t dot = name.rfind('.'); if (dot == std::string::npos) continue;
      std::string ext = name.substr(dot);
      if (ext == ".wav" || ext == ".aif" || ext == ".aiff" || ext == ".mp3") paths.push_back(std::string(argv[i]) + "/" + name);
    }
    closedir(dir);
  }
  std::sort(paths.begin(), paths.end()); return paths;
}

int main(int argc, char** argv) {
  try {
    essentia::init();
    mode("shared_fft");
    bool unsupported = false;
    try { essentia::paxEngineMode(); } catch (const std::exception&) { unsupported = true; }
    require(unsupported, "Shared FFT must be unavailable in the probe library");
    require(argc > 1, "Fixture arguments are required");
    const size_t lengths[] = {0, 1, 511, 512, 513, 2047, 2048, 2049, 4095, 4096, 4097, 44100, 88217, 132311};
    for (size_t n : lengths) for (int kind=0; kind<3; ++kind)
      oneCase("synthetic-" + std::to_string(n) + "-" + std::to_string(kind), synthetic(n, kind), false);
    reconfigureAndRecover();
    boundScopeUnwind();
    for (const std::string& file : inputs(argc, argv)) {
      Signal audio; Owned loader(AlgorithmFactory::create("MonoLoader", "filename", file, "sampleRate", 44100));
      loader->output("audio").set(audio); loader->compute(); loader.reset();
      oneCase(file.substr(file.find_last_of('/') + 1), audio, true);
    }
    require(counts.fixtures == 14, "Expected exactly 14 valid fixture cases");
    std::cout << "{\"summary\":true,\"passed\":true,\"comparison\":\"exact float32 bits, zero tolerance\",\"checksum\":\"FNV-1a-64 diagnostic only; equality uses memcmp\",\"cases\":" << counts.cases
      << ",\"syntheticCases\":" << counts.synthetic << ",\"fixtureCases\":" << counts.fixtures
      << ",\"modeComparisons\":" << counts.modeComparisons << ",\"candidateVectorsCompared\":" << counts.candidateVectors
      << ",\"fftFramesCompared\":" << counts.fftFrames << ",\"errorComparisons\":" << counts.errorComparisons
      << ",\"reconfigurationComparisons\":" << counts.reconfigurationComparisons << ",\"recoveryComparisons\":" << counts.recoveryComparisons
      << ",\"boundScopeUnwindComparisons\":" << counts.boundScopeUnwindComparisons << "}" << std::endl;
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "{\"passed\":false,\"error\":" << quoted(e.what()) << "}" << std::endl;
    return 1;
  }
}
