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
// a factor group in words: [when it counts for the champion, when it counts against]
const REASON = {
  'champion meta strength': ['high priority this patch', 'low priority this patch'],
  'player comfort': ['player comfort', 'low player comfort'],
  'role fit': ['fits an open role', 'poor role fit'],
  'synergy/counter': ['goes with what’s drafted', 'clashes with what’s drafted'],
  'team tendency': ['these teams favour it', 'not these teams’ habit'],
  'draft context': ['suits this stage', 'doesn’t suit this stage'],
  'series context': ['series so far favours it', 'series so far counts against'],
  'team strength': ['team strength', 'team strength counts against'],
  'champion attributes': ['champion type fits', 'champion type fits less'],
};
const GROUP_LABEL = {
  'champion meta strength': 'Meta strength',
  'player comfort': 'Player comfort',
  'role fit': 'Role fit',
  'synergy/counter': 'Synergy / counter',
  'team tendency': 'Team tendency',
  'draft context': 'Draft context',
  'series context': 'Series context',
  'team strength': 'Team strength',
  'champion attributes': 'Champion type',
};
const groupLabel = (g) => GROUP_LABEL[g] || g;
// What one feature says about a champion, in words: [when it is above the field on that
// feature, when it is below]. The field is the available champions weighted by how likely each
// is, the same comparison the factor numbers use. {A} is the team acting now, {O} its opponent.
// A reason uses a fact only when the fact points the way the reason does (above the field and
// counting for, or below it and counting against); otherwise it falls back to REASON.
const FACT = {
  team_pick_rate_own: ['{A} pick it often', '{A} rarely pick it'],
  team_pick_rate_own_recent: ['{A} picked it lately', '{A} haven’t picked it lately'],
  team_pick_rate_opp: ['{O} pick it often', '{O} rarely pick it'],
  team_pick_rate_opp_recent: ['{O} picked it lately', '{O} haven’t picked it lately'],
  team_ban_rate_own: ['{A} often ban it', '{A} rarely ban it'],
  team_ban_rate_opp: ['{O} often ban it', '{O} rarely ban it'],
  team_banned_against_own: ['often banned against {A}', 'rarely banned against {A}'],
  team_banned_against_opp: ['often banned against {O}', 'rarely banned against {O}'],
  player_games_own: ['a {A} comfort pick', 'not a {A} comfort pick'],
  player_games_own_max: ['a {A} comfort pick', 'not a {A} comfort pick'],
  player_career_own: ['a {A} comfort pick', 'not a {A} comfort pick'],
  player_share_own: ['a {A} comfort pick', 'not a {A} comfort pick'],
  player_win_resid_own: ['{A} players win on it', '{A} players struggle on it'],
  player_pool_share_left_own: ['much of {A}’s remaining pool', 'little of {A}’s remaining pool'],
  player_games_opp: ['a {O} comfort pick', 'not a {O} comfort pick'],
  player_games_opp_max: ['a {O} comfort pick', 'not a {O} comfort pick'],
  player_career_opp: ['a {O} comfort pick', 'not a {O} comfort pick'],
  player_share_opp: ['a {O} comfort pick', 'not a {O} comfort pick'],
  player_win_resid_opp: ['{O} players win on it', '{O} players struggle on it'],
  player_pool_share_left_opp: ['much of {O}’s remaining pool', 'little of {O}’s remaining pool'],
  meta_presence: ['popular this patch', 'unpopular this patch'],
  meta_presence_patch_raw: ['popular this patch', 'unpopular this patch'],
  meta_presence_recent: ['popular lately', 'out of favour lately'],
  meta_presence_long: ['a long-time priority', 'rarely a priority'],
  meta_presence_league: ['popular in this league', 'unpopular in this league'],
  meta_pick_rate: ['picked a lot this patch', 'rarely picked this patch'],
  meta_ban_rate: ['banned a lot this patch', 'rarely banned this patch'],
  meta_rate_type_pick: ['picked a lot this patch', 'rarely picked this patch'],
  meta_rate_type_ban: ['banned a lot this patch', 'rarely banned this patch'],
  meta_rate_phase_slot: ['often taken at this stage', 'rarely taken at this stage'],
  meta_rate_action: ['often taken at this turn', 'rarely taken at this turn'],
  meta_win_resid: ['wins more than expected', 'wins less than expected'],
  meta_win_resid_patch: ['winning this patch', 'losing this patch'],
  role_fit_own: ['fills a role {A} need', '{A} have that role covered'],
  role_fit_opp: ['fills a role {O} need', '{O} have that role covered'],
  pair_colift_own: ['pairs with {A}’s picks', 'rarely paired with {A}’s picks'],
  pair_colift_opp: ['pairs with {O}’s picks', 'rarely paired with {O}’s picks'],
  pair_vslift_opp: ['often drafted into {O}’s picks', 'rarely drafted into {O}’s picks'],
  pair_vslift_own: ['often drafted into {A}’s picks', 'rarely drafted into {A}’s picks'],
  pair_syn_own: ['wins with {A}’s picks', 'loses with {A}’s picks'],
  pair_syn_opp: ['wins with {O}’s picks', 'loses with {O}’s picks'],
  pair_ctr_vs_opp: ['beats {O}’s picks', 'loses to {O}’s picks'],
  pair_ctr_vs_own: ['beats {A}’s picks', 'loses to {A}’s picks'],
  series_ban_own_prev: ['{A} banned it earlier this series', null],
  series_ban_opp_prev: ['{O} banned it earlier this series', null],
};

let META = null;            // GET /api/meta
let ORDER = [];             // META.draft_order
let CHAMP = new Map();      // name -> champion
let TEAM = new Map();       // lower-case name -> team
let S = null;               // persistent state
let R = null;               // last accepted prediction response
let reqSeq = 0;             // id of the latest request sent
let predictTimer = 0;
let busyRetries = 0;        // consecutive "busy, try again" answers for the current request
let apiError = '';
let tiles = new Map();      // name -> tile element
let booted = false;         // false during the first render: nothing animates in on page load
let movedFor = { grid: '', preds: '' }; // the draft position each list last animated for
const REDUCED_MOTION = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

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
// splash art for a pick slot; falls back to the square icon if the art cannot be loaded
function splash(name, cls) {
  const img = el('img', cls);
  const c = CHAMP.get(name);
  img.alt = '';
  img.draggable = false;
  if (c) {
    img.onerror = () => { img.onerror = null; img.src = `/img/champion/${encodeURIComponent(c.id)}.png`; };
    img.src = `/img/splash/${encodeURIComponent(c.id)}.jpg`;
  }
  return img;
}
// FLIP: remember where keyed elements are, change the DOM, then slide each one from its old place
function rectsOf(nodes) {
  const m = new Map();
  for (const n of nodes) if (!n.hidden) m.set(n.dataset.name, n.getBoundingClientRect());
  return m;
}
function slideFrom(before, nodes) {
  if (REDUCED_MOTION || !before || !before.size) return;
  for (const n of nodes) {
    if (n.hidden || !n.animate) continue;
    const a = before.get(n.dataset.name);
    if (!a) { n.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 260, easing: 'ease-out' }); continue; }
    const b = n.getBoundingClientRect();
    const dx = a.left - b.left, dy = a.top - b.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    n.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], { duration: 320, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
  }
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
  let retryIn = 0;
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
    // the server is busy or this address has used its minute: wait as long as it asks, then ask again
    if (resp.status === 503 || resp.status === 429) retryIn = Math.min(60, Math.max(1, parseInt(resp.headers.get('Retry-After') || '3', 10) || 3));
  } catch (e) {
    err = 'Cannot reach the server (' + e.message + ')';
  }
  if (seq !== reqSeq) return; // a newer request is in flight: drop this one
  if (retryIn && busyRetries < 8) {
    busyRetries++;
    apiError = err;
    renderNotices();
    clearTimeout(predictTimer);
    predictTimer = setTimeout(predict, retryIn * 1000);
    return; // still "loading": the last good numbers stay on screen until the retry answers
  }
  busyRetries = 0;
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
  const az = S.ui.sort === 'az';
  $('sortToggle').classList.toggle('on', az);
  $('sortToggle').setAttribute('aria-pressed', String(az));
  const fearless = { hard: 'Hard Fearless', soft: 'Soft Fearless', none: 'No Fearless' }[S.fearless];
  const sb = $('setupBtn');
  sb.textContent = '';
  sb.append(el('b', null, S.league), document.createTextNode(` · Patch ${S.patch} · ${fearless}`), el('span', 'caret', '▾'));
}

/* ---------- team columns ---------- */

function buildTeamColumn(side) {
  const col = side === 'blue' ? $('teamBlue') : $('teamRed');
  col.textContent = '';

  const top = el('div', 'team-top');
  top.appendChild(el('span', 'team-rule'));
  const head = el('div', 'team-head');
  const fpTag = el('div', 'fp-tag', 'first pick');
  fpTag.dataset.role = 'fp';

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
  head.append(combo, fpTag);
  bindCombo(side, input, list);

  const score = el('div', 'team-score');
  score.dataset.role = 'score';
  top.append(head, score);
  col.appendChild(top);

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

  const bans = el('div', 'bans');
  bans.appendChild(el('span', 'bans-label', 'Bans'));
  for (let k = 0; k < 5; k++) {
    if (k === 3) bans.appendChild(el('div', 'ban-gap'));
    const s = el('div', 'ban slot');
    s.dataset.slot = 'ban' + k;
    bans.appendChild(s);
  }
  col.appendChild(bans);

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
  // The list is placed on the viewport, not inside the team column: the column scrolls and would
  // cut it off. It hangs under the name, opening toward the middle of the page.
  const place = () => {
    const r = input.getBoundingClientRect();
    const w = Math.min(Math.max(r.width, 320), window.innerWidth - 16);
    const left = Math.max(8, Math.min(side === 'red' ? r.right - w : r.left, window.innerWidth - w - 8));
    const top = r.bottom + 4;
    list.style.width = w + 'px';
    list.style.left = Math.round(left) + 'px';
    list.style.top = Math.round(top) + 'px';
    list.style.maxHeight = Math.max(140, Math.min(420, window.innerHeight - top - 12)) + 'px';
  };
  const open = () => { fill(false); list.hidden = false; place(); };
  const close = () => { list.hidden = true; active = -1; };
  const follow = () => { if (!list.hidden) place(); };
  window.addEventListener('resize', follow);
  window.addEventListener('scroll', follow, { passive: true });
  $(side === 'blue' ? 'teamBlue' : 'teamRed').addEventListener('scroll', follow, { passive: true });
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
  input.addEventListener('input', () => { fill(true); list.hidden = false; place(); if (input.value.trim()) highlight(0); });
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

  for (let i = 0; i < ORDER.length; i++) {
    const { side: sd, type, k } = slotOf(i);
    if (sd !== side) continue;
    const s = col.querySelector(`[data-slot=${type}${k}]`);
    const info = S.actionInfo[i];
    const key = i < S.actions.length ? `filled|${i}|${S.actions[i]}|${info ? info.rank + '/' + info.p : ''}`
      : i === S.actions.length ? 'next' : 'empty';
    if (s.dataset.key === key) continue;
    const wasFilled = (s.dataset.key || '').startsWith('filled');
    s.dataset.key = key;
    s.className = `${type} slot`;
    s.textContent = '';
    s.removeAttribute('title');
    if (i < S.actions.length) {
      fillSlot(s, i, type);
      if (booted && !wasFilled) s.classList.add('just');
    } else if (i === S.actions.length) {
      s.classList.add('next');
      s.appendChild(el('span', 'slot-next', type === 'ban' ? 'Banning' : 'Picking'));
    } else {
      s.classList.add('empty');
    }
  }

  // Fearless: champions this team cannot pick in this game
  const bx = col.querySelector('[data-role=burned]');
  bx.textContent = '';
  const burned = burnedFor(side);
  if (S.fearless !== 'none' && burned.length) {
    bx.appendChild(el('div', null, `Used earlier in the series (${burned.length})`));
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
  s.appendChild(type === 'pick' ? splash(name, 'slot-art') : icon(name, 'slot-icon'));
  const modelTxt = info ? `model gave it ${probLabel(info.p)} (rank ${info.rank})` : '';
  s.title = `Action ${i + 1}: ${name}${modelTxt ? ' · ' + modelTxt : ''}`;
  if (type === 'pick') {
    const txt = el('div', 'slot-text');
    txt.appendChild(el('div', 'slot-name', name));
    if (info) {
      // how the model rated this pick just before it happened
      const meta = el('div', 'slot-meta' + (info.rank > 10 ? ' surprise' : ''), `${probLabel(info.p)} · rank ${info.rank}`);
      txt.appendChild(meta);
    }
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
  $('missedBtn').hidden = done || ORDER[i].type !== 'ban';
  renderTimeline();
  if (done) {
    st.appendChild(el('span', 'status-main', 'Draft complete'));
    $('winBlue').textContent = teamName('blue') + ' won';
    $('winRed').textContent = teamName('red') + ' won';
    document.body.dataset.acting = '';
    return;
  }
  const a = ORDER[i];
  const side = sideOf(i);
  document.body.dataset.acting = side;
  const main = el('span', 'status-main');
  main.append(el('b', side + '-text', teamName(side)), document.createTextNode(` to ${a.type}`));
  const sub = el('span', 'status-sub', `${phaseLabel(a.phase)} · ${i + 1} of ${ORDER.length}`);
  st.append(main, sub);
}

// one mark per draft action: short = ban, tall = pick, coloured by the side that acts
function renderTimeline() {
  const box = $('timeline');
  box.textContent = '';
  const n = nextIndex();
  ORDER.forEach((a, i) => {
    if (i && a.phase !== ORDER[i - 1].phase) box.appendChild(el('span', 'tl-gap'));
    const side = sideOf(i);
    const state = i < n ? 'done' : i === n ? 'now' : 'todo';
    const m = el('span', `tl-step tl-${a.type} tl-${side} tl-${state}`);
    const what = i < n ? ': ' + (S.actions[i] || 'missed ban') : i === n ? ' (now)' : '';
    m.title = `${i + 1}. ${teamName(side)} ${a.type} ${nthOfType(i)}${what}`;
    box.appendChild(m);
  });
}
function phaseLabel(ph) {
  return { ban1: 'Ban phase 1', pick1: 'Pick phase 1', ban2: 'Ban phase 2', pick2: 'Pick phase 2' }[ph] || ph;
}

/* ---------- champion grid ---------- */

const LIKELY_MIN_P = 0.01;  // "Most likely" holds every champion at or above this probability...
const LIKELY_MIN_N = 6;     // ...but never fewer than this many
const LIKELY_MAX_N = 28;

function buildGrid() {
  const box = $('likelyBox');
  for (const c of META.champions) {
    const t = el('button', 'tile');
    t.type = 'button';
    t.dataset.name = c.name;
    t.setAttribute('role', 'listitem');
    t.appendChild(icon(c.name, 'tile-icon'));
    t.appendChild(el('span', 'tile-reason'));
    t.appendChild(el('span', 'tile-name', c.name));
    t.appendChild(el('span', 'tile-prob'));
    t.appendChild(el('span', 'tile-bar'));
    tiles.set(c.name, t);
    box.appendChild(t);
  }
  $('grid').addEventListener('click', (e) => {
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

function placeTiles(box, list) {
  const cur = box.children;
  let same = cur.length === list.length;
  for (let k = 0; same && k < list.length; k++) if (cur[k] !== list[k]) same = false;
  if (!same) box.replaceChildren(...list);
}

function renderGrid() {
  const grid = $('grid');
  const probs = currentProbs();
  const done = draftDone();
  const actingSide = done ? null : sideOf(nextIndex());
  const q = norm($('search').value);
  const role = S.ui.role;
  // the numbers for this state are on their way: keep the layout still instead of re-sorting twice
  const waiting = !probs && !done && !!R && !apiError;
  grid.classList.toggle('waiting', waiting);
  if (waiting) {
    for (const c of META.champions) {
      const t = tiles.get(c.name);
      t.hidden = !((!q || norm(c.name).includes(q)) && roleMatch(c, role));
      if (usedThisGame(c.name)) t.classList.add('off');
    }
    updateGridHeads();
    return;
  }
  const maxP = probs && R.next_action.probabilities.length ? R.next_action.probabilities[0].probability : 1;
  const otherBurned = actingSide ? new Set(burnedFor(other(actingSide))) : new Set();
  const rows = [];
  for (const c of META.champions) {
    const t = tiles.get(c.name);
    const st = champStatus(c.name, probs, actingSide);
    t.hidden = !((!q || norm(c.name).includes(q)) && roleMatch(c, role));
    t.classList.toggle('off', !st.available);
    t.setAttribute('aria-disabled', String(!st.available));
    t.querySelector('.tile-reason').textContent = st.reason || '';
    const probEl = t.querySelector('.tile-prob');
    const bar = t.querySelector('.tile-bar');
    if (st.available && st.p != null) {
      t.style.setProperty('--heat', Math.min(1, Math.sqrt(st.p / maxP)).toFixed(3));
      bar.style.width = (100 * st.p / maxP).toFixed(1) + '%';
      probEl.textContent = st.p >= 0.001 ? probLabel(st.p) : '';
      t.title = `${c.name}: ${pct(st.p)} (rank ${st.rank})`;
    } else {
      t.style.setProperty('--heat', '0');
      bar.style.width = '0';
      probEl.textContent = '';
      t.title = st.why ? `${c.name}: ${st.why}` : c.name;
    }
    const ob = st.available && otherBurned.has(c.name);
    t.classList.toggle('burn-other', ob);
    if (ob) t.title += ` · Fearless: ${teamName(other(actingSide))} can no longer use it`;
    rows.push({ t, name: c.name, st });
  }

  const sectioned = S.ui.sort === 'prob' && !!probs;
  grid.classList.toggle('flat', !sectioned);
  grid.classList.toggle('done', done);
  if (!sectioned) {
    rows.sort((a, b) => a.name.localeCompare(b.name));
    placeTiles($('restBox'), []);
    placeTiles($('offBox'), []);
    placeTiles($('likelyBox'), rows.map((r) => r.t));
    updateGridHeads();
    return;
  }
  const avail = rows.filter((r) => r.st.available).sort((a, b) => a.st.rank - b.st.rank);
  let nLikely = avail.filter((r) => r.st.p >= LIKELY_MIN_P).length;
  nLikely = Math.min(avail.length, LIKELY_MAX_N, Math.max(nLikely, LIKELY_MIN_N));
  const reasonOrder = { Banned: 0, Picked: 0, Fearless: 1 };
  const off = rows.filter((r) => !r.st.available).sort((a, b) =>
    ((reasonOrder[a.st.reason] ?? 2) - (reasonOrder[b.st.reason] ?? 2)) || a.name.localeCompare(b.name));
  const likely = avail.slice(0, nLikely).map((r) => r.t);
  const moved = movedFor.grid !== String(nextIndex());
  const before = moved && booted && movedFor.grid !== '' ? rectsOf($('likelyBox').children) : null;
  placeTiles($('likelyBox'), likely);
  placeTiles($('restBox'), avail.slice(nLikely).map((r) => r.t));
  placeTiles($('offBox'), off.map((r) => r.t));
  if (moved) { movedFor.grid = String(nextIndex()); slideFrom(before, likely); }
  const covered = avail.slice(0, nLikely).reduce((a, r) => a + r.st.p, 0);
  $('likelyNote').textContent = `${nLikely} champions cover ${Math.round(covered * 100)}% of the probability`;
  updateGridHeads();
}

// section headings follow what the search and role filter leave visible
function updateGridHeads() {
  const flat = $('grid').classList.contains('flat');
  let total = 0;
  for (const [head, box, note] of [['likelyHead', 'likelyBox', null], ['restHead', 'restBox', 'restNote'], ['offHead', 'offBox', 'offNote']]) {
    let n = 0;
    for (const t of $(box).children) if (!t.hidden) n++;
    total += n;
    $(head).hidden = flat || !n;
    $(box).hidden = !n;
    if (note) $(note).textContent = String(n);
  }
  $('gridEmpty').hidden = total > 0;
  // the coverage note describes the whole section, so it goes away while a filter hides part of it
  $('likelyNote').hidden = !!norm($('search').value) || S.ui.role !== 'all';
}

function firstVisibleAvailable() {
  for (const t of $('grid').querySelectorAll('.tile')) if (!t.hidden && !t.classList.contains('off')) return t.dataset.name;
  return null;
}

/* ---------- recap: once the draft is complete, the champion grid gives way to how the model rated each action ---------- */

function renderRecap() {
  const done = draftDone();
  $('grid').hidden = done;
  $('toolbar').hidden = done;
  const box = $('recap');
  box.hidden = !done;
  box.textContent = '';
  if (!done) return;
  const rated = S.actionInfo.filter((x, i) => x && S.actions[i]);
  const top1 = rated.filter((x) => x.rank === 1).length;
  const top5 = rated.filter((x) => x.rank <= 5).length;
  const head = el('div', 'sec-head');
  head.append(el('span', null, 'How the model rated each action'),
    el('span', 'sec-note', rated.length ? `first choice ${top1} of ${rated.length} · in its top 5 ${top5} of ${rated.length}` : ''));
  box.appendChild(head);
  const cols = el('div', 'recap-cols');
  let col = null;
  ORDER.forEach((a, i) => {
    if (!i || a.phase !== ORDER[i - 1].phase) {
      if (a.phase === 'ban1' || a.phase === 'ban2') { col = el('div', 'recap-col'); cols.appendChild(col); }
      col.appendChild(el('div', 'recap-phase', phaseLabel(a.phase)));
    }
    const side = sideOf(i);
    const name = S.actions[i];
    const info = name ? S.actionInfo[i] : null;
    const row = el('div', `recap-row rc-${side} rc-${a.type}`);
    row.title = `${i + 1}. ${teamName(side)} ${a.type}` + (name ? `: ${name}` : ': missed ban');
    row.append(el('span', 'recap-n', String(i + 1)), name ? icon(name, 'recap-icon') : el('span', 'recap-icon'),
      el('span', 'recap-name', name || 'No ban'),
      el('span', 'recap-p' + (info && info.rank > 10 ? ' surprise' : ''), info ? `${probLabel(info.p)} · rank ${info.rank}` : '–'));
    col.appendChild(row);
  });
  box.appendChild(cols);
}

/* ---------- predictions panel ---------- */

// One factor group in words. The specific version states a fact about the feature that drives
// the group (the strongest one pushing the same way as the group) and how the champion compares
// with the field on it; without a fitting fact it falls back to the group's stock phrase.
function reasonWords(f, actionType, acting) {
  const pos = f.shap_relative >= 0;
  const driver = (f.features || []).find((x) => (x.shap_relative >= 0) === pos);
  if (driver && driver.field_mean != null && typeof driver.value === 'number') {
    const key = driver.name === 'meta_rate_type' ? `meta_rate_type_${actionType}` : driver.name;
    const diff = driver.value - driver.field_mean;
    const agrees = Math.abs(diff) > 1e-6 && (diff > 0) === pos;
    const fact = FACT[key] && agrees ? FACT[key][pos ? 0 : 1] : null;
    if (fact) return fact.replace('{A}', teamName(acting)).replace('{O}', teamName(other(acting)));
  }
  return (REASON[f.group] || [f.group, 'less ' + f.group])[pos ? 0 : 1];
}
const capFirst = (t) => t[0].toUpperCase() + t.slice(1);

// what speaks for the champion first, then what speaks against it; strongest first within each
function reasons(c, actionType, acting) {
  const fs = sortedFactors(c);
  const ordered = fs.filter((f) => f.shap_relative >= 0).concat(fs.filter((f) => f.shap_relative < 0));
  const seen = new Set();
  const out = [];
  for (const f of ordered) {
    const words = reasonWords(f, actionType, acting);
    if (seen.has(words)) continue;
    seen.add(words);
    out.push(el('span', 'reason ' + (f.shap_relative >= 0 ? 'for' : 'against'), out.length ? words : capFirst(words)));
    if (out.length === 3) break;
  }
  return out;
}

function renderPreds() {
  const list = $('predList');
  const before = booted ? rectsOf(list.querySelectorAll('.pred')) : null;
  const widthBefore = new Map();
  list.querySelectorAll('.pred').forEach((li) => widthBefore.set(li.dataset.name, li.querySelector('.pred-bar i').style.width));
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
  $('predTitle').append(document.createTextNode(`Likely ${ORDER[i].type}s for `), el('b', side + '-text', teamName(side)));
  const na = R && R.next_action;
  const fresh = na && na.action_index === i;
  $('predCount').textContent = fresh ? `of ${na.n_candidates} available` : '';
  if (!na) { list.appendChild(el('li', 'pred-empty muted', 'Waiting for the model…')); return; }
  list.classList.toggle('stale', !fresh);
  const maxP = na.candidates.length ? na.candidates[0].probability : 1;
  for (const c of na.candidates) {
    const li = el('li', 'pred');
    li.tabIndex = 0;
    li.dataset.name = c.champion;
    li.setAttribute('aria-label', `${c.champion}, ${pct(c.probability)}. Press Enter to apply.`);
    li.appendChild(el('span', 'pred-rank', String(c.rank)));
    li.appendChild(icon(c.champion, 'pred-icon'));
    const body = el('div', 'pred-body');
    const top = el('div', 'pred-top');
    const bar = el('div', 'pred-bar');
    const fill = el('i');
    fill.style.width = (100 * c.probability / maxP).toFixed(1) + '%';
    bar.appendChild(fill);
    top.append(el('span', 'pred-name', c.champion), bar, el('span', 'pred-pct', pct(c.probability)));
    const why = el('div', 'pred-why');
    why.append(...reasons(c, na.action_type, side));
    body.append(top, why);
    li.appendChild(body);
    list.appendChild(li);
  }
  if (fresh && movedFor.preds !== String(i)) {
    movedFor.preds = String(i);
    slideFrom(before, list.children);
    if (!REDUCED_MOTION && before && before.size) {
      for (const li of list.children) {
        const bar = li.querySelector('.pred-bar i');
        if (bar && bar.animate) bar.animate([{ width: widthBefore.get(li.dataset.name) || '0%' }, { width: bar.style.width }], { duration: 420, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
      }
    }
  }
}
function sortedFactors(c) {
  return (c.top_factors || []).slice().sort((a, b) => Math.abs(b.shap_relative) - Math.abs(a.shap_relative));
}

/* ---------- "why" card: the full factor breakdown of one candidate, shown on hover or focus ---------- */

function candidate(name) {
  const na = R && R.next_action;
  if (!na || na.action_index !== nextIndex()) return null;
  return na.candidates.find((c) => c.champion === name) || null;
}

function showWhy(anchor) {
  const card = $('why');
  const c = candidate(anchor.dataset.name);
  if (!c) { hideWhy(); return; }
  if (!card.hidden && card.dataset.name === c.champion) return;
  card.dataset.name = c.champion;
  const na = R.next_action;
  const side = sideOf(nextIndex());
  card.textContent = '';
  const head = el('div', 'why-head');
  const ttl = el('div', 'why-title');
  ttl.append(el('div', 'why-name', c.champion),
    el('div', 'muted small', `Rank ${c.rank} of ${na.n_candidates} for ${teamName(side)}'s ${na.action_type}`));
  head.append(icon(c.champion, 'why-icon'), ttl, el('div', 'why-pct', pct(c.probability)));
  card.appendChild(head);
  card.appendChild(el('div', 'why-sub', 'Compared with the other likely choices'));
  const factors = sortedFactors(c);
  const scale = Math.max(0.5, ...factors.map((f) => Math.abs(f.shap_relative)));
  for (const f of factors) {
    const pos = f.shap_relative >= 0;
    const row = el('div', 'why-row');
    const track = el('div', 'why-track');
    const fill = el('i', pos ? 'for' : 'against');
    fill.style.width = (50 * Math.abs(f.shap_relative) / scale).toFixed(1) + '%';
    track.appendChild(fill);
    row.append(el('span', 'why-group', groupLabel(f.group)), track, el('b', pos ? 'for' : 'against', signed(f.shap_relative)));
    card.appendChild(row);
    card.appendChild(el('div', 'why-words', capFirst(reasonWords(f, na.action_type, side))));
    const feats = el('div', 'why-feats');
    (f.features || []).slice(0, 3).forEach((x) => {
      const line = el('div', 'why-feat');
      line.append(el('span', null, `${x.name} = ${fmtNum(x.value)}`), el('span', null, signed(x.shap_relative)));
      feats.appendChild(line);
    });
    card.appendChild(feats);
  }
  card.appendChild(el('div', 'why-foot muted', 'Numbers are log-odds: +0.7 roughly doubles the odds, −0.7 halves them. Click the row to enter this champion.'));

  card.hidden = false;
  const r = anchor.getBoundingClientRect();
  const panel = anchor.closest('.card').getBoundingClientRect();
  const w = card.offsetWidth, h = card.offsetHeight;
  let left = panel.left - w - 14;
  if (left < 8) left = Math.max(8, Math.min(window.innerWidth - w - 8, panel.right + 10));
  let top = Math.max(8, Math.min(r.top - 10, window.innerHeight - h - 8));
  if (left + w > panel.left && r.bottom + h + 8 < window.innerHeight) top = r.bottom + 6; // no room beside the list: go below the row
  card.style.left = Math.round(left) + 'px';
  card.style.top = Math.round(top) + 'px';
}
function hideWhy() { $('why').hidden = true; $('why').dataset.name = ''; }

function fmtNum(v) {
  if (typeof v !== 'number') return String(v);
  return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3);
}

/* ---------- win probability ---------- */

let shownPb = null;   // the blue win probability the numbers currently show
let pbFrame = 0;
// set every blue/red percentage on the page to p, counting from the value shown before
function countWinTo(p, paint) {
  cancelAnimationFrame(pbFrame);
  const from = shownPb == null || REDUCED_MOTION ? p : shownPb;
  const t0 = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - t0) / 600);
    const e = 1 - Math.pow(1 - k, 3);
    shownPb = from + (p - from) * e;
    paint(shownPb);
    if (k < 1) pbFrame = requestAnimationFrame(step);
  };
  if (Math.abs(p - from) < 0.0005) { shownPb = p; paint(p); } else pbFrame = requestAnimationFrame(step);
}

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
  $('evalbar').style.setProperty('--pb', (pb * 100).toFixed(2));
  $('evalbar').style.setProperty('--base', (w.p_blue_win_baseline * 100).toFixed(2));
  $('evalBase').title = `Baseline (team strength + side + first pick): ${blueName} ${pct(w.p_blue_win_baseline)}`;
  $('evalbar').title = `${blueName} ${pct(pb)} · ${redName} ${pct(1 - pb)}\nLight tick = baseline ${pct(w.p_blue_win_baseline)} (team strength, side, first pick)\n\n${META.win_note}`;

  const row = el('div', 'win-row');
  const bt = el('div', 'win-team blue');
  const bluePct = el('span', 'win-pct'), redPct = el('span', 'win-pct');
  bt.append(el('span', 'win-name', blueName), bluePct);
  const rt = el('div', 'win-team red');
  rt.append(el('span', 'win-name', redName), redPct);
  row.append(bt, rt);
  countWinTo(pb, (p) => {
    bluePct.textContent = pct(p);
    redPct.textContent = pct(1 - p);
    $('evalBluePct').textContent = Math.round(p * 100) + '%';
    $('evalRedPct').textContent = Math.round((1 - p) * 100) + '%';
  });

  // where the number comes from: the baseline, then the draft's (unvalidated) shift
  const eff = w.draft_effect_blue;
  const effSide = eff >= 0 ? 'blue' : 'red';
  const meta = el('div', 'win-meta');
  const base = el('span', 'win-stat');
  base.title = 'Team strength, side and first pick only';
  base.append(el('span', 'muted', 'Baseline '), el('b', null, pct(w.p_blue_win_baseline)));
  const draft = el('span', 'win-stat');
  draft.append(el('span', 'muted', 'Draft '), el('b', effSide + '-text', `${Math.abs(eff * 100).toFixed(1)} pts → ${teamName(effSide)}`));
  const cav = el('button', 'caveat');
  cav.type = 'button';
  cav.id = 'caveatBtn';
  cav.setAttribute('aria-expanded', String(!$('caveatPop').hidden));
  cav.append(document.createTextNode('not validated'), el('span', 'info-dot', 'i'));
  draft.appendChild(cav);
  meta.append(base, draft);
  nums.append(row, meta);

  renderGraph(w);

  const fx = $('winFactors');
  fx.textContent = '';
  const factors = (w.top_factors || []).slice(0, 4);
  const scale = Math.max(0.2, ...factors.map((f) => Math.abs(f.shap)));
  factors.forEach((f) => {
    const toward = f.favours === 'Blue' ? 'blue' : 'red';
    const r = el('div', 'wf-row');
    const track = el('div', 'wf-track');
    const fill = el('i', toward);
    fill.style.width = Math.max(2, 100 * Math.abs(f.shap) / scale).toFixed(1) + '%';
    track.appendChild(fill);
    r.append(el('span', 'wf-group', groupLabel(f.group)), track, el('span', 'wf-team ' + toward + '-text', teamName(toward)));
    r.title = `${f.group}: ${signed(f.shap, 3)} log-odds of a blue win\n` +
      (f.features || []).map((x) => `  ${x.name} = ${fmtNum(x.value)} (${signed(x.shap, 3)})`).join('\n');
    fx.appendChild(r);
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
  if (!n) return;
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
  const rm = el('button', 'btn small quiet', 'Remove last');
  rm.type = 'button';
  rm.title = `Remove game ${n} from the series`;
  rm.onclick = removeLastGame;
  const rs = el('button', 'btn small quiet', 'Reset');
  rs.type = 'button';
  rs.title = 'Reset the series to game 1';
  rs.onclick = resetSeries;
  box.append(rm, rs);
}

/* ---------- render all ---------- */

function render() {
  hideWhy();
  renderSetup();
  SIDES.forEach(renderTeam);
  renderStatus();
  renderGrid();
  renderRecap();
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
  $('sortToggle').addEventListener('click', () => {
    S.ui.sort = S.ui.sort === 'az' ? 'prob' : 'az';
    save(); renderSetup(); renderGrid();
    $('grid').scrollTop = 0;
  });
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

  const preds = $('predList');
  preds.addEventListener('mouseover', (e) => { const li = e.target.closest('.pred'); if (li) showWhy(li); });
  preds.addEventListener('mouseleave', hideWhy);
  preds.addEventListener('focusin', (e) => { const li = e.target.closest('.pred'); if (li) showWhy(li); });
  preds.addEventListener('focusout', hideWhy);
  preds.addEventListener('scroll', hideWhy);

  const pop = $('caveatPop');
  pop.textContent = META.win_note;
  const setupPop = $('setupPop');
  const togglePop = (show) => {
    pop.hidden = show === undefined ? !pop.hidden : !show;
    const b = $('caveatBtn');
    if (b) b.setAttribute('aria-expanded', String(!pop.hidden));
  };
  const toggleSetup = (show) => {
    setupPop.hidden = show === undefined ? !setupPop.hidden : !show;
    $('setupBtn').setAttribute('aria-expanded', String(!setupPop.hidden));
  };
  $('winNums').addEventListener('click', (e) => {
    if (e.target.closest('.caveat')) { e.stopPropagation(); togglePop(); }
  });
  $('setupBtn').addEventListener('click', (e) => { e.stopPropagation(); toggleSetup(); });
  const aboutPop = $('aboutPop');
  const toggleAbout = (show) => {
    aboutPop.hidden = show === undefined ? !aboutPop.hidden : !show;
    $('aboutBtn').setAttribute('aria-expanded', String(!aboutPop.hidden));
  };
  $('aboutBtn').addEventListener('click', (e) => { e.stopPropagation(); toggleAbout(); });
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target)) togglePop(false);
    if (!setupPop.hidden && !setupPop.contains(e.target)) toggleSetup(false);
    if (!aboutPop.hidden && !aboutPop.contains(e.target)) toggleAbout(false);
    const wl = document.querySelector('.warn-list');
    if (wl && !wl.hidden && !wl.contains(e.target)) wl.hidden = true;
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { togglePop(false); toggleSetup(false); toggleAbout(false); }
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
  const through = new Date(META.data_through + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  $('brand').title = `Model data through ${through}`;
  $('aboutData').textContent = `It knows ${META.n_games.toLocaleString()} games played up to ${through}; nothing after that date.`;
  $('dataThrough').textContent = `Data to ${through}`;

  S = sanitize(loadStored());
  save();
  SIDES.forEach(buildTeamColumn);
  buildGrid();
  bindEvents();
  render();
  booted = true;
  schedulePredict();
}

boot();
