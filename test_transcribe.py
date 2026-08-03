"""Small offline checks for the realtime transcription CLI."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import transcribe


def test_model_configuration() -> None:
    parsed = urlparse(transcribe.realtime_url())
    assert parsed.scheme == "wss"
    assert parsed.netloc == "api.elevenlabs.io"
    assert parsed.path == "/v1/speech-to-text/realtime"
    assert parse_qs(parsed.query) == {
        "model_id": ["scribe_v2_realtime"],
        "audio_format": ["pcm_16000"],
        "commit_strategy": ["manual"],
        "language_code": ["ja"],
        "secondary_languages": ["en", "ko"],
    }
    assert transcribe.SAMPLE_RATE == 16_000
    assert transcribe.CHUNK_MILLISECONDS == 10
    assert transcribe.UPLOAD_CHUNK_MILLISECONDS == 100


def test_translation_configuration_and_output() -> None:
    payload = transcribe.translation_payload("Hello. 안녕하세요.")
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["store"] is False
    assert transcribe.response_output_text(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "こんにちは。"}
                    ],
                }
            ]
        }
    ) == "こんにちは。"


def test_translation_flag() -> None:
    args = transcribe.build_parser().parse_args(
        ["--device", "0", "--translate-ja"]
    )
    assert args.translate_ja


def test_cards_flags() -> None:
    args = transcribe.build_parser().parse_args(
        [
            "--device",
            "0",
            "--cards",
            "--cards-port",
            "9000",
            "--cards-character-threshold",
            "120",
            "--cards-idle-seconds",
            "8",
            "--cards-max-seconds",
            "45",
        ]
    )
    assert args.cards
    assert args.cards_port == 9000
    assert args.cards_character_threshold == 120
    assert args.cards_idle_seconds == 8
    assert args.cards_max_seconds == 45


def test_api_key_auto_load() -> None:
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(
            transcribe.Path,
            "read_text",
            return_value=(
                "export ELEVENLABS_API_KEY='eleven-from-file'\n"
                "OPENAI_API_KEY='openai-from-file'\n"
            ),
        ),
    ):
        assert transcribe.load_elevenlabs_api_key() == "eleven-from-file"
        assert transcribe.load_openai_api_key() == "openai-from-file"

    with (
        patch.dict(
            os.environ,
            {
                "ELEVENLABS_API_KEY": "eleven-from-env",
                "OPENAI_API_KEY": "openai-from-env",
            },
            clear=True,
        ),
        patch.object(transcribe.Path, "read_text") as read_text,
    ):
        assert transcribe.load_elevenlabs_api_key() == "eleven-from-env"
        assert transcribe.load_openai_api_key() == "openai-from-env"
        read_text.assert_not_called()


def test_local_turn_detection() -> None:
    loud = (transcribe.SILENCE_RMS_THRESHOLD + 1).to_bytes(
        2, "little", signed=True
    ) * (transcribe.CHUNK_BYTES // 2)
    silence = bytes(transcribe.CHUNK_BYTES)
    detector = transcribe.LocalTurnDetector()

    pre_roll_chunks = (
        transcribe.PRE_ROLL_MILLISECONDS // transcribe.CHUNK_MILLISECONDS
    )
    for _ in range(pre_roll_chunks + 10):
        assert detector.observe(silence) == ([], False)
    assert not detector.has_buffered_audio()

    chunks, should_commit = detector.observe(loud)
    assert len(chunks) == pre_roll_chunks
    assert chunks[-1] == loud
    assert not should_commit
    silence_chunks = (
        transcribe.SILENCE_COMMIT_MILLISECONDS // transcribe.CHUNK_MILLISECONDS
    )
    for _ in range(silence_chunks - 1):
        assert detector.observe(silence) == ([silence], False)
    assert detector.observe(silence) == ([silence], True)

    detector.mark_committed()
    assert detector.commits_sent == 1
    assert not detector.has_buffered_audio()

    max_chunks = transcribe.MAX_TURN_MILLISECONDS // transcribe.CHUNK_MILLISECONDS
    assert detector.observe(loud) == ([loud], False)
    for _ in range(max_chunks - 2):
        assert detector.observe(loud) == ([loud], False)
    assert detector.observe(loud) == ([loud], True)


async def test_idle_silence_is_not_uploaded() -> None:
    class SilenceStdout:
        def __init__(self) -> None:
            self.remaining = 100

        async def readexactly(self, expected: int) -> bytes:
            if self.remaining:
                self.remaining -= 1
                return bytes(expected)
            raise asyncio.IncompleteReadError(partial=b"", expected=expected)

    class ErrorReader:
        async def read(self, _: int = -1) -> bytes:
            return b"capture ended"

    class FakeProcess:
        stdout = SilenceStdout()
        stderr = ErrorReader()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, message: str) -> None:
            self.messages.append(message)

    websocket = FakeWebSocket()
    try:
        await transcribe.stream_audio(
            websocket,
            FakeProcess(),  # type: ignore[arg-type]
            asyncio.Event(),
            transcribe.LocalTurnDetector(),
        )
    except transcribe.CaptureError:
        pass
    else:
        raise AssertionError("CaptureError was not raised")
    assert websocket.messages == []


async def test_audio_chunks_are_batched_and_committed() -> None:
    class OneChunkStdout:
        def __init__(self) -> None:
            self.sent = False

        async def readexactly(self, expected: int) -> bytes:
            if not self.sent:
                self.sent = True
                return bytes(expected)
            raise asyncio.IncompleteReadError(partial=b"", expected=expected)

    class ErrorReader:
        async def read(self, _: int = -1) -> bytes:
            return b"capture ended"

    class FakeProcess:
        stdout = OneChunkStdout()
        stderr = ErrorReader()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, message: str) -> None:
            self.messages.append(message)

    class FakeDetector:
        commits_sent = 0

        def observe(self, _: bytes) -> tuple[list[bytes], bool]:
            return [b"x" * transcribe.CHUNK_BYTES] * 11, True

        def mark_committed(self) -> None:
            self.commits_sent += 1

        def has_buffered_audio(self) -> bool:
            return False

    websocket = FakeWebSocket()
    try:
        await transcribe.stream_audio(
            websocket,
            FakeProcess(),  # type: ignore[arg-type]
            asyncio.Event(),
            FakeDetector(),  # type: ignore[arg-type]
        )
    except transcribe.CaptureError:
        pass
    else:
        raise AssertionError("CaptureError was not raised")

    messages = [json.loads(message) for message in websocket.messages]
    assert [message["message_type"] for message in messages] == [
        "input_audio_chunk",
        "input_audio_chunk",
        "input_audio_chunk",
    ]
    assert len(base64.b64decode(messages[0]["audio_base_64"])) == (
        transcribe.UPLOAD_CHUNK_BYTES
    )
    assert len(base64.b64decode(messages[1]["audio_base_64"])) == (
        transcribe.CHUNK_BYTES
    )
    assert messages[0]["sample_rate"] == transcribe.SAMPLE_RATE
    assert messages[0]["commit"] is False
    assert messages[2] == {
        "message_type": "input_audio_chunk",
        "audio_base_64": "",
        "commit": True,
        "sample_rate": transcribe.SAMPLE_RATE,
    }


async def test_stop_commits_buffered_audio() -> None:
    stop_event = asyncio.Event()

    class StoppedStdout:
        async def readexactly(self, expected: int) -> bytes:
            stop_event.set()
            raise asyncio.IncompleteReadError(partial=b"", expected=expected)

    class FakeProcess:
        stdout = StoppedStdout()
        stderr = None

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, message: str) -> None:
            self.messages.append(message)

    class FakeDetector:
        commits_sent = 0

        def observe(self, _: bytes) -> tuple[list[bytes], bool]:
            raise AssertionError("audio should not be observed")

        def mark_committed(self) -> None:
            self.commits_sent += 1

        def has_buffered_audio(self) -> bool:
            return True

    websocket = FakeWebSocket()
    detector = FakeDetector()
    await transcribe.stream_audio(
        websocket,
        FakeProcess(),  # type: ignore[arg-type]
        stop_event,
        detector,  # type: ignore[arg-type]
    )

    assert detector.commits_sent == 1
    assert json.loads(websocket.messages[-1]) == {
        "message_type": "input_audio_chunk",
        "audio_base_64": "",
        "commit": True,
        "sample_rate": transcribe.SAMPLE_RATE,
    }


def test_realtime_events_and_repeated_transcripts() -> None:
    written: list[str] = []
    reducer = transcribe.TranscriptReducer(written.append)

    assert reducer.handle(
        {"message_type": "partial_transcript", "text": "Hello wor"}
    ) == ("Hello wor", False)
    assert reducer.handle(
        {"message_type": "committed_transcript", "text": "Hello world"}
    ) == ("Hello world", True)
    assert reducer.handle(
        {"message_type": "committed_transcript", "text": "Hello world"}
    ) == ("Hello world", True)
    assert written == ["Hello world", "Hello world"]
    assert reducer.take_ready() == ["Hello world", "Hello world"]
    assert reducer.commits_received == 2


def test_missing_key_stops_before_capture() -> None:
    stderr = io.StringIO()
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("transcribe.shutil.which", return_value="/usr/local/bin/ffmpeg"),
        patch("transcribe.load_elevenlabs_api_key", return_value=""),
        patch("transcribe.asyncio.run") as run,
        redirect_stderr(stderr),
    ):
        assert transcribe.main(["--device", "0"]) == 2
    run.assert_not_called()
    assert "ELEVENLABS_API_KEY" in stderr.getvalue()


async def test_capture_eof_and_process_cleanup() -> None:
    class EmptyReader:
        async def read(self, _: int = -1) -> bytes:
            return b"capture failed"

    class EmptyStdout:
        async def readexactly(self, expected: int) -> bytes:
            raise asyncio.IncompleteReadError(partial=b"", expected=expected)

    class FakeProcess:
        stdout = EmptyStdout()
        stderr = EmptyReader()

    class FakeWebSocket:
        async def send(self, _: str) -> None:
            raise AssertionError("EOF must not send audio")

    try:
        await transcribe.stream_audio(
            FakeWebSocket(),
            FakeProcess(),  # type: ignore[arg-type]
            asyncio.Event(),
            transcribe.LocalTurnDetector(),
        )
    except transcribe.CaptureError as error:
        assert "capture failed" in str(error)
    else:
        raise AssertionError("CaptureError was not raised")

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    )
    await transcribe.stop_process(process)
    assert process.returncode is not None


async def test_websocket_error_preserves_completed_output() -> None:
    messages = [
        '{"message_type":"session_started","session_id":"a"}',
        '{"message_type":"committed_transcript","text":"Saved."}',
        '{"message_type":"rate_limited","error":"network failed"}',
    ]

    class FakeWebSocket:
        def __aiter__(self):
            self.messages = iter(messages)
            return self

        async def __anext__(self) -> str:
            try:
                return next(self.messages)
            except StopIteration as error:
                raise StopAsyncIteration from error

    written: list[str] = []
    reducer = transcribe.TranscriptReducer(written.append)
    try:
        await transcribe.receive_events(FakeWebSocket(), reducer, asyncio.Event())
    except transcribe.RealtimeAPIError as error:
        assert "network failed" in str(error)
    else:
        raise AssertionError("RealtimeAPIError was not raised")
    assert written == ["Saved."]


async def test_cards_receive_original_text_and_close_with_translation() -> None:
    pipeline_instances: list[FakePipeline] = []
    viewer_instances: list[FakeViewer] = []

    class FakePipeline:
        def __init__(self, api_key: str, **options: object) -> None:
            self.api_key = api_key
            self.options = options
            self.json_path = Path("cards.json")
            self.html_path = Path("cards.html")
            self.texts: list[str] = []
            self.started = False
            self.closed = False
            pipeline_instances.append(self)

        def start(self) -> None:
            self.started = True

        def add(self, text: str) -> None:
            self.texts.append(text)

        async def close(self) -> list[object]:
            self.closed = True
            return []

    class FakeViewer:
        def __init__(self, cards_path: Path, port: int) -> None:
            self.cards_path = cards_path
            self.port = port
            self.url = f"http://127.0.0.1:{port}"
            self.started = False
            self.stopped = False
            viewer_instances.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

    class FakeProcess:
        returncode = 0

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, message: str) -> None:
            self.sent.append(message)

    websocket = FakeWebSocket()

    class FakeConnection:
        async def __aenter__(self) -> FakeWebSocket:
            return websocket

        async def __aexit__(self, *_: object) -> None:
            return None

    connect_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def connect(*args: object, **options: object) -> FakeConnection:
        connect_calls.append((args, options))
        return FakeConnection()

    client_module = ModuleType("websockets.asyncio.client")
    client_module.connect = connect  # type: ignore[attr-defined]
    asyncio_module = ModuleType("websockets.asyncio")
    asyncio_module.client = client_module  # type: ignore[attr-defined]
    websockets_module = ModuleType("websockets")
    websockets_module.asyncio = asyncio_module  # type: ignore[attr-defined]

    finalized = asyncio.Event()

    async def fake_stream(
        _: object,
        __: object,
        stop_event: asyncio.Event,
        ___: object,
    ) -> None:
        await finalized.wait()
        stop_event.set()

    async def fake_receive(
        _: object,
        __: object,
        ___: asyncio.Event,
        finalize_text: object,
    ) -> None:
        assert callable(finalize_text)
        await finalize_text("Original text")
        finalized.set()
        await asyncio.Event().wait()

    with tempfile.TemporaryDirectory() as directory:
        transcript_path = Path(directory) / "transcript.md"
        with (
            patch.dict(
                sys.modules,
                {
                    "websockets": websockets_module,
                    "websockets.asyncio": asyncio_module,
                    "websockets.asyncio.client": client_module,
                },
            ),
            patch("transcribe.CardPipeline", FakePipeline),
            patch("transcribe.ViewerServer", FakeViewer),
            patch("transcribe.export_cards") as export_cards,
            patch("transcribe.subprocess.run") as open_browser,
            patch(
                "transcribe.asyncio.create_subprocess_exec",
                return_value=FakeProcess(),
            ),
            patch("transcribe.stream_audio", fake_stream),
            patch("transcribe.receive_events", fake_receive),
            patch("transcribe.transcript_path", return_value=transcript_path),
            patch(
                "transcribe.translate_to_japanese", return_value="日本語訳"
            ) as translate,
        ):
            result = await transcribe.run_transcription(
                0,
                "eleven-key",
                "openai-key",
                "/usr/bin/ffmpeg",
                translate_to_ja=True,
                cards_enabled=True,
                cards_port=9000,
                cards_character_threshold=120,
                cards_idle_seconds=8,
                cards_max_seconds=45,
            )

        saved = transcript_path.read_text(encoding="utf-8")

    assert result == transcript_path
    assert "Original text" in saved
    assert "日本語訳" in saved
    assert connect_calls[0][0] == (transcribe.realtime_url(),)
    assert connect_calls[0][1]["additional_headers"] == {
        "xi-api-key": "eleven-key"
    }
    translate.assert_called_once_with("Original text", "openai-key")
    pipeline = pipeline_instances[0]
    assert pipeline.api_key == "openai-key"
    assert pipeline.options == {
        "character_threshold": 120,
        "idle_seconds": 8,
        "max_seconds": 45,
    }
    assert pipeline.started
    assert pipeline.texts == ["Original text"]
    assert pipeline.closed
    viewer = viewer_instances[0]
    assert viewer.cards_path == pipeline.json_path
    assert viewer.started
    assert viewer.stopped
    open_browser.assert_called_once_with(["open", viewer.url], check=False)
    export_cards.assert_called_once_with([], pipeline.html_path)


def main() -> None:
    test_model_configuration()
    test_translation_configuration_and_output()
    test_translation_flag()
    test_cards_flags()
    test_api_key_auto_load()
    test_local_turn_detection()
    asyncio.run(test_idle_silence_is_not_uploaded())
    asyncio.run(test_audio_chunks_are_batched_and_committed())
    asyncio.run(test_stop_commits_buffered_audio())
    test_realtime_events_and_repeated_transcripts()
    test_missing_key_stops_before_capture()
    asyncio.run(test_capture_eof_and_process_cleanup())
    asyncio.run(test_websocket_error_preserves_completed_output())
    asyncio.run(test_cards_receive_original_text_and_close_with_translation())
    print("ok")


if __name__ == "__main__":
    main()
