#!/usr/bin/env python3
"""Check immutable native ABI separation and non-root port 80, locally only."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import time
import uuid


def command(*args, check=True):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def check_runtime(image):
    metadata = json.loads(command('docker', 'image', 'inspect', image).stdout)[0]
    immutable = metadata['Id']
    host_arch = command('docker', 'info', '--format', '{{.Architecture}}').stdout.strip()
    host_arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(host_arch, host_arch)
    require(metadata['Config']['User'] == '10001:10001', 'Runtime must use UID/GID 10001')
    script = '''
if command -v python3 >/dev/null || command -v python >/dev/null || command -v rustc >/dev/null || command -v cargo >/dev/null; then exit 1; fi
ffmpeg -version | head -1
ldd /usr/bin/ffmpeg
printf 'NATIVE\\n'
ldd /app/audio-analysis
printf 'CAPABILITY\\n'
getcap /app/audio-analysis
id -u
id -g
'''
    output = command('docker', 'run', '--rm', '--entrypoint', 'sh', immutable, '-c', script).stdout
    cli, native = output.split('NATIVE\n', 1)
    native, identity = native.split('CAPABILITY\n', 1)
    require('ffmpeg version 5.1.9-0+deb12u1' in cli, 'Wrong FFmpeg CLI version')
    require('/opt/control' not in cli and '/opt/compat-lib' not in cli, 'CLI loaded private native dependencies')
    require('libavcodec.so.59' in cli and 'libavformat.so.59' in cli, 'Wrong CLI ABI')
    require('not found' not in cli + native, 'Missing runtime dependency')
    required = {
        'libfftw3f.so.3': '/opt/control/',
        'libavformat.so.58': '/opt/control/',
        'libavcodec.so.58': '/opt/control/',
        'libavutil.so.56': '/opt/control/',
        'libswresample.so.3': '/opt/control/',
        'libsamplerate.so.0': '/opt/compat-lib/',
    }
    native_lines = native.splitlines()
    for library, location in required.items():
        require(any(library + ' =>' in line and location in line for line in native_lines), 'Wrong native dependency: ' + library)
    for library in ('libc.so.6', 'libstdc++.so.6', 'libgcc_s.so.1'):
        require(any(library + ' =>' in line and '/opt/' not in line for line in native_lines), 'Old core runtime was copied: ' + library)
    require('cap_net_bind_service=ep' in identity, 'Binary bind capability is missing')
    require(identity.splitlines()[-2:] == ['10001', '10001'], 'Runtime identity changed')

    name = 'pax-native-port-' + uuid.uuid4().hex[:12]
    started = False
    try:
        command('docker', 'run', '--rm', '-d', '--name', name,
                '--sysctl', 'net.ipv4.ip_unprivileged_port_start=1024',
                '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
                '-p', '127.0.0.1::80',
                '-e', 'AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD=local-port-proof-only',
                '-e', 'AUDIO_ANALYSIS_ALLOWED_HOSTS=example.com', immutable)
        started = True
        address = command('docker', 'port', name, '80/tcp').stdout.strip()
        require(address.startswith('127.0.0.1:'), 'Proof port must be loopback only')
        port = int(address.rsplit(':', 1)[1])
        deadline = time.monotonic() + 10
        health = None
        while time.monotonic() < deadline:
            client = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                client.request('GET', '/healthz')
                response = client.getresponse()
                body = response.read()
                if response.status == 200:
                    health = json.loads(body)
                    break
            except (OSError, http.client.HTTPException):
                pass
            finally:
                client.close()
            time.sleep(0.05)
        require(isinstance(health, dict) and health.get('status') == 'ok' and isinstance(health.get('release'), str), 'Non-root port 80 health check failed')
        observed = command('docker', 'exec', name, 'sh', '-c',
                           'id -u; cat /proc/sys/net/ipv4/ip_unprivileged_port_start; grep CapEff /proc/1/status').stdout.splitlines()
        require(observed[0:2] == ['10001', '1024'], 'Non-root port restriction is missing')
        capability = observed[2].split(':', 1)[1].strip()
        require(int(capability, 16) == 0x400, 'Unexpected effective capability set')
        return {
            'passed': True, 'imageId': immutable, 'architecture': metadata['Architecture'],
            'dockerHostArchitecture': host_arch, 'emulated': metadata['Architecture'] != host_arch,
            'performanceMeasured': False,
            'pythonAbsent': True, 'rustToolsAbsent': True, 'uid': 10001,
            'ffmpegVersion': cli.splitlines()[0], 'cliUsesPrivateNativeLibraries': False,
            'nativeAbiLines': [line.strip() for line in native_lines if any(lib in line for lib in required)],
            'port80': {'unprivilegedPortStart': 1024, 'effectiveCapabilities': capability, 'health': health},
        }
    finally:
        if started:
            command('docker', 'stop', '--timeout', '10', name, check=False)
            command('docker', 'rm', '--force', name, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = check_runtime(args.image)
    result['checkerSha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    text = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
