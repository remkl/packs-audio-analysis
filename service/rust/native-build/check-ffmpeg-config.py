"""Verify reviewed exclusions and preserve source-built FFmpeg evidence."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

prefix = Path(sys.argv[1])
policy = json.loads(Path('/tmp/ffmpeg-policy.json').read_text())
config_paths = [Path('/ffmpeg/config.h'), Path('/ffmpeg/config_components.h')]
macros = {}
for path in config_paths:
    macros.update(dict(re.findall(r'^#define (CONFIG_\w+) ([01])$', path.read_text(), re.M)))
for row in policy['advisories']:
    for macro in row['expectedDisabledMacros']:
        if macros.get(macro) != '0':
            raise SystemExit(row['id'] + ': required build exclusion is absent: ' + macro)
# Inputs stay file-only and restricted to the service's existing container list.
required_demuxers = {'WAV', 'AIFF', 'MP3', 'FLAC', 'OGG', 'MOV', 'MATROSKA'}
actual_demuxers = {k.removeprefix('CONFIG_').removesuffix('_DEMUXER')
                  for k, v in macros.items() if k.endswith('_DEMUXER') and v == '1'}
if actual_demuxers != required_demuxers:
    raise SystemExit('Unexpected compiled demuxer set: ' + str(sorted(actual_demuxers)))
actual_protocols = {k for k, v in macros.items() if k.endswith('_PROTOCOL') and v == '1'}
if actual_protocols != {'CONFIG_FILE_PROTOCOL'}:
    raise SystemExit('Unexpected compiled protocol set: ' + str(sorted(actual_protocols)))
out = prefix / 'share/pax-build'
out.mkdir(parents=True, exist_ok=True)
for path in config_paths + [Path('/ffmpeg/ffbuild/config.mak'), Path('/tmp/ffmpeg-policy.json'), prefix / 'decoder-registry.tsv']:
    shutil.copy2(path, out / path.name)
inputs = {}
for name in ('ffmpeg.tar.xz', 'ffmpeg-debian.tar.xz'):
    inputs[name] = hashlib.sha256((Path('/tmp') / name).read_bytes()).hexdigest()
(out / 'source-inputs.json').write_text(json.dumps({
    'sourceVersion': policy['version'], 'sourceArchiveSha256': inputs,
    'excludedAdvisories': [r['id'] for r in policy['advisories']],
    'scope': policy['scope'],
}, indent=2) + '\n')
print('FFmpeg configured exclusions passed; source and configuration evidence retained')
