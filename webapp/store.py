"""Atomic session manifests and legacy-session discovery."""

from __future__ import annotations

import json
import os
import re
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import ai
from webapp.config import cards_dir, recordings_dir, sessions_dir, transcripts_dir


SESSION_ID = re.compile(r"^(\d{8}-\d{6})(?:-\d+)?$")


@dataclass
class Session:
    version: int
    id: str
    title: str | None
    title_source: str | None
    created_at: str
    updated_at: str
    state: str
    duration_seconds: float | None
    has_audio: bool
    finalized: bool
    origin: str
    paths: dict[str, str]
    ai_provider: str
    ai_model: str
    processing_warnings: list[str] = field(default_factory=list)
    _root: Path = field(default=Path("."), repr=False, compare=False)

    @property
    def resumable(self) -> bool:
        return _root_path(self._root, self.paths["audio"]).is_file() or not self.has_audio

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "title": self.title,
            "title_source": self.title_source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state,
            "duration_seconds": self.duration_seconds,
            "has_audio": self.has_audio,
            "finalized": self.finalized,
            "origin": self.origin,
            "paths": self.paths,
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "processing_warnings": self.processing_warnings,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any], root: Path) -> Session:
        paths = dict(value["paths"])
        if "segments" not in paths:
            transcript_stem = Path(paths["transcript"]).stem
            paths["segments"] = f"transcripts/{transcript_stem}-segments.json"
        card_stem = Path(paths["cards"]).stem
        if "outline" not in paths:
            paths["outline"] = f"cards_output/{card_stem}-outline.json"
        if "knowledge" not in paths:
            paths["knowledge"] = f"cards_output/{card_stem}-knowledge.json"
        return cls(
            version=value["version"],
            id=value["id"],
            title=value["title"],
            title_source=value["title_source"],
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            state=value["state"],
            duration_seconds=value["duration_seconds"],
            has_audio=value["has_audio"],
            finalized=value["finalized"],
            origin=value["origin"],
            paths=paths,
            ai_provider=value.get("ai_provider", ai.DEFAULT_AI_MODEL.provider),
            ai_model=value.get("ai_model", ai.DEFAULT_AI_MODEL.model),
            processing_warnings=value.get("processing_warnings", []),
            _root=root,
        )


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.directory = sessions_dir(root)
        self.directory.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[Session]:
        sessions = [self._read(path) for path in self.directory.glob("*.json")]
        return sorted(
            sessions,
            key=lambda session: (
                datetime.fromisoformat(session.created_at),
                session.id,
            ),
            reverse=True,
        )

    def get(self, id: str) -> Session | None:
        path = self._manifest_path(id)
        return self._read(path) if path.exists() else None

    def create(self, now: datetime | None = None) -> Session:
        timestamp = now or datetime.now().astimezone()
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone()
        base = timestamp.strftime("%Y%m%d-%H%M%S")
        id = base
        suffix = 2
        while self._id_in_use(id):
            id = f"{base}-{suffix}"
            suffix += 1
        iso_timestamp = timestamp.isoformat(timespec="seconds")
        session = Session(
            version=2,
            id=id,
            title=None,
            title_source=None,
            created_at=iso_timestamp,
            updated_at=iso_timestamp,
            state="stopped",
            duration_seconds=None,
            has_audio=False,
            finalized=False,
            origin="web",
            paths=_session_paths(id),
            ai_provider=ai.DEFAULT_AI_MODEL.provider,
            ai_model=ai.DEFAULT_AI_MODEL.model,
            processing_warnings=[],
            _root=self.root,
        )
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        session._root = self.root
        output = self._manifest_path(session.id)
        temporary = output.with_suffix(f"{output.suffix}.tmp")
        try:
            temporary.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, output)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def rename(self, id: str, title: str) -> Session:
        session = self.get(id)
        if session is None:
            raise KeyError(id)
        session.title = title
        session.title_source = "user"
        session.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.save(session)
        return session

    def delete(self, id: str) -> None:
        session = self.get(id)
        if session is None:
            return
        for relative in session.paths.values():
            _root_path(self.root, relative).unlink(missing_ok=True)
        self._manifest_path(id).unlink(missing_ok=True)

    def recover_crashed(self) -> int:
        recovered = 0
        for session in self.list():
            if session.state != "recording":
                continue
            session.state = "stopped"
            session.updated_at = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            self.save(session)
            recovered += 1
        return recovered

    def scan_legacy(self) -> list[Session]:
        registered = self.list()
        registered_ids = {session.id for session in registered}
        stems = sorted(
            path.stem
            for path in transcripts_dir(self.root).glob("*.md")
            if not path.stem.endswith("-final")
            and path.stem not in registered_ids
            and _stem_datetime(path.stem) is not None
        )
        if not stems:
            return []

        used_audio = {Path(session.paths["audio"]).stem for session in registered}
        audio_candidates = {
            path.stem
            for path in recordings_dir(self.root).glob("*.wav")
            if _stem_datetime(path.stem) is not None and path.stem not in used_audio
        }
        used_cards = {Path(session.paths["cards"]).stem for session in registered}
        card_candidates = _card_stems(cards_dir(self.root)) - used_cards
        audio_matches = _match_stems(stems, audio_candidates)
        card_matches = _match_stems(stems, card_candidates)

        created: list[Session] = []
        for stem in stems:
            timestamp = _stem_datetime(stem)
            assert timestamp is not None
            audio_stem = audio_matches.get(stem, stem)
            card_stem = card_matches.get(stem, stem)
            paths = _session_paths(stem, audio_stem, card_stem)
            audio_path = _root_path(self.root, paths["audio"])
            iso_timestamp = timestamp.isoformat(timespec="seconds")
            session = Session(
                version=2,
                id=stem,
                title=None,
                title_source=None,
                created_at=iso_timestamp,
                updated_at=iso_timestamp,
                state="stopped",
                duration_seconds=_wav_duration(audio_path),
                has_audio=True,
                finalized=_root_path(
                    self.root, paths["final_transcript"]
                ).is_file(),
                origin="cli",
                paths=paths,
                ai_provider=ai.DEFAULT_AI_MODEL.provider,
                ai_model=ai.DEFAULT_AI_MODEL.model,
                processing_warnings=[],
                _root=self.root,
            )
            self.save(session)
            created.append(session)
        return created

    def _read(self, path: Path) -> Session:
        value = json.loads(path.read_text(encoding="utf-8"))
        return Session.from_dict(value, self.root)

    def _manifest_path(self, id: str) -> Path:
        if SESSION_ID.fullmatch(id) is None:
            raise ValueError(f"invalid session id: {id}")
        return self.directory / f"{id}.json"

    def _id_in_use(self, id: str) -> bool:
        return self._manifest_path(id).exists() or any(
            _root_path(self.root, relative).exists()
            for relative in _session_paths(id).values()
        )


def _session_paths(
    transcript_stem: str,
    audio_stem: str | None = None,
    card_stem: str | None = None,
) -> dict[str, str]:
    audio_stem = audio_stem or transcript_stem
    card_stem = card_stem or transcript_stem
    return {
        "transcript": f"transcripts/{transcript_stem}.md",
        "final_transcript": f"transcripts/{transcript_stem}-final.md",
        "segments": f"transcripts/{transcript_stem}-segments.json",
        "audio": f"recordings/{audio_stem}.wav",
        "cards": f"cards_output/{card_stem}.json",
        "final_cards": f"cards_output/{card_stem}-final.json",
        "outline": f"cards_output/{card_stem}-outline.json",
        "knowledge": f"cards_output/{card_stem}-knowledge.json",
        "cards_html": f"cards_output/{card_stem}.html",
    }


def _root_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    path.relative_to(root.resolve())
    return path


def _stem_datetime(stem: str) -> datetime | None:
    match = SESSION_ID.fullmatch(stem)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").astimezone()


def _card_stems(directory: Path) -> set[str]:
    stems: set[str] = set()
    for path in (*directory.glob("*.json"), *directory.glob("*.html")):
        stem = path.stem.removesuffix("-final")
        if _stem_datetime(stem) is not None:
            stems.add(stem)
    return stems


def _match_stems(stems: list[str], candidates: set[str]) -> dict[str, str]:
    available = set(candidates)
    matches: dict[str, str] = {}
    for stem in stems:
        if stem in available:
            matches[stem] = stem
            available.remove(stem)

    pairs = sorted(
        (
            abs((_stem_datetime(stem) - _stem_datetime(candidate)).total_seconds()),
            stem,
            candidate,
        )
        for stem in stems
        if stem not in matches
        for candidate in available
    )
    used_stems: set[str] = set()
    used_candidates: set[str] = set()
    for distance, stem, candidate in pairs:
        if distance > 5:
            break
        if stem in used_stems or candidate in used_candidates:
            continue
        matches[stem] = candidate
        used_stems.add(stem)
        used_candidates.add(candidate)
    return matches


def _wav_duration(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        with wave.open(str(path), "rb") as recording:
            return recording.getnframes() / recording.getframerate()
    except (EOFError, OSError, ZeroDivisionError, wave.Error):
        return None
