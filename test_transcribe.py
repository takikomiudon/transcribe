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
    assert session["audio"]["input"]["turn_detection"] is None
    assert transcribe.CHUNK_MILLISECONDS == 10


def test_local_turn_detection() -> None:
    loud = (transcribe.SILENCE_RMS_THRESHOLD + 1).to_bytes(
        2, "little", signed=True
    ) * (transcribe.CHUNK_BYTES // 2)
    silence = bytes(transcribe.CHUNK_BYTES)
    detector = transcribe.LocalTurnDetector()

    assert not detector.observe(loud)
    silence_chunks = (
        transcribe.SILENCE_COMMIT_MILLISECONDS // transcribe.CHUNK_MILLISECONDS
    )
    for _ in range(silence_chunks - 1):
        assert not detector.observe(silence)
    assert detector.observe(silence)

    detector.mark_committed()
    assert detector.commits_sent == 1
    assert not detector.has_buffered_audio()

    max_chunks = transcribe.MAX_TURN_MILLISECONDS // transcribe.CHUNK_MILLISECONDS
    for _ in range(max_chunks - 1):
        assert not detector.observe(loud)
    assert detector.observe(loud)


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
    test_local_turn_detection()
    test_event_order_and_deduplication()
    test_missing_key_stops_before_capture()
    asyncio.run(test_capture_eof_and_process_cleanup())
    asyncio.run(test_websocket_error_preserves_completed_output())
    print("ok")


if __name__ == "__main__":
    main()
