#!/usr/bin/env python3
"""Build from public sources and run the local Rust replacement gates."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SERVICE = ROOT.parent


def run(args, log, *, required=True):
    print("Checking " + log.stem, flush=True)
    with log.open("w") as output:
        result = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        print(log.read_text()[-6000:], file=sys.stderr)
        if required:
            raise SystemExit(result.returncode)
    return result.returncode == 0


def sources():
    paths = [p for p in ROOT.rglob("*") if p.is_file()
             and not any(part in {".test-results", ".compat-data", "target", "__pycache__"} for part in p.relative_to(ROOT).parts)]
    paths += list(SERVICE.glob("*.py"))
    paths += [SERVICE / name for name in (
        "Dockerfile.vercel", "Dockerfile.python", ".dockerignore",
        "requirements.lock", "requirements-dev.txt",
    )]
    paths += [p for p in (SERVICE / "tests").rglob("*")
              if p.is_file() and "__pycache__" not in p.parts]
    return {str(p.relative_to(SERVICE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def build_image(args, output, *, platform, target, tag, dockerfile, scope):
    command = ["docker", "buildx", "build", "--load", "--platform", platform, "-t", tag]
    if target:
        command += ["--target", target]
    if args.cache_gha:
        command += ["--cache-from", "type=gha,scope=" + scope,
                    "--cache-to", "type=gha,mode=max,scope=" + scope]
        if scope == "audio-analysis-rust-test":
            command += ["--cache-from", "type=gha,scope=audio-analysis-rust-runtime"]
    command += ["-f", str(SERVICE / dockerfile), str(SERVICE)]
    run(command, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=["amd64", "arm64"], default="amd64")
    parser.add_argument("--python-image", default="pax-audio-analysis:python-contract")
    parser.add_argument("--build-python", action="store_true")
    parser.add_argument("--cache-gha", action="store_true", default=os.environ.get("PAX_AUDIO_BUILD_CACHE") == "gha",
                        help="Use the GitHub Actions Buildx cache (CI only)")
    args = parser.parse_args()
    platform = "linux/" + args.architecture
    output = ROOT / ".test-results" / args.architecture
    output.mkdir(parents=True, exist_ok=True)
    before = sources()
    runtime = "pax-audio-analysis:rust-check-" + args.architecture
    probe = "pax-audio-analysis:rust-probe-check-" + args.architecture
    if args.build_python:
        build_image(args, output / "python-build.log", platform="linux/amd64", target=None,
                    tag=args.python_image, dockerfile="Dockerfile.python", scope="audio-analysis-python")
    python_image = subprocess.check_output(
        ["docker", "image", "inspect", args.python_image, "--format", "{{.Id}}"], text=True).strip()
    run(["docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
         "-v", str(ROOT / "transport-check.py") + ":/app/transport-check.py:ro",
         "--entrypoint", "python3", python_image, "/app/transport-check.py"], output / "python-transport.log")
    for target, tag in [("runtime", runtime), ("test", probe)]:
        build_image(args, output / (target + "-build.log"), platform=platform, target=target,
                    tag=tag, dockerfile="Dockerfile.vercel", scope="audio-analysis-rust-" + target)
    images = {name: subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
              for name, image in [("runtime", runtime), ("probe", probe)]}
    images["python"] = python_image
    runtime, probe = images["runtime"], images["probe"]
    run([sys.executable, str(ROOT / "native-build/check-runtime.py"), "--image", runtime,
         "--output", str(output / "image-runtime.json")], output / "image-runtime.log")
    spec = importlib.util.spec_from_file_location("runtime_check", ROOT / "runtime-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    corpus = module.extract_corpus()
    fixtures = json.loads((corpus / "goldens/amd64-wheel-golden.json").read_text())["outputs"]
    if args.architecture == "amd64":
        run([sys.executable, str(ROOT / "decoder-parity-check.py"), "--python-image", python_image,
             "--candidate-image", probe, "--output", str(output / "decoder-parity.json")],
            output / "decoder-parity.log")
    run(["docker", "run", "--rm", "--network", "none", "--platform", platform,
         "-v", str(corpus / "fixtures") + ":/fixtures:ro", "--entrypoint", "/app/bounded-decode-check",
         probe, *["/fixtures/" + name for name in fixtures]], output / "bounded-decode.log")
    run(["docker", "run", "--rm", "--network", "none", "--platform", platform,
         "-v", str(corpus / "fixtures") + ":/fixtures:ro", probe, "/fixtures"], output / "engine-probe.log")
    rows = [json.loads(line) for line in (output / "engine-probe.log").read_text().splitlines() if line.startswith("{")]
    assert rows[-1]["passed"] and rows[-1]["cases"] == 56, "Native probe did not complete its fixed corpus"
    http_passed = run([sys.executable, str(ROOT / "tests/http_parity.py"), "--python-image", python_image,
         "--rust-image", runtime, "--output", str(output / "http-parity.json")], output / "http-parity.log", required=False)
    runtime_passed = run([sys.executable, str(ROOT / "runtime-check.py"), "--python-image", python_image,
         "--image", runtime, "--architecture", args.architecture,
         "--output", str(output / "runtime-check.json")], output / "runtime-check.log", required=False)
    assert before == sources(), "Candidate/test sources changed during verification"
    passed = http_passed and runtime_passed
    manifest = {"passed": passed, "architecture": args.architecture, "sourceSha256": before,
                "checks": {"httpParity": http_passed, "runtime": runtime_passed},
                "images": images, "deployedServiceChanged": False}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Rust replacement gates " + ("passed: " if passed else "FAILED: ") + str(output))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
