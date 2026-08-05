"""Offline checks for appendable WAV recordings."""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path

import transcribe
from webapp.wav import WavAppender


def test_new_wav_is_readable() -> None:
    pcm = b"\x01\x00\x02\x00\x03\x00"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recording.wav"
        appender = WavAppender(path)
        appender.open()
        appender.write(pcm)
        assert appender.tell() == 3
        appender.close()
        appender.close()

        with wave.open(str(path), "rb") as recording:
            assert recording.getnchannels() == 1
            assert recording.getsampwidth() == 2
            assert recording.getframerate() == 16_000
            assert recording.getnframes() == 3
            assert recording.readframes(3) == pcm


def test_reopen_appends_frames() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recording.wav"
        first = WavAppender(path)
        first.open()
        first.write(b"\x01\x00\x02\x00")
        first.close()

        second = WavAppender(path)
        second.open()
        assert second.tell() == 2
        second.write(b"\x03\x00\x04\x00\x05\x00")
        assert second.tell() == 5
        second.close()

        with wave.open(str(path), "rb") as recording:
            assert recording.getnframes() == 5


def test_reopen_repairs_stale_header() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recording.wav"
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(16_000)
        with path.open("ab") as audio_file:
            audio_file.write(b"\x01\x00\x02\x00")

        appender = WavAppender(path)
        appender.open()
        assert appender.tell() == 2
        appender.close()

        with wave.open(str(path), "rb") as recording:
            assert recording.getnframes() == 2
            assert recording.readframes(2) == b"\x01\x00\x02\x00"


def test_reopen_truncates_incomplete_sample() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recording.wav"
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(16_000)
            recording.writeframes(b"\x01\x00")
        with path.open("ab") as audio_file:
            audio_file.write(b"\xff")

        appender = WavAppender(path)
        appender.open()
        assert appender.tell() == 1
        appender.close()

        with wave.open(str(path), "rb") as recording:
            assert recording.getnframes() == 1
            assert recording.readframes(1) == b"\x01\x00"


def test_rejects_different_format() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recording.wav"
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(44_100)

        try:
            WavAppender(path).open()
        except ValueError as error:
            assert "format" in str(error).lower()
        else:
            raise AssertionError("different WAV format was accepted")


def test_snapshot_recording_compatibility() -> None:
    pcm = b"\x01\x00\x02\x00"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "recording.wav"
        appender = WavAppender(path)
        appender.open()
        appender.write(pcm)
        snapshot = transcribe.snapshot_recording(appender, appender, path)
        try:
            with wave.open(str(snapshot), "rb") as recording:
                assert recording.getnframes() == 2
                assert recording.readframes(2) == pcm
        finally:
            snapshot.unlink()
            appender.close()


def main() -> None:
    test_new_wav_is_readable()
    test_reopen_appends_frames()
    test_reopen_repairs_stale_header()
    test_reopen_truncates_incomplete_sample()
    test_rejects_different_format()
    test_snapshot_recording_compatibility()
    print("ok")


if __name__ == "__main__":
    main()
