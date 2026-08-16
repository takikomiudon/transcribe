"""Local polling viewer and offline export for diagram cards."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from cards import Card
from card_models import Topic


VIEWER_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'">
<title>Realtime Transcription</title>
<style>
:root {
  color-scheme: light;
  --bg: #f4f6f8;
  --surface: #fff;
  --text: #18202a;
  --muted: #66717f;
  --line: #dbe1e8;
  --soft: #edf3ff;
  --accent: #315fca;
  --accent-strong: #20489e;
  --danger: #a53a3a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic UI", sans-serif;
  line-height: 1.65;
}
main { width: min(920px, calc(100% - 32px)); margin: 0 auto; padding: 48px 0 80px; }
.page-header { margin-bottom: 28px; }
.page-header p { margin: 4px 0 0; color: var(--muted); }
h1 { margin: 0; font-size: clamp(1.65rem, 4vw, 2.25rem); letter-spacing: -.03em; }
.panel { margin-bottom: 28px; }
.panel > h2 { margin: 0 0 12px; font-size: 1.25rem; }
.cards-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.cards-header h2 { margin: 0; font-size: 1.25rem; }
.card-tabs { display: flex; gap: 4px; padding: 4px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }
.card-tab { padding: 7px 14px; border: 0; border-radius: 8px; background: transparent; color: var(--muted); font: inherit; font-weight: 700; cursor: pointer; }
.card-tab[aria-selected="true"] { background: var(--soft); color: var(--accent-strong); }
.card-tab:disabled { cursor: not-allowed; opacity: .45; }
.card-tab:focus-visible { outline: 3px solid rgb(49 95 202 / 35%); outline-offset: 2px; }
.card-status { min-height: 1.65em; margin: -4px 0 12px; color: var(--muted); font-size: .88rem; }
.outline { margin: 0; padding: 16px 20px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); }
.outline ol { margin: 8px 0 0; padding-left: 24px; }
.outline button { border: 0; background: transparent; color: var(--accent-strong); font: inherit; font-weight: 700; cursor: pointer; text-align: left; }
.outline button:hover { text-decoration: underline; }
.outline button:focus-visible { outline: 3px solid rgb(49 95 202 / 35%); outline-offset: 2px; }
.outline-summary { margin: 2px 0 8px; color: var(--muted); font-size: .88rem; }
.transcript {
  min-height: 140px;
  margin: 0;
  padding: 20px 24px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: 0 8px 24px rgb(24 32 42 / 6%);
  color: var(--text);
  font: inherit;
  white-space: pre-wrap;
}
.cards { display: grid; gap: 20px; }
.card {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: 0 8px 24px rgb(24 32 42 / 6%);
}
.card > header { padding: 20px 24px 0; }
.card h2 { margin: 0; font-size: 1.25rem; letter-spacing: -.015em; }
.card-content { padding: 18px 24px 24px; }
.card.error { border-color: #e6b7b7; }
.card.error h2 { color: var(--danger); }
.card-body > :first-child { margin-top: 0; }
.card-body > :last-child { margin-bottom: 0; }
.flow { display: flex; align-items: stretch; gap: 10px; overflow-x: auto; padding: 4px 0; }
.flow-step { min-width: 150px; flex: 1; padding: 14px; border-radius: 10px; background: var(--soft); }
.flow-arrow { align-self: center; color: var(--accent); font-weight: 700; }
.compare { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.compare-item { padding: 16px; border: 1px solid var(--line); border-radius: 10px; }
.tree { padding-left: 10px; }
.tree-branch { margin: 8px 0; padding: 8px 0 8px 18px; border-left: 3px solid var(--accent); }
.timeline { display: grid; gap: 0; }
.timeline-item { position: relative; padding: 2px 0 18px 24px; border-left: 2px solid var(--line); }
.timeline-item::before { position: absolute; top: 7px; left: -6px; width: 10px; height: 10px; border-radius: 50%; background: var(--accent); content: ""; }
.keyvalue { display: grid; gap: 1px; overflow: hidden; border: 1px solid var(--line); border-radius: 10px; background: var(--line); }
.keyvalue-item { display: grid; grid-template-columns: minmax(110px, .35fr) 1fr; background: var(--surface); }
.key, .value { padding: 11px 14px; }
.key { background: var(--soft); font-weight: 700; }
.callout { padding: 16px 18px; border-left: 4px solid var(--accent); border-radius: 8px; background: var(--soft); }
.label { display: inline-block; margin-bottom: 5px; color: var(--accent-strong); font-size: .78rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.accent { color: var(--accent-strong); }
.muted { color: var(--muted); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; }
th { background: var(--soft); }
details { border-top: 1px solid var(--line); }
summary { cursor: pointer; padding: 12px 24px; color: var(--muted); font-size: .88rem; }
summary:focus-visible { outline: 3px solid rgb(49 95 202 / 35%); outline-offset: -3px; }
details pre { margin: 0; padding: 0 24px 20px; color: var(--muted); font: inherit; white-space: pre-wrap; }
.empty { padding: 32px; border: 1px dashed var(--line); border-radius: 12px; color: var(--muted); text-align: center; }
@media (max-width: 620px) {
  main { width: min(100% - 20px, 920px); padding-top: 28px; }
  .cards-header { align-items: stretch; flex-direction: column; }
  .card-tabs { align-self: flex-start; }
  .compare { grid-template-columns: 1fr; }
  .keyvalue-item { grid-template-columns: 1fr; }
  .card > header, .card-content { padding-right: 18px; padding-left: 18px; }
}
</style>
</head>
<body>
<main>
  <header class="page-header">
    <h1>Realtime Transcription</h1>
    <p>Scribe v2による精度重視の全文を自動更新しています。</p>
  </header>
  <section id="transcript-section" class="panel" aria-labelledby="transcript-heading">
    <h2 id="transcript-heading">Final Transcript</h2>
    <pre id="transcript" class="transcript" aria-live="polite">最初の文字起こしを待っています。</pre>
  </section>
  <nav id="outline-section" class="panel" aria-labelledby="outline-heading" hidden>
    <h2 id="outline-heading">目次</h2>
    <div id="outline" class="outline"></div>
  </nav>
  <section id="cards-section" class="panel" aria-labelledby="cards-heading">
    <div class="cards-header">
      <h2 id="cards-heading">Diagram Cards</h2>
      <div class="card-tabs" role="tablist" aria-label="カードの種類">
        <button id="final-cards-tab" class="card-tab" type="button" role="tab" aria-selected="false" aria-controls="cards" aria-disabled="true" tabindex="-1" disabled>品質版</button>
        <button id="live-cards-tab" class="card-tab" type="button" role="tab" aria-selected="true" aria-controls="cards" tabindex="0">速報版</button>
      </div>
    </div>
    <p id="cards-status" class="card-status" role="status" aria-live="polite">速報版を表示しています。</p>
    <div id="cards" class="cards" role="tabpanel" aria-labelledby="live-cards-tab" aria-live="polite"><p class="empty">カードを待っています。</p></div>
  </section>
</main>
<script>
const INITIAL_CARDS = null;
const INITIAL_FINAL_CARDS = null;
const INITIAL_TOPICS = [];
const CARDS_ENABLED = true;
const TRANSCRIPT_ENABLED = true;
const transcriptSection = document.getElementById("transcript-section");
const transcript = document.getElementById("transcript");
const cardsSection = document.getElementById("cards-section");
const outlineSection = document.getElementById("outline-section");
const outline = document.getElementById("outline");
const root = document.getElementById("cards");
const status = document.getElementById("cards-status");
const finalTab = document.getElementById("final-cards-tab");
const liveTab = document.getElementById("live-cards-tab");
const tabs = [finalTab, liveTab];
const versions = new Map();
let transcriptVersion = "";
let liveCards = INITIAL_CARDS || [];
let finalCards = INITIAL_FINAL_CARDS || [];
let finalReady = finalCards.length > 0;
let activeView = "live";

function cardElement(card) {
  const article = document.createElement("article");
  article.id = `card-${card.id}`;
  article.className = `card ${card.status === "error" ? "error" : ""}`;
  article.tabIndex = -1;
  if (card.topic_id) article.dataset.topicId = card.topic_id;

  const header = document.createElement("header");
  const title = document.createElement("h2");
  title.textContent = card.title;
  header.append(title);

  const content = document.createElement("div");
  content.className = "card-content";
  content.innerHTML = card.html;

  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "元の文字起こし";
  const source = document.createElement("pre");
  source.textContent = card.source_text;
  details.append(summary, source);
  article.append(header, content, details);
  return article;
}

function outlineList(topics, parentId) {
  const list = document.createElement("ol");
  for (const topic of topics.filter(item => item.parent_id === parentId)) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = topic.title;
    button.addEventListener("click", () => showTopic(topic.id));
    const summary = document.createElement("p");
    summary.className = "outline-summary";
    summary.textContent = topic.summary;
    item.append(button, summary);
    const children = topics.filter(child => child.parent_id === topic.id);
    if (children.length) item.append(outlineList(topics, topic.id));
    list.append(item);
  }
  return list;
}

function renderOutline() {
  outline.textContent = "";
  outlineSection.hidden = !INITIAL_TOPICS.length;
  if (INITIAL_TOPICS.length) outline.append(outlineList(INITIAL_TOPICS, null));
}

function showTopic(topicId) {
  let view = "live";
  if (finalCards.some(card => card.topic_id === topicId)) view = "final";
  selectCardView(view, true);
  requestAnimationFrame(() => {
    const card = root.querySelector(`[data-topic-id="${CSS.escape(topicId)}"]`);
    if (!card) return;
    card.scrollIntoView({behavior: "smooth", block: "start"});
    card.focus({preventScroll: true});
  });
}

function render(cards, reset = false) {
  const atBottom = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 80;
  if (reset) {
    root.textContent = "";
    versions.clear();
  }
  const currentIds = new Set(cards.map(card => card.id));
  for (const existing of root.querySelectorAll(":scope > .card")) {
    const cardId = existing.id.replace(/^card-/, "");
    if (currentIds.has(cardId)) continue;
    existing.remove();
    versions.delete(cardId);
  }
  if (!cards.length && !root.querySelector(".empty")) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "カードを待っています。";
    root.append(empty);
  }
  if (cards.length && root.querySelector(".empty")) root.textContent = "";
  for (const card of cards) {
    const version = JSON.stringify(card);
    if (versions.get(card.id) === version) continue;
    const element = cardElement(card);
    const existing = document.getElementById(`card-${card.id}`);
    if (existing) existing.replaceWith(element);
    else root.append(element);
    versions.set(card.id, version);
  }
  if (atBottom) requestAnimationFrame(() => window.scrollTo({top: document.documentElement.scrollHeight, behavior: "smooth"}));
}

function selectCardView(view, announce = false) {
  if (view === "final" && !finalReady) return;
  activeView = view;
  for (const tab of tabs) {
    const selected = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  const selectedTab = view === "final" ? finalTab : liveTab;
  root.setAttribute("aria-labelledby", selectedTab.id);
  render(view === "final" ? finalCards : liveCards, true);
  const label = view === "final" ? "品質版" : "速報版";
  status.textContent = announce ? `${label}に切り替えました。` : `${label}を表示しています。`;
}

finalTab.dataset.view = "final";
liveTab.dataset.view = "live";
for (const tab of tabs) {
  tab.addEventListener("click", () => selectCardView(tab.dataset.view, true));
  tab.addEventListener("keydown", event => {
    const enabledTabs = tabs.filter(item => !item.disabled);
    const current = enabledTabs.indexOf(tab);
    let next = null;
    if (event.key === "ArrowLeft") next = (current - 1 + enabledTabs.length) % enabledTabs.length;
    if (event.key === "ArrowRight") next = (current + 1) % enabledTabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = enabledTabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    enabledTabs[next].focus();
    selectCardView(enabledTabs[next].dataset.view, true);
  });
}

async function refreshTranscript() {
  try {
    const response = await fetch("/transcript", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const text = await response.text();
    if (text === transcriptVersion) return;
    transcriptVersion = text;
    transcript.textContent = text || "最初の文字起こしを待っています。";
  } catch (transcriptFetchError) {
    if (!(transcriptFetchError instanceof Error)) throw transcriptFetchError;
    console.warn("文字起こしを取得できません", transcriptFetchError);
  }
}

async function refreshCards() {
  try {
    const response = await fetch("/cards", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    liveCards = await response.json();
    if (activeView === "live") render(liveCards);
  } catch (liveCardsFetchError) {
    if (!(liveCardsFetchError instanceof Error)) throw liveCardsFetchError;
    console.warn("カードを取得できません", liveCardsFetchError);
  }
}

async function refreshFinalCards() {
  try {
    const response = await fetch("/cards-final", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const cards = await response.json();
    if (!cards.length) return;
    finalCards = cards;
    finalTab.disabled = false;
    finalTab.setAttribute("aria-disabled", "false");
    if (!finalReady && cards.length) {
      finalReady = true;
      selectCardView("final", true);
    } else if (activeView === "final") render(finalCards, true);
  } catch (finalCardsFetchError) {
    if (!(finalCardsFetchError instanceof Error)) throw finalCardsFetchError;
    console.warn("品質版カードを取得できません", finalCardsFetchError);
  }
}

if (TRANSCRIPT_ENABLED) {
  refreshTranscript();
  setInterval(refreshTranscript, 2000);
} else transcriptSection.hidden = true;

if (CARDS_ENABLED && INITIAL_CARDS === null) {
  refreshCards();
  setInterval(refreshCards, 2000);
  refreshFinalCards();
  setInterval(refreshFinalCards, 2000);
} else if (CARDS_ENABLED) {
  if (finalReady) {
    finalTab.disabled = false;
    finalTab.setAttribute("aria-disabled", "false");
    selectCardView("final");
  } else selectCardView("live");
} else cardsSection.hidden = true;
renderOutline();
</script>
</body>
</html>
"""


def viewer_page(
    cards: list[Card] | None = None,
    *,
    final_cards: list[Card] | None = None,
    topics: list[Topic] | None = None,
    cards_enabled: bool = True,
    transcript_enabled: bool = True,
) -> str:
    replacements = {
        "const CARDS_ENABLED = true;": (
            f"const CARDS_ENABLED = {str(cards_enabled).lower()};"
        ),
        "const TRANSCRIPT_ENABLED = true;": (
            f"const TRANSCRIPT_ENABLED = {str(transcript_enabled).lower()};"
        ),
    }
    for name, initial in (
        ("INITIAL_CARDS", cards),
        ("INITIAL_FINAL_CARDS", final_cards),
        ("INITIAL_TOPICS", topics),
    ):
        if initial is None:
            continue
        serialized = json.dumps(
            [asdict(card) for card in initial],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        serialized = (
            serialized.replace("<", "\\u003c")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        marker = f"const {name} = null;"
        if name == "INITIAL_TOPICS":
            marker = "const INITIAL_TOPICS = [];"
        replacements[marker] = f"const {name} = {serialized};"
    markers = re.compile("|".join(re.escape(marker) for marker in replacements))
    return markers.sub(
        lambda match: replacements[match.group(0)],
        VIEWER_HTML,
    )


def export_cards(
    cards: list[Card],
    output_path: Path,
    *,
    final_cards: list[Card] | None = None,
    topics: list[Topic] | None = None,
) -> Path:
    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    try:
        temporary.write_text(
            viewer_page(
                cards,
                final_cards=final_cards,
                topics=topics,
                transcript_enabled=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, output_path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return output_path


def final_cards_path(cards_path: Path) -> Path:
    return cards_path.with_name(f"{cards_path.stem}-final.json")


class ViewerServer:
    def __init__(
        self,
        transcript_path: Path,
        cards_path: Path | None = None,
        port: int = 8765,
    ) -> None:
        self.transcript_path = transcript_path
        self.cards_path = cards_path
        self.final_cards_path = (
            final_cards_path(cards_path) if cards_path is not None else None
        )
        self.final_cards_served = threading.Event()
        self.thread: threading.Thread | None = None
        transcript_file = self.transcript_path
        cards_file = self.cards_path
        final_cards_file = self.final_cards_path
        final_cards_served = self.final_cards_served

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/":
                    page = viewer_page(cards_enabled=cards_file is not None)
                    self._send(200, "text/html; charset=utf-8", page.encode())
                    return
                if path == "/transcript":
                    try:
                        body = transcript_file.read_bytes()
                    except FileNotFoundError:
                        body = b""
                    self._send(200, "text/plain; charset=utf-8", body)
                    return
                if path == "/cards":
                    if cards_file is None:
                        body = b"[]"
                    else:
                        try:
                            body = cards_file.read_bytes()
                        except FileNotFoundError:
                            body = b"[]"
                    self._send(200, "application/json; charset=utf-8", body)
                    return
                if path == "/cards-final":
                    if final_cards_file is None:
                        body = b"[]"
                    else:
                        try:
                            body = final_cards_file.read_bytes()
                        except FileNotFoundError:
                            body = b"[]"
                    if final_cards_file is not None and final_cards_file.exists():
                        final_cards_served.set()
                    self._send(200, "application/json; charset=utf-8", body)
                    return
                self._send(404, "text/plain; charset=utf-8", "Not found".encode())

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread is None:
            self.server.server_close()
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.thread = None

    def wait_for_final_cards(self, timeout: float = 3) -> bool:
        return self.final_cards_served.wait(timeout)
