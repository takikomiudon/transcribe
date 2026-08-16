"""Generate and persist realtime diagram cards from completed transcripts."""

from __future__ import annotations

import asyncio
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import ai

CARD_TIMEOUT_SECONDS = 20
CARD_CHARACTER_THRESHOLD = 300
CARD_IDLE_SECONDS = 20
CARD_MAX_SECONDS = 90
CARD_MAX_ATTEMPTS = 5
CARD_RETRY_BASE_SECONDS = 1.0
CARD_CANDIDATE_LIMIT = 3
MIN_LIVE_CHUNK_CHARACTERS = 3

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

CARD_JSON_EXAMPLE = {
    "decision": "create",
    "card_ids": [],
    "title": "話題のタイトル",
    "html": '<div class="callout"><p>要点の説明</p></div>',
    "summary": "要点の短い要約",
    "keywords": ["主要語"],
}
CARD_SKIP_JSON_EXAMPLE = {
    "decision": "skip",
    "card_ids": [],
    "title": "",
    "html": "",
    "summary": "",
    "keywords": [],
}
CARD_INSTRUCTIONS = f"""\
あなたは講義のライブ文字起こしを、コンパクトな日本語の図解カードに変換します。
新しいテキスト・候補カード・トピック概要を踏まえ、必ず次のいずれか1つを選びます:
- create: 新しい話題であり、独立したカードに値する。card_idsは空にする。
- update: 既存候補1件の補完・修正。card_idsへ対象IDを1件入れる。
- merge: 重複する既存候補を1件へ統合。card_idsへ対象IDを2件以上入れる。
- link: 新しいカードを候補へ関連付ける。card_idsへ関連先IDを入れる。
- skip: 雑談・繰り返し・図解に値しない断片である。
候補と同じ話題が続いている場合は create より update を優先します。
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

create、update、merge、linkでは、簡潔なタイトル、カードの内側のHTML、
1文のsummary、検索用のkeywordsを返します。候補にないIDを返してはいけません。
使用可能なタグは div, span, p, ul, ol, li, table, thead, tbody,
tr, th, td, h3, h4, strong, em です。使用可能なクラスは card-body, flow,
flow-step, flow-arrow, compare, compare-item, tree, tree-branch, timeline,
timeline-item, keyvalue, keyvalue-item, key, value, callout, label, accent,
muted です。html, style, script, インラインスタイル, イベントハンドラ,
URL, 画像, その他のタグやクラスは決して出力しません。各カードは見出しと
3〜7個の要素に収め、内部スクロールを発生させません。明らかな文字起こし
ミスは根拠のない事実を加えない範囲で自然に修正しますが、確信のない
固有名詞を推測で「修正」してはいけません — 推測するくらいなら文字起こしの
表記をそのまま残します。skip の場合のみ、card_idsとkeywordsを空配列、
その他を空文字列で返します。
出力例:
- create:
{json.dumps(CARD_JSON_EXAMPLE, ensure_ascii=False)}
- skip:
{json.dumps(CARD_SKIP_JSON_EXAMPLE, ensure_ascii=False)}
最終応答はJSONで返し、例と同じフィールドだけを含めます。
"""

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["create", "update", "merge", "link", "skip"],
        },
        "card_ids": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
        "html": {"type": "string"},
        "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "decision",
        "card_ids",
        "title",
        "html",
        "summary",
        "keywords",
    ],
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
    topic_id: str | None = None
    unit_ids: list[str] = field(default_factory=list)
    evidence_segment_ids: list[str] = field(default_factory=list)
    component: str | None = None
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    provisional: bool = False
    related_card_ids: list[str] = field(default_factory=list)
    reconciliation_history: list[dict[str, object]] = field(default_factory=list)


def _normalized_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)


def _search_grams(value: str) -> set[str]:
    normalized = _normalized_search_text(value)
    if not normalized:
        return set()
    if len(normalized) < 3:
        return {normalized}
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _card_search_text(card: Card) -> str:
    return " ".join([card.title, card.summary, *card.keywords])


def _search_score(source_text: str, candidate_text: str) -> int:
    return len(_search_grams(source_text) & _search_grams(candidate_text))


def _reconciliation_score(source_text: str, candidate_text: str) -> int:
    # ponytail: 25% trigram overlap is a cheap correspondence gate; replace
    # with evaluated semantic matching when reconciliation errors are measured.
    source = _search_grams(source_text)
    candidate = _search_grams(candidate_text)
    overlap = source & candidate
    union = source | candidate
    if not union or len(overlap) * 4 < len(union):
        return 0
    return len(overlap)


def select_card_candidates(
    source_text: str,
    cards: list[Card],
    limit: int = CARD_CANDIDATE_LIMIT,
) -> list[dict[str, object]]:
    # ponytail: character trigrams avoid a vector service; replace only when
    # measured retrieval misses justify the operational cost.
    ranked: list[tuple[int, int, Card]] = []
    for index, card in enumerate(cards):
        score = _search_score(source_text, _card_search_text(card))
        if score > 0:
            ranked.append((score, index, card))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "id": card.id,
            "title": card.title,
            "summary": card.summary,
            "keywords": list(card.keywords),
        }
        for _, _, card in ranked[:limit]
    ]


def reconcile_live_cards(
    live_cards: list[Card], final_cards: list[Card]
) -> list[Card]:
    matches: list[list[str]] = []
    for live_card in live_cards:
        ranked: list[tuple[int, int, Card]] = []
        for index, final_card in enumerate(final_cards):
            live_parts = [live_card.title]
            final_parts = [final_card.title]
            if live_card.summary and final_card.summary:
                live_parts.append(live_card.summary)
                final_parts.append(final_card.summary)
            if live_card.keywords and final_card.keywords:
                live_parts.extend(live_card.keywords)
                final_parts.extend(final_card.keywords)
            score = _reconciliation_score(
                " ".join(live_parts), " ".join(final_parts)
            )
            if score > 0:
                ranked.append((score, index, final_card))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        final_ids: list[str] = []
        if ranked:
            top_score = ranked[0][0]
            final_ids = [card.id for score, _, card in ranked if score == top_score]
        matches.append(final_ids)

    single_targets = [ids[0] for ids in matches if len(ids) == 1]
    reconciled: list[Card] = []
    for live_card, final_ids in zip(live_cards, matches, strict=True):
        action = "drop"
        if len(final_ids) > 1:
            action = "split"
        elif len(final_ids) == 1:
            final_id = final_ids[0]
            if single_targets.count(final_id) > 1:
                action = "merge"
            else:
                final_card = next(card for card in final_cards if card.id == final_id)
                action = "update"
                if _normalized_search_text(live_card.title) == _normalized_search_text(
                    final_card.title
                ):
                    action = "keep"
        history = [
            *live_card.reconciliation_history,
            {"action": action, "final_card_ids": final_ids},
        ]
        reconciled.append(replace(live_card, reconciliation_history=history))
    return reconciled


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
            if tag in BLOCKED_CONTENT_TAGS:
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
            if tag in BLOCKED_CONTENT_TAGS:
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
    candidates: list[dict[str, object]],
    topic_outline: list[str],
    model: ai.AIModel = ai.DEFAULT_AI_MODEL,
) -> dict[str, Any]:
    context = {
        "new_text": source_text,
        "candidates": candidates,
        "topic_outline": topic_outline,
    }
    return {
        "model": model.model,
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
        "max_output_tokens": 4_096,
        "store": False,
    }


def _response_output_text(
    response: dict[str, Any], model: ai.AIModel = ai.DEFAULT_AI_MODEL
) -> str:
    if model.provider == ai.OPENAI_PROVIDER and response.get("status") != "completed":
        raise CardGenerationError("図解生成が完了しませんでした。")
    try:
        return ai.response_text(response, model)
    except ValueError as error:
        raise CardGenerationError(str(error)) from error


def _request_card_generation(
    payload: dict[str, Any],
    api_key: str,
    model: ai.AIModel = ai.DEFAULT_AI_MODEL,
) -> dict[str, Any]:
    try:
        return ai.request(payload, api_key, CARD_TIMEOUT_SECONDS, model)
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


def generate_card(
    source_text: str,
    api_key: str,
    candidates: list[dict[str, object]],
    topic_outline: list[str],
    model: ai.AIModel = ai.DEFAULT_AI_MODEL,
) -> dict[str, object]:
    response = _request_card_generation(
        card_generation_payload(source_text, candidates, topic_outline, model),
        api_key,
        model,
    )

    try:
        result = json.loads(
            ai.strip_code_fence(_response_output_text(response, model))
        )
        decision = result["decision"]
        card_ids = result["card_ids"]
        title = str(result["title"]).strip()
        raw_html = str(result["html"])
        summary = str(result["summary"]).strip()
        keywords = result["keywords"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise CardGenerationError("図解生成の応答形式が不正です。") from error
    if decision not in {"create", "update", "merge", "link", "skip"}:
        raise CardGenerationError("図解生成の判定が不正です。")
    if not isinstance(card_ids, list) or not all(
        isinstance(card_id, str) for card_id in card_ids
    ):
        raise CardGenerationError("図解生成の対象カードIDが不正です。")
    if not isinstance(keywords, list) or not all(
        isinstance(keyword, str) for keyword in keywords
    ):
        raise CardGenerationError("図解生成の主要語が不正です。")
    candidate_ids = {str(candidate["id"]) for candidate in candidates}
    if any(card_id not in candidate_ids for card_id in card_ids):
        raise CardGenerationError("図解生成が候補外のカードを参照しました。")
    valid_target_count = (
        (decision in {"create", "skip"} and not card_ids)
        or (decision in {"update", "link"} and len(card_ids) == 1)
        or (decision == "merge" and len(card_ids) >= 2)
    )
    if not valid_target_count:
        raise CardGenerationError("図解生成の対象カード数が不正です。")
    if decision == "skip":
        title = ""
        sanitized_html = ""
        summary = ""
        keywords = []
    else:
        sanitized_html = sanitize_card_html(raw_html)
        if not title or not sanitized_html or not summary or not keywords:
            raise CardGenerationError("図解生成のタイトルまたはHTMLが空です。")
    total_tokens = (response.get("usage") or {}).get("total_tokens")
    return {
        "decision": decision,
        "card_ids": card_ids,
        "title": title,
        "html": sanitized_html,
        "summary": summary,
        "keywords": keywords,
        "total_tokens": total_tokens,
    }


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
            known_fields = {card_field.name for card_field in fields(Card)}
            store.cards = []
            for value in json.loads(json_path.read_text(encoding="utf-8")):
                if not isinstance(value, dict):
                    raise ValueError("カードJSONの形式が不正です。")
                dropped = sorted(set(value) - known_fields)
                if dropped:
                    print(
                        "警告: 未知のカード項目を読み飛ばしました: "
                        + ", ".join(dropped),
                        file=sys.stderr,
                    )
                store.cards.append(
                    Card(
                        **{
                            key: item
                            for key, item in value.items()
                            if key in known_fields
                        }
                    )
                )
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
        retry_base_seconds: float = CARD_RETRY_BASE_SECONDS,
        generator: Callable[
            [
                str,
                str,
                list[dict[str, object]],
                list[str],
                ai.AIModel,
            ],
            dict[str, object],
        ] = generate_card,
        store: CardStore | None = None,
        model: ai.AIModel = ai.DEFAULT_AI_MODEL,
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
        self.model = model
        self.retry_base_seconds = retry_base_seconds
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None
        self.timer_task: asyncio.Task[None] | None = None
        self.closed = False
        self.errors: list[str] = []

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

    async def close(self, *, generate_pending: bool = True) -> list[Card]:
        if self.closed:
            return self.store.snapshot()
        self.closed = True
        if not generate_pending:
            tasks = [
                task
                for task in (self.timer_task, self.worker_task)
                if task is not None
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return self.store.snapshot()
        if self.worker_task is None:
            self.start()
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
        if len(_normalized_search_text(source_text)) < MIN_LIVE_CHUNK_CHARACTERS:
            return
        last_error: Exception | None = None
        started = time.perf_counter()
        for attempt in range(CARD_MAX_ATTEMPTS):
            try:
                candidates = select_card_candidates(
                    source_text, self.store.snapshot()
                )
                result = await asyncio.to_thread(
                    self.generator,
                    source_text,
                    self.api_key,
                    candidates,
                    self.store.titles(),
                    self.model,
                )
            except (CardGenerationError, OSError, RuntimeError, ValueError) as error:
                last_error = error
                if attempt < CARD_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(self.retry_base_seconds * (2**attempt))
                continue
            decision = str(result["decision"])
            if decision == "skip":
                return
            try:
                self._store_decision(source_text, decision, result)
            except (
                CardGenerationError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                self.errors.append(str(error))
                print(
                    f"[cards] error: {time.perf_counter() - started:.1f}s, "
                    "tokens unknown",
                    flush=True,
                )
                print(f"[cards] 警告: {error}", file=sys.stderr, flush=True)
                return
            token_log = "tokens unknown"
            if "total_tokens" in result and result["total_tokens"] is not None:
                token_log = f"{result['total_tokens']} tokens"
            print(
                f"[cards] {decision}: {time.perf_counter() - started:.1f}s, "
                f"{token_log}",
                flush=True,
            )
            return

        message = "図解を生成できませんでした。文字起こしは継続します。"
        error_message = message
        if last_error is not None:
            error_message = str(last_error)
        self.errors.append(error_message)
        print(
            f"[cards] error: {time.perf_counter() - started:.1f}s, tokens unknown",
            flush=True,
        )
        print(f"[cards] 警告: {error_message}", file=sys.stderr, flush=True)

    def _store_decision(
        self,
        source_text: str,
        decision: str,
        result: dict[str, object],
    ) -> None:
        title = str(result["title"]).strip()
        card_html = str(result["html"])
        summary = str(result["summary"]).strip()
        keywords = [str(keyword) for keyword in result["keywords"]]
        card_ids = [str(card_id) for card_id in result["card_ids"]]
        existing = self.store.snapshot()
        by_id = {card.id: card for card in existing}
        if any(card_id not in by_id for card_id in card_ids):
            raise CardGenerationError("候補にない速報カードは更新できません。")
        if decision == "update":
            previous = by_id[card_ids[0]]
            updated = Card(
                id=previous.id,
                title=title,
                html=card_html,
                source_text=f"{previous.source_text}\n\n{source_text}",
                created_at=previous.created_at,
                status="done",
                summary=summary,
                keywords=keywords,
                provisional=True,
                related_card_ids=previous.related_card_ids,
                reconciliation_history=previous.reconciliation_history,
            )
            updated_cards: list[Card] = []
            for card in existing:
                if card.id == previous.id:
                    updated_cards.append(updated)
                else:
                    updated_cards.append(card)
            self.store.replace_all(updated_cards)
            return
        if decision == "merge":
            selected = [card for card in existing if card.id in card_ids]
            target = selected[0]
            merged_source = "\n\n".join(
                [*[card.source_text for card in selected], source_text]
            )
            merged = Card(
                id=target.id,
                title=title,
                html=card_html,
                source_text=merged_source,
                created_at=target.created_at,
                status="done",
                summary=summary,
                keywords=keywords,
                provisional=True,
                related_card_ids=list(
                    dict.fromkeys(
                        related_id
                        for card in selected
                        for related_id in card.related_card_ids
                        if related_id not in card_ids
                    )
                ),
                reconciliation_history=target.reconciliation_history,
            )
            merged_cards: list[Card] = []
            for card in existing:
                if card.id == target.id:
                    merged_cards.append(merged)
                elif card.id not in card_ids:
                    merged_cards.append(card)
            self.store.replace_all(merged_cards)
            return
        if decision not in {"create", "link"}:
            raise CardGenerationError("速報カードの判定が不正です。")
        related_card_ids: list[str] = []
        if decision == "link":
            related_card_ids = card_ids
        self.store.append(
            Card(
                id=uuid.uuid4().hex,
                title=title,
                html=card_html,
                source_text=source_text,
                created_at=time.time(),
                status="done",
                summary=summary,
                keywords=keywords,
                provisional=True,
                related_card_ids=related_card_ids,
            )
        )
