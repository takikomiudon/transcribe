"""Offline checks for browser recording runners and WebSocket routing."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import ai
import card_compiler
import card_models
import cards
import transcribe
import transcript_segments
from webapp.app import app
from webapp.config import ensure_dirs
from webapp.runner import (
    RunnerBusyError,
    RunnerRegistry,
    SessionFinalizer,
    SessionRunner,
)
from webapp.store import Session, SessionStore
from webapp.ws import WebSocketHub


class FakeElevenLabsWS:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.connect_args: tuple[tuple[object, ...], dict[str, object]] | None = None
        self.initial_events_sent = False
        self.closed = False

    def __aiter__(self) -> FakeElevenLabsWS:
        return self

    async def __anext__(self) -> str:
        event = await self.incoming.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        self.sent.append(payload)
        if not payload["commit"] and not self.initial_events_sent:
            self.initial_events_sent = True
            self.incoming.put_nowait(
                {"message_type": "partial_transcript", "text": "hel"}
            )
            self.incoming.put_nowait(
                {"message_type": "committed_transcript", "text": "hello"}
            )
        if payload["commit"]:
            self.incoming.put_nowait(
                {"message_type": "committed_transcript", "text": ""}
            )

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)


class FakeBrowserWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def send_json(self, event: dict[str, object]) -> None:
        self.events.append(event)


def fake_correct(
    text: str, _: str, __: list[str], ___: str, ____: object
) -> str:
    return f"補正:{text}"


def fake_card_generator(*_: object) -> dict[str, object]:
    return {
        "decision": "create",
        "card_ids": [],
        "title": "Live card",
        "html": "<p>Live</p>",
        "summary": "Live summary",
        "keywords": ["live"],
    }


def fake_final_compiler(
    segments: list[transcript_segments.TranscriptSegment],
    _: str,
    __: object,
) -> card_compiler.CompilationResult:
    topic = card_models.Topic(
        "topic-0001", "Final topic", "Summary", [segments[0].id]
    )
    unit = card_models.KnowledgeUnit(
        "unit-0001",
        topic.id,
        "claim",
        "Final card",
        "Final",
        [],
        [segments[0].id],
    )
    return card_compiler.CompilationResult([topic], [unit], [], [])


def fake_final_renderer(*_: object) -> list[cards.Card]:
    return [
        cards.Card(
            id="final-card",
            title="Final card",
            html='<div class="callout"><p>Final</p></div>',
            source_text="Batch final",
            created_at=1,
            status="done",
        )
    ]


async def wait_until(predicate, timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def make_runner(
    session: Session,
    store: SessionStore,
    root: Path,
    registry: RunnerRegistry,
    websocket: FakeElevenLabsWS,
    events: list[dict[str, object]],
) -> SessionRunner:
    async def connect(*args: object, **kwargs: object) -> FakeElevenLabsWS:
        websocket.connect_args = (args, kwargs)
        return websocket

    async def broadcast(event: dict[str, object]) -> None:
        events.append(event)

    return SessionRunner(
        session,
        store,
        root,
        connect=connect,
        batch_fn=lambda *_: "Batch final",
        batch_words_fn=lambda *_: transcript_segments.BatchTranscript(
            "Batch final",
            [
                transcript_segments.TranscriptWord(
                    "word-000001", "Batch final", 0, 1_000
                )
            ],
        ),
        correct_fn=fake_correct,
        glossary_fn=lambda *_: [],
        card_generator=fake_card_generator,
        final_compiler=fake_final_compiler,
        final_renderer=fake_final_renderer,
        title_fn=lambda _text, _key, _model: "Final title",
        elevenlabs_key="eleven-key",
        openai_key="openai-key",
        deepseek_key="deepseek-key",
        curl_path="/usr/bin/curl",
        refresh_interval=3_600,
        registry=registry,
        broadcast=broadcast,
    )


def test_deepseek_runner_resolves_current_model_and_key() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        session.ai_provider = ai.DEEPSEEK_FLASH_MODEL.provider
        session.ai_model = ai.DEEPSEEK_FLASH_MODEL.model
        store.save(session)
        runner = SessionRunner(
            session,
            store,
            root,
            openai_key="openai-key",
            deepseek_key="deepseek-key",
            registry=RunnerRegistry(),
        )

        with patch("ai.is_deepseek_peak_hour", return_value=True):
            assert runner._ai_credentials() == (
                "openai-key",
                ai.DEFAULT_AI_MODEL,
            )
        with patch("ai.is_deepseek_peak_hour", return_value=False):
            assert runner._ai_credentials() == (
                "deepseek-key",
                ai.DEEPSEEK_FLASH_MODEL,
            )


async def test_runner_streams_corrects_and_finalizes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        registry = RunnerRegistry()
        websocket = FakeElevenLabsWS()
        events: list[dict[str, object]] = []
        runner = make_runner(session, store, root, registry, websocket, events)

        await runner.start()
        assert runner.feed_audio(bytes(4_000))
        await wait_until(
            lambda: any(event["type"] == "corrected" for event in events)
        )
        await runner.stop()
        await registry.finalizing[session.id]

        audio_path = root / session.paths["audio"]
        with wave.open(str(audio_path), "rb") as recording:
            assert recording.getnframes() == 2_000

        audio_messages = websocket.sent
        assert base64.b64decode(audio_messages[0]["audio_base_64"]) == bytes(3_200)
        assert audio_messages[0] == {
            "message_type": "input_audio_chunk",
            "audio_base_64": audio_messages[0]["audio_base_64"],
            "commit": False,
            "sample_rate": 16_000,
        }
        assert audio_messages[-1]["commit"] is True
        assert base64.b64decode(audio_messages[-1]["audio_base_64"]) == bytes(800)

        event_types = [event["type"] for event in events]
        assert event_types.index("partial") < event_types.index("committed")
        assert event_types.index("committed") < event_types.index("corrected")
        committed = next(event for event in events if event["type"] == "committed")
        corrected = next(event for event in events if event["type"] == "corrected")
        final_transcript_event = next(
            event for event in events if event["type"] == "final_transcript"
        )
        final_cards_event = next(
            event for event in events if event["type"] == "cards_final"
        )
        assert committed["seq"] == corrected["seq"] == 1
        assert corrected["text"] == "補正:hello"
        assert final_transcript_event["segments"][0]["id"] == "seg-0001"
        assert final_cards_event["outline"][0]["title"] == "Final topic"
        assert final_cards_event["knowledge"][0]["id"] == "unit-0001"

        transcript_path = root / session.paths["transcript"]
        assert "- Device: browser" in transcript_path.read_text(encoding="utf-8")
        assert "補正:hello" in transcript_path.read_text(encoding="utf-8")
        assert (root / session.paths["final_transcript"]).is_file()
        segment_payload = json.loads(
            (root / session.paths["segments"]).read_text(encoding="utf-8")
        )
        assert segment_payload["items"][0]["raw_text"] == "Batch final"
        assert (root / session.paths["outline"]).is_file()
        assert (root / session.paths["knowledge"]).is_file()
        final_cards = json.loads(
            (root / session.paths["final_cards"]).read_text(encoding="utf-8")
        )
        assert final_cards[0]["title"] == "Final card"
        live_cards = json.loads(
            (root / session.paths["cards"]).read_text(encoding="utf-8")
        )
        assert live_cards[0]["provisional"] is True
        assert live_cards[0]["reconciliation_history"] == [
            {"action": "drop", "final_card_ids": []}
        ]
        assert audio_path.is_file()

        saved = store.get(session.id)
        assert saved.state == "stopped"
        assert saved.finalized
        assert saved.duration_seconds == 2_000 / 16_000
        assert saved.title == "Final title"
        assert registry.active_session_id is None
        assert websocket.closed
        assert websocket.connect_args[0] == (transcribe.realtime_url(),)
        assert websocket.connect_args[1] == {
            "additional_headers": {"xi-api-key": "eleven-key"},
            "max_size": None,
        }


async def test_runner_stop_releases_before_background_finalizer_finishes() -> None:
    finalizer_started = asyncio.Event()
    finish_finalizer = asyncio.Event()

    class BlockingFinalizer(SessionFinalizer):
        async def run(self) -> None:
            await self._emit_status("finalizing")
            finalizer_started.set()
            await finish_finalizer.wait()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        first_session = store.create()
        second_session = store.create()
        registry = RunnerRegistry()
        events: list[dict[str, object]] = []
        first = make_runner(
            first_session, store, root, registry, FakeElevenLabsWS(), events
        )
        second = make_runner(
            second_session, store, root, registry, FakeElevenLabsWS(), []
        )

        with patch("webapp.runner.SessionFinalizer", BlockingFinalizer):
            await first.start()
            stop_task = asyncio.create_task(first.stop())
            await finalizer_started.wait()
            finalizer_task = registry.finalizing[first_session.id]
            stop_returned = stop_task.done()
            first_released = registry.active_session_id is None
            first_stopped = store.get(first_session.id).state == "stopped"
            second_started = False
            if first_released:
                await second.start()
                second_started = registry.active_session_id == second_session.id
                await second.stop(finalize=False)
            finish_finalizer.set()
            await stop_task
            await finalizer_task

        states = [
            event
            for event in events
            if event["type"] == "status"
        ]
        finalizing = next(
            event for event in states if event["state"] == "finalizing"
        )
        assert stop_returned
        assert first_released
        assert first_stopped
        assert second_started
        assert first_session.id not in registry.finalizing
        assert finalizing["active_session_id"] is None
        assert finalizing["session_id"] == first_session.id
        assert [event["state"] for event in states].index("idle") < [
            event["state"] for event in states
        ].index("finalizing")


async def test_status_broadcast_is_hub_wide_and_cleans_subject_recorder() -> None:
    hub = WebSocketHub()
    first = FakeBrowserWebSocket()
    second = FakeBrowserWebSocket()
    recorder = FakeBrowserWebSocket()
    hub.connections = {"first": {first}, "second": {second}}
    hub.recorders["first"] = recorder
    hub.runners["first"] = object()

    status_event = {
        "type": "status",
        "session_id": "first",
        "active_session_id": None,
        "state": "idle",
    }
    await hub.broadcast("second", status_event)
    await hub.broadcast("first", {"type": "partial", "text": "hello"})

    assert first.events == [
        status_event,
        {"type": "partial", "text": "hello"},
    ]
    assert second.events == [status_event]
    assert "first" not in hub.recorders
    assert "first" not in hub.runners


async def test_runner_stop_releases_registry_when_appender_close_fails(
    capsys,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        registry = RunnerRegistry()
        events: list[dict[str, object]] = []
        runner = make_runner(
            session, store, root, registry, FakeElevenLabsWS(), events
        )

        await runner.start()
        close = runner.appender.close

        def fail_close() -> None:
            raise OSError("disk full")

        runner.appender.close = fail_close
        try:
            await runner.stop(finalize=False)
        finally:
            runner.appender.close = close
            close()

        assert runner._stopped
        assert registry.active_session_id is None
        assert any(
            event["type"] == "status" and event["state"] == "idle"
            for event in events
        )
        assert "録音ファイルを閉じられませんでした" in capsys.readouterr().err


async def test_runner_stop_cancels_watch_task() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        runner = make_runner(
            session, store, root, RunnerRegistry(), FakeElevenLabsWS(), []
        )
        await runner.start()
        assert runner._watch_task is not None
        runner._watch_task.cancel()
        await asyncio.gather(runner._watch_task, return_exceptions=True)

        waiting = asyncio.Event()

        async def wait_forever() -> None:
            waiting.set()
            await asyncio.Event().wait()

        watch_task = asyncio.create_task(wait_forever())
        runner._watch_task = watch_task
        await waiting.wait()

        await runner.stop(finalize=False)

        assert watch_task.cancelled()


async def test_abort_start_cancels_created_worker_tasks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        runner = make_runner(
            session, store, root, RunnerRegistry(), FakeElevenLabsWS(), []
        )
        emit_calls = 0

        async def fail_first_session_event() -> None:
            nonlocal emit_calls
            emit_calls += 1
            if emit_calls == 1:
                raise OSError("late start failure")

        runner._emit_session = fail_first_session_event
        try:
            with pytest.raises(OSError, match="late start failure"):
                await runner.start()
            tasks = [
                task
                for task in (
                    runner._uplink_task,
                    runner._receiver_task,
                    runner._correction_task,
                    runner._watch_task,
                    *runner._background_tasks,
                )
                if task is not None
            ]
            assert tasks
            assert all(task.done() for task in tasks)
        finally:
            tasks = [
                task
                for task in (
                    runner._uplink_task,
                    runner._receiver_task,
                    runner._correction_task,
                    runner._watch_task,
                    *runner._background_tasks,
                )
                if task is not None
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_correction_stop_task_is_retained_and_logs_failure(capsys) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        runner = make_runner(
            store.create(), store, root, RunnerRegistry(), FakeElevenLabsWS(), []
        )

        async def fail_finalize(_: int, __: str) -> None:
            raise OSError("write failed")

        async def fail_stop(*, finalize: bool = True) -> None:
            raise RuntimeError("stop failed")

        runner._finalize = fail_finalize
        runner.stop = fail_stop
        worker = asyncio.create_task(runner._correct())
        runner.correction_queue.put_nowait((1, "hello"))
        await runner.correction_queue.join()
        await wait_until(
            lambda: runner._stop_task is not None and runner._stop_task.done()
        )
        runner.correction_queue.put_nowait(None)
        await worker

        assert runner._stop_task is not None
        await asyncio.gather(runner._stop_task, return_exceptions=True)
        assert "停止処理に失敗しました: stop failed" in capsys.readouterr().err


async def test_runner_bounds_audio_queue_and_prunes_partial_tasks(capsys) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        runner = make_runner(
            store.create(), store, root, RunnerRegistry(), FakeElevenLabsWS(), []
        )
        runner._accepting_audio = True
        assert runner.audio_queue.maxsize > 0
        for _ in range(runner.audio_queue.maxsize):
            assert runner.feed_audio(b"\x00\x00")

        assert runner.feed_audio(b"\x00\x00") is False
        assert "音声チャンクを破棄しました" in capsys.readouterr().err

        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        runner._partial_tasks.append(completed)
        runner._on_partial("partial")
        assert len(runner._partial_tasks) == 1
        await asyncio.gather(*runner._partial_tasks)


async def test_light_stop_persists_without_finalizing() -> None:
    def forbidden(*_: object) -> object:
        raise AssertionError("light stop made a network-backed call")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        registry = RunnerRegistry()
        events: list[dict[str, object]] = []
        runner = make_runner(
            session, store, root, registry, FakeElevenLabsWS(), events
        )
        runner.correct_fn = forbidden
        runner.batch_words_fn = forbidden
        runner.card_generator = forbidden
        runner.final_compiler = forbidden
        runner.title_fn = forbidden

        await runner.start()
        runner.appender.write(bytes(4_000))
        runner._has_audio = True
        runner.correction_queue.put_nowait((1, "hello"))
        await runner.stop(finalize=False)

        transcript = (root / session.paths["transcript"]).read_text(
            encoding="utf-8"
        )
        saved = store.get(session.id)
        assert "hello" in transcript
        assert (root / session.paths["audio"]).is_file()
        assert (root / session.paths["cards"]).is_file()
        assert not (root / session.paths["final_transcript"]).exists()
        assert saved is not None
        assert saved.state == "stopped"
        assert saved.finalized is False
        assert saved.resumable
        assert registry.active_session_id is None
        assert not any(
            event["type"] in {"final_transcript", "cards_final"}
            for event in events
        )


async def test_runner_resumes_same_wav_and_transcript() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        registry = RunnerRegistry()

        first = make_runner(
            session, store, root, registry, FakeElevenLabsWS(), []
        )
        await first.start()
        first.feed_audio(bytes(200))
        await first.stop(finalize=False)
        assert store.get(session.id).finalized is False
        with wave.open(str(root / session.paths["audio"]), "rb") as recording:
            first_frames = recording.getnframes()

        resumed_session = store.get(session.id)
        second = make_runner(
            resumed_session, store, root, registry, FakeElevenLabsWS(), []
        )
        await second.start()
        second.feed_audio(bytes(400))
        await second.stop()
        await registry.finalizing[session.id]
        with wave.open(str(root / session.paths["audio"]), "rb") as recording:
            second_frames = recording.getnframes()

        assert first_frames == 100
        assert second_frames == 300
        assert store.get(session.id).finalized is True
        transcript = (root / session.paths["transcript"]).read_text(encoding="utf-8")
        assert "再開:" in transcript


async def test_session_finalizer_reprocesses_stored_session() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        session.has_audio = True
        store.save(session)
        (root / session.paths["audio"]).write_bytes(b"audio")
        transcript_path = root / session.paths["transcript"]
        with transcript_path.open("w", encoding="utf-8") as output:
            transcribe.write_header(output, "browser")
            output.write("Python wheel\n")
        live_store = cards.CardStore.attach(
            root / session.paths["cards"], root / session.paths["cards_html"]
        )
        live_store.append(
            cards.Card(
                "live-card",
                "Python wheel",
                "<p>Live</p>",
                "Python wheel",
                1,
                "done",
                summary="Python wheel",
                keywords=["python", "wheel"],
                provisional=True,
            )
        )
        events: list[dict[str, object]] = []

        def render_final(*_: object) -> list[cards.Card]:
            return [
                cards.Card(
                    "final-card",
                    "Python wheel",
                    "<p>Final</p>",
                    "Python wheel",
                    1,
                    "done",
                    summary="Python wheel",
                    keywords=["python", "wheel"],
                )
            ]

        async def broadcast(event: dict[str, object]) -> None:
            events.append(event)

        finalizer = SessionFinalizer(
            session,
            store,
            root,
            elevenlabs_key="eleven-key",
            openai_key="openai-key",
            batch_words_fn=lambda *_: transcript_segments.BatchTranscript(
                "Python wheel",
                [
                    transcript_segments.TranscriptWord(
                        "word-000001", "Python wheel", 0, 1_000
                    )
                ],
            ),
            final_compiler=fake_final_compiler,
            final_renderer=render_final,
            title_fn=lambda *_: "Final title",
            broadcast=broadcast,
        )

        await finalizer.run()

        saved = store.get(session.id)
        live_cards = json.loads(
            (root / session.paths["cards"]).read_text(encoding="utf-8")
        )
        ordered: list[object] = []
        for event in events:
            if event["type"] == "status":
                ordered.append(event["state"])
            else:
                ordered.append(event["type"])
        assert ordered == [
            "finalizing",
            "final_transcript",
            "cards_final",
            "cards",
            "session",
        ]
        assert events[0]["session_id"] == session.id
        assert saved is not None and saved.finalized
        assert saved.title == "Final title"
        assert live_cards[0]["reconciliation_history"] == [
            {"action": "keep", "final_card_ids": ["final-card"]}
        ]
        for name in (
            "final_transcript",
            "segments",
            "outline",
            "knowledge",
            "final_cards",
            "cards_html",
        ):
            assert (root / session.paths[name]).is_file()


async def test_registry_rejects_second_session() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        first_session = store.create()
        second_session = store.create()
        registry = RunnerRegistry()
        first = make_runner(
            first_session, store, root, registry, FakeElevenLabsWS(), []
        )
        second = make_runner(
            second_session, store, root, registry, FakeElevenLabsWS(), []
        )

        await first.start()
        try:
            await second.start()
        except RunnerBusyError:
            pass
        else:
            raise AssertionError("second session acquired the registry")
        assert registry.active_session_id == first_session.id
        await first.stop()
        await registry.finalizing[first_session.id]


async def test_realtime_failure_is_fatal_and_still_finalizes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        registry = RunnerRegistry()
        websocket = FakeElevenLabsWS()
        events: list[dict[str, object]] = []
        runner = make_runner(session, store, root, registry, websocket, events)

        await runner.start()
        websocket.incoming.put_nowait(
            {"message_type": "auth_error", "error": "bad realtime session"}
        )
        await wait_until(lambda: registry.active_session_id is None)
        await wait_until(lambda: store.get(session.id).finalized)
        await wait_until(lambda: session.id not in registry.finalizing)

        assert any(
            event["type"] == "error" and event["fatal"] is True
            for event in events
        )
        saved = store.get(session.id)
        assert saved.state == "stopped"
        assert saved.finalized
        assert (root / session.paths["audio"]).is_file()


async def test_runner_title_falls_back_to_realtime_transcript() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ensure_dirs(root)
        store = SessionStore(root)
        session = store.create()
        registry = RunnerRegistry()
        events: list[dict[str, object]] = []
        runner = make_runner(
            session,
            store,
            root,
            registry,
            FakeElevenLabsWS(),
            events,
        )
        title_inputs: list[str] = []

        def fail_batch(*_: object) -> str:
            raise RuntimeError("batch failed")

        def title_from(text: str, _: str, __: object) -> str:
            title_inputs.append(text)
            return "Realtime title"

        runner.batch_words_fn = fail_batch
        runner.title_fn = title_from
        await runner.start()
        runner.feed_audio(bytes(4_000))
        await wait_until(
            lambda: any(event["type"] == "corrected" for event in events)
        )
        await runner.stop()
        await registry.finalizing[session.id]

        assert title_inputs == ["補正:hello"]
        assert store.get(session.id).title == "Realtime title"
        assert any(
            event["type"] == "session"
            and event["session"]["title"] == "Realtime title"
            for event in events
        )


def test_card_store_attach_reuses_paths_and_cards() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        json_path = root / "cards/session.json"
        html_path = root / "cards/session.html"
        json_path.parent.mkdir(parents=True)
        json_path.write_text(
            json.dumps(
                [
                    {
                        "id": "existing",
                        "title": "Existing",
                        "html": "<p>Existing</p>",
                        "source_text": "source",
                        "created_at": 1,
                        "status": "done",
                    }
                ]
            ),
            encoding="utf-8",
        )

        attached = cards.CardStore.attach(json_path, html_path)
        pipeline = cards.CardPipeline(
            "key", store=attached, generator=fake_card_generator
        )

        assert attached.json_path == json_path
        assert attached.html_path == html_path
        assert attached.snapshot()[0].title == "Existing"
        assert pipeline.store is attached


def test_websocket_ping_pong() -> None:
    with tempfile.TemporaryDirectory() as directory:
        environment = {
            "TRANSCRIBE_WEBAPP_ROOT": directory,
            "ELEVENLABS_API_KEY": "test-elevenlabs-key",
            "OPENAI_API_KEY": "test-openai-key",
        }
        with patch.dict(os.environ, environment), TestClient(app) as client:
            id = client.post("/api/sessions").json()["id"]
            with client.websocket_connect(f"/api/sessions/{id}/ws") as websocket:
                assert websocket.receive_json()["type"] == "status"
                websocket.send_json({"type": "ping"})
                assert websocket.receive_json() == {"type": "pong"}


def test_websocket_rejects_foreign_origin_and_allows_no_origin() -> None:
    environment = {
        "TRANSCRIBE_WEBAPP_ROOT": tempfile.mkdtemp(),
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "OPENAI_API_KEY": "test-openai-key",
    }
    with patch.dict(os.environ, environment), TestClient(app) as client:
        id = client.post("/api/sessions").json()["id"]
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                f"/api/sessions/{id}/ws",
                headers={"origin": "https://attacker.example"},
            ):
                pass
        assert rejected.value.code == 1008

        with client.websocket_connect(f"/api/sessions/{id}/ws") as websocket:
            assert websocket.receive_json()["type"] == "status"


def test_websocket_start_uses_saved_model() -> None:
    captured: dict[str, object] = {}

    class CapturingRunner:
        def __init__(self, session: Session, *_: object, **kwargs: object) -> None:
            self.session = session
            captured.update(kwargs)

        async def start(self) -> None:
            return None

        async def stop(self, *, finalize: bool = True) -> None:
            captured.setdefault("stops", []).append(finalize)
            return None

        def feed_audio(self, _: bytes) -> bool:
            return True

    environment = {
        "TRANSCRIBE_WEBAPP_ROOT": tempfile.mkdtemp(),
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "OPENAI_API_KEY": "test-openai-key",
        "DEEPSEEK_API_KEY": "test-deepseek-key",
    }
    with patch.dict(os.environ, environment), TestClient(app) as client:
        app.state.runner_factory = CapturingRunner
        try:
            session = client.post("/api/sessions").json()
            client.patch(
                f"/api/sessions/{session['id']}/model",
                json={"provider": "deepseek", "model": "deepseek-v4-flash"},
            )
            with client.websocket_connect(
                f"/api/sessions/{session['id']}/ws"
            ) as websocket:
                assert websocket.receive_json()["type"] == "status"
                websocket.send_json({"type": "start", "sample_rate": 16_000})
                websocket.send_json({"type": "ping"})
                assert websocket.receive_json() == {"type": "pong"}
                assert captured["deepseek_key"] == "test-deepseek-key"
                assert captured["openai_key"] == "test-openai-key"
                started = app.state.store.get(session["id"])
                assert started.ai_provider == "deepseek"
                assert started.ai_model == "deepseek-v4-flash"
            assert captured["stops"] == [False]
        finally:
            del app.state.runner_factory


def test_websocket_explicit_stop_requests_finalization() -> None:
    stops: list[bool] = []

    class CapturingRunner:
        def __init__(self, session: Session, *_: object, **__: object) -> None:
            self.session = session

        async def start(self) -> None:
            return None

        async def stop(self, *, finalize: bool = True) -> None:
            stops.append(finalize)

        def feed_audio(self, _: bytes) -> bool:
            return True

    environment = {
        "TRANSCRIBE_WEBAPP_ROOT": tempfile.mkdtemp(),
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "OPENAI_API_KEY": "test-openai-key",
    }
    with patch.dict(os.environ, environment), TestClient(app) as client:
        app.state.runner_factory = CapturingRunner
        try:
            id = client.post("/api/sessions").json()["id"]
            with client.websocket_connect(f"/api/sessions/{id}/ws") as websocket:
                assert websocket.receive_json()["type"] == "status"
                websocket.send_json({"type": "start", "sample_rate": 16_000})
                websocket.send_json({"type": "stop"})
                websocket.send_json({"type": "ping"})
                assert websocket.receive_json() == {"type": "pong"}
        finally:
            del app.state.runner_factory

    assert stops == [True]


def test_websocket_rejects_start_while_reprocessing() -> None:
    environment = {
        "TRANSCRIBE_WEBAPP_ROOT": tempfile.mkdtemp(),
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "OPENAI_API_KEY": "test-openai-key",
    }
    with patch.dict(os.environ, environment), TestClient(app) as client:
        id = client.post("/api/sessions").json()["id"]
        app.state.finalizing[id] = object()
        try:
            with client.websocket_connect(f"/api/sessions/{id}/ws") as websocket:
                assert websocket.receive_json()["type"] == "status"
                websocket.send_json({"type": "start", "sample_rate": 16_000})
                assert websocket.receive_json() == {
                    "type": "error",
                    "message": "再処理中は録音を開始できません。",
                    "fatal": False,
                }
        finally:
            del app.state.finalizing[id]


def test_reprocess_status_reaches_other_session_websocket() -> None:
    class BroadcastingFinalizer:
        def __init__(
            self,
            session: Session,
            *_: object,
            broadcast,
            **__: object,
        ) -> None:
            self.session = session
            self.broadcast = broadcast

        async def run(self) -> None:
            await self.broadcast(
                {
                    "type": "status",
                    "session_id": self.session.id,
                    "active_session_id": None,
                    "state": "finalizing",
                }
            )

    environment = {
        "TRANSCRIBE_WEBAPP_ROOT": tempfile.mkdtemp(),
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "OPENAI_API_KEY": "test-openai-key",
    }
    with patch.dict(os.environ, environment), TestClient(app) as client:
        target = client.post("/api/sessions").json()
        other = client.post("/api/sessions").json()
        stored = app.state.store.get(target["id"])
        assert stored is not None
        stored.has_audio = True
        app.state.store.save(stored)
        (app.state.root / stored.paths["audio"]).write_bytes(b"audio")
        app.state.finalizer_factory = BroadcastingFinalizer
        try:
            with (
                client.websocket_connect(
                    f"/api/sessions/{target['id']}/ws"
                ) as target_socket,
                client.websocket_connect(
                    f"/api/sessions/{other['id']}/ws"
                ) as other_socket,
            ):
                assert target_socket.receive_json()["type"] == "status"
                assert other_socket.receive_json()["type"] == "status"

                assert client.post(
                    f"/api/sessions/{target['id']}/finalize"
                ).status_code == 202
                target_events = [
                    target_socket.receive_json(),
                    target_socket.receive_json(),
                ]
                other_events = [
                    other_socket.receive_json(),
                    other_socket.receive_json(),
                ]

                assert [event["state"] for event in target_events] == [
                    "finalizing",
                    "idle",
                ]
                assert other_events == target_events
                assert all(
                    event["session_id"] == target["id"]
                    for event in target_events
                )
        finally:
            del app.state.finalizer_factory


def main() -> None:
    asyncio.run(test_runner_streams_corrects_and_finalizes())
    asyncio.run(test_runner_stop_releases_before_background_finalizer_finishes())
    asyncio.run(test_light_stop_persists_without_finalizing())
    asyncio.run(test_runner_resumes_same_wav_and_transcript())
    asyncio.run(test_session_finalizer_reprocesses_stored_session())
    asyncio.run(test_registry_rejects_second_session())
    asyncio.run(test_realtime_failure_is_fatal_and_still_finalizes())
    asyncio.run(test_runner_title_falls_back_to_realtime_transcript())
    test_card_store_attach_reuses_paths_and_cards()
    test_websocket_ping_pong()
    test_websocket_rejects_foreign_origin_and_allows_no_origin()
    test_websocket_start_uses_saved_model()
    test_websocket_explicit_stop_requests_finalization()
    test_websocket_rejects_start_while_reprocessing()
    print("ok")


if __name__ == "__main__":
    main()
