#!/usr/bin/env python3
"""Compare decoded float32 bytes with the frozen Python wheel, without tolerances."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent


def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def image_id(image):
    return run("docker", "image", "inspect", image, "--format", "{{.Id}}").stdout.decode().strip()


def check(python_image, candidate_image, output):
    python_image, candidate_image = image_id(python_image), image_id(candidate_image)
    spec = importlib.util.spec_from_file_location("runtime_check", ROOT / "runtime-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    corpus = module.extract_corpus()
    fixtures = json.loads((corpus / "goldens/amd64-wheel-golden.json").read_text())["outputs"]
    with tempfile.TemporaryDirectory(prefix="pax-decoder-parity-") as directory:
        scratch = Path(directory)
        scratch.chmod(0o777)
        inputs = scratch / "input"
        inputs.mkdir()
        for name in fixtures:
            shutil.copyfile(corpus / "fixtures" / name, inputs / name)
        for folder in (scratch / "reference", scratch / "candidate"):
            folder.mkdir(mode=0o777)
            folder.chmod(0o777)
        docker = ["docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
                  "-v", str(scratch) + ":/proof"]
        # These containers and gapless tails were absent from the original corpus.
        controls = [
            ("flac", "flac", 44100, 2, []),
            ("ogg", "libvorbis", 44100, 2, []),
            ("ogg", "libvorbis", 48000, 2, []),
            ("webm", "libvorbis", 44100, 2, []),
            ("m4a", "aac", 22050, 1, []),
            ("m4a", "aac", 44100, 2, []),
            ("m4a", "aac", 48000, 2, []),
            ("mp3", "libmp3lame", 44100, 2, ["-q:a", "2"]),
            ("mp3", "libmp3lame", 48000, 1, ["-b:a", "128k"]),
        ]
        for extension, codec, rate, channels, options in controls:
            name = "control-%s-%s-%s.%s" % (codec, rate, channels, extension)
            run(*docker, "--entrypoint", "ffmpeg", python_image, "-v", "error", "-i",
                "/proof/input/pulse-120-a4.wav", "-t", "2.037", "-map_metadata", "-1",
                "-ar", str(rate), "-ac", str(channels), "-c:a", codec, *options,
                "/proof/input/" + name)
        # Ogg and Matroska have different legacy handling of Vorbis end padding.
        # Preserve the explicit Matroska discard even when retaining Ogg's tail.
        run(*docker, "--entrypoint", "ffmpeg", python_image, "-v", "error", "-i",
            "/proof/input/control-libvorbis-44100-2.ogg", "-c:a", "copy",
            "/proof/input/control-vorbis-discard-padding.webm")
        oracle = """from pathlib import Path
import essentia.standard as es
import numpy as np
for path in sorted(Path('/proof/input').iterdir()):
    samples = np.asarray(es.MonoLoader(filename=str(path), sampleRate=44100)(), dtype='<f4')
    assert len(samples) > 0
    samples.tofile('/proof/reference/' + path.name + '.pcm')
"""
        run(*docker, "--entrypoint", "python3", python_image, "-c", oracle)
        rows = []
        for path in sorted(inputs.iterdir()):
            run(*docker, "--entrypoint", "/app/decoder-pcm", candidate_image,
                "/proof/input/" + path.name, "/proof/candidate/" + path.name + ".pcm")
            reference = (scratch / "reference" / (path.name + ".pcm")).read_bytes()
            candidate = (scratch / "candidate" / (path.name + ".pcm")).read_bytes()
            row = {
                "fixture": path.name, "inputSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "referenceSamples": len(reference) // 4, "candidateSamples": len(candidate) // 4,
                "referencePcmSha256": hashlib.sha256(reference).hexdigest(),
                "candidatePcmSha256": hashlib.sha256(candidate).hexdigest(),
                "passed": reference == candidate,
            }
            if not row["passed"]:
                offset = next((i for i, pair in enumerate(zip(reference, candidate))
                               if pair[0] != pair[1]), min(len(reference), len(candidate)))
                row["firstDifferentSample"] = offset // 4
            rows.append(row)
        result = {"passed": all(row["passed"] for row in rows), "cases": rows,
                  "referenceImage": python_image, "candidateImage": candidate_image,
                  "comparison": "float32 bytes, exact", "sharedFft": False}
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"passed": result["passed"], "cases": len(rows),
                          "mismatches": [row["fixture"] for row in rows if not row["passed"]]}))
        return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if check(args.python_image, args.candidate_image, args.output) else 1)
