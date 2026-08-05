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

from fastapi.testclient import TestClient

import ai
import cards
import transcribe
from webapp.app import app
from webapp.config import ensure_dirs
from webapp.runner import RunnerBusyError, RunnerRegistry, SessionRunner
from webapp.store import Session, SessionStore


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


def fake_correct(
    text: str, _: str, __: list[str], ___: str, ____: object
) -> str:
    return f"補正:{text}"


def fake_card_generator(*_: object) -> dict[str, object]:
    return {"decision": "skip", "title": "", "html": ""}


def fake_final_card_generator(
    _: str, __: str, ___: object
) -> list[cards.Card]:
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
        correct_fn=fake_correct,
        glossary_fn=lambda *_: [],
        card_generator=fake_card_generator,
        final_card_generator=fake_final_card_generator,
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
        assert committed["seq"] == corrected["seq"] == 1
        assert corrected["text"] == "補正:hello"

        transcript_path = root / session.paths["transcript"]
        assert "- Device: browser" in transcript_path.read_text(encoding="utf-8")
        assert "補正:hello" in transcript_path.read_text(encoding="utf-8")
        assert (root / session.paths["final_transcript"]).is_file()
        final_cards = json.loads(
            (root / session.paths["final_cards"]).read_text(encoding="utf-8")
        )
        assert final_cards[0]["title"] == "Final card"
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
        await first.stop()
        with wave.open(str(root / session.paths["audio"]), "rb") as recording:
            first_frames = recording.getnframes()

        resumed_session = store.get(session.id)
        second = make_runner(
            resumed_session, store, root, registry, FakeElevenLabsWS(), []
        )
        await second.start()
        second.feed_audio(bytes(400))
        await second.stop()
        with wave.open(str(root / session.paths["audio"]), "rb") as recording:
            second_frames = recording.getnframes()

        assert first_frames == 100
        assert second_frames == 300
        transcript = (root / session.paths["transcript"]).read_text(encoding="utf-8")
        assert "再開:" in transcript


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
        events: list[dict[str, object]] = []
        runner = make_runner(
            session,
            store,
            root,
            RunnerRegistry(),
            FakeElevenLabsWS(),
            events,
        )
        title_inputs: list[str] = []

        def fail_batch(*_: object) -> str:
            raise RuntimeError("batch failed")

        def title_from(text: str, _: str, __: object) -> str:
            title_inputs.append(text)
            return "Realtime title"

        runner.batch_fn = fail_batch
        runner.title_fn = title_from
        await runner.start()
        runner.feed_audio(bytes(4_000))
        await wait_until(
            lambda: any(event["type"] == "corrected" for event in events)
        )
        await runner.stop()

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


def test_websocket_start_uses_saved_model() -> None:
    captured: dict[str, object] = {}

    class CapturingRunner:
        def __init__(self, session: Session, *_: object, **kwargs: object) -> None:
            self.session = session
            captured.update(kwargs)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
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
        finally:
            del app.state.runner_factory


def main() -> None:
    asyncio.run(test_runner_streams_corrects_and_finalizes())
    asyncio.run(test_runner_resumes_same_wav_and_transcript())
    asyncio.run(test_registry_rejects_second_session())
    asyncio.run(test_realtime_failure_is_fatal_and_still_finalizes())
    asyncio.run(test_runner_title_falls_back_to_realtime_transcript())
    test_card_store_attach_reuses_paths_and_cards()
    test_websocket_ping_pong()
    print("ok")


if __name__ == "__main__":
    main()
