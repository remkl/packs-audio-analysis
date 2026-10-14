"""Shared input rejection rules, proved without external storage."""
import asyncio
import ctypes
import json
import math
import os
import subprocess
import wave
from types import SimpleNamespace

import pytest

os.environ.setdefault("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", "dev-analysis-secret")
os.environ.setdefault("AUDIO_ANALYSIS_ALLOWED_HOSTS", "127.0.0.1")

import main
from codec_compatibility import COMPATIBLE_AUDIO_CODECS, WHEEL_AUDIO_CODECS


def encode_webm(tmp_path, codec):
    source = tmp_path / f"{codec}.webm"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", codec, str(source)],
        check=True,
    )
    return source


def test_probe_rejects_opus_before_the_pinned_decoder(tmp_path):
    source = encode_webm(tmp_path, "libopus")
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.probe_duration(source))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        422, "decode", "Audio could not be decoded"
    )
    assert isinstance(error.value.__cause__, main.UndecodableAudio)


@pytest.mark.parametrize("codec", ["adpcm_ima_wav", "adpcm_ms"])
def test_probe_rejects_unsupported_adpcm_before_the_pinned_decoder(tmp_path, codec):
    source = tmp_path / f"{codec}.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", codec, str(source)],
        check=True,
    )
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.probe_duration(source))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        422, "decode", "Audio could not be decoded"
    )
    assert isinstance(error.value.__cause__, main.UndecodableAudio)


def test_frozen_codecs_match_the_pinned_wheel_registry():
    import essentia._essentia

    library = ctypes.CDLL(essentia._essentia.__file__)
    library.avcodec_version.restype = ctypes.c_uint
    library.avcodec_version.argtypes = []
    # AVCodec's prefix below is the public libavcodec 56 ABI in the pinned wheel.
    assert library.avcodec_version() >> 16 == 56, "Review the wheel ABI before updating this test"

    class Codec(ctypes.Structure):
        _fields_ = [
            ("name", ctypes.c_char_p), ("long_name", ctypes.c_char_p),
            ("media_type", ctypes.c_int), ("codec_id", ctypes.c_int),
        ]

    codec_pointer = ctypes.POINTER(Codec)
    library.avcodec_register_all.restype = None
    library.avcodec_register_all.argtypes = []
    library.av_codec_next.restype = codec_pointer
    library.av_codec_next.argtypes = [codec_pointer]
    library.av_codec_is_decoder.restype = ctypes.c_int
    library.av_codec_is_decoder.argtypes = [codec_pointer]
    library.avcodec_get_name.restype = ctypes.c_char_p
    library.avcodec_get_name.argtypes = [ctypes.c_int]
    library.avcodec_register_all()
    registered = set()
    codec = library.av_codec_next(None)
    while codec:
        if codec.contents.media_type == 1 and library.av_codec_is_decoder(codec):
            registered.add(library.avcodec_get_name(codec.contents.codec_id).decode("ascii"))
        codec = library.av_codec_next(codec)
    assert registered == WHEEL_AUDIO_CODECS
    assert len(WHEEL_AUDIO_CODECS) == 63
    assert COMPATIBLE_AUDIO_CODECS == WHEEL_AUDIO_CODECS - {"pcm_zork"}
    assert len(COMPATIBLE_AUDIO_CODECS) == 62
    assert {"pcm_zork", "adpcm_zork"}.isdisjoint(COMPATIBLE_AUDIO_CODECS)


def test_probe_and_decoder_keep_vorbis_support(tmp_path):
    from analyzer import AudioAnalyzer

    source = encode_webm(tmp_path, "libvorbis")
    duration = asyncio.run(main.probe_duration(source))
    assert duration is not None and 0.9 < duration < 1.1
    result = AudioAnalyzer().analyze_file(str(source))
    assert 0.9 < result["duration"] < 1.1
    assert all(math.isfinite(result[key]) for key in ("bpm", "bpm_confidence", "key_confidence"))


def test_legacy_zork_wav_is_rejected_before_analysis(tmp_path, monkeypatch):
    # Legacy 8-bit WAV tag 0x0011 was named pcm_zork by FFmpeg 2.8;
    # current ffprobe calls the same encoding adpcm_zork.
    unsigned_pcm = [int(128 + 32 * math.sin(2 * math.pi * 440 * index / 22050)) for index in range(4410)]
    encoded = bytes(value if value >= 128 else 128 - value for value in unsigned_pcm)
    source = tmp_path / "legacy-zork.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(1)
        output.setframerate(22050)
        output.writeframes(encoded)
    container = bytearray(source.read_bytes())
    format_start = container.index(b"fmt ") + 8
    container[format_start:format_start + 2] = b"\x11\x00"
    source.write_bytes(container)
    async def download(_url, _directory):
        return source

    async def unexpected(*_args):
        pytest.fail("Zork input must not reach the decoder or analysis")

    monkeypatch.setattr(main, "download_source", download)
    monkeypatch.setattr(main, "run_analysis", unexpected)
    monkeypatch.setattr(main, "job_slots", main.JobSlots(1, 0))
    request = SimpleNamespace(state=SimpleNamespace(stage="request"))
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.run_job(request, "http://local/source", None))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        422, "decode", "Audio could not be decoded"
    )
    assert isinstance(error.value.__cause__, main.UndecodableAudio)
    assert not source.exists()


@pytest.mark.parametrize("codec", ["pcm_zork", "adpcm_zork"])
def test_both_zork_codec_names_are_rejected(tmp_path, monkeypatch, codec):
    async def command(_args):
        return 0, json.dumps({"format": {"duration": "0.2"}, "streams": [{"codec_name": codec}]}).encode()

    monkeypatch.setattr(main, "run_media_command", command)
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.probe_duration(tmp_path / "source.wav"))
    assert (error.value.status_code, error.value.stage) == (422, "decode")
    assert isinstance(error.value.__cause__, main.UndecodableAudio)


def test_long_opus_keeps_duration_error_before_codec_rejection(tmp_path, monkeypatch):
    source = tmp_path / "overlong.webm"
    source.write_bytes(b"fixture")

    async def command(args):
        assert args[args.index("-select_streams") + 1] == "a:0"
        return 0, json.dumps({"format": {"duration": "2"}, "streams": [{"codec_name": "opus"}]}).encode()

    async def download(_url, _directory):
        return source

    async def unexpected(*_args):
        pytest.fail("An overlong source must not reach the decoder")

    monkeypatch.setattr(main, "run_media_command", command)
    monkeypatch.setattr(main, "download_source", download)
    monkeypatch.setattr(main, "run_analysis", unexpected)
    monkeypatch.setattr(main, "MAX_DURATION_SECONDS", 1)
    monkeypatch.setattr(main, "job_slots", main.JobSlots(1, 0))
    request = SimpleNamespace(state=SimpleNamespace(stage="request"))
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.run_job(request, "http://local/source", None))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        413, "probe", "Audio is longer than 1 seconds"
    )
    assert not source.exists()


@pytest.mark.parametrize("stdout", [
    b"{", b"[]", b'{"format": [], "streams": []}', b'{"format": {}, "streams": {}}',
    b'{"format": {}, "streams": [1]}', b"\xff", b"[" * 65 + b"0" + b"]" * 65,
    b" " * 65537,
])
def test_malformed_or_unbounded_probe_json_is_a_controlled_error(tmp_path, monkeypatch, stdout):
    async def command(_args):
        return 0, stdout

    monkeypatch.setattr(main, "run_media_command", command)
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.probe_duration(tmp_path / "source.wav"))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        422, "probe", "Audio could not be decoded"
    )


def test_probe_keeps_unknown_duration_supported(tmp_path, monkeypatch):
    async def command(_args):
        return 0, b'{"format": {}, "streams": [{"codec_name": "vorbis"}]}'

    monkeypatch.setattr(main, "run_media_command", command)
    assert asyncio.run(main.probe_duration(tmp_path / "source.ogg")) is None


@pytest.mark.parametrize("kind", ["hls", "concat"])
def test_probe_rejects_playlists_disguised_as_audio(tmp_path, kind):
    wav = tmp_path / "private.wav"
    with wave.open(str(wav), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b"\0\0" * 44100)
    mp3 = tmp_path / "private.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), str(mp3)], check=True)
    source = tmp_path / "source.wav"
    source.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
        f"#EXTINF:1.000,\n{mp3}\n#EXT-X-ENDLIST\n"
        if kind == "hls" else "ffconcat version 1.0\nfile private.mp3\n"
    )
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.probe_duration(source))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        422, "probe", "Audio could not be decoded"
    )


def test_decoded_duration_overrides_understated_metadata(tmp_path, monkeypatch):
    source = tmp_path / "source.flac"
    source.write_bytes(b"fixture")
    uploaded = []

    async def download(_url, _directory):
        return source

    async def probe(_path):
        return 0.1

    async def analyze(_path, _lease):
        return {"duration": 2.0}

    async def playback(*_args):
        uploaded.append(True)

    monkeypatch.setattr(main, "download_source", download)
    monkeypatch.setattr(main, "probe_duration", probe)
    monkeypatch.setattr(main, "run_analysis", analyze)
    monkeypatch.setattr(main, "create_playback_derivative", playback)
    monkeypatch.setattr(main, "MAX_DURATION_SECONDS", 1)
    monkeypatch.setattr(main, "job_slots", main.JobSlots(1, 0))
    request = SimpleNamespace(state=SimpleNamespace(stage="request"))
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.run_job(request, "http://local/source", "http://local/output"))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        413, "decode", "Audio is longer than 1 seconds"
    )
    assert not uploaded
    assert not source.exists()


def test_decoded_memory_limit_keeps_its_public_error_and_log_class(tmp_path, monkeypatch):
    def reject(*_args):
        raise main.DecodedAudioTooLarge("Decoded audio exceeds memory limit")

    monkeypatch.setattr(main, "get_analyzer", lambda: SimpleNamespace(analyze_file=reject))
    lease = main.SlotLease(main.JobSlots(1, 0))
    with pytest.raises(main.ServiceError) as error:
        asyncio.run(main.run_analysis(tmp_path / "source.wav", lease))
    assert (error.value.status_code, error.value.stage, error.value.detail) == (
        413, "decode", "Decoded audio exceeds memory limit"
    )
    assert isinstance(error.value.__cause__, main.DecodedAudioTooLarge)
