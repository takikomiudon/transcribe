"""Offline checks for web-app session persistence."""

from __future__ import annotations

import json
import os
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from webapp import config
from webapp.store import SessionStore


JST = timezone(timedelta(hours=9))


def test_config_helpers_and_api_keys() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / ".env.local").write_text(
            "ELEVENLABS_API_KEY=eleven\nOPENAI_API_KEY=openai\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"TRANSCRIBE_WEBAPP_ROOT": str(root)}):
            assert config.get_root() == root
            config.ensure_dirs(root)
            assert config.elevenlabs_key(root) == "eleven"
            assert config.openai_key(root) == "openai"

        assert config.transcripts_dir(root) == root / "transcripts"
        assert config.recordings_dir(root) == root / "recordings"
        assert config.cards_dir(root) == root / "cards_output"
        assert config.sessions_dir(root) == root / "sessions"
        assert all(
            path.is_dir()
            for path in (
                root / "transcripts",
                root / "recordings",
                root / "cards_output",
                root / "sessions",
            )
        )


def test_create_get_list_and_id_collision() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SessionStore(Path(directory))
        now = datetime(2026, 8, 5, 14, 0, tzinfo=JST)
        first = store.create(now)
        second = store.create(now)
        earlier = store.create(now - timedelta(days=1))

        assert first.id == "20260805-140000"
        assert second.id == "20260805-140000-2"
        assert first.created_at == "2026-08-05T14:00:00+09:00"
        assert store.get(first.id) == first
        assert [session.id for session in store.list()] == [
            second.id,
            first.id,
            earlier.id,
        ]
        assert first.paths == {
            "transcript": "transcripts/20260805-140000.md",
            "final_transcript": "transcripts/20260805-140000-final.md",
            "audio": "recordings/20260805-140000.wav",
            "cards": "cards_output/20260805-140000.json",
            "final_cards": "cards_output/20260805-140000-final.json",
            "cards_html": "cards_output/20260805-140000.html",
        }
        assert first.resumable


def test_rename_and_delete_related_files() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = SessionStore(root)
        session = store.create(datetime(2026, 8, 5, 14, 0, tzinfo=JST))
        for relative in session.paths.values():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")

        renamed = store.rename(session.id, "講義タイトル")
        assert renamed.title == "講義タイトル"
        assert renamed.title_source == "user"
        assert store.get(session.id).title == "講義タイトル"

        store.delete(session.id)
        assert store.get(session.id) is None
        assert not any((root / relative).exists() for relative in session.paths.values())


def test_recover_crashed_session() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SessionStore(Path(directory))
        session = store.create(datetime(2026, 8, 5, 14, 0, tzinfo=JST))
        session.state = "recording"
        store.save(session)

        assert store.recover_crashed() == 1
        assert store.get(session.id).state == "stopped"
        assert store.recover_crashed() == 0


def test_scan_legacy_matches_nearby_artifacts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config.ensure_dirs(root)
        (root / "transcripts/20260101-000000.md").write_text("live", encoding="utf-8")
        (root / "transcripts/20260101-000000-final.md").write_text(
            "final", encoding="utf-8"
        )
        (root / "cards_output/20260101-000001.json").write_text(
            "[]", encoding="utf-8"
        )
        (root / "transcripts/20260101-001000.md").write_text("live", encoding="utf-8")
        audio_path = root / "recordings/20260101-001002.wav"
        with wave.open(str(audio_path), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(16_000)
            recording.writeframes(bytes(16_000))

        store = SessionStore(root)
        assert len(store.scan_legacy()) == 2
        without_wav = store.get("20260101-000000")
        with_wav = store.get("20260101-001000")

        assert without_wav.origin == "cli"
        assert without_wav.finalized
        assert without_wav.paths["cards"] == "cards_output/20260101-000001.json"
        assert without_wav.has_audio
        assert not without_wav.resumable
        assert with_wav.paths["audio"] == "recordings/20260101-001002.wav"
        assert with_wav.duration_seconds == 0.5
        assert with_wav.resumable
        assert store.scan_legacy() == []


def test_scan_legacy_parses_collision_suffix() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config.ensure_dirs(root)
        (root / "transcripts/20260101-000000-2.md").write_text(
            "live", encoding="utf-8"
        )

        session = SessionStore(root).scan_legacy()[0]

        assert session.id == "20260101-000000-2"
        assert session.created_at.startswith("2026-01-01T00:00:00")


def test_save_replaces_only_complete_json() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SessionStore(Path(directory))
        session = store.create(datetime(2026, 8, 5, 14, 0, tzinfo=JST))
        real_replace = os.replace
        replacements: list[dict[str, object]] = []

        def checked_replace(source: str | Path, destination: str | Path) -> None:
            replacements.append(json.loads(Path(source).read_text(encoding="utf-8")))
            real_replace(source, destination)

        session.title = "atomic"
        with patch("webapp.store.os.replace", side_effect=checked_replace):
            store.save(session)

        assert replacements[0]["title"] == "atomic"
        assert json.loads(
            (Path(directory) / f"sessions/{session.id}.json").read_text(
                encoding="utf-8"
            )
        )["title"] == "atomic"


def main() -> None:
    test_config_helpers_and_api_keys()
    test_create_get_list_and_id_collision()
    test_rename_and_delete_related_files()
    test_recover_crashed_session()
    test_scan_legacy_matches_nearby_artifacts()
    test_scan_legacy_parses_collision_suffix()
    test_save_replaces_only_complete_json()
    print("ok")


if __name__ == "__main__":
    main()
