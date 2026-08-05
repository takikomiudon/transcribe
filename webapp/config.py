"""Filesystem and API-key configuration for the web app."""

from __future__ import annotations

import os
from pathlib import Path

import transcribe


def get_root() -> Path:
    configured = os.environ.get("TRANSCRIBE_WEBAPP_ROOT")
    return Path(configured) if configured else Path(__file__).resolve().parent.parent


def transcripts_dir(root: Path) -> Path:
    return root / "transcripts"


def recordings_dir(root: Path) -> Path:
    return root / "recordings"


def cards_dir(root: Path) -> Path:
    return root / "cards_output"


def sessions_dir(root: Path) -> Path:
    return root / "sessions"


def ensure_dirs(root: Path) -> None:
    for path in (
        transcripts_dir(root),
        recordings_dir(root),
        cards_dir(root),
        sessions_dir(root),
    ):
        path.mkdir(parents=True, exist_ok=True)


def elevenlabs_key(root: Path) -> str:
    return transcribe.load_api_key("ELEVENLABS_API_KEY", root / ".env.local")


def openai_key(root: Path) -> str:
    return transcribe.load_api_key("OPENAI_API_KEY", root / ".env.local")
