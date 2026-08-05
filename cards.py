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
あなたは講義のライブ文字起こしを、コンパクトな日本語の図解カードに変換します。
新しいテキスト・直前のカード・トピック概要を踏まえ、必ず次のいずれか1つを選びます:
- new_card: 新しい話題であり、独立したカードに値する。
- update_last: 新しいテキストが直前のカードの補完・修正にとどまる。
- skip: 雑談・繰り返し・図解に値しない断片である。
直前のカードと同じ話題が続いている場合は new_card より update_last を優先します。
1〜2文の言い換えにしかならないカードや、既存カードとほぼ重複するカードを
作るくらいなら skip を優先します。

HTMLを書く前に、まず内容の論理構造を特定し、それに合致するコンポーネント
ルートを1つだけ選びます。安易に flow を既定にしてはいけません。
- flow: 真の系列のみ — 順序そのものに意味がある3段階以上の手順・段階・
  因果連鎖。判定基準: 箱を並べ替えても意味が変わらないなら flow は誤りです。
- compare: 2つ以上の選択肢・対比・ビフォーアフター・長所短所・対立する
  アプローチ。1つの側につき compare-item を1つ使います。
- tree: 1つの概念が種類・原因・要因・論点に枝分かれするもの
  (分類や分解で、順序を持たないもの)。
- timeline: 日付・時代・明示的な時系列に紐づく出来事。
- keyvalue: 名前付きの事実や数値 — 指標・定義・用語と意味の対。
  具体的な数値が主役の内容では常に keyvalue を優先します。
- table: 複数の対象を2つ以上の共通属性で比較するもの。
- callout: 単一の主張・洞察・結論とその補足。1つの考えだけの内容に
  適しています — 1つの考えを flow に水増ししてはいけません。

記述ルール:
- 1つの文を複数の flow-step に分割してはいけません。各ステップは独立した
  段階を自分の言葉で記述します。
- flow-step・compare-item・tree-branch の内部では、太字の見出しと説明文を
  別々の要素(strong の後に p または span)に分け、1行に連結しません。
- 文字起こし中の具体的な数値・金額・割合・固有名詞は、カードにそのまま
  残します。これらは最も価値の高い情報です。省略・丸め・捏造は禁止です。
- 1カードにつき主コンポーネントは1つ。結論や注意点のための callout を
  最大1つまで追加できます。同種のコンポーネントを2つ重ねてはいけません。

new_card と update_last では、簡潔なタイトルとカードの内側のHTMLのみを
返します。使用可能なタグは div, span, p, ul, ol, li, table, thead, tbody,
tr, th, td, h3, h4, strong, em です。使用可能なクラスは card-body, flow,
flow-step, flow-arrow, compare, compare-item, tree, tree-branch, timeline,
timeline-item, keyvalue, keyvalue-item, key, value, callout, label, accent,
muted です。html, style, script, インラインスタイル, イベントハンドラ,
URL, 画像, その他のタグやクラスは決して出力しません。各カードは見出しと
3〜7個の要素に収め、内部スクロールを発生させません。明らかな文字起こし
ミスは根拠のない事実を加えない範囲で自然に修正しますが、確信のない
固有名詞を推測で「修正」してはいけません — 推測するくらいなら文字起こしの
表記をそのまま残します。skip の場合はタイトルとHTMLを空文字列で返します。
"""

FINAL_CARD_INSTRUCTIONS = """\
あなたは講義の完全な最終文字起こしを、コンパクトな日本語の図解カードに
変換します。まず文字起こし全体を読み、意味のある話題単位に分割します。
話者が見出しやセクション名を読み上げている場合は、それを話題の区切りの
手がかりとして使います。カードは文字起こしの出現順に返します。雑談・
繰り返し・図解に値しない内容はスキップします。各カードには、対応する
最終文字起こしの該当箇所を source_text にそのまま写します。

HTMLを書く前に、まず内容の論理構造を特定し、それに合致するコンポーネント
ルートを1つだけ選びます。安易に flow を既定にしてはいけません。
- flow: 真の系列のみ — 順序そのものに意味がある3段階以上の手順・段階・
  因果連鎖。判定基準: 箱を並べ替えても意味が変わらないなら flow は誤りです。
- compare: 2つ以上の選択肢・対比・ビフォーアフター・長所短所・対立する
  アプローチ。1つの側につき compare-item を1つ使います。
- tree: 1つの概念が種類・原因・要因・論点に枝分かれするもの
  (分類や分解で、順序を持たないもの)。
- timeline: 日付・時代・明示的な時系列に紐づく出来事。
- keyvalue: 名前付きの事実や数値 — 指標・定義・用語と意味の対。
  具体的な数値が主役の内容では常に keyvalue を優先します。
- table: 複数の対象を2つ以上の共通属性で比較するもの。
- callout: 単一の主張・洞察・結論とその補足。1つの考えだけの内容に
  適しています — 1つの考えを flow に水増ししてはいけません。

記述ルール:
- 1つの文を複数の flow-step に分割してはいけません。各ステップは独立した
  段階を自分の言葉で記述します。
- flow-step・compare-item・tree-branch の内部では、太字の見出しと説明文を
  別々の要素(strong の後に p または span)に分け、1行に連結しません。
- 文字起こし中の具体的な数値・金額・割合・固有名詞は、カードにそのまま
  残します。これらは最も価値の高い情報です。省略・丸め・捏造は禁止です。
- 1カードにつき主コンポーネントは1つ。結論や注意点のための callout を
  最大1つまで追加できます。同種のコンポーネントを2つ重ねてはいけません。

簡潔なタイトルとカードの内側のHTMLのみを返します。使用可能なタグは div,
span, p, ul, ol, li, table, thead, tbody, tr, th, td, h3, h4, strong, em
です。使用可能なクラスは card-body, flow, flow-step, flow-arrow, compare,
compare-item, tree, tree-branch, timeline, timeline-item, keyvalue,
keyvalue-item, key, value, callout, label, accent, muted です。html, style,
script, インラインスタイル, イベントハンドラ, URL, 画像, その他のタグや
クラスは決して出力しません。各カードは見出しと3〜7個の要素に収め、内部
スクロールを発生させません。明らかな文字起こしミスは根拠のない事実を
加えない範囲で自然に修正します。
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

    @classmethod
    def attach(cls, json_path: Path, html_path: Path) -> "CardStore":
        store = cls.__new__(cls)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        store.json_path = json_path
        store.html_path = html_path
        if json_path.exists():
            store.cards = [
                Card(**value)
                for value in json.loads(json_path.read_text(encoding="utf-8"))
            ]
        else:
            store.cards = []
            store._persist()
        return store

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
        store: CardStore | None = None,
    ) -> None:
        self.api_key = api_key
        self.store = store if store is not None else CardStore(output_dir)
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
