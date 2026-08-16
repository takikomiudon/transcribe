"""Browser-audio recording pipeline for one active session."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import cards
import card_compiler
import card_renderer
import transcribe
import transcript_segments
import viewer
import ai
from websockets.exceptions import WebSocketException
from webapp.store import Session, SessionStore
from webapp.titles import generate_title
from webapp.wav import WavAppender


class RunnerBusyError(RuntimeError):
    pass


class SessionNotResumableError(RuntimeError):
    pass


def _ignore_event(_: dict[str, Any]) -> None:
    pass


class _EventEmitter:
    session: Session
    broadcast: Callable[[dict[str, Any]], Awaitable[None] | None]

    async def _emit(self, event: dict[str, Any]) -> None:
        result = self.broadcast(event)
        if inspect.isawaitable(result):
            await result

    async def _emit_error(self, error: Exception, *, fatal: bool) -> None:
        await self._emit(
            {"type": "error", "message": str(error), "fatal": fatal}
        )

    async def _emit_session(self) -> None:
        await self._emit({"type": "session", "session": _session_value(self.session)})

    async def _emit_status(self, state: str) -> None:
        active_session_id: str | None = None
        if self.session.state == "recording":
            active_session_id = self.session.id
        await self._emit(
            {
                "type": "status",
                "active_session_id": active_session_id,
                "state": state,
            }
        )


class SessionFinalizer(_EventEmitter):
    def __init__(
        self,
        session: Session,
        store: SessionStore,
        root: Path,
        *,
        elevenlabs_key: str,
        openai_key: str = "",
        deepseek_key: str = "",
        curl_path: str = "curl",
        broadcast: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        batch_words_fn: Callable[..., transcript_segments.BatchTranscript] = (
            transcribe.batch_transcribe_with_words
        ),
        final_compiler: Callable[..., card_compiler.CompilationResult] = (
            card_compiler.compile_final_knowledge
        ),
        final_renderer: Callable[..., list[cards.Card]] = card_renderer.render_cards,
        title_fn: Callable[..., str | None] | None = generate_title,
    ) -> None:
        self.session = session
        self.store = store
        self.root = root
        self.elevenlabs_key = elevenlabs_key
        self.openai_key = openai_key
        self.deepseek_key = deepseek_key
        self.curl_path = curl_path
        self.broadcast = _ignore_event
        if broadcast is not None:
            self.broadcast = broadcast
        self.batch_words_fn = batch_words_fn
        self.final_compiler = final_compiler
        self.final_renderer = final_renderer
        self.title_fn = title_fn
        self.ai_model = ai.model_from_values(session.ai_provider, session.ai_model)
        self._ai_credentials()

    def _ai_credentials(self) -> tuple[str, ai.AIModel]:
        model = ai.effective_model(self.ai_model)
        key = ai.api_key_for(model, self.openai_key, self.deepseek_key)
        return key, model

    async def run(self) -> None:
        await self._emit_status("finalizing")
        text: str | None = None
        evidence_segments: list[transcript_segments.TranscriptSegment] = []
        final_cards: list[cards.Card] = []
        outline_topics: list[card_compiler.Topic] = []
        try:
            batch_transcript = await asyncio.to_thread(
                self.batch_words_fn,
                _session_path(self.root, self.session, "audio"),
                self.elevenlabs_key,
                self.curl_path,
            )
            text = batch_transcript.text
            transcribe.write_batch_transcript(
                _session_path(self.root, self.session, "transcript"), text
            )
            evidence_segments = transcript_segments.segment_transcript(
                batch_transcript
            )
            transcript_segments.save_segments(
                _session_path(self.root, self.session, "segments"),
                self.session.id,
                evidence_segments,
            )
            await self._emit(
                {
                    "type": "final_transcript",
                    "text": text,
                    "segments": [asdict(segment) for segment in evidence_segments],
                }
            )
        except (
            transcribe.BatchTranscriptionError,
            OSError,
            RuntimeError,
        ) as error:
            await self._emit_error(error, fatal=False)

        if text is not None:
            try:
                key, model = self._ai_credentials()
                compilation = await asyncio.to_thread(
                    self.final_compiler,
                    evidence_segments,
                    key,
                    model,
                )
                self.session.processing_warnings.extend(
                    warning
                    for warning in compilation.warnings
                    if warning not in self.session.processing_warnings
                )
                card_compiler.save_compilation(
                    compilation,
                    self.session.id,
                    _session_path(self.root, self.session, "outline"),
                    _session_path(self.root, self.session, "knowledge"),
                )
                final_cards = self.final_renderer(
                    compilation.topics,
                    compilation.units,
                    evidence_segments,
                )
                outline_topics = compilation.topics
                cards.save_cards(
                    final_cards,
                    _session_path(self.root, self.session, "final_cards"),
                )
                await self._emit(
                    {
                        "type": "cards_final",
                        "cards": _card_values(final_cards),
                        "outline": [asdict(topic) for topic in outline_topics],
                        "knowledge": [
                            asdict(unit) for unit in compilation.units
                        ],
                    }
                )
            except (
                cards.CardGenerationError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                await self._emit_error(error, fatal=False)

        try:
            live_store = cards.CardStore.attach(
                _session_path(self.root, self.session, "cards"),
                _session_path(self.root, self.session, "cards_html"),
            )
            live_cards = live_store.snapshot()
            if final_cards:
                live_cards = cards.reconcile_live_cards(live_cards, final_cards)
                live_store.replace_all(live_cards)
            await self._emit(
                {"type": "cards", "cards": _card_values(live_cards)}
            )
            viewer.export_cards(
                live_cards,
                _session_path(self.root, self.session, "cards_html"),
                final_cards=final_cards,
                topics=outline_topics,
            )
        except (
            cards.CardGenerationError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            await self._emit_error(error, fatal=False)

        if self.title_fn is not None and self.session.title_source != "user":
            try:
                title_text = text
                if title_text is None:
                    title_text = _transcript_body(
                        _session_path(self.root, self.session, "transcript")
                    )
                if title_text is None:
                    title_text = ""
                key, model = self._ai_credentials()
                title = await asyncio.to_thread(
                    self.title_fn,
                    title_text,
                    key,
                    model,
                )
                if title is not None and title.strip():
                    self.session.title = title.strip()
                    self.session.title_source = "auto"
            except (
                transcribe.TranslationError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                await self._emit_error(error, fatal=False)

        self.session.finalized = True
        self.session.updated_at = _now()
        _save_session_to_store(self.store, self.session)
        await self._emit_session()


class RunnerRegistry:
    def __init__(self) -> None:
        self.active: SessionRunner | Any | None = None
        self.finalizing: dict[str, asyncio.Task[None]] = {}

    def start(self, runner: SessionRunner | Any) -> None:
        if self.active is not None and self.active is not runner:
            raise RunnerBusyError("別のセッションを録音中です。")
        self.active = runner
        if hasattr(runner, "registry"):
            runner.registry = self

    def release(self) -> None:
        self.active = None

    @property
    def active_session_id(self) -> str | None:
        return self.active.session.id if self.active is not None else None


async def run_finalizer(
    start: asyncio.Event,
    registry: RunnerRegistry,
    id: str,
    finalizer: Any,
    broadcast: Callable[[dict[str, Any]], Awaitable[None] | None],
) -> None:
    try:
        await start.wait()
        await finalizer.run()
    finally:
        del registry.finalizing[id]
        active = registry.active_session_id
        state = "idle"
        if active is not None:
            state = "recording"
        result = broadcast(
            {
                "type": "status",
                "active_session_id": active,
                "state": state,
            }
        )
        if inspect.isawaitable(result):
            await result


class SessionRunner(_EventEmitter):
    def __init__(
        self,
        session: Session,
        store: SessionStore,
        root: Path,
        *,
        connect: Callable[..., Any] | None = None,
        batch_fn: Callable[..., str] = transcribe.batch_transcribe,
        batch_words_fn: Callable[..., transcript_segments.BatchTranscript] = (
            transcribe.batch_transcribe_with_words
        ),
        correct_fn: Callable[..., str] = transcribe.correct_transcript,
        glossary_fn: Callable[..., list[str]] = transcribe.extract_glossary,
        card_generator: Callable[..., dict[str, object]] = cards.generate_card,
        final_compiler: Callable[..., card_compiler.CompilationResult] = (
            card_compiler.compile_final_knowledge
        ),
        final_renderer: Callable[..., list[cards.Card]] = card_renderer.render_cards,
        title_fn: Callable[..., str | None] | None = generate_title,
        elevenlabs_key: str = "",
        openai_key: str = "",
        deepseek_key: str = "",
        curl_path: str = "curl",
        refresh_interval: float = 30.0,
        registry: RunnerRegistry,
        broadcast: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        if connect is None:
            from websockets.asyncio.client import connect as websocket_connect

            connect = websocket_connect
        self.session = session
        self.store = store
        self.root = root
        self.connect = connect
        self.batch_fn = batch_fn
        self.batch_words_fn = batch_words_fn
        self.correct_fn = correct_fn
        self.glossary_fn = glossary_fn
        self.card_generator = card_generator
        self.final_compiler = final_compiler
        self.final_renderer = final_renderer
        self.title_fn = title_fn
        self.elevenlabs_key = elevenlabs_key
        self.ai_model = ai.model_from_values(session.ai_provider, session.ai_model)
        self.openai_key = openai_key
        self.deepseek_key = deepseek_key
        self._ai_credentials()
        self.curl_path = curl_path
        self.refresh_interval = refresh_interval
        self.registry = registry
        self.broadcast = broadcast or (lambda _: None)

        self.appender = WavAppender(self._path("audio"))
        self.reducer = transcribe.TranscriptReducer(lambda _: None)
        self.progress_changed = asyncio.Event()
        self.glossary: list[str] = []
        self.audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.correction_queue: asyncio.Queue[tuple[int, str] | None] = (
            asyncio.Queue()
        )
        self.refresh_stop = asyncio.Event()
        self.pipeline: cards.CardPipeline | None = None
        self.websocket: Any = None
        self._connection: Any = None
        self._transcript_file: TextIO | None = None
        self._uplink_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._correction_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._background_tasks: list[asyncio.Task[None]] = []
        self._partial_tasks: list[asyncio.Task[None]] = []
        self._stop_lock = asyncio.Lock()
        self._seq = 0
        self._has_audio = False
        self._started = False
        self._stopping = False
        self._stopped = False
        self._skip_corrections = False
        self._accepting_audio = False
        self._last_cards: list[dict[str, Any]] = []

    def _ai_credentials(self) -> tuple[str, ai.AIModel]:
        model = ai.effective_model(self.ai_model)
        key = ai.api_key_for(model, self.openai_key, self.deepseek_key)
        return key, model

    def _generate_card(
        self,
        source_text: str,
        _: str,
        candidates: list[dict[str, object]],
        glossary: list[str],
        __: ai.AIModel,
    ) -> dict[str, object]:
        key, model = self._ai_credentials()
        return self.card_generator(source_text, key, candidates, glossary, model)

    def _extract_glossary(
        self, text: str, _: str, __: ai.AIModel
    ) -> list[str]:
        key, model = self._ai_credentials()
        return self.glossary_fn(text, key, model)

    async def start(self) -> None:
        if self._started:
            return
        if self._stopped:
            raise RuntimeError("このrunnerは停止済みです。")
        if not self.session.resumable:
            raise SessionNotResumableError("このセッションは再開できません。")
        if self.registry is not None:
            self.registry.start(self)

        try:
            self.session.state = "recording"
            self.session.updated_at = _now()
            self._save_session()
            await self._emit_status("recording")

            self.appender.open()
            self.session.has_audio = True
            self.session.updated_at = _now()
            self._save_session()

            transcript_path = self._path("transcript")
            resumed = transcript_path.exists()
            self._transcript_file = transcript_path.open("a", encoding="utf-8")
            if resumed:
                self._transcript_file.write(f"\n---\n再開: {_now()}\n\n")
                self._transcript_file.flush()
            else:
                transcribe.write_header(self._transcript_file, "browser")  # type: ignore[arg-type]

            card_store = cards.CardStore.attach(
                self._path("cards"), self._path("cards_html")
            )
            key, model = self._ai_credentials()
            self.pipeline = cards.CardPipeline(
                key,
                store=card_store,
                generator=self._generate_card,
                model=model,
            )
            self.pipeline.start()
            self._last_cards = _card_values(card_store.snapshot())

            connection = self.connect(
                transcribe.realtime_url(),
                additional_headers={"xi-api-key": self.elevenlabs_key},
                max_size=None,
            )
            self._connection = connection
            if inspect.isawaitable(connection):
                self.websocket = await connection
            elif hasattr(connection, "__aenter__"):
                self.websocket = await connection.__aenter__()
            else:
                self.websocket = connection

            self._started = True
            self._accepting_audio = True
            self._uplink_task = asyncio.create_task(self._uplink())
            self._receiver_task = asyncio.create_task(self._receive())
            self._correction_task = asyncio.create_task(self._correct())
            self._watch_task = asyncio.create_task(self._watch_realtime())
            self._background_tasks = [
                asyncio.create_task(self._refresh_final()),
                asyncio.create_task(self._watch_final_transcript()),
                asyncio.create_task(self._save_metadata()),
                asyncio.create_task(self._watch_cards()),
            ]
            await self._emit_session()
        except RunnerBusyError:
            raise
        except (
            WebSocketException,
            OSError,
            RuntimeError,
            ValueError,
            transcribe.TranscriptionError,
            cards.CardGenerationError,
        ) as error:
            await self._emit_error(error, fatal=True)
            await self._abort_start()
            raise

    def feed_audio(self, pcm: bytes) -> bool:
        if not self._accepting_audio or not pcm:
            return False
        self.audio_queue.put_nowait(pcm)
        return True

    async def stop(self, *, finalize: bool = True) -> None:
        async with self._stop_lock:
            if self._stopped or not self._started:
                return
            self._stopping = True
            if not finalize:
                self._skip_corrections = True
            self._accepting_audio = False
            await self._emit_status("stopping")
            expected_commits = self.reducer.commits_received + 1

            self.audio_queue.put_nowait(None)
            if self._uplink_task is not None:
                try:
                    await self._uplink_task
                except (
                    WebSocketException,
                    OSError,
                    RuntimeError,
                    ValueError,
                    transcribe.TranscriptionError,
                ) as error:
                    await self._emit_error(error, fatal=True)

            if (
                self._has_audio
                and self._receiver_task is not None
                and not self._receiver_task.done()
            ):
                try:
                    await asyncio.wait_for(
                        transcribe.wait_for_committed_transcripts(
                            self.reducer,
                            self.progress_changed,
                            expected_commits,
                        ),
                        timeout=transcribe.FINAL_WAIT_SECONDS,
                    )
                except TimeoutError:
                    pass

            if self._receiver_task is not None and not self._receiver_task.done():
                self._receiver_task.cancel()
            if self._receiver_task is not None:
                await asyncio.gather(self._receiver_task, return_exceptions=True)
            await self._close_websocket()

            await self.correction_queue.join()
            self.correction_queue.put_nowait(None)
            if self._correction_task is not None:
                await self._correction_task
            if self._partial_tasks:
                await asyncio.gather(*self._partial_tasks, return_exceptions=True)

            self.refresh_stop.set()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self.appender.close()
            if self._transcript_file is not None:
                self._transcript_file.close()
                self._transcript_file = None
            self.session.duration_seconds = self.appender.tell() / transcribe.SAMPLE_RATE
            self.session.updated_at = _now()
            self._save_session()

            try:
                assert self.pipeline is not None
                await self.pipeline.close(generate_pending=finalize)
                self.session.processing_warnings.extend(
                    warning
                    for warning in self.pipeline.errors
                    if warning not in self.session.processing_warnings
                )
                await self._broadcast_cards_if_changed(force=True)
            except (
                cards.CardGenerationError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                await self._emit_error(error, fatal=False)

            self.session.state = "stopped"
            self.session.updated_at = _now()
            try:
                self._save_session()
            finally:
                self.registry.release()
                self._started = False
                self._stopped = True

            finalizer_start = asyncio.Event()
            if finalize:
                finalizer = SessionFinalizer(
                    self.session,
                    self.store,
                    self.root,
                    elevenlabs_key=self.elevenlabs_key,
                    openai_key=self.openai_key,
                    deepseek_key=self.deepseek_key,
                    curl_path=self.curl_path,
                    broadcast=self.broadcast,
                    batch_words_fn=self.batch_words_fn,
                    final_compiler=self.final_compiler,
                    final_renderer=self.final_renderer,
                    title_fn=self.title_fn,
                )
                task = asyncio.create_task(
                    run_finalizer(
                        finalizer_start,
                        self.registry,
                        self.session.id,
                        finalizer,
                        self.broadcast,
                    )
                )
                self.registry.finalizing[self.session.id] = task

            try:
                await self._emit_session()
                await self._emit_status("idle")
            finally:
                finalizer_start.set()

    async def _uplink(self) -> None:
        buffer = bytearray()
        while True:
            chunk = await self.audio_queue.get()
            try:
                if chunk is None:
                    if self._has_audio:
                        await self._send_audio(bytes(buffer), commit=True)
                    return
                self.appender.write(chunk)
                self._has_audio = True
                buffer.extend(chunk)
                while len(buffer) >= transcribe.UPLOAD_CHUNK_BYTES:
                    await self._send_audio(bytes(buffer[: transcribe.UPLOAD_CHUNK_BYTES]))
                    del buffer[: transcribe.UPLOAD_CHUNK_BYTES]
            finally:
                self.audio_queue.task_done()

    async def _send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        await self.websocket.send(
            json.dumps(
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(audio).decode("ascii"),
                    "commit": commit,
                    "sample_rate": transcribe.SAMPLE_RATE,
                }
            )
        )

    async def _receive(self) -> None:
        await transcribe.receive_events(
            self.websocket,
            self.reducer,
            self.progress_changed,
            self._enqueue_committed,
            on_partial=self._on_partial,
        )

    def _on_partial(self, text: str) -> None:
        task = asyncio.create_task(self._emit({"type": "partial", "text": text}))
        self._partial_tasks.append(task)

    async def _enqueue_committed(self, text: str) -> None:
        if self._partial_tasks:
            await asyncio.gather(*self._partial_tasks, return_exceptions=True)
            self._partial_tasks.clear()
        self._seq += 1
        await self._emit({"type": "committed", "seq": self._seq, "text": text})
        self.correction_queue.put_nowait((self._seq, text))

    async def _correct(self) -> None:
        context = ""
        while True:
            item = await self.correction_queue.get()
            try:
                if item is None:
                    return
                seq, text = item
                corrected = text
                if not self._skip_corrections:
                    try:
                        key, model = self._ai_credentials()
                        corrected = await asyncio.to_thread(
                            self.correct_fn,
                            text,
                            context,
                            list(self.glossary),
                            key,
                            model,
                        )
                    except (
                        transcribe.TranslationError,
                        OSError,
                        RuntimeError,
                        ValueError,
                    ) as error:
                        await self._emit_error(error, fatal=False)
                try:
                    await self._finalize(seq, corrected)
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    WebSocketException,
                    transcribe.TranscriptionError,
                ) as error:
                    await self._emit_error(error, fatal=True)
                    asyncio.create_task(self.stop())
                else:
                    context = "\n".join(filter(None, (context, corrected)))[-500:]
            finally:
                self.correction_queue.task_done()

    async def _finalize(self, seq: int, corrected_text: str) -> None:
        await self._emit(
            {"type": "corrected", "seq": seq, "text": corrected_text}
        )
        assert self._transcript_file is not None
        self._transcript_file.write(corrected_text + "\n\n")
        self._transcript_file.flush()
        assert self.pipeline is not None
        try:
            self.pipeline.add(corrected_text)
            await self._broadcast_cards_if_changed()
        except (OSError, RuntimeError, WebSocketException) as error:
            await self._emit_error(error, fatal=False)
        self._save_session()
        if self.session.title is None:
            self.session.title = corrected_text.strip()[:30]
            self.session.title_source = "auto"
            self.session.updated_at = _now()
            self._save_session()
            await self._emit_session()

    async def _watch_realtime(self) -> None:
        assert self._uplink_task is not None and self._receiver_task is not None
        done, _ = await asyncio.wait(
            (self._uplink_task, self._receiver_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._stopping:
            return
        task = next(iter(done))
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except (
            OSError,
            RuntimeError,
            ValueError,
            WebSocketException,
            transcribe.TranscriptionError,
        ) as error:
            failure = error
        else:
            failure = transcribe.TranscriptionError(
                "ElevenLabs Realtime APIとの接続が終了しました。"
            )
        await self._emit_error(failure, fatal=True)
        await self.stop()

    async def _refresh_final(self) -> None:
        original_batch = transcribe.batch_transcribe
        original_glossary = transcribe.extract_glossary
        transcribe.batch_transcribe = self.batch_fn
        transcribe.extract_glossary = self._extract_glossary
        try:
            key, model = self._ai_credentials()
            await transcribe.periodically_refresh_final(
                self._path("transcript"),
                self._path("audio"),
                self.appender,
                self.appender,
                self.elevenlabs_key,
                self.curl_path,
                self.refresh_stop,
                interval_seconds=self.refresh_interval,
                glossary=self.glossary,
                openai_api_key=key,
                model=model,
            )
        finally:
            transcribe.batch_transcribe = original_batch
            transcribe.extract_glossary = original_glossary

    async def _watch_final_transcript(self) -> None:
        path = self._path("final_transcript")
        modified = path.stat().st_mtime_ns if path.exists() else None
        while not self.refresh_stop.is_set():
            try:
                await asyncio.wait_for(self.refresh_stop.wait(), timeout=2)
                return
            except TimeoutError:
                pass
            current = path.stat().st_mtime_ns if path.exists() else None
            if current is None or current == modified:
                continue
            modified = current
            text = _transcript_body(path)
            if text is not None:
                await self._emit({"type": "final_transcript", "text": text})

    async def _save_metadata(self) -> None:
        while not self.refresh_stop.is_set():
            try:
                await asyncio.wait_for(self.refresh_stop.wait(), timeout=10)
                return
            except TimeoutError:
                pass
            self.session.duration_seconds = self.appender.tell() / transcribe.SAMPLE_RATE
            self.session.updated_at = _now()
            self._save_session()
            await self._emit_session()

    async def _watch_cards(self) -> None:
        while not self.refresh_stop.is_set():
            try:
                await asyncio.wait_for(self.refresh_stop.wait(), timeout=1)
                return
            except TimeoutError:
                pass
            await self._broadcast_cards_if_changed()

    async def _broadcast_cards_if_changed(self, *, force: bool = False) -> None:
        if self.pipeline is None:
            return
        current = _card_values(self.pipeline.store.snapshot())
        if force or current != self._last_cards:
            self._last_cards = current
            await self._emit({"type": "cards", "cards": current})

    async def _close_websocket(self) -> None:
        if self.websocket is not None:
            try:
                await self.websocket.close()
            except (OSError, RuntimeError, WebSocketException) as error:
                await self._emit_error(error, fatal=False)
            self.websocket = None
        if hasattr(self._connection, "__aexit__"):
            try:
                await self._connection.__aexit__(None, None, None)
            except (OSError, RuntimeError, WebSocketException) as error:
                await self._emit_error(error, fatal=False)

    async def _abort_start(self) -> None:
        self.refresh_stop.set()
        if self.pipeline is not None:
            try:
                await self.pipeline.close()
            except (
                cards.CardGenerationError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                await self._emit_error(error, fatal=False)
        if self.websocket is not None:
            await self._close_websocket()
        try:
            self.appender.close()
        except (OSError, ValueError):
            pass
        if self._transcript_file is not None:
            self._transcript_file.close()
            self._transcript_file = None
        self.session.state = "stopped"
        self.session.updated_at = _now()
        self._save_session()
        await self._emit_session()
        await self._emit_status("idle")
        if self.registry is not None:
            self.registry.release()
        self._stopped = True

    def _save_session(self) -> None:
        _save_session_to_store(self.store, self.session)

    def _path(self, name: str) -> Path:
        return _session_path(self.root, self.session, name)


def _save_session_to_store(store: SessionStore, session: Session) -> None:
    latest = store.get(session.id)
    if latest is not None and latest.title_source == "user":
        session.title = latest.title
        session.title_source = "user"
    store.save(session)


def _session_path(root: Path, session: Session, name: str) -> Path:
    resolved_root = root.resolve()
    path = (resolved_root / session.paths[name]).resolve()
    path.relative_to(resolved_root)
    return path


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _card_values(values: list[cards.Card]) -> list[dict[str, Any]]:
    return [asdict(card) for card in values]


def _session_value(session: Session) -> dict[str, Any]:
    value = session.to_dict()
    value.pop("paths")
    value["resumable"] = session.resumable
    return value


def _transcript_body(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for index, line in enumerate(lines):
        if line.strip() == "## Transcript":
            return "\n".join(lines[index + 1 :]).strip()
    return None
