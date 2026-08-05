"""Offline checks for the FastAPI session API."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp.app import app
from webapp.wav import WavAppender


@contextmanager
def api_client(
    deepseek_key: str = "",
) -> Iterator[tuple[TestClient, Path]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = {
            "TRANSCRIBE_WEBAPP_ROOT": str(root),
            "ELEVENLABS_API_KEY": "test-elevenlabs-key",
            "OPENAI_API_KEY": "test-openai-key",
            "DEEPSEEK_API_KEY": deepseek_key,
        }
        with patch.dict(os.environ, environment):
            with TestClient(app) as client:
                yield client, root


def sample_card(title: str = "Card title") -> dict[str, object]:
    return {
        "id": "card-1",
        "title": title,
        "html": '<div class="callout"><p>Body</p></div>',
        "source_text": "Source text",
        "created_at": 1.0,
        "status": "done",
    }


def test_post_get_and_list_roundtrip() -> None:
    with api_client() as (client, _):
        first = client.post("/api/sessions")
        second = client.post("/api/sessions")

        assert first.status_code == 201
        assert second.status_code == 201
        first_session = first.json()
        second_session = second.json()
        assert first_session["resumable"] is True
        assert "paths" not in first_session

        detail = client.get(f"/api/sessions/{first_session['id']}")
        assert detail.status_code == 200
        assert detail.json()["session"] == first_session
        assert detail.json()["transcript"] == {"segments": []}
        assert detail.json()["final_transcript"] is None
        assert detail.json()["cards"] == []
        assert detail.json()["final_cards"] == []

        listed = client.get("/api/sessions")
        assert listed.status_code == 200
        assert [session["id"] for session in listed.json()["sessions"]] == [
            second_session["id"],
            first_session["id"],
        ]


def test_config_lists_only_configured_models() -> None:
    with api_client() as (client, _):
        payload = client.get("/api/config").json()
        assert payload["default_model"] == {
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "label": "GPT-5.6 Luna",
        }
        assert [model["model"] for model in payload["models"]] == [
            "gpt-5.6-luna"
        ]

    with api_client("deepseek-key") as (client, _):
        assert [model["model"] for model in client.get("/api/config").json()["models"]] == [
            "gpt-5.6-luna",
            "deepseek-v4-flash",
        ]


def test_index_includes_accessible_model_selector() -> None:
    with api_client() as (client, _):
        html = client.get("/").text
        assert 'id="model-select"' in html
        assert 'for="model-select"' in html


def test_model_can_be_selected_before_recording_only() -> None:
    with api_client("deepseek-key") as (client, _):
        session = client.post("/api/sessions").json()
        id = session["id"]
        selected = client.patch(
            f"/api/sessions/{id}/model",
            json={"provider": "deepseek", "model": "deepseek-v4-flash"},
        )
        assert selected.status_code == 200
        assert selected.json()["ai_provider"] == "deepseek"
        assert selected.json()["ai_model"] == "deepseek-v4-flash"

        stored = app.state.store.get(id)
        stored.has_audio = True
        app.state.store.save(stored)
        locked = client.patch(
            f"/api/sessions/{id}/model",
            json={"provider": "openai", "model": "gpt-5.6-luna"},
        )
        assert locked.status_code == 409


def test_model_selection_rejects_unknown_or_unconfigured_model() -> None:
    with api_client() as (client, _):
        id = client.post("/api/sessions").json()["id"]
        unknown = client.patch(
            f"/api/sessions/{id}/model",
            json={"provider": "deepseek", "model": "deepseek-v4-flash"},
        )
        assert unknown.status_code == 422

        invalid = client.patch(
            f"/api/sessions/{id}/model",
            json={"provider": "openai", "model": "unknown"},
        )
        assert invalid.status_code == 422

def test_detail_reads_transcripts_and_cards() -> None:
    with api_client() as (client, root):
        session = client.post("/api/sessions").json()
        id = session["id"]
        (root / f"transcripts/{id}.md").write_text(
            "# Realtime\n\nMetadata\n\n## Transcript\n\n"
            "First paragraph.\ncontinued\n\nSecond paragraph.\n",
            encoding="utf-8",
        )
        (root / f"transcripts/{id}-final.md").write_text(
            "# Final\n\n## Transcript\n\nFinal first.\n\nFinal second.\n",
            encoding="utf-8",
        )
        cards = [sample_card()]
        final_cards = [sample_card("Final card")]
        (root / f"cards_output/{id}.json").write_text(
            json.dumps(cards), encoding="utf-8"
        )
        (root / f"cards_output/{id}-final.json").write_text(
            json.dumps(final_cards), encoding="utf-8"
        )

        payload = client.get(f"/api/sessions/{id}").json()

        assert payload["transcript"]["segments"] == [
            "First paragraph.\ncontinued",
            "Second paragraph.",
        ]
        assert payload["final_transcript"] == "Final first.\n\nFinal second."
        assert payload["cards"] == cards
        assert payload["final_cards"] == final_cards


def test_patch_rename_and_reject_blank_title() -> None:
    with api_client() as (client, _):
        id = client.post("/api/sessions").json()["id"]

        renamed = client.patch(
            f"/api/sessions/{id}", json={"title": "  New title  "}
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "New title"
        assert renamed.json()["title_source"] == "user"
        for title in ("", "   "):
            assert (
                client.patch(f"/api/sessions/{id}", json={"title": title}).status_code
                == 422
            )


def test_unknown_session_returns_404() -> None:
    with api_client() as (client, _):
        id = "20260101-000000"
        responses = [
            client.get(f"/api/sessions/{id}"),
            client.patch(f"/api/sessions/{id}", json={"title": "Missing"}),
            client.delete(f"/api/sessions/{id}"),
            client.get(f"/api/sessions/{id}/audio"),
            client.get(f"/api/sessions/{id}/export"),
        ]

        assert all(response.status_code == 404 for response in responses)
        assert all("detail" in response.json() for response in responses)


def test_delete_removes_manifest_and_related_files() -> None:
    with api_client() as (client, root):
        session = client.post("/api/sessions").json()
        stored = app.state.store.get(session["id"])
        for relative in stored.paths.values():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")

        response = client.delete(f"/api/sessions/{session['id']}")

        assert response.status_code == 204
        assert response.content == b""
        assert not (root / f"sessions/{session['id']}.json").exists()
        assert not any((root / relative).exists() for relative in stored.paths.values())


def test_delete_active_session_returns_409() -> None:
    with api_client() as (client, root):
        id = client.post("/api/sessions").json()["id"]
        app.state.registry.start(
            SimpleNamespace(session=app.state.store.get(id))
        )

        response = client.delete(f"/api/sessions/{id}")

        assert response.status_code == 409
        assert (root / f"sessions/{id}.json").exists()
        app.state.registry.release()


def test_status_reports_idle_and_active_session() -> None:
    with api_client() as (client, _):
        page = client.get("/")
        assert page.status_code == 200
        assert "Transcribe" in page.text
        assert client.get("/api/status").json() == {
            "active_session_id": None,
            "state": "idle",
        }

        id = client.post("/api/sessions").json()["id"]
        app.state.registry.start(
            SimpleNamespace(session=app.state.store.get(id))
        )
        assert client.get("/api/status").json() == {
            "active_session_id": id,
            "state": "recording",
        }
        app.state.registry.release()


def test_audio_returns_wav_or_404() -> None:
    with api_client() as (client, root):
        id = client.post("/api/sessions").json()["id"]
        assert client.get(f"/api/sessions/{id}/audio").status_code == 404

        appender = WavAppender(root / f"recordings/{id}.wav")
        appender.open()
        appender.write(b"\x01\x00\x02\x00")
        appender.close()

        response = client.get(f"/api/sessions/{id}/audio")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content.startswith(b"RIFF")


def test_export_returns_self_contained_html() -> None:
    with api_client() as (client, root):
        id = client.post("/api/sessions").json()["id"]
        (root / f"cards_output/{id}.json").write_text(
            json.dumps([sample_card("Export title")]), encoding="utf-8"
        )

        response = client.get(f"/api/sessions/{id}/export")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="{id}.html"'
        )
        assert "Export title" in response.text


def test_lifespan_rejects_missing_api_keys_and_curl() -> None:
    with tempfile.TemporaryDirectory() as directory:
        environment = {
            "TRANSCRIBE_WEBAPP_ROOT": directory,
            "ELEVENLABS_API_KEY": "",
            "OPENAI_API_KEY": "test-openai-key",
        }
        with patch.dict(os.environ, environment):
            try:
                with TestClient(app):
                    pass
            except RuntimeError as error:
                assert str(error).startswith("ELEVENLABS_API_KEY")
            else:
                raise AssertionError("missing API key was accepted")

        environment["ELEVENLABS_API_KEY"] = "test-elevenlabs-key"
        with (
            patch.dict(os.environ, environment),
            patch("webapp.app.shutil.which", return_value=None),
        ):
            try:
                with TestClient(app):
                    pass
            except RuntimeError as error:
                assert str(error) == "curlが見つかりません。"
            else:
                raise AssertionError("missing curl was accepted")


def main() -> None:
    test_post_get_and_list_roundtrip()
    test_detail_reads_transcripts_and_cards()
    test_patch_rename_and_reject_blank_title()
    test_unknown_session_returns_404()
    test_delete_removes_manifest_and_related_files()
    test_delete_active_session_returns_409()
    test_status_reports_idle_and_active_session()
    test_audio_returns_wav_or_404()
    test_export_returns_self_contained_html()
    test_lifespan_rejects_missing_api_keys_and_curl()
    print("ok")


if __name__ == "__main__":
    main()
