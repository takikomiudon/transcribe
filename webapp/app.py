"""FastAPI routes for stored transcription sessions."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

import ai
from cards import Card
from card_models import Topic
from viewer import viewer_page
from webapp.config import (
    deepseek_key,
    elevenlabs_key,
    ensure_dirs,
    get_root,
    openai_key,
)
from webapp.store import Session, SessionStore
from webapp.runner import RunnerRegistry, SessionFinalizer, run_finalizer
from webapp.ws import WebSocketHub, router as websocket_router


STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    root = get_root()
    ensure_dirs(root)
    elevenlabs_api_key = elevenlabs_key(root)
    openai_api_key = openai_key(root)
    deepseek_api_key = deepseek_key(root)
    if not elevenlabs_api_key:
        raise RuntimeError(
            "ELEVENLABS_API_KEYが環境変数または.env.localに設定されていません。"
        )
    if not openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEYが環境変数または.env.localに設定されていません。"
        )
    curl_path = shutil.which("curl")
    if curl_path is None:
        raise RuntimeError("curlが見つかりません。")

    store = SessionStore(root)
    store.recover_crashed()
    store.scan_legacy()
    application.state.root = root
    application.state.store = store
    application.state.elevenlabs_key = elevenlabs_api_key
    application.state.openai_key = openai_api_key
    application.state.deepseek_key = deepseek_api_key
    application.state.curl_path = curl_path
    application.state.registry = RunnerRegistry()
    application.state.hub = WebSocketHub()
    application.state.finalizing = application.state.registry.finalizing
    try:
        yield
    finally:
        tasks = list(application.state.finalizing.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Transcribe", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(websocket_router)


class RenameRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, title: str) -> str:
        title = title.strip()
        if not title:
            raise ValueError("タイトルを入力してください。")
        return title


class ModelSelectionRequest(BaseModel):
    provider: str
    model: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def api_status(request: Request) -> dict[str, str | None]:
    active = request.app.state.registry.active_session_id
    return {
        "active_session_id": active,
        "state": "recording" if active is not None else "idle",
    }


@app.get("/api/config")
def api_config(request: Request) -> dict[str, Any]:
    models = ai.available_models(request.app.state.deepseek_key)
    return {
        "default_model": asdict(ai.DEFAULT_AI_MODEL),
        "models": [asdict(model) for model in models],
        "deepseek_peak_hours_utc": list(ai.DEEPSEEK_PEAK_HOURS_UTC),
    }


@app.get("/api/sessions")
def list_sessions(request: Request) -> dict[str, list[dict[str, Any]]]:
    return {
        "sessions": [_summary(session) for session in _store(request).list()]
    }


@app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
def create_session(request: Request) -> dict[str, Any]:
    return _summary(_store(request).create())


@app.get("/api/sessions/{id}")
def get_session(id: str, request: Request) -> dict[str, Any]:
    session = _session(request, id)
    transcript = _transcript(_path(request, session, "transcript"))
    return {
        "session": _summary(session),
        "transcript": {
            "segments": (
                [
                    paragraph.strip()
                    for paragraph in re.split(r"\n\s*\n", transcript)
                    if paragraph.strip()
                ]
                if transcript is not None
                else []
            )
        },
        "final_transcript": _transcript(
            _path(request, session, "final_transcript")
        ),
        "cards": _json_list(_path(request, session, "cards")),
        "final_cards": _json_list(_path(request, session, "final_cards")),
        "segments": _json_items(_path(request, session, "segments")),
        "outline": _json_items(_path(request, session, "outline")),
        "knowledge": _json_items(_path(request, session, "knowledge")),
    }


@app.patch("/api/sessions/{id}")
def rename_session(
    id: str, body: RenameRequest, request: Request
) -> dict[str, Any]:
    _session(request, id)
    return _summary(_store(request).rename(id, body.title))


@app.patch("/api/sessions/{id}/model")
def select_model(
    id: str, body: ModelSelectionRequest, request: Request
) -> dict[str, Any]:
    session = _session(request, id)
    if session.state == "recording" or session.has_audio:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="録音開始後はモデルを変更できません。",
        )
    try:
        model = ai.model_from_values(body.provider, body.model)
        ai.api_key_for(
            model,
            request.app.state.openai_key,
            request.app.state.deepseek_key,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    session.ai_provider = model.provider
    session.ai_model = model.model
    session.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _store(request).save(session)
    return _summary(session)


@app.delete("/api/sessions/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(id: str, request: Request) -> Response:
    _session(request, id)
    if request.app.state.registry.active_session_id == id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="録音中のセッションは削除できません。",
        )
    _store(request).delete(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/sessions/{id}/finalize",
    status_code=status.HTTP_202_ACCEPTED,
)
async def finalize_session(id: str, request: Request) -> dict[str, str]:
    session = _session(request, id)
    state = request.app.state
    if state.registry.active_session_id == id or session.state == "recording":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="録音中のセッションは再処理できません。",
        )
    if id in state.finalizing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="このセッションは再処理中です。",
        )
    if not _path(request, session, "audio").is_file():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="音声ファイルがないため再処理できません。",
        )

    factory = getattr(state, "finalizer_factory", SessionFinalizer)
    finalizer = factory(
        session,
        state.store,
        state.root,
        elevenlabs_key=state.elevenlabs_key,
        openai_key=state.openai_key,
        deepseek_key=state.deepseek_key,
        curl_path=state.curl_path,
        broadcast=lambda event: state.hub.broadcast(id, event),
    )
    task = asyncio.create_task(_run_finalizer(request.app, id, finalizer))
    state.finalizing[id] = task
    return {"status": "accepted"}


@app.get("/api/sessions/{id}/audio")
def get_audio(id: str, request: Request) -> FileResponse:
    session = _session(request, id)
    path = _path(request, session, "audio")
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="音声ファイルが見つかりません。",
        )
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/sessions/{id}/export")
def export_session(id: str, request: Request) -> HTMLResponse:
    session = _session(request, id)
    cards = _json_list(_path(request, session, "cards"))
    final_cards = _json_list(_path(request, session, "final_cards"))
    topics = _json_items(_path(request, session, "outline"))
    page = viewer_page(
        [Card(**card) for card in cards],
        final_cards=[Card(**card) for card in final_cards],
        topics=[Topic(**topic) for topic in topics],
        cards_enabled=True,
        transcript_enabled=False,
    )
    return HTMLResponse(
        page,
        headers={
            "Content-Disposition": f'attachment; filename="{session.id}.html"'
        },
    )


async def _run_finalizer(application: FastAPI, id: str, finalizer: Any) -> None:
    finalizer_start = asyncio.Event()
    finalizer_start.set()
    await run_finalizer(
        finalizer_start,
        application.state.registry,
        id,
        finalizer,
        lambda event: application.state.hub.broadcast(id, event),
    )


def _store(request: Request) -> SessionStore:
    return request.app.state.store


def _session(request: Request, id: str) -> Session:
    try:
        session = _store(request).get(id)
    except ValueError:
        session = None
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="セッションが見つかりません。",
        )
    return session


def _summary(session: Session) -> dict[str, Any]:
    summary = session.to_dict()
    summary.pop("paths")
    summary["resumable"] = session.resumable
    return summary


def _path(request: Request, session: Session, name: str) -> Path:
    root = request.app.state.root.resolve()
    path = (root / session.paths[name]).resolve()
    path.relative_to(root)
    return path


def _transcript(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for index, line in enumerate(lines):
        if line.strip() == "## Transcript":
            return "\n".join(lines[index + 1 :]).strip()
    return None


def _json_list(path: Path) -> list[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _json_items(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return value["items"]
