"use strict";

const state = {
  sessions: [],
  models: [],
  currentId: null,
  status: {active_session_id: null, state: "idle"},
  ws: null,
  recorder: null,
};

const elements = {
  app: document.getElementById("app"),
  menuButton: document.getElementById("menu-button"),
  sidebarClose: document.getElementById("sidebar-close"),
  sidebarOverlay: document.getElementById("sidebar-overlay"),
  newSession: document.getElementById("new-session"),
  sessionList: document.getElementById("session-list"),
  sessionTitle: document.getElementById("session-title"),
  sessionTitleButton: document.getElementById("session-title-button"),
  sessionTitleInput: document.getElementById("session-title-input"),
  sessionMeta: document.getElementById("session-meta"),
  modelSelect: document.getElementById("model-select"),
  statusBadge: document.getElementById("status-badge"),
  recordButton: document.getElementById("record-button"),
  toast: document.getElementById("toast"),
  levelRow: document.getElementById("level-row"),
  levelTrack: document.querySelector(".level-track"),
  levelFill: document.getElementById("level-fill"),
  audioPlayer: document.getElementById("audio-player"),
  transcriptScroll: document.getElementById("transcript-scroll"),
  transcriptContent: document.getElementById("transcript-content"),
  liveTranscriptButton: document.getElementById("live-transcript-button"),
  finalTranscriptButton: document.getElementById("final-transcript-button"),
  cards: document.getElementById("cards"),
  cardsStatus: document.getElementById("cards-status"),
  liveCardsTab: document.getElementById("live-cards-tab"),
  finalCardsTab: document.getElementById("final-cards-tab"),
};

let currentDetail = null;
let selectionVersion = 0;
let startRequested = false;
let transcriptMode = "live";
let transcriptItems = [];
let partialText = "";
let liveCards = [];
let finalCards = [];
let finalReady = false;
let activeCardView = "live";
let toastTimer = null;
let headerRenameActive = false;
let reconnectAttempt = 0;
let reconnectTimer = null;
let recordingInterrupted = false;
const cardVersions = new Map();
const cardTabs = [elements.liveCardsTab, elements.finalCardsTab];

class Recorder {
  constructor() {
    this.stream = null;
    this.context = null;
    this.source = null;
    this.node = null;
    this.sink = null;
  }

  async start() {
    const constraints = {
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: 1,
      },
    };
    try {
      this.stream = await navigator.mediaDevices.getUserMedia(constraints);
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) throw new Error("このブラウザは音声録音に対応していません。");
      try {
        this.context = new AudioContextClass({sampleRate: 16000});
      } catch (_error) {
        this.context = new AudioContextClass();
      }
      await this.context.audioWorklet.addModule("/static/recorder-worklet.js");
      this.source = this.context.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.context, "recorder-processor", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {
          targetSampleRate: 16000,
          sourceSampleRate: this.context.sampleRate,
        },
      });
      this.sink = this.context.createGain();
      this.sink.gain.value = 0;
      this.node.port.onmessage = event => {
        if (event.data instanceof ArrayBuffer) {
          if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send(event.data);
          }
          return;
        }
        if (typeof event.data?.level === "number") updateLevel(event.data.level);
      };
      this.source.connect(this.node);
      this.node.connect(this.sink).connect(this.context.destination);
      if (this.context.state === "suspended") await this.context.resume();
    } catch (error) {
      await this.stop();
      throw error;
    }
  }

  async stop() {
    for (const track of this.stream?.getTracks() || []) track.stop();
    this.node?.disconnect();
    this.source?.disconnect();
    this.sink?.disconnect();
    if (this.node) this.node.port.onmessage = null;
    if (this.context && this.context.state !== "closed") {
      await this.context.close();
    }
    this.stream = null;
    this.context = null;
    this.source = null;
    this.node = null;
    this.sink = null;
    updateLevel(0);
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      ...(options.body ? {"Content-Type": "application/json"} : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let message = `通信に失敗しました（${response.status}）。`;
    try {
      const payload = await response.json();
      if (payload.detail) message = payload.detail;
    } catch (_error) {
      // HTTP status is enough when the response isn't JSON.
    }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

async function initialize() {
  bindControls();
  try {
    const [sessionsPayload, statusPayload, configPayload] = await Promise.all([
      request("/api/sessions"),
      request("/api/status"),
      request("/api/config"),
    ]);
    state.sessions = sortSessions(sessionsPayload.sessions);
    state.status = statusPayload;
    state.models = configPayload.models;
    renderSidebar();
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await createSession();
  } catch (error) {
    showToast(error.message);
  }
}

function bindControls() {
  elements.newSession.addEventListener("click", createSession);
  elements.recordButton.addEventListener("click", handleRecordButton);
  elements.modelSelect.addEventListener("change", changeModel);
  elements.sessionTitleButton.addEventListener("click", beginHeaderRename);
  elements.sessionTitleInput.addEventListener("blur", finishHeaderRename);
  elements.sessionTitleInput.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      elements.sessionTitleInput.blur();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelHeaderRename();
      elements.sessionTitleButton.focus();
    }
  });
  elements.menuButton.addEventListener("click", () => setSidebarOpen(true));
  elements.sidebarClose.addEventListener("click", () => setSidebarOpen(false));
  elements.sidebarOverlay.addEventListener("click", () => setSidebarOpen(false));
  elements.liveTranscriptButton.addEventListener("click", () => setTranscriptMode("live"));
  elements.finalTranscriptButton.addEventListener("click", () => setTranscriptMode("final"));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      const openMenuButton = document.querySelector('.session-menu-button[aria-expanded="true"]');
      closeSessionMenus();
      openMenuButton?.focus();
      setSidebarOpen(false);
    }
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".session-menu-wrap")) closeSessionMenus();
  });
  for (const tab of cardTabs) {
    tab.addEventListener("click", () => selectCardView(tab.dataset.view, true));
    tab.addEventListener("keydown", event => {
      const enabledTabs = cardTabs.filter(item => !item.disabled);
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
}

async function createSession() {
  elements.newSession.disabled = true;
  try {
    const session = await request("/api/sessions", {method: "POST"});
    upsertSession(session);
    await selectSession(session.id);
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.newSession.disabled = false;
  }
}

async function selectSession(id) {
  const version = ++selectionVersion;
  if (state.recorder) await stopLocalRecorder();
  closeSocket(true);
  cancelHeaderRename();
  recordingInterrupted = false;
  state.currentId = id;
  currentDetail = null;
  liveCards = [];
  finalCards = [];
  finalReady = false;
  updateCardTabs();
  selectCardView("live");
  renderSidebar();
  renderHeader();
  renderTranscript();
  setSidebarOpen(false);
  try {
    const detail = await request(`/api/sessions/${encodeURIComponent(id)}`);
    if (version !== selectionVersion) return;
    applySessionDetail(detail);
    openSocket(id);
  } catch (error) {
    if (version === selectionVersion) showToast(error.message);
  }
}

function openSocket(id, reconnecting = false) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/api/sessions/${encodeURIComponent(id)}/ws`);
  socket.intentionalClose = false;
  socket.reconnecting = reconnecting;
  state.ws = socket;
  socket.addEventListener("open", async () => {
    if (state.ws !== socket) return;
    reconnectAttempt = 0;
    if (!socket.reconnecting) return;
    const interrupted = recordingInterrupted;
    try {
      await resyncCurrentSession(id);
      if (!interrupted) showToast("サーバーへ再接続しました。");
    } catch (error) {
      showToast(error.message);
    }
  });
  socket.addEventListener("message", async event => {
    if (state.ws !== socket) return;
    try {
      await handleServerEvent(JSON.parse(event.data));
    } catch (_error) {
      showToast("サーバーから不正なメッセージを受信しました。");
    }
  });
  socket.addEventListener("close", async () => {
    if (state.ws !== socket) return;
    const wasRecording = Boolean(state.recorder);
    state.ws = null;
    startRequested = false;
    await stopLocalRecorder();
    if (wasRecording) {
      recordingInterrupted = true;
      state.status = {active_session_id: null, state: "idle"};
      if (currentDetail) {
        currentDetail.session.state = "stopped";
        upsertSession(currentDetail.session);
      }
      showToast("録音が中断されました。サーバーで停止処理を続けています。");
    } else if (!socket.intentionalClose) {
      showToast("接続が切れました。再接続します。");
    }
    renderHeader();
    if (!socket.intentionalClose) scheduleReconnect(id);
  });
}

function closeSocket(intentional = false) {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  reconnectAttempt = 0;
  if (!state.ws) return;
  state.ws.intentionalClose = intentional;
  state.ws.close();
  state.ws = null;
  startRequested = false;
}

function scheduleReconnect(id) {
  if (state.currentId !== id || reconnectTimer !== null) return;
  const delay = Math.min(1_000 * (2 ** reconnectAttempt), 15_000);
  reconnectAttempt += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (state.currentId === id && state.ws === null) openSocket(id, true);
  }, delay);
}

async function resyncCurrentSession(id) {
  const detail = await request(`/api/sessions/${encodeURIComponent(id)}`);
  if (state.currentId !== id) return;
  if (recordingInterrupted && detail.session.state === "recording") {
    detail.session.state = "stopped";
  }
  applySessionDetail(detail);
}

function applySessionDetail(detail) {
  currentDetail = detail;
  upsertSession(detail.session);
  transcriptItems = detail.transcript.segments.map(text => ({seq: null, text, pending: false}));
  partialText = "";
  liveCards = detail.cards;
  finalCards = detail.final_cards;
  finalReady = finalCards.length > 0 || detail.session.finalized;
  transcriptMode = "live";
  renderAll();
  updateCardTabs();
  selectCardView(finalReady ? "final" : "live");
}

async function handleServerEvent(event) {
  switch (event.type) {
    case "status":
      if (recordingInterrupted && event.active_session_id === state.currentId && event.state === "recording") {
        state.status = {active_session_id: null, state: "idle"};
      } else {
        state.status = event;
      }
      if (event.state === "idle") recordingInterrupted = false;
      if (event.state === "recording" && event.active_session_id === state.currentId && startRequested && !state.recorder) {
        await startLocalRecorder();
      }
      if (["stopping", "finalizing", "idle"].includes(event.state)) {
        await stopLocalRecorder();
        if (event.state === "idle") startRequested = false;
      }
      renderAll();
      break;
    case "session":
      if (recordingInterrupted && event.session.state === "recording") {
        event.session.state = "stopped";
      }
      upsertSession(event.session);
      if (currentDetail && event.session.id === state.currentId) {
        currentDetail.session = event.session;
      }
      renderAll();
      break;
    case "partial":
      updatePartial(event.text);
      break;
    case "committed":
      appendCommitted(event.seq, event.text);
      break;
    case "corrected":
      applyCorrection(event.seq, event.text);
      break;
    case "final_transcript":
      if (currentDetail) currentDetail.final_transcript = event.text;
      elements.finalTranscriptButton.disabled = false;
      if (transcriptMode === "final") renderTranscript();
      break;
    case "cards":
      liveCards = event.cards;
      if (activeCardView === "live") renderCards(liveCards);
      break;
    case "cards_final": {
      const firstFinal = !finalReady;
      finalCards = event.cards;
      finalReady = true;
      updateCardTabs();
      if (firstFinal) selectCardView("final", true);
      else if (activeCardView === "final") renderCards(finalCards);
      break;
    }
    case "error":
      showToast(event.message);
      startRequested = false;
      if (event.fatal) await stopLocalRecorder();
      renderHeader();
      break;
    default:
      break;
  }
}

async function handleRecordButton() {
  const action = elements.recordButton.dataset.action;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    showToast("録音サーバーへ接続できていません。");
    return;
  }
  if (action === "start") {
    startRequested = true;
    elements.recordButton.disabled = true;
    elements.recordButton.textContent = "開始中…";
    state.ws.send(JSON.stringify({type: "start", sample_rate: 16000}));
  } else if (action === "stop") {
    await stopLocalRecorder();
    state.status = {active_session_id: state.currentId, state: "stopping"};
    renderHeader();
    state.ws.send(JSON.stringify({type: "stop"}));
  }
}

async function startLocalRecorder() {
  const recorder = new Recorder();
  state.recorder = recorder;
  try {
    await recorder.start();
    startRequested = false;
    renderHeader();
  } catch (_error) {
    state.recorder = null;
    startRequested = false;
    showToast("マイクを利用できません。ブラウザのマイク権限を確認してください。");
    if (state.ws?.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({type: "stop"}));
    }
    renderHeader();
  }
}

async function stopLocalRecorder() {
  if (!state.recorder) return;
  const recorder = state.recorder;
  state.recorder = null;
  await recorder.stop();
  updateLevel(0);
}

function renderAll() {
  renderSidebar();
  renderHeader();
  renderTranscript();
  renderAudio();
}

function renderSidebar() {
  elements.sessionList.textContent = "";
  if (!state.sessions.length) {
    const empty = document.createElement("p");
    empty.className = "sidebar-empty";
    empty.textContent = "まだセッションがありません。";
    elements.sessionList.append(empty);
    return;
  }
  const groups = new Map();
  for (const session of sortSessions(state.sessions)) {
    const label = dateGroupLabel(session.created_at);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(session);
  }
  for (const [label, sessions] of groups) {
    const section = document.createElement("section");
    section.className = "session-group";
    const heading = document.createElement("h2");
    heading.className = "session-group-title";
    heading.textContent = label;
    section.append(heading);
    for (const session of sessions) section.append(sessionButton(session));
    elements.sessionList.append(section);
  }
}

function sessionButton(session) {
  const item = document.createElement("div");
  item.className = `session-item ${session.id === state.currentId ? "active" : ""}`;
  const select = document.createElement("button");
  select.type = "button";
  select.className = "session-select";
  select.setAttribute("aria-current", session.id === state.currentId ? "page" : "false");
  const title = document.createElement("span");
  title.className = "session-item-title";
  title.textContent = session.title || stemTitle(session.id);
  select.append(title);
  if (session.duration_seconds !== null) {
    const duration = document.createElement("span");
    duration.className = "session-duration";
    duration.textContent = formatDuration(session.duration_seconds);
    select.append(duration);
  }
  if (session.state === "recording") {
    const dot = document.createElement("span");
    dot.className = "recording-dot";
    dot.setAttribute("aria-label", "録音中");
    select.append(dot);
  }
  select.addEventListener("click", () => selectSession(session.id));

  const menuWrap = document.createElement("div");
  menuWrap.className = "session-menu-wrap";
  const menuButton = document.createElement("button");
  menuButton.type = "button";
  menuButton.className = "session-menu-button";
  menuButton.setAttribute("aria-label", `${title.textContent}のメニュー`);
  menuButton.setAttribute("aria-haspopup", "menu");
  menuButton.setAttribute("aria-expanded", "false");
  menuButton.textContent = "⋯";
  const menu = document.createElement("div");
  menu.className = "session-menu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;

  const rename = document.createElement("button");
  rename.type = "button";
  rename.className = "session-menu-action";
  rename.setAttribute("role", "menuitem");
  rename.textContent = "名前を変更";
  rename.addEventListener("click", () => beginSidebarRename(session, item));

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "session-menu-action danger-text";
  remove.setAttribute("role", "menuitem");
  remove.textContent = "削除";
  remove.disabled = session.state === "recording" || state.status.active_session_id === session.id;
  if (remove.disabled) remove.title = "録音中のセッションは削除できません";
  remove.addEventListener("click", () => deleteSession(session));

  const exportLink = document.createElement("a");
  exportLink.className = "session-menu-action";
  exportLink.setAttribute("role", "menuitem");
  exportLink.href = `/api/sessions/${encodeURIComponent(session.id)}/export`;
  exportLink.download = `${session.id}.html`;
  exportLink.textContent = "HTMLエクスポート";

  menu.append(rename, remove, exportLink);
  menuButton.addEventListener("click", () => {
    const opening = menu.hidden;
    closeSessionMenus();
    menu.hidden = !opening;
    menuButton.setAttribute("aria-expanded", String(opening));
    if (opening) menu.querySelector("[role=menuitem]:not(:disabled)")?.focus();
  });
  menuWrap.append(menuButton, menu);
  item.append(select, menuWrap);
  return item;
}

function closeSessionMenus() {
  for (const menu of document.querySelectorAll(".session-menu")) menu.hidden = true;
  for (const button of document.querySelectorAll(".session-menu-button")) {
    button.setAttribute("aria-expanded", "false");
  }
}

function beginSidebarRename(session, item) {
  closeSessionMenus();
  const input = document.createElement("input");
  input.className = "session-item-input";
  input.type = "text";
  input.value = session.title || "";
  input.placeholder = stemTitle(session.id);
  input.setAttribute("aria-label", "セッション名");
  item.replaceChildren(input);
  let finished = false;
  const finish = async save => {
    if (finished) return;
    finished = true;
    const title = input.value.trim();
    if (save && title) {
      try {
        await renameSession(session.id, title);
        return;
      } catch (error) {
        showToast(error.message);
      }
    } else if (save) {
      showToast("タイトルを入力してください。");
    }
    renderSidebar();
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      input.blur();
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.focus();
  input.select();
}

async function renameSession(id, title) {
  const session = await request(`/api/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({title}),
  });
  upsertSession(session);
  if (currentDetail && id === state.currentId) currentDetail.session = session;
  renderHeader();
  return session;
}

async function changeModel() {
  const session = currentDetail?.session;
  const model = state.models.find(item => modelKey(item) === elements.modelSelect.value);
  if (!session || session.has_audio || !model) return;
  try {
    const updated = await request(`/api/sessions/${encodeURIComponent(session.id)}/model`, {
      method: "PATCH",
      body: JSON.stringify({provider: model.provider, model: model.model}),
    });
    currentDetail.session = updated;
    upsertSession(updated);
    renderHeader();
  } catch (error) {
    showToast(error.message);
    renderHeader();
  }
}

async function deleteSession(session) {
  closeSessionMenus();
  const title = session.title || stemTitle(session.id);
  if (!confirm(`「${title}」を削除しますか？`)) return;
  try {
    await request(`/api/sessions/${encodeURIComponent(session.id)}`, {method: "DELETE"});
    state.sessions = state.sessions.filter(item => item.id !== session.id);
    if (state.currentId !== session.id) {
      renderSidebar();
      return;
    }
    closeSocket(true);
    cancelHeaderRename();
    state.currentId = null;
    currentDetail = null;
    renderAll();
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await createSession();
  } catch (error) {
    showToast(error.message);
  }
}

function beginHeaderRename() {
  const session = currentDetail?.session;
  if (!session || headerRenameActive) return;
  headerRenameActive = true;
  elements.sessionTitleButton.hidden = true;
  elements.sessionTitleInput.hidden = false;
  elements.sessionTitleInput.value = session.title || "";
  elements.sessionTitleInput.placeholder = stemTitle(session.id);
  elements.sessionTitleInput.focus();
  elements.sessionTitleInput.select();
}

async function finishHeaderRename() {
  if (!headerRenameActive) return;
  const id = state.currentId;
  const title = elements.sessionTitleInput.value.trim();
  cancelHeaderRename();
  if (!id || !title) {
    if (!title) showToast("タイトルを入力してください。");
    return;
  }
  try {
    await renameSession(id, title);
  } catch (error) {
    showToast(error.message);
  }
}

function cancelHeaderRename() {
  headerRenameActive = false;
  elements.sessionTitleInput.hidden = true;
  elements.sessionTitleButton.hidden = false;
}

function renderHeader() {
  const session = currentDetail?.session || state.sessions.find(item => item.id === state.currentId);
  if (!session) {
    elements.sessionTitle.textContent = "セッションを選択";
    elements.sessionTitleButton.disabled = true;
    elements.sessionMeta.textContent = "履歴からセッションを選んでください。";
    elements.recordButton.disabled = true;
    elements.recordButton.dataset.action = "";
    renderModelSelector(null, false, false);
    return;
  }

  elements.sessionTitle.textContent = session.title || stemTitle(session.id);
  elements.sessionTitleButton.disabled = false;
  elements.sessionMeta.textContent = `${formatDateTime(session.created_at)}${session.duration_seconds === null ? "" : ` · ${formatDuration(session.duration_seconds)}`}`;
  const activeId = state.status.active_session_id;
  const activeHere = activeId === session.id;
  const finalizing = activeHere && ["stopping", "finalizing"].includes(state.status.state);
  const recording = activeHere && state.status.state === "recording";
  renderModelSelector(session, recording, finalizing);

  elements.statusBadge.className = "status-badge stopped";
  elements.statusBadge.textContent = "停止";
  if (recording) {
    elements.statusBadge.className = "status-badge recording";
    elements.statusBadge.textContent = "録音中";
  } else if (finalizing) {
    elements.statusBadge.className = "status-badge finalizing";
    elements.statusBadge.textContent = "最終処理中";
  }

  const button = elements.recordButton;
  button.className = "record-button";
  button.title = "";
  button.disabled = false;
  button.dataset.action = "start";
  if (finalizing) {
    button.classList.add("finalizing");
    button.textContent = "最終処理中…";
    button.disabled = true;
    button.dataset.action = "";
  } else if (recording) {
    button.classList.add("danger");
    button.textContent = "停止";
    button.dataset.action = "stop";
  } else if (activeId && activeId !== session.id) {
    button.textContent = "別のセッションを録音中です";
    button.disabled = true;
    button.dataset.action = "";
  } else if (!session.has_audio) {
    button.textContent = "録音開始";
  } else if (session.resumable) {
    button.textContent = "録音を再開";
  } else {
    button.textContent = "録音を再開";
    button.disabled = true;
    button.dataset.action = "";
    button.title = "録音ファイルが削除済みのため再開できません";
  }
  elements.levelRow.hidden = !recording;
}

function modelKey(model) {
  return `${model.provider}:${model.model}`;
}

function renderModelSelector(session, recording, finalizing) {
  const select = elements.modelSelect;
  if (select.options.length !== state.models.length) {
    select.replaceChildren(...state.models.map(model => {
      const option = document.createElement("option");
      option.value = modelKey(model);
      option.textContent = model.label;
      return option;
    }));
  }
  if (!session || !state.models.length) {
    select.disabled = true;
    return;
  }
  const value = modelKey(session);
  if ([...select.options].some(option => option.value === value)) select.value = value;
  select.disabled = session.has_audio || recording || finalizing || startRequested;
}

function renderAudio() {
  const session = currentDetail?.session;
  const available = Boolean(session?.has_audio && session.resumable);
  elements.audioPlayer.hidden = !available;
  if (!available) {
    elements.audioPlayer.removeAttribute("src");
    return;
  }
  const source = `/api/sessions/${encodeURIComponent(session.id)}/audio`;
  if (elements.audioPlayer.getAttribute("src") !== source) {
    elements.audioPlayer.src = source;
  }
}

function renderTranscript() {
  const atBottom = nearBottom(elements.transcriptScroll);
  elements.transcriptContent.textContent = "";
  if (!currentDetail) {
    appendEmpty(elements.transcriptContent, "セッションを選択してください。");
    return;
  }
  if (transcriptMode === "final") {
    appendParagraphs(splitParagraphs(currentDetail.final_transcript || ""));
  } else {
    for (const item of transcriptItems) appendTranscriptItem(item);
    if (partialText) appendPartial(partialText);
  }
  if (!elements.transcriptContent.children.length) {
    appendEmpty(elements.transcriptContent, "文字起こしを待っています。");
  }
  elements.finalTranscriptButton.disabled = !currentDetail.final_transcript;
  elements.liveTranscriptButton.classList.toggle("active", transcriptMode === "live");
  elements.finalTranscriptButton.classList.toggle("active", transcriptMode === "final");
  elements.liveTranscriptButton.setAttribute("aria-pressed", String(transcriptMode === "live"));
  elements.finalTranscriptButton.setAttribute("aria-pressed", String(transcriptMode === "final"));
  scrollToBottomIfNeeded(elements.transcriptScroll, atBottom);
}

function appendParagraphs(paragraphs) {
  for (const text of paragraphs) appendTranscriptItem({seq: null, text, pending: false});
}

function appendTranscriptItem(item) {
  const paragraph = document.createElement("p");
  paragraph.textContent = item.text;
  if (item.seq !== null) paragraph.dataset.seq = String(item.seq);
  if (item.pending) paragraph.classList.add("pending");
  elements.transcriptContent.append(paragraph);
}

function appendPartial(text) {
  const paragraph = document.createElement("p");
  paragraph.className = "partial";
  paragraph.textContent = text;
  elements.transcriptContent.append(paragraph);
}

function appendCommitted(seq, text) {
  const atBottom = nearBottom(elements.transcriptScroll);
  transcriptItems.push({seq, text, pending: true});
  partialText = "";
  if (transcriptMode === "live") {
    elements.transcriptContent.querySelector(".empty")?.remove();
    elements.transcriptContent.querySelector(".partial")?.remove();
    appendTranscriptItem({seq, text, pending: true});
    scrollToBottomIfNeeded(elements.transcriptScroll, atBottom);
  }
}

function applyCorrection(seq, text) {
  const item = transcriptItems.find(value => value.seq === seq);
  if (item) {
    item.text = text;
    item.pending = false;
  } else {
    transcriptItems.push({seq, text, pending: false});
  }
  if (transcriptMode !== "live") return;
  const paragraph = elements.transcriptContent.querySelector(`[data-seq="${seq}"]`);
  if (paragraph) {
    paragraph.textContent = text;
    paragraph.classList.remove("pending");
  } else {
    appendTranscriptItem({seq, text, pending: false});
  }
}

function updatePartial(text) {
  partialText = text;
  if (transcriptMode !== "live") return;
  const atBottom = nearBottom(elements.transcriptScroll);
  let partial = elements.transcriptContent.querySelector(".partial");
  if (!text) {
    partial?.remove();
    return;
  }
  elements.transcriptContent.querySelector(".empty")?.remove();
  if (!partial) {
    appendPartial(text);
    partial = elements.transcriptContent.querySelector(".partial");
  } else {
    partial.textContent = text;
  }
  scrollToBottomIfNeeded(elements.transcriptScroll, atBottom);
}

function setTranscriptMode(mode) {
  if (mode === "final" && !currentDetail?.final_transcript) return;
  transcriptMode = mode;
  renderTranscript();
}

function cardElement(card) {
  const article = document.createElement("article");
  article.id = `card-${card.id}`;
  article.className = `card ${card.status === "error" ? "error" : ""}`;

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

function renderCards(cards, reset = false) {
  const atBottom = nearBottom(elements.cards);
  if (reset) {
    elements.cards.textContent = "";
    cardVersions.clear();
  }
  if (!cards.length && !elements.cards.querySelector(".empty")) {
    appendEmpty(elements.cards, "カードを待っています。");
  }
  if (cards.length && elements.cards.querySelector(".empty")) elements.cards.textContent = "";
  for (const card of cards) {
    const version = JSON.stringify(card);
    if (cardVersions.get(card.id) === version) continue;
    const element = cardElement(card);
    const existing = document.getElementById(`card-${card.id}`);
    if (existing) existing.replaceWith(element);
    else elements.cards.append(element);
    cardVersions.set(card.id, version);
  }
  scrollToBottomIfNeeded(elements.cards, atBottom);
}

function selectCardView(view, announce = false) {
  if (view === "final" && !finalReady) return;
  activeCardView = view;
  for (const tab of cardTabs) {
    const selected = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  const selectedTab = view === "final" ? elements.finalCardsTab : elements.liveCardsTab;
  elements.cards.setAttribute("aria-labelledby", selectedTab.id);
  renderCards(view === "final" ? finalCards : liveCards, true);
  const label = view === "final" ? "品質版" : "速報版";
  elements.cardsStatus.textContent = announce ? `${label}に切り替えました。` : `${label}を表示しています。`;
}

function updateCardTabs() {
  elements.finalCardsTab.disabled = !finalReady;
  elements.finalCardsTab.setAttribute("aria-disabled", String(!finalReady));
}

function upsertSession(session) {
  const index = state.sessions.findIndex(item => item.id === session.id);
  if (index === -1) state.sessions.push(session);
  else state.sessions[index] = session;
  state.sessions = sortSessions(state.sessions);
  renderSidebar();
}

function sortSessions(sessions) {
  return [...sessions].sort((first, second) => {
    const timeDifference = new Date(second.created_at) - new Date(first.created_at);
    return timeDifference || second.id.localeCompare(first.id);
  });
}

function dateGroupLabel(value) {
  const date = new Date(value);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (sameDay(date, today)) return "今日";
  if (sameDay(date, yesterday)) return "昨日";
  if (date.getFullYear() === today.getFullYear()) {
    return `${date.getMonth() + 1}月${date.getDate()}日`;
  }
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
}

function sameDay(first, second) {
  return first.getFullYear() === second.getFullYear()
    && first.getMonth() === second.getMonth()
    && first.getDate() === second.getDate();
}

function stemTitle(id) {
  const match = id.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/);
  return match ? `${match[1]}/${match[2]}/${match[3]} ${match[4]}:${match[5]}` : id;
}

function formatDuration(value) {
  const seconds = Math.max(0, Math.floor(value));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatDateTime(value) {
  return new Intl.DateTimeFormat("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function splitParagraphs(text) {
  return text.split(/\n\s*\n/).map(value => value.trim()).filter(Boolean);
}

function appendEmpty(root, message) {
  const empty = document.createElement("p");
  empty.className = "empty";
  empty.textContent = message;
  root.append(empty);
}

function nearBottom(root) {
  return root.scrollTop + root.clientHeight >= root.scrollHeight - 80;
}

function scrollToBottomIfNeeded(root, shouldScroll) {
  if (shouldScroll) requestAnimationFrame(() => root.scrollTo({top: root.scrollHeight, behavior: "smooth"}));
}

function updateLevel(level) {
  const normalized = Math.max(0, Math.min(1, level));
  elements.levelFill.style.width = `${normalized * 100}%`;
  elements.levelTrack.setAttribute("aria-valuenow", normalized.toFixed(3));
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 5000);
}

function setSidebarOpen(open) {
  elements.app.classList.toggle("sidebar-open", open);
  elements.menuButton.setAttribute("aria-expanded", String(open));
}

initialize();
