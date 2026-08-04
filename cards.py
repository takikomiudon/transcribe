"""Generate and persist realtime diagram cards from completed transcripts."""

from __future__ import annotations

import asyncio
import html
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


CARD_MODEL = "gpt-5.6-luna"
RESPONSES_URL = "https://api.openai.com/v1/responses"
CARD_TIMEOUT_SECONDS = 20
CARD_CHARACTER_THRESHOLD = 300
CARD_IDLE_SECONDS = 20
CARD_MAX_SECONDS = 90

ALLOWED_TAGS = {
    "div",
    "em",
    "h3",
    "h4",
    "li",
    "ol",
    "p",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
ALLOWED_CLASSES = {
    "card-body",
    "flow",
    "flow-step",
    "flow-arrow",
    "compare",
    "compare-item",
    "tree",
    "tree-branch",
    "timeline",
    "timeline-item",
    "keyvalue",
    "keyvalue-item",
    "key",
    "value",
    "callout",
    "label",
    "accent",
    "muted",
}
BLOCKED_CONTENT_TAGS = {"iframe", "math", "object", "script", "style", "svg"}

CARD_INSTRUCTIONS = """\
You turn live lecture transcripts into compact Japanese diagram cards.
Use the new text, the last card, and the topic outline to choose exactly one:
- new_card: a new topic deserves its own card.
- update_last: the new text completes or corrects only the latest card.
- skip: the text is chatter, repetition, or too insubstantial to diagram.

For new_card and update_last, return a concise title and only the card's inner
HTML. Use one of these component roots: flow, compare, tree, timeline,
keyvalue, or callout. Allowed tags are div, span, p, ul, ol, li, table, thead,
tbody, tr, th, td, h3, h4, strong, and em. Allowed classes are card-body,
flow, flow-step, flow-arrow, compare, compare-item, tree, tree-branch,
timeline, timeline-item, keyvalue, keyvalue-item, key, value, callout, label,
accent, and muted. Never output html, style, script, inline style, event
handlers, URLs, images, or any other tags/classes. Keep each card to a heading
and 3-7 elements with no internal scrolling. Naturally correct obvious
transcription errors without adding unsupported facts. For skip, return empty
title and html strings.
"""

FINAL_CARD_INSTRUCTIONS = """\
You turn a complete final lecture transcript into compact Japanese diagram
cards. Read the whole transcript first, then split it into meaningful topics.
Return the cards in transcript order. Skip chatter, repetition, and content too
insubstantial to diagram. For each card, copy the relevant final transcript
passage into source_text.

Return a concise title and only the card's inner HTML. Use one of these
component roots: flow, compare, tree, timeline, keyvalue, or callout. Allowed
tags are div, span, p, ul, ol, li, table, thead, tbody, tr, th, td, h3, h4,
strong, and em. Allowed classes are card-body, flow, flow-step, flow-arrow,
compare, compare-item, tree, tree-branch, timeline, timeline-item, keyvalue,
keyvalue-item, key, value, callout, label, accent, and muted. Never output html,
style, script, inline style, event handlers, URLs, images, or any other
tags/classes. Keep each card to a heading and 3-7 elements with no internal
scrolling. Naturally correct obvious transcription errors without adding
unsupported facts.
"""

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["new_card", "update_last", "skip"],
        },
        "title": {"type": "string"},
        "html": {"type": "string"},
    },
    "required": ["decision", "title", "html"],
    "additionalProperties": False,
}

FINAL_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "html": {"type": "string"},
                    "source_text": {"type": "string"},
                },
                "required": ["title", "html", "source_text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


class CardGenerationError(Exception):
    """OpenAI could not produce a valid diagram card."""


@dataclass
class Card:
    id: str
    title: str
    html: str
    source_text: str
    created_at: float
    status: str


class TextBuffer:
    def __init__(
        self,
        character_threshold: int = CARD_CHARACTER_THRESHOLD,
        idle_seconds: float = CARD_IDLE_SECONDS,
        max_seconds: float = CARD_MAX_SECONDS,
        started_at: float | None = None,
    ) -> None:
        self.character_threshold = character_threshold
        self.idle_seconds = idle_seconds
        self.max_seconds = max_seconds
        self.parts: list[str] = []
        self.last_text_at: float | None = None
        self.last_flush_at = time.monotonic() if started_at is None else started_at

    def add(self, text: str, now: float | None = None) -> str | None:
        text = text.strip()
        if not text:
            return None
        timestamp = time.monotonic() if now is None else now
        self.parts.append(text)
        self.last_text_at = timestamp
        if len("\n".join(self.parts)) >= self.character_threshold:
            return self.flush(timestamp)
        return None

    def due(self, now: float | None = None) -> bool:
        if not self.parts or self.last_text_at is None:
            return False
        timestamp = time.monotonic() if now is None else now
        return (
            timestamp - self.last_text_at >= self.idle_seconds
            or timestamp - self.last_flush_at >= self.max_seconds
        )

    def flush(self, now: float | None = None) -> str | None:
        if not self.parts:
            return None
        timestamp = time.monotonic() if now is None else now
        text = "\n".join(self.parts)
        self.parts.clear()
        self.last_text_at = None
        self.last_flush_at = timestamp
        return text


class _CardHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.blocked_depth:
            self.blocked_depth += 1
            return
        if tag in BLOCKED_CONTENT_TAGS:
            self.blocked_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        classes: list[str] = []
        for name, value in attrs:
            if name == "class" and value:
                classes.extend(
                    class_name
                    for class_name in value.split()
                    if class_name in ALLOWED_CLASSES
                )
        class_attribute = (
            f' class="{html.escape(" ".join(classes), quote=True)}"'
            if classes
            else ""
        )
        self.output.append(f"<{tag}{class_attribute}>")
        self.open_tags.append(tag)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.blocked_depth:
            self.blocked_depth -= 1
            return
        if self.open_tags and self.open_tags[-1] == tag:
            self.open_tags.pop()
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.output.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.open_tags:
            self.output.append(f"</{self.open_tags.pop()}>")
        return "".join(self.output).strip()


def sanitize_card_html(value: str) -> str:
    sanitizer = _CardHTMLSanitizer()
    try:
        sanitizer.feed(value)
        sanitizer.close()
    except (AssertionError, ValueError) as error:
        raise CardGenerationError("図解HTMLを解析できませんでした。") from error
    return sanitizer.result()


def card_generation_payload(
    source_text: str,
    last_card: Card | None,
    topic_outline: list[str],
) -> dict[str, Any]:
    context = {
        "new_text": source_text,
        "last_card": (
            {
                "title": last_card.title,
                "source_text": last_card.source_text,
            }
            if last_card
            else None
        ),
        "topic_outline": topic_outline,
    }
    return {
        "model": CARD_MODEL,
        "reasoning": {"effort": "none"},
        "instructions": CARD_INSTRUCTIONS,
        "input": json.dumps(context, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "diagram_card",
                "schema": CARD_SCHEMA,
                "strict": True,
            }
        },
        "max_output_tokens": 2_048,
        "store": False,
    }


def final_card_generation_payload(transcript: str) -> dict[str, Any]:
    # ponytail: one full-transcript request; add chapter batching only if model
    # limits are reached in real recordings.
    return {
        "model": CARD_MODEL,
        "reasoning": {"effort": "none"},
        "instructions": FINAL_CARD_INSTRUCTIONS,
        "input": transcript,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "final_diagram_cards",
                "schema": FINAL_CARD_SCHEMA,
                "strict": True,
            }
        },
        "max_output_tokens": 16_384,
        "store": False,
    }


def _response_output_text(response: dict[str, Any]) -> str:
    if response.get("status") != "completed":
        raise CardGenerationError("図解生成が完了しませんでした。")
    parts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise CardGenerationError("図解生成が拒否されました。")
            if content.get("type") == "output_text":
                parts.append(str(content.get("text", "")))
    text = "".join(parts).strip()
    if not text:
        raise CardGenerationError("図解生成の応答が空でした。")
    return text


def _request_card_generation(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=CARD_TIMEOUT_SECONDS
        ) as response:
            response_payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", {}).get("message")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        raise CardGenerationError(str(detail or f"HTTP {error.code}")) from error
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise CardGenerationError(f"図解生成との通信に失敗しました: {error}") from error
    except json.JSONDecodeError as error:
        raise CardGenerationError("図解生成から不正なJSONを受信しました。") from error
    return response_payload


def generate_card(
    source_text: str,
    api_key: str,
    last_card: Card | None,
    topic_outline: list[str],
) -> dict[str, object]:
    payload = _request_card_generation(
        card_generation_payload(source_text, last_card, topic_outline), api_key
    )

    try:
        result = json.loads(_response_output_text(payload))
        decision = result["decision"]
        title = str(result["title"]).strip()
        raw_html = str(result["html"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise CardGenerationError("図解生成の応答形式が不正です。") from error
    if decision not in {"new_card", "update_last", "skip"}:
        raise CardGenerationError("図解生成の判定が不正です。")
    if decision == "skip":
        title = ""
        sanitized_html = ""
    else:
        sanitized_html = sanitize_card_html(raw_html)
        if not title or not sanitized_html:
            raise CardGenerationError("図解生成のタイトルまたはHTMLが空です。")
    total_tokens = (payload.get("usage") or {}).get("total_tokens")
    return {
        "decision": decision,
        "title": title,
        "html": sanitized_html,
        "total_tokens": total_tokens,
    }


def generate_final_cards(transcript: str, api_key: str) -> list[Card]:
    payload = _request_card_generation(
        final_card_generation_payload(transcript), api_key
    )
    try:
        result = json.loads(_response_output_text(payload))
        raw_cards = result["cards"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise CardGenerationError("final図解生成の応答形式が不正です。") from error
    if not isinstance(raw_cards, list) or not raw_cards:
        raise CardGenerationError("final図解生成のカードが空です。")

    generated: list[Card] = []
    for raw_card in raw_cards:
        if not isinstance(raw_card, dict) or not all(
            isinstance(raw_card.get(key), str)
            for key in ("title", "html", "source_text")
        ):
            raise CardGenerationError("final図解生成のカード形式が不正です。")
        title = raw_card["title"].strip()
        source_text = raw_card["source_text"].strip()
        card_html = sanitize_card_html(raw_card["html"])
        if not title or not card_html or not source_text:
            raise CardGenerationError("final図解生成のカードに空の項目があります。")
        generated.append(
            Card(
                id=uuid.uuid4().hex,
                title=title,
                html=card_html,
                source_text=source_text,
                created_at=time.time(),
                status="done",
            )
        )
    return generated


def save_cards(cards: list[Card], output_path: Path) -> None:
    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            [asdict(card) for card in cards],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(output_path)


class CardStore:
    def __init__(self, output_dir: Path, now: datetime | None = None) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")
        stem = timestamp
        suffix = 2
        while (output_dir / f"{stem}.json").exists() or (
            output_dir / f"{stem}.html"
        ).exists():
            stem = f"{timestamp}-{suffix}"
            suffix += 1
        self.json_path = output_dir / f"{stem}.json"
        self.html_path = output_dir / f"{stem}.html"
        self.cards: list[Card] = []
        self._persist()

    def snapshot(self) -> list[Card]:
        return list(self.cards)

    def titles(self) -> list[str]:
        return [card.title for card in self.cards]

    def last(self) -> Card | None:
        return self.cards[-1] if self.cards else None

    def append(self, card: Card) -> None:
        self.cards.append(card)
        self._persist()

    def replace_last(self, card: Card) -> None:
        self.cards[-1] = card
        self._persist()

    def replace_all(self, cards: list[Card]) -> None:
        self.cards = list(cards)
        self._persist()

    def _persist(self) -> None:
        save_cards(self.cards, self.json_path)


class CardPipeline:
    def __init__(
        self,
        api_key: str,
        *,
        output_dir: Path = Path("cards_output"),
        character_threshold: int = CARD_CHARACTER_THRESHOLD,
        idle_seconds: float = CARD_IDLE_SECONDS,
        max_seconds: float = CARD_MAX_SECONDS,
        generator: Callable[
            [str, str, Card | None, list[str]], dict[str, object]
        ] = generate_card,
    ) -> None:
        self.api_key = api_key
        self.store = CardStore(output_dir)
        self.json_path = self.store.json_path
        self.html_path = self.store.html_path
        self.buffer = TextBuffer(
            character_threshold,
            idle_seconds,
            max_seconds,
        )
        self.generator = generator
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None
        self.timer_task: asyncio.Task[None] | None = None
        self.closed = False

    def start(self) -> None:
        if self.worker_task is not None:
            return
        self.worker_task = asyncio.create_task(self._worker())
        self.timer_task = asyncio.create_task(self._timer())

    def add(self, text: str) -> None:
        chunk = self.buffer.add(text)
        if chunk:
            self.queue.put_nowait(chunk)

    def flush(self) -> None:
        chunk = self.buffer.flush()
        if chunk:
            self.queue.put_nowait(chunk)

    async def close(self) -> list[Card]:
        if self.closed:
            return self.store.snapshot()
        if self.worker_task is None:
            self.start()
        self.closed = True
        if self.timer_task is not None:
            self.timer_task.cancel()
            await asyncio.gather(self.timer_task, return_exceptions=True)
        self.flush()
        self.queue.put_nowait(None)
        assert self.worker_task is not None
        await self.worker_task
        return self.store.snapshot()

    async def _timer(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.buffer.due():
                self.flush()

    async def _worker(self) -> None:
        while True:
            source_text = await self.queue.get()
            try:
                if source_text is None:
                    return
                await self._generate(source_text)
            finally:
                self.queue.task_done()

    async def _generate(self, source_text: str) -> None:
        last_error: Exception | None = None
        started = time.perf_counter()
        for _ in range(2):
            try:
                result = await asyncio.to_thread(
                    self.generator,
                    source_text,
                    self.api_key,
                    self.store.last(),
                    self.store.titles(),
                )
            except Exception as error:
                last_error = error
                continue
            decision = str(result.get("decision", ""))
            if decision == "skip":
                return
            self._store_decision(source_text, decision, result)
            total_tokens = result.get("total_tokens")
            token_log = f"{total_tokens} tokens" if total_tokens is not None else "tokens unknown"
            print(
                f"[cards] {decision}: {time.perf_counter() - started:.1f}s, "
                f"{token_log}",
                flush=True,
            )
            return

        message = "図解を生成できませんでした。文字起こしは継続します。"
        self.store.append(
            Card(
                id=uuid.uuid4().hex,
                title="図解生成エラー",
                html=f'<div class="callout"><p>{message}</p></div>',
                source_text=source_text,
                created_at=time.time(),
                status="error",
            )
        )
        print(
            f"[cards] error: {time.perf_counter() - started:.1f}s, tokens unknown",
            flush=True,
        )
        print(f"[cards] 警告: {last_error or message}", file=sys.stderr, flush=True)

    def _store_decision(
        self,
        source_text: str,
        decision: str,
        result: dict[str, object],
    ) -> None:
        title = str(result.get("title", "")).strip()
        card_html = str(result.get("html", ""))
        previous = self.store.last()
        if decision == "update_last" and previous is not None:
            self.store.replace_last(
                Card(
                    id=previous.id,
                    title=title,
                    html=card_html,
                    source_text=f"{previous.source_text}\n\n{source_text}",
                    created_at=previous.created_at,
                    status="done",
                )
            )
            return
        self.store.append(
            Card(
                id=uuid.uuid4().hex,
                title=title,
                html=card_html,
                source_text=source_text,
                created_at=time.time(),
                status="done",
            )
        )
