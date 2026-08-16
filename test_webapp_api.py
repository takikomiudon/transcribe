"""Offline checks for the FastAPI session API."""

from __future__ import annotations

import json
import os
import tempfile
import time
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
        assert detail.json()["segments"] == []
        assert detail.json()["outline"] == []
        assert detail.json()["knowledge"] == []

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
        assert payload["deepseek_peak_hours_utc"] == [[1, 4], [6, 10]]
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
        assert "AIモデル" in html


def test_index_includes_topic_views_and_evidence_navigation() -> None:
    with api_client() as (client, _):
        html = client.get("/").text
        script = client.get("/static/app.js").text

        for label in ("概要", "目次", "カード", "全文"):
            assert label in html
        assert 'role="tablist"' in html
        assert "jumpToEvidence" in script
        assert "audioPlayer.currentTime" in script
        assert "cardVersions.delete" in script


def test_static_app_does_not_reconnect_after_server_shutdown() -> None:
    with api_client() as (client, _):
        script = client.get("/static/app.js").text
        assert "event.code === 1012" in script
        assert "event.code === 1001" in script


def test_static_app_includes_reprocess_control() -> None:
    with api_client() as (client, _):
        script = client.get("/static/app.js").text

        assert "再処理" in script
        assert "/finalize" in script


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
        segments = [
            {
                "id": "seg-0001",
                "raw_text": "Raw evidence.",
                "normalized_text": "Normalized evidence.",
                "start_ms": 1_500,
                "end_ms": 2_500,
                "word_ids": ["word-000001"],
                "speaker_id": None,
                "uncertain_spans": [],
            }
        ]
        outline = [
            {
                "id": "topic-0001",
                "title": "Topic",
                "summary": "Topic summary",
                "segment_ids": ["seg-0001"],
                "parent_id": None,
                "order": 0,
            }
        ]
        knowledge = [
            {
                "id": "unit-0001",
                "topic_id": "topic-0001",
                "kind": "claim",
                "title": "Claim",
                "summary": "Claim summary",
                "items": [],
                "evidence_segment_ids": ["seg-0001"],
                "relations": [],
                "status": "verified",
            }
        ]
        for relative, items in (
            (f"transcripts/{id}-segments.json", segments),
            (f"cards_output/{id}-outline.json", outline),
            (f"cards_output/{id}-knowledge.json", knowledge),
        ):
            (root / relative).write_text(
                json.dumps({"schema_version": 2, "session_id": id, "items": items}),
                encoding="utf-8",
            )

        payload = client.get(f"/api/sessions/{id}").json()

        assert payload["transcript"]["segments"] == [
            "First paragraph.\ncontinued",
            "Second paragraph.",
        ]
        assert payload["final_transcript"] == "Final first.\n\nFinal second."
        assert payload["cards"] == cards
        assert payload["final_cards"] == final_cards
        assert payload["segments"] == segments
        assert payload["outline"] == outline
        assert payload["knowledge"] == knowledge


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
            client.post(f"/api/sessions/{id}/finalize"),
        ]

        assert all(response.status_code == 404 for response in responses)
        assert all("detail" in response.json() for response in responses)


def test_finalize_session_runs_in_background_and_cleans_up() -> None:
    runs: list[str] = []

    class StubFinalizer:
        def __init__(self, session, store, *_: object, **__: object) -> None:
            self.session = session
            self.store = store

        async def run(self) -> None:
            runs.append(self.session.id)
            self.session.finalized = True
            self.store.save(self.session)

    with api_client() as (client, root):
        session = client.post("/api/sessions").json()
        id = session["id"]
        stored = app.state.store.get(id)
        stored.has_audio = True
        stored.finalized = True
        app.state.store.save(stored)
        (root / stored.paths["audio"]).write_bytes(b"audio")
        app.state.finalizer_factory = StubFinalizer
        try:
            response = client.post(f"/api/sessions/{id}/finalize")
            assert response.status_code == 202
            for _ in range(100):
                saved = app.state.store.get(id)
                if saved.finalized and id not in app.state.finalizing:
                    break
                time.sleep(0.01)
            assert saved.finalized
            assert runs == [id]
            assert id not in app.state.finalizing
        finally:
            del app.state.finalizer_factory


def test_finalize_session_rejects_recording_missing_audio_and_duplicate() -> None:
    with api_client() as (client, root):
        recording_id = client.post("/api/sessions").json()["id"]
        recording = app.state.store.get(recording_id)
        recording.has_audio = True
        app.state.store.save(recording)
        (root / recording.paths["audio"]).write_bytes(b"audio")
        app.state.registry.start(SimpleNamespace(session=recording))
        assert (
            client.post(f"/api/sessions/{recording_id}/finalize").status_code
            == 409
        )
        app.state.registry.release()

        missing_id = client.post("/api/sessions").json()["id"]
        missing = app.state.store.get(missing_id)
        missing.has_audio = True
        app.state.store.save(missing)
        assert client.post(f"/api/sessions/{missing_id}/finalize").status_code == 409

        duplicate_id = client.post("/api/sessions").json()["id"]
        duplicate = app.state.store.get(duplicate_id)
        duplicate.has_audio = True
        app.state.store.save(duplicate)
        (root / duplicate.paths["audio"]).write_bytes(b"audio")
        app.state.finalizing[duplicate_id] = object()
        try:
            assert (
                client.post(f"/api/sessions/{duplicate_id}/finalize").status_code
                == 409
            )
        finally:
            app.state.finalizing.pop(duplicate_id)


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
        assert app.state.finalizing is app.state.registry.finalizing
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
        final_card = sample_card("Final export title")
        final_card["topic_id"] = "topic-0001"
        final_card["evidence_segment_ids"] = ["seg-0001"]
        (root / f"cards_output/{id}-final.json").write_text(
            json.dumps([final_card]), encoding="utf-8"
        )
        (root / f"cards_output/{id}-outline.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "session_id": id,
                    "items": [
                        {
                            "id": "topic-0001",
                            "title": "Export topic",
                            "summary": "Summary",
                            "segment_ids": ["seg-0001"],
                            "parent_id": None,
                            "order": 0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        response = client.get(f"/api/sessions/{id}/export")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="{id}.html"'
        )
        assert "Export title" in response.text
        assert "Export topic" in response.text
        assert "showTopic" in response.text


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
    test_finalize_session_runs_in_background_and_cleans_up()
    test_finalize_session_rejects_recording_missing_audio_and_duplicate()
    test_delete_removes_manifest_and_related_files()
    test_delete_active_session_returns_409()
    test_status_reports_idle_and_active_session()
    test_audio_returns_wav_or_404()
    test_export_returns_self_contained_html()
    test_lifespan_rejects_missing_api_keys_and_curl()
    test_static_app_includes_reprocess_control()
    print("ok")


if __name__ == "__main__":
    main()
