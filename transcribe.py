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
import urllib.error
import urllib.request
from array import array
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from math import isqrt
from pathlib import Path
from typing import Any

from cards import (
    CARD_CHARACTER_THRESHOLD,
    CARD_IDLE_SECONDS,
    CARD_MAX_SECONDS,
    CardPipeline,
)
from viewer import ViewerServer, export_cards


MODEL = "gpt-live-transcribe"
TRANSLATION_MODEL = "gpt-5.6-luna"
WEBSOCKET_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
RESPONSES_URL = "https://api.openai.com/v1/responses"
SAMPLE_RATE = 24_000
CHUNK_MILLISECONDS = 10
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MILLISECONDS // 1_000
SILENCE_RMS_THRESHOLD = 500
PRE_ROLL_MILLISECONDS = 500
SILENCE_COMMIT_MILLISECONDS = 800
MAX_TURN_MILLISECONDS = 30_000
FINAL_WAIT_SECONDS = 5
TRANSLATION_TIMEOUT_SECONDS = 20
ENV_FILE = Path(".env.local")


class TranscriptionError(Exception):
    """An expected, user-facing transcription failure."""


class CaptureError(TranscriptionError):
    """ffmpeg couldn't capture the selected audio device."""


class RealtimeAPIError(TranscriptionError):
    """OpenAI returned an error event."""


class TranslationError(TranscriptionError):
    """OpenAI couldn't translate a completed transcript."""


def pcm16_rms(chunk: bytes) -> int:
    samples = array("h")
    samples.frombytes(chunk)
    if sys.byteorder != "little":
        samples.byteswap()
    return isqrt(sum(sample * sample for sample in samples) // len(samples))


class LocalTurnDetector:
    """Gate uploads until speech, retaining a short local pre-roll."""

    def __init__(self, silence_rms_threshold: int = SILENCE_RMS_THRESHOLD) -> None:
        self.silence_rms_threshold = silence_rms_threshold
        self.buffered_milliseconds = 0
        self.silent_milliseconds = 0
        self.heard_speech = False
        self.commits_sent = 0
        self.pre_roll: deque[bytes] = deque(
            maxlen=PRE_ROLL_MILLISECONDS // CHUNK_MILLISECONDS
        )

    def observe(self, chunk: bytes) -> tuple[list[bytes], bool]:
        is_speech = pcm16_rms(chunk) >= self.silence_rms_threshold
        if not self.heard_speech:
            self.pre_roll.append(chunk)
            if not is_speech:
                return [], False
            self.heard_speech = True
            chunks = list(self.pre_roll)
            self.pre_roll.clear()
            self.buffered_milliseconds = len(chunks) * CHUNK_MILLISECONDS
            self.silent_milliseconds = 0
        else:
            chunks = [chunk]
            self.buffered_milliseconds += CHUNK_MILLISECONDS
            if is_speech:
                self.silent_milliseconds = 0
            else:
                self.silent_milliseconds += CHUNK_MILLISECONDS

        should_commit = self.buffered_milliseconds >= MAX_TURN_MILLISECONDS or (
            self.silent_milliseconds >= SILENCE_COMMIT_MILLISECONDS
        )
        return chunks, should_commit

    def mark_committed(self) -> None:
        self.commits_sent += 1
        self.buffered_milliseconds = 0
        self.silent_milliseconds = 0
        self.heard_speech = False
        self.pre_roll.clear()

    def has_buffered_audio(self) -> bool:
        return self.heard_speech and self.buffered_milliseconds > 0


class TranscriptReducer:
    """Order asynchronous completion events by their commit order."""

    def __init__(self, write_final: Callable[[str], None]) -> None:
        self.write_final = write_final
        self.order: list[str] = []
        self.partials: dict[str, str] = {}
        self.completed: dict[str, str] = {}
        self.committed_item_ids: set[str] = set()
        self.written: set[str] = set()
        self.next_to_write = 0
        self.ready: deque[str] = deque()

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
                    self.ready.append(text)
                self.written.add(item_id)
            self.next_to_write += 1

    def handle(self, event: dict[str, Any]) -> tuple[str, bool]:
        """Return (new delta text, whether a new completion arrived)."""
        event_type = event.get("type")
        item_id = event.get("item_id")

        if event_type == "input_audio_buffer.committed" and item_id:
            self._remember(item_id)
            self.committed_item_ids.add(item_id)
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
            return "", is_new

        return "", False

    def has_pending(self) -> bool:
        return any(item_id not in self.completed for item_id in self.order)

    def take_ready(self) -> list[str]:
        ready = list(self.ready)
        self.ready.clear()
        return ready


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
    parser.add_argument(
        "--silence-threshold",
        type=non_negative_integer,
        default=SILENCE_RMS_THRESHOLD,
        metavar="RMS",
        help=f"無音判定のRMS値（既定: {SILENCE_RMS_THRESHOLD}）",
    )
    parser.add_argument(
        "--translate-ja",
        action="store_true",
        help="確定した英語・韓国語を日本語へ翻訳",
    )
    parser.add_argument(
        "--cards",
        action="store_true",
        help="確定した発話から図解カードを生成",
    )
    parser.add_argument(
        "--cards-port",
        type=port_number,
        default=8765,
        metavar="PORT",
        help="図解ビューアのポート（既定: 8765）",
    )
    parser.add_argument(
        "--cards-character-threshold",
        type=positive_integer,
        default=CARD_CHARACTER_THRESHOLD,
        metavar="CHARS",
        help=f"カード生成を始める累積文字数（既定: {CARD_CHARACTER_THRESHOLD}）",
    )
    parser.add_argument(
        "--cards-idle-seconds",
        type=positive_integer,
        default=CARD_IDLE_SECONDS,
        metavar="SECONDS",
        help=f"カード生成を始める無音秒数（既定: {CARD_IDLE_SECONDS}）",
    )
    parser.add_argument(
        "--cards-max-seconds",
        type=positive_integer,
        default=CARD_MAX_SECONDS,
        metavar="SECONDS",
        help=f"カード生成までの最大秒数（既定: {CARD_MAX_SECONDS}）",
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


def positive_integer(value: str) -> int:
    number = non_negative_integer(value)
    if number == 0:
        raise argparse.ArgumentTypeError("1以上の整数を指定してください")
    return number


def port_number(value: str) -> int:
    number = positive_integer(value)
    if number > 65_535:
        raise argparse.ArgumentTypeError("1〜65535のポート番号を指定してください")
    return number


def load_api_key(env_file: Path = ENV_FILE) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        return api_key
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        if separator and name.strip() == "OPENAI_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value.strip()
    return ""


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
                        "languages": ["ja", "en", "ko"],
                        "delay": "low",
                    },
                    "turn_detection": None,
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


def translation_payload(text: str) -> dict[str, Any]:
    return {
        "model": TRANSLATION_MODEL,
        "reasoning": {"effort": "none"},
        "instructions": (
            "Translate only English and Korean portions into natural Japanese. "
            "Leave existing Japanese unchanged. Preserve names, numbers, facts, "
            "and formatting. Output only the resulting Japanese text."
        ),
        "input": text,
        "max_output_tokens": 1_024,
        "store": False,
    }


def response_output_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(str(content.get("text", "")))
    text = "".join(parts).strip()
    if not text:
        raise TranslationError("日本語訳が空でした。")
    return text


def translate_to_japanese(text: str, api_key: str) -> str:
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(translation_payload(text)).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TRANSLATION_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", {}).get("message")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        raise TranslationError(str(detail or f"HTTP {error.code}")) from error
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise TranslationError(f"日本語翻訳との通信に失敗しました: {error}") from error
    except json.JSONDecodeError as error:
        raise TranslationError("日本語翻訳から不正なJSONを受信しました。") from error
    return response_output_text(payload)


def write_header(file: Any, device: int, translate_to_ja: bool = False) -> None:
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    translation = (
        f"- Translation model: `{TRANSLATION_MODEL}`\n" if translate_to_ja else ""
    )
    file.write(
        "# Transcript\n\n"
        f"- Started: {started}\n"
        f"- Device: {device}\n"
        f"- Model: `{MODEL}`\n"
        f"{translation}\n"
        "## Transcript\n\n"
    )
    file.flush()


async def stream_audio(
    websocket: Any,
    process: asyncio.subprocess.Process,
    stop_event: asyncio.Event,
    turn_detector: LocalTurnDetector,
) -> None:
    assert process.stdout is not None
    while not stop_event.is_set():
        try:
            chunk = await process.stdout.readexactly(CHUNK_BYTES)
        except asyncio.IncompleteReadError:
            if stop_event.is_set():
                return
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            detail = stderr.decode(errors="replace").strip()
            raise CaptureError(detail or "音声入力が終了しました。")
        if stop_event.is_set():
            return
        chunks, should_commit = turn_detector.observe(chunk)
        for upload_chunk in chunks:
            await websocket.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(upload_chunk).decode("ascii"),
                    }
                )
            )
        if should_commit:
            await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
            turn_detector.mark_committed()


async def receive_events(
    websocket: Any,
    reducer: TranscriptReducer,
    progress_changed: asyncio.Event,
    finalize_text: Callable[[str], Awaitable[None]] | None = None,
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
        for text in reducer.take_ready():
            if finalize_text is not None:
                await finalize_text(text)
        if delta:
            print(delta, end="", flush=True)
        if completed:
            print(flush=True)
        if completed or event.get("type") == "input_audio_buffer.committed":
            progress_changed.set()

    raise TranscriptionError("Realtime APIとの接続が終了しました。")


async def wait_for_committed_transcripts(
    reducer: TranscriptReducer,
    progress_changed: asyncio.Event,
    expected_commits: int,
) -> None:
    while len(reducer.committed_item_ids) < expected_commits:
        progress_changed.clear()
        if len(reducer.committed_item_ids) >= expected_commits:
            break
        await progress_changed.wait()

    while reducer.has_pending():
        progress_changed.clear()
        if not reducer.has_pending():
            return
        await progress_changed.wait()


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        process.kill()
        await process.wait()


async def run_transcription(
    device: int,
    api_key: str,
    ffmpeg: str,
    silence_rms_threshold: int = SILENCE_RMS_THRESHOLD,
    translate_to_ja: bool = False,
    cards_enabled: bool = False,
    cards_port: int = 8765,
    cards_character_threshold: int = CARD_CHARACTER_THRESHOLD,
    cards_idle_seconds: int = CARD_IDLE_SECONDS,
    cards_max_seconds: int = CARD_MAX_SECONDS,
) -> Path:
    try:
        from websockets.asyncio.client import connect
    except ImportError as error:
        raise TranscriptionError("`uv run transcribe.py` で起動してください。") from error

    stop_event = asyncio.Event()
    progress_changed = asyncio.Event()
    turn_detector = LocalTurnDetector(silence_rms_threshold)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[Any]] = []
    pipeline: CardPipeline | None = None
    viewer_server: ViewerServer | None = None

    if cards_enabled:
        try:
            pipeline = CardPipeline(
                api_key,
                character_threshold=cards_character_threshold,
                idle_seconds=cards_idle_seconds,
                max_seconds=cards_max_seconds,
            )
            pipeline.start()
        except Exception as error:
            print(f"[cards] 警告: 図解保存を開始できません: {error}", file=sys.stderr)
            pipeline = None
        if pipeline is not None:
            try:
                viewer_server = ViewerServer(pipeline.json_path, cards_port)
                viewer_server.start()
                print(f"図解ビューア: {viewer_server.url}")
            except OSError as error:
                print(
                    f"[cards] 警告: 図解ビューアを開始できません: {error}",
                    file=sys.stderr,
                )
                viewer_server = None
            else:
                try:
                    subprocess.run(["open", viewer_server.url], check=False)
                except OSError as error:
                    print(
                        f"[cards] 警告: ブラウザを開けません: {error}",
                        file=sys.stderr,
                    )

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
                write_header(transcript, device, translate_to_ja)
                print(f"保存先: {output_path}")

                def write_final(text: str) -> None:
                    transcript.write(text + "\n\n")
                    transcript.flush()

                async def finalize_text(text: str) -> None:
                    write_final(text)
                    if pipeline is not None:
                        try:
                            pipeline.add(text)
                        except Exception as error:
                            print(
                                f"[cards] 警告: 原文を図解へ渡せません: {error}",
                                file=sys.stderr,
                            )
                    if not translate_to_ja:
                        return
                    translated = await asyncio.to_thread(
                        translate_to_japanese, text, api_key
                    )
                    if translated == text:
                        return
                    quote = translated.replace("\n", "\n> ")
                    transcript.write(f"> **日本語訳:** {quote}\n\n")
                    transcript.flush()
                    print(f"[日本語訳] {translated}", flush=True)

                needs_finalize = translate_to_ja or pipeline is not None
                reducer = TranscriptReducer(
                    (lambda _: None) if needs_finalize else write_final
                )
                sender = asyncio.create_task(
                    stream_audio(websocket, process, stop_event, turn_detector)
                )
                receiver = asyncio.create_task(
                    receive_events(
                        websocket,
                        reducer,
                        progress_changed,
                        finalize_text if needs_finalize else None,
                    )
                )
                stopper = asyncio.create_task(stop_event.wait())
                tasks.extend([sender, receiver, stopper])

                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    stop_event.set()
                    await stop_process(process)
                    await asyncio.gather(sender, return_exceptions=True)
                    if turn_detector.has_buffered_audio():
                        await websocket.send(
                            json.dumps({"type": "input_audio_buffer.commit"})
                        )
                        turn_detector.mark_committed()
                    try:
                        await asyncio.wait_for(
                            wait_for_committed_transcripts(
                                reducer,
                                progress_changed,
                                turn_detector.commits_sent,
                            ),
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
        if pipeline is not None:
            try:
                generated_cards = await pipeline.close()
                export_path = export_cards(generated_cards, pipeline.html_path)
                print(f"図解HTML: {export_path}")
            except Exception as error:
                print(
                    f"[cards] 警告: 図解の終了処理に失敗しました: {error}",
                    file=sys.stderr,
                )
        if viewer_server is not None:
            try:
                viewer_server.stop()
            except OSError as error:
                print(
                    f"[cards] 警告: 図解ビューアを停止できません: {error}",
                    file=sys.stderr,
                )
        loop.remove_signal_handler(signal.SIGINT)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpegが見つかりません。", file=sys.stderr)
        return 2

    if args.list_devices:
        return list_audio_devices(ffmpeg)

    try:
        api_key = load_api_key()
    except OSError as error:
        print(f".env.localを読み込めませんでした: {error}", file=sys.stderr)
        return 2
    if not api_key:
        print(
            "OPENAI_API_KEYが環境変数または.env.localに設定されていません。",
            file=sys.stderr,
        )
        return 2

    try:
        output_path = asyncio.run(
            run_transcription(
                args.device,
                api_key,
                ffmpeg,
                silence_rms_threshold=args.silence_threshold,
                translate_to_ja=args.translate_ja,
                cards_enabled=args.cards,
                cards_port=args.cards_port,
                cards_character_threshold=args.cards_character_threshold,
                cards_idle_seconds=args.cards_idle_seconds,
                cards_max_seconds=args.cards_max_seconds,
            )
        )
    except TranscriptionError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    print(f"保存しました: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
