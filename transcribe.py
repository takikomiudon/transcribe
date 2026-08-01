#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "websockets>=15,<17",
# ]
# ///
"""Stream one macOS audio input to OpenAI Realtime transcription."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any


MODEL = "gpt-live-transcribe"
WEBSOCKET_URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
SAMPLE_RATE = 24_000
CHUNK_MILLISECONDS = 100
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MILLISECONDS // 1_000
FINAL_WAIT_SECONDS = 5


class TranscriptionError(Exception):
    """An expected, user-facing transcription failure."""


class CaptureError(TranscriptionError):
    """ffmpeg couldn't capture the selected audio device."""


class RealtimeAPIError(TranscriptionError):
    """OpenAI returned an error event."""


class TranscriptReducer:
    """Order asynchronous completion events by their speech-start order."""

    def __init__(self, write_final: Callable[[str], None]) -> None:
        self.write_final = write_final
        self.order: list[str] = []
        self.partials: dict[str, str] = {}
        self.completed: dict[str, str] = {}
        self.written: set[str] = set()
        self.next_to_write = 0
        self.active_item_id: str | None = None

    def _remember(self, item_id: str) -> None:
        if item_id not in self.partials:
            self.order.append(item_id)
            self.partials[item_id] = ""

    def _flush_ready(self) -> None:
        while self.next_to_write < len(self.order):
            item_id = self.order[self.next_to_write]
            if item_id not in self.completed:
                break
            if item_id not in self.written:
                text = self.completed[item_id].strip()
                if text:
                    self.write_final(text)
                self.written.add(item_id)
            self.next_to_write += 1

    def handle(self, event: dict[str, Any]) -> tuple[str, bool]:
        """Return (new delta text, whether a new completion arrived)."""
        event_type = event.get("type")
        item_id = event.get("item_id")

        if event_type == "input_audio_buffer.speech_started" and item_id:
            self._remember(item_id)
            self.active_item_id = item_id
        elif event_type == "input_audio_buffer.speech_stopped" and item_id:
            self._remember(item_id)
            if self.active_item_id == item_id:
                self.active_item_id = None
        elif (
            event_type == "conversation.item.input_audio_transcription.delta"
            and item_id
        ):
            self._remember(item_id)
            delta = str(event.get("delta", ""))
            self.partials[item_id] += delta
            return delta, False
        elif (
            event_type == "conversation.item.input_audio_transcription.completed"
            and item_id
        ):
            self._remember(item_id)
            is_new = item_id not in self.completed
            if is_new:
                transcript = event.get("transcript")
                self.completed[item_id] = (
                    str(transcript) if transcript is not None else self.partials[item_id]
                )
                self._flush_ready()
            if self.active_item_id == item_id:
                self.active_item_id = None
            return "", is_new

        return "", False

    def has_pending(self) -> bool:
        return any(item_id not in self.completed for item_id in self.order)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="macOSの音声入力をリアルタイムで文字起こしします。"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--list-devices", action="store_true", help="音声入力デバイスを一覧表示"
    )
    mode.add_argument(
        "--device", type=non_negative_integer, metavar="INDEX", help="音声入力番号"
    )
    return parser


def non_negative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("0以上の整数を指定してください") from error
    if number < 0:
        raise argparse.ArgumentTypeError("0以上の整数を指定してください")
    return number


def parse_audio_devices(output: str) -> list[tuple[int, str]]:
    """Extract the AVFoundation audio section from ffmpeg's stderr."""
    in_audio_section = False
    devices: list[tuple[int, str]] = []
    for line in output.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio_section = True
            continue
        if not in_audio_section:
            continue
        match = re.search(r"\]\s+\[(\d+)\]\s+(.+)$", line)
        if match:
            devices.append((int(match.group(1)), match.group(2).strip()))
    return devices


def list_audio_devices(ffmpeg: str) -> int:
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-f",
            "avfoundation",
            "-list_devices",
            "true",
            "-i",
            "",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    devices = parse_audio_devices(result.stderr)
    if not devices:
        print(
            "音声入力を取得できませんでした。ターミナルのマイク権限を確認してください。",
            file=sys.stderr,
        )
        return 1
    for index, name in devices:
        print(f"{index}: {name}")
    return 0


def ffmpeg_command(ffmpeg: str, device: int) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "avfoundation",
        "-i",
        f":{device}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]


def session_update() -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "transcription": {
                        "model": MODEL,
                        "languages": ["ja", "en"],
                        "delay": "low",
                    },
                    "turn_detection": {"type": "server_vad"},
                }
            },
        },
    }


def transcript_path(now: datetime | None = None) -> Path:
    output_dir = Path("transcripts")
    output_dir.mkdir(exist_ok=True)
    timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")
    candidate = output_dir / f"{timestamp}.md"
    suffix = 2
    while candidate.exists():
        candidate = output_dir / f"{timestamp}-{suffix}.md"
        suffix += 1
    return candidate


def write_header(file: Any, device: int) -> None:
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    file.write(
        "# Transcript\n\n"
        f"- Started: {started}\n"
        f"- Device: {device}\n"
        f"- Model: `{MODEL}`\n\n"
        "## Transcript\n\n"
    )
    file.flush()


async def stream_audio(
    websocket: Any,
    process: asyncio.subprocess.Process,
    stop_event: asyncio.Event,
) -> None:
    assert process.stdout is not None
    while not stop_event.is_set():
        chunk = await process.stdout.read(CHUNK_BYTES)
        if not chunk:
            if stop_event.is_set():
                return
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            detail = stderr.decode(errors="replace").strip()
            raise CaptureError(detail or "音声入力が終了しました。")
        if stop_event.is_set():
            return
        await websocket.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )
        )


async def receive_events(
    websocket: Any,
    reducer: TranscriptReducer,
    completion_changed: asyncio.Event,
) -> None:
    async for raw_message in websocket:
        try:
            event = json.loads(raw_message)
        except json.JSONDecodeError as error:
            raise TranscriptionError("APIから不正なJSONを受信しました。") from error

        if event.get("type") == "error":
            error = event.get("error") or {}
            message = error.get("message") or "Realtime APIでエラーが発生しました。"
            raise RealtimeAPIError(str(message))

        delta, completed = reducer.handle(event)
        if delta:
            print(delta, end="", flush=True)
        if completed:
            print(flush=True)
            completion_changed.set()

    raise TranscriptionError("Realtime APIとの接続が終了しました。")


async def wait_for_pending(
    reducer: TranscriptReducer, completion_changed: asyncio.Event
) -> None:
    while reducer.has_pending():
        completion_changed.clear()
        if not reducer.has_pending():
            return
        await completion_changed.wait()


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        process.kill()
        await process.wait()


async def run_transcription(device: int, api_key: str, ffmpeg: str) -> Path:
    try:
        from websockets.asyncio.client import connect
    except ImportError as error:
        raise TranscriptionError("`uv run transcribe.py` で起動してください。") from error

    stop_event = asyncio.Event()
    completion_changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[Any]] = []

    try:
        async with connect(
            WEBSOCKET_URL,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            max_size=None,
        ) as websocket:
            await websocket.send(json.dumps(session_update()))
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_command(ffmpeg, device),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            output_path = transcript_path()
            with output_path.open("x", encoding="utf-8") as transcript:
                write_header(transcript, device)
                print(f"保存先: {output_path}")

                def write_final(text: str) -> None:
                    transcript.write(text + "\n\n")
                    transcript.flush()

                reducer = TranscriptReducer(write_final)
                sender = asyncio.create_task(
                    stream_audio(websocket, process, stop_event)
                )
                receiver = asyncio.create_task(
                    receive_events(websocket, reducer, completion_changed)
                )
                stopper = asyncio.create_task(stop_event.wait())
                tasks.extend([sender, receiver, stopper])

                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    stop_event.set()
                    await stop_process(process)
                    await asyncio.gather(sender, return_exceptions=True)
                    if reducer.active_item_id is not None:
                        await websocket.send(
                            json.dumps({"type": "input_audio_buffer.commit"})
                        )
                    try:
                        await asyncio.wait_for(
                            wait_for_pending(reducer, completion_changed),
                            timeout=FINAL_WAIT_SECONDS,
                        )
                    except TimeoutError:
                        pass
                    if receiver.done():
                        receiver.result()
                elif sender in done:
                    sender.result()
                    raise CaptureError("音声入力が予期せず終了しました。")
                else:
                    receiver.result()

            return output_path
    except TranscriptionError:
        raise
    except OSError as error:
        raise TranscriptionError(f"接続または音声入力に失敗しました: {error}") from error
    except Exception as error:
        raise TranscriptionError(f"Realtime APIとの通信に失敗しました: {error}") from error
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await stop_process(process)
        loop.remove_signal_handler(signal.SIGINT)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpegが見つかりません。", file=sys.stderr)
        return 2

    if args.list_devices:
        return list_audio_devices(ffmpeg)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEYが設定されていません。", file=sys.stderr)
        return 2

    try:
        output_path = asyncio.run(run_transcription(args.device, api_key, ffmpeg))
    except TranscriptionError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    print(f"保存しました: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
