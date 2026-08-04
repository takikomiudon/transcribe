"""Small offline checks for the realtime transcription CLI."""

from __future__ import annotations

import asyncio
import base64
import io
import inspect
import json
import os
import sys
import tempfile
import time
import urllib.error
import wave
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from cards import Card, CardGenerationError
import transcribe


def test_model_configuration() -> None:
    parsed = urlparse(transcribe.realtime_url())
    assert parsed.scheme == "wss"
    assert parsed.netloc == "api.elevenlabs.io"
    assert parsed.path == "/v1/speech-to-text/realtime"
    assert parse_qs(parsed.query) == {
        "model_id": ["scribe_v2_realtime"],
        "audio_format": ["pcm_16000"],
        "commit_strategy": ["vad"],
        "vad_silence_threshold_secs": ["0.8"],
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


def test_transcript_correction_configuration_and_fallback() -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "セブンイレブンです。"}
                    ],
                }
            ]
        }
    ).encode()
    with patch("transcribe.urllib.request.urlopen", return_value=response) as urlopen:
        corrected = transcribe.correct_transcript(
            "セルユニットです。",
            "コンビニの話です。",
            ["セブンイレブン"],
            "openai-key",
        )

    assert corrected == "セブンイレブンです。"
    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["model"] == transcribe.TRANSLATION_MODEL
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["store"] is False
    assert json.loads(payload["input"]) == {
        "context": "コンビニの話です。",
        "glossary": ["セブンイレブン"],
        "text": "セルユニットです。",
    }
    assert urlopen.call_args.kwargs["timeout"] == 10

    for error in (TimeoutError("slow"), urllib.error.URLError("offline")):
        stderr = io.StringIO()
        with (
            patch("transcribe.urllib.request.urlopen", side_effect=error),
            redirect_stderr(stderr),
        ):
            assert (
                transcribe.correct_transcript("原文です。", "", [], "openai-key")
                == "原文です。"
            )
        assert "[補正] 警告:" in stderr.getvalue()


def test_batch_transcription_request_and_output() -> None:
    result = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"text": "Final transcript."}),
        stderr="",
    )
    with patch("transcribe.subprocess.run", return_value=result) as run:
        text = transcribe.batch_transcribe(
            Path("recordings/session.wav"),
            "secret-key",
            "/usr/bin/curl",
        )

    assert text == "Final transcript."
    command = run.call_args.args[0]
    assert command == [
        "/usr/bin/curl",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--header",
        "@-",
        "--form",
        "file=@recordings/session.wav;type=audio/wav",
        "--form",
        "model_id=scribe_v2",
        "--form",
        "timestamps_granularity=none",
        "--form",
        "tag_audio_events=false",
        "https://api.elevenlabs.io/v1/speech-to-text",
    ]
    assert "secret-key" not in " ".join(command)
    assert run.call_args.kwargs == {
        "input": "xi-api-key: secret-key\n",
        "capture_output": True,
        "text": True,
        "check": False,
    }


def test_batch_transcription_errors_are_user_facing() -> None:
    for result, expected in (
        (
            SimpleNamespace(returncode=22, stdout="quota exceeded", stderr=""),
            "quota exceeded",
        ),
        (SimpleNamespace(returncode=0, stdout="not json", stderr=""), "JSON"),
        (
            SimpleNamespace(returncode=0, stdout='{"language_code":"ja"}', stderr=""),
            "text",
        ),
    ):
        with patch("transcribe.subprocess.run", return_value=result):
            try:
                transcribe.batch_transcribe(
                    Path("recordings/session.wav"),
                    "secret-key",
                    "/usr/bin/curl",
                )
            except transcribe.BatchTranscriptionError as error:
                assert expected in str(error)
            else:
                raise AssertionError("BatchTranscriptionError was not raised")


def test_recording_snapshot_is_a_complete_wav() -> None:
    first = bytes(transcribe.CHUNK_BYTES)
    second = bytes([1]) * transcribe.CHUNK_BYTES
    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "recording.wav"
        with (
            audio_path.open("xb") as audio_file,
            wave.open(audio_file, "wb") as recording,
        ):
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(transcribe.SAMPLE_RATE)
            recording.writeframesraw(first)
            recording.writeframesraw(second)
            snapshot = transcribe.snapshot_recording(
                recording, audio_file, audio_path
            )

            with wave.open(str(snapshot), "rb") as saved:
                assert saved.getnchannels() == 1
                assert saved.getsampwidth() == 2
                assert saved.getframerate() == transcribe.SAMPLE_RATE
                assert saved.readframes(saved.getnframes()) == first + second

            snapshot.unlink()


def test_batch_refresh_helpers() -> None:
    assert transcribe.should_refresh_final(0, 0)
    assert not transcribe.should_refresh_final(100, 149)
    assert transcribe.should_refresh_final(100, 150)
    assert transcribe.should_refresh_final(100, 151)

    assert transcribe.merge_glossary(
        ["既存語", "共通語", "古い語"],
        ["新出語", "共通語", "新出語"],
        limit=4,
    ) == ["新出語", "共通語", "既存語", "古い語"]
    assert transcribe.merge_glossary(
        ["既存語", "古い語"], ["新出語"], limit=2
    ) == ["新出語", "既存語"]


def test_recording_tail_snapshot_is_a_valid_wav() -> None:
    frames = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "recording.wav"
        with (
            audio_path.open("xb") as audio_file,
            wave.open(audio_file, "wb") as recording,
        ):
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(transcribe.SAMPLE_RATE)
            recording.writeframesraw(frames)

            tail = transcribe.snapshot_recording_tail(
                recording,
                audio_file,
                audio_path,
                2 / transcribe.SAMPLE_RATE,
            )
            with wave.open(str(tail), "rb") as saved:
                assert saved.getnchannels() == 1
                assert saved.getsampwidth() == 2
                assert saved.getframerate() == transcribe.SAMPLE_RATE
                assert saved.getnframes() == 2
                assert saved.readframes(2) == frames[-4:]
            tail.unlink()

            complete = transcribe.snapshot_recording_tail(
                recording, audio_file, audio_path, seconds=1
            )
            with wave.open(str(complete), "rb") as saved:
                assert saved.getnframes() == 4
                assert saved.readframes(4) == frames
            complete.unlink()


async def test_periodic_batch_refresh_retries_without_overlap() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    active = 0
    maximum_active = 0
    attempts = 0
    snapshots: list[Path] = []
    recording = MagicMock()
    recording.tell.side_effect = [100, 100]

    def make_snapshot(*_: object) -> Path:
        temporary = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temporary.close()
        snapshot = Path(temporary.name)
        snapshots.append(snapshot)
        return snapshot

    def transcribe_snapshot(*_: object) -> str:
        nonlocal active, attempts, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        attempts += 1
        time.sleep(0.02)
        active -= 1
        if attempts == 1:
            raise transcribe.BatchTranscriptionError("temporary failure")
        loop.call_soon_threadsafe(stop_event.set)
        return "Updated transcript."

    with tempfile.TemporaryDirectory() as directory:
        realtime_path = Path(directory) / "session.md"
        with (
            patch(
                "transcribe.snapshot_recording", side_effect=make_snapshot
            ) as full_snapshot,
            patch("transcribe.snapshot_recording_tail") as tail_snapshot,
            patch("transcribe.batch_transcribe", side_effect=transcribe_snapshot),
        ):
            await transcribe.periodically_refresh_final(
                realtime_path,
                Path("recording.wav"),
                recording,
                object(),
                "secret-key",
                "/usr/bin/curl",
                stop_event,
                interval_seconds=0.01,
            )

        final_path = transcribe.final_transcript_path(realtime_path)
        assert "Updated transcript." in final_path.read_text(encoding="utf-8")

    assert attempts == 2
    assert maximum_active == 1
    assert full_snapshot.call_count == 2
    tail_snapshot.assert_not_called()
    assert all(not snapshot.exists() for snapshot in snapshots)


async def test_periodic_batch_failure_keeps_previous_final() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    temporary = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    temporary.close()
    snapshot = Path(temporary.name)
    recording = MagicMock()
    recording.tell.return_value = 100

    def fail(*_: object) -> str:
        loop.call_soon_threadsafe(stop_event.set)
        raise transcribe.BatchTranscriptionError("temporary failure")

    with tempfile.TemporaryDirectory() as directory:
        realtime_path = Path(directory) / "session.md"
        final_path = transcribe.final_transcript_path(realtime_path)
        final_path.write_text("previous checkpoint", encoding="utf-8")
        with (
            patch("transcribe.snapshot_recording", return_value=snapshot),
            patch("transcribe.batch_transcribe", side_effect=fail),
        ):
            await transcribe.periodically_refresh_final(
                realtime_path,
                Path("recording.wav"),
                recording,
                object(),
                "secret-key",
                "/usr/bin/curl",
                stop_event,
                interval_seconds=0.01,
            )

        assert final_path.read_text(encoding="utf-8") == "previous checkpoint"
    assert not snapshot.exists()


async def test_periodic_batch_refresh_updates_correction_glossary() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    temporary = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    temporary.close()
    snapshot = Path(temporary.name)
    transcript = "前" * 4_100 + "セブンイレブンとOpenAI"
    glossary = ["古い用語"]
    recording = MagicMock()
    recording.tell.return_value = 100

    def transcribe_snapshot(*_: object) -> str:
        loop.call_soon_threadsafe(stop_event.set)
        return transcript

    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "terms": [
                                        " セブンイレブン ",
                                        "OpenAI",
                                        "セブンイレブン",
                                    ]
                                }
                            ),
                        }
                    ],
                }
            ]
        }
    ).encode()

    with tempfile.TemporaryDirectory() as directory:
        realtime_path = Path(directory) / "session.md"
        with (
            patch("transcribe.snapshot_recording", return_value=snapshot),
            patch("transcribe.batch_transcribe", side_effect=transcribe_snapshot),
            patch(
                "transcribe.urllib.request.urlopen", return_value=response
            ) as urlopen,
        ):
            await transcribe.periodically_refresh_final(
                realtime_path,
                Path("recording.wav"),
                recording,
                object(),
                "eleven-key",
                "/usr/bin/curl",
                stop_event,
                interval_seconds=0.01,
                glossary=glossary,
                openai_api_key="openai-key",
            )

    assert glossary == ["セブンイレブン", "OpenAI", "古い用語"]
    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["input"] == transcript[-4_000:]

    received_glossaries: list[list[str]] = []

    def capture_glossary(
        text: str, context: str, terms: list[str], api_key: str
    ) -> str:
        received_glossaries.append(terms)
        return text

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def finalize_text(_: str) -> None:
        return None

    with patch("transcribe.correct_transcript", side_effect=capture_glossary):
        worker = asyncio.create_task(
            transcribe.process_corrections(
                queue, glossary, "openai-key", finalize_text
            )
        )
        queue.put_nowait("次の発話")
        queue.put_nowait(None)
        await queue.join()
        await worker

    assert received_glossaries == [["セブンイレブン", "OpenAI", "古い用語"]]


async def test_periodic_batch_refresh_uses_growth_and_tail_windows() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    recording = MagicMock()
    recording.tell.side_effect = [100, 149, 150, 224, 225]
    glossary = ["既存語"]
    snapshots: list[Path] = []
    transcriptions = iter(
        ["全文100", "末尾149", "全文150", "末尾224", "全文225"]
    )

    def make_snapshot(*_: object) -> Path:
        temporary = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temporary.close()
        snapshot = Path(temporary.name)
        snapshots.append(snapshot)
        return snapshot

    def transcribe_snapshot(*_: object) -> str:
        text = next(transcriptions)
        if text == "全文225":
            loop.call_soon_threadsafe(stop_event.set)
        return text

    with tempfile.TemporaryDirectory() as directory:
        realtime_path = Path(directory) / "session.md"
        final_path = transcribe.final_transcript_path(realtime_path)
        with (
            patch(
                "transcribe.snapshot_recording", side_effect=make_snapshot
            ) as full_snapshot,
            patch(
                "transcribe.snapshot_recording_tail", side_effect=make_snapshot
            ) as tail_snapshot,
            patch("transcribe.batch_transcribe", side_effect=transcribe_snapshot),
            patch(
                "transcribe.extract_glossary",
                side_effect=lambda text, _: [text],
            ),
            patch(
                "transcribe.write_batch_transcript", return_value=final_path
            ) as write_final,
        ):
            await transcribe.periodically_refresh_final(
                realtime_path,
                Path("recording.wav"),
                recording,
                object(),
                "eleven-key",
                "/usr/bin/curl",
                stop_event,
                interval_seconds=0.01,
                glossary=glossary,
                openai_api_key="openai-key",
            )

    assert full_snapshot.call_count == 3
    assert tail_snapshot.call_count == 2
    assert all(value.args[3] == 60 for value in tail_snapshot.call_args_list)
    assert [value.args[1] for value in write_final.call_args_list] == [
        "全文100",
        "全文150",
        "全文225",
    ]
    assert glossary == [
        "全文225",
        "末尾224",
        "全文150",
        "末尾149",
        "全文100",
        "既存語",
    ]
    assert all(not snapshot.exists() for snapshot in snapshots)


async def test_periodic_batch_skips_tail_without_glossary() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    recording = MagicMock()
    frame_counts = iter([100, 149])
    batch_calls = 0
    snapshots: list[Path] = []

    def current_frames() -> int:
        value = next(frame_counts)
        if value == 149:
            loop.call_soon(stop_event.set)
        return value

    def make_snapshot(*_: object) -> Path:
        temporary = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temporary.close()
        snapshot = Path(temporary.name)
        snapshots.append(snapshot)
        return snapshot

    def transcribe_snapshot(*_: object) -> str:
        nonlocal batch_calls
        batch_calls += 1
        if batch_calls == 2:
            loop.call_soon_threadsafe(stop_event.set)
        return "全文"

    recording.tell.side_effect = current_frames
    with tempfile.TemporaryDirectory() as directory:
        with (
            patch("transcribe.snapshot_recording", side_effect=make_snapshot),
            patch("transcribe.snapshot_recording_tail") as tail_snapshot,
            patch("transcribe.batch_transcribe", side_effect=transcribe_snapshot),
        ):
            await transcribe.periodically_refresh_final(
                Path(directory) / "session.md",
                Path("recording.wav"),
                recording,
                object(),
                "eleven-key",
                "/usr/bin/curl",
                stop_event,
                interval_seconds=0.01,
            )

    assert batch_calls == 1
    tail_snapshot.assert_not_called()
    assert all(not snapshot.exists() for snapshot in snapshots)


async def test_correction_worker_preserves_original_and_order() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output_path = Path(directory) / "session.md"
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def write_final(text: str) -> None:
            with output_path.open("a", encoding="utf-8") as output:
                output.write(text + "\n")

        stderr = io.StringIO()
        with (
            patch(
                "transcribe.urllib.request.urlopen",
                side_effect=TimeoutError("slow"),
            ),
            redirect_stderr(stderr),
        ):
            worker = asyncio.create_task(
                transcribe.process_corrections(
                    queue, [], "openai-key", write_final
                )
            )
            queue.put_nowait("失わない原文")
            queue.put_nowait(None)
            await queue.join()
            await worker

        assert output_path.read_text(encoding="utf-8") == "失わない原文\n"
        assert "[補正] 警告:" in stderr.getvalue()

    calls: list[tuple[str, str]] = []
    written: list[str] = []

    def correct_in_different_durations(
        text: str, context: str, glossary: list[str], api_key: str
    ) -> str:
        calls.append((text, context))
        time.sleep(0.02 if text == "first" else 0)
        return f"{text}-corrected"

    async def collect(text: str) -> None:
        written.append(text)

    queue = asyncio.Queue()
    with patch(
        "transcribe.correct_transcript", side_effect=correct_in_different_durations
    ):
        worker = asyncio.create_task(
            transcribe.process_corrections(queue, [], "openai-key", collect)
        )
        for text in ("first", "second", "third"):
            queue.put_nowait(text)
        queue.put_nowait(None)
        await queue.join()
        await worker

    assert written == ["first-corrected", "second-corrected", "third-corrected"]
    assert calls[1][1] == "first-corrected"
    assert len(calls[2][1]) <= 500


def test_completed_batch_is_saved_before_recording_is_deleted() -> None:
    with tempfile.TemporaryDirectory() as directory:
        realtime_path = Path(directory) / "20260804-120000.md"
        audio_path = Path(directory) / "20260804-120000.wav"
        audio_path.write_bytes(b"audio")
        with patch(
            "transcribe.batch_transcribe", return_value="Final transcript."
        ):
            final_path, final_text = transcribe.finalize_recording(
                realtime_path,
                audio_path,
                "secret-key",
                "/usr/bin/curl",
            )

        assert final_path == Path(directory) / "20260804-120000-final.md"
        assert final_text == "Final transcript."
        assert "Final transcript." in final_path.read_text(encoding="utf-8")
        assert not audio_path.exists()


def test_batch_or_save_failure_preserves_recording() -> None:
    with tempfile.TemporaryDirectory() as directory:
        realtime_path = Path(directory) / "20260804-120000.md"
        audio_path = Path(directory) / "20260804-120000.wav"
        audio_path.write_bytes(b"audio")
        with patch(
            "transcribe.batch_transcribe",
            side_effect=transcribe.BatchTranscriptionError("API failed"),
        ):
            try:
                transcribe.finalize_recording(
                    realtime_path,
                    audio_path,
                    "secret-key",
                    "/usr/bin/curl",
                )
            except transcribe.BatchTranscriptionError:
                pass
            else:
                raise AssertionError("BatchTranscriptionError was not raised")
        assert audio_path.exists()

        final_path = Path(directory) / "20260804-120000-final.md"
        final_path.write_text("existing", encoding="utf-8")
        with (
            patch("transcribe.batch_transcribe", return_value="Final transcript."),
            patch(
                "transcribe.Path.replace",
                side_effect=OSError("disk failed"),
            ),
        ):
            try:
                transcribe.finalize_recording(
                    realtime_path,
                    audio_path,
                    "secret-key",
                    "/usr/bin/curl",
                )
            except transcribe.BatchTranscriptionError:
                pass
            else:
                raise AssertionError("BatchTranscriptionError was not raised")
        assert audio_path.exists()
        assert final_path.read_text(encoding="utf-8") == "existing"


def test_final_transcript_replaces_previous_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as directory:
        realtime_path = Path(directory) / "20260804-120000.md"
        audio_path = Path(directory) / "20260804-120000.wav"
        final_path = Path(directory) / "20260804-120000-final.md"
        audio_path.write_bytes(b"audio")
        final_path.write_text("old checkpoint", encoding="utf-8")
        with patch(
            "transcribe.batch_transcribe", return_value="Complete transcript."
        ):
            transcribe.finalize_recording(
                realtime_path,
                audio_path,
                "secret-key",
                "/usr/bin/curl",
            )

        assert "Complete transcript." in final_path.read_text(encoding="utf-8")
        assert "old checkpoint" not in final_path.read_text(encoding="utf-8")
        assert not audio_path.exists()


def test_transcript_header_hides_model_version() -> None:
    output = io.StringIO()
    transcribe.write_header(output, 0)
    header = output.getvalue()
    assert "- Model: `ElevenLabs Scribe Realtime`" in header
    assert "scribe_v2_realtime" not in header


def test_translation_flag() -> None:
    args = transcribe.build_parser().parse_args(
        ["--device", "0", "--translate-ja"]
    )
    assert args.translate_ja
    assert "no_correction" not in vars(args)
    assert "silence_threshold" not in vars(args)

    with redirect_stderr(io.StringIO()):
        try:
            transcribe.build_parser().parse_args(["--device", "0", "--no-correction"])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("--no-correction must be rejected")

    assert "correction_enabled" not in inspect.signature(
        transcribe.run_transcription
    ).parameters


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
    assert args.batch_refresh_seconds == transcribe.BATCH_REFRESH_SECONDS

    args = transcribe.build_parser().parse_args(
        ["--device", "0", "--batch-refresh-seconds", "60"]
    )
    assert args.batch_refresh_seconds == 60


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
        assert transcribe.load_api_key("ELEVENLABS_API_KEY") == "eleven-from-file"
        assert transcribe.load_api_key("OPENAI_API_KEY") == "openai-from-file"

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
        assert transcribe.load_api_key("ELEVENLABS_API_KEY") == "eleven-from-env"
        assert transcribe.load_api_key("OPENAI_API_KEY") == "openai-from-env"
        read_text.assert_not_called()


async def test_all_audio_is_streamed_in_batches_and_committed_on_stop() -> None:
    stop_event = asyncio.Event()

    class SilenceStdout:
        def __init__(self) -> None:
            self.remaining = 11

        async def readexactly(self, expected: int) -> bytes:
            if self.remaining:
                self.remaining -= 1
                return bytes(expected)
            stop_event.set()
            raise asyncio.IncompleteReadError(partial=b"", expected=expected)

    class FakeProcess:
        stdout = SilenceStdout()
        stderr = None

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, message: str) -> None:
            self.messages.append(message)

    websocket = FakeWebSocket()
    recorded = bytearray()
    committed = await transcribe.stream_audio(
        websocket,
        FakeProcess(),  # type: ignore[arg-type]
        stop_event,
        recorded.extend,
    )
    messages = [json.loads(message) for message in websocket.messages]
    assert committed
    assert len(messages) == 2
    assert messages[0]["commit"] is False
    assert base64.b64decode(messages[0]["audio_base_64"]) == bytes(
        transcribe.UPLOAD_CHUNK_BYTES
    )
    assert messages[1]["commit"] is True
    assert base64.b64decode(messages[1]["audio_base_64"]) == bytes(
        transcribe.CHUNK_BYTES
    )
    assert recorded == bytes(transcribe.CHUNK_BYTES * 11)


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
        patch("transcribe.load_api_key", return_value=""),
        patch("transcribe.asyncio.run") as run,
        redirect_stderr(stderr),
    ):
        assert transcribe.main(["--device", "0"]) == 2
    run.assert_not_called()
    assert "ELEVENLABS_API_KEY" in stderr.getvalue()


def test_openai_key_is_always_required() -> None:
    def load_key(name: str) -> str:
        return "eleven-key" if name == "ELEVENLABS_API_KEY" else ""

    stderr = io.StringIO()
    with (
        patch("transcribe.shutil.which", return_value="/usr/local/bin/ffmpeg"),
        patch("transcribe.load_api_key", side_effect=load_key),
        patch("transcribe.asyncio.run") as run,
        redirect_stderr(stderr),
    ):
        assert transcribe.main(["--device", "0"]) == 2
    run.assert_not_called()
    assert "OPENAI_API_KEY" in stderr.getvalue()


def test_batch_failure_returns_error_and_reports_preserved_recording() -> None:
    stderr = io.StringIO()
    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "session.wav"
        audio_path.write_bytes(b"audio")
        viewer_server = MagicMock()
        with (
            patch(
                "transcribe.shutil.which",
                side_effect=lambda name: f"/usr/bin/{name}",
            ),
            patch("transcribe.load_api_key", return_value="eleven-key"),
            patch(
                "transcribe.run_transcription",
                new_callable=MagicMock,
                return_value="coroutine",
            ),
            patch(
                "transcribe.asyncio.run",
                return_value=(
                    Path("transcripts/session.md"),
                    audio_path,
                    None,
                    None,
                    viewer_server,
                ),
            ),
            patch(
                "transcribe.finalize_recording",
                side_effect=transcribe.BatchTranscriptionError("API failed"),
            ),
            redirect_stderr(stderr),
        ):
            assert transcribe.main(["--device", "0"]) == 1

        assert audio_path.exists()
        assert "API failed" in stderr.getvalue()
        assert str(audio_path) in stderr.getvalue()
        viewer_server.stop.assert_called_once_with()


def test_final_cards_replace_live_artifacts_only_on_success() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        output_path = root / "session.md"
        audio_path = root / "session.wav"
        final_path = root / "session-final.md"
        cards_path = root / "cards.json"
        html_path = root / "cards.html"
        quality_card = Card(
            "final",
            "Final card",
            '<div class="callout"><p>Accurate</p></div>',
            "Accurate final transcript.",
            1,
            "done",
        )
        live_card = Card(
            "live",
            "Live card",
            '<div class="callout"><p>Fast</p></div>',
            "Realtime transcript.",
            1,
            "done",
        )

        for error in (None, CardGenerationError("failed")):
            live_json = json.dumps([asdict(live_card)])
            cards_path.write_text(live_json, encoding="utf-8")
            html_path.write_text("live html", encoding="utf-8")
            final_cards_path = root / "cards-final.json"
            final_cards_path.unlink(missing_ok=True)
            viewer_server = MagicMock()
            stderr = io.StringIO()
            with (
                patch(
                    "transcribe.shutil.which",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                patch(
                    "transcribe.load_api_key",
                    side_effect=lambda name: (
                        "eleven-key" if name == "ELEVENLABS_API_KEY" else "openai-key"
                    ),
                ),
                patch(
                    "transcribe.run_transcription",
                    new_callable=MagicMock,
                    return_value="coroutine",
                ),
                patch(
                    "transcribe.asyncio.run",
                    return_value=(
                        output_path,
                        audio_path,
                        cards_path,
                        html_path,
                        viewer_server,
                    ),
                ),
                patch(
                    "transcribe.finalize_recording",
                    return_value=(final_path, "Accurate final transcript."),
                ),
                patch(
                    "transcribe.generate_final_cards",
                    side_effect=error,
                    return_value=[quality_card],
                ) as generate,
                redirect_stderr(stderr),
            ):
                assert transcribe.main(["--device", "0", "--cards"]) == 0

            generate.assert_called_once_with(
                "Accurate final transcript.", "openai-key"
            )
            if error is None:
                assert cards_path.read_text(encoding="utf-8") == live_json
                assert json.loads(
                    final_cards_path.read_text(encoding="utf-8")
                )[0]["title"] == "Final card"
                assert "Live card" in html_path.read_text(encoding="utf-8")
                assert "Final card" in html_path.read_text(encoding="utf-8")
                viewer_server.wait_for_final_cards.assert_called_once_with()
            else:
                assert cards_path.read_text(encoding="utf-8") == live_json
                assert not final_cards_path.exists()
                assert html_path.read_text(encoding="utf-8") == "live html"
                assert "速報版を保持" in stderr.getvalue()
                viewer_server.wait_for_final_cards.assert_not_called()
            viewer_server.stop.assert_called_once_with()


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
            lambda _: None,
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


async def test_realtime_text_is_hidden_and_provider_error_events() -> None:
    class FakeWebSocket:
        def __init__(self, messages: list[str]) -> None:
            self.source = messages

        def __aiter__(self):
            self.messages = iter(self.source)
            return self

        async def __anext__(self) -> str:
            try:
                return next(self.messages)
            except StopIteration as error:
                raise StopAsyncIteration from error

    written: list[str] = []
    output = io.StringIO()
    reducer = transcribe.TranscriptReducer(written.append)
    with redirect_stdout(output):
        try:
            await transcribe.receive_events(
                FakeWebSocket(
                    [
                        '{"message_type":"partial_transcript","text":"Hel"}',
                        '{"message_type":"committed_transcript","text":"Hello"}',
                    ]
                ),
                reducer,
                asyncio.Event(),
            )
        except transcribe.TranscriptionError:
            pass
        else:
            raise AssertionError("TranscriptionError was not raised")
    assert output.getvalue() == ""
    assert written == ["Hello"]

    for event_type in (
        "auth_error",
        "quota_exceeded",
        "unaccepted_terms",
        "rate_limited",
        "input_error",
    ):
        try:
            await transcribe.receive_events(
                FakeWebSocket(
                    [
                        json.dumps(
                            {"message_type": event_type, "error": event_type}
                        )
                    ]
                ),
                transcribe.TranscriptReducer(lambda _: None),
                asyncio.Event(),
            )
        except transcribe.RealtimeAPIError as error:
            assert str(error) == event_type
        else:
            raise AssertionError(f"{event_type} was not raised")


async def test_corrected_text_reaches_all_outputs() -> None:
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
        def __init__(
            self,
            transcript_path: Path,
            cards_path: Path | None,
            port: int,
        ) -> None:
            self.transcript_path = transcript_path
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
        record_audio: object,
    ) -> bool:
        assert callable(record_audio)
        record_audio(bytes(transcribe.CHUNK_BYTES))
        await finalized.wait()
        stop_event.set()
        return False

    async def fake_receive(
        _: object,
        reducer: transcribe.TranscriptReducer,
        ___: asyncio.Event,
        finalize_text: object,
    ) -> None:
        if callable(finalize_text):
            await finalize_text("Original text")
        else:
            reducer.handle(
                {"message_type": "committed_transcript", "text": "Original text"}
            )
        finalized.set()
        await asyncio.Event().wait()

    with tempfile.TemporaryDirectory() as directory:
        transcript_path = Path(directory) / "transcript.md"
        recording_path = Path(directory) / "recording.wav"
        terminal = io.StringIO()
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
            patch(
                "transcribe.transcript_path",
                return_value=transcript_path,
            ),
            patch(
                "transcribe.recording_path",
                return_value=recording_path,
            ),
            patch(
                "transcribe.correct_transcript", return_value="Corrected text"
            ) as correct,
            patch(
                "transcribe.translate_to_japanese", return_value="日本語訳"
            ) as translate,
            redirect_stdout(terminal),
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
        with wave.open(str(recording_path), "rb") as recording:
            assert recording.getnchannels() == 1
            assert recording.getsampwidth() == 2
            assert recording.getframerate() == transcribe.SAMPLE_RATE
            assert recording.readframes(recording.getnframes()) == bytes(
                transcribe.CHUNK_BYTES
            )

    assert result == (
        transcript_path,
        recording_path,
        pipeline_instances[0].json_path,
        pipeline_instances[0].html_path,
        viewer_instances[0],
    )
    assert "Corrected text" in saved
    assert "Original text" not in saved
    assert "日本語訳" in saved
    assert "Corrected text\n" in terminal.getvalue()
    assert "Original text" not in terminal.getvalue()
    assert connect_calls[0][0] == (transcribe.realtime_url(),)
    assert connect_calls[0][1]["additional_headers"] == {
        "xi-api-key": "eleven-key"
    }
    correct.assert_called_once_with("Original text", "", [], "openai-key")
    translate.assert_called_once_with("Corrected text", "openai-key")
    pipeline = pipeline_instances[0]
    assert pipeline.api_key == "openai-key"
    assert pipeline.options == {
        "character_threshold": 120,
        "idle_seconds": 8,
        "max_seconds": 45,
    }
    assert pipeline.started
    assert pipeline.texts == ["Corrected text"]
    assert pipeline.closed
    viewer = viewer_instances[0]
    assert viewer.transcript_path == transcribe.final_transcript_path(
        transcript_path
    )
    assert viewer.cards_path == pipeline.json_path
    assert viewer.started
    assert not viewer.stopped
    open_browser.assert_called_once_with(["open", viewer.url], check=False)
    export_cards.assert_called_once_with([], pipeline.html_path)


def main() -> None:
    test_model_configuration()
    test_translation_configuration_and_output()
    test_transcript_correction_configuration_and_fallback()
    test_batch_transcription_request_and_output()
    test_batch_transcription_errors_are_user_facing()
    test_recording_snapshot_is_a_complete_wav()
    test_batch_refresh_helpers()
    test_recording_tail_snapshot_is_a_valid_wav()
    asyncio.run(test_periodic_batch_refresh_retries_without_overlap())
    asyncio.run(test_periodic_batch_failure_keeps_previous_final())
    asyncio.run(test_periodic_batch_refresh_updates_correction_glossary())
    asyncio.run(test_periodic_batch_refresh_uses_growth_and_tail_windows())
    asyncio.run(test_periodic_batch_skips_tail_without_glossary())
    asyncio.run(test_correction_worker_preserves_original_and_order())
    test_completed_batch_is_saved_before_recording_is_deleted()
    test_batch_or_save_failure_preserves_recording()
    test_final_transcript_replaces_previous_checkpoint()
    test_transcript_header_hides_model_version()
    test_translation_flag()
    test_cards_flags()
    test_api_key_auto_load()
    asyncio.run(test_all_audio_is_streamed_in_batches_and_committed_on_stop())
    test_realtime_events_and_repeated_transcripts()
    test_missing_key_stops_before_capture()
    test_openai_key_is_always_required()
    test_batch_failure_returns_error_and_reports_preserved_recording()
    test_final_cards_replace_live_artifacts_only_on_success()
    asyncio.run(test_capture_eof_and_process_cleanup())
    asyncio.run(test_websocket_error_preserves_completed_output())
    asyncio.run(test_realtime_text_is_hidden_and_provider_error_events())
    asyncio.run(test_corrected_text_reaches_all_outputs())
    print("ok")


if __name__ == "__main__":
    main()
