"""FastAPI routes for stored transcription sessions."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from cards import Card
from viewer import viewer_page
from webapp.config import (
    elevenlabs_key,
    ensure_dirs,
    get_root,
    openai_key,
)
from webapp.store import Session, SessionStore
from webapp.runner import RunnerRegistry
from webapp.ws import WebSocketHub, router as websocket_router


STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    root = get_root()
    ensure_dirs(root)
    elevenlabs_api_key = elevenlabs_key(root)
    openai_api_key = openai_key(root)
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
    application.state.curl_path = curl_path
    application.state.registry = RunnerRegistry()
    application.state.hub = WebSocketHub()
    yield


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
    }


@app.patch("/api/sessions/{id}")
def rename_session(
    id: str, body: RenameRequest, request: Request
) -> dict[str, Any]:
    _session(request, id)
    return _summary(_store(request).rename(id, body.title))


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
    page = viewer_page(
        [Card(**card) for card in cards],
        final_cards=[Card(**card) for card in final_cards],
        cards_enabled=True,
        transcript_enabled=False,
    )
    return HTMLResponse(
        page,
        headers={
            "Content-Disposition": f'attachment; filename="{session.id}.html"'
        },
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
