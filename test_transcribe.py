"""Small offline checks for the realtime transcription CLI."""

from __future__ import annotations

import asyncio
import io
import os
import sys
from contextlib import redirect_stderr
from unittest.mock import patch

import transcribe


def test_model_configuration() -> None:
    assert (
        transcribe.WEBSOCKET_URL
        == "wss://api.openai.com/v1/realtime?intent=transcription"
    )
    session = transcribe.session_update()["session"]
    assert session["type"] == "transcription"
    assert session["audio"]["input"]["transcription"]["model"] == (
        "gpt-live-transcribe"
    )
    assert session["audio"]["input"]["transcription"]["languages"] == [
        "ja",
        "en",
        "ko",
    ]
    assert session["audio"]["input"]["turn_detection"] is None
    assert transcribe.CHUNK_MILLISECONDS == 10


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


def test_api_key_auto_load() -> None:
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(
            transcribe.Path,
            "read_text",
            return_value="export OPENAI_API_KEY='from-file'\n",
        ),
    ):
        assert transcribe.load_api_key() == "from-file"

    with (
        patch.dict(os.environ, {"OPENAI_API_KEY": "from-env"}, clear=True),
        patch.object(transcribe.Path, "read_text") as read_text,
    ):
        assert transcribe.load_api_key() == "from-env"
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


def test_event_order_and_deduplication() -> None:
    written: list[str] = []
    reducer = transcribe.TranscriptReducer(written.append)

    reducer.handle({"type": "input_audio_buffer.committed", "item_id": "a"})
    reducer.handle(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "a",
            "delta": "Hello ",
        }
    )
    reducer.handle(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "a",
            "delta": "world",
        }
    )
    reducer.handle({"type": "input_audio_buffer.committed", "item_id": "b"})
    reducer.handle(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "b",
            "transcript": "Second.",
        }
    )
    assert written == []

    reducer.handle(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "a",
            "transcript": "First.",
        }
    )
    assert reducer.partials["a"] == "Hello world"
    assert written == ["First.", "Second."]
    assert reducer.take_ready() == ["First.", "Second."]
    assert reducer.committed_item_ids == {"a", "b"}

    reducer.handle(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "a",
            "transcript": "Duplicate.",
        }
    )
    assert written == ["First.", "Second."]


def test_missing_key_stops_before_capture() -> None:
    stderr = io.StringIO()
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("transcribe.shutil.which", return_value="/usr/local/bin/ffmpeg"),
        patch("transcribe.load_api_key", return_value=""),
        patch("transcribe.asyncio.run") as run,
        redirect_stderr(stderr),
    ):
        assert transcribe.main(["--device", "0"]) == 2
    run.assert_not_called()
    assert "OPENAI_API_KEY" in stderr.getvalue()


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
        '{"type":"input_audio_buffer.speech_started","item_id":"a"}',
        '{"type":"conversation.item.input_audio_transcription.completed",'
        '"item_id":"a","transcript":"Saved."}',
        '{"type":"error","error":{"message":"network failed"}}',
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


def main() -> None:
    test_model_configuration()
    test_translation_configuration_and_output()
    test_translation_flag()
    test_api_key_auto_load()
    test_local_turn_detection()
    asyncio.run(test_idle_silence_is_not_uploaded())
    test_event_order_and_deduplication()
    test_missing_key_stops_before_capture()
    asyncio.run(test_capture_eof_and_process_cleanup())
    asyncio.run(test_websocket_error_preserves_completed_output())
    print("ok")


if __name__ == "__main__":
    main()
