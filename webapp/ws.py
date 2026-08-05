"""Browser WebSocket protocol and per-session broadcast hubs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from webapp.runner import (
    RunnerBusyError,
    SessionNotResumableError,
    SessionRunner,
)
from webapp.store import Session


router = APIRouter()


class WebSocketHub:
    def __init__(self) -> None:
        self.connections: dict[str, set[WebSocket]] = {}
        self.recorders: dict[str, WebSocket] = {}
        self.runners: dict[str, SessionRunner] = {}

    async def connect(self, id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(id, set()).add(websocket)

    def recorder(self, id: str, websocket: WebSocket) -> SessionRunner | None:
        if self.recorders.get(id) is websocket:
            return self.runners.get(id)
        return None

    def reserve(self, id: str, websocket: WebSocket, runner: SessionRunner) -> None:
        if id in self.recorders:
            raise RunnerBusyError("このセッションには既に録音接続があります。")
        self.recorders[id] = websocket
        self.runners[id] = runner

    def finish(self, id: str, websocket: WebSocket) -> None:
        if self.recorders.get(id) is websocket:
            self.recorders.pop(id, None)
            self.runners.pop(id, None)

    async def disconnect(self, id: str, websocket: WebSocket) -> None:
        connections = self.connections.get(id)
        if connections is not None:
            connections.discard(websocket)
            if not connections:
                self.connections.pop(id, None)

    async def broadcast(self, id: str, event: dict[str, Any]) -> None:
        disconnected: list[WebSocket] = []
        for websocket in list(self.connections.get(id, ())):
            try:
                await websocket.send_json(event)
            except Exception:
                disconnected.append(websocket)
        for websocket in disconnected:
            await self.disconnect(id, websocket)
        if event.get("type") == "status" and event.get("state") == "idle":
            self.recorders.pop(id, None)
            self.runners.pop(id, None)


@router.websocket("/api/sessions/{id}/ws")
async def session_websocket(websocket: WebSocket, id: str) -> None:
    state = websocket.app.state
    hub: WebSocketHub = state.hub
    await hub.connect(id, websocket)
    active = state.registry.active_session_id
    await websocket.send_json(
        {
            "type": "status",
            "active_session_id": active,
            "state": "recording" if active is not None else "idle",
        }
    )
    if active == id:
        session = _get_session(state.store, id)
        if session is not None:
            await websocket.send_json(
                {"type": "session", "session": _session_value(session)}
            )

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                runner = hub.recorder(id, websocket)
                if runner is not None:
                    runner.feed_audio(message["bytes"])
                continue
            if message.get("text") is None:
                continue
            try:
                command = json.loads(message["text"])
            except json.JSONDecodeError:
                await _send_error(websocket, "JSON形式が不正です。")
                continue
            command_type = command.get("type")
            if command_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif command_type == "start":
                await _start(websocket, id, command)
            elif command_type == "stop":
                runner = hub.recorder(id, websocket)
                if runner is not None:
                    await runner.stop()
                    hub.finish(id, websocket)
            else:
                await _send_error(websocket, "未対応のメッセージです。")
    except WebSocketDisconnect:
        pass
    finally:
        runner = hub.recorder(id, websocket)
        await hub.disconnect(id, websocket)
        if runner is not None:
            await runner.stop()
            hub.finish(id, websocket)


async def _start(websocket: WebSocket, id: str, command: dict[str, Any]) -> None:
    if command.get("sample_rate") != 16_000:
        await _send_error(websocket, "sample_rateは16000を指定してください。")
        return
    state = websocket.app.state
    hub: WebSocketHub = state.hub
    session = _get_session(state.store, id)
    if session is None:
        await _send_error(websocket, "セッションが見つかりません。")
        return
    if not session.resumable:
        await _send_error(websocket, "このセッションは再開できません。")
        return
    if id in hub.recorders:
        await _send_error(websocket, "このセッションには既に録音接続があります。")
        return

    factory: Callable[..., SessionRunner] = getattr(
        state, "runner_factory", SessionRunner
    )
    runner = factory(
        session,
        state.store,
        state.root,
        elevenlabs_key=state.elevenlabs_key,
        openai_key=state.openai_key,
        curl_path=state.curl_path,
        registry=state.registry,
        broadcast=lambda event: hub.broadcast(id, event),
    )
    try:
        hub.reserve(id, websocket, runner)
        await runner.start()
    except (RunnerBusyError, SessionNotResumableError) as error:
        hub.finish(id, websocket)
        await _send_error(websocket, str(error))
    except Exception:
        hub.finish(id, websocket)


async def _send_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json({"type": "error", "message": message, "fatal": False})


def _get_session(store: Any, id: str) -> Session | None:
    try:
        return store.get(id)
    except ValueError:
        return None


def _session_value(session: Session) -> dict[str, Any]:
    value = session.to_dict()
    value.pop("paths")
    value["resumable"] = session.resumable
    return value
