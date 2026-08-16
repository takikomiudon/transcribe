"""Offline checks for the local diagram card viewer."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from cards import Card
from card_models import Topic
import viewer


def sample_card() -> Card:
    return Card(
        id="card-1",
        title="Overview",
        html='<div class="flow"><div class="flow-step">Start</div></div>',
        source_text="Original transcript",
        created_at=1,
        status="done",
    )


def test_viewer_server_routes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        transcript_path = Path(directory) / "session-final.md"
        cards_path = Path(directory) / "cards.json"
        final_cards_path = Path(directory) / "cards-final.json"
        cards_path.write_text(
            json.dumps([asdict(sample_card())]), encoding="utf-8"
        )
        server = viewer.ViewerServer(transcript_path, cards_path, port=0)
        server.start()
        try:
            with urllib.request.urlopen(server.url, timeout=2) as response:
                page = response.read().decode()
                assert response.headers.get_content_type() == "text/html"
            with urllib.request.urlopen(
                f"{server.url}/transcript", timeout=2
            ) as response:
                assert response.read() == b""

            transcript_path.write_text("# Final\n\nAccurate text.", encoding="utf-8")
            with urllib.request.urlopen(
                f"{server.url}/transcript", timeout=2
            ) as response:
                transcript = response.read().decode()
                assert response.headers.get_content_type() == "text/plain"
            with urllib.request.urlopen(
                f"{server.url}/cards", timeout=2
            ) as response:
                payload = json.loads(response.read())
                assert response.headers.get("Cache-Control") == "no-store"
            with urllib.request.urlopen(
                f"{server.url}/cards-final", timeout=2
            ) as response:
                assert json.loads(response.read()) == []

            final_card = sample_card()
            final_card.title = "Final Overview"
            final_cards_path.write_text(
                json.dumps([asdict(final_card)]), encoding="utf-8"
            )
            with urllib.request.urlopen(
                f"{server.url}/cards-final", timeout=2
            ) as response:
                final_payload = json.loads(response.read())

            assert "Diagram Cards" in page
            assert transcript == "# Final\n\nAccurate text."
            assert payload[0]["title"] == "Overview"
            assert final_payload[0]["title"] == "Final Overview"
            assert server.final_cards_served.is_set()

            try:
                urllib.request.urlopen(f"{server.url}/missing", timeout=2)
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError("missing route must return 404")
        finally:
            server.stop()


def test_viewer_without_cards() -> None:
    with tempfile.TemporaryDirectory() as directory:
        transcript_path = Path(directory) / "session-final.md"
        server = viewer.ViewerServer(transcript_path, port=0)
        server.start()
        try:
            with urllib.request.urlopen(server.url, timeout=2) as response:
                page = response.read().decode()
            with urllib.request.urlopen(
                f"{server.url}/cards", timeout=2
            ) as response:
                assert json.loads(response.read()) == []
        finally:
            server.stop()

    assert "const CARDS_ENABLED = false;" in page


def test_viewer_page_behavior_and_export() -> None:
    page = viewer.viewer_page()
    assert 'fetch("/transcript", {cache: "no-store"})' in page
    assert "if (text === transcriptVersion) return" in page
    assert "transcript.textContent = text" in page
    assert "setInterval(refreshTranscript, 2000)" in page
    assert "setInterval(refreshCards, 2000)" in page
    assert 'fetch("/cards-final", {cache: "no-store"})' in page
    assert "source.textContent = card.source_text" in page
    assert "const atBottom" in page
    assert "existing.replaceWith(element)" in page
    assert "versions.delete" in page
    assert 'role="tablist"' in page
    assert 'role="tab"' in page
    assert 'role="tabpanel"' in page
    assert 'id="final-cards-tab"' in page
    assert 'id="live-cards-tab"' in page
    assert "ArrowLeft" in page and "ArrowRight" in page
    assert "Home" in page and "End" in page
    assert 'tab.addEventListener("click"' in page
    assert "if (!finalReady && cards.length)" in page
    assert 'selectCardView("final")' in page
    assert "renderOutline" in page
    assert "showTopic" in page
    for component in ("flow", "compare", "tree", "timeline", "keyvalue", "callout"):
        assert f".{component}" in page

    live_card = sample_card()
    live_card.title = "</script><b>unsafe live title"
    live_card.source_text = "</script><img src=x>"
    final_card = sample_card()
    final_card.id = "final-card"
    final_card.title = "Final title"
    final_card.topic_id = "topic-0001"
    topics = [
        Topic("topic-0001", "章", "章の概要", ["seg-0001"], order=0),
        Topic(
            "topic-0002",
            "節",
            "節の概要",
            ["seg-0002"],
            parent_id="topic-0001",
            order=1,
        ),
    ]
    with tempfile.TemporaryDirectory() as directory:
        output_path = Path(directory) / "export.html"
        assert viewer.export_cards(
            [live_card], output_path, final_cards=[final_card], topics=topics
        ) == output_path
        exported = output_path.read_text(encoding="utf-8")

    assert "const INITIAL_CARDS = [" in exported
    assert "const INITIAL_FINAL_CARDS = [" in exported
    assert "const TRANSCRIPT_ENABLED = false;" in exported
    assert "\\u003c/script>" in exported
    assert "Final title" in exported
    assert "章の概要" in exported
    assert 'const INITIAL_TOPICS = [' in exported
    assert "if (CARDS_ENABLED && INITIAL_CARDS === null)" in exported


def test_final_cards_path() -> None:
    assert viewer.final_cards_path(Path("cards/session.json")) == Path(
        "cards/session-final.json"
    )


def test_viewer_page_does_not_rescan_injected_marker_text() -> None:
    live = sample_card()
    live.title = "const INITIAL_FINAL_CARDS = null;"
    final = sample_card()
    final.id = "final-card"

    page = viewer.viewer_page([live], final_cards=[final])

    assert page.count("const INITIAL_FINAL_CARDS = [") == 1
    assert json.dumps(live.title, ensure_ascii=False) in page


def test_export_cards_replaces_only_complete_temporary_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "cards.html"
        real_replace = os.replace
        replacements: list[str] = []

        def checked_replace(source: str | Path, destination: str | Path) -> None:
            replacements.append(Path(source).read_text(encoding="utf-8"))
            real_replace(source, destination)

        with patch("viewer.os.replace", side_effect=checked_replace):
            viewer.export_cards([sample_card()], output)

        assert len(replacements) == 1
        assert replacements[0].startswith("<!doctype html>")
        assert output.read_text(encoding="utf-8") == replacements[0]
        assert not output.with_suffix(".html.tmp").exists()


def main() -> None:
    test_viewer_server_routes()
    test_viewer_without_cards()
    test_viewer_page_behavior_and_export()
    test_final_cards_path()
    print("ok")


if __name__ == "__main__":
    main()
