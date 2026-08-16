"use strict";

const state = {
  sessions: [],
  models: [],
  defaultModel: {provider: "openai", model: "gpt-5.6-luna", label: "GPT-5.6 Luna"},
  deepseekPeakHoursUtc: [],
  deepseekPeak: false,
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
  overview: document.getElementById("overview"),
  outline: document.getElementById("outline"),
};

let currentDetail = null;
let selectionVersion = 0;
let startRequested = false;
let transcriptMode = "live";
let transcriptItems = [];
let partialText = "";
let liveCards = [];
let finalCards = [];
let evidenceSegments = [];
let outlineTopics = [];
let knowledgeUnits = [];
let finalReady = false;
let activeCardView = "live";
let activePrimaryView = "cards";
let toastTimer = null;
let headerRenameActive = false;
let reconnectAttempt = 0;
let reconnectTimer = null;
let recordingInterrupted = false;
let renderedTranscriptSignature = "";
let activeSessionId = "";
const cardVersions = new Map();
const finalizingSessionIds = new Set();
const cardTabs = [elements.liveCardsTab, elements.finalCardsTab];
const primaryTabs = [...document.querySelectorAll(".knowledge-tab")];
const primaryPanels = [...document.querySelectorAll(".knowledge-panel, .transcript-panel, .cards-panel")];

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
      } catch (audioContextOptionsError) {
        if (!(audioContextOptionsError instanceof TypeError || audioContextOptionsError instanceof DOMException)) {
          throw audioContextOptionsError;
        }
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
    } catch (recorderError) {
      if (!(recorderError instanceof Error)) throw recorderError;
      await this.stop();
      throw recorderError;
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
    } catch (responseJsonError) {
      if (!(responseJsonError instanceof SyntaxError)) throw responseJsonError;
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
    if (typeof statusPayload.active_session_id === "string") {
      activeSessionId = statusPayload.active_session_id;
    }
    state.models = configPayload.models;
    state.defaultModel = configPayload.default_model;
    state.deepseekPeakHoursUtc = configPayload.deepseek_peak_hours_utc;
    state.deepseekPeak = isDeepSeekPeakTime();
    renderSidebar();
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await createSession();
  } catch (initializationError) {
    if (!(initializationError instanceof Error)) throw initializationError;
    showToast(initializationError.message);
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
  for (const tab of primaryTabs) {
    tab.addEventListener("click", () => selectPrimaryView(tab.dataset.view));
    tab.addEventListener("keydown", event => {
      const current = primaryTabs.indexOf(tab);
      let next = -1;
      if (event.key === "ArrowLeft") next = (current - 1 + primaryTabs.length) % primaryTabs.length;
      if (event.key === "ArrowRight") next = (current + 1) % primaryTabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = primaryTabs.length - 1;
      if (next < 0) return;
      event.preventDefault();
      primaryTabs[next].focus();
      selectPrimaryView(primaryTabs[next].dataset.view);
    });
  }
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
  } catch (sessionCreationError) {
    if (!(sessionCreationError instanceof Error)) throw sessionCreationError;
    showToast(sessionCreationError.message);
  } finally {
    elements.newSession.disabled = false;
  }
}

async function selectSession(id) {
  if (state.recorder && state.currentId !== id) {
    if (!confirm("録音を停止して別のセッションへ移動しますか？")) return;
  }
  const version = ++selectionVersion;
  if (state.recorder) await stopLocalRecorder();
  closeSocket(true);
  cancelHeaderRename();
  recordingInterrupted = false;
  state.currentId = id;
  currentDetail = null;
  liveCards = [];
  finalCards = [];
  evidenceSegments = [];
  outlineTopics = [];
  knowledgeUnits = [];
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
  } catch (sessionSelectionError) {
    if (!(sessionSelectionError instanceof Error)) throw sessionSelectionError;
    if (version === selectionVersion) showToast(sessionSelectionError.message);
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
    } catch (resyncError) {
      if (!(resyncError instanceof Error)) throw resyncError;
      showToast(resyncError.message);
    }
  });
  socket.addEventListener("message", async event => {
    if (state.ws !== socket) return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (serverMessageError) {
      showToast("サーバーから不正なメッセージを受信しました。");
      return;
    }
    try {
      await handleServerEvent(payload);
    } catch (serverEventError) {
      console.error("サーバーメッセージの処理に失敗しました。", serverEventError);
    }
  });
  socket.addEventListener("close", async event => {
    if (state.ws !== socket) return;
    const serverShutdown = event.code === 1012 || event.code === 1001;
    const wasRecording = Boolean(state.recorder);
    state.ws = null;
    startRequested = false;
    await stopLocalRecorder();
    if (wasRecording) {
      recordingInterrupted = true;
      activeSessionId = "";
      state.status = {session_id: id, active_session_id: null, state: "idle"};
      if (currentDetail) {
        currentDetail.session.state = "stopped";
        upsertSession(currentDetail.session);
      }
      showToast("録音を保存しました。「再処理」で最終結果を生成できます。");
    } else if (!socket.intentionalClose && !serverShutdown) {
      showToast("接続が切れました。再接続します。");
    }
    renderHeader();
    if (!socket.intentionalClose && !serverShutdown) scheduleReconnect(id);
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
  evidenceSegments = detail.segments;
  outlineTopics = detail.outline;
  knowledgeUnits = detail.knowledge;
  finalReady = finalCards.length > 0 || detail.session.finalized;
  transcriptMode = "live";
  renderedTranscriptSignature = "";
  renderAll();
  updateCardTabs();
  selectCardView(finalReady ? "final" : "live");
}

async function handleServerEvent(event) {
  switch (event.type) {
    case "status": {
      let subjectId = "";
      if (typeof event.session_id === "string") subjectId = event.session_id;
      const finalizationCompleted = Boolean(
        subjectId
        && finalizingSessionIds.has(subjectId)
        && event.state !== "finalizing",
      );
      if (subjectId && event.state === "finalizing") {
        finalizingSessionIds.add(subjectId);
      } else if (finalizationCompleted) {
        finalizingSessionIds.delete(subjectId);
      }
      if (typeof event.active_session_id === "string") {
        activeSessionId = event.active_session_id;
      } else if (event.state === "idle" && subjectId === activeSessionId) {
        activeSessionId = "";
      }
      if (recordingInterrupted && event.active_session_id === state.currentId && event.state === "recording") {
        activeSessionId = "";
        state.status = {session_id: subjectId, active_session_id: null, state: "idle"};
      } else {
        state.status = event;
      }
      if (event.state === "idle" && subjectId === state.currentId) recordingInterrupted = false;
      if (event.state === "recording" && event.active_session_id === state.currentId && startRequested && !state.recorder) {
        await startLocalRecorder();
      }
      if (subjectId === state.currentId && ["stopping", "finalizing", "idle"].includes(event.state)) {
        await stopLocalRecorder();
        if (event.state === "idle") startRequested = false;
      }
      renderAll();
      if (finalizationCompleted && subjectId !== state.currentId) {
        const sessionsPayload = await request("/api/sessions");
        state.sessions = sortSessions(sessionsPayload.sessions);
        renderSidebar();
      }
      break;
    }
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
      if (currentDetail) {
        currentDetail.final_transcript = event.text;
        if (event.segments) currentDetail.segments = event.segments;
      }
      if (event.segments) evidenceSegments = event.segments;
      renderedTranscriptSignature = "";
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
      if (event.outline) {
        outlineTopics = event.outline;
        if (currentDetail) currentDetail.outline = event.outline;
      }
      if (event.knowledge) {
        knowledgeUnits = event.knowledge;
        if (currentDetail) currentDetail.knowledge = event.knowledge;
      }
      finalReady = true;
      renderOverview();
      renderOutline();
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
    activeSessionId = state.currentId;
    state.status = {session_id: state.currentId, active_session_id: state.currentId, state: "stopping"};
    renderHeader();
    state.ws.send(JSON.stringify({type: "stop"}));
  }
}

async function startLocalRecorder() {
  const recorder = new Recorder();
  state.recorder = recorder;
  try {
    await recorder.start();
    if (state.recorder !== recorder) {
      await recorder.stop();
      return;
    }
    startRequested = false;
    renderHeader();
  } catch (recorderStartError) {
    if (state.recorder !== recorder) {
      await recorder.stop();
      return;
    }
    if (!(recorderStartError instanceof Error)) throw recorderStartError;
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
  renderOverview();
  renderOutline();
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
  if (session.has_audio && !session.finalized && session.state !== "recording") {
    const dot = document.createElement("span");
    dot.className = "unfinalized-dot";
    dot.title = "最終処理が未実行";
    dot.setAttribute("aria-label", "最終処理が未実行");
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

  const reprocess = document.createElement("button");
  reprocess.type = "button";
  reprocess.className = "session-menu-action";
  reprocess.setAttribute("role", "menuitem");
  reprocess.textContent = "再処理";
  const finalizing = finalizingSessionIds.has(session.id);
  const stopping = state.status.session_id === session.id && state.status.state === "stopping";
  if (finalizing) reprocess.textContent = "再処理中…";
  reprocess.disabled = !session.has_audio || session.state === "recording" || activeSessionId === session.id || finalizing || stopping;
  if (reprocess.disabled) reprocess.title = "停止済みの録音を再処理できます";
  reprocess.addEventListener("click", () => reprocessSession(session, reprocess));

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "session-menu-action danger-text";
  remove.setAttribute("role", "menuitem");
  remove.textContent = "削除";
  remove.disabled = session.state === "recording" || activeSessionId === session.id || finalizing || stopping;
  if (remove.disabled) remove.title = "録音中のセッションは削除できません";
  remove.addEventListener("click", () => deleteSession(session));

  const exportLink = document.createElement("a");
  exportLink.className = "session-menu-action";
  exportLink.setAttribute("role", "menuitem");
  exportLink.href = `/api/sessions/${encodeURIComponent(session.id)}/export`;
  exportLink.download = `${session.id}.html`;
  exportLink.textContent = "HTMLエクスポート";

  menu.append(rename, reprocess, remove, exportLink);
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
      } catch (sidebarRenameError) {
        if (!(sidebarRenameError instanceof Error)) throw sidebarRenameError;
        showToast(sidebarRenameError.message);
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
  if (isDeepSeekPeakTime() && session.ai_provider === "deepseek") {
    renderHeader();
    return;
  }
  try {
    const updated = await request(`/api/sessions/${encodeURIComponent(session.id)}/model`, {
      method: "PATCH",
      body: JSON.stringify({provider: model.provider, model: model.model}),
    });
    currentDetail.session = updated;
    upsertSession(updated);
    renderHeader();
  } catch (modelChangeError) {
    if (!(modelChangeError instanceof Error)) throw modelChangeError;
    showToast(modelChangeError.message);
    renderHeader();
  }
}

async function deleteSession(session) {
  closeSessionMenus();
  const title = session.title || stemTitle(session.id);
  let message = `「${title}」を削除しますか？`;
  if (state.recorder && session.id === state.currentId) {
    message = `録音を停止して「${title}」を削除しますか？`;
  }
  if (!confirm(message)) return;
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
  } catch (sessionDeletionError) {
    if (!(sessionDeletionError instanceof Error)) throw sessionDeletionError;
    showToast(sessionDeletionError.message);
  }
}

async function reprocessSession(session, button) {
  button.disabled = true;
  button.textContent = "再処理中…";
  try {
    await request(`/api/sessions/${encodeURIComponent(session.id)}/finalize`, {
      method: "POST",
    });
    closeSessionMenus();
    showToast("再処理を開始しました。");
  } catch (sessionReprocessError) {
    if (!(sessionReprocessError instanceof Error)) throw sessionReprocessError;
    button.disabled = false;
    button.textContent = "再処理";
    showToast(sessionReprocessError.message);
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
  } catch (headerRenameError) {
    if (!(headerRenameError instanceof Error)) throw headerRenameError;
    showToast(headerRenameError.message);
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
  const activeId = activeSessionId;
  const activeHere = activeId === session.id;
  const statusHere = state.status.session_id === session.id;
  const finalizing = finalizingSessionIds.has(session.id) || (statusHere && state.status.state === "stopping");
  const recording = activeHere && !finalizing;
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

function isDeepSeekPeakTime() {
  const hour = new Date().getUTCHours();
  return state.deepseekPeakHoursUtc.some(([start, end]) => start <= hour && hour < end);
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
  const forcedToLuna = session.ai_provider === "deepseek" && isDeepSeekPeakTime();
  let selectedModel = state.defaultModel;
  if (!forcedToLuna) {
    selectedModel = {provider: session.ai_provider, model: session.ai_model};
  }
  const value = modelKey(selectedModel);
  if ([...select.options].some(option => option.value === value)) select.value = value;
  select.disabled = session.has_audio || recording || finalizing || startRequested || forcedToLuna;
}

function renderAudio() {
  const session = currentDetail?.session;
  const available = Boolean(session?.has_audio && session.resumable);
  elements.audioPlayer.hidden = !available;
  if (!available) {
    elements.audioPlayer.removeAttribute("src");
    return;
  }
  let version = "recording";
  if (session.state !== "recording") {
    version = `${session.finalized}-${session.duration_seconds}`;
  }
  const source = `/api/sessions/${encodeURIComponent(session.id)}/audio?v=${encodeURIComponent(version)}`;
  if (elements.audioPlayer.getAttribute("src") !== source) {
    elements.audioPlayer.src = source;
  }
}

function renderTranscript() {
  let itemCount = transcriptItems.length;
  let lastId = "";
  let lastText = "";
  if (transcriptMode === "final" && evidenceSegments.length) {
    itemCount = evidenceSegments.length;
    const last = evidenceSegments[evidenceSegments.length - 1];
    lastId = last.id;
    lastText = `${last.raw_text}:${last.normalized_text}`;
  } else if (transcriptMode === "final" && currentDetail) {
    const paragraphs = splitParagraphs(currentDetail.final_transcript || "");
    itemCount = paragraphs.length;
    if (paragraphs.length) lastText = paragraphs[paragraphs.length - 1];
  } else if (transcriptItems.length) {
    const last = transcriptItems[transcriptItems.length - 1];
    lastId = String(last.seq);
    lastText = last.text;
  }
  const signature = `${state.currentId}:${transcriptMode}:${itemCount}:${lastId}:${lastText}`;
  if (signature === renderedTranscriptSignature) return;
  renderedTranscriptSignature = signature;
  const atBottom = nearBottom(elements.transcriptScroll);
  elements.transcriptContent.textContent = "";
  if (!currentDetail) {
    appendEmpty(elements.transcriptContent, "セッションを選択してください。");
    return;
  }
  if (transcriptMode === "final") {
    if (evidenceSegments.length) {
      for (const segment of evidenceSegments) appendEvidenceSegment(segment);
    } else {
      appendParagraphs(splitParagraphs(currentDetail.final_transcript || ""));
    }
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

function appendEvidenceSegment(segment) {
  const section = document.createElement("section");
  section.id = `transcript-${segment.id}`;
  section.className = "evidence-segment";
  section.tabIndex = -1;
  section.dataset.segmentId = segment.id;
  const raw = document.createElement("p");
  raw.textContent = segment.raw_text;
  section.append(raw);
  if (segment.normalized_text !== segment.raw_text) {
    const normalized = document.createElement("details");
    normalized.className = "normalized-text";
    const summary = document.createElement("summary");
    summary.textContent = "正規化テキスト";
    const text = document.createElement("p");
    text.textContent = segment.normalized_text;
    normalized.append(summary, text);
    section.append(normalized);
  }
  elements.transcriptContent.append(section);
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
  article.tabIndex = -1;
  if (card.topic_id) article.dataset.topicId = card.topic_id;

  const header = document.createElement("header");
  const title = document.createElement("h2");
  title.textContent = card.title;
  header.append(title);
  if (card.status === "needs_review") {
    const badge = document.createElement("span");
    badge.className = "review-badge";
    badge.textContent = "要確認";
    header.append(badge);
  }

  const content = document.createElement("div");
  content.className = "card-content";
  // card.html の唯一のXSS防御はサーバー側の cards.sanitize_card_html です。
  content.innerHTML = card.html;

  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "元の文字起こし";
  const source = document.createElement("pre");
  source.textContent = card.source_text;
  details.append(summary, source);
  article.append(header, content, details);
  if (card.evidence_segment_ids && card.evidence_segment_ids.length) {
    const evidenceButton = document.createElement("button");
    evidenceButton.type = "button";
    evidenceButton.className = "evidence-button";
    evidenceButton.textContent = "原文・音声へ";
    evidenceButton.addEventListener("click", () => jumpToEvidence(card.evidence_segment_ids));
    article.append(evidenceButton);
  }
  return article;
}

function renderCards(cards, reset = false) {
  if (activeCardView === "final" && outlineTopics.length) {
    renderGroupedCards(cards);
    return;
  }
  const atBottom = nearBottom(elements.cards);
  if (reset) {
    elements.cards.textContent = "";
    cardVersions.clear();
  }
  const currentIds = new Set(cards.map(card => card.id));
  for (const existing of elements.cards.querySelectorAll(":scope > .card")) {
    const cardId = existing.id.replace(/^card-/, "");
    if (currentIds.has(cardId)) continue;
    existing.remove();
    cardVersions.delete(cardId);
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

function renderGroupedCards(cards) {
  elements.cards.textContent = "";
  cardVersions.clear();
  if (!cards.length) {
    appendEmpty(elements.cards, "カードを待っています。");
    return;
  }
  const rendered = new Set();
  const topics = [...outlineTopics].sort((first, second) => first.order - second.order);
  for (const topic of topics) {
    const topicCards = cards.filter(card => card.topic_id === topic.id);
    if (!topicCards.length) continue;
    const section = document.createElement("section");
    section.className = "topic-group";
    section.id = `topic-${topic.id}`;
    const heading = document.createElement("h3");
    heading.textContent = topic.title;
    const summary = document.createElement("p");
    summary.className = "topic-summary";
    summary.textContent = topic.summary;
    section.append(heading, summary);
    for (const card of topicCards) {
      section.append(cardElement(card));
      rendered.add(card.id);
    }
    elements.cards.append(section);
  }
  for (const card of cards) {
    if (!rendered.has(card.id)) elements.cards.append(cardElement(card));
  }
}

function selectPrimaryView(view) {
  activePrimaryView = view;
  for (const tab of primaryTabs) {
    const selected = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = -1;
    if (selected) tab.tabIndex = 0;
  }
  for (const panel of primaryPanels) panel.hidden = panel.id !== `${view}-panel`;
  if (view === "full" && currentDetail?.final_transcript) {
    transcriptMode = "final";
    renderTranscript();
  }
}

function outlineList(parentId) {
  const list = document.createElement("ol");
  for (const topic of outlineTopics.filter(item => item.parent_id === parentId)) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = topic.title;
    button.addEventListener("click", () => showTopic(topic));
    const summary = document.createElement("p");
    summary.textContent = topic.summary;
    item.append(button, summary);
    if (outlineTopics.some(child => child.parent_id === topic.id)) {
      item.append(outlineList(topic.id));
    }
    list.append(item);
  }
  return list;
}

function renderOutline() {
  elements.outline.textContent = "";
  if (!outlineTopics.length) {
    appendEmpty(elements.outline, "品質版の目次を待っています。");
    return;
  }
  elements.outline.append(outlineList(null));
}

function showTopic(topic) {
  selectPrimaryView("cards");
  selectCardView("final", true);
  setAudioTime(topic.segment_ids);
  requestAnimationFrame(() => {
    const card = elements.cards.querySelector(`[data-topic-id="${CSS.escape(topic.id)}"]`);
    if (!card) return;
    card.scrollIntoView({behavior: "smooth", block: "start"});
    card.focus({preventScroll: true});
  });
}

function jumpToEvidence(segmentIds) {
  selectPrimaryView("full");
  setTranscriptMode("final");
  for (const segment of elements.transcriptContent.querySelectorAll(".evidence-segment")) {
    segment.classList.toggle("selected-evidence", segmentIds.includes(segment.dataset.segmentId));
  }
  setAudioTime(segmentIds);
  const first = elements.transcriptContent.querySelector(`[data-segment-id="${CSS.escape(segmentIds[0])}"]`);
  if (!first) return;
  first.scrollIntoView({behavior: "smooth", block: "center"});
  first.focus({preventScroll: true});
}

function setAudioTime(segmentIds) {
  const segment = evidenceSegments.find(item => segmentIds.includes(item.id) && typeof item.start_ms === "number");
  if (!segment || elements.audioPlayer.hidden) return;
  elements.audioPlayer.currentTime = segment.start_ms / 1000;
}

function renderOverview() {
  elements.overview.textContent = "";
  if (!currentDetail) {
    appendEmpty(elements.overview, "セッションを選択してください。");
    return;
  }
  const stats = document.createElement("dl");
  stats.className = "overview-stats";
  const values = [
    ["主要トピック", String(outlineTopics.length)],
    ["未確認の用語", String(knowledgeUnits.filter(unit => unit.status === "needs_review").length)],
    ["処理警告", String(currentDetail.session.processing_warnings.length)],
  ];
  for (const [label, value] of values) {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    stats.append(term, description);
  }
  elements.overview.append(stats);
  if (outlineTopics.length) {
    const heading = document.createElement("h3");
    heading.textContent = "重要ポイント";
    const list = document.createElement("ul");
    for (const topic of outlineTopics.filter(item => item.parent_id === null)) {
      const item = document.createElement("li");
      item.textContent = topic.summary;
      list.append(item);
    }
    elements.overview.append(heading, list);
  }
  if (currentDetail.session.processing_warnings.length) {
    const warnings = document.createElement("ul");
    warnings.className = "processing-warnings";
    for (const warning of currentDetail.session.processing_warnings) {
      const item = document.createElement("li");
      item.textContent = warning;
      warnings.append(item);
    }
    elements.overview.append(warnings);
  }
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
setInterval(() => {
  const peak = isDeepSeekPeakTime();
  if (peak === state.deepseekPeak) return;
  state.deepseekPeak = peak;
  renderHeader();
}, 30_000);
