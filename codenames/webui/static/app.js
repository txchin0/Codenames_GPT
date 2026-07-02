/* Codenames web UI -- polls the server for game state and renders the board,
 * history and the context-sensitive action bar (spymaster clue / operative
 * guess / bot thinking / game over). */

"use strict";

const POLL_MS = 650;
const SLOW_POLL_MS = 2000;

const ROLE_LABEL = { codemaster: "Spymaster", guesser: "Operative" };
const KIND_LABEL = { human: "Human", ai: "AI agent", random: "Random bot", network: "Remote player" };
const SEATS = ["codemaster_red", "guesser_red", "codemaster_blue", "guesser_blue"];
const DEFAULTS = {
  codemaster_red: "human", guesser_red: "human",
  codemaster_blue: "random", guesser_blue: "random",
};
const SEAT_LABEL = {
  codemaster_red: "Red Spymaster", guesser_red: "Red Operative",
  codemaster_blue: "Blue Spymaster", guesser_blue: "Blue Operative",
};

// Seat tokens for network games. A browser may claim one or more "network" seats;
// we send the token of whichever seat is currently acting so the spymaster sees the
// key on their turn and actions are authorised. Persisted so a refresh keeps our seats.
let myTokens = {};
try { myTokens = JSON.parse(localStorage.getItem("cn_tokens") || "{}"); } catch (_) {}
function saveTokens() { localStorage.setItem("cn_tokens", JSON.stringify(myTokens)); }
function seatOf(pending) { return pending ? `${pending.role}_${pending.team.toLowerCase()}` : null; }
// Token to poll/act with: the pending seat's (if we own it), else any seat we hold.
function activeToken() {
  const seat = seatOf(state && state.pending);
  if (seat && myTokens[seat]) return myTokens[seat];
  const held = Object.keys(myTokens);
  return held.length ? myTokens[held[0]] : null;
}
function ownPending() {
  const seat = seatOf(state && state.pending);
  return !!(seat && myTokens[seat]);
}

let state = null;
let boardKey = "";          // identity of the current board layout
let cellEls = [];
let prevRevealed = new Set();
let actionSig = null;
let awaiting = false;       // a human action is in flight
let awaitingVersion = -1;
let aiAvailable = false;

// -- tiny DOM helper --------------------------------------------------------
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
const $ = (id) => document.getElementById(id);

async function post(url, body, token) {
  try {
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = "Bearer " + token;
    const r = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  } catch (_) { return { ok: false }; }
}

// Submit a move for the seat currently pending, authorised with that seat's token
// (only relevant in network mode; local games pass no token and behave as before).
function postAction(body) {
  const seat = seatOf(state && state.pending);
  return post("/api/action", body, seat ? myTokens[seat] : null);
}

// -- polling loop -----------------------------------------------------------
async function poll() {
  try {
    const token = activeToken();
    const url = token ? "/api/state?token=" + encodeURIComponent(token) : "/api/state";
    const r = await fetch(url);
    const s = await r.json();
    aiAvailable = !!s.ai_available;
    state = s;
    render();
  } catch (_) { /* server momentarily unavailable */ }
  // Poll quickly while a game is actively in play; back off when the tab is
  // hidden or nothing is happening (idle / finished) to spare the server.
  const quiet = document.hidden ||
    !state || state.status === "idle" || state.status === "finished";
  setTimeout(poll, quiet ? SLOW_POLL_MS : POLL_MS);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

// -- rendering --------------------------------------------------------------
function render() {
  if (!state) return;
  if (awaiting && state.version !== awaitingVersion) awaiting = false;

  const activeTeam = state.pending ? state.pending.team
    : state.actor ? state.actor.team : null;

  renderTurnStrip(activeTeam);
  renderLobby();
  renderScoreboard(activeTeam);
  renderBoard();
  renderAction();
  renderHistory();
  renderSpyToggle();
  renderBanner();
}

// -- lobby (network games): claim the seats this browser will play ----------
function renderLobby() {
  const box = $("lobby");
  if (!state.network_mode || state.status === "idle") { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = "";
  box.append(h("div", { class: "lobby-title" }, "Multiplayer seats"));
  const seats = state.seats_status || {};
  const row = h("div", { class: "lobby-seats" });
  for (const seat of SEATS) {
    const info = seats[seat];
    if (!info) continue;
    const claimable = info.kind === "network" || info.kind === "human";
    const mine = !!myTokens[seat];
    let status, cls = "lobby-seat";
    if (!claimable) { status = KIND_LABEL[info.kind] || info.kind; cls += " local"; }
    else if (mine) { status = "You"; cls += " mine"; }
    else if (info.claimed) { status = "Taken"; cls += " taken"; }
    else { status = null; cls += " open"; }
    const node = h("div", { class: cls }, h("span", { class: "ls-name" }, SEAT_LABEL[seat]));
    if (claimable && !mine && !info.claimed) {
      node.append(h("button", { class: "btn tiny", onclick: () => claimSeat(seat) }, "Claim"));
    } else {
      node.append(h("span", { class: "ls-status" }, status));
    }
    row.append(node);
  }
  box.append(row);
}

async function claimSeat(seat) {
  const res = await post("/api/join", { seat });
  if (res && res.ok) {
    myTokens[seat] = res.token;
    saveTokens();
    poll();
  }
}

function renderTurnStrip(activeTeam) {
  const strip = $("turn-strip");
  strip.innerHTML = "";
  if (state.status !== "running" || !activeTeam) return;
  const human = !!state.pending;
  strip.append(h("div", { class: `turn-pill ${activeTeam.toLowerCase()}` },
    human ? h("span", { class: "dot" }, "● ") : null,
    `${activeTeam} team — ${human ? "your move" : "thinking…"}`));
}

function renderScoreboard(activeTeam) {
  const sb = $("scoreboard");
  sb.innerHTML = "";
  if (state.status === "idle") return;
  const rem = state.remaining || { Red: 9, Blue: 8 };
  for (const team of ["Red", "Blue"]) {
    if (team === "Blue" && state.single_team) continue;
    const cls = `score ${team.toLowerCase()}` + (activeTeam === team ? " active" : "");
    sb.append(h("div", { class: cls },
      h("span", { class: "label" }, `${team} left`),
      h("span", { class: "count" }, rem[team] ?? 0)));
  }
}

function renderBoard() {
  const board = $("board");
  const cells = state.board || [];
  const key = cells.map((c) => c.word).join("|");

  if (key !== boardKey) {           // new game / different layout -> rebuild
    boardKey = key;
    board.innerHTML = "";
    cellEls = [];
    prevRevealed = new Set();
    cells.forEach((c) => {
      const el = h("div", { class: "cell", onclick: () => onCellClick(c.index) },
        h("span", { class: "tag" }), h("span", { class: "word" }, c.word));
      cellEls.push(el);
      board.append(el);
    });
  }

  const guessing = state.pending && state.pending.type === "guess" && !awaiting;
  cells.forEach((c, i) => {
    const el = cellEls[i];
    if (!el) return;
    let cls = "cell";
    if (c.revealed) {
      cls += " revealed " + c.identity.toLowerCase();
    } else if (c.identity) {         // spymaster view of a hidden card
      cls += " spy-" + shortId(c.identity);
    }
    if (guessing && !c.revealed) cls += " clickable";
    if (c.revealed && !prevRevealed.has(i)) cls += " just-revealed";
    el.className = cls;

    const tag = el.querySelector(".tag");
    tag.textContent = c.revealed ? c.identity.toUpperCase() : "";
    tag.style.display = c.revealed ? "" : "none";
  });
  prevRevealed = new Set(cells.filter((c) => c.revealed).map((c) => c.index));
}

function shortId(identity) {
  return identity === "Civilian" ? "civ" : identity.toLowerCase();
}

function onCellClick(index) {
  if (awaiting) return;
  if (!state.pending || state.pending.type !== "guess") return;
  if (state.network_mode && !ownPending()) return;   // not your seat
  const cell = state.board[index];
  if (!cell || cell.revealed) return;
  awaiting = true; awaitingVersion = state.version;
  renderBoard();
  postAction({ word: cell.word });
}

function renderAction() {
  const bar = $("action-bar");
  const p = state.pending;
  const sig = [state.status, p ? p.type : "", p ? p.team : "",
    p ? (p.feedback || "") : "", state.actor ? state.actor.team + state.actor.role : "",
    state.winner || "", state.error || "",
    state.network_mode ? (ownPending() ? "mine" : "wait") : ""].join("|");
  if (sig === actionSig) {
    const btn = bar.querySelector("button[data-guard]");
    if (btn) btn.disabled = awaiting;   // keep in-flight state fresh
    return;
  }
  actionSig = sig;
  bar.className = "action-bar";
  bar.innerHTML = "";

  if (state.status === "idle") {
    bar.append(h("div", { class: "action-placeholder" }, "Start a new game to begin."));
    return;
  }
  if (state.status === "error") {
    bar.append(h("div", { class: "action-main" },
      h("div", { class: "action-title" }, "Something went wrong"),
      h("div", { class: "feedback" }, state.error || "Unknown error.")));
    return;
  }
  if (state.status === "finished") {
    const who = state.winner === "R" ? "Red" : "Blue";
    bar.classList.add(who.toLowerCase() + "-turn");
    bar.append(h("div", { class: "action-main" },
      h("div", { class: "action-title" }, `🏆 ${who} team wins!`),
      h("div", { class: "action-sub" }, "Press “New Game” to play again.")));
    return;
  }

  if (p) {
    bar.classList.add(p.team.toLowerCase() + "-turn");
    // In a network game only the player who owns the acting seat gets the
    // controls; everyone else waits (and never receives the key in state).
    if (state.network_mode && !ownPending()) {
      const label = `${p.team} ${ROLE_LABEL[p.role] || p.role}`;
      bar.append(h("div", { class: "thinking" },
        h("div", { class: "spinner" }),
        h("div", null, `Waiting for the ${label}…`)));
      return;
    }
    if (p.type === "clue") return renderClueForm(bar, p);
    if (p.type === "guess") return renderGuessPrompt(bar, p);
    if (p.type === "keep_guessing") return renderKeepPrompt(bar, p);
  }

  // a bot (or the engine) is working
  const actor = state.actor;
  const label = actor ? `${actor.team} ${ROLE_LABEL[actor.role] || actor.role}` : "The game";
  bar.append(h("div", { class: "thinking" },
    h("div", { class: "spinner" }),
    h("div", null, `${label} is thinking…`)));
}

function renderClueForm(bar, p) {
  const word = h("input", { type: "text", id: "clue-word", placeholder: "clue", autocomplete: "off", maxlength: "24" });
  const num = h("input", { type: "number", id: "clue-num", value: "1", min: "0", max: "9" });
  const submit = () => {
    const w = word.value.trim();
    if (!w) { word.focus(); return; }
    awaiting = true; awaitingVersion = state.version;
    btn.disabled = true;
    postAction({ word: w, number: parseInt(num.value || "1", 10) });
  };
  const btn = h("button", { class: "btn primary", "data-guard": "1", onclick: submit }, "Give Clue");
  const form = h("form", { class: "clue-form", onsubmit: (e) => { e.preventDefault(); submit(); } },
    word, num, btn, h("span", { class: "field-hint" }, "one word · 0 = unlimited"));

  bar.append(h("div", { class: "action-main" },
    h("div", { class: "action-title" }, `You are the ${p.team} Spymaster`),
    h("div", { class: "action-sub" }, "The key is revealed below. Give a one-word clue that links your team’s words."),
    p.feedback ? h("div", { class: "feedback" }, "Rejected: " + p.feedback) : null,
    form));
  setTimeout(() => word.focus(), 0);
}

function renderGuessPrompt(bar, p) {
  bar.append(h("div", { class: "action-main" },
    h("div", { class: "action-title" }, `You are the ${p.team} Operative`),
    h("div", { class: "clue-banner" },
      "Clue: ", h("span", { class: "clue-word" }, String(p.clue || "—").toUpperCase()),
      h("span", { class: "clue-num" }, p.num)),
    h("div", { class: "action-sub" }, "Click a word on the board to guess it."),
    p.feedback ? h("div", { class: "feedback" }, "Rejected: " + p.feedback) : null));
}

function renderKeepPrompt(bar, p) {
  const keep = (v) => { awaiting = true; awaitingVersion = state.version; postAction({ keep: v }); };
  bar.append(
    h("div", { class: "action-main" },
      h("div", { class: "action-title" }, "✅ Correct — keep guessing?"),
      h("div", { class: "clue-banner" },
        "Clue: ", h("span", { class: "clue-word" }, String(p.clue || "—").toUpperCase()),
        h("span", { class: "clue-num" }, p.num))),
    h("div", { class: "action-side" },
      h("button", { class: "btn primary", "data-guard": "1", onclick: () => keep(true) }, "Keep Guessing"),
      h("button", { class: "btn", onclick: () => keep(false) }, "End Turn")));
}

function renderHistory() {
  const box = $("history");
  const hist = state.history || [];
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = "";
  if (!hist.length) {
    box.append(h("div", { class: "history-empty" }, "No moves yet."));
    return;
  }

  let group = null;
  const groups = [];
  for (const m of hist) {
    const [who, ...rest] = m;
    const [team, role] = who.split("_");
    if (role === "Codemaster") {
      group = { team, clue: rest[0], num: rest[1], guesses: [] };
      groups.push(group);
    } else {
      if (!group || group.team !== team) { group = { team, clue: null, num: null, guesses: [] }; groups.push(group); }
      const identity = String(rest[1]).replace(/\*/g, "");
      group.guesses.push({ word: rest[0], identity, correct: identity === team });
    }
  }

  for (const g of groups) {
    const tclass = g.team.toLowerCase();
    const node = h("div", { class: `turn-group ${tclass}` });
    if (g.clue !== null) {
      node.append(h("div", { class: `clue-row ${tclass}` },
        h("span", { class: "who" }, `${g.team} SM`),
        h("span", { class: "clue-name" }, String(g.clue || "—").toUpperCase()),
        h("span", { class: "n" }, g.num)));
    }
    for (const gs of g.guesses) {
      node.append(h("div", { class: "guess-row" },
        h("span", { class: "swatch " + gs.identity.toLowerCase() }),
        h("span", { class: "g-word" }, gs.word.toUpperCase()),
        h("span", { class: "g-out " + (gs.correct ? "hit" : "miss") },
          gs.correct ? "✓" : "✗ " + gs.identity)));
    }
    box.append(node);
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderSpyToggle() {
  const wrap = $("spy-toggle-wrap");
  const cb = $("spy-toggle");
  // The global spymaster toggle would leak the key to everyone, so it is
  // meaningless (and disabled server-side) in a network game.
  if (state.network_mode) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const p = state.pending;
  let disabled = state.status !== "running";
  let checked = state.reveal_override;
  if (p && p.type === "clue") { checked = true; disabled = true; }
  else if (p && (p.type === "guess" || p.type === "keep_guessing")) { checked = false; disabled = true; }
  cb.checked = checked;
  cb.disabled = disabled;
  wrap.classList.toggle("disabled", disabled);
}

function renderBanner() {
  const b = $("banner");
  if (state.status === "finished") {
    const who = state.winner === "R" ? "Red" : "Blue";
    b.hidden = false; b.className = "banner " + who.toLowerCase();
    b.textContent = `🏆 ${who} Team Wins!`;
  } else if (state.status === "error") {
    b.hidden = false; b.className = "banner error"; b.textContent = "Game error — see the action bar.";
  } else {
    b.hidden = true;
  }
}

// -- setup / controls -------------------------------------------------------
function buildSelects() {
  for (const seat of SEATS) {
    const sel = $(seat);
    sel.innerHTML = "";
    for (const kind of ["human", "ai", "random", "network"]) {
      const opt = h("option", { value: kind }, KIND_LABEL[kind]);
      if (kind === "ai" && !aiAvailable) { opt.disabled = true; opt.textContent += " (unavailable)"; }
      sel.append(opt);
    }
    sel.value = DEFAULTS[seat];
  }
  const note = $("ai-note");
  if (!aiAvailable) {
    note.className = "ai-note warn";
    note.textContent = "AI agents need the local LLM server running (see gpt_manager.py). Human and Random bots work with no setup.";
  } else {
    note.className = "ai-note";
    note.textContent = "Tip: “Human” plays here, “Remote player” lets a friend claim the seat over the tunnel (browser or agent), or mix in AI / Random bots.";
  }
}

function syncSingleTeam() {
  const single = $("single_team").checked;
  $("blue-col").classList.toggle("disabled", single);
  $("codemaster_blue").disabled = single;
  $("guesser_blue").disabled = single;
}

function openModal() { $("setup-modal").hidden = false; }
function closeModal() { $("setup-modal").hidden = true; }

async function startGame() {
  const seats = {};
  for (const seat of SEATS) seats[seat] = $(seat).value;
  const cfg = {
    seats,
    single_team: $("single_team").checked,
    seed: $("seed").value.trim() || "time",
  };
  const res = await post("/api/new_game", cfg);
  if (res && res.ok) {
    boardKey = ""; actionSig = null; awaiting = false; prevRevealed = new Set();
    myTokens = {}; saveTokens();   // seat tokens from a previous game are now invalid
    closeModal();
  } else if (res && res.error) {
    const note = $("ai-note"); note.className = "ai-note warn"; note.textContent = res.error;
  }
}

function init() {
  $("new-game-btn").addEventListener("click", openModal);
  $("start-btn").addEventListener("click", startGame);
  $("single_team").addEventListener("change", syncSingleTeam);
  $("spy-toggle").addEventListener("change", (e) => post("/api/reveal", { reveal: e.target.checked }));

  fetch("/api/state").then((r) => r.json()).then((s) => {
    aiAvailable = !!s.ai_available;
    buildSelects();
    syncSingleTeam();
    if (s.status === "idle") openModal();
  }).catch(() => { buildSelects(); openModal(); });

  poll();
}

document.addEventListener("DOMContentLoaded", init);
