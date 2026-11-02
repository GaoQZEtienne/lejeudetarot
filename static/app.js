'use strict';

const $ = id => document.getElementById(id);
const el = (tag, className = '', text = '') => {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
};
const SUITS = {S: '♠', H: '♥', D: '♦', C: '♣', T: '✦', F: '✧'};
const SUIT_NAMES = {S: '黑桃', H: '红心', D: '方块', C: '梅花', T: '将牌', F: '愚人'};
const PHASES = {lobby: '等待入座', bidding: '叫牌', discard: '庄家扣牌', playing: '出牌', collecting: '收墩', finished: '本局结算'};
const POSITIONS = ['bottom', 'right', 'top', 'left'];
let state = null, ws = null, pending = null, selection = [], selectionKey = '', retryTimer;
let connected = false, retryCount = 0, toastTimer, clockOffset = 0, token = '';
let intentionalClose = false;
let leaving = false;
try { token = localStorage.getItem('tarot-seat-token') || ''; } catch (_) { /* handled on join */ }
try { intentionalClose = sessionStorage.getItem('tarot-on-home') === '1'; } catch (_) {}

function refreshOccupancy() {
  fetch('/api/lobby').then(r => r.json()).then(data => {
    $('occupancy').textContent = `${data.occupied} / 4 已入座`;
  }).catch(() => {});
}

function showHome() {
  intentionalClose = true;
  clearTimeout(retryTimer);
  try { sessionStorage.setItem('tarot-on-home', '1'); } catch (_) {}
  // Detach first: late messages and close events must not re-open the table.
  const socket = ws;
  ws = null;
  if (socket) socket.close();
  connected = false;
  pending = null;
  state = null;
  selection = [];
  selectionKey = '';
  retryCount = 0;
  clearTimeout(toastTimer);
  $('toast').hidden = true;
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  $('game').hidden = true;
  $('welcome').hidden = false;
  $('offline-banner').hidden = true;
  $('join-form').hidden = Boolean(token);
  $('resume-button').hidden = !token;
  $('resume-note').hidden = !token;
  $('join-error').textContent = '';
  $('connection').textContent = '私人四人牌桌';
  $('connection').className = 'connection';
  refreshOccupancy();
  window.scrollTo(0, 0);
  (token ? $('resume-button') : $('nickname')).focus({preventScroll: true});
}

function forgetSeat() {
  try {
    if (localStorage.getItem('tarot-seat-token') === token) localStorage.removeItem('tarot-seat-token');
  } catch (_) {}
  token = '';
  $('join-form').reset();
  showHome();
}

async function leaveRoom() {
  if (leaving) return;
  leaving = true;
  $('leave-button').disabled = true;
  const departingToken = token;
  try {
    const response = await fetch('/api/leave', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: departingToken}), signal: AbortSignal.timeout(15000)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '暂时无法离开房间');
    // A released message may have already returned this tab to the home page.
    if (token !== departingToken) return;
    if (result.released) forgetSeat();
    else showHome();
  } catch (error) {
    if (token === departingToken) toast('离开尚未确认，请恢复连接后重试。' + (error.message || ''));
  } finally {
    leaving = false;
    $('leave-button').disabled = false;
    if (state) { renderHand(); renderActions(); renderVote(); }
  }
}

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
}

function cardName(card) {
  if (card === 'F') return '愚人 Excuse';
  const rank = Number(card.slice(1));
  return SUIT_NAMES[card[0]] + ' ' + (card[0] === 'T' ? rank : ({11: 'V', 12: 'C', 13: 'D', 14: 'R'}[rank] || rank));
}

function cardElement(card, interactive = false) {
  const suit = card[0], rank = Number(card.slice(1));
  const mark = card === 'F' ? '★' : suit === 'T' ? String(rank) : ({11: 'V', 12: 'C', 13: 'D', 14: 'R'}[rank] || String(rank));
  const node = el(interactive ? 'button' : 'div', 'playing-card');
  if (interactive) node.type = 'button';
  node.classList.toggle('red', suit === 'H' || suit === 'D');
  node.classList.toggle('trump', suit === 'T');
  node.classList.toggle('fool', suit === 'F');
  node.setAttribute('aria-label', cardName(card));
  node.dataset.card = card;
  node.append(el('span', 'card-corner', mark), el('span', 'card-symbol', SUITS[suit]),
    el('span', 'card-label', card === 'F' ? 'EXCUSE' : suit === 'T' ? (rank === 1 ? 'PETIT' : 'ATOUT') : ''),
    el('span', 'card-corner bottom-corner', mark));
  return node;
}

function name(seat) { return state?.players[seat]?.name || '空位'; }
function pos(seat) { return POSITIONS[(seat - state.seat + 4) % 4]; }
function signed(value) { return value > 0 ? '+' + value : String(value); }
function actionButton(text, action, payload = {}, enabled = true, primary = true) {
  const button = el('button', 'button ' + (primary ? 'primary' : 'quiet'), text);
  button.disabled = !enabled || !connected || Boolean(pending) || leaving;
  button.addEventListener('click', () => send(action, payload));
  return button;
}

function send(action, payload = {}) {
  if (!connected || pending || leaving || !state) return;
  const id = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
  pending = id;
  ws.send(JSON.stringify({id, action, payload, revision: state.revision}));
  renderHand();
  renderActions();
  renderVote();
}

function connect() {
  if (!token) return;
  clearTimeout(retryTimer);
  intentionalClose = false;
  try { sessionStorage.removeItem('tarot-on-home'); } catch (_) {}
  $('resume-note').hidden = true;
  $('join-error').textContent = '';
  $('connection').textContent = '连接中';
  $('connection').className = 'connection offline';
  const socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws = socket;
  socket.addEventListener('open', () => {
    if (socket !== ws) { socket.close(); return; }
    socket.send(JSON.stringify({token}));
  });
  socket.addEventListener('message', event => {
    if (socket !== ws) return;
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === 'state') {
      connected = true;
      retryCount = 0;
      clockOffset = message.server_time * 1000 - Date.now();
      state = message;
      const key = `${state.round}:${state.phase}:${state.turn}`;
      if (key !== selectionKey) { selection = []; selectionKey = key; }
      if (state.phase === 'discard' && state.seat === state.taker) selection = [...state.draft];
      else selection = selection.filter(c => state.legal.includes(c));
      render();
    } else if (message.type === 'ack' || message.type === 'error') {
      if (message.id === pending) pending = null;
      if (message.type === 'error') toast(message.message);
      if (state) { renderHand(); renderActions(); renderVote(); }
    } else if (message.type === 'released') {
      forgetSeat();
    } else if (message.type === 'auth_error') {
      forgetSeat();
      $('join-error').textContent = message.message;
    }
  });
  socket.addEventListener('close', () => {
    if (socket !== ws) return;
    connected = false;
    pending = null;
    $('connection').textContent = '连接已断开';
    $('connection').className = 'connection offline';
    $('offline-banner').hidden = !state;
    if (state) { renderHand(); renderActions(); renderVote(); }
    if (!intentionalClose && token) {
      retryTimer = setTimeout(connect, Math.min(1000 * 2 ** retryCount++, 10000));
      $('join-error').textContent = '正在恢复连接，请稍候…';
    }
  });
  socket.addEventListener('error', () => socket.close());
}

function renderSeats() {
  $('seats').replaceChildren();
  state.players.forEach((player, i) => {
    const seat = el('div', 'seat ' + pos(i));
    seat.classList.toggle('active', state.turn === i);
    seat.classList.toggle('empty', !player);
    seat.classList.toggle('offline', Boolean(player && !player.online));
    seat.append(el('div', 'avatar', player ? player.name.slice(0, 1) : '+'));
    const info = el('div');
    const label = el('div', 'seat-name', player ? player.name : '等待好友');
    label.title = player?.name || '等待好友';
    if (player) label.append(el('span', 'seat-role', (i === state.seat ? '你 ' : '') + (i === state.taker && player.played_this_round ? '庄家' : i === state.host ? '房主' : '')));
    info.append(label, el('div', 'seat-detail', !player ? '空座位' : !player.online ? '离线 · 保留座位' :
      state.phase === 'lobby' ? '已入座' : state.phase === 'bidding' && state.passed.includes(i) ? '不叫' : `${player.count} 张 · ${player.tricks} 墩`));
    seat.append(info);
    $('seats').append(seat);
  });
}

function renderTable() {
  const center = $('table-center');
  center.replaceChildren();
  $('trick-cards').replaceChildren();
  center.hidden = state.trick.length > 0;
  if (!center.hidden) {
    const occupied = state.players.filter(Boolean).length;
    const online = state.players.filter(p => p?.online).length;
    let title, detail;
    if (state.phase === 'lobby') { title = `${occupied} / 4 已入座`; detail = online === 4 ? '四人到齐，等待房主开始' : '把同一个链接分享给好友'; }
    else if (state.phase === 'bidding') { title = 'Petit，还是不叫？'; detail = `等待 ${name(state.turn)} 叫牌`; }
    else if (state.phase === 'discard') { title = '庄家正在扣牌'; detail = '从原手牌中扣六张，再收入底牌'; }
    else if (state.phase === 'finished') { title = '本局结束'; detail = '最后一张落定，看看这局的收获'; }
    else { title = `第 ${state.trick_number} 墩`; detail = `${name(state.turn)} 先出`; }
    center.append(el('div', 'center-symbol', '♠'), el('h2', '', title), el('p', '', detail));
  }
  for (const play of state.trick) {
    const node = el('div', 'trick-play ' + pos(play.seat));
    node.append(cardElement(play.card), el('small', '', name(play.seat)));
    $('trick-cards').append(node);
  }
  $('last-open').disabled = !state.last_trick;
}

function renderHand() {
  if (!state) return;
  const scroll = $('hand').scrollLeft;
  $('hand').replaceChildren();
  const isDiscard = state.phase === 'discard' && state.seat === state.taker;
  const plain = state.hand.filter(c => c[0] !== 'T' && c !== 'F' && !c.endsWith('14'));
  const trumpQuota = Math.max(0, 6 - plain.length);
  const selectedTrumps = selection.filter(c => c[0] === 'T').length;
  for (const card of state.hand) {
    const node = cardElement(card, true);
    const selected = selection.includes(card);
    node.classList.toggle('selected', selected);
    node.setAttribute('aria-pressed', String(selected));
    const limit = isDiscard && !selected && (selection.length >= 6 || (card[0] === 'T' && selectedTrumps >= trumpQuota));
    node.disabled = !connected || Boolean(pending) || leaving || !state.legal.includes(card) || limit;
    node.title = state.reasons[card] || (limit ? '已选满六张或已达到必要扣将数量' : cardName(card));
    node.addEventListener('click', () => {
      if (isDiscard) {
        selection = selected ? selection.filter(c => c !== card) : [...selection, card];
        send('draft', {cards: selection});
      } else {
        selection = selected ? [] : [card];
        renderHand();
        renderActions();
      }
    });
    $('hand').append(node);
  }
  if (!state.hand.length) $('hand').append(el('p', 'empty-hand', state.phase === 'finished' ? '本局手牌已出完' : '等待发牌，祝你有一手好牌。'));
  $('hand').scrollLeft = scroll;
  $('hand-count').textContent = state.hand.length + ' 张';
  $('my-role').textContent = !state.players[state.seat]?.played_this_round ? '等待开局' : state.taker === null ? '尚未定庄' : state.taker === state.seat ? '庄家 · 一对三' : '防守方';
  $('hand-hint').textContent = !connected ? '正在重连，恢复后即可继续操作。' : isDiscard ? '只能选原手牌；国王、Bout 与非必要将牌不可扣。' :
    state.phase === 'playing' && state.turn === state.seat ? '亮起的牌可以出，点选后确认。出将无需压大。' :
    state.phase === 'bidding' ? '看看手牌，决定是否成为庄家。' : state.phase === 'finished' ? '结算已保存，等待房主开始下一局。' : '轮到你时，可出的牌会亮起。';
}

function renderActions() {
  $('actions').replaceChildren();
  let note = '';
  if (state.phase === 'lobby' || state.phase === 'finished') {
    const ready = state.players.every(p => p?.online);
    if (state.seat === state.host) $('actions').append(actionButton(state.phase === 'finished' ? '开始下一局 →' : '开始游戏 →', 'start', {}, ready));
    note = ready ? (state.seat === state.host ? '四人到齐，可以开始。' : '等待房主开始。') : '四位玩家全部在线后即可开始。';
  } else if (state.turn !== state.seat) {
    note = state.turn === null ? '正在收墩…' : `等待 ${name(state.turn)} 操作`;
  } else if (state.phase === 'bidding') {
    note = '叫牌后，你将独自对抗另外三位。';
    $('actions').append(actionButton('不叫', 'bid', {bid: 'pass'}, true, false), actionButton('Petit ×1', 'bid', {bid: 'take'}));
  } else if (state.phase === 'discard') {
    note = `已选 ${selection.length} / 6 张`;
    $('actions').append(actionButton('确认扣六张', 'discard', {cards: selection}, selection.length === 6));
  } else if (state.phase === 'playing') {
    note = selection.length ? '已选 ' + cardName(selection[0]) : '选择一张亮起的牌';
    $('actions').append(actionButton('出牌 →', 'play', {card: selection[0]}, selection.length === 1));
  }
  $('selection-note').textContent = pending ? '正在确认…' : note;
}

function renderResult() {
  const panel = $('result');
  panel.hidden = !state.result;
  panel.replaceChildren();
  if (!state.result) return;
  const r = state.result;
  const participants = r.players || state.players;
  panel.append(el('span', 'eyebrow', 'FIN DE LA PARTIE'));
  const title = el('h2', '', r.won ? `${participants[state.taker].name} 完成了合同` : '防守方赢得了这一局');
  title.id = 'result-title';
  panel.append(title);
  const stats = el('div', 'result-stats');
  for (const [label, value] of [['庄家牌分', r.points], ['目标分', r.target], ['庄家 Bout', r.bouts]]) {
    const item = el('div', 'result-stat', label);
    item.append(el('b', '', String(value)));
    stats.append(item);
  }
  panel.append(stats, el('p', 'result-note', `基础 ${signed(r.base)} · 末将 ${signed(r.petit)} · 大满贯 ${signed(r.slam)} = 每份 ${signed(r.unit)} 分。${r.excuse_note} 庄家愚人补偿净额 ${signed(r.excuse_adjustment)} 分。`));
  const table = el('table');
  const head = el('thead'), hr = el('tr');
  for (const label of ['玩家', '身份', '本局', '累计']) hr.append(el('th', '', label));
  head.append(hr); table.append(head);
  const body = el('tbody');
  participants.forEach((p, i) => {
    const row = el('tr');
    row.append(el('td', '', p.name + (i === state.seat && state.players[i]?.played_this_round ? '（你）' : '')), el('td', '', i === state.taker ? '庄家' : '防守'),
      el('td', r.scores[i] >= 0 ? 'positive' : 'negative', signed(r.scores[i])), el('td', '', signed(p.total)));
    body.append(row);
  });
  table.append(body); panel.append(table);
  panel.append(el('p', 'result-note', `庄家扣牌：${state.discard.map(cardName).join('、')}。两方总牌分 ${r.points + r.defense_points}，四人成绩合计 ${r.scores.reduce((a, b) => a + b, 0)}。`));
}

function renderVote() {
  if (!state) return;
  const active = !['lobby', 'finished'].includes(state.phase);
  const votes = state.restart_votes || [];
  const voted = votes.includes(state.seat);
  $('restart-panel').hidden = !active;
  $('restart-status').textContent = votes.length ? `${votes.map(name).join('、')} 赞成重开（${votes.length}/2）` : '两人赞成即重新发牌，本局不计分。';
  $('restart-button').textContent = voted ? `已赞成（${votes.length}/2）` : votes.length ? `赞成重开（${votes.length}/2）` : '重开投票';
  $('restart-button').disabled = !connected || Boolean(pending) || leaving || voted;
  $('leave-button').title = active ? '回到初始界面并保留局内座位' : '释放座位并回到填写昵称的首页';
}

function render() {
  $('welcome').hidden = true;
  $('game').hidden = false;
  $('offline-banner').hidden = connected;
  $('connection').textContent = connected ? '已连接 · 四人房间' : '正在重连';
  $('connection').className = 'connection ' + (connected ? 'online' : 'offline');
  $('round-label').textContent = state.round ? `第 ${state.round} 次发牌` : '等待开局';
  $('phase-label').textContent = PHASES[state.phase];
  $('trick-label').textContent = ['playing', 'collecting'].includes(state.phase) ? `${state.trick_number} / 18 墩` + (state.lead ? ' · ' + SUIT_NAMES[state.lead] : '') : '';
  $('turn-label').textContent = state.turn === null ? state.phase === 'collecting' ? '收墩展示' : '' : state.turn === state.seat ? '轮到你了' : name(state.turn) + ' 的回合';
  $('notice').textContent = state.notice;
  renderSeats(); renderTable(); renderHand(); renderActions(); renderResult(); renderVote();
  $('kitty-panel').hidden = state.phase !== 'discard';
  $('kitty-cards').replaceChildren(...state.kitty.map(c => cardElement(c)));
  $('kitty-note').textContent = state.seat === state.taker ? '这六张牌不能扣掉，将在扣牌后全部收入。' : '庄家将从原手牌扣六张，再收入这些底牌。';
  $('exposed-trumps').hidden = !state.exposed_trumps.length;
  $('exposed-trumps').textContent = '庄家必要扣下的将牌：' + state.exposed_trumps.map(cardName).join('、');
  updateClock();
}

function updateClock() {
  if (!state?.deadline) { $('timer').textContent = '—'; return; }
  const remaining = Math.max(0, Math.ceil((state.deadline * 1000 - Date.now() - clockOffset) / 1000));
  $('timer').textContent = remaining + 's';
  $('timer').classList.toggle('urgent', remaining <= 8 && state.phase !== 'collecting');
}
setInterval(updateClock, 250);

$('join-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('join-error').textContent = '';
  $('join-button').disabled = true;
  try {
    // Verify storage before reserving an irreplaceable seat.
    localStorage.setItem('tarot-storage-check', '1');
    localStorage.removeItem('tarot-storage-check');
    const response = await fetch('/api/join', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: $('nickname').value.trim()})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '暂时无法入座');
    token = result.token;
    localStorage.setItem('tarot-seat-token', token);
    $('join-form').hidden = true;
    $('resume-button').hidden = false;
    connect();
  } catch (error) { $('join-error').textContent = error.message || '无法连接，请稍后再试'; }
  finally { $('join-button').disabled = false; }
});
$('resume-button').addEventListener('click', () => {
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
});
$('leave-button').addEventListener('click', leaveRoom);
$('restart-button').addEventListener('click', () => send('restart_vote', {round: state.round}));
$('invite-button').addEventListener('click', async () => {
  const link = location.origin + '/';
  try { await navigator.clipboard.writeText(link); toast('邀请链接已复制，发给另外三位好友即可。'); }
  catch (_) { toast('邀请链接：' + link); }
});
$('rules-open').addEventListener('click', () => $('rules-dialog').showModal());
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
$('last-open').addEventListener('click', () => {
  if (!state?.last_trick) return;
  const row = el('div', 'last-plays');
  const previousName = seat => state.result?.players?.[seat]?.name || name(seat);
  for (const play of state.last_trick.plays) {
    const item = el('div');
    item.append(cardElement(play.card), el('p', '', previousName(play.seat)));
    row.append(item);
  }
  $('last-content').replaceChildren(row, el('p', 'result-note', previousName(state.last_trick.winner) + ' 赢得这一墩。'));
  $('last-dialog').showModal();
});
window.addEventListener('online', () => { if (!intentionalClose && token && (!ws || ws.readyState === WebSocket.CLOSED)) connect(); });
window.addEventListener('storage', event => {
  if (event.key === 'tarot-seat-token' && token && event.oldValue === token && event.newValue !== token) {
    token = '';
    $('join-form').reset();
    showHome();
  }
});
setInterval(() => { if (!$('welcome').hidden && !document.hidden) refreshOccupancy(); }, 3000);
if (intentionalClose) {
  showHome();
} else if (token) {
  $('join-form').hidden = true;
  $('resume-button').hidden = false;
  connect();
} else {
  refreshOccupancy();
}
