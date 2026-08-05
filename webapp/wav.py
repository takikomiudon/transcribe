"""Constant-time appends to a mono 16-bit PCM WAV file."""

from __future__ import annotations

import os
import struct
import wave
from pathlib import Path
from typing import BinaryIO


class WavAppender:
    def __init__(self, path: Path, sample_rate: int = 16_000) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self._file: BinaryIO | None = None
        self._data_offset = 0
        self._size_offset = 0
        self._payload_bytes = 0

    def open(self) -> None:
        if self._file is not None:
            return
        if not self.path.exists():
            with wave.open(str(self.path), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(self.sample_rate)

        audio_file = self.path.open("r+b")
        try:
            header = audio_file.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
                raise ValueError("invalid WAV header")

            file_size = os.fstat(audio_file.fileno()).st_size
            fmt_found = False
            data_offset = size_offset = None
            offset = 12
            while offset + 8 <= file_size:
                audio_file.seek(offset)
                chunk_id, chunk_size = struct.unpack("<4sI", audio_file.read(8))
                payload_offset = offset + 8
                if chunk_id == b"fmt ":
                    audio_file.seek(payload_offset)
                    fmt = audio_file.read(min(chunk_size, 16))
                    if len(fmt) < 16:
                        raise ValueError("invalid WAV fmt chunk")
                    audio_format, channels, rate, _, _, bits = struct.unpack(
                        "<HHIIHH", fmt
                    )
                    if (audio_format, channels, rate, bits) != (
                        1,
                        1,
                        self.sample_rate,
                        16,
                    ):
                        raise ValueError("incompatible WAV format")
                    fmt_found = True
                elif chunk_id == b"data":
                    data_offset = payload_offset
                    size_offset = payload_offset - 4
                    break
                offset = payload_offset + chunk_size + (chunk_size & 1)

            if not fmt_found or data_offset is None or size_offset is None:
                raise ValueError("WAV fmt or data chunk is missing")

            payload_bytes = file_size - data_offset
            if payload_bytes < 0:
                raise ValueError("invalid WAV data offset")
            if payload_bytes & 1:
                payload_bytes -= 1
                audio_file.truncate(data_offset + payload_bytes)

            self._file = audio_file
            self._data_offset = data_offset
            self._size_offset = size_offset
            self._payload_bytes = payload_bytes
            self.sync_header()
        except Exception:
            audio_file.close()
            self._file = None
            raise

    def write(self, pcm: bytes) -> None:
        audio_file = self._require_open()
        audio_file.write(pcm)
        self._payload_bytes += len(pcm)

    def tell(self) -> int:
        return self._payload_bytes // 2

    def sync_header(self) -> None:
        audio_file = self._require_open()
        file_size = self._data_offset + self._payload_bytes
        audio_file.seek(4)
        audio_file.write(struct.pack("<I", file_size - 8))
        audio_file.seek(self._size_offset)
        audio_file.write(struct.pack("<I", self._payload_bytes))
        audio_file.seek(file_size)
        audio_file.flush()

    def close(self) -> None:
        if self._file is None:
            return
        try:
            self.sync_header()
        finally:
            self._file.close()
            self._file = None

    def writeframes(self, data: bytes) -> None:
        assert data == b""
        self.sync_header()

    def flush(self) -> None:
        self.sync_header()

    def _require_open(self) -> BinaryIO:
        if self._file is None:
            raise ValueError("WAV is not open")
        return self._file
