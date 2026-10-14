"""Pinned Essentia scoped buffer borrowing. Shared FFT is deliberately absent.

Run after patch_configuration.py. Production always borrows immutable request
input. --probe permits baseline/buffers selection only in the test library.
"""
import argparse
import hashlib
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, default=Path('/essentia'))
parser.add_argument('--probe', action='store_true', help='Permit baseline/buffer comparison in a test-only library')
args = parser.parse_args()
root = args.root / 'src/algorithms/rhythm'
files = {name: (root / name).read_text() for name in [
    'onsetdetectionglobal.h', 'onsetdetectionglobal.cpp',
    'beattrackermultifeature.h', 'beattrackermultifeature.cpp',
    'rhythmextractor2013.h', 'rhythmextractor2013.cpp',
]}
expected = {
    'onsetdetectionglobal.h': '28239cca267b16eb3371259cdbfef2c28f7127b3b67f0484c886c1e36a597bad',
    'onsetdetectionglobal.cpp': '666915af9e462571f3a29a4c2cb0271a085191f5b6772453597014edf69a124a',
    'beattrackermultifeature.h': 'f2c93353ed11242ef2182d56989ee644fe515742ec8c86be52519860b44f9cda',
    'beattrackermultifeature.cpp': 'ac54039e23bdf361af6e86f18db0a63a1be755e72c24fd390969491fb87bc8c4',
    'rhythmextractor2013.h': '97e4a10a510c5077ed07849f5240d6ce93aed78ba271c3b5862b4aa2a3e2084d',
    'rhythmextractor2013.cpp': '8e1fc871c7fec994bd850ec2eaebf127390a5c14748f4980d130f9275983d07a',
}
for name, value in files.items():
    assert hashlib.sha256(value.encode()).hexdigest() == expected[name], ('Unexpected pinned source', name)


def replace(name, old, new, count=1):
    assert files[name].count(old) == count, (name, old[:100], files[name].count(old))
    files[name] = files[name].replace(old, new)


assert 'Constructor already configured this child' in files['rhythmextractor2013.cpp'], 'Apply v4 configuration guard first'
assert 'PaxBorrowScope' not in files['rhythmextractor2013.cpp'], 'Patch already applied'

replace('onsetdetectionglobal.h', '  std::vector<Real> _spectrum_1;', '  std::vector<Real> _spectrum_1;\n  std::vector<Real> _paxEmpty;')
replace('onsetdetectionglobal.h', '  void computeBeatEmphasis();', """  void computeBeatEmphasis();
  void clearPaxInput() {
    _signal.set(_paxEmpty);
    _frameCutter->input("signal").set(_paxEmpty);
  }""")
replace('onsetdetectionglobal.h', '  standard::Algorithm * _onsetDetectionGlobal;', '''  standard::Algorithm * _onsetDetectionGlobal;
  const std::vector<Real>* _paxSignal;
''')
replace('onsetdetectionglobal.h', '  ~OnsetDetectionGlobal();', '''  ~OnsetDetectionGlobal();
  void setPaxSignal(const std::vector<Real>* signal);
  standard::OnsetDetectionGlobal* paxStandard() {
    return static_cast<standard::OnsetDetectionGlobal*>(_onsetDetectionGlobal);
  }''')
replace('onsetdetectionglobal.cpp', 'OnsetDetectionGlobal::OnsetDetectionGlobal() : AlgorithmComposite() {', '''// Same token acquisition/release as PoolStorage, without storing a second
// full signal when a standard request owns a stable immutable input vector.
class PaxSignalStorage : public PoolStorage<Real> {
 public:
  bool borrowed;
  PaxSignalStorage(Pool* pool) : PoolStorage<Real>(pool, "internal.signal"), borrowed(false) {}
  AlgorithmStatus process() {
    if (!borrowed) return PoolStorage<Real>::process();
    int ntokens = std::min(_descriptor.available(), _descriptor.buffer().bufferInfo().maxContiguousElements);
    ntokens = std::max(ntokens, 1);
    if (!_descriptor.acquire(ntokens)) return NO_INPUT;
    _descriptor.release(ntokens);
    return OK;
  }
};

void OnsetDetectionGlobal::setPaxSignal(const std::vector<Real>* signal) {
  _paxSignal = signal;
  static_cast<PaxSignalStorage*>(_poolStorage)->borrowed = signal != 0;
  if (!signal) paxStandard()->clearPaxInput();
}

OnsetDetectionGlobal::OnsetDetectionGlobal() : AlgorithmComposite(), _paxSignal(0) {''')
replace('onsetdetectionglobal.cpp', '  _poolStorage = new PoolStorage<Real>(&_pool, "internal.signal");', '  _poolStorage = new PaxSignalStorage(&_pool);')
replace('onsetdetectionglobal.cpp', '''  _onsetDetectionGlobal->input("signal").set(_pool.value<vector<Real> >("internal.signal"));
  _onsetDetectionGlobal->output("onsetDetections").set(detections);
  _onsetDetectionGlobal->compute();''', '''  const vector<Real>& input = _paxSignal ? *_paxSignal : _pool.value<vector<Real> >("internal.signal");
  _onsetDetectionGlobal->input("signal").set(input);
  _onsetDetectionGlobal->output("onsetDetections").set(detections);
  _onsetDetectionGlobal->compute();''')

# Thread the borrow down an existing graph. Scope teardown clears pointers even
# when native compute throws. Input lifetime remains the caller's whole vector.
replace('beattrackermultifeature.h', '#include "network.h"', '#include "network.h"\n#include "onsetdetectionglobal.h"')
replace('beattrackermultifeature.h', '  AlgorithmStatus process();', '''  AlgorithmStatus process();
  void setPaxSignal(const std::vector<Real>* signal);
  std::vector<std::vector<Real> > paxCandidates() const;''')
replace('beattrackermultifeature.h', '  void createInnerNetwork();\n\n  static const char* name;', '''  void createInnerNetwork();
  std::vector<std::vector<Real> > paxCandidates() const;

  static const char* name;''')
replace('beattrackermultifeature.cpp', 'void BeatTrackerMultiFeature::clearAlgos() {', '''void BeatTrackerMultiFeature::setPaxSignal(const vector<Real>* signal) {
  OnsetDetectionGlobal* beat = static_cast<OnsetDetectionGlobal*>(_onsetBeatEmphasis3);
  OnsetDetectionGlobal* info = static_cast<OnsetDetectionGlobal*>(_onsetInfogain4);
  beat->setPaxSignal(signal);
  info->setPaxSignal(signal);
}

vector<vector<Real> > BeatTrackerMultiFeature::paxCandidates() const {
  const char* names[] = {"internal.ticksComplex", "internal.ticksRms", "internal.ticksMelFlux", "internal.ticksBeatEmphasis", "internal.ticksInfogain"};
  vector<vector<Real> > result(5);
  for (int i=0; i<5; ++i) if (_pool.contains<vector<Real> >(names[i])) result[i] = _pool.value<vector<Real> >(names[i]);
  return result;
}

void BeatTrackerMultiFeature::clearAlgos() {''')
replace('beattrackermultifeature.cpp', '#include "beattrackermultifeature.h"', '#include "beattrackermultifeature.h"\n#include "paxenginescope.h"')
replace('beattrackermultifeature.cpp', '  _vectorInput->setVector(&signal);\n  _network->run();', '''  _vectorInput->setVector(&signal);
  PaxBorrowScope<streaming::BeatTrackerMultiFeature> scopedInput(
      static_cast<streaming::BeatTrackerMultiFeature*>(_beatTracker), signal);
  _network->run();''')
replace('beattrackermultifeature.cpp', 'void BeatTrackerMultiFeature::reset() {\n  _network->reset();', '''vector<vector<Real> > BeatTrackerMultiFeature::paxCandidates() const {
  return static_cast<streaming::BeatTrackerMultiFeature*>(_beatTracker)->paxCandidates();
}

void BeatTrackerMultiFeature::reset() {
  _network->reset();''')
replace('rhythmextractor2013.h', '  AlgorithmStatus process();', '  AlgorithmStatus process();\n  void setPaxSignal(const std::vector<Real>* signal);')
replace('rhythmextractor2013.cpp', '#include "rhythmextractor2013.h"', '#include "rhythmextractor2013.h"\n#include "beattrackermultifeature.h"\n#include "paxenginescope.h"')
replace('rhythmextractor2013.cpp', 'AlgorithmStatus RhythmExtractor2013::process() {', '''void RhythmExtractor2013::setPaxSignal(const vector<Real>* signal) {
  if (_method == "multifeature") {
    static_cast<BeatTrackerMultiFeature*>(_beatTracker)->setPaxSignal(signal);
  }
}

AlgorithmStatus RhythmExtractor2013::process() {''')
replace('rhythmextractor2013.cpp', '  _vectorInput->setVector(&signal);\n\n  _network->run();', '''  _vectorInput->setVector(&signal);
  PaxBorrowScope<streaming::RhythmExtractor2013> scopedInput(
      static_cast<streaming::RhythmExtractor2013*>(_rhythmExtractor), signal);

  _network->run();''')

scope = '''#ifndef PAX_ENGINE_SCOPE_H
#define PAX_ENGINE_SCOPE_H
#include <cstdlib>
#include <cstring>
namespace essentia {
// Configuration is read once per standard request. No global borrowed data.
inline int paxEngineMode() {
  const char* value = std::getenv("PAX_ENGINE_MODE");
  if (!value || std::strcmp(value, "baseline") == 0) return 0;
  if (std::strcmp(value, "buffers") == 0) return 1;
  throw EssentiaException("Unknown PAX_ENGINE_MODE");
}
template<class Graph> class PaxBorrowScope {
  Graph* graph;
 public:
  PaxBorrowScope(Graph* owner, const std::vector<Real>& input) : graph(owner) {
    const int mode = paxEngineMode();
    // Preserve the original empty-stream failure path (no internal.signal pool).
    graph->setPaxSignal(mode && !input.empty() ? &input : 0);
  }
  ~PaxBorrowScope() { graph->setPaxSignal(0); }
 private:
  PaxBorrowScope(const PaxBorrowScope&);
  PaxBorrowScope& operator=(const PaxBorrowScope&);
};
}
#endif
'''
if not args.probe:
    begin = scope.index('  const char* value = std::getenv("PAX_ENGINE_MODE");')
    end = scope.index('\n}', begin)
    scope = scope[:begin] + '  return 1;  // Production always borrows; no environment switch.' + scope[end:]
for name, content in files.items():
    (root / name).write_text(content)
(root / 'paxenginescope.h').write_text(scope)
print('Scoped buffer borrowing installed; shared FFT is not compiled')
