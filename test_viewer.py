"""Offline checks for the local diagram card viewer."""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

from cards import Card
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
        cards_path = Path(directory) / "cards.json"
        cards_path.write_text(
            json.dumps([asdict(sample_card())]), encoding="utf-8"
        )
        server = viewer.ViewerServer(cards_path, port=0)
        server.start()
        try:
            with urllib.request.urlopen(server.url, timeout=2) as response:
                page = response.read().decode()
                assert response.headers.get_content_type() == "text/html"
            with urllib.request.urlopen(
                f"{server.url}/cards", timeout=2
            ) as response:
                payload = json.loads(response.read())
                assert response.headers.get("Cache-Control") == "no-store"
            assert "Realtime Diagram Cards" in page
            assert payload[0]["title"] == "Overview"

            try:
                urllib.request.urlopen(f"{server.url}/missing", timeout=2)
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError("missing route must return 404")
        finally:
            server.stop()


def test_viewer_page_behavior_and_export() -> None:
    page = viewer.viewer_page()
    assert "setInterval(refresh, 2000)" in page
    assert "source.textContent = card.source_text" in page
    assert "const atBottom" in page
    assert "existing.replaceWith(element)" in page
    for component in ("flow", "compare", "tree", "timeline", "keyvalue", "callout"):
        assert f".{component}" in page

    card = sample_card()
    card.title = "</script><b>unsafe title"
    card.source_text = "</script><img src=x>"
    with tempfile.TemporaryDirectory() as directory:
        output_path = Path(directory) / "export.html"
        assert viewer.export_cards([card], output_path) == output_path
        exported = output_path.read_text(encoding="utf-8")

    assert "const INITIAL_CARDS = [" in exported
    assert "\\u003c/script>" in exported
    assert "if (INITIAL_CARDS === null)" in exported


def main() -> None:
    test_viewer_server_routes()
    test_viewer_page_behavior_and_export()
    print("ok")


if __name__ == "__main__":
    main()
