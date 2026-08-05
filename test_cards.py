"""Offline checks for realtime diagram card generation."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import ai
import cards


def test_text_buffer_triggers() -> None:
    buffer = cards.TextBuffer(
        character_threshold=10,
        idle_seconds=5,
        max_seconds=20,
        started_at=0,
    )

    assert buffer.add("abcd", now=1) is None
    assert not buffer.due(now=5.9)
    assert buffer.due(now=6)
    assert buffer.flush(now=6) == "abcd"

    assert buffer.add("12345", now=7) is None
    assert buffer.add("67890", now=8) == "12345\n67890"

    max_buffer = cards.TextBuffer(
        character_threshold=100,
        idle_seconds=100,
        max_seconds=20,
        started_at=8,
    )
    assert max_buffer.add("remaining", now=9) is None
    assert not max_buffer.due(now=27.9)
    assert max_buffer.due(now=28)


def test_sanitize_card_html() -> None:
    unsafe = (
        '<div class="flow unknown" onclick="steal()">'
        '<p>Hello <strong>world</strong></p>'
        '<script><p>stolen</p></script>'
        '<img src="x"><span class="label">safe</span></div>'
    )
    assert cards.sanitize_card_html(unsafe) == (
        '<div class="flow"><p>Hello <strong>world</strong></p>'
        '<span class="label">safe</span></div>'
    )


def test_card_generation_payload() -> None:
    previous = cards.Card(
        id="card-1",
        title="Previous",
        html='<div class="callout"><p>Previous</p></div>',
        source_text="old text",
        created_at=1,
        status="done",
    )
    payload = cards.card_generation_payload(
        "new text", previous, ["First", "Previous"]
    )

    assert payload["model"] == "gpt-5.6-luna"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["store"] is False
    context = json.loads(payload["input"])
    assert context == {
        "new_text": "new text",
        "last_card": {"title": "Previous", "source_text": "old text"},
        "topic_outline": ["First", "Previous"],
    }
    output_format = payload["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["properties"]["decision"]["enum"] == [
        "new_card",
        "update_last",
        "skip",
    ]


def test_generate_card_parses_and_sanitizes_response() -> None:
    response_body = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "decision": "new_card",
                                "title": "Generated",
                                "html": (
                                    '<div class="callout" onclick="bad()">'
                                    "<p>Safe</p><script>bad()</script></div>"
                                ),
                            }
                        ),
                    }
                ],
            }
        ],
        "usage": {"total_tokens": 123},
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(response_body).encode()

    with patch("cards.urllib.request.urlopen", return_value=FakeResponse()):
        result = cards.generate_card("source", "api-key", None, [])

    assert result == {
        "decision": "new_card",
        "title": "Generated",
        "html": '<div class="callout"><p>Safe</p></div>',
        "total_tokens": 123,
    }


def test_deepseek_card_generation_uses_json_output() -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"decision": "skip", "title": "", "html": ""}
                        )
                    }
                }
            ]
        }
    ).encode()
    with patch("cards.urllib.request.urlopen", return_value=response) as urlopen:
        result = cards.generate_card(
            "source",
            "deepseek-key",
            None,
            [],
            ai.DEEPSEEK_FLASH_MODEL,
        )

    body = json.loads(urlopen.call_args.args[0].data)
    assert urlopen.call_args.args[0].full_url == ai.DEEPSEEK_CHAT_COMPLETIONS_URL
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert result["decision"] == "skip"


def test_final_card_generation_payload() -> None:
    payload = cards.final_card_generation_payload("Complete final transcript.")

    assert payload["input"] == "Complete final transcript."
    assert payload["store"] is False
    output_format = payload["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    item = output_format["schema"]["properties"]["cards"]["items"]
    assert item["required"] == ["title", "html", "source_text"]


def test_generate_final_cards_parses_and_sanitizes_all_cards() -> None:
    response_body = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "cards": [
                                    {
                                        "title": "First",
                                        "html": (
                                            '<div class="callout" onclick="bad()">'
                                            "<p>First</p></div>"
                                        ),
                                        "source_text": "Accurate first section.",
                                    },
                                    {
                                        "title": "Second",
                                        "html": '<div class="flow"><p>Second</p></div>',
                                        "source_text": "Accurate second section.",
                                    },
                                ]
                            }
                        ),
                    }
                ],
            }
        ],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(response_body).encode()

    with patch("cards.urllib.request.urlopen", return_value=FakeResponse()):
        generated = cards.generate_final_cards("final", "api-key")

    assert [card.title for card in generated] == ["First", "Second"]
    assert generated[0].html == '<div class="callout"><p>First</p></div>'
    assert generated[1].source_text == "Accurate second section."
    assert all(card.status == "done" for card in generated)


def test_generate_final_cards_rejects_empty_or_invalid_cards() -> None:
    class FakeResponse:
        def __init__(self, result: object) -> None:
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(self.result),
                                }
                            ],
                        }
                    ],
                }
            ).encode()

    for result in (
        {"cards": []},
        {"cards": [{"title": "", "html": "<p>x</p>", "source_text": "x"}]},
        {"cards": [{"title": "x", "html": "", "source_text": "x"}]},
        {"cards": [{"title": "x", "html": "<p>x</p>", "source_text": ""}]},
    ):
        with patch(
            "cards.urllib.request.urlopen", return_value=FakeResponse(result)
        ):
            try:
                cards.generate_final_cards("final", "api-key")
            except cards.CardGenerationError:
                pass
            else:
                raise AssertionError("CardGenerationError was not raised")


def test_card_store_replaces_all_cards_atomically() -> None:
    live = cards.Card("live", "Live", "<p>live</p>", "live", 1, "done")
    final = cards.Card("final", "Final", "<p>final</p>", "final", 2, "done")
    with tempfile.TemporaryDirectory() as directory:
        store = cards.CardStore(Path(directory))
        store.append(live)
        store.replace_all([final])

        assert store.snapshot() == [final]
        saved = json.loads(store.json_path.read_text(encoding="utf-8"))
        assert [card["id"] for card in saved] == ["final"]


async def test_card_pipeline_applies_decisions_in_order() -> None:
    generated = [
        {
            "decision": "new_card",
            "title": "First",
            "html": '<div class="callout"><p>First</p></div>',
            "total_tokens": 10,
        },
        {
            "decision": "update_last",
            "title": "Updated",
            "html": '<div class="callout"><p>Updated</p></div>',
            "total_tokens": 11,
        },
        {
            "decision": "skip",
            "title": "",
            "html": "",
            "total_tokens": 2,
        },
    ]
    contexts: list[tuple[str, cards.Card | None, list[str]]] = []

    def fake_generate(
        source_text: str,
        _: str,
        last_card: cards.Card | None,
        titles: list[str],
        __: object,
    ) -> dict[str, object]:
        contexts.append((source_text, last_card, titles))
        return generated.pop(0)

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            generator=fake_generate,
        )
        pipeline.start()
        pipeline.add("first")
        pipeline.flush()
        pipeline.add("second")
        pipeline.flush()
        pipeline.add("aside")
        pipeline.flush()
        result = await pipeline.close()

        assert len(result) == 1
        assert result[0].title == "Updated"
        assert result[0].source_text == "first\n\nsecond"
        assert contexts[1][1] is not None
        assert contexts[1][1].title == "First"
        assert contexts[2][2] == ["Updated"]
        persisted = json.loads(pipeline.json_path.read_text(encoding="utf-8"))
        assert persisted[0]["id"] == result[0].id
        assert persisted[0]["title"] == "Updated"


async def test_card_pipeline_records_error_after_two_failures() -> None:
    attempts = 0

    def fail_generation(*_: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        raise cards.CardGenerationError("failed")

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            generator=fail_generation,
        )
        pipeline.start()
        pipeline.add("source")
        pipeline.flush()
        result = await pipeline.close()

    assert attempts == 2
    assert len(result) == 1
    assert result[0].status == "error"
    assert result[0].source_text == "source"


def main() -> None:
    test_text_buffer_triggers()
    test_sanitize_card_html()
    test_card_generation_payload()
    test_generate_card_parses_and_sanitizes_response()
    test_final_card_generation_payload()
    test_generate_final_cards_parses_and_sanitizes_all_cards()
    test_generate_final_cards_rejects_empty_or_invalid_cards()
    test_card_store_replaces_all_cards_atomically()
    asyncio.run(test_card_pipeline_applies_decisions_in_order())
    asyncio.run(test_card_pipeline_records_error_after_two_failures())
    print("ok")


if __name__ == "__main__":
    main()
