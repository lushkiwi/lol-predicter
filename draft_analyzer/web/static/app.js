/* Draft Predictor front end. Vanilla JS, no dependencies.
 *
 * State (S) is the only source of truth; it is saved to localStorage after every change and sent
 * to POST /api/predict. The last response (R) only decorates the page (probabilities, win bar).
 */
'use strict';

const STORE_KEY = 'draft-predictor.state.v1';
const SIDES = ['blue', 'red'];
const ROLE_SHORT = { top: 'Top', jungle: 'Jgl', mid: 'Mid', bot: 'Bot', support: 'Sup' };
const ROLE_MIN = 0.15; // role filter: champion counts for a role if played there this often
const GROUP_SHORT = {
  'champion meta strength': 'meta',
  'player comfort': 'player',
  'role fit': 'role',
  'synergy/counter': 'synergy',
  'team tendency': 'team',
  'draft context': 'draft',
  'series context': 'series',
  'team strength': 'strength',
};

let META = null;            // GET /api/meta
let ORDER = [];             // META.draft_order
let CHAMP = new Map();      // name -> champion
let TEAM = new Map();       // lower-case name -> team
let S = null;               // persistent state
let R = null;               // last accepted prediction response
let reqSeq = 0;             // id of the latest request sent
let predictTimer = 0;
let apiError = '';
let tiles = new Map();      // name -> tile element

const $ = (id) => document.getElementById(id);
const other = (side) => (side === 'blue' ? 'red' : 'blue');
const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function icon(name, cls) {
  const img = el('img', cls);
  const c = CHAMP.get(name);
  img.alt = name;
  img.draggable = false;
  img.loading = 'lazy';
  if (c) img.src = `/img/champion/${encodeURIComponent(c.id)}.png`;
  return img;
}
function pct(p, digits = 1) {
  if (p == null || isNaN(p)) return '–';
  return (p * 100).toFixed(digits) + '%';
}
function probLabel(p) {
  if (p == null) return '';
  if (p >= 0.1) return Math.round(p * 100) + '%';
  if (p >= 0.001) return (p * 100).toFixed(1) + '%';
  return '<0.1%';
}
function signed(x, d = 2) {
  return (x >= 0 ? '+' : '−') + Math.abs(x).toFixed(d);
}

/* ---------- persistence ---------- */

function loadStored() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}
function save() {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(S));
  } catch (e) { /* storage unavailable: the page still works for this session */ }
}

function defaultState() {
  const lck = META.teams.filter((t) => t.league === 'LCK');
  const pickTeam = (name, fallbackIdx) =>
    lck.find((t) => t.name === name) || lck[fallbackIdx] || META.teams[fallbackIdx] || { name: '', roster: ['', '', '', '', ''] };
  const a = pickTeam('Gen.G', 0);
  const b = pickTeam('T1', 1);
  return {
    league: META.leagues.includes('LCK') ? 'LCK' : META.leagues[0],
    patch: META.default_patch,
    fearless: 'hard',
    blue: { team: a.name, players: a.roster.slice() },
    red: { team: b.name, players: b.roster.slice() },
    firstPick: 'blue',
    actions: [],
    actionInfo: [],
    previous: [],
    ui: { sort: 'prob', role: 'all' },
  };
}

function sanitize(s) {
  const d = defaultState();
  if (!s || typeof s !== 'object') return d;
  const str = (v, fb) => (typeof v === 'string' ? v : fb);
  const out = {
    league: str(s.league, d.league),
    patch: str(s.patch, d.patch),
    fearless: ['hard', 'soft', 'none'].includes(s.fearless) ? s.fearless : 'hard',
    firstPick: SIDES.includes(s.firstPick) ? s.firstPick : 'blue',
    previous: Array.isArray(s.previous) ? s.previous.filter((g) => g && Array.isArray(g.actions)) : [],
    ui: {
      sort: s.ui && s.ui.sort === 'az' ? 'az' : 'prob',
      role: s.ui && (s.ui.role === 'all' || META.roles.includes(s.ui.role)) ? s.ui.role : 'all',
    },
  };
  for (const side of SIDES) {
    const t = s[side] || {};
    const players = Array.isArray(t.players) ? t.players.slice(0, 5).map((p) => str(p, '')) : [];
    while (players.length < 5) players.push('');
    out[side] = { team: str(t.team, d[side].team), players };
  }
  // keep the longest valid prefix of the draft
  const actions = [];
  const seen = new Set();
  for (const a of Array.isArray(s.actions) ? s.actions.slice(0, ORDER.length) : []) {
    if (a === null && ORDER[actions.length].type === 'ban') { actions.push(null); continue; }
    if (typeof a !== 'string' || !CHAMP.has(a) || seen.has(a)) break;
    seen.add(a);
    actions.push(a);
  }
  out.actions = actions;
  out.actionInfo = Array.isArray(s.actionInfo) ? s.actionInfo.slice(0, actions.length) : [];
  while (out.actionInfo.length < actions.length) out.actionInfo.push(null);
  return out;
}

/* ---------- draft helpers ---------- */

function firstPickSide() { return S.firstPick; }
function sideOf(i) {
  return ORDER[i].slot === 'first' ? firstPickSide() : other(firstPickSide());
}
// for action i: its side and its position among that side's bans or picks (0..4)
function slotOf(i) {
  const side = sideOf(i);
  let k = 0;
  for (let j = 0; j < i; j++) if (ORDER[j].type === ORDER[i].type && sideOf(j) === side) k++;
  return { side, type: ORDER[i].type, k };
}
function nthOfType(i) {
  let k = 0;
  for (let j = 0; j <= i; j++) if (ORDER[j].type === ORDER[i].type) k++;
  return k;
}
function teamName(side) { return S[side].team.trim() || (side === 'blue' ? 'Blue' : 'Red'); }
function nextIndex() { return S.actions.length; }
function draftDone() { return S.actions.length >= ORDER.length; }

function usedThisGame(name) {
  const i = S.actions.indexOf(name);
  return i < 0 ? null : { index: i, side: sideOf(i), type: ORDER[i].type };
}

// probabilities from the last response, only when it was computed for the current action index
function currentProbs() {
  if (!R || !R.next_action || R.next_action.action_index !== nextIndex()) return null;
  if (!R._probMap) {
    R._probMap = new Map();
    R.next_action.probabilities.forEach((x, idx) => R._probMap.set(x.champion, { p: x.probability, rank: idx + 1 }));
  }
  return R._probMap;
}
function burnedFor(side) {
  if (!R || !R.game) return [];
  return R.game[side + '_burned'] || [];
}

/* ---------- prediction requests ---------- */

function buildRequest() {
  return {
    patch: S.patch,
    league: S.league,
    blue_team: S.blue.team.trim(),
    red_team: S.red.team.trim(),
    first_pick_team: S[firstPickSide()].team.trim(),
    blue_players: S.blue.players.map((p) => p.trim()),
    red_players: S.red.players.map((p) => p.trim()),
    fearless: S.fearless,
    previous_games: S.previous,
    actions: S.actions,
    top: 12,
  };
}

function schedulePredict(delay = 0) {
  clearTimeout(predictTimer);
  if (delay) predictTimer = setTimeout(predict, delay);
  else predict();
}

async function predict() {
  const seq = ++reqSeq;
  document.body.classList.add('loading');
  let data = null;
  let err = '';
  try {
    const resp = await fetch('/api/predict', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildRequest()),
    });
    let body = null;
    try { body = await resp.json(); } catch (e) { body = null; }
    if (!resp.ok) err = (body && body.detail) ? String(body.detail) : `HTTP ${resp.status}`;
    else data = body;
  } catch (e) {
    err = 'Cannot reach the server (' + e.message + ')';
  }
  if (seq !== reqSeq) return; // a newer request is in flight: drop this one
  document.body.classList.remove('loading');
  if (err) {
    if (err !== apiError) toast('Prediction failed: ' + err, { kind: 'error' });
    apiError = err;
  } else {
    apiError = '';
    R = data;
  }
  render();
}

/* ---------- mutations ---------- */

function commit(delay = 0) {
  save();
  render();
  schedulePredict(delay);
}

function applyAction(name) {
  if (draftDone()) return;
  const i = nextIndex();
  if (name === null) {
    if (ORDER[i].type !== 'ban') return;
  } else {
    if (!CHAMP.has(name) || usedThisGame(name)) return;
    const probs = currentProbs();
    if (probs && !probs.has(name)) return;
  }
  const probs = currentProbs();
  const info = name && probs && probs.get(name);
  S.actions.push(name);
  S.actionInfo.push(info ? { p: info.p, rank: info.rank } : null);
  commit();
}

function undo() {
  if (!S.actions.length) return;
  S.actions.pop();
  S.actionInfo.pop();
  commit();
}

function snapshot() { return JSON.parse(JSON.stringify(S)); }
function restore(snap, msg) {
  S = snap;
  commit();
  toast(msg);
}

function newDraft() {
  if (!S.actions.length) return;
  const snap = snapshot();
  S.actions = [];
  S.actionInfo = [];
  commit();
  toast('Draft cleared.', { action: { label: 'Undo', fn: () => restore(snap, 'Draft restored.') } });
}

function recordWinner(side) {
  if (!draftDone()) return;
  S.previous.push({
    blue_team: S.blue.team.trim(),
    red_team: S.red.team.trim(),
    first_pick_team: S[firstPickSide()].team.trim(),
    blue_players: S.blue.players.map((p) => p.trim()),
    red_players: S.red.players.map((p) => p.trim()),
    actions: S.actions.slice(),
    winner: S[side].team.trim(),
  });
  S.actions = [];
  S.actionInfo = [];
  commit();
  toast(`Game ${S.previous.length} recorded: ${teamName(side)} won. Game ${S.previous.length + 1} started; set sides and first pick above.`, { ttl: 7000 });
}

function removeLastGame() {
  if (!S.previous.length) return;
  const snap = snapshot();
  const g = S.previous.pop();
  commit();
  toast(`Removed game ${S.previous.length + 1} (${g.winner} won).`, { action: { label: 'Undo', fn: () => restore(snap, 'Game restored.') } });
}

function resetSeries() {
  if (!S.previous.length) return;
  const snap = snapshot();
  S.previous = [];
  commit();
  toast('Series reset to game 1.', { action: { label: 'Undo', fn: () => restore(snap, 'Series restored.') } });
}

function swapSides() {
  const b = S.blue;
  S.blue = S.red;
  S.red = b;
  S.firstPick = other(S.firstPick); // first pick stays with the same team
  commit();
}

function setTeam(side, value) {
  const name = value.trim();
  if (!name) { render(); return; }
  const t = TEAM.get(name.toLowerCase());
  if (t) {
    const changed = S[side].team !== t.name;
    S[side].team = t.name;
    if (changed || S[side].players.every((p) => !p.trim())) S[side].players = t.roster.slice();
  } else {
    if (S[side].team === name) { render(); return; }
    S[side].team = name;
  }
  commit();
}

/* ---------- toasts and notices ---------- */

function toast(msg, opts = {}) {
  const box = $('toasts');
  const t = el('div', 'toast' + (opts.kind ? ' ' + opts.kind : ''));
  t.appendChild(el('span', null, msg));
  const close = () => { t.classList.add('out'); setTimeout(() => t.remove(), 250); };
  if (opts.action) {
    const b = el('button', 'btn small', opts.action.label);
    b.type = 'button';
    b.onclick = () => { close(); opts.action.fn(); };
    t.appendChild(b);
  }
  const x = el('button', 'toast-x', '×');
  x.type = 'button';
  x.setAttribute('aria-label', 'Dismiss');
  x.onclick = close;
  t.appendChild(x);
  box.appendChild(t);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(close, opts.ttl || (opts.kind === 'error' ? 8000 : 5000));
}

function localWarnings() {
  const w = [];
  const b = S.blue.team.trim(), r = S.red.team.trim();
  if (!b || !r) w.push('A team name is empty.');
  if (b && b.toLowerCase() === r.toLowerCase()) w.push('Both sides have the same team name, so first pick is ambiguous.');
  for (const side of SIDES) {
    const empty = S[side].players.filter((p) => !p.trim()).length;
    if (empty) w.push(`${teamName(side)}: ${empty} player name(s) empty.`);
  }
  return w;
}

function renderNotices() {
  const box = $('notices');
  const warnings = localWarnings().concat((R && R.warnings) || []);
  box.textContent = '';
  if (apiError) {
    const e = el('span', 'pill error', 'API error');
    e.title = apiError;
    box.appendChild(e);
  }
  if (!warnings.length) return;
  const wrap = el('div', 'warn-wrap');
  const btn = el('button', 'pill warn', `${warnings.length} warning${warnings.length > 1 ? 's' : ''}`);
  btn.type = 'button';
  btn.setAttribute('aria-expanded', 'false');
  const list = el('ul', 'warn-list');
  list.hidden = true;
  warnings.forEach((w) => list.appendChild(el('li', null, w)));
  btn.onclick = (ev) => {
    ev.stopPropagation();
    list.hidden = !list.hidden;
    btn.setAttribute('aria-expanded', String(!list.hidden));
  };
  wrap.append(btn, list);
  box.appendChild(wrap);
}

/* ---------- setup controls ---------- */

function fillSelect(sel, values, current) {
  sel.textContent = '';
  const vals = values.slice();
  if (current && !vals.includes(current)) vals.unshift(current);
  vals.forEach((v) => { const o = el('option', null, v); o.value = v; sel.appendChild(o); });
  sel.value = current;
}

function setSeg(id, value) {
  for (const b of $(id).querySelectorAll('button')) {
    const on = b.dataset.v === value;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  }
}
function bindSeg(id, fn) {
  $(id).addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b && b.dataset.v) fn(b.dataset.v);
  });
}

function renderSetup() {
  if ($('league').value !== S.league) fillSelect($('league'), META.leagues, S.league);
  if ($('patch').value !== S.patch) fillSelect($('patch'), META.patches, S.patch);
  setSeg('fearless', S.fearless);
  setSeg('firstPick', S.firstPick);
  const fp = $('firstPick').querySelectorAll('button');
  fp[0].textContent = teamName('blue');
  fp[1].textContent = teamName('red');
  fp[0].title = `${teamName('blue')} (blue side) has first pick`;
  fp[1].title = `${teamName('red')} (red side) has first pick`;
  setSeg('roleFilter', S.ui.role);
  setSeg('sortMode', S.ui.sort);
}

/* ---------- team columns ---------- */

function buildTeamColumn(side) {
  const col = side === 'blue' ? $('teamBlue') : $('teamRed');
  col.textContent = '';

  const head = el('div', 'team-head');
  head.appendChild(el('div', 'side-tag', side === 'blue' ? 'Blue side' : 'Red side'));
  const fpTag = el('div', 'fp-tag', 'First pick');
  fpTag.dataset.role = 'fp';
  head.appendChild(fpTag);
  col.appendChild(head);

  const combo = el('div', 'combo');
  const input = el('input', 'team-input');
  input.type = 'text';
  input.autocomplete = 'off';
  input.spellcheck = false;
  input.setAttribute('aria-label', `${side} team`);
  input.dataset.role = 'team';
  const list = el('div', 'combo-list');
  list.hidden = true;
  list.setAttribute('role', 'listbox');
  combo.append(input, list);
  col.appendChild(combo);
  bindCombo(side, input, list);

  const score = el('div', 'team-score');
  score.dataset.role = 'score';
  col.appendChild(score);

  const players = el('div', 'players');
  META.roles.forEach((role, i) => {
    const row = el('label', 'player');
    row.appendChild(el('span', 'role-tag', ROLE_SHORT[role] || role));
    const p = el('input');
    p.type = 'text';
    p.spellcheck = false;
    p.autocomplete = 'off';
    p.dataset.role = 'player';
    p.dataset.i = i;
    p.addEventListener('input', () => {
      S[side].players[i] = p.value;
      save();
      renderNotices();
      schedulePredict(350);
    });
    row.appendChild(p);
    players.appendChild(row);
  });
  col.appendChild(players);

  col.appendChild(el('div', 'sub', 'Bans'));
  const bans = el('div', 'bans');
  for (let k = 0; k < 5; k++) {
    if (k === 3) bans.appendChild(el('div', 'ban-gap'));
    const s = el('div', 'ban slot');
    s.dataset.slot = 'ban' + k;
    bans.appendChild(s);
  }
  col.appendChild(bans);

  col.appendChild(el('div', 'sub', 'Picks'));
  const picks = el('div', 'picks');
  for (let k = 0; k < 5; k++) {
    const s = el('div', 'pick slot');
    s.dataset.slot = 'pick' + k;
    picks.appendChild(s);
  }
  col.appendChild(picks);

  const burned = el('div', 'burned');
  burned.dataset.role = 'burned';
  col.appendChild(burned);
}

function bindCombo(side, input, list) {
  let active = -1;
  const options = () => Array.from(list.querySelectorAll('.opt'));
  const open = () => { fill(false); list.hidden = false; };
  const close = () => { list.hidden = true; active = -1; };
  const highlight = (i) => {
    const opts = options();
    opts.forEach((o, j) => o.classList.toggle('active', j === i));
    active = i;
    if (opts[i]) opts[i].scrollIntoView({ block: 'nearest' });
  };
  function fill(filter) {
    const q = filter ? norm(input.value) : '';
    list.textContent = '';
    const leagues = META.leagues.slice();
    META.teams.forEach((t) => { if (!leagues.includes(t.league)) leagues.push(t.league); });
    let n = 0;
    for (const lg of leagues) {
      const teams = META.teams.filter((t) => t.league === lg && (!q || norm(t.name).includes(q) || norm(lg).includes(q)));
      if (!teams.length) continue;
      list.appendChild(el('div', 'opt-group', lg));
      for (const t of teams) {
        const o = el('div', 'opt' + (t.name === S[side].team ? ' current' : ''));
        o.setAttribute('role', 'option');
        o.dataset.name = t.name;
        o.appendChild(el('span', null, t.name));
        o.appendChild(el('span', 'muted small', t.roster.join(' · ')));
        list.appendChild(o);
        n++;
      }
    }
    if (!n && !input.value.trim()) list.appendChild(el('div', 'opt-empty muted', 'No teams'));
    if (input.value.trim() && !TEAM.has(input.value.trim().toLowerCase())) {
      const o = el('div', 'opt custom');
      o.dataset.name = input.value.trim();
      o.textContent = `Use "${input.value.trim()}" (team not in the data)`;
      list.appendChild(o);
    }
    active = -1;
  }
  input.addEventListener('focus', () => { input.select(); open(); });
  input.addEventListener('input', () => { fill(true); list.hidden = false; if (input.value.trim()) highlight(0); });
  input.addEventListener('keydown', (e) => {
    const opts = options();
    if (e.key === 'ArrowDown') { e.preventDefault(); if (list.hidden) open(); highlight(Math.min(active + 1, opts.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(Math.max(active - 1, 0)); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const pick = opts[active];
      close();
      setTeam(side, pick ? pick.dataset.name : input.value);
      input.blur();
    } else if (e.key === 'Escape') { close(); input.value = S[side].team; input.blur(); }
  });
  input.addEventListener('blur', () => {
    setTimeout(() => {
      if (list.hidden) return;
      close();
      setTeam(side, input.value);
    }, 120);
  });
  list.addEventListener('mousedown', (e) => {
    const o = e.target.closest('.opt');
    if (!o) return;
    e.preventDefault();
    close();
    setTeam(side, o.dataset.name);
    input.blur();
  });
}

function renderTeam(side) {
  const col = side === 'blue' ? $('teamBlue') : $('teamRed');
  const input = col.querySelector('[data-role=team]');
  if (document.activeElement !== input) input.value = S[side].team;
  col.querySelector('[data-role=fp]').hidden = firstPickSide() !== side;
  col.querySelectorAll('[data-role=player]').forEach((p) => {
    if (document.activeElement !== p) p.value = S[side].players[+p.dataset.i];
  });

  const wins = S.previous.filter((g) => g.winner && g.winner === S[side].team.trim()).length;
  const sc = col.querySelector('[data-role=score]');
  sc.textContent = S.previous.length ? `${wins} win${wins === 1 ? '' : 's'} in series` : '';

  // clear slots
  col.querySelectorAll('.slot').forEach((s) => {
    s.className = s.className.split(' ').filter((c) => c === 'ban' || c === 'pick' || c === 'slot').join(' ');
    s.textContent = '';
    s.removeAttribute('title');
  });
  for (let i = 0; i < ORDER.length; i++) {
    const { side: sd, type, k } = slotOf(i);
    if (sd !== side) continue;
    const s = col.querySelector(`[data-slot=${type}${k}]`);
    if (i < S.actions.length) fillSlot(s, i, type);
    else if (i === S.actions.length) {
      s.classList.add('next');
      s.appendChild(el('span', 'slot-next', type === 'ban' ? 'Banning…' : 'Picking…'));
    } else {
      s.classList.add('empty');
      if (type === 'pick') s.appendChild(el('span', 'slot-order', '#' + (i + 1)));
    }
  }

  // Fearless: champions this team cannot pick in this game
  const bx = col.querySelector('[data-role=burned]');
  bx.textContent = '';
  const burned = burnedFor(side);
  if (S.fearless !== 'none' && burned.length) {
    bx.appendChild(el('div', 'sub', `Fearless: unavailable (${burned.length})`));
    const wrap = el('div', 'burned-icons');
    burned.forEach((n) => { const im = icon(n, 'bicon'); im.title = `${n}: used in an earlier game`; wrap.appendChild(im); });
    bx.appendChild(wrap);
  }
}

function fillSlot(s, i, type) {
  const name = S.actions[i];
  const info = S.actionInfo[i];
  s.classList.add('filled');
  if (name === null) {
    s.classList.add('missed');
    s.appendChild(el('span', 'slot-missed', 'none'));
    s.title = `Action ${i + 1}: missed ban`;
    return;
  }
  s.appendChild(icon(name, 'slot-icon'));
  const modelTxt = info ? `model gave it ${probLabel(info.p)} (rank ${info.rank})` : '';
  s.title = `Action ${i + 1}: ${name}${modelTxt ? ' · ' + modelTxt : ''}`;
  if (type === 'pick') {
    const txt = el('div', 'slot-text');
    txt.appendChild(el('div', 'slot-name', name));
    txt.appendChild(el('div', 'slot-meta', `#${i + 1}` + (info ? ` · ${probLabel(info.p)} · rank ${info.rank}` : '')));
    s.appendChild(txt);
  }
}

/* ---------- status line ---------- */

function renderStatus() {
  const st = $('status');
  st.textContent = '';
  const done = draftDone();
  $('winnerAsk').hidden = !done;
  $('undoBtn').disabled = !S.actions.length;
  $('newDraftBtn').disabled = !S.actions.length;
  const i = nextIndex();
  $('missedBtn').disabled = done || ORDER[i].type !== 'ban';
  const gameNo = S.previous.length + 1;
  st.title = '';
  if (done) {
    st.appendChild(el('span', 'status-main', `Game ${gameNo}: draft complete`));
    $('winBlue').textContent = teamName('blue') + ' won';
    $('winRed').textContent = teamName('red') + ' won';
    document.body.dataset.acting = '';
    return;
  }
  const a = ORDER[i];
  const side = sideOf(i);
  document.body.dataset.acting = side;
  const dot = el('span', 'dot ' + side);
  const main = el('span', 'status-main');
  main.append(el('b', side + '-text', teamName(side)), document.createTextNode(` to ${a.type}`));
  const sub = el('span', 'status-sub', `(${a.type} ${nthOfType(i)} of 10 · action ${i + 1}/20)`);
  st.title = `${teamName(side)} to ${a.type}: ${phaseLabel(a.phase)}, ${a.type} ${nthOfType(i)} of 10, action ${i + 1} of 20, game ${gameNo}`;
  st.append(dot, main, sub);
}
function phaseLabel(ph) {
  return { ban1: 'ban phase 1', pick1: 'pick phase 1', ban2: 'ban phase 2', pick2: 'pick phase 2' }[ph] || ph;
}

/* ---------- champion grid ---------- */

function buildGrid() {
  const grid = $('grid');
  grid.textContent = '';
  for (const c of META.champions) {
    const t = el('button', 'tile');
    t.type = 'button';
    t.dataset.name = c.name;
    t.setAttribute('role', 'listitem');
    const im = icon(c.name, 'tile-icon');
    t.appendChild(im);
    t.appendChild(el('span', 'tile-rank'));
    t.appendChild(el('span', 'tile-reason'));
    t.appendChild(el('span', 'tile-name', c.name));
    t.appendChild(el('span', 'tile-prob'));
    t.appendChild(el('span', 'tile-bar'));
    tiles.set(c.name, t);
    grid.appendChild(t);
  }
  grid.addEventListener('click', (e) => {
    const t = e.target.closest('.tile');
    if (!t || t.classList.contains('off')) return;
    applyAction(t.dataset.name);
  });
}

function roleMatch(c, role) {
  return role === 'all' || c.primary_role === role || (c.roles[role] || 0) >= ROLE_MIN;
}

// what the grid shows for one champion: {available, p, rank, reason, why}
function champStatus(name, probs, actingSide) {
  const used = usedThisGame(name);
  if (used) {
    return { available: false, reason: used.type === 'ban' ? 'Banned' : 'Picked',
      why: `${used.type === 'ban' ? 'Banned' : 'Picked'} by ${teamName(used.side)} (action ${used.index + 1})` };
  }
  if (!actingSide) return { available: false, reason: '', why: 'Draft complete' };
  if (burnedFor(actingSide).includes(name) && (!probs || !probs.has(name))) {
    return { available: false, reason: 'Fearless', why: `Fearless: ${teamName(actingSide)} cannot use ${name} (played in an earlier game)` };
  }
  if (probs) {
    const x = probs.get(name);
    if (!x) return { available: false, reason: 'N/A', why: 'Not available for this action' };
    return { available: true, p: x.p, rank: x.rank };
  }
  return { available: true };
}

function renderGrid() {
  const probs = currentProbs();
  const done = draftDone();
  const actingSide = done ? null : sideOf(nextIndex());
  const q = norm($('search').value);
  const role = S.ui.role;
  const maxP = probs && R.next_action.probabilities.length ? R.next_action.probabilities[0].probability : 1;
  const otherBurned = actingSide ? new Set(burnedFor(other(actingSide))) : new Set();
  const rows = [];
  for (const c of META.champions) {
    const t = tiles.get(c.name);
    const st = champStatus(c.name, probs, actingSide);
    const visible = (!q || norm(c.name).includes(q)) && roleMatch(c, role);
    t.hidden = !visible;
    t.classList.toggle('off', !st.available);
    t.setAttribute('aria-disabled', String(!st.available));
    t.querySelector('.tile-reason').textContent = st.reason || '';
    const probEl = t.querySelector('.tile-prob');
    const rankEl = t.querySelector('.tile-rank');
    const bar = t.querySelector('.tile-bar');
    if (st.available && st.p != null) {
      const heat = Math.min(1, Math.sqrt(st.p / maxP));
      t.style.setProperty('--heat', heat.toFixed(3));
      bar.style.width = (100 * st.p / maxP).toFixed(1) + '%';
      probEl.textContent = probLabel(st.p);
      rankEl.textContent = st.rank <= 12 ? String(st.rank) : '';
      t.classList.toggle('top', st.rank <= 12);
      t.title = `${c.name}: ${pct(st.p)} (rank ${st.rank})`;
    } else {
      t.style.setProperty('--heat', '0');
      bar.style.width = '0';
      probEl.textContent = '';
      rankEl.textContent = '';
      t.classList.remove('top');
      t.title = st.why ? `${c.name}: ${st.why}` : c.name;
    }
    const ob = st.available && otherBurned.has(c.name);
    t.classList.toggle('burn-other', ob);
    if (ob) t.title += ` · Fearless: ${teamName(other(actingSide))} can no longer use it`;
    rows.push({ t, name: c.name, st });
  }
  if (S.ui.sort === 'prob' && probs) {
    rows.sort((a, b) => {
      if (a.st.available !== b.st.available) return a.st.available ? -1 : 1;
      if (a.st.available) return (a.st.rank || 1e9) - (b.st.rank || 1e9);
      return a.name.localeCompare(b.name);
    });
  } else {
    rows.sort((a, b) => a.name.localeCompare(b.name));
  }
  const grid = $('grid');
  // re-order only if needed (moving nodes is cheap but avoid needless layout)
  const cur = grid.children;
  let same = cur.length === rows.length;
  for (let k = 0; same && k < rows.length; k++) if (cur[k] !== rows[k].t) same = false;
  if (!same) rows.forEach((r) => grid.appendChild(r.t));
  grid.classList.toggle('done', done);
}

function firstVisibleAvailable() {
  for (const t of $('grid').children) if (!t.hidden && !t.classList.contains('off')) return t.dataset.name;
  return null;
}

/* ---------- predictions panel ---------- */

function renderPreds() {
  const list = $('predList');
  list.textContent = '';
  const errBox = $('predError');
  errBox.hidden = !apiError;
  errBox.textContent = apiError ? 'Prediction failed: ' + apiError + (R ? ' (showing the last good result)' : '') : '';
  const done = draftDone();
  if (done) {
    $('predTitle').textContent = 'Draft complete';
    $('predCount').textContent = '';
    list.appendChild(el('li', 'pred-empty muted', 'All 20 actions are in. Record the winner to start the next game.'));
    return;
  }
  const i = nextIndex();
  const side = sideOf(i);
  $('predTitle').textContent = '';
  $('predTitle').append(document.createTextNode('Next: '), el('b', side + '-text', teamName(side)), document.createTextNode(` ${ORDER[i].type} ${nthOfType(i)}`));
  const na = R && R.next_action;
  const fresh = na && na.action_index === i;
  $('predCount').textContent = fresh ? `${na.n_candidates} possible` : '';
  if (!na) { list.appendChild(el('li', 'pred-empty muted', 'Waiting for the model…')); return; }
  list.classList.toggle('stale', !fresh);
  const maxP = na.candidates.length ? na.candidates[0].probability : 1;
  for (const c of na.candidates) {
    const li = el('li', 'pred');
    li.tabIndex = 0;
    li.dataset.name = c.champion;
    li.title = `Apply ${c.champion}`;
    li.appendChild(el('span', 'pred-rank', String(c.rank)));
    li.appendChild(icon(c.champion, 'pred-icon'));
    const body = el('div', 'pred-body');
    const top = el('div', 'pred-top');
    const bar = el('div', 'pred-bar');
    const fill = el('i');
    fill.style.width = (100 * c.probability / maxP).toFixed(1) + '%';
    bar.appendChild(fill);
    top.append(el('span', 'pred-name', c.champion), bar, el('span', 'pred-pct', pct(c.probability)));
    const chips = el('div', 'chips');
    (c.top_factors || []).slice()
      .sort((a, b) => Math.abs(b.shap_relative) - Math.abs(a.shap_relative))
      .slice(0, 3)
      .forEach((f) => {
        const ch = el('span', 'chip ' + (f.direction === 'for' ? 'for' : 'against'),
          `${GROUP_SHORT[f.group] || f.group} ${signed(f.shap_relative)}`);
        ch.title = `${f.group}: ${signed(f.shap_relative, 3)} log-odds vs the field (${f.direction})\n` +
          (f.features || []).map((x) => `  ${x.name} = ${fmtNum(x.value)} (${signed(x.shap_relative, 3)})`).join('\n');
        chips.appendChild(ch);
      });
    body.append(top, chips);
    li.appendChild(body);
    list.appendChild(li);
  }
}
function fmtNum(v) {
  if (typeof v !== 'number') return String(v);
  return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3);
}

/* ---------- win probability ---------- */

function renderWin() {
  const w = R && R.win;
  const blueName = teamName('blue'), redName = teamName('red');
  $('railBlueName').textContent = blueName;
  $('railRedName').textContent = redName;
  const nums = $('winNums');
  nums.textContent = '';
  if (!w) {
    $('evalBluePct').textContent = '–';
    $('evalRedPct').textContent = '–';
    return;
  }
  const pb = w.p_blue_win;
  $('evalBlue').style.height = (pb * 100).toFixed(2) + '%';
  $('evalBluePct').textContent = Math.round(pb * 100) + '%';
  $('evalRedPct').textContent = Math.round((1 - pb) * 100) + '%';
  $('evalBase').style.bottom = (w.p_blue_win_baseline * 100).toFixed(2) + '%';
  $('evalBase').title = `Baseline (team strength + side + first pick): ${blueName} ${pct(w.p_blue_win_baseline)}`;
  $('evalbar').title = `${blueName} ${pct(pb)} · ${redName} ${pct(1 - pb)}\nTick = baseline ${pct(w.p_blue_win_baseline)}\n\n${META.win_note}`;

  const row = el('div', 'win-row');
  const bt = el('div', 'win-team blue');
  bt.append(el('span', 'win-name', blueName), el('span', 'win-pct', pct(pb)));
  const rt = el('div', 'win-team red');
  rt.append(el('span', 'win-name', redName), el('span', 'win-pct', pct(w.p_red_win)));
  row.append(bt, rt);
  const eff = w.draft_effect_blue;
  const effSide = eff >= 0 ? blueName : redName;
  const meta = el('div', 'win-meta muted small');
  meta.textContent = `Baseline ${blueName} ${pct(w.p_blue_win_baseline)} · draft effect ${Math.abs(eff * 100).toFixed(1)} pts toward ${effSide}`;
  nums.append(row, meta);

  renderGraph(w);

  const fx = $('winFactors');
  fx.textContent = '';
  (w.top_factors || []).slice(0, 4).forEach((f) => {
    const toward = f.favours === 'Blue' ? 'blue' : 'red';
    const ch = el('span', 'chip side-' + toward, `${GROUP_SHORT[f.group] || f.group} → ${teamName(toward)} ${Math.abs(f.shap).toFixed(2)}`);
    ch.title = `${f.group}: ${signed(f.shap, 3)} log-odds of a blue win\n` +
      (f.features || []).map((x) => `  ${x.name} = ${fmtNum(x.value)} (${signed(x.shap, 3)})`).join('\n');
    fx.appendChild(ch);
  });
}

function renderGraph(w) {
  const svg = $('winGraph');
  const W = Math.max(100, svg.clientWidth || 300);
  const H = Math.max(60, svg.clientHeight || 110);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const pts = w.p_blue_win_by_action || [];
  const n = ORDER.length;
  const pad = 4;
  const x = (i) => pad + (W - 2 * pad) * (i / n);
  const y = (p) => pad + (H - 2 * pad) * (1 - p);
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  svg.textContent = '';
  svg.appendChild(mk('rect', { x: 0, y: 0, width: W, height: H, class: 'g-bg' }));
  // phase boundaries
  [6, 12, 16].forEach((i) => svg.appendChild(mk('line', { x1: x(i), x2: x(i), y1: 0, y2: H, class: 'g-phase' })));
  [0.25, 0.75].forEach((p) => svg.appendChild(mk('line', { x1: 0, x2: W, y1: y(p), y2: y(p), class: 'g-grid' })));
  svg.appendChild(mk('line', { x1: 0, x2: W, y1: y(0.5), y2: y(0.5), class: 'g-half' }));
  if (pts.length) {
    const line = pts.map((p, i) => `${x(i).toFixed(1)},${y(p).toFixed(1)}`);
    const last = pts.length - 1;
    // blue area below the line, red above (higher = better for blue, like the bar)
    svg.appendChild(mk('path', { d: `M${x(0)},${H} L${line.join(' L')} L${x(last)},${H} Z`, class: 'g-blue' }));
    svg.appendChild(mk('path', { d: `M${x(0)},0 L${line.join(' L')} L${x(last)},0 Z`, class: 'g-red' }));
    svg.appendChild(mk('line', { x1: 0, x2: W, y1: y(w.p_blue_win_baseline), y2: y(w.p_blue_win_baseline), class: 'g-base' }));
    svg.appendChild(mk('polyline', { points: line.join(' '), class: 'g-line' }));
    pts.forEach((p, i) => {
      const c = mk('circle', { cx: x(i), cy: y(p), r: i === last ? 3.5 : 2, class: i === last ? 'g-dot last' : 'g-dot' });
      const t = mk('title', {});
      t.textContent = (i === 0 ? 'Before the draft' : `After action ${i} (${S.actions[i - 1] || 'missed ban'})`) + `: ${teamName('blue')} ${pct(p)}`;
      c.appendChild(t);
      svg.appendChild(c);
    });
  }
}

/* ---------- series ---------- */

function renderSeries() {
  const box = $('series');
  box.textContent = '';
  const n = S.previous.length;
  const lbl = el('span', 'series-game', `Game ${n + 1}`);
  box.appendChild(lbl);
  if (!n) {
    box.appendChild(el('span', 'muted small', 'series not started'));
    return;
  }
  const b = S.blue.team.trim(), r = S.red.team.trim();
  const wb = S.previous.filter((g) => g.winner === b).length;
  const wr = S.previous.filter((g) => g.winner === r).length;
  const score = el('span', 'series-score');
  score.append(el('span', 'blue-text', `${teamName('blue')} ${wb}`), document.createTextNode(' – '), el('span', 'red-text', `${wr} ${teamName('red')}`));
  box.appendChild(score);
  S.previous.forEach((g, k) => {
    const wonSide = g.winner === b ? 'blue' : g.winner === r ? 'red' : '';
    const chip = el('span', 'game-chip ' + (wonSide ? 'side-' + wonSide : ''), `G${k + 1} ${g.winner}`);
    const picks = g.actions.filter((a, i) => ORDER[i].type === 'pick' && a);
    chip.title = `Game ${k + 1}: ${g.blue_team} (blue) vs ${g.red_team} (red), first pick ${g.first_pick_team}. Winner: ${g.winner}\nPicks: ${picks.join(', ')}`;
    box.appendChild(chip);
  });
  const rm = el('button', 'btn small ghost', 'Remove last');
  rm.type = 'button';
  rm.title = `Remove game ${n} from the series`;
  rm.onclick = removeLastGame;
  const rs = el('button', 'btn small ghost', 'Reset series');
  rs.type = 'button';
  rs.onclick = resetSeries;
  box.append(rm, rs);
}

/* ---------- render all ---------- */

function render() {
  renderSetup();
  SIDES.forEach(renderTeam);
  renderStatus();
  renderGrid();
  renderPreds();
  renderWin();
  renderSeries();
  renderNotices();
}

/* ---------- events ---------- */

function bindEvents() {
  $('league').addEventListener('change', (e) => { S.league = e.target.value; commit(); });
  $('patch').addEventListener('change', (e) => { S.patch = e.target.value; commit(); });
  bindSeg('fearless', (v) => { S.fearless = v; commit(); });
  bindSeg('firstPick', (v) => { if (S.firstPick !== v) { S.firstPick = v; commit(); } });
  bindSeg('roleFilter', (v) => { S.ui.role = v; save(); renderSetup(); renderGrid(); });
  bindSeg('sortMode', (v) => { S.ui.sort = v; save(); renderSetup(); renderGrid(); });
  $('swapSides').addEventListener('click', swapSides);
  $('undoBtn').addEventListener('click', undo);
  $('missedBtn').addEventListener('click', () => applyAction(null));
  $('newDraftBtn').addEventListener('click', newDraft);
  $('winBlue').addEventListener('click', () => recordWinner('blue'));
  $('winRed').addEventListener('click', () => recordWinner('red'));

  const search = $('search');
  search.addEventListener('input', renderGrid);
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const name = firstVisibleAvailable();
      if (name && search.value.trim()) {
        search.value = '';
        applyAction(name);
      }
    } else if (e.key === 'Escape') {
      search.value = '';
      renderGrid();
      search.blur();
    }
  });

  $('predList').addEventListener('click', (e) => {
    const li = e.target.closest('.pred');
    if (li) applyAction(li.dataset.name);
  });
  $('predList').addEventListener('keydown', (e) => {
    const li = e.target.closest('.pred');
    if (li && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); applyAction(li.dataset.name); }
  });

  const pop = $('caveatPop');
  pop.textContent = META.win_note;
  const togglePop = (show) => {
    pop.hidden = show === undefined ? !pop.hidden : !show;
    $('caveatBtn').setAttribute('aria-expanded', String(!pop.hidden));
  };
  $('caveatBtn').addEventListener('click', (e) => { e.stopPropagation(); togglePop(); });
  $('railInfo').addEventListener('click', (e) => { e.stopPropagation(); togglePop(); });
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target)) togglePop(false);
    const wl = document.querySelector('.warn-list');
    if (wl && !wl.hidden && !wl.contains(e.target)) wl.hidden = true;
  });

  document.addEventListener('keydown', (e) => {
    const t = e.target;
    const editing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'z') {
      if (editing && !(t === search && !search.value)) return; // let text fields undo their own text
      e.preventDefault();
      undo();
    } else if (e.key === '/' && !editing) {
      e.preventDefault();
      search.focus();
    }
  });

  let rz = 0;
  window.addEventListener('resize', () => {
    clearTimeout(rz);
    rz = setTimeout(() => { if (R && R.win) renderGraph(R.win); }, 100);
  });
  // another tab changed the state
  window.addEventListener('storage', (e) => {
    if (e.key !== STORE_KEY) return;
    S = sanitize(loadStored());
    render();
    schedulePredict();
  });
}

/* ---------- boot ---------- */

async function boot() {
  try {
    const resp = await fetch('/api/meta');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    META = await resp.json();
  } catch (e) {
    document.body.classList.add('boot-failed');
    $('status').textContent = 'Could not load /api/meta (' + e.message + '). Is the server running? Reload to retry.';
    return;
  }
  ORDER = META.draft_order;
  META.champions.sort((a, b) => a.name.localeCompare(b.name));
  META.champions.forEach((c) => CHAMP.set(c.name, c));
  META.teams.forEach((t) => TEAM.set(t.name.toLowerCase(), t));
  $('brand').title = `Model data through ${META.data_through}`;

  S = sanitize(loadStored());
  save();
  SIDES.forEach(buildTeamColumn);
  buildGrid();
  bindEvents();
  render();
  schedulePredict();
}

boot();
