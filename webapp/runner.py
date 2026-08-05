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
import transcribe
import viewer
from webapp.store import Session, SessionStore
from webapp.titles import generate_title
from webapp.wav import WavAppender


class RunnerBusyError(RuntimeError):
    pass


class SessionNotResumableError(RuntimeError):
    pass


class RunnerRegistry:
    def __init__(self) -> None:
        self.active: SessionRunner | Any | None = None

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


class SessionRunner:
    def __init__(
        self,
        session: Session,
        store: SessionStore,
        root: Path,
        *,
        connect: Callable[..., Any] | None = None,
        batch_fn: Callable[..., str] = transcribe.batch_transcribe,
        correct_fn: Callable[..., str] = transcribe.correct_transcript,
        glossary_fn: Callable[..., list[str]] = transcribe.extract_glossary,
        card_generator: Callable[..., dict[str, object]] = cards.generate_card,
        final_card_generator: Callable[..., list[cards.Card]] = (
            cards.generate_final_cards
        ),
        title_fn: Callable[..., str | None] | None = generate_title,
        elevenlabs_key: str = "",
        openai_key: str = "",
        curl_path: str = "curl",
        refresh_interval: float = 30.0,
        registry: RunnerRegistry | None = None,
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
        self.correct_fn = correct_fn
        self.glossary_fn = glossary_fn
        self.card_generator = card_generator
        self.final_card_generator = final_card_generator
        self.title_fn = title_fn
        self.elevenlabs_key = elevenlabs_key
        self.openai_key = openai_key
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
        self._accepting_audio = False
        self._last_cards: list[dict[str, Any]] = []

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
            self.pipeline = cards.CardPipeline(
                self.openai_key,
                store=card_store,
                generator=self.card_generator,
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
        except Exception as error:
            await self._emit_error(error, fatal=True)
            await self._abort_start()
            raise

    def feed_audio(self, pcm: bytes) -> bool:
        if not self._accepting_audio or not pcm:
            return False
        self.audio_queue.put_nowait(pcm)
        return True

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._stopped or not self._started:
                return
            self._stopping = True
            self._accepting_audio = False
            await self._emit_status("stopping")
            expected_commits = self.reducer.commits_received + 1

            self.audio_queue.put_nowait(None)
            if self._uplink_task is not None:
                try:
                    await self._uplink_task
                except Exception as error:
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

            await self._emit_status("finalizing")
            text: str | None = None
            final_cards: list[cards.Card] = []
            try:
                text = await asyncio.to_thread(
                    self.batch_fn,
                    self._path("audio"),
                    self.elevenlabs_key,
                    self.curl_path,
                )
                transcribe.write_batch_transcript(self._path("transcript"), text)
                await self._emit({"type": "final_transcript", "text": text})
            except Exception as error:
                await self._emit_error(error, fatal=False)

            if text is not None:
                try:
                    final_cards = await asyncio.to_thread(
                        self.final_card_generator, text, self.openai_key
                    )
                    cards.save_cards(final_cards, self._path("final_cards"))
                    await self._emit(
                        {"type": "cards_final", "cards": _card_values(final_cards)}
                    )
                except Exception as error:
                    await self._emit_error(error, fatal=False)

            live_cards: list[cards.Card] = []
            try:
                assert self.pipeline is not None
                live_cards = await self.pipeline.close()
                await self._broadcast_cards_if_changed(force=True)
                viewer.export_cards(
                    live_cards,
                    self._path("cards_html"),
                    final_cards=final_cards,
                )
            except Exception as error:
                await self._emit_error(error, fatal=False)

            if self.title_fn is not None:
                self._save_session()
                if self.session.title_source != "user":
                    try:
                        title_text = text or _transcript_body(
                            self._path("transcript")
                        ) or ""
                        title = await asyncio.to_thread(
                            self.title_fn, title_text, self.openai_key
                        )
                        if title and title.strip():
                            self.session.title = title.strip()
                            self.session.title_source = "auto"
                            self.session.updated_at = _now()
                            self._save_session()
                            await self._emit_session()
                    except Exception as error:
                        await self._emit_error(error, fatal=False)

            self.session.state = "stopped"
            self.session.finalized = True
            self.session.updated_at = _now()
            try:
                self._save_session()
                await self._emit_session()
                await self._emit_status("idle")
            finally:
                if self.registry is not None:
                    self.registry.release()
                self._started = False
                self._stopped = True

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
                try:
                    corrected = await asyncio.to_thread(
                        self.correct_fn,
                        text,
                        context,
                        list(self.glossary),
                        self.openai_key,
                    )
                except Exception as error:
                    await self._emit_error(error, fatal=False)
                    corrected = text
                try:
                    await self._finalize(seq, corrected)
                except Exception as error:
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
        except Exception as error:
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
        except Exception as error:
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
        transcribe.extract_glossary = self.glossary_fn
        try:
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
                openai_api_key=self.openai_key,
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
            except Exception as error:
                await self._emit_error(error, fatal=False)
            self.websocket = None
        if hasattr(self._connection, "__aexit__"):
            try:
                await self._connection.__aexit__(None, None, None)
            except Exception as error:
                await self._emit_error(error, fatal=False)

    async def _abort_start(self) -> None:
        self.refresh_stop.set()
        if self.pipeline is not None:
            try:
                await self.pipeline.close()
            except Exception as error:
                await self._emit_error(error, fatal=False)
        if self.websocket is not None:
            await self._close_websocket()
        try:
            self.appender.close()
        except Exception:
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
        await self._emit(
            {
                "type": "status",
                "active_session_id": (
                    None if state == "idle" else self.session.id
                ),
                "state": state,
            }
        )

    def _save_session(self) -> None:
        latest = self.store.get(self.session.id)
        if latest is not None and latest.title_source == "user":
            self.session.title = latest.title
            self.session.title_source = "user"
        self.store.save(self.session)

    def _path(self, name: str) -> Path:
        root = self.root.resolve()
        path = (root / self.session.paths[name]).resolve()
        path.relative_to(root)
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
