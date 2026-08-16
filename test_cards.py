"""Offline checks for realtime diagram card generation."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import ai
import cards
import pytest


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
    assert cards.sanitize_card_html(
        "<svg><img></svg><p>after svg</p>"
    ) == "<p>after svg</p>"
    assert cards.sanitize_card_html(
        "<svg></p><p>leaked?</p></svg>rest"
    ) == "rest"


def test_card_generation_payload() -> None:
    candidate = cards.Card(
        id="card-1",
        title="Previous",
        html='<div class="callout"><p>Previous</p></div>',
        source_text="old text",
        created_at=1,
        status="done",
        summary="Previous summary",
        keywords=["previous"],
    )
    candidates = cards.select_card_candidates("previous", [candidate])
    payload = cards.card_generation_payload(
        "new text", candidates, ["First", "Previous"]
    )

    assert payload["model"] == "gpt-5.6-luna"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["store"] is False
    context = json.loads(payload["input"])
    assert context == {
        "new_text": "new text",
        "candidates": [
            {
                "id": "card-1",
                "title": "Previous",
                "summary": "Previous summary",
                "keywords": ["previous"],
            }
        ],
        "topic_outline": ["First", "Previous"],
    }
    assert "old text" not in payload["input"]
    assert "<div" not in payload["input"]
    output_format = payload["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["properties"]["decision"]["enum"] == [
        "create",
        "update",
        "merge",
        "link",
        "skip",
    ]
    assert payload["max_output_tokens"] == 4096
    assert json.dumps(cards.CARD_JSON_EXAMPLE, ensure_ascii=False) in payload[
        "instructions"
    ]
    assert json.dumps(cards.CARD_SKIP_JSON_EXAMPLE, ensure_ascii=False) in payload[
        "instructions"
    ]
    assert set(cards.CARD_JSON_EXAMPLE) == set(
        output_format["schema"]["properties"]
    )
    assert set(cards.CARD_SKIP_JSON_EXAMPLE) == set(
        output_format["schema"]["properties"]
    )


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
                                "decision": "create",
                                "card_ids": [],
                                "title": "Generated",
                                "html": (
                                    '<div class="callout" onclick="bad()">'
                                    "<p>Safe</p><script>bad()</script></div>"
                                ),
                                "summary": "Generated summary",
                                "keywords": ["generated"],
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
        result = cards.generate_card("source", "api-key", [], [])

    assert result == {
        "decision": "create",
        "card_ids": [],
        "title": "Generated",
        "html": '<div class="callout"><p>Safe</p></div>',
        "summary": "Generated summary",
        "keywords": ["generated"],
        "total_tokens": 123,
    }


def test_generate_card_strips_code_fence() -> None:
    response = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '```json\n{"decision":"create","card_ids":[],"title":"Generated","html":"<p>Safe</p>","summary":"Summary","keywords":["safe"]}\n```',
                    }
                ],
            }
        ],
    }
    with patch("cards._request_card_generation", return_value=response):
        result = cards.generate_card("source", "api-key", [], [])

    assert result["title"] == "Generated"
    assert result["html"] == "<p>Safe</p>"


def test_generate_card_rejects_empty_title_html() -> None:
    response = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"decision":"create","card_ids":[],"title":"","html":"","summary":"","keywords":[]}',
                    }
                ],
            }
        ],
    }
    with patch("cards._request_card_generation", return_value=response):
        with pytest.raises(cards.CardGenerationError, match="タイトルまたはHTMLが空"):
            cards.generate_card("source", "api-key", [], [])


def test_deepseek_card_generation_uses_json_output() -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "skip",
                                "card_ids": [],
                                "title": "",
                                "html": "",
                                "summary": "",
                                "keywords": [],
                            }
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
            [],
            [],
            ai.DEEPSEEK_FLASH_MODEL,
        )

    body = json.loads(urlopen.call_args.args[0].data)
    assert urlopen.call_args.args[0].full_url == ai.DEEPSEEK_CHAT_COMPLETIONS_URL
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert "json" in body["messages"][0]["content"].lower()
    assert "decision" in body["messages"][0]["content"]
    assert result["decision"] == "skip"


def test_deepseek_truncated_card_response_reports_length() -> None:
    response = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": None},
            }
        ]
    }
    with patch("cards._request_card_generation", return_value=response):
        with pytest.raises(cards.CardGenerationError, match="max_tokens"):
            cards.generate_card(
                "source",
                "deepseek-key",
                [],
                [],
                ai.DEEPSEEK_FLASH_MODEL,
            )


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
            "decision": "create",
            "card_ids": [],
            "title": "First",
            "html": '<div class="callout"><p>First</p></div>',
            "summary": "First",
            "keywords": ["first"],
            "total_tokens": 10,
        },
        {
            "decision": "update",
            "title": "Updated",
            "html": '<div class="callout"><p>Updated</p></div>',
            "summary": "Updated",
            "keywords": ["updated"],
            "total_tokens": 11,
        },
        {
            "decision": "skip",
            "title": "",
            "html": "",
            "total_tokens": 2,
        },
    ]
    contexts: list[tuple[str, list[dict[str, object]], list[str]]] = []

    def fake_generate(
        source_text: str,
        _: str,
        candidates: list[dict[str, object]],
        titles: list[str],
        __: object,
    ) -> dict[str, object]:
        contexts.append((source_text, candidates, titles))
        result = generated.pop(0)
        if result["decision"] == "update":
            result["card_ids"] = [candidates[0]["id"]]
        return result

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            generator=fake_generate,
        )
        pipeline.start()
        pipeline.add("first")
        pipeline.flush()
        pipeline.add("first followup")
        pipeline.flush()
        pipeline.add("aside")
        pipeline.flush()
        result = await pipeline.close()

        assert len(result) == 1
        assert result[0].title == "Updated"
        assert result[0].source_text == "first\n\nfirst followup"
        assert contexts[1][1][0]["title"] == "First"
        assert contexts[2][2] == ["Updated"]
        persisted = json.loads(pipeline.json_path.read_text(encoding="utf-8"))
        assert persisted[0]["id"] == result[0].id
        assert persisted[0]["title"] == "Updated"


async def test_card_pipeline_records_error_after_five_failures() -> None:
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
            retry_base_seconds=0,
        )
        pipeline.start()
        pipeline.add("source")
        pipeline.flush()
        result = await pipeline.close()

    assert attempts == 5
    assert result == []
    assert pipeline.errors == ["failed"]


async def test_card_pipeline_close_without_generation_preserves_snapshot() -> None:
    calls = 0

    def generate(*_: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise AssertionError("pending card generation ran")

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            generator=generate,
        )
        existing = cards.Card(
            "existing",
            "Existing",
            "<p>Existing</p>",
            "source",
            1,
            "done",
        )
        pipeline.store.append(existing)
        pipeline.start()
        pipeline.add("pending")

        result = await pipeline.close(generate_pending=False)

    assert result == [existing]
    assert calls == 0
    assert pipeline.worker_task is not None and pipeline.worker_task.cancelled()
    assert pipeline.timer_task is not None and pipeline.timer_task.cancelled()


def test_old_card_json_uses_new_metadata_defaults() -> None:
    value = {
        "id": "legacy",
        "title": "Legacy",
        "html": "<p>Legacy</p>",
        "source_text": "source",
        "created_at": 1,
        "status": "done",
    }

    card = cards.Card(**value)

    assert card.topic_id is None
    assert card.unit_ids == []
    assert card.evidence_segment_ids == []
    assert card.component is None
    assert card.summary == ""
    assert card.keywords == []
    assert card.provisional is False
    assert card.related_card_ids == []
    assert card.reconciliation_history == []


def test_card_store_attach_drops_unknown_fields(capsys) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        json_path = root / "cards.json"
        json_path.write_text(
            json.dumps(
                [
                    {
                        "id": "future",
                        "title": "Future card",
                        "html": "<p>body</p>",
                        "source_text": "source",
                        "created_at": 1,
                        "status": "done",
                        "future_field": "newer version",
                    }
                ]
            ),
            encoding="utf-8",
        )

        store = cards.CardStore.attach(json_path, root / "cards.html")

        assert store.snapshot()[0].title == "Future card"
        assert "future_field" in capsys.readouterr().err


async def test_card_pipeline_retries_with_backoff() -> None:
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
        with patch("cards.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await pipeline._generate("source")

    assert attempts == 5
    assert sleep.await_args_list == [call(1.0), call(2.0), call(4.0), call(8.0)]


async def test_card_pipeline_preserves_order_during_retry() -> None:
    attempts: dict[str, int] = {}

    def generate(
        source_text: str,
        _: str,
        __: list[dict[str, object]],
        ___: list[str],
        ____: ai.AIModel,
    ) -> dict[str, object]:
        attempts[source_text] = attempts.get(source_text, 0) + 1
        if source_text == "first" and attempts[source_text] < 3:
            raise cards.CardGenerationError("retry first")
        return {
            "decision": "create",
            "card_ids": [],
            "title": source_text,
            "html": f"<p>{source_text}</p>",
            "summary": source_text,
            "keywords": [source_text],
        }

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            character_threshold=1,
            retry_base_seconds=0,
            generator=generate,
        )
        pipeline.start()
        pipeline.add("first")
        pipeline.add("second")
        result = await pipeline.close()

    assert [card.title for card in result] == ["first", "second"]
    assert attempts == {"first": 3, "second": 1}


async def test_card_pipeline_keeps_working_after_decision_persist_failure() -> None:
    generated: list[str] = []

    def generate(
        source_text: str,
        _: str,
        __: list[dict[str, object]],
        ___: list[str],
        ____: ai.AIModel,
    ) -> dict[str, object]:
        generated.append(source_text)
        return {
            "decision": "create",
            "card_ids": [],
            "title": source_text,
            "html": f"<p>{source_text}</p>",
            "summary": source_text,
            "keywords": [source_text],
        }

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            character_threshold=1,
            retry_base_seconds=0,
            generator=generate,
        )
        persist = pipeline.store._persist
        persist_calls = 0

        def fail_first_persist() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                raise OSError("disk full")
            persist()

        pipeline.store._persist = fail_first_persist
        pipeline.start()
        pipeline.add("first")
        pipeline.add("second")
        result = await pipeline.close()

    assert generated == ["first", "second"]
    assert [card.title for card in result] == ["first", "second"]
    assert pipeline.errors == ["disk full"]


def test_card_candidates_are_lightweight_and_rank_non_last_match() -> None:
    first = cards.Card(
        "first",
        "Python packaging",
        "<p>large first body</p>",
        "large first source",
        1,
        "done",
        summary="Python wheel publishing",
        keywords=["python", "wheel"],
    )
    second = cards.Card(
        "second",
        "Database migration",
        "<p>large second body</p>",
        "large second source",
        2,
        "done",
        summary="Schema changes",
        keywords=["database"],
    )

    candidates = cards.select_card_candidates(
        "Python wheel packagingに戻ります", [first, second]
    )

    assert [candidate["id"] for candidate in candidates] == ["first"]
    assert set(candidates[0]) == {"id", "title", "summary", "keywords"}
    assert "large first body" not in json.dumps(candidates)
    assert "large first source" not in json.dumps(candidates)


async def test_card_pipeline_updates_non_last_and_merges_with_stable_id() -> None:
    def generate(
        source_text: str,
        _: str,
        candidates: list[dict[str, object]],
        __: list[str],
        ___: ai.AIModel,
    ) -> dict[str, object]:
        if source_text == "Python wheel first":
            return {
                "decision": "create",
                "card_ids": [],
                "title": "Python wheel",
                "html": "<p>first</p>",
                "summary": "Python wheel",
                "keywords": ["python", "wheel"],
            }
        if source_text == "Database schema":
            return {
                "decision": "create",
                "card_ids": [],
                "title": "Database schema",
                "html": "<p>second</p>",
                "summary": "Database schema",
                "keywords": ["database", "schema"],
            }
        if source_text == "Python wheel update":
            return {
                "decision": "update",
                "card_ids": [candidates[0]["id"]],
                "title": "Python wheel updated",
                "html": "<p>updated</p>",
                "summary": "Python wheel updated",
                "keywords": ["python", "wheel"],
            }
        return {
            "decision": "merge",
            "card_ids": [candidate["id"] for candidate in candidates],
            "title": "Python and database",
            "html": "<p>merged</p>",
            "summary": "Python database integration",
            "keywords": ["python", "database"],
        }

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            character_threshold=1,
            generator=generate,
        )
        pipeline.start()
        pipeline.add("Python wheel first")
        pipeline.add("Database schema")
        await pipeline.queue.join()
        created = pipeline.store.snapshot()
        stable_id = created[0].id
        pipeline.add("Python wheel update")
        await pipeline.queue.join()
        updated = pipeline.store.snapshot()
        assert updated[0].id == stable_id
        assert updated[0].title == "Python wheel updated"
        assert updated[1].title == "Database schema"
        pipeline.add("Python database integration")
        result = await pipeline.close()

    assert len(result) == 1
    assert result[0].id == stable_id
    assert result[0].source_text == (
        "Python wheel first\n\nPython wheel update\n\n"
        "Database schema\n\nPython database integration"
    )
    assert result[0].provisional is True


async def test_card_pipeline_skips_extremely_short_tail_before_generation() -> None:
    calls = 0

    def generate(*_: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "decision": "create",
            "card_ids": [],
            "title": "Fragment",
            "html": "<p>fragment</p>",
            "summary": "Fragment",
            "keywords": ["fragment"],
        }

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key", output_dir=Path(directory), generator=generate
        )
        pipeline.add("。")
        result = await pipeline.close()

    assert calls == 0
    assert result == []


async def test_card_pipeline_links_new_card_to_existing_candidate() -> None:
    call = 0

    def generate(
        _: str,
        __: str,
        candidates: list[dict[str, object]],
        ___: list[str],
        ____: ai.AIModel,
    ) -> dict[str, object]:
        nonlocal call
        call += 1
        decision = "create"
        card_ids: list[object] = []
        if call == 2:
            decision = "link"
            card_ids = [candidates[0]["id"]]
        return {
            "decision": decision,
            "card_ids": card_ids,
            "title": f"Python {call}",
            "html": f"<p>{call}</p>",
            "summary": f"Python relation {call}",
            "keywords": ["python"],
        }

    with tempfile.TemporaryDirectory() as directory:
        pipeline = cards.CardPipeline(
            "api-key",
            output_dir=Path(directory),
            character_threshold=1,
            generator=generate,
        )
        pipeline.start()
        pipeline.add("Python first")
        pipeline.add("Python related")
        result = await pipeline.close()

    assert len(result) == 2
    assert result[1].related_card_ids == [result[0].id]


def test_live_final_reconciliation_history_is_recorded() -> None:
    live = [
        cards.Card(
            "live-1",
            "減価償却の計算方法",
            "<p>live</p>",
            "source",
            1,
            "done",
            summary="取得価額と耐用年数から各期の費用を求める。",
            keywords=["定額法", "耐用年数"],
            provisional=True,
        ),
        cards.Card(
            "live-2",
            "Unrelated note",
            "<p>drop</p>",
            "source",
            2,
            "done",
            summary="Unrelated note",
            keywords=["unrelated"],
            provisional=True,
        ),
    ]
    final = [
        cards.Card(
            "final-1",
            "減価償却の計算方法",
            "<p>final</p>",
            "source",
            0,
            "done",
        )
    ]

    reconciled = cards.reconcile_live_cards(live, final)

    assert reconciled[0].reconciliation_history == [
        {"action": "keep", "final_card_ids": ["final-1"]}
    ]
    assert reconciled[1].reconciliation_history == [
        {"action": "drop", "final_card_ids": []}
    ]


def main() -> None:
    test_text_buffer_triggers()
    test_sanitize_card_html()
    test_card_generation_payload()
    test_generate_card_parses_and_sanitizes_response()
    test_card_store_replaces_all_cards_atomically()
    asyncio.run(test_card_pipeline_applies_decisions_in_order())
    asyncio.run(test_card_pipeline_records_error_after_five_failures())
    asyncio.run(test_card_pipeline_close_without_generation_preserves_snapshot())
    asyncio.run(test_card_pipeline_retries_with_backoff())
    asyncio.run(test_card_pipeline_preserves_order_during_retry())
    print("ok")


if __name__ == "__main__":
    main()
