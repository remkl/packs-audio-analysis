"""Skip identical setup only for freshly constructed rhythm children."""
import hashlib
from pathlib import Path
p=Path('/essentia/src/algorithms/rhythm/rhythmextractor2013.cpp')
s=p.read_text()
if hashlib.sha256(s.encode()).hexdigest() != 'c296c3f4c9c8d4c91c9a12ca0100414787ca2c320798041fc88e0ce163114b8d':
    raise SystemExit('Unexpected pinned rhythm source')
old='''RhythmExtractor2013::RhythmExtractor2013() {
  declareInput'''
new='''RhythmExtractor2013::RhythmExtractor2013() {
  _configured = false;
  declareInput'''
assert s.count(old)==1
s=s.replace(old,new)
old='''void RhythmExtractor2013::configure() {
  _rhythmExtractor->configure(INHERIT("maxTempo"), INHERIT("minTempo"),
                              INHERIT("method"));
}'''
new='''void RhythmExtractor2013::configure() {
  // Constructor already configured this child. Preserve reconfiguration after use.
  if (!_configured &&
      _rhythmExtractor->parameter("method").toLower() == parameter("method").toLower() &&
      _rhythmExtractor->parameter("minTempo").toInt() == parameter("minTempo").toInt() &&
      _rhythmExtractor->parameter("maxTempo").toInt() == parameter("maxTempo").toInt()) {
    _configured = true;
    return;
  }
  _rhythmExtractor->configure(INHERIT("maxTempo"), INHERIT("minTempo"),
                              INHERIT("method"));
  _configured = true;
}'''
assert s.count(old)==1
s=s.replace(old,new)
old='''  _beatTracker->configure(INHERIT("minTempo"), INHERIT("maxTempo"));'''
new='''  // createInnerNetwork produced a fresh child configured with its defaults.
  if (_beatTracker->parameter("minTempo").toInt() != parameter("minTempo").toInt() ||
      _beatTracker->parameter("maxTempo").toInt() != parameter("maxTempo").toInt()) {
    _beatTracker->configure(INHERIT("minTempo"), INHERIT("maxTempo"));
  }'''
assert s.count(old)==1
s=s.replace(old,new)
p.write_text(s)
print('Guarded fresh-configuration patch applied')
